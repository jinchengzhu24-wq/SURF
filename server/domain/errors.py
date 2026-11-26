"""Pure domain error hierarchy (framework-agnostic)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DomainError(Exception):
    """Base domain error with machine-readable code and i18n message key."""

    def __init__(
        self,
        code: str,
        message_key: str = "",
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message_key = str(message_key or f"error.{self.code.lower()}")
        self.details: dict[str, Any] = dict(details) if details is not None else {}
        self.retryable = bool(retryable)
        super().__init__(message or self.message_key or self.code)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "messageKey": self.message_key,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class NotFoundError(DomainError):
    """Requested domain entity does not exist."""

    def __init__(
        self,
        code: str = "NOT_FOUND",
        message_key: str = "error.session_not_found",
        *,
        details: Mapping[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=False,
            message=message,
        )


class ConflictError(DomainError):
    """State conflict prevents the requested transition."""

    def __init__(
        self,
        code: str = "CONFLICT",
        message_key: str = "error.version_conflict",
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=retryable,
            message=message,
        )


class ValidationError(DomainError):
    """Input or invariant validation failed."""

    def __init__(
        self,
        code: str = "VALIDATION_ERROR",
        message_key: str = "error.validation_failed",
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=retryable,
            message=message,
        )


class AuthError(DomainError):
    """Caller lacks valid credentials or session authorization."""

    def __init__(
        self,
        code: str = "AUTH_ERROR",
        message_key: str = "error.session_access_denied",
        *,
        details: Mapping[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=False,
            message=message,
        )


class UpstreamLLMError(DomainError):
    """Upstream model provider error or invalid structured response."""

    def __init__(
        self,
        code: str = "UPSTREAM_LLM_ERROR",
        message_key: str = "error.upstream_llm",
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = True,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=retryable,
            message=message,
        )


class DeadlineExceededError(DomainError):
    """Operation exceeded its allocated wall-clock deadline."""

    def __init__(
        self,
        code: str = "DEADLINE_EXCEEDED",
        message_key: str = "error.deadline_exceeded",
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = True,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=retryable,
            message=message,
        )


class IdempotencyConflictError(ConflictError):
    """Same idempotency key was reused with a different request payload."""

    def __init__(
        self,
        code: str = "IDEMPOTENCY_CONFLICT",
        message_key: str = "error.idempotency_conflict",
        *,
        details: Mapping[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=False,
            message=message,
        )


class RateLimitError(DomainError):
    """Upstream or local rate limit reached."""

    def __init__(
        self,
        code: str = "RATE_LIMIT_EXCEEDED",
        message_key: str = "error.rate_limit",
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = True,
        message: str | None = None,
    ) -> None:
        super().__init__(
            code,
            message_key,
            details=details,
            retryable=retryable,
            message=message,
        )
