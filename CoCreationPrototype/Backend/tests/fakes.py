"""Deterministic test fakes and DI builders for 8010 backend tests."""
from __future__ import annotations

import copy
import inspect
import json
import tempfile
import unittest
from collections import defaultdict, deque
from contextlib import contextmanager
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from cocreation.api.app_factory import create_app
from cocreation.api.services import (
    CoCreationServices,
    CoCreationSettings,
    OnlineSyncGateway,
    build_version_sync_event,
)
from cocreation.clock import Clock, iso_time, parse_time
from cocreation.domain.demo_level_generator import generate_demo_level
from cocreation.llm.config_and_runtime import LLMConfig, _ACTIVE_LLM_SERVICE
from cocreation.llm.service import CoCreationLLMService
from cocreation.persistence.database import SQLiteRepository


def offline_client(*args):
    raise AssertionError("This test must inject a provider response")


class RecordedCall:
    """A scripted collaborator with call history for assertions."""

    def __init__(self, *, return_value=None, side_effect=None):
        self.return_value = return_value
        self.side_effect = side_effect if callable(side_effect) or isinstance(side_effect, BaseException) else (
            iter(side_effect) if side_effect is not None else None
        )
        self.call_args_list = []

    @property
    def call_count(self):
        return len(self.call_args_list)

    @property
    def call_args(self):
        return self.call_args_list[-1] if self.call_args_list else None

    def __call__(self, *args, **kwargs):
        self.call_args_list.append(SimpleNamespace(args=args, kwargs=kwargs))
        if self.side_effect is None:
            return self.return_value
        if isinstance(self.side_effect, BaseException):
            raise self.side_effect
        if callable(self.side_effect):
            return self.side_effect(*args, **kwargs)
        result = next(self.side_effect)
        if isinstance(result, BaseException):
            raise result
        return result

    def assert_not_called(self):
        assert self.call_count == 0, self.call_args_list

    def assert_called_once(self):
        assert self.call_count == 1, self.call_args_list

    def assert_called_once_with(self, *args, **kwargs):
        self.assert_called_once()
        assert (self.call_args.args, self.call_args.kwargs) == (args, kwargs), self.call_args


class RecordedAsyncCall(RecordedCall):
    @property
    def await_count(self):
        return self.call_count

    @property
    def await_args(self):
        return self.call_args

    @property
    def await_args_list(self):
        return self.call_args_list

    async def __call__(self, *args, **kwargs):
        result = super().__call__(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def assert_awaited_once(self):
        self.assert_called_once()


TEST_LLM_ENV = {"KIMI_API_KEY": "test-kimi-key"}


def offline_llm(*, env=None, config=None, base=None, handlers=None, **dependencies):
    """Build a new model service using only the supplied scenario dependencies."""
    if base is None:
        defaults = {"client_factory": offline_client}
        config_snapshot = LLMConfig.from_env(TEST_LLM_ENV)
    else:
        defaults = {name: getattr(base, name) for name in (
            "client_factory", "clock", "event_logger", "completion_requester",
            "intent_candidate_reviewer", "intent_component_repairer",
            "manual_edit_conflict_classifier", "revision_search_generator", "solver_fn",
            "remaining_until_fn",
        )}
        config_snapshot = base.config
    if env is not None:
        config_snapshot = LLMConfig.from_env(env)
    if config:
        config_snapshot = replace(config_snapshot, **config)
    inherited = getattr(base, "_handlers", {})
    return StubLLMService(config_snapshot, handlers={**inherited, **(handlers or {})}, **(defaults | dependencies))


@contextmanager
def llm_scope(**dependencies):
    # Low-level workflow tests call module functions under an explicitly chosen service.
    service = offline_llm(base=_ACTIVE_LLM_SERVICE.get(), **dependencies)
    with service.activated():
        yield service


class FakeClock:
    """Deterministic Clock fake for tests without real sleeps or wall-clock drift."""

    def __init__(
        self,
        initial_monotonic: float = 1000.0,
        initial_utc: datetime | None = None,
    ) -> None:
        self._monotonic = float(initial_monotonic)
        if initial_utc is None:
            self._utc = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        elif initial_utc.tzinfo is None:
            self._utc = initial_utc.replace(tzinfo=timezone.utc)
        else:
            self._utc = initial_utc.astimezone(timezone.utc)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._monotonic

    def utc_datetime(self) -> datetime:
        return self._utc

    def utc_now(self) -> str:
        return iso_time(self._utc)

    def advance(self, seconds: float) -> datetime:
        delta = float(seconds)
        self._monotonic += delta
        self._utc = self._utc + timedelta(seconds=delta)
        return self._utc

    def set_utc(self, value: datetime | str) -> datetime:
        dt = parse_time(value) if isinstance(value, str) else value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        self._utc = dt
        return self._utc

    def sleep(self, seconds: float) -> None:
        duration = max(0.0, float(seconds))
        self.sleeps.append(duration)
        self.advance(duration)

    async def async_sleep(self, seconds: float) -> None:
        duration = max(0.0, float(seconds))
        self.sleeps.append(duration)
        self.advance(duration)


def _is_response_tuple(value: Any) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[1], str)
        and value[1] in {"stop", "length", "content_filter", "tool_calls", "function_call"}
    )


def _invoke_with_optional_call_num(handler: Callable[..., Any], kwargs: dict[str, Any], call_num: int) -> Any:
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(kwargs, call_num)
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(positional) >= 2:
        return handler(kwargs, call_num)
    if len(positional) == 1:
        return handler(kwargs)
    return handler()


def _invoke_schema_validator(
    validator: Callable[..., Any],
    content: Any,
    kwargs: dict[str, Any],
) -> None:
    try:
        sig = inspect.signature(validator)
    except (TypeError, ValueError):
        validator(content, kwargs)
        return
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(positional) >= 2:
        first_name = positional[0].name.casefold() if positional else ""
        if first_name.startswith(("kw", "req", "call", "param")):
            validator(kwargs, content)
        else:
            validator(content, kwargs)
        return
    if len(positional) == 1:
        name = positional[0].name.casefold()
        if name in {"kwargs", "request", "call", "params"}:
            validator(kwargs)
        elif name in {"payload", "content", "response", "data", "text", "output"}:
            validator(content)
        else:
            validator(kwargs)
        return
    validator()


def _coerce_openai_response(item: Any, kwargs: dict[str, Any]) -> Any:
    if isinstance(item, BaseException):
        raise item
    if isinstance(item, type) and issubclass(item, BaseException):
        raise item()
    if hasattr(item, "choices"):
        return item
    finish_reason = "stop"
    payload = item
    if _is_response_tuple(item):
        payload, finish_reason = item
    if isinstance(payload, BaseException):
        raise payload
    if isinstance(payload, type) and issubclass(payload, BaseException):
        raise payload()
    if payload is None:
        content = ""
    elif isinstance(payload, str):
        content = payload
    elif isinstance(payload, (dict, list)):
        content = json.dumps(payload, ensure_ascii=False)
    else:
        content = str(payload)
    message = SimpleNamespace(content=content, reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model=kwargs.get("model"),
    )


class _FakeCompletions:
    def __init__(self, parent: FakeAsyncOpenAIClient) -> None:
        self._parent = parent

    async def create(self, **kwargs: Any) -> Any:
        return await self._parent._handle_create(**kwargs)


class _FakeChat:
    def __init__(self, parent: FakeAsyncOpenAIClient) -> None:
        self.completions = _FakeCompletions(parent)


class FakeAsyncOpenAIClient:
    """In-memory replacement for openai.AsyncOpenAI during tests."""

    def __init__(
        self,
        handler_or_items: Any,
        schema_validator: Callable[..., Any] | None = None,
    ) -> None:
        self._handler_or_items = handler_or_items
        self._queue: deque[Any] | None = None
        if isinstance(handler_or_items, (list, deque)):
            self._queue = deque(handler_or_items)
        elif isinstance(handler_or_items, Iterator) and not isinstance(handler_or_items, (str, bytes)):
            self._queue = deque(handler_or_items)
        self.schema_validator = schema_validator
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.chat = _FakeChat(self)

    async def _handle_create(self, **kwargs: Any) -> Any:
        recorded = copy.deepcopy(kwargs)
        self.calls.append(recorded)
        call_num = len(self.calls)
        if self._queue is not None:
            if not self._queue:
                raise AssertionError(f"FakeAsyncOpenAIClient exhausted after {call_num - 1} call(s).")
            outcome = self._queue.popleft()
            if callable(outcome) and not isinstance(outcome, type):
                outcome = _invoke_with_optional_call_num(outcome, recorded, call_num)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
        elif callable(self._handler_or_items) and not isinstance(self._handler_or_items, type):
            outcome = _invoke_with_optional_call_num(self._handler_or_items, recorded, call_num)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        else:
            outcome = self._handler_or_items
        response = _coerce_openai_response(outcome, recorded)
        if self.schema_validator is not None:
            content = response.choices[0].message.content if getattr(response, "choices", None) else response
            _invoke_schema_validator(self.schema_validator, content, recorded)
        return response

    async def close(self) -> None:
        self.closed = True


def _deepcopy_or_same(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


class StubLLMService(CoCreationLLMService):
    """CoCreationLLMService double that records calls and dispatches registered stubs or super()."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        handlers: Mapping[str, Any] | None = None,
        client_factory: Callable[[str, str, float], Any] | None = None,
        clock: Clock | None = None,
        **kwargs: Any,
    ) -> None:
        effective_config = (
            config
            if config is not None
            else LLMConfig(api_key="test-kimi-key", base_url="https://example.invalid/v1")
        )
        super().__init__(
            effective_config,
            client_factory=client_factory,
            clock=clock,
            **kwargs,
        )
        self.calls: list[dict[str, Any]] = []
        self.method_calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._handlers: dict[str, Any] = {}
        self._queues: dict[str, deque[Any]] = {}
        if handlers:
            self.set_handlers(handlers)

    def set_handler(self, name: str, handler_or_value: Any) -> StubLLMService:
        self._queues.pop(name, None)
        self._handlers.pop(name, None)
        if isinstance(handler_or_value, (list, deque)):
            self._queues[name] = deque(handler_or_value)
        elif isinstance(handler_or_value, Iterator) and not isinstance(handler_or_value, (str, bytes)):
            self._queues[name] = deque(handler_or_value)
        else:
            self._handlers[name] = handler_or_value
        return self

    def set_handlers(self, handlers: Mapping[str, Any]) -> StubLLMService:
        for name, value in handlers.items():
            self.set_handler(name, value)
        return self

    def _record_call(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        entry = {"method": name, "args": args, "kwargs": dict(kwargs)}
        self.calls.append(entry)
        self.method_calls[name].append({"args": args, "kwargs": dict(kwargs)})

    def _resolve_outcome(self, outcome: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome()
        if callable(outcome):
            result = outcome(*args, **kwargs)
            if isinstance(result, BaseException):
                raise result
            return result
        return _deepcopy_or_same(outcome)

    def _dispatch(
        self,
        name: str,
        super_fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        with self.activated():
            self._record_call(name, args, kwargs)
            if name in self._queues:
                queue = self._queues[name]
                if not queue:
                    raise AssertionError(f"StubLLMService exhausted stubbed responses for {name}.")
                return self._resolve_outcome(queue.popleft(), args, kwargs)
            if name in self._handlers:
                return self._resolve_outcome(self._handlers[name], args, kwargs)
            return super_fn(*args, **kwargs)

    async def _dispatch_async(
        self,
        name: str,
        super_fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        self._record_call(name, args, kwargs)
        if name in self._queues:
            queue = self._queues[name]
            if not queue:
                raise AssertionError(f"StubLLMService exhausted stubbed responses for {name}.")
            result = self._resolve_outcome(queue.popleft(), args, kwargs)
            return await result if inspect.isawaitable(result) else result
        if name in self._handlers:
            result = self._resolve_outcome(self._handlers[name], args, kwargs)
            return await result if inspect.isawaitable(result) else result
        return await super_fn(*args, **kwargs)

    def solve_stage(self, rows: Any, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("solve_stage", super().solve_stage, (rows, *args), kwargs)

    async def request_completion(self, *args: Any, **kwargs: Any) -> Any:
        return await self._dispatch_async("request_completion", super().request_completion, args, kwargs)

    async def review_intent_candidate_async(self, *args: Any, **kwargs: Any) -> Any:
        return await self._dispatch_async(
            "review_intent_candidate_async",
            super().review_intent_candidate_async,
            args,
            kwargs,
        )

    async def repair_intent_components_async(self, *args: Any, **kwargs: Any) -> Any:
        return await self._dispatch_async(
            "repair_intent_components_async",
            super().repair_intent_components_async,
            args,
            kwargs,
        )

    def classify_manual_edit_conflict(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("classify_manual_edit_conflict", super().classify_manual_edit_conflict, args, kwargs)

    def generate_revision_search_proposal_sync(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch(
            "generate_revision_search_proposal_sync",
            super().generate_revision_search_proposal_sync,
            args,
            kwargs,
        )

    def generate_stage_assessment(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("generate_stage_assessment", super().generate_stage_assessment, args, kwargs)

    def generate_chat_reply(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("generate_chat_reply", super().generate_chat_reply, args, kwargs)

    def classify_turn_understanding(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("classify_turn_understanding", super().classify_turn_understanding, args, kwargs)

    def classify_revision_request(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("classify_revision_request", super().classify_revision_request, args, kwargs)

    def classify_challenge_reason(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("classify_challenge_reason", super().classify_challenge_reason, args, kwargs)

    def recover_human_edit_engagement(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("recover_human_edit_engagement", super().recover_human_edit_engagement, args, kwargs)

    def review_intent_feedback(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("review_intent_feedback", super().review_intent_feedback, args, kwargs)

    def review_question_answers(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("review_question_answers", super().review_question_answers, args, kwargs)

    def rewrite_intent_progress(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("rewrite_intent_progress", super().rewrite_intent_progress, args, kwargs)

    def translate_turns(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("translate_turns", super().translate_turns, args, kwargs)

    def generate_binary_step(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("generate_binary_step", super().generate_binary_step, args, kwargs)

    def build_revision_execution_contract(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch(
            "build_revision_execution_contract",
            super().build_revision_execution_contract,
            args,
            kwargs,
        )

    def proposal_route_features(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("proposal_route_features", super().proposal_route_features, args, kwargs)

    def execute_revision_operations(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("execute_revision_operations", super().execute_revision_operations, args, kwargs)

    def validate_ai_edit_diff(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("validate_ai_edit_diff", super().validate_ai_edit_diff, args, kwargs)

    def validate_frozen_requirements(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("validate_frozen_requirements", super().validate_frozen_requirements, args, kwargs)

    def normalize_revision_plan(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("normalize_revision_plan", super().normalize_revision_plan, args, kwargs)

    def validate_execution_brief(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("validate_execution_brief", super().validate_execution_brief, args, kwargs)

    def challenge_choice_card_copy(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("challenge_choice_card_copy", super().challenge_choice_card_copy, args, kwargs)

    def repair_stage_one_opening_display(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("repair_stage_one_opening_display", super().repair_stage_one_opening_display, args, kwargs)

    def sanitize_visible_model_text(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("sanitize_visible_model_text", super().sanitize_visible_model_text, args, kwargs)

    def validate_map_grounding_texts(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("validate_map_grounding_texts", super().validate_map_grounding_texts, args, kwargs)

    def visible_chinese_language_issue(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("visible_chinese_language_issue", super().visible_chinese_language_issue, args, kwargs)

    def strip_unverified_state_claims(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("strip_unverified_state_claims", super().strip_unverified_state_claims, args, kwargs)

    def validate_turn_understanding(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("validate_turn_understanding", super().validate_turn_understanding, args, kwargs)

    def dialogue_background_prompt(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("dialogue_background_prompt", super().dialogue_background_prompt, args, kwargs)

    def contains_user_design_direction(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("contains_user_design_direction", super().contains_user_design_direction, args, kwargs)

    def guidance_advice_request(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("guidance_advice_request", super().guidance_advice_request, args, kwargs)

    def guidance_confusion_request(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("guidance_confusion_request", super().guidance_confusion_request, args, kwargs)

    async def review_proposal_clarification(self, *args, **kwargs):
        return await self._dispatch_async("review_proposal_clarification", super().review_proposal_clarification, args, kwargs)

    def review_proposal_requirements(self, *args, **kwargs):
        return self._dispatch("review_proposal_requirements", super().review_proposal_requirements, args, kwargs)

    def generate_verified_proposal_failure_analysis(self, *args, **kwargs):
        return self._dispatch("generate_verified_proposal_failure_analysis", super().generate_verified_proposal_failure_analysis, args, kwargs)

    def generate_plain_chat(self, *args, **kwargs):
        return self._dispatch("generate_plain_chat", super().generate_plain_chat, args, kwargs)

    async def translate_with_model_fallback(self, *args, **kwargs):
        return await self._dispatch_async("translate_with_model_fallback", super().translate_with_model_fallback, args, kwargs)

    async def review_ordinary_intent_with_recovery(self, *args, **kwargs):
        return await self._dispatch_async("review_ordinary_intent_with_recovery", super().review_ordinary_intent_with_recovery, args, kwargs)

    def validate_manual_edit_pair(self, *args, **kwargs):
        return self._dispatch("validate_manual_edit_pair", super().validate_manual_edit_pair, args, kwargs)

    def safe_grounding_chat_reply(self, *args, **kwargs):
        return self._dispatch("safe_grounding_chat_reply", super().safe_grounding_chat_reply, args, kwargs)

    def server_snapshot_fallback_message(self, *args, **kwargs):
        return self._dispatch("server_snapshot_fallback_message", super().server_snapshot_fallback_message, args, kwargs)

    def apply_deterministic_guidance_fallback(self, *args, **kwargs):
        return self._dispatch("apply_deterministic_guidance_fallback", super().apply_deterministic_guidance_fallback, args, kwargs)


class FakeOnlineSyncGateway:
    """In-memory OnlineSyncGateway double recording synced events, versions, turns, and intentions."""

    def __init__(
        self,
        *,
        on_event: Callable[..., Any] | None = None,
        on_version: Callable[..., Any] | None = None,
        on_turn: Callable[..., Any] | None = None,
        on_intention: Callable[..., Any] | None = None,
        errors: Mapping[str, BaseException] | None = None,
        cocreation_deadline_seconds: int = 1200,
    ) -> None:
        self.events: list[dict[str, Any]] = []
        self.versions: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.intentions: list[dict[str, Any]] = []
        self.on_event = on_event
        self.on_version = on_version
        self.on_turn = on_turn
        self.on_intention = on_intention
        self._errors: dict[str, Any] = dict(errors or {})
        self.cocreation_deadline_seconds = cocreation_deadline_seconds

    def set_error(self, method_name: str, error: BaseException | None) -> FakeOnlineSyncGateway:
        if error is None:
            self._errors.pop(method_name, None)
        else:
            self._errors[method_name] = error
        return self

    def _maybe_raise(self, method_name: str) -> None:
        err = self._errors.get(method_name)
        if isinstance(err, BaseException):
            raise err
        if isinstance(err, type) and issubclass(err, BaseException):
            raise err()

    def sync_event(
        self,
        session: Any,
        event_type: str,
        stage_number: int | None = None,
        version_id: str | None = None,
        source: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        match_id = str(session["match_id"] or "").strip() if session is not None else ""
        player_number = session["player_number"] if session is not None else None
        is_demo = bool(session["demo_mode"]) if session is not None and "demo_mode" in session.keys() else False
        if is_demo or not match_id or player_number not in (1, 2):
            return False
        record = {
            "sessionId": session["id"],
            "matchId": match_id,
            "playerNumber": player_number,
            "eventType": event_type,
            "stageNumber": stage_number,
            "versionId": version_id,
            "source": source,
            "summary": summary,
            "metadata": copy.deepcopy(metadata) if metadata is not None else None,
        }
        self.events.append(record)
        self._maybe_raise("sync_event")
        if self.on_event is not None:
            result = self.on_event(session, event_type, stage_number, version_id, source, summary, metadata)
            if isinstance(result, bool):
                return result
        return True

    def sync_version(
        self,
        database: Any,
        session: Any,
        version_row: Any,
        *,
        event_type: str = "stage",
        duration_seconds: float | None = None,
    ) -> bool:
        payload = build_version_sync_event(
            database,
            session,
            version_row,
            event_type=event_type,
            duration_seconds=duration_seconds,
            cocreation_deadline_seconds=self.cocreation_deadline_seconds,
        )
        if payload is None:
            return False
        self.versions.append(copy.deepcopy(payload))
        self._maybe_raise("sync_version")
        if self.on_version is not None:
            result = self.on_version(database, session, version_row, event_type=event_type, duration_seconds=duration_seconds)
            if isinstance(result, bool):
                return result
        return True

    def sync_turn(
        self,
        session: Any,
        turn_id: str,
        role: str,
        content: str,
        language: str,
        stage_number: int | None,
        created_at: str,
        guidance: dict[str, Any] | None = None,
    ) -> bool:
        match_id = str(session["match_id"] or "").strip() if session is not None else ""
        player_number = session["player_number"] if session is not None else None
        is_demo = bool(session["demo_mode"]) if session is not None and "demo_mode" in session.keys() else False
        if is_demo or not match_id or player_number not in (1, 2):
            return False
        record = {
            "sessionId": session["id"],
            "matchId": match_id,
            "playerNumber": player_number,
            "turnId": turn_id,
            "role": role,
            "content": content,
            "language": language,
            "stageNumber": stage_number,
            "createdAt": created_at,
            "guidance": copy.deepcopy(guidance) if guidance is not None else None,
        }
        self.turns.append(record)
        self._maybe_raise("sync_turn")
        if self.on_turn is not None:
            result = self.on_turn(session, turn_id, role, content, language, stage_number, created_at, guidance)
            if isinstance(result, bool):
                return result
        return True

    def sync_final_intention(
        self,
        session: Any,
        content: str,
        language: str | None = None,
        created_at: str | None = None,
        *,
        event_id: str = "",
    ) -> bool:
        if session is None:
            return False
        match_id = str(session["match_id"] or "").strip()
        player_number = session["player_number"]
        is_demo = bool(session["demo_mode"]) if "demo_mode" in session.keys() else False
        if is_demo or not match_id or player_number not in (1, 2):
            return False
        record = {
            "sessionId": session["id"],
            "matchId": match_id,
            "playerNumber": player_number,
            "content": content,
            "language": language,
            "createdAt": created_at,
            "eventId": event_id,
        }
        self.intentions.append(record)
        self._maybe_raise("sync_final_intention")
        if self.on_intention is not None:
            result = self.on_intention(session, content, language, created_at, event_id=event_id)
            if isinstance(result, bool):
                return result
        return True


def create_test_services(
    tmp_dir: str | Path,
    *,
    settings_overrides: Mapping[str, Any] | None = None,
    llm_config_overrides: Mapping[str, Any] | None = None,
    llm: CoCreationLLMService | None = None,
    client_factory: Callable[[str, str, float], Any] | None = None,
    online_sync: OnlineSyncGateway | None = None,
    clock: Clock | None = None,
    demo_level_generator: Callable[..., Any] | None = None,
    **service_overrides: Any,
) -> CoCreationServices:
    """Construct isolated CoCreationServices backed by a temp SQLite file and test doubles."""
    base_dir = Path(tmp_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    active_clock: Clock = clock if clock is not None else FakeClock()

    default_settings = CoCreationSettings(
        database_path=base_dir / "cocreation.sqlite3",
        online_backend_base_url="",
        intention_sync_secret="test-intention-sync-secret",
        token_secret="test-token-secret",
        sweeper_enabled=False,
    )
    if settings_overrides:
        merged_settings = dict(settings_overrides)
        if "database_path" in merged_settings and merged_settings["database_path"] is not None:
            merged_settings["database_path"] = Path(merged_settings["database_path"])
        if "allowed_origins" in merged_settings and not isinstance(merged_settings["allowed_origins"], tuple):
            merged_settings["allowed_origins"] = tuple(merged_settings["allowed_origins"])
        active_settings = CoCreationSettings(
            **{
                **{field_name: getattr(default_settings, field_name) for field_name in default_settings.__dataclass_fields__},
                **merged_settings,
            }
        )
    else:
        active_settings = default_settings

    base_llm_config = LLMConfig(
        api_key="test-kimi-key",
        base_url="https://example.invalid/v1",
    )
    if llm_config_overrides:
        active_llm_config = LLMConfig(
            **{
                **{field_name: getattr(base_llm_config, field_name) for field_name in base_llm_config.__dataclass_fields__},
                **dict(llm_config_overrides),
            }
        )
    else:
        active_llm_config = base_llm_config

    if llm is not None:
        active_llm = llm
    else:
        active_llm = StubLLMService(
            config=active_llm_config,
            client_factory=client_factory or offline_client,
            clock=active_clock,
        )

    active_online_sync: OnlineSyncGateway = (
        online_sync
        if online_sync is not None
        else FakeOnlineSyncGateway(cocreation_deadline_seconds=active_settings.cocreation_deadline_seconds)
    )
    overrides = dict(service_overrides)
    repository = overrides.pop("repository", None) or SQLiteRepository(
        active_settings.database_path,
        clock=active_clock,
    )
    return CoCreationServices(
        settings=active_settings,
        repository=repository,
        llm=active_llm,
        online_sync=active_online_sync,
        clock=active_clock,
        demo_level_generator=demo_level_generator if demo_level_generator is not None else generate_demo_level,
        **overrides,
    )


def create_test_client(
    services: CoCreationServices,
    *,
    base_url: str = "https://testserver",
    raise_server_exceptions: bool = True,
) -> TestClient:
    """Build a FastAPI TestClient wired to an explicit CoCreationServices container."""
    return TestClient(
        create_app(services),
        base_url=base_url,
        raise_server_exceptions=raise_server_exceptions,
    )


class SessionTestCase(unittest.TestCase):
    """Give each test its own database, app, browser, and offline collaborators."""

    def open_workspace(self, *, clock=None, llm_handlers=None, **options):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.services = create_test_services(directory, clock=clock, **options)
        self.services.llm.set_handlers(llm_handlers or {})
        self.client = create_test_client(self.services)
        self.addCleanup(self.client.close)

    @contextmanager
    def using_dependencies(self, *, llm_handlers=None, llm_options=None, online_handlers=None, settings=None, **options):
        old_services, old_client = self.services, self.client
        if llm_options or llm_handlers:
            model_options = dict(llm_options or {})
            model_options['handlers'] = {**model_options.get('handlers', {}), **(llm_handlers or {})}
            options['llm'] = offline_llm(base=old_services.llm, **model_options)
        if online_handlers:
            options['online_sync'] = ScenarioOnlineSync(old_services.online_sync, online_handlers)
        if settings:
            options['settings'] = replace(old_services.settings, **settings)
        services = replace(old_services, **options)
        client = create_test_client(services, base_url=str(old_client.base_url))
        client.cookies = old_client.cookies
        self.services, self.client = services, client
        try:
            yield services
        finally:
            old_client.cookies = client.cookies
            client.close()
            self.services, self.client = old_services, old_client


class ScenarioOnlineSync:
    """Inject faults at the outgoing synchronization boundary for a scenario."""

    def __init__(self, delegate, handlers):
        self.delegate = delegate
        self.handlers = handlers

    def sync_event(self, session, event_type, *, metadata=None, **kwargs):
        if 'event' in self.handlers:
            return self.handlers['event'](session, metadata)
        return self.delegate.sync_event(session, event_type, metadata=metadata, **kwargs)

    def sync_version(self, database, session, version_row, *, event_type, duration_seconds=None):
        if 'version' in self.handlers:
            return self.handlers['version'](session['id'], version_row['id'], event_type)
        if 'version_event' in self.handlers:
            event = build_version_sync_event(database, session, version_row, event_type,
                                             duration_seconds=duration_seconds)
            if event is not None:
                return self.handlers['version_event'](session, event)
        return self.delegate.sync_version(database, session, version_row, event_type=event_type,
                                          duration_seconds=duration_seconds)

    def sync_turn(self, *args, **kwargs):
        return self.delegate.sync_turn(*args, **kwargs)

    def sync_final_intention(self, session, content, language=None, created_at=None, *, event_id=''):
        if 'intention' in self.handlers:
            return self.handlers['intention'](session['match_id'], session['player_number'], content,
                                              event_id=event_id, session_id=session['id'])
        return self.delegate.sync_final_intention(session, content, language, created_at, event_id=event_id)
