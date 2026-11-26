"""Async Design Guidance (DG) summary service with bilingual deterministic fallback."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from server.core.llm_client import LLMError
from server.core.settings import redact_secrets
from server.domain.blueprints import DGContext, build_dg_fallback_summary
from server.domain.errors import DomainError, ValidationError
from server.i18n.messages import normalize_language


class DGGuidePipelineProtocol(Protocol):
    """Protocol satisfied by server.llm.pipelines.dg_guide.DGGuidePipeline."""

    async def create_summary(
        self,
        context: DGContext | Mapping[str, Any],
        *,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 1,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class DGGuideExecutionResult:
    """Normalized DG guide summary response plus header metadata."""

    value: dict[str, Any]
    source: str
    request_id: str = ""
    attempts_used: int = 0
    latency_ms: int = 0


def _extract_payload_dict(raw: Any) -> dict[str, Any]:
    if hasattr(raw, "to_wire_dict") and callable(raw.to_wire_dict):
        return dict(raw.to_wire_dict())
    if isinstance(raw, Mapping):
        if "value" in raw and isinstance(raw["value"], Mapping):
            return dict(raw["value"])
        return dict(raw)
    if hasattr(raw, "value") and isinstance(raw.value, Mapping):
        return dict(raw.value)
    if hasattr(raw, "model_dump"):
        return raw.model_dump(by_alias=True)
    raise ValueError("Unsupported DG pipeline result type")


class DGGuideService:
    """Wraps DGGuidePipeline with language-aware deterministic fallback."""

    def __init__(
        self,
        pipeline: DGGuidePipelineProtocol | None = None,
        *,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        self._pipeline = pipeline
        self._extra_secrets = extra_secrets

    async def create_summary(
        self,
        raw_context: DGContext | Mapping[str, Any],
        *,
        request_id: str = "",
        deadline: float | None = None,
        max_attempts: int = 1,
    ) -> DGGuideExecutionResult:
        """Validate 4 neutral DG answers and return LLM or deterministic summary."""
        try:
            dg_ctx = (
                raw_context
                if isinstance(raw_context, DGContext)
                else DGContext.model_validate(raw_context)
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                "INVALID_DG_CONTEXT",
                "error.validation_failed",
                message=f"Invalid DG neutral answer: {exc}",
            ) from exc

        locale = normalize_language(dg_ctx.language)
        ctx_dict = dg_ctx.model_dump(by_alias=True)
        ctx_dict["language"] = locale

        if self._pipeline is None:
            fallback = build_dg_fallback_summary(ctx_dict, reason="llm_unconfigured")
            return DGGuideExecutionResult(
                value=fallback,
                source=str(fallback["source"]),
                request_id=request_id,
            )

        try:
            raw_result = await self._pipeline.create_summary(
                ctx_dict,
                deadline=deadline,
                request_id=request_id,
                max_attempts=max_attempts,
            )
            payload = _extract_payload_dict(raw_result)
            source = str(payload.get("source") or "llm").strip() or "llm"
            result_dict: dict[str, Any] = {
                "summary": str(payload["summary"]).strip(),
                "recommendedDifficulty": str(payload["recommendedDifficulty"]).strip(),
                "difficultyRationale": str(payload["difficultyRationale"]).strip(),
                "recommendedLayout": str(payload["recommendedLayout"]).strip(),
                "layoutRationale": str(payload["layoutRationale"]).strip(),
                "source": source,
            }
            if payload.get("fallbackReason"):
                result_dict["fallbackReason"] = redact_secrets(
                    str(payload["fallbackReason"]), self._extra_secrets
                )[:400]
            return DGGuideExecutionResult(
                value=result_dict,
                source=source,
                request_id=str(getattr(raw_result, "request_id", "") or request_id),
                attempts_used=int(getattr(raw_result, "attempts_used", 0) or 0),
                latency_ms=int(getattr(raw_result, "latency_ms", 0) or 0),
            )
        except (LLMError, DomainError, ValueError, KeyError, TypeError) as exc:
            safe_reason = redact_secrets(str(exc), self._extra_secrets)[:400]
            fallback = build_dg_fallback_summary(ctx_dict, reason=safe_reason)
            return DGGuideExecutionResult(
                value=fallback,
                source=str(fallback["source"]),
                request_id=request_id,
            )
