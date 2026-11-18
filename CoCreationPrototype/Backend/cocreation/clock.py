"""Explicit clock abstraction for deterministic time and deadline testing."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


def iso_time(value: datetime) -> str:
    """Format a timezone-aware datetime as canonical ISO-8601 UTC with Z."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp string (accepting Z or +00:00)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@runtime_checkable
class Clock(Protocol):
    """Time and sleep provider injected across API, persistence, and LLM layers."""

    def monotonic(self) -> float: ...

    def utc_datetime(self) -> datetime: ...

    def utc_now(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    async def async_sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production clock backed by OS monotonic and UTC clocks."""

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_datetime(self) -> datetime:
        return datetime.now(timezone.utc)

    def utc_now(self) -> str:
        return iso_time(self.utc_datetime())

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))

    async def async_sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, float(seconds)))


SYSTEM_CLOCK = SystemClock()
