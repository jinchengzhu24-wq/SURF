from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

from cocreation.clock import Clock, SYSTEM_CLOCK
from cocreation.domain.level_validation import validate_and_solve

from . import binary_model as _binary_model
from . import chat_generation as _chat_generation
from . import intent_and_review as _intent_and_review
from . import proposal_planning as _proposal_planning
from . import snapshot_grounding as _snapshot_grounding
from . import translations as _translations
from .config_and_runtime import (
    _ACTIVE_LLM_SERVICE,
    LLMConfig,
    _default_create_async_client,
    _default_log_llm_event,
    _default_request_completion,
)


class CoCreationLLMService:
    """Explicitly wired 8010 Kimi LLM service and execution boundary."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        client_factory: Callable[[str, str, float], Any] | None = None,
        clock: Clock | None = None,
        event_logger: Callable[..., None] | None = None,
        completion_requester: Callable[..., Awaitable[Any]] | None = None,
        intent_candidate_reviewer: Callable[..., Awaitable[Any]] | None = None,
        intent_component_repairer: Callable[..., Awaitable[Any]] | None = None,
        manual_edit_conflict_classifier: Callable[..., Any] | None = None,
        revision_search_generator: Callable[..., Any] | None = None,
        solver_fn: Callable[..., Any] | None = None,
        remaining_until_fn: Callable[[float | None], float] | None = None,
    ) -> None:
        self.config = config if config is not None else LLMConfig()
        self.clock = clock if clock is not None else SYSTEM_CLOCK
        self.client_factory = client_factory
        self.event_logger = event_logger
        self.completion_requester = completion_requester
        self.intent_candidate_reviewer = intent_candidate_reviewer
        self.intent_component_repairer = intent_component_repairer
        self.manual_edit_conflict_classifier = manual_edit_conflict_classifier
        self.revision_search_generator = revision_search_generator
        self.solver_fn = solver_fn
        self.remaining_until_fn = remaining_until_fn

    @contextmanager
    def activated(self):
        token = _ACTIVE_LLM_SERVICE.set(self)
        try:
            yield self
        finally:
            _ACTIVE_LLM_SERVICE.reset(token)

    def credentials(self) -> tuple[str, str]:
        return (self.config.api_key, self.config.base_url)

    def remaining_until(self, deadline: float | None) -> float:
        if self.remaining_until_fn is not None:
            return float(self.remaining_until_fn(deadline))
        if deadline is None:
            return float(self.config.backend_request_timeout_seconds)
        return max(0.0, float(deadline) - self.clock.monotonic())

    def log_event(self, event: str, **fields: Any) -> None:
        if self.event_logger is not None:
            self.event_logger(event, **fields)
            return
        _default_log_llm_event(self.clock, event, **fields)

    def solve_stage(self, rows: Any, *args: Any, **kwargs: Any) -> Any:
        if self.solver_fn is not None:
            return self.solver_fn(rows, *args, **kwargs)
        if 'clock' not in kwargs:
            kwargs['clock'] = self.clock
        return validate_and_solve(rows, *args, **kwargs)

    def create_async_client(self, api_key: str, base_url: str, timeout_seconds: float) -> Any:
        if self.client_factory is not None:
            return self.client_factory(api_key, base_url, timeout_seconds)
        return _default_create_async_client(api_key, base_url, timeout_seconds)

    create_client = create_async_client

    async def request_completion(
        self,
        api_key: str,
        base_url: str,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        *args: Any,
        timeout_seconds: float | None = None,
        temperature: float = 0.6,
        structured: bool = True,
        response_format: dict[str, Any] | None = None,
        response_format_override: dict[str, Any] | None = None,
        task: str | None = None,
        manual_edit_direction_count: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> Any:
        effective_max_tokens = max_tokens if max_tokens is not None else max_completion_tokens
        effective_timeout = timeout_seconds
        effective_temperature = temperature
        effective_structured = structured
        if args:
            if len(args) == 1:
                effective_timeout = float(args[0])
            elif len(args) >= 2 and isinstance(args[1], bool):
                effective_timeout = float(args[0])
                effective_structured = args[1]
                if len(args) >= 3:
                    task = args[2]
                if len(args) >= 4:
                    manual_edit_direction_count = args[3]
                if len(args) >= 5:
                    response_format_override = args[4]
            else:
                effective_temperature = float(args[0])
                effective_timeout = float(args[1])
        effective_rf = response_format_override if response_format_override is not None else response_format
        with self.activated():
            if self.completion_requester is not None:
                return await self.completion_requester(
                    api_key,
                    base_url,
                    model,
                    messages,
                    effective_max_tokens,
                    effective_timeout,
                    structured=effective_structured,
                    task=task,
                    manual_edit_direction_count=manual_edit_direction_count,
                    response_format_override=effective_rf,
                )
            return await _default_request_completion(
                api_key,
                base_url,
                model,
                messages,
                effective_max_tokens,
                effective_timeout,
                structured=effective_structured,
                task=task,
                manual_edit_direction_count=manual_edit_direction_count,
                response_format_override=effective_rf,
                temperature=effective_temperature,
            )

    _request_completion = request_completion

    def readiness_status(self) -> dict[str, Any]:
        key = str(self.config.api_key or '').strip()
        configured = bool(key) and key not in {'your_kimi_api_key_here', 'your_llm_api_key_here'}
        return {
            'configured': configured,
            'baseUrl': self.config.base_url,
            'model': self.config.model,
        }

    async def review_intent_candidate_async(self, *args: Any, **kwargs: Any) -> Any:
        with self.activated():
            if self.intent_candidate_reviewer is not None:
                return await self.intent_candidate_reviewer(*args, **kwargs)
            return await _intent_and_review._review_intent_candidate_async(*args, **kwargs)

    async def repair_intent_components_async(self, *args: Any, **kwargs: Any) -> Any:
        with self.activated():
            if self.intent_component_repairer is not None:
                return await self.intent_component_repairer(*args, **kwargs)
            return await _intent_and_review._repair_intent_components_async(*args, **kwargs)

    def classify_manual_edit_conflict(self, *args: Any, **kwargs: Any) -> Any:
        with self.activated():
            if self.manual_edit_conflict_classifier is not None:
                return self.manual_edit_conflict_classifier(*args, **kwargs)
            return _intent_and_review._classify_manual_edit_conflict(*args, **kwargs)

    def generate_revision_search_proposal_sync(self, *args: Any, **kwargs: Any) -> Any:
        with self.activated():
            if self.revision_search_generator is not None:
                return self.revision_search_generator(*args, **kwargs)
            if not args:
                api_key, base_url = self.credentials()
                if kwargs.get('api_key') is None:
                    kwargs['api_key'] = api_key
                if kwargs.get('base_url') is None:
                    kwargs['base_url'] = base_url
            return _proposal_planning._generate_revision_search_proposal_sync(*args, **kwargs)

    def generate_stage_assessment(
        self,
        conversation,
        rows,
        language,
        solver_metrics,
        play_summary,
        request_id,
        stage_context=None,
        _deadline=None,
    ):
        with self.activated():
            return _chat_generation.generate_stage_assessment(
                conversation,
                rows,
                language,
                solver_metrics,
                play_summary,
                request_id,
                stage_context=stage_context,
                _deadline=_deadline,
            )

    def generate_chat_reply(
        self,
        conversation,
        rows,
        request_id,
        language='en',
        solver_metrics=None,
        play_summary=None,
        assessment_only=False,
        proposal_validator=None,
        stage_context=None,
        _max_attempts=None,
        _deadline=None,
    ):
        attempts = self.config.chat_max_attempts if _max_attempts is None else _max_attempts
        with self.activated():
            return _chat_generation.generate_chat_reply(
                conversation,
                rows,
                request_id,
                language=language,
                solver_metrics=solver_metrics,
                play_summary=play_summary,
                assessment_only=assessment_only,
                proposal_validator=proposal_validator,
                stage_context=stage_context,
                _max_attempts=attempts,
                _deadline=_deadline,
            )

    def classify_turn_understanding(
        self,
        conversation,
        snapshot,
        request_id,
        *,
        forced_proposal=False,
        proposal_context=None,
        _deadline=None,
    ):
        with self.activated():
            return _intent_and_review.classify_turn_understanding(
                conversation,
                snapshot,
                request_id,
                forced_proposal=forced_proposal,
                proposal_context=proposal_context,
                _deadline=_deadline,
            )

    def classify_revision_request(self, conversation, stage_context=None):
        with self.activated():
            return _proposal_planning.classify_revision_request(conversation, stage_context=stage_context)

    def classify_challenge_reason(
        self,
        user_reason,
        hypotheses,
        proposal_summary,
        request_id,
        *,
        _deadline=None,
        simple=False,
        comparison_validator=None,
        stage_snapshot=None,
    ):
        with self.activated():
            return _intent_and_review.classify_challenge_reason(
                user_reason,
                hypotheses,
                proposal_summary,
                request_id,
                _deadline=_deadline,
                simple=simple,
                comparison_validator=comparison_validator,
                stage_snapshot=stage_snapshot,
            )

    def recover_human_edit_engagement(self, source, request_id, deadline):
        with self.activated():
            return _intent_and_review.recover_human_edit_engagement(source, request_id, deadline)

    def review_intent_feedback(
        self,
        candidate,
        active_inclinations,
        evidence_summary,
        language,
        request_id,
        *,
        candidate_claims=None,
        _deadline=None,
    ):
        with self.activated():
            return _intent_and_review.review_intent_feedback(
                candidate,
                active_inclinations,
                evidence_summary,
                language,
                request_id,
                candidate_claims=candidate_claims,
                _deadline=_deadline,
            )

    def review_question_answers(self, questions, user_reply, language, request_id, *, _deadline=None):
        with self.activated():
            return _intent_and_review.review_question_answers(
                questions,
                user_reply,
                language,
                request_id,
                _deadline=_deadline,
            )

    def rewrite_intent_progress(self, kind, canonical_text, safe_evidence, language, request_id, *, _deadline=None):
        with self.activated():
            return _intent_and_review.rewrite_intent_progress(
                kind,
                canonical_text,
                safe_evidence,
                language,
                request_id,
                _deadline=_deadline,
            )

    def translate_turns(self, items, target_language, request_id, *, _deadline=None):
        with self.activated():
            return _translations.translate_turns(
                items,
                target_language,
                request_id,
                _deadline=_deadline,
            )

    def generate_binary_step(
        self,
        topic,
        context,
        validator,
        strategy_validator,
        request_id,
        deadline,
        preflight_budget=20.0,
    ):
        with self.activated():
            return _binary_model.generate(
                topic,
                context,
                validator,
                strategy_validator,
                request_id,
                deadline,
                preflight_budget=preflight_budget,
                service=self,
            )

    def build_revision_execution_contract(self, plan, authorized_brief, stage_context=None):
        with self.activated():
            return _proposal_planning._build_revision_execution_contract(
                plan,
                authorized_brief,
                stage_context=stage_context,
            )

    def proposal_route_features(self, rows, validation=None, entity_bindings=None, *, deadline=None):
        with self.activated():
            return _proposal_planning._proposal_route_features(
                rows,
                validation,
                entity_bindings=entity_bindings,
                deadline=deadline,
            )

    def execute_revision_operations(self, base_rows, operations, revision_contract=None, strategy_index=0):
        with self.activated():
            return _proposal_planning.execute_revision_operations(
                base_rows,
                operations,
                revision_contract,
                strategy_index,
            )

    def validate_ai_edit_diff(self, base_rows, candidate_rows):
        with self.activated():
            return _proposal_planning.validate_ai_edit_diff(base_rows, candidate_rows)

    def validate_frozen_requirements(
        self,
        base_rows,
        candidate_rows,
        workflow,
        baseline_validation,
        candidate_validation,
        entity_bindings=None,
        *,
        deadline=None,
    ):
        # Support both (base_rows, candidate_rows, workflow, ...) and (workflow, base_rows, candidate_rows, ...).
        if isinstance(base_rows, dict) and isinstance(candidate_rows, (list, tuple)):
            workflow, base_rows, candidate_rows = base_rows, candidate_rows, workflow
        with self.activated():
            return _proposal_planning.validate_frozen_requirements(
                base_rows,
                candidate_rows,
                workflow,
                baseline_validation,
                candidate_validation,
                entity_bindings,
                deadline=deadline,
            )

    def normalize_revision_plan(self, value, rows=None, entity_bindings=None):
        with self.activated():
            return _proposal_planning.normalize_revision_plan(value, rows=rows, entity_bindings=entity_bindings)

    def validate_execution_brief(self, value, rows=None, entity_bindings=None):
        with self.activated():
            return _proposal_planning.validate_execution_brief(value, rows=rows, entity_bindings=entity_bindings)

    def challenge_choice_card_copy(self, user_reason, proposal_summary, language='en', *, legacy=False):
        with self.activated():
            return _snapshot_grounding.challenge_choice_card_copy(
                user_reason,
                proposal_summary,
                language=language,
                legacy=legacy,
            )

    def repair_stage_one_opening_display(self, message, rows, language):
        with self.activated():
            return _chat_generation._repair_stage_one_opening_display(message, rows, language)

    def sanitize_visible_model_text(self, value, language='en'):
        with self.activated():
            return _snapshot_grounding._sanitize_visible_model_text(value, language=language)

    def validate_map_grounding_texts(self, texts, rows, *, historical_reference=False, entity_bindings=None):
        with self.activated():
            return _snapshot_grounding._validate_map_grounding_texts(
                texts,
                rows,
                historical_reference=historical_reference,
                entity_bindings=entity_bindings,
            )

    def visible_chinese_language_issue(self, value):
        with self.activated():
            return _snapshot_grounding._visible_chinese_language_issue(value)

    def strip_unverified_state_claims(self, body):
        with self.activated():
            return _intent_and_review._strip_unverified_state_claims(body)

    def validate_turn_understanding(self, payload, snapshot, latest_user_text='', *, forced_proposal=False, proposal_context=None):
        with self.activated():
            return _intent_and_review._validate_turn_understanding(
                payload,
                snapshot,
                latest_user_text=latest_user_text,
                forced_proposal=forced_proposal,
                proposal_context=proposal_context,
            )

    def dialogue_background_prompt(self, stage_context):
        return _chat_generation._dialogue_background_prompt(stage_context)

    def contains_user_design_direction(self, message):
        return _proposal_planning._contains_user_design_direction(message)

    def guidance_advice_request(self, message):
        return _proposal_planning._guidance_advice_request(message)

    def guidance_confusion_request(self, message):
        return _proposal_planning._guidance_confusion_request(message)

    async def review_proposal_clarification(self, *args, **kwargs):
        with self.activated():
            return await _proposal_planning._review_proposal_clarification(*args, **kwargs)

    def review_proposal_requirements(self, *args, **kwargs):
        with self.activated():
            return _proposal_planning._review_proposal_requirements(*args, **kwargs)

    def generate_verified_proposal_failure_analysis(self, *args, **kwargs):
        with self.activated():
            return _proposal_planning._generate_verified_proposal_failure_analysis(*args, **kwargs)

    def generate_plain_chat(self, *args, **kwargs):
        with self.activated():
            return _chat_generation._generate_plain_chat_sync(*args, **kwargs)

    async def translate_with_model_fallback(self, *args, **kwargs):
        with self.activated():
            return await _translations._translate_with_model_fallback(*args, **kwargs)

    async def review_ordinary_intent_with_recovery(self, *args, **kwargs):
        with self.activated():
            return await _intent_and_review._review_ordinary_intent_with_recovery(*args, **kwargs)

    def validate_manual_edit_pair(self, *args, **kwargs):
        with self.activated():
            return _chat_generation._validate_manual_edit_pair_payload(*args, **kwargs)

    def safe_grounding_chat_reply(self, *args, **kwargs):
        with self.activated():
            return _chat_generation._safe_grounding_chat_reply(*args, **kwargs)

    def server_snapshot_fallback_message(self, *args, **kwargs):
        with self.activated():
            return _chat_generation._server_snapshot_fallback_message(*args, **kwargs)

    def apply_deterministic_guidance_fallback(self, *args, **kwargs):
        with self.activated():
            return _proposal_planning._apply_deterministic_guidance_fallback(*args, **kwargs)
