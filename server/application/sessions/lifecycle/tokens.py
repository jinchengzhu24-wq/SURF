"""Decision 5B random token generation, hashing, URL builders, and deadline helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from server.core.clock import Clock, parse_iso, utc_now_iso
from server.core.settings import safe_compare_digest
from server.persistence.database import dump_json

from ..config import (
    COCREATION_DEADLINE_SECONDS,
    DEFAULT_PUBLIC_BASE_URL,
    DEFAULT_TOKEN_SECRET,
    DEFAULT_WEBGL_BASE_URL,
    warn_if_default_token_secret,
)


def generate_random_token(nbytes: int = 32) -> str:
    """Generate a cryptographically random URL-safe token."""
    return secrets.token_urlsafe(nbytes)


def generate_session_secret() -> str:
    """Generate a random per-session secret stored only as a hash."""
    return secrets.token_hex(32)


def hash_token(token: str, *, secret_bytes: bytes | None = None) -> str:
    """Hash a token with keyed HMAC-SHA256 (or SHA-256 when unkeyed) for storage."""
    raw = str(token).encode("utf-8")
    if secret_bytes:
        return hmac.new(secret_bytes, raw, hashlib.sha256).hexdigest()
    return hashlib.sha256(raw).hexdigest()


def token_matches(
    supplied_token: str | None,
    expected_hash: str | None,
    *,
    secret_bytes: bytes | None = None,
) -> bool:
    """Constant-time verification of a supplied token against a stored hash."""
    if not supplied_token or not expected_hash:
        return False
    candidate_hmac = hash_token(supplied_token, secret_bytes=secret_bytes)
    if safe_compare_digest(candidate_hmac, expected_hash):
        return True
    if secret_bytes:
        candidate_plain = hash_token(supplied_token, secret_bytes=None)
        return safe_compare_digest(candidate_plain, expected_hash)
    return False


def build_launch_url(
    session_id: str,
    bootstrap_token: str,
    *,
    mode: str = "unity",
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL,
) -> str:
    """Build the co-creation launch URL containing the bootstrap fragment."""
    base = public_base_url.rstrip("/")
    return (
        f"{base}/#session={quote(session_id)}&bootstrap={quote(bootstrap_token)}&mode={quote(mode)}"
    )


def build_return_url(
    session_id: str,
    version_id: str,
    *,
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL,
) -> str:
    """Build the co-creation return URL for a specific Stage version."""
    base = public_base_url.rstrip("/")
    return f"{base}/#session={quote(session_id)}&stage={quote(version_id)}"


def build_play_url(
    attempt_id: str,
    ticket: str,
    *,
    webgl_base_url: str = DEFAULT_WEBGL_BASE_URL,
) -> str:
    """Build the WebGL play-test URL for a play attempt."""
    separator = "&" if "?" in webgl_base_url else "?"
    return (
        f"{webgl_base_url}{separator}cocreationAttempt={quote(attempt_id)}"
        f"&cocreationPlay={quote(ticket)}"
    )


def request_digest(payload: Any) -> str:
    """Deterministic SHA-256 fingerprint for idempotency request bodies."""
    return hashlib.sha256(dump_json(payload).encode("utf-8")).hexdigest()


def is_deadline_expired(deadline_at: str | None, *, now: datetime) -> bool:
    """Return True when a session deadline ISO timestamp is at or before ``now``."""
    if not deadline_at:
        return False
    return parse_iso(deadline_at) <= now


def remaining_deadline_seconds(deadline_at: str | None, *, now: datetime) -> int | None:
    """Return non-negative remaining seconds until ``deadline_at``, or None if unset."""
    if not deadline_at:
        return None
    return max(0, int((parse_iso(deadline_at) - now).total_seconds()))


def compute_cocreation_duration_seconds(
    deadline_at: str | None,
    finalized_at: str | None,
    *,
    cocreation_deadline_seconds: int = COCREATION_DEADLINE_SECONDS,
) -> int | None:
    """Compute elapsed co-creation seconds clamped to the configured deadline budget."""
    if not deadline_at or not finalized_at:
        return None
    rem = max(0, int((parse_iso(deadline_at) - parse_iso(finalized_at)).total_seconds()))
    return min(cocreation_deadline_seconds, max(0, cocreation_deadline_seconds - rem))


def utc_plus_seconds_iso(clock: Clock, seconds: int) -> str:
    """Return an ISO-8601 UTC timestamp offset from ``clock`` by ``seconds``."""
    return utc_now_iso(clock.utc_datetime() + timedelta(seconds=seconds))


__all__ = [
    "COCREATION_DEADLINE_SECONDS",
    "DEFAULT_PUBLIC_BASE_URL",
    "DEFAULT_TOKEN_SECRET",
    "DEFAULT_WEBGL_BASE_URL",
    "build_launch_url",
    "build_play_url",
    "build_return_url",
    "compute_cocreation_duration_seconds",
    "generate_random_token",
    "generate_session_secret",
    "hash_token",
    "is_deadline_expired",
    "remaining_deadline_seconds",
    "request_digest",
    "token_matches",
    "utc_plus_seconds_iso",
    "warn_if_default_token_secret",
]
