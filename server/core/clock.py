"""Explicit clock abstraction and UTC ISO helpers for deterministic time handling."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def utc_now_iso(value: datetime | None = None) -> str:
    """Format a timezone-aware datetime (or current UTC time) as canonical ISO-8601 Z."""
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None:
        raise ValueError("Datetime value must be timezone-aware.")
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp string (Z or offset) into a UTC-aware datetime."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp string must be non-empty.")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp string must include a timezone offset or 'Z'.")
    return parsed.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """Time and sleep provider injected across core, backend, and LLM layers."""

    def monotonic(self) -> float: ...

    def perf_counter(self) -> float: ...

    def utc_datetime(self) -> datetime: ...

    def utc_now(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    async def async_sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production clock backed by OS monotonic and UTC clocks."""

    def monotonic(self) -> float:
        return time.monotonic()

    def perf_counter(self) -> float:
        return time.perf_counter()

    def utc_datetime(self) -> datetime:
        return datetime.now(UTC)

    def utc_now(self) -> str:
        return utc_now_iso(self.utc_datetime())

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))

    async def async_sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, float(seconds)))


class FakeClock:
    """Deterministic controllable clock for unit and deadline tests."""

    def __init__(
        self,
        *,
        monotonic_time: float = 0.0,
        utc_time: datetime | None = None,
    ) -> None:
        self._monotonic = float(monotonic_time)
        initial_utc = utc_time if utc_time is not None else datetime(2026, 1, 1, tzinfo=UTC)
        if initial_utc.tzinfo is None:
            raise ValueError("FakeClock utc_time must be timezone-aware.")
        self._utc = initial_utc.astimezone(UTC)
        self.sleep_calls: list[float] = []

    def advance(self, seconds: float) -> None:
        delta = float(seconds)
        if delta < 0:
            raise ValueError("Cannot advance FakeClock backwards.")
        self._monotonic += delta
        self._utc = self._utc + timedelta(seconds=delta)

    def monotonic(self) -> float:
        return self._monotonic

    def perf_counter(self) -> float:
        return self._monotonic

    def utc_datetime(self) -> datetime:
        return self._utc

    def utc_now(self) -> str:
        return utc_now_iso(self._utc)

    def sleep(self, seconds: float) -> None:
        duration = max(0.0, float(seconds))
        self.sleep_calls.append(duration)
        self.advance(duration)

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


SYSTEM_CLOCK = SystemClock()


def remaining_seconds(deadline: float | None, *, clock: Clock | None = None) -> float:
    """Return remaining seconds until a monotonic deadline, clamped at 0.0."""
    if deadline is None:
        return float("inf")
    active_clock = clock if clock is not None else SYSTEM_CLOCK
    return max(0.0, float(deadline) - active_clock.monotonic())
