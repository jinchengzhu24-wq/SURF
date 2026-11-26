"""Shared async OpenAI-compatible transport and strict structured LLM caller."""

from __future__ import annotations

import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from .clock import SYSTEM_CLOCK, Clock, remaining_seconds
from .settings import redact_secrets


class LLMError(Exception):
    """Base structured LLM error carrying code and retryability."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        request_id: str = "",
        attempts_used: int = 0,
        provider_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)
        self.request_id = str(request_id)
        self.attempts_used = int(attempts_used)
        self.provider_status = provider_status


class LLMValidationError(LLMError):
    """Raised when model output is empty, truncated, malformed JSON, or invalid."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str = "",
        attempts_used: int = 0,
    ) -> None:
        super().__init__(
            code,
            message,
            retryable=True,
            request_id=request_id,
            attempts_used=attempts_used,
        )


class UpstreamLLMError(LLMError):
    """Raised when the upstream OpenAI-compatible provider rejects or fails."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        request_id: str = "",
        attempts_used: int = 0,
        provider_status: int | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            retryable=retryable,
            request_id=request_id,
            attempts_used=attempts_used,
            provider_status=provider_status,
        )


class DeadlineExceededError(TimeoutError, LLMError):
    """Raised when the caller's monotonic deadline is exhausted."""

    def __init__(
        self,
        message: str = "LLM deadline exceeded.",
        *,
        request_id: str = "",
        attempts_used: int = 0,
    ) -> None:
        TimeoutError.__init__(self, message)
        LLMError.__init__(
            self,
            "DEADLINE_EXCEEDED",
            message,
            retryable=False,
            request_id=request_id,
            attempts_used=attempts_used,
        )


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """Normalized completion payload returned by an LLMTransport."""

    content: str
    finish_reason: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@runtime_checkable
class LLMTransport(Protocol):
    """Async completion transport protocol for production and fake test clients."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class StructuredCallResult[T: BaseModel]:
    """Validated result of a structured LLM call."""

    parsed: T
    raw_text: str
    model: str
    task: str
    attempts_used: int
    request_id: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)


def _reject_non_finite_json_constant(constant: str) -> Any:
    raise ValueError(f"Non-finite JSON constant is not allowed: {constant}")


def _unwrap_single_json_fence(raw: str) -> str:
    """Strip a single outer Markdown ``` or ```json fence without regex scraping."""
    text = str(raw or "").strip()
    if not text:
        return text
    lines = text.splitlines()
    if (
        len(lines) >= 2
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text


def _sanitize_schema_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(name or "structured_output"))
    return cleaned[:64] or "structured_output"


def _classify_provider_exception(
    exc: Exception,
    *,
    request_id: str,
    attempts_used: int,
    secrets_to_redact: tuple[str, ...],
) -> UpstreamLLMError:
    if isinstance(exc, APITimeoutError):
        return UpstreamLLMError(
            "UPSTREAM_TIMEOUT",
            "LLM provider timed out.",
            retryable=True,
            request_id=request_id,
            attempts_used=attempts_used,
            provider_status=504,
        )
    if isinstance(exc, RateLimitError):
        return UpstreamLLMError(
            "UPSTREAM_RATE_LIMIT",
            "LLM provider rate limited the request.",
            retryable=True,
            request_id=request_id,
            attempts_used=attempts_used,
            provider_status=429,
        )
    if isinstance(exc, APIConnectionError):
        detail = redact_secrets(str(exc), secrets_to_redact)[:240]
        return UpstreamLLMError(
            "UPSTREAM_CONNECTION_ERROR",
            f"Could not connect to LLM provider: {detail}" if detail else "Connection error.",
            retryable=True,
            request_id=request_id,
            attempts_used=attempts_used,
        )
    if isinstance(exc, APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0) or None
        if status in {401, 403}:
            return UpstreamLLMError(
                "UPSTREAM_AUTHENTICATION_FAILED",
                f"LLM provider rejected credentials (HTTP {status}).",
                retryable=False,
                request_id=request_id,
                attempts_used=attempts_used,
                provider_status=status,
            )
        retryable = status == 429 or (status is not None and status >= 500)
        code = "UPSTREAM_SERVER_ERROR" if retryable else "UPSTREAM_REQUEST_REJECTED"
        detail = redact_secrets(str(exc), secrets_to_redact)[:240]
        msg = f"LLM provider returned HTTP {status or 'error'}: {detail}".strip()
        return UpstreamLLMError(
            code,
            msg,
            retryable=retryable,
            request_id=request_id,
            attempts_used=attempts_used,
            provider_status=status,
        )
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamLLMError(
            "UPSTREAM_TIMEOUT",
            "HTTP transport timed out.",
            retryable=True,
            request_id=request_id,
            attempts_used=attempts_used,
            provider_status=504,
        )
    if isinstance(exc, httpx.TransportError):
        detail = redact_secrets(str(exc), secrets_to_redact)[:240]
        return UpstreamLLMError(
            "UPSTREAM_CONNECTION_ERROR",
            f"HTTP transport error: {detail}" if detail else "HTTP transport error.",
            retryable=True,
            request_id=request_id,
            attempts_used=attempts_used,
        )
    raise exc


class AsyncOpenAITransport:
    """AsyncOpenAI transport sharing one httpx.AsyncClient per (base_url, api_key).

    Designed for FastAPI async lifespans: never calls asyncio.run() and spawns no
    daemon threads. Call `await transport.close()` on shutdown.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        default_timeout_seconds: float = 60.0,
        max_connections: int = 40,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 60.0,
        response_format_mode: Literal["json_schema", "json_object", "prompt_only"] = "json_schema",
        default_extra_body: Mapping[str, Any] | None = None,
        fixed_temperature: float | None = None,
        use_max_completion_tokens: bool = False,
    ) -> None:
        self.default_api_key = str(api_key or "").strip()
        self.default_base_url = str(base_url or "").strip()
        self.default_timeout_seconds = max(0.1, float(default_timeout_seconds))
        self.response_format_mode: Literal["json_schema", "json_object", "prompt_only"] = (
            response_format_mode
        )
        self.default_extra_body: dict[str, Any] | None = (
            dict(default_extra_body) if default_extra_body is not None else None
        )
        self.fixed_temperature: float | None = (
            float(fixed_temperature) if fixed_temperature is not None else None
        )
        self.use_max_completion_tokens = bool(use_max_completion_tokens)
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )
        self._clients: dict[tuple[str, str], tuple[AsyncOpenAI, httpx.AsyncClient]] = {}

    @property
    def cached_client_count(self) -> int:
        return len(self._clients)

    def get_client(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AsyncOpenAI:
        resolved_base_url = str(base_url if base_url is not None else self.default_base_url).strip()
        resolved_api_key = str(api_key if api_key is not None else self.default_api_key).strip()
        if not resolved_api_key:
            raise UpstreamLLMError(
                "CONFIGURATION_ERROR",
                "LLM API key is missing.",
                retryable=False,
            )
        key = (resolved_base_url, resolved_api_key)
        cached = self._clients.get(key)
        if cached is not None:
            return cached[0]

        http_client = httpx.AsyncClient(limits=self._limits)
        client = AsyncOpenAI(
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            timeout=self.default_timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )
        self._clients[key] = (client, http_client)
        return client

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> TransportResponse:
        client = self.get_client(base_url=base_url, api_key=api_key)
        if timeout_seconds is not None:
            secs = max(0.001, float(timeout_seconds))
            scoped = client.with_options(
                timeout=httpx.Timeout(secs, connect=min(10.0, secs))
            )
        else:
            scoped = client

        effective_temperature = (
            self.fixed_temperature if self.fixed_temperature is not None else float(temperature)
        )
        token_limit = max(1, int(max_tokens))
        token_param = "max_completion_tokens" if self.use_max_completion_tokens else "max_tokens"
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [dict(m) for m in messages],
            "temperature": effective_temperature,
            token_param: token_limit,
            "stream": False,
        }
        if response_format is not None:
            kwargs["response_format"] = dict(response_format)
        merged_extra_body: dict[str, Any] = (
            dict(self.default_extra_body) if self.default_extra_body is not None else {}
        )
        if extra_body is not None:
            merged_extra_body.update(extra_body)
        if merged_extra_body:
            kwargs["extra_body"] = merged_extra_body

        response = await scoped.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMValidationError(
                "MODEL_EMPTY_RESPONSE",
                "The model returned no completion choices.",
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        usage = getattr(response, "usage", None)
        return TransportResponse(
            content=str(content or ""),
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client, http_client in clients:
            try:
                await client.close()
            finally:
                if not http_client.is_closed:
                    await http_client.aclose()


def _build_repair_messages(
    original_messages: Sequence[Mapping[str, Any]],
    raw_content: str,
    validation_error: str,
    *,
    error_code: str = "",
) -> list[dict[str, Any]]:
    repaired = [dict(message) for message in original_messages]
    if error_code == "MODEL_OUTPUT_TRUNCATED":
        if raw_content:
            snippet = f"{raw_content[:200]}..." if len(raw_content) > 200 else raw_content
            repaired.append(
                {
                    "role": "assistant",
                    "content": f"[truncated output ({len(raw_content)} chars): {snippet}]",
                }
            )
        repaired.append(
            {
                "role": "user",
                "content": (
                    "Your previous JSON response was truncated at max_tokens "
                    f"({len(raw_content)} chars). Keep every text field concise and compact, "
                    "avoid verbose explanations, and output one complete, closed, valid JSON "
                    "object matching the required schema only. "
                    f"Validation error: {validation_error[:400]}"
                ),
            }
        )
        return repaired
    if raw_content:
        repaired.append({"role": "assistant", "content": raw_content})
    repaired.append(
        {
            "role": "user",
            "content": (
                "Your previous JSON response failed schema or domain validation. "
                "Return valid JSON matching the required schema only. "
                f"Validation error: {validation_error[:800]}"
            ),
        }
    )
    return repaired


def _append_schema_to_messages(
    messages: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> list[dict[str, Any]]:
    compact_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    instruction = (
        "\n\nReturn a single valid JSON object matching this exact JSON Schema "
        "(use the exact property names, types, and enums; include all required fields "
        "and add no extra keys):\n"
        f"{compact_schema}"
    )
    updated = [dict(message) for message in messages]
    for message in updated:
        if str(message.get("role") or "") == "system":
            existing = str(message.get("content") or "")
            message["content"] = f"{existing}{instruction}" if existing else instruction.lstrip()
            return updated
    updated.insert(0, {"role": "system", "content": instruction.lstrip()})
    return updated


async def call_structured[T: BaseModel](
    transport: LLMTransport,
    *,
    task: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    response_model: type[T],
    validator: Callable[[T], T | None | Awaitable[T | None]] | None = None,
    deadline: float | None = None,
    max_attempts: int = 2,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    attempt_timeout_seconds: float | None = None,
    min_retry_budget_seconds: float = 1.0,
    retry_delay_seconds: float = 0.0,
    extra_body: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
    request_id: str = "",
    extra_secrets: Iterable[str] = (),
) -> StructuredCallResult[T]:
    """Execute a strictly schema-enforced structured LLM call with bounded retries.

    Never silently downgrades `json_schema` to `json_object` on HTTP 400/422,
    and never swallows internal Python bugs (`TypeError`, `KeyError`,
    `IndexError`, `AttributeError`) as retryable validation failures.
    """
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    resolved_request_id = request_id or uuid.uuid4().hex
    attempts_limit = max(1, int(max_attempts))
    secret_tuple = tuple(extra_secrets)
    default_key = getattr(transport, "default_api_key", "")
    if isinstance(default_key, str) and default_key.strip():
        secret_tuple = (*secret_tuple, default_key.strip())

    schema = response_model.model_json_schema()
    response_format_mode = str(
        getattr(transport, "response_format_mode", "json_schema") or "json_schema"
    )
    if response_format_mode == "json_object":
        response_format: dict[str, Any] | None = {"type": "json_object"}
        base_messages = _append_schema_to_messages(messages, schema)
    elif response_format_mode == "prompt_only":
        response_format = None
        base_messages = _append_schema_to_messages(messages, schema)
    else:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": _sanitize_schema_name(task or response_model.__name__),
                "strict": True,
                "schema": schema,
            },
        }
        base_messages = [dict(message) for message in messages]

    started_at = active_clock.perf_counter()
    current_messages = [dict(message) for message in base_messages]
    last_error: LLMError | None = None

    for attempt in range(1, attempts_limit + 1):
        remaining = remaining_seconds(deadline, clock=active_clock)
        if deadline is not None and remaining <= 0.0:
            raise DeadlineExceededError(
                f"LLM deadline exceeded before attempt {attempt}.",
                request_id=resolved_request_id,
                attempts_used=attempt - 1,
            )
        if attempt > 1 and deadline is not None and remaining < min_retry_budget_seconds:
            if last_error is not None:
                raise last_error
            raise DeadlineExceededError(
                "Insufficient remaining budget for retry.",
                request_id=resolved_request_id,
                attempts_used=attempt - 1,
            )

        effective_timeout = attempt_timeout_seconds
        if deadline is not None:
            effective_timeout = (
                remaining
                if effective_timeout is None
                else min(float(effective_timeout), remaining)
            )

        raw_content = ""
        try:
            response = await transport.complete(
                model=model,
                messages=current_messages,
                response_format=response_format,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=effective_timeout,
                extra_body=extra_body,
            )
        except (
            APITimeoutError,
            RateLimitError,
            APIConnectionError,
            APIStatusError,
            httpx.TimeoutException,
            httpx.TransportError,
        ) as exc:
            err = _classify_provider_exception(
                exc,
                request_id=resolved_request_id,
                attempts_used=attempt,
                secrets_to_redact=secret_tuple,
            )
            last_error = err
            if not err.retryable or attempt >= attempts_limit:
                raise err from exc
            if retry_delay_seconds > 0:
                await active_clock.async_sleep(retry_delay_seconds)
            continue
        except LLMValidationError as exc:
            err = LLMValidationError(
                exc.code,
                redact_secrets(str(exc), secret_tuple),
                request_id=resolved_request_id,
                attempts_used=attempt,
            )
            last_error = err
            if attempt >= attempts_limit:
                raise err from exc
            current_messages = _build_repair_messages(
                base_messages,
                raw_content,
                str(err),
                error_code=err.code,
            )
            if retry_delay_seconds > 0:
                await active_clock.async_sleep(retry_delay_seconds)
            continue
        except UpstreamLLMError as exc:
            err = UpstreamLLMError(
                exc.code,
                redact_secrets(str(exc), secret_tuple),
                retryable=exc.retryable,
                request_id=resolved_request_id,
                attempts_used=attempt,
                provider_status=exc.provider_status,
            )
            last_error = err
            if not err.retryable or attempt >= attempts_limit:
                raise err from exc
            if retry_delay_seconds > 0:
                await active_clock.async_sleep(retry_delay_seconds)
            continue

        raw_content = str(response.content or "")
        try:
            if response.finish_reason == "length":
                raise LLMValidationError(
                    "MODEL_OUTPUT_TRUNCATED",
                    "Model output was truncated at max_tokens.",
                    request_id=resolved_request_id,
                    attempts_used=attempt,
                )
            if not raw_content.strip():
                raise LLMValidationError(
                    "MODEL_EMPTY_RESPONSE",
                    "The model returned an empty response.",
                    request_id=resolved_request_id,
                    attempts_used=attempt,
                )
            unwrapped = _unwrap_single_json_fence(raw_content)
            try:
                decoded = json.loads(
                    unwrapped,
                    parse_constant=_reject_non_finite_json_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise LLMValidationError(
                    "MODEL_JSON_INVALID",
                    redact_secrets(f"Invalid JSON from model: {exc}", secret_tuple),
                    request_id=resolved_request_id,
                    attempts_used=attempt,
                ) from exc
            if not isinstance(decoded, dict):
                raise LLMValidationError(
                    "MODEL_JSON_INVALID",
                    "Model output JSON root must be an object.",
                    request_id=resolved_request_id,
                    attempts_used=attempt,
                )

            try:
                parsed_model = response_model.model_validate(decoded)
            except ValidationError as exc:
                raise LLMValidationError(
                    "MODEL_SCHEMA_INVALID",
                    redact_secrets(f"Model output failed schema validation: {exc}", secret_tuple),
                    request_id=resolved_request_id,
                    attempts_used=attempt,
                ) from exc

            if validator is not None:
                try:
                    maybe_refined = validator(parsed_model)
                    if inspect.isawaitable(maybe_refined):
                        maybe_refined = await maybe_refined
                except LLMValidationError as exc:
                    raise LLMValidationError(
                        exc.code,
                        redact_secrets(str(exc), secret_tuple),
                        request_id=resolved_request_id,
                        attempts_used=attempt,
                    ) from exc
                except ValueError as exc:
                    raise LLMValidationError(
                        "MODEL_VALIDATION_FAILED",
                        redact_secrets(str(exc), secret_tuple),
                        request_id=resolved_request_id,
                        attempts_used=attempt,
                    ) from exc
                if maybe_refined is not None:
                    parsed_model = maybe_refined
        except LLMValidationError as err:
            last_error = err
            if attempt >= attempts_limit:
                raise
            current_messages = _build_repair_messages(
                base_messages,
                redact_secrets(raw_content, secret_tuple),
                str(err),
                error_code=err.code,
            )
            if retry_delay_seconds > 0:
                await active_clock.async_sleep(retry_delay_seconds)
            continue

        elapsed_ms = round((active_clock.perf_counter() - started_at) * 1000)
        return StructuredCallResult(
            parsed=parsed_model,
            raw_text=raw_content,
            model=model,
            task=task,
            attempts_used=attempt,
            request_id=resolved_request_id,
            latency_ms=elapsed_ms,
            usage={
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
            },
        )

    assert last_error is not None
    raise last_error
