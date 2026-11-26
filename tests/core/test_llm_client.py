"""Tests for server.core.llm_client."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest
from openai import APIStatusError, APITimeoutError
from pydantic import BaseModel
from server.core.clock import FakeClock
from server.core.llm_client import (
    AsyncOpenAITransport,
    DeadlineExceededError,
    LLMValidationError,
    TransportResponse,
    UpstreamLLMError,
    call_structured,
)


class SampleSchema(BaseModel):
    move_hint: str
    score: int


class FakeTransport:
    def __init__(self, outcomes: Sequence[TransportResponse | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

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
    ) -> TransportResponse:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "response_format": response_format,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "extra_body": extra_body,
            }
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_call_structured_uses_json_schema_and_explicit_max_tokens() -> None:
    transport = FakeTransport(
        [
            TransportResponse(
                content=json.dumps({"move_hint": "push right", "score": 10}),
                finish_reason="stop",
                prompt_tokens=15,
                completion_tokens=8,
                total_tokens=23,
            )
        ]
    )
    clock = FakeClock()
    result = await call_structured(
        transport,
        task="hint_gen",
        model="kimi-k2.6",
        messages=[{"role": "user", "content": "Hint?"}],
        response_model=SampleSchema,
        max_tokens=512,
        temperature=0.2,
        clock=clock,
    )
    assert result.parsed == SampleSchema(move_hint="push right", score=10)
    assert result.attempts_used == 1
    assert result.usage["total_tokens"] == 23

    call = transport.calls[0]
    assert call["max_tokens"] == 512
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["schema"] == SampleSchema.model_json_schema()


async def test_call_structured_retries_on_validation_error_with_repair_prompt() -> None:
    transport = FakeTransport(
        [
            TransportResponse(content="{not valid json"),
            TransportResponse(content=json.dumps({"move_hint": "ok", "score": 5})),
        ]
    )
    result = await call_structured(
        transport,
        task="hint_gen",
        model="kimi-k2.6",
        messages=[{"role": "user", "content": "Hint?"}],
        response_model=SampleSchema,
        max_attempts=2,
        clock=FakeClock(),
    )
    assert result.attempts_used == 2
    assert result.parsed.score == 5
    assert len(transport.calls[1]["messages"]) == 3
    assert transport.calls[1]["messages"][1]["role"] == "assistant"


async def test_call_structured_rejects_truncation_and_non_finite_json() -> None:
    truncated = FakeTransport(
        [TransportResponse(content='{"move_hint":"x"', finish_reason="length")]
    )
    with pytest.raises(LLMValidationError) as exc_trunc:
        await call_structured(
            truncated,
            task="hint_gen",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            response_model=SampleSchema,
            max_attempts=1,
        )
    assert exc_trunc.value.code == "MODEL_OUTPUT_TRUNCATED"

    non_finite = FakeTransport([TransportResponse(content='{"move_hint": "x", "score": NaN}')])
    with pytest.raises(LLMValidationError) as exc_nan:
        await call_structured(
            non_finite,
            task="hint_gen",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            response_model=SampleSchema,
            max_attempts=1,
        )
    assert exc_nan.value.code == "MODEL_JSON_INVALID"


@pytest.mark.parametrize(
    "bug",
    [
        TypeError("internal type bug"),
        KeyError("missing_internal_key"),
        IndexError("bad_index"),
        AttributeError("missing_attr"),
    ],
)
async def test_call_structured_never_swallows_python_bugs(bug: Exception) -> None:
    transport = FakeTransport(
        [
            TransportResponse(content=json.dumps({"move_hint": "ok", "score": 1})),
            TransportResponse(content=json.dumps({"move_hint": "ok", "score": 2})),
        ]
    )

    def buggy_validator(_parsed: SampleSchema) -> SampleSchema:
        raise bug

    with pytest.raises(type(bug)):
        await call_structured(
            transport,
            task="hint_gen",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            response_model=SampleSchema,
            validator=buggy_validator,
            max_attempts=2,
        )
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status_code", [400, 422, 401, 403])
async def test_call_structured_no_silent_downgrade_or_retry_on_4xx(status_code: int) -> None:
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(status_code, request=req)
    transport = FakeTransport(
        [
            APIStatusError("bad request", response=resp, body={"error": {"message": "rejected"}}),
            TransportResponse(content=json.dumps({"move_hint": "ok", "score": 1})),
        ]
    )
    with pytest.raises(UpstreamLLMError) as exc_info:
        await call_structured(
            transport,
            task="hint_gen",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            response_model=SampleSchema,
            max_attempts=2,
        )
    assert exc_info.value.retryable is False
    assert exc_info.value.provider_status == status_code
    assert len(transport.calls) == 1


async def test_call_structured_retries_upstream_timeout_and_enforces_deadline() -> None:
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    transport = FakeTransport(
        [
            APITimeoutError(request=req),
            TransportResponse(content=json.dumps({"move_hint": "recovered", "score": 9})),
        ]
    )
    clock = FakeClock(monotonic_time=0.0)
    result = await call_structured(
        transport,
        task="hint_gen",
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        response_model=SampleSchema,
        deadline=20.0,
        max_attempts=2,
        retry_delay_seconds=0.5,
        clock=clock,
    )
    assert result.parsed.move_hint == "recovered"
    assert result.attempts_used == 2
    assert clock.sleep_calls == [0.5]

    # Expired deadline before attempt 1 raises DeadlineExceededError without transport calls.
    idle_transport = FakeTransport([])
    with pytest.raises(DeadlineExceededError):
        await call_structured(
            idle_transport,
            task="hint_gen",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            response_model=SampleSchema,
            deadline=0.0,
            clock=clock,
        )
    assert len(idle_transport.calls) == 0


async def test_async_openai_transport_pools_by_key_and_closes_cleanly() -> None:
    transport = AsyncOpenAITransport(
        api_key="sk-test-12345678901234",
        base_url="https://api.example.com/v1",
    )
    client_a = transport.get_client()
    client_b = transport.get_client()
    client_c = transport.get_client(api_key="sk-other-12345678901234")
    assert client_a is client_b
    assert client_a is not client_c
    assert transport.cached_client_count == 2

    await transport.close()
    assert transport.cached_client_count == 0

    empty_transport = AsyncOpenAITransport(api_key="", base_url="https://api.example.com/v1")
    with pytest.raises(UpstreamLLMError) as exc_info:
        empty_transport.get_client()
    assert exc_info.value.code == "CONFIGURATION_ERROR"
