"""Tests for server.core.clock and server.core.settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import SecretStr
from server.core.clock import (
    SYSTEM_CLOCK,
    Clock,
    FakeClock,
    SystemClock,
    parse_iso,
    remaining_seconds,
    utc_now_iso,
)
from server.core.settings import (
    BaseSokobanSettings,
    parse_allowed_origins,
    redact_secrets,
    safe_compare_digest,
)


async def test_fake_clock_and_iso_helpers() -> None:
    initial = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(monotonic_time=100.0, utc_time=initial)
    assert isinstance(clock, Clock)
    assert isinstance(SYSTEM_CLOCK, SystemClock)
    assert isinstance(SYSTEM_CLOCK, Clock)

    assert clock.monotonic() == 100.0
    assert clock.utc_now() == "2026-05-01T12:00:00Z"

    clock.advance(5.5)
    assert clock.monotonic() == 105.5
    assert clock.utc_now() == "2026-05-01T12:00:05.500000Z"

    clock.sleep(1.5)
    await clock.async_sleep(2.0)
    assert clock.sleep_calls == [1.5, 2.0]
    assert clock.monotonic() == 109.0

    assert remaining_seconds(None, clock=clock) == float("inf")
    assert remaining_seconds(115.0, clock=clock) == 6.0
    assert remaining_seconds(50.0, clock=clock) == 0.0

    parsed = parse_iso("2026-05-01T14:00:00+02:00")
    assert parsed == datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    assert utc_now_iso(parsed) == "2026-05-01T12:00:00Z"

    with pytest.raises(ValueError):
        parse_iso("2026-05-01T12:00:00")
    with pytest.raises(ValueError):
        utc_now_iso(datetime(2026, 5, 1, 12, 0, 0))
    with pytest.raises(ValueError):
        clock.advance(-1.0)

    plus_two = timezone(timedelta(hours=2))
    assert utc_now_iso(datetime(2026, 5, 1, 14, 0, 0, tzinfo=plus_two)) == "2026-05-01T12:00:00Z"


def test_redact_secrets_hides_keys_tokens_and_passwords() -> None:
    raw = (
        "Authorization: Bearer tok_bearer_123456 "
        "api_key=my_api_key_99 "
        "password=superpass "
        "player_token=ptok_abc123 "
        "https://alice:hunter2@example.com/path?token=qtok_xyz "
        "sk-1234567890abcdef "
        "custom_secret_val"
    )
    redacted = redact_secrets(raw, extra_secrets=["custom_secret_val"])
    for secret in (
        "tok_bearer_123456",
        "my_api_key_99",
        "superpass",
        "ptok_abc123",
        "hunter2",
        "qtok_xyz",
        "sk-1234567890abcdef",
        "custom_secret_val",
    ):
        assert secret not in redacted
    assert "[redacted]" in redacted
    assert redact_secrets(None) == ""


def test_settings_helpers_and_base_class() -> None:
    assert parse_allowed_origins(None) == ("*",)
    assert parse_allowed_origins("") == ("*",)
    assert parse_allowed_origins("https://a.test, http://localhost:8000 ") == (
        "https://a.test",
        "http://localhost:8000",
    )
    assert safe_compare_digest("abc", "abc") is True
    assert safe_compare_digest("abc", "abd") is False

    class DemoSettings(BaseSokobanSettings):
        api_key: SecretStr = SecretStr("sk-demo-secret-12345")
        delete_password: str = "pw-9876"
        public_name: str = "sokoban"

    cfg = DemoSettings(log_level=" debug ", allowed_origins="http://a.local, http://b.local")
    assert cfg.log_level == "DEBUG"
    assert cfg.allowed_origins == ("http://a.local", "http://b.local")
    secrets = cfg.secret_values()
    assert "sk-demo-secret-12345" in secrets
    assert "pw-9876" in secrets
    assert "sokoban" not in secrets
