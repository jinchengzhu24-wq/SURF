"""Native-async 8000 DG Guide summary pipeline with aligned partner prefix & language support."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import (
    LLMError,
    LLMTransport,
    LLMValidationError,
    call_structured,
)
from server.domain.blueprints import (
    DG_DIFFICULTY_ANSWERS,
    DG_LAYOUT_ANSWERS,
    DGContext,
    DifficultyLevel,
    LayoutStyle,
    build_dg_fallback_summary,
    dg_preference_allowed_scores,
)
from server.i18n.messages import normalize_language
from server.llm.prompts.dg_guide import build_dg_guide_messages
from server.llm.schemas import DGGuideSummaryPayload

# Precompiled module-level regexes supporting both EN and unspaced zh-CN sentences
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)|[。！？]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])\s*")
_EN_INTERJECTION_RE = re.compile(
    r"^(?:Got it|Nice|Sure|Okay|Alright|Sounds good)!\s+", re.IGNORECASE
)
_ZH_INTERJECTION_RE = re.compile(r"^(?:明白|好的|好呀|收到)[！!]\s*")
_EN_FIRST_PERSON_RE = re.compile(r"\bI(?:['’](?:d|m|ve|ll))?\b", re.IGNORECASE)

# Aligned with dg_guide prompt (NO "My read is" / "Your choices suggest" contradiction!)
_EN_PARTNER_PREFIXES: tuple[str, ...] = (
    "got it",
    "nice!",
    "sure!",
    "okay!",
    "alright!",
    "sounds good!",
    "let's try",
    "let us try",
    "i'd try",
    "how about",
    "i picture",
    "i'm picturing",
    "i think",
    "i feel",
)
_ZH_PARTNER_PREFIXES: tuple[str, ...] = (
    "明白",
    "好的",
    "好呀",
    "收到",
    "我先",
    "我会",
    "咱们",
    "我们先",
    "试试",
)


@dataclass(frozen=True, slots=True)
class DGGuideResult:
    """Validated DG guide summary (from LLM or deterministic fallback)."""

    summary: str
    recommended_difficulty: str
    difficulty_rationale: str
    recommended_layout: str
    layout_rationale: str
    source: str
    fallback_reason: str = ""
    attempts_used: int = 0
    request_id: str = ""
    latency_ms: int = 0

    def to_wire_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": self.summary,
            "recommendedDifficulty": self.recommended_difficulty,
            "difficultyRationale": self.difficulty_rationale,
            "recommendedLayout": self.recommended_layout,
            "layoutRationale": self.layout_rationale,
            "source": self.source,
        }
        if self.fallback_reason:
            payload["fallbackReason"] = self.fallback_reason
        return payload


def _fit_within_limit(text: str, maximum_length: int, *, preserve_terminal_punct: bool) -> str:
    clean = str(text or "").strip()
    if len(clean) <= maximum_length:
        return clean
    if preserve_terminal_punct:
        terminal = clean[-1] if clean[-1] in ".!?。！？" else "."
        cutoff = max(1, maximum_length - 1)
        trimmed = clean[:cutoff].rstrip(" ,;:-，；：")
        if " " in trimmed:
            candidate = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:-")
            if len(candidate) >= 24:
                trimmed = candidate
        return trimmed.rstrip(".!?。！？") + terminal

    sentences = _SENTENCE_SPLIT_RE.split(clean)
    if len(sentences) >= 3:
        kept: list[str] = []
        for sentence in sentences:
            candidate = " ".join([*kept, sentence]).strip()
            if len(candidate) <= maximum_length:
                kept.append(sentence)
            else:
                break
        if len(kept) >= 2:
            return " ".join(kept)
    cutoff = max(1, maximum_length - 1)
    return clean[:cutoff].rstrip(" ,;:-.!?。！？") + "."


def normalize_dg_partner_summary(summary: str, language: str = "en") -> str:
    """Normalize DG summary with an aligned warm first-person partner prefix."""
    locale = normalize_language(language)
    clean = " ".join(str(summary or "").split()).strip()
    if not clean:
        raise ValueError("DG guide summary is required.")

    if locale == "zh-CN":
        normalized = clean if clean.startswith(_ZH_PARTNER_PREFIXES) else f"明白，{clean}"
        interjection = bool(_ZH_INTERJECTION_RE.match(normalized))
    else:
        normalized = (
            clean if clean.lower().startswith(_EN_PARTNER_PREFIXES) else f"Got it, {clean}"
        )
        interjection = bool(_EN_INTERJECTION_RE.match(normalized))

    normalized = _fit_within_limit(normalized, 480, preserve_terminal_punct=False)
    sentence_count = len(_SENTENCE_END_RE.findall(normalized))
    max_sentences = 4 if interjection else 3
    if sentence_count < 2 or sentence_count > max_sentences:
        raise ValueError("DG guide summary must be 2 or 3 concise sentences.")
    return normalized


def normalize_dg_partner_rationale(rationale: str, label: str, language: str = "en") -> str:
    """Validate one-sentence first-person rationale honoring the requested language."""
    locale = normalize_language(language)
    clean = " ".join(str(rationale or "").split()).strip()
    min_len = 12 if locale == "zh-CN" else 30
    sentence_count = len(_SENTENCE_END_RE.findall(clean))
    if len(clean) < min_len or sentence_count != 1:
        raise ValueError(f"DG guide {label} rationale must be exactly one sentence.")

    if locale == "zh-CN":
        if "我" not in clean:
            clean = f"我把这组选择理解为：{clean}"
    elif not _EN_FIRST_PERSON_RE.search(clean):
        clean = f"I'd connect this to: {clean}"
    return _fit_within_limit(clean, 320, preserve_terminal_punct=True)


class DGGuidePipeline:
    """Native-async 8000 DG guide summary pipeline with deterministic fallback."""

    def __init__(
        self,
        transport: LLMTransport,
        *,
        model: str,
        clock: Clock | None = None,
        extra_body: Mapping[str, Any] | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.transport = transport
        self.model = model
        self.clock = clock if clock is not None else SYSTEM_CLOCK
        self.extra_body = dict(extra_body) if extra_body is not None else None
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def create_summary(
        self,
        context: DGContext | Mapping[str, Any],
        *,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 1,
    ) -> DGGuideResult:
        """Generate a validated DG summary or cleanly fall back to deterministic copy."""
        started = self.clock.perf_counter()
        ctx_model = context if isinstance(context, DGContext) else DGContext.model_validate(context)
        ctx = ctx_model.model_dump(by_alias=True)
        locale = normalize_language(ctx.get("language"))
        messages = build_dg_guide_messages(ctx)

        diff_scores = dg_preference_allowed_scores(
            ctx["firstMovePreference"], ctx["pushPlanningPreference"], DG_DIFFICULTY_ANSWERS
        )
        layout_scores = dg_preference_allowed_scores(
            ctx["spacePreference"], ctx["routeRhythmPreference"], DG_LAYOUT_ANSWERS
        )
        allowed_diff = (
            {DifficultyLevel.RANDOM.value}
            if diff_scores is None
            else {("Easy", "Medium", "Hard")[s] for s in diff_scores}
        )
        allowed_layout = (
            {LayoutStyle.RANDOM.value}
            if layout_scores is None
            else {("Compact", "Balanced", "Open")[s] for s in layout_scores}
        )
        validated_holder: dict[str, str] = {}

        def _validate(parsed: DGGuideSummaryPayload) -> DGGuideSummaryPayload:
            if parsed.recommended_difficulty not in allowed_diff:
                raise LLMValidationError(
                    "DG_DIFFICULTY_RULE_VIOLATION",
                    f"difficulty={parsed.recommended_difficulty} not in {sorted(allowed_diff)}.",
                )
            if parsed.recommended_layout not in allowed_layout:
                raise LLMValidationError(
                    "DG_LAYOUT_RULE_VIOLATION",
                    f"layout={parsed.recommended_layout} not in {sorted(allowed_layout)}.",
                )
            try:
                validated_holder["summary"] = normalize_dg_partner_summary(parsed.summary, locale)
                validated_holder["difficultyRationale"] = normalize_dg_partner_rationale(
                    parsed.difficulty_rationale, "difficulty", locale
                )
                validated_holder["layoutRationale"] = normalize_dg_partner_rationale(
                    parsed.layout_rationale, "layout", locale
                )
            except ValueError as exc:
                raise LLMValidationError("DG_SUMMARY_INVALID", str(exc)) from exc
            return parsed

        try:
            res = await call_structured(
                self.transport,
                task="dg_guide_summary",
                model=self.model,
                messages=messages,
                response_model=DGGuideSummaryPayload,
                validator=_validate,
                deadline=deadline,
                max_attempts=max_attempts,
                temperature=0.2,
                max_tokens=2048,
                attempt_timeout_seconds=self.timeout_seconds,
                extra_body=self.extra_body,
                clock=self.clock,
                request_id=request_id,
            )
            return DGGuideResult(
                summary=validated_holder["summary"],
                recommended_difficulty=str(res.parsed.recommended_difficulty),
                difficulty_rationale=validated_holder["difficultyRationale"],
                recommended_layout=str(res.parsed.recommended_layout),
                layout_rationale=validated_holder["layoutRationale"],
                source="llm",
                attempts_used=res.attempts_used,
                request_id=res.request_id,
                latency_ms=res.latency_ms,
            )
        except LLMError as exc:
            fb = build_dg_fallback_summary(ctx, reason=str(exc))
            return DGGuideResult(
                summary=fb["summary"],
                recommended_difficulty=fb["recommendedDifficulty"],
                difficulty_rationale=fb["difficultyRationale"],
                recommended_layout=fb["recommendedLayout"],
                layout_rationale=fb["layoutRationale"],
                source="deterministic_fallback",
                fallback_reason=str(exc),
                attempts_used=exc.attempts_used,
                request_id=exc.request_id or request_id,
                latency_ms=round((self.clock.perf_counter() - started) * 1000),
            )
