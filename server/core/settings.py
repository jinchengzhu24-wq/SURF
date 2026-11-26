"""Shared pydantic-settings base helpers and secret redaction utilities."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from typing import Annotated, Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_SECRET_FIELD_MARKERS = ("key", "secret", "password", "token")

_BEARER_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+|bearer\s+)[A-Za-z0-9._~+/=-]{6,}"
)
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|x-api-key|x-player-token|x-sync-secret|"
    r"x-delete-password|player[_-]?token|session[_-]?token|sync[_-]?secret|"
    r"delete[_-]?password|password|secret|token)\b\s*[:=]\s*['\"]?)"
    r"(?P<value>[^\s'\",;&}{]+)"
)
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|player[_-]?token|secret|password)=)([^&\s'\"]+)"
)
_URL_USERINFO_PATTERN = re.compile(r"(https?://)([^:\s/@]+):([^@\s/]+)@")
_PREFIXED_TOKEN_PATTERN = re.compile(
    r"\b(?:sk|key|kimi|ds)-[A-Za-z0-9_-]{12,}\b"
)


def redact_secrets(text: object, extra_secrets: Iterable[str] = ()) -> str:
    """Redact API keys, bearer tokens, passwords, player tokens, and extra literals."""
    result = str("" if text is None else text)
    if not result:
        return ""

    literals = sorted(
        {
            item.strip()
            for item in extra_secrets
            if isinstance(item, str) and item.strip()
        },
        key=len,
        reverse=True,
    )
    for secret in literals:
        result = result.replace(secret, "[redacted]")

    result = _URL_USERINFO_PATTERN.sub(r"\1[redacted]:[redacted]@", result)
    result = _BEARER_PATTERN.sub(r"\1[redacted]", result)
    result = _QUERY_SECRET_PATTERN.sub(r"\1[redacted]", result)
    result = _KEY_VALUE_SECRET_PATTERN.sub(r"\g<prefix>[redacted]", result)
    result = _PREFIXED_TOKEN_PATTERN.sub("[redacted]", result)
    return result


def parse_allowed_origins(raw: object) -> tuple[str, ...]:
    """Parse comma-separated CORS origins, defaulting to wildcard when empty."""
    if raw is None:
        return ("*",)
    if isinstance(raw, (list, tuple)):
        items = tuple(str(item).strip() for item in raw if str(item).strip())
        return items or ("*",)
    text = str(raw).strip()
    if not text:
        return ("*",)
    items = tuple(item.strip() for item in text.split(",") if item.strip())
    return items or ("*",)


def safe_compare_digest(left: object, right: object) -> bool:
    """Constant-time string comparison for secrets and passwords."""
    left_bytes = str("" if left is None else left).encode("utf-8")
    right_bytes = str("" if right is None else right).encode("utf-8")
    return secrets.compare_digest(left_bytes, right_bytes)


class BaseSokobanSettings(BaseSettings):
    """Shared BaseSettings foundation for 8000 and 8010 backend configurations."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    log_level: str = "INFO"
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = ("*",)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        text = str(value or "INFO").strip().upper()
        return text or "INFO"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _normalize_allowed_origins(cls, value: Any) -> tuple[str, ...]:
        return parse_allowed_origins(value)

    def secret_values(self) -> tuple[str, ...]:
        """Collect configured non-empty secret values for log/error redaction."""
        collected: list[str] = []
        for field_name in type(self).model_fields:
            value = getattr(self, field_name, None)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value().strip()
                if raw:
                    collected.append(raw)
                continue
            lowered = field_name.lower()
            has_marker = any(marker in lowered for marker in _SECRET_FIELD_MARKERS)
            if isinstance(value, str) and has_marker:
                raw = value.strip()
                if raw:
                    collected.append(raw)
        return tuple(dict.fromkeys(collected))
