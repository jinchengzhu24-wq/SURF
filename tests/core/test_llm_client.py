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
    _prepare_schema_for_mode,
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
    assert call["response_format"]["json_schema"]["schema"] == _prepare_schema_for_mode(
        SampleSchema.model_json_schema(), "json_schema"
    )


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


class JsonObjectFakeTransport(FakeTransport):
    response_format_mode = "json_object"


class PromptOnlyFakeTransport(FakeTransport):
    response_format_mode = "prompt_only"


async def test_call_structured_prompt_only_mode_omits_response_format_and_keeps_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_schema = json.dumps(
        _prepare_schema_for_mode(SampleSchema.model_json_schema(), "prompt_only"),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    transport = PromptOnlyFakeTransport(
        [
            TransportResponse(content="not json"),
            TransportResponse(
                content='```json\n{"move_hint": "prompt ok", "score": 9}\n```'
            ),
        ]
    )
    result = await call_structured(
        transport,
        task="dg_guide",
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": "Follow the contract."},
            {"role": "user", "content": "Guide?"},
        ],
        response_model=SampleSchema,
        max_attempts=2,
        clock=FakeClock(),
    )
    assert result.attempts_used == 2
    assert result.parsed == SampleSchema(move_hint="prompt ok", score=9)
    for call in transport.calls:
        assert call["response_format"] is None
        assert call["messages"][0]["role"] == "system"
        assert "Follow the contract." in call["messages"][0]["content"]
        assert expected_schema in call["messages"][0]["content"]

    captured_kwargs: list[dict[str, Any]] = []

    class _FakeMessage:
        content = json.dumps({"move_hint": "ds ok", "score": 5})

    class _FakeChoice:
        message = _FakeMessage()
        finish_reason = "stop"

    class _FakeResponse:
        choices = [_FakeChoice()]
        usage = None

    class _FakeCompletions:
        async def create(self, **kwargs: Any) -> _FakeResponse:
            captured_kwargs.append(kwargs)
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    real_transport = AsyncOpenAITransport(
        api_key="ds-test-12345678901234",
        base_url="https://api.deepseek.com",
        response_format_mode="prompt_only",
        default_extra_body={"thinking": {"type": "disabled"}},
    )
    monkeypatch.setattr(real_transport, "get_client", lambda **_kw: _FakeClient())

    res = await call_structured(
        real_transport,
        task="level_plan",
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "Plan?"}],
        response_model=SampleSchema,
        max_attempts=1,
        temperature=0.6,
        clock=FakeClock(),
    )
    assert res.parsed == SampleSchema(move_hint="ds ok", score=5)
    assert len(captured_kwargs) == 1
    assert "response_format" not in captured_kwargs[0]
    assert captured_kwargs[0]["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_call_structured_json_object_mode_appends_schema_and_keeps_it_on_repair() -> None:
    expected_schema = json.dumps(
        _prepare_schema_for_mode(SampleSchema.model_json_schema(), "json_object"),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    transport = JsonObjectFakeTransport(
        [
            TransportResponse(content="{not valid json"),
            TransportResponse(content=json.dumps({"move_hint": "step left", "score": 7})),
        ]
    )
    result = await call_structured(
        transport,
        task="hint_gen",
        model="kimi-k2.6",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hint?"},
        ],
        response_model=SampleSchema,
        max_attempts=2,
        clock=FakeClock(),
    )
    assert result.attempts_used == 2
    assert result.parsed == SampleSchema(move_hint="step left", score=7)
    for call in transport.calls:
        assert call["response_format"] == {"type": "json_object"}
        assert call["messages"][0]["role"] == "system"
        assert "Be concise." in call["messages"][0]["content"]
        assert expected_schema in call["messages"][0]["content"]

    no_sys_transport = JsonObjectFakeTransport(
        [TransportResponse(content=json.dumps({"move_hint": "up", "score": 3}))]
    )
    res_no_sys = await call_structured(
        no_sys_transport,
        task="hint_gen",
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "Hint?"}],
        response_model=SampleSchema,
        max_attempts=1,
        clock=FakeClock(),
    )
    assert res_no_sys.parsed == SampleSchema(move_hint="up", score=3)
    first_messages = no_sys_transport.calls[0]["messages"]
    assert first_messages[0]["role"] == "system"
    assert expected_schema in first_messages[0]["content"]
    assert first_messages[1] == {"role": "user", "content": "Hint?"}


async def test_async_openai_transport_applies_provider_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: list[dict[str, Any]] = []

    class _FakeMessage:
        content = json.dumps({"move_hint": "ok", "score": 1})

    class _FakeChoice:
        message = _FakeMessage()
        finish_reason = "stop"

    class _FakeUsage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _FakeResponse:
        choices = [_FakeChoice()]
        usage = _FakeUsage()

    class _FakeCompletions:
        async def create(self, **kwargs: Any) -> _FakeResponse:
            captured_kwargs.append(kwargs)
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    transport = AsyncOpenAITransport(
        api_key="sk-test-12345678901234",
        base_url="https://api.moonshot.cn/v1",
        response_format_mode="json_object",
        default_extra_body={"thinking": {"type": "disabled"}},
        fixed_temperature=0.6,
        use_max_completion_tokens=True,
    )
    monkeypatch.setattr(transport, "get_client", lambda **_kw: _FakeClient())

    resp = await transport.complete(
        model="kimi-k2.6",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
        temperature=0.35,
        max_tokens=2400,
        extra_body={"foo": "bar"},
    )
    assert resp.finish_reason == "stop"
    assert resp.total_tokens == 15
    assert len(captured_kwargs) == 1
    sent = captured_kwargs[0]
    assert sent["temperature"] == 0.6
    assert sent["max_completion_tokens"] == 2400
    assert "max_tokens" not in sent
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}, "foo": "bar"}


async def test_call_structured_unwraps_single_json_fence_and_rejects_bad_roots() -> None:
    json_fenced = JsonObjectFakeTransport(
        [
            TransportResponse(
                content='  ```json\r\n{"move_hint": "push up", "score": 11}\r\n```  '
            )
        ]
    )
    res_json_fence = await call_structured(
        json_fenced,
        task="hint_gen",
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "Hint?"}],
        response_model=SampleSchema,
        max_attempts=1,
        clock=FakeClock(),
    )
    assert res_json_fence.parsed == SampleSchema(move_hint="push up", score=11)

    bare_fenced = JsonObjectFakeTransport(
        [TransportResponse(content='```\n{"move_hint": "push down", "score": 12}\n```')]
    )
    res_bare_fence = await call_structured(
        bare_fenced,
        task="hint_gen",
        model="kimi-k2.6",
        messages=[{"role": "user", "content": "Hint?"}],
        response_model=SampleSchema,
        max_attempts=1,
        clock=FakeClock(),
    )
    assert res_bare_fence.parsed == SampleSchema(move_hint="push down", score=12)

    prose_wrapped = JsonObjectFakeTransport(
        [
            TransportResponse(
                content='Here is the JSON:\n```json\n{"move_hint": "push up", "score": 11}\n```'
            )
        ]
    )
    with pytest.raises(LLMValidationError) as exc_prose:
        await call_structured(
            prose_wrapped,
            task="hint_gen",
            model="kimi-k2.6",
            messages=[{"role": "user", "content": "Hint?"}],
            response_model=SampleSchema,
            max_attempts=1,
            clock=FakeClock(),
        )
    assert exc_prose.value.code == "MODEL_JSON_INVALID"

    for bad_root in ('["push up", 11]', "42", '"just a string"'):
        non_dict = JsonObjectFakeTransport([TransportResponse(content=bad_root)])
        with pytest.raises(LLMValidationError) as exc_non_dict:
            await call_structured(
                non_dict,
                task="hint_gen",
                model="kimi-k2.6",
                messages=[{"role": "user", "content": "Hint?"}],
                response_model=SampleSchema,
                max_attempts=1,
                clock=FakeClock(),
            )
        assert exc_non_dict.value.code == "MODEL_JSON_INVALID"


async def test_call_structured_truncation_repair_omits_raw_and_supports_async_validator() -> None:
    huge_partial = '{"move_hint":"' + ("x" * 1500)
    transport = FakeTransport(
        [
            TransportResponse(content=huge_partial, finish_reason="length"),
            TransportResponse(content=json.dumps({"move_hint": "concise", "score": 8})),
        ]
    )

    async def _async_validator(parsed: SampleSchema) -> SampleSchema:
        return SampleSchema(move_hint=parsed.move_hint.strip(), score=parsed.score)

    res = await call_structured(
        transport,
        task="hint_gen",
        model="kimi-k2.6",
        messages=[{"role": "user", "content": "Hint?"}],
        response_model=SampleSchema,
        validator=_async_validator,
        max_attempts=2,
        clock=FakeClock(),
    )
    assert res.attempts_used == 2
    assert res.parsed == SampleSchema(move_hint="concise", score=8)
    repair_msgs = transport.calls[1]["messages"]
    assert len(repair_msgs) == 3
    assert len(repair_msgs[1]["content"]) < 300
    assert huge_partial not in repair_msgs[1]["content"]
    assert "truncated" in repair_msgs[2]["content"].lower()
    assert "concise" in repair_msgs[2]["content"].lower()


def test_prepare_schema_for_mode_strict_and_title_stripping() -> None:
    class NestedChild(BaseModel):
        title: str
        note: str | None = None

    class ParentWithTitleProp(BaseModel):
        title: str
        child: NestedChild
        optional_flag: bool = False

    raw = ParentWithTitleProp.model_json_schema()
    assert "title" in raw  # Pydantic emits top-level title metadata

    # json_schema strict mode: additionalProperties=False and all properties in required recursively
    strict_schema = _prepare_schema_for_mode(raw, "json_schema")
    assert strict_schema["additionalProperties"] is False
    assert set(strict_schema["required"]) == {"title", "child", "optional_flag"}
    child_def = strict_schema["$defs"]["NestedChild"]
    assert child_def["additionalProperties"] is False
    assert set(child_def["required"]) == {"title", "note"}

    # json_object & prompt_only: strip noisy 'title' metadata but keep literal 'title' property!
    for mode in ("json_object", "prompt_only"):
        compact = _prepare_schema_for_mode(raw, mode)
        assert "title" not in compact
        assert "title" in compact["properties"]
        assert "title" not in compact["properties"]["title"]
        assert "title" in compact["required"]
        nested = compact["$defs"]["NestedChild"]
        assert "title" not in nested
        assert "title" in nested["properties"]
        assert "title" not in nested["properties"]["title"]

    # json_schema strict mode strips 'discriminator' and converts 'oneOf' -> 'anyOf'
    raw_union = {
        "type": "object",
        "properties": {
            "branch": {
                "oneOf": [{"type": "object", "properties": {"kind": {"const": "a"}}}],
                "discriminator": {"propertyName": "kind"},
            }
        },
    }
    norm_union = _prepare_schema_for_mode(raw_union, "json_schema")
    branch_node = norm_union["properties"]["branch"]
    assert "discriminator" not in branch_node
    assert "oneOf" not in branch_node
    assert "anyOf" in branch_node
    assert branch_node["anyOf"][0]["additionalProperties"] is False
    assert branch_node["anyOf"][0]["required"] == ["kind"]
