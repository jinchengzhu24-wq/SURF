from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from contextvars import ContextVar
from difflib import SequenceMatcher
import hashlib
import json
import os
import re
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from cocreation.clock import Clock, SYSTEM_CLOCK
from cocreation.domain.proposal_search import (
    Focus,
    MetricGoal,
    ProposalSearchExhausted,
    RevisionPlan,
    parse_revision_plan,
    search_revision_plan,
    validate_revision_plan_against_map,
)
from cocreation.domain.level_validation import (
    TILE_LEGEND,
    build_map_facts,
    build_stage_snapshot,
    analyze_user_map_claims,
    minimum_pushes,
    summarize_verified_diff,
    validate_and_solve,
)
from cocreation.domain.design_context import validate_design_context_patch
from cocreation.persistence.repository import map_fingerprint
from cocreation.reliability.reply_reliability import (ComponentCheck, OptionalRepairBudget, parse_json_object, requirement_cache_key,
    CURRENT_REPLY_TRACE, REPLY_PIPELINE_VERSION, trace_reply, failure_reason)
from cocreation.reliability.component_patches import PatchRequest, PatchError, Target, patch_response_format, dependent_components, restore_operation_sources, PATCH_VERSION
from cocreation.domain.conversation_evidence import CONTEXT_VERSION
from cocreation.domain.understanding_review import review_schema, review_targets, merge_review, review_patch_request, REVIEW_VERSION
from cocreation.domain import proposal_options
from cocreation.domain.proposal_understanding import (
    turn_schema, validate_topic, designer_sources, critical_review_reason, topic_signature, validate_plan_scope,
    engagement_schema, validate_engagement, MEANING_REVIEW_VERSION,
    understanding_patch_request, validate_option_plan_scope,
)
from cocreation.domain.proposal_diagnostics import (proposal_phase, trace_proposal, trace_repair, reason_code, safe_issues,
    CURRENT_PROPOSAL_DIAGNOSTIC, ProposalValidationError)
from cocreation.domain.design_requirements import (
    requirement_response_schema, validate_requirement_record, evaluate_requirements, preflight_requirements,
    goal_rank, tradeoff_message, protected_change_requested,
    validate_requirement_components,
    requirement_model_view, REQUIREMENT_SEMANTICS, REQUIREMENT_DEFINITIONS, INTERPRETATION_VERSION,
)
from cocreation.domain.revision_workflow import (
    SemanticConstraintError,
    build_revision_workflow,
    validate_semantic_constraints,
)


KIMI_MODEL = 'kimi-k2.6'
KIMI_BASE_URL = 'https://api.moonshot.cn/v1'
UNIFIED_MODEL = KIMI_MODEL
DEFAULT_MODEL = KIMI_MODEL
DEFAULT_PROPOSAL_MODEL = KIMI_MODEL
DEFAULT_BASE_URL = KIMI_BASE_URL
BACKEND_REQUEST_TIMEOUT_SECONDS = 120.0
LLM_INTERNAL_DEADLINE_SECONDS = 116.0
PRIMARY_ATTEMPT_TIMEOUT_SECONDS = 70.0
MIN_RETRY_BUDGET_SECONDS = 20.0
CHAT_TIMEOUT_SECONDS = BACKEND_REQUEST_TIMEOUT_SECONDS
CHAT_MAX_ATTEMPTS = 2
ORDINARY_CHAT_MAX_ATTEMPTS = 3
PROPOSAL_GENERATION_ATTEMPTS = 2
PROPOSAL_REQUEST_TIMEOUT_SECONDS = 300.0
PROPOSAL_INTERNAL_DEADLINE_SECONDS = 296.0
PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS = 140.0
PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS = 35.0
PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS = 180.0
PROPOSAL_SEARCH_DEADLINE_SECONDS = 56.0
REVISION_CONTRACT_SCHEMA_VERSION = 2
REVISION_MIN_CHANGED_CELLS = 1
REVISION_MAX_CHANGED_CELLS = 12
PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS = 30.0
PROPOSAL_ATTEMPT_TIMEOUT_SECONDS = PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS
CHAT_MAX_COMPLETION_TOKENS = 2600
CHAT_MAX_TOKENS = CHAT_MAX_COMPLETION_TOKENS
PLAIN_CHAT_TIMEOUT_SECONDS = BACKEND_REQUEST_TIMEOUT_SECONDS
PLAIN_PRIMARY_TIMEOUT_SECONDS = 70.0
ORDINARY_CHAT_ATTEMPT_TIMEOUTS = (55.0, 35.0)
PLAIN_CHAT_MAX_COMPLETION_TOKENS = 2200
PLAIN_CHAT_MAX_TOKENS = PLAIN_CHAT_MAX_COMPLETION_TOKENS
PROPOSAL_MAX_COMPLETION_TOKENS = 2400
PROPOSAL_MAX_TOKENS = PROPOSAL_MAX_COMPLETION_TOKENS
PROPOSAL_PLAN_MAX_COMPLETION_TOKENS = 2400
PROPOSAL_PLAN_MAX_TOKENS = PROPOSAL_PLAN_MAX_COMPLETION_TOKENS
PROPOSAL_OPERATION_MAX_COMPLETION_TOKENS = 1400
PROPOSAL_OPERATION_MAX_TOKENS = PROPOSAL_OPERATION_MAX_COMPLETION_TOKENS
PROPOSAL_CANDIDATE_LIMIT = 3
PROPOSAL_OPERATION_LIMIT = 24
TRANSLATION_MAX_COMPLETION_TOKENS = 3200
TRANSLATION_MAX_TOKENS = TRANSLATION_MAX_COMPLETION_TOKENS
CHAT_RESPONSE_MAX_LENGTH = 8000
CHAT_RESPONSE_HARD_LENGTH = 24000
ROUTE_REASONING_PASSAGE_LIMIT = 2
PERSONAL_REFLECTION_SENTENCE_LIMIT = 2
COORDINATE_LINK_LIMIT = 12
CHAT_MAX_PARAGRAPHS = 6
CHAT_MAX_SENTENCES = 12
CHAT_PARAGRAPH_MAX_CHINESE_CHARS = 240
CHAT_PARAGRAPH_MAX_LATIN_WORDS = 160
CHAT_COMPACT_EXPERIMENT_ENV = 'COCREATION_CHAT_COMPACT_EXPERIMENT'
PROPOSAL_CLARIFICATION_COMPACT_EXPERIMENT_ENV = 'COCREATION_PROPOSAL_CLARIFICATION_COMPACT_EXPERIMENT'
PROMPT_VERSION = 'cocreation-v66-binary-promises'
INTENT_FEEDBACK_REVIEW_MAX_COMPLETION_TOKENS = 500
INTENT_CANDIDATE_REVIEW_MAX_COMPLETION_TOKENS = 1400
INTENT_COMPONENT_REPAIR_MAX_COMPLETION_TOKENS = 1400
INTENT_REVIEW_VERSION = 'intent-candidate-review-v2-joint-components'
QUESTION_ANSWER_REVIEW_MAX_COMPLETION_TOKENS = 700
INTENT_PROGRESS_REWRITE_MAX_COMPLETION_TOKENS = 700
MANUAL_EDIT_PAIR_MAX_COMPLETION_TOKENS = 3200
MANUAL_EDIT_CONFLICT_MAX_COMPLETION_TOKENS = 1600
MANUAL_EDIT_DIRECTION_KINDS = frozenset({'design_goal', 'design_constraint', 'confirmed_decision', 'confirmed_inclination'})
MANUAL_EDIT_EFFECT_EVIDENCE_KINDS = frozenset({'verified_map_diff', 'verified_change_summary', 'solver_comparison', 'play_evidence'})
GUIDANCE_MOVES = {'observe_stage', 'clarify_intent', 'offer_perspective', 'challenge_tradeoff', 'reflect_on_play', 'offer_revision', 'deliver_revision'}
INTENT_CONFIDENCE_LEVELS = {'low', 'medium', 'high'}
UI_CUE_TYPES = {'manual_edit', 'warning', 'tradeoff', 'clarification'}
GUIDANCE_REQUEST_MODES = {'revision_advice', 'discussion', 'needs_clarification', 'proposal_blocked', 'none'}
DISAGREEMENT_STATUSES = {'active', 'acknowledged', 'resolved'}
DISAGREEMENT_SUBJECTS = {'ai_revision', 'human_edit', 'user_request'}
DISAGREEMENT_RESOLUTIONS = {'user', 'ai', 'compromise', 'retain_current'}
_PROVIDER_DETAIL_LIMIT = 360
_VISIBLE_CHINESE_LATIN_ALLOWLIST = {'P', 'B', 'T', 'B1', 'B2', 'T1', 'T2', 'Stage', 'AI', 'Kimi', 'DeepSeek', 'Moonshot', 'Sokoban', 'OpenAI', 'ChatGPT', 'WebGL', 'FastAPI', 'JavaScript', 'TypeScript', 'iPhone'}

def _structured_response_format(task=None, manual_edit_direction_count=None):
    """Build a compact schema contract that Kimi can follow reliably.

    The application still performs the authoritative validation after parsing.
    This schema exists to keep Kimi's JSON envelope stable and to avoid asking
    the model to infer the wire shape from the much larger design rules.
    """
    task = str(task or 'chat')
    if task.endswith('_patch'):
        return patch_response_format()
    if task == 'translation_permission_review':
        return {'type': 'json_schema', 'json_schema': {'name': 'cocreation_translation_permission_review', 'strict': True, 'schema': {'type': 'object', 'additionalProperties': False, 'properties': {'accepted': {'type': 'boolean'}}, 'required': ['accepted']}}}
    if task == 'proposal_clarification_review':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'bodyIssues': {'type': 'array', 'items': {'type': 'string'}}, 'questionIssue': {'type': ['string', 'null']}}, 'required': ['bodyIssues', 'questionIssue']}
        return {'type': 'json_schema', 'json_schema': {'name': 'cocreation_proposal_clarification_review', 'strict': True, 'schema': schema}}
    if task == 'proposal_failure_analysis':
        return {'type': 'json_schema', 'json_schema': {'name': 'cocreation_proposal_failure_analysis', 'strict': True, 'schema': {'type': 'object', 'additionalProperties': False, 'properties': {'body': {'type': 'string'}}, 'required': ['body']}}}
    if task in {'manual_edit_assessment_pair_conflict', 'manual_edit_assessment_pair_no_conflict'}:
        assessment_schema = {'type': 'object', 'additionalProperties': False, 'properties': {'solutionSummary': {'type': 'string'}, 'difficultyOpinion': {'type': 'string'}, 'features': {'type': 'array', 'items': {'type': 'string'}}, 'suggestions': {'type': 'array', 'items': {'type': 'string'}}, 'satisfactionQuestion': {'type': ['string', 'null']}}, 'required': ['solutionSummary', 'difficultyOpinion', 'features', 'suggestions', 'satisfactionQuestion']}
        conflict_schema = {'type': 'object', 'additionalProperties': False, 'properties': {'userPosition': {'type': 'string'}, 'aiPosition': {'type': 'string'}, 'coreDisagreement': {'type': 'string'}, 'nextQuestion': {'type': 'string'}}, 'required': ['userPosition', 'aiPosition', 'coreDisagreement', 'nextQuestion']}
        if task == 'manual_edit_assessment_pair_no_conflict':
            conflict_schema = {'type': 'null'}
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'openingMessage': {'type': 'string'}, 'assessment': assessment_schema, 'reviewMessage': {'type': 'string'}, 'conflict': conflict_schema}, 'required': ['openingMessage', 'assessment', 'reviewMessage', 'conflict']}
        name = 'cocreation_manual_edit_assessment_pair_conflict' if task == 'manual_edit_assessment_pair_conflict' else 'cocreation_manual_edit_assessment_pair_no_conflict'
    elif task == 'manual_edit_conflict_classification':
        comparison_schema = {'type': 'object', 'additionalProperties': False, 'properties': {'relation': {'type': 'string', 'enum': ['conflict', 'aligned', 'tradeoff', 'unrelated', 'unclear']}, 'explanation': {'type': 'string'}}, 'required': ['relation', 'explanation']}
        direction_count = int(manual_edit_direction_count or 0)
        if direction_count < 1:
            raise ValueError('manual_edit_direction_count is required for conflict classification.')
        comparison_properties = {f'direction_{index}': comparison_schema for index in range(1, direction_count + 1)}
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'comparisons': {'type': 'object', 'additionalProperties': False, 'properties': comparison_properties, 'required': list(comparison_properties)}}, 'required': ['comparisons']}
        name = 'cocreation_manual_edit_conflict_classification'
    elif task == 'challenge_reason_classification':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'relation': {'type': 'string', 'enum': ['primary', 'secondary', 'different', 'unclear']}, 'merit': {'type': 'string', 'enum': ['reasonable', 'not_yet_reasonable', 'unclear']}, 'comparison': {'type': 'string'}}, 'required': ['relation', 'merit', 'comparison']}
        name = 'cocreation_challenge_reason_classification'
    elif task == 'revision_requirements':
        schema = requirement_response_schema()
        name = 'cocreation_revision_requirements'
    elif task == 'revision_requirement_review':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'accepted': {'type': 'boolean'}, 'issues': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'requirementIds': {'type': 'array', 'items': {'type': 'string'}}, 'kind': {'type': 'string', 'enum': ['evidence', 'meaning', 'coverage', 'conflict']}, 'evidenceSpans': {'type': 'array', 'items': {'type': 'string'}}, 'repairInstruction': {'type': 'string'}}, 'required': ['requirementIds', 'kind', 'evidenceSpans', 'repairInstruction']}}}, 'required': ['accepted', 'issues']}
        name = 'cocreation_revision_requirement_review'
    elif task in {'human_edit_engagement_repair', 'human_edit_engagement_recovery'}:
        schema = engagement_schema()
        name = 'cocreation_human_edit_engagement_repair'
    elif task in {'turn_understanding', 'turn_understanding_review'}:
        schema = turn_schema()
        name = 'cocreation_turn_understanding'
        if task == 'turn_understanding_review':
            schema = review_schema()
            name = 'cocreation_turn_understanding_review'
    elif task == 'intent_candidate_review':
        claim_schema = {'type': 'object', 'additionalProperties': False, 'properties': {'normalizedMeaning': {'type': 'string'}, 'subjectType': {'type': 'string'}, 'subjectText': {'type': 'string'}, 'attributeType': {'type': 'string'}, 'attributeText': {'type': 'string'}, 'direction': {'type': 'string', 'enum': ['increase', 'decrease', 'maintain', 'avoid', 'evaluate', 'unspecified']}, 'degree': {'type': 'string'}, 'aspect': {'type': 'string'}, 'scopeType': {'type': 'string'}, 'scopeText': {'type': 'string'}, 'evidenceSpan': {'type': 'string'}}, 'required': ['normalizedMeaning', 'subjectType', 'subjectText', 'attributeType', 'attributeText', 'direction', 'degree', 'aspect', 'scopeType', 'scopeText', 'evidenceSpan']}
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'classification': {'type': 'string', 'enum': ['none', 'candidate', 'reference_only']}, 'claims': {'type': 'array', 'maxItems': 4, 'items': claim_schema}, 'cardText': {'type': 'string'}, 'answeredQuestionIds': {'type': 'array', 'maxItems': 24, 'items': {'type': 'string'}}, 'cardTextValid': {'type': 'boolean'}, 'bodyValid': {'type': 'boolean'}, 'relation': {'type': 'string', 'enum': ['none', 'compatible', 'conflict', 'unclear']}, 'conflictingHypothesisIds': {'type': 'array', 'items': {'type': 'string'}}, 'explanation': {'type': ['string', 'null']}, 'issues': {'type': 'array', 'items': {'type': 'string'}}, 'reviewedActiveClaims': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'hypothesisId': {'type': 'string'}, 'claims': {'type': 'array', 'maxItems': 4, 'items': claim_schema}}, 'required': ['hypothesisId', 'claims']}}}, 'required': ['classification', 'claims', 'cardText', 'cardTextValid', 'bodyValid', 'relation', 'conflictingHypothesisIds', 'explanation', 'issues', 'reviewedActiveClaims', 'answeredQuestionIds']}
        name = 'cocreation_intent_candidate_review'
    elif task == 'intent_card_repair':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'cardText': {'type': 'string'}}, 'required': ['cardText']}
        name = 'cocreation_intent_card_repair'
    elif task in {'intent_component_repair', 'intent_body_repair'}:
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'bodyParagraphs': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {'type': 'string'}}, 'cardSentences': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': {'type': 'string'}}}, 'required': ['bodyParagraphs', 'cardSentences']}
        if task == 'intent_body_repair':
            schema['properties'].pop('cardSentences')
            schema['required'] = ['bodyParagraphs']
        name = 'cocreation_intent_component_repair'
    elif task == 'intent_feedback_review':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'verdict': {'type': 'string', 'enum': ['compatible', 'conflict', 'unclear']}, 'explanation': {'type': ['string', 'null']}, 'supersedesHypothesisIds': {'type': 'array', 'items': {'type': 'string'}}, 'conflictingHypothesisIds': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['verdict', 'explanation', 'supersedesHypothesisIds', 'conflictingHypothesisIds']}
        name = 'cocreation_intent_feedback_review'
    elif task == 'question_answer_review':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'results': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'questionId': {'type': 'string'}, 'status': {'type': 'string', 'enum': ['answered', 'unanswered']}}, 'required': ['questionId', 'status']}}}, 'required': ['results']}
        name = 'cocreation_question_answer_review'
    elif task == 'intent_progress_rewrite':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'detailedText': {'type': ['string', 'null']}, 'summaryText': {'type': 'string'}}, 'required': ['detailedText', 'summaryText']}
        name = 'cocreation_intent_progress_rewrite'
    elif task == 'proposal_clarification':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'body': {'type': 'string'}, 'question': {'type': 'string'}}, 'required': ['body', 'question']}
        name = 'cocreation_proposal_clarification'
    elif task == 'translation':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'translations': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['translations']}
        name = 'cocreation_translation'
    elif task == 'revision_plan':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'strategies': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['strategies']}
        name = 'cocreation_revision_plan'
    elif task == 'operation_candidates':
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'candidates': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['candidates']}
        name = 'cocreation_operation_candidates'
    else:
        stage_opening = task == 'stage_assessment'
        guidance_properties = {'move': {'type': 'string', 'enum': ['observe_stage'] if stage_opening else sorted(GUIDANCE_MOVES)}, 'intentHypothesis': {'type': ['string', 'null']}, 'intentConfidence': {'type': ['string', 'null'], 'enum': ['low', 'medium', 'high', None]}, 'followUpQuestion': {'type': ['string', 'null']}, 'proposalOffer': {'type': ['object', 'null']}, 'disagreement': {'type': ['object', 'null']}, 'uiCues': {'type': 'array', 'items': {'type': 'object'}}, 'coordinateLinks': {'type': 'array', 'items': {'type': 'object'}}, 'designContextPatch': {'type': ['object', 'null']}}
        guidance_schema = {'type': 'object', 'additionalProperties': False, 'properties': guidance_properties, 'required': ['move', 'intentHypothesis', 'intentConfidence', 'followUpQuestion', 'proposalOffer', 'disagreement', 'uiCues', 'coordinateLinks', 'designContextPatch']}
        assessment_schema = {'type': ['object', 'null'], 'properties': {'solutionSummary': {'type': 'string'}, 'difficultyOpinion': {'type': 'string'}, 'features': {'type': 'array', 'items': {'type': 'string'}}, 'suggestions': {'type': 'array', 'items': {'type': 'string'}}, 'satisfactionQuestion': {'type': ['string', 'null']}}, 'required': ['solutionSummary', 'difficultyOpinion', 'features', 'suggestions', 'satisfactionQuestion'], 'additionalProperties': False}
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {'assistantMessage': {'type': 'string'}, 'contentBlocks': {'type': ['array', 'null'], 'items': {'type': 'object'}}, 'guidance': guidance_schema, 'assessment': assessment_schema, 'proposedRows': {'type': ['array', 'null'], 'items': {'type': 'string'}}, 'modificationSummary': {'type': 'string'}, 'designContextPatch': {'type': ['object', 'null']}}, 'required': ['assistantMessage', 'contentBlocks', 'guidance', 'assessment', 'proposedRows', 'modificationSummary', 'designContextPatch']}
        if stage_opening:
            schema['properties']['assessment'] = {'type': 'object', 'additionalProperties': False, 'properties': {'solutionSummary': {'type': 'string'}, 'difficultyOpinion': {'type': 'string'}, 'features': {'type': 'array', 'items': {'type': 'string'}}, 'suggestions': {'type': 'array', 'items': {'type': 'string'}}, 'satisfactionQuestion': {'type': ['string', 'null']}}, 'required': ['solutionSummary', 'difficultyOpinion', 'features', 'suggestions', 'satisfactionQuestion']}
        name = 'cocreation_stage_assessment' if task == 'stage_assessment' else 'cocreation_chat'
    return {'type': 'json_schema', 'json_schema': {'name': name, 'strict': True, 'schema': schema}}

@dataclass(frozen=True)
class LLMConfig:
    """Immutable 8010 configuration for the Kimi provider."""

    api_key: str = ''
    base_url: str = KIMI_BASE_URL
    model: str = KIMI_MODEL
    chat_compact_experiment: bool = False
    proposal_clarification_compact_experiment: bool = False
    llm_internal_deadline_seconds: float = LLM_INTERNAL_DEADLINE_SECONDS
    backend_request_timeout_seconds: float = BACKEND_REQUEST_TIMEOUT_SECONDS
    primary_attempt_timeout_seconds: float = PRIMARY_ATTEMPT_TIMEOUT_SECONDS
    min_retry_budget_seconds: float = MIN_RETRY_BUDGET_SECONDS
    plain_chat_timeout_seconds: float = PLAIN_CHAT_TIMEOUT_SECONDS
    plain_primary_timeout_seconds: float = PLAIN_PRIMARY_TIMEOUT_SECONDS
    ordinary_chat_attempt_timeouts: tuple[float, ...] = ORDINARY_CHAT_ATTEMPT_TIMEOUTS
    proposal_request_timeout_seconds: float = PROPOSAL_REQUEST_TIMEOUT_SECONDS
    proposal_internal_deadline_seconds: float = PROPOSAL_INTERNAL_DEADLINE_SECONDS
    proposal_plan_primary_timeout_seconds: float = PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS
    proposal_plan_retry_timeout_seconds: float = PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS
    proposal_llm_phase_timeout_seconds: float = PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS
    proposal_search_deadline_seconds: float = PROPOSAL_SEARCH_DEADLINE_SECONDS
    proposal_operation_attempt_timeout_seconds: float = PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS
    ordinary_chat_max_attempts: int = ORDINARY_CHAT_MAX_ATTEMPTS
    chat_max_attempts: int = CHAT_MAX_ATTEMPTS
    proposal_generation_attempts: int = PROPOSAL_GENERATION_ATTEMPTS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        source = os.environ if env is None else env
        provider = str(source.get('COCREATION_LLM_PROVIDER', '')).strip().lower()
        if provider not in {'', 'kimi', 'moonshot'}:
            api_key = ''
        else:
            api_key = (
                str(source.get('COCREATION_LLM_API_KEY', '')).strip()
                or str(source.get('KIMI_API_KEY', '')).strip()
                or str(source.get('MOONSHOT_API_KEY', '')).strip()
            )
        base_url = (
            str(source.get('COCREATION_LLM_BASE_URL', '')).strip()
            or str(source.get('KIMI_BASE_URL', '')).strip()
            or str(source.get('MOONSHOT_BASE_URL', '')).strip()
            or KIMI_BASE_URL
        )
        model = (
            str(source.get('COCREATION_LLM_MODEL', '')).strip()
            or str(source.get('KIMI_MODEL', '')).strip()
            or str(source.get('MOONSHOT_MODEL', '')).strip()
            or KIMI_MODEL
        )
        chat_compact = str(source.get(CHAT_COMPACT_EXPERIMENT_ENV, '')).strip().casefold() in {'1', 'true', 'yes', 'on'}
        clarification_compact = str(source.get(PROPOSAL_CLARIFICATION_COMPACT_EXPERIMENT_ENV, '')).strip().casefold() in {'1', 'true', 'yes', 'on'}
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            chat_compact_experiment=chat_compact,
            proposal_clarification_compact_experiment=clarification_compact,
        )


_ACTIVE_LLM_SERVICE: ContextVar[Any] = ContextVar('cocreation_active_llm_service', default=None)
_FALLBACK_INERT_SERVICE: Any = None


def _current_service(service: Any = None):
    global _FALLBACK_INERT_SERVICE
    if service is not None:
        return service
    active = _ACTIVE_LLM_SERVICE.get()
    if active is not None:
        return active
    if _FALLBACK_INERT_SERVICE is None:
        from .service import CoCreationLLMService
        _FALLBACK_INERT_SERVICE = CoCreationLLMService(LLMConfig())
    return _FALLBACK_INERT_SERVICE


def _active_config(service: Any = None) -> LLMConfig:
    return _current_service(service).config


def _active_clock(service: Any = None) -> Clock:
    return _current_service(service).clock


def _monotonic(service: Any = None) -> float:
    return _active_clock(service).monotonic()


def _solve_stage(rows, *args, _service: Any = None, **kwargs):
    return _current_service(_service).solve_stage(rows, *args, **kwargs)


def _unified_model_attempts(count, _service: Any = None):
    """Return bounded attempts for the one production model used by 8010."""
    return [_active_config(_service).model] * max(1, int(count))


def _llm_credentials(_service: Any = None):
    """Resolve only the 8010 Kimi credentials from the injected LLMConfig."""
    return _current_service(_service).credentials()


def _request_deadline(started_at=None, budget_seconds=None, _service: Any = None):
    svc = _current_service(_service)
    base = svc.clock.monotonic() if started_at is None else started_at
    budget = svc.config.llm_internal_deadline_seconds if budget_seconds is None else float(budget_seconds)
    return base + budget


def _chat_compact_experiment_enabled(_service: Any = None):
    """Read the opt-in presentation experiment from the injected LLMConfig."""
    return bool(_active_config(_service).chat_compact_experiment)


def _proposal_clarification_compact_experiment_enabled(_service: Any = None):
    return bool(_active_config(_service).proposal_clarification_compact_experiment)


def _remaining_until(deadline, _service: Any = None):
    return _current_service(_service).remaining_until(deadline)


def _retry_budget_available(deadline, *, request_id, task, attempt, max_attempts, response_mode, fallback_reason=None, minimum_seconds=None, _service: Any = None):
    """Prevent a retry that cannot realistically finish before the public deadline."""
    svc = _current_service(_service)
    min_budget = svc.config.min_retry_budget_seconds if minimum_seconds is None else minimum_seconds
    remaining = svc.remaining_until(deadline)
    if remaining >= min_budget:
        return True
    svc.log_event('llm_retry_skipped', requestId=request_id, task=task, attempt=attempt, maxAttempts=max_attempts, responseMode=response_mode, remainingSeconds=round(remaining, 3), retrySkippedReason='insufficient_remaining_budget', fallbackReason=fallback_reason)
    return False

@dataclass(frozen=True)
class LLMExecutionResult:
    assistant_message: str
    attempts_used: int
    request_id: str
    assessment: dict = field(default_factory=dict)
    proposed_rows: list[str] | None = None
    modification_summary: str = ''
    model: str = ''
    latency_ms: int = 0
    guidance: dict = field(default_factory=dict)
    revision_plan: dict = field(default_factory=dict)
    revision_contract: dict = field(default_factory=dict)
    revision_operations: list[dict] = field(default_factory=list)
    proposal_diagnostics: dict = field(default_factory=dict)
    proposal_binding: dict = field(default_factory=dict)
    secondary_execution: object | None = None

@dataclass(frozen=True)
class TranslationExecutionResult:
    translations: list[dict]
    attempts_used: int
    request_id: str
    model: str = ''
    latency_ms: int = 0

class LLMServiceError(Exception):

    def __init__(self, code, message, request_id, retryable, attempts_used, status_code, provider_status=None, provider_error_type=None, provider_error_code=None, provider_param=None, provider_message=None):
        super().__init__(message)
        self.code = str(code)
        self.safe_message = str(message)
        self.request_id = str(request_id)
        self.retryable = bool(retryable)
        self.attempts_used = int(attempts_used)
        self.status_code = int(status_code)
        self.provider_status = int(provider_status) if provider_status not in (None, '') else None
        self.provider_error_type = str(provider_error_type or '')
        self.provider_error_code = str(provider_error_code or '')
        self.provider_param = str(provider_param or '')
        self.provider_message = str(provider_message or '')

class EmptyModelResponse(ValueError):
    pass

class LowQualityModelResponse(ValueError):
    pass


def _require_first_choice(response):
    """Validate that a completion payload includes at least one choice."""
    choices = getattr(response, 'choices', None)
    if not choices:
        raise EmptyModelResponse('The model returned an empty response.')
    return choices[0]

def _visible_reply_style(mode='conversation'):
    """Expression guidance for visible prose only, never semantic/execution tasks."""
    common = "Visible expression: speak as a warm, equal design peer with an independent view. Answer the latest contribution first, then give only the reasoning it needs. State each judgment once; do not paraphrase it later or append a default summary. Use everyday words and natural first-person observations when useful, without requiring a map analysis, route explanation, personal reflection and follow-up in every reply. A simple difficulty question needs a judgment and its strongest reason; a complex question may need depth. Write plain text for the existing interface, without Markdown emphasis markers or headings. When it helps explain this exchange, light metaphor, gentle personification or self-directed wit is welcome. Humor is optional: no joke quota, stock catchphrase, forced punchline or theatrical persona. Never mock the designer's ability, trivialize a disagreement, or invent player reactions or personal play experience. Keep verified facts, subjective predictions, tentative intentions, suggestions and server-confirmed actions distinct. Figurative wording must not add unsupported positions, routes, reachability, mandatory paths or success claims; humor never exempts a claim from evidence checks. Be clear about uncertainty without adding a disclaimer to every sentence. Use the length, paragraphing and order the actual question needs; a short complete answer is welcome. Preserve useful design reasoning, relevant verified route explanation and your own grounded perspective when relevant, without padding, a minimum length or a fixed rhetorical template. Apply this voice only to visible prose, never machine-only fields, evidence quotes or semantic decisions. Keep cards, warnings and action/status explanations precise and plain. During disagreement, stay warm and restrained; do not joke about the user's position. "
    context = {'conversation': 'Ordinary discussion and route explanation may use light humor when it clarifies the point. ', 'opening': 'For a Stage opening, keep any humor subtle and secondary to grounded observation. ', 'clarification': 'In proposal clarification, only the body may be lightly playful. Keep every option, trade-off and the question neutral and equally respectful; do not make one direction more attractive through jokes or loaded wording. The body introduces the unresolved decision; options carry the treatments, effects and trade-offs without repeating the body. Keep option fields complementary and preserve necessary distinctions and costs. Later rounds address only what remains unresolved, without recapping settled goals or locked options. Three questions are a ceiling, not a required sequence; never pad the allowance. An explanation request is not a choice: explain the referenced option without adding a new preference question. Do not infer consent or reopen the question budget. ', 'review': "For human-edit observations, comparisons and disagreements, use restrained warmth. Keep evidence, alternatives and the designer's position clear; do not use humor to persuade. Keep the opening about the current map and the review about the verified change and design direction; do not repeat the same analysis in both messages. State the concrete disagreement, its reason and next choice without replaying the whole discussion. ", 'repair': 'Repair only the specified factual or semantic defect. Preserve reliable views and natural voice, including harmless existing wit; remove unsupported humor with its claim. Do not introduce a joke merely to satisfy the style guidance. '}
    return common + context[mode]

def _dialogue_background_prompt(stage_context):
    """Discuss prior wording without replaying historical map claims as facts."""
    evidence = (stage_context or {}).get('dialogueEvidence') or {}
    records = []
    for turn in evidence.get('turns', []):
        text = str(turn.get('content') or '')
        if turn.get('role') == 'assistant':
            text = text.split('<GUIDANCE>', 1)[0]
            text = _strip_unreferenced_current_map_claims(text)
            text = ' '.join((sentence for sentence in _content_block_sentences(text) if not re.search('[（(]\\s*\\d+\\s*[,，]\\s*\\d+\\s*[)）]|第\\s*\\d+\\s*[行列]|\\b(?:push|move|route|path)\\b|路线|路径|推到|位于|相邻|\\b(?:water|wall|floor|player|box|crate|target)\\s+(?:is|are|occupies|sits)\\b|(?:水域|墙|箱子|目标|玩家|地板).{0,8}(?:在|占据).{0,10}(?:左|右|上|下|旁|角|区域)', sentence, re.I)))
        else:
            claims = analyze_user_map_claims(text, (stage_context or {}).get('stageSnapshot'))
            for conflict in claims.get('conflicts') or []:
                text = text.replace(str(conflict.get('sourceText') or ''), '')
        if text.strip():
            records.append({'sourceTurnId': turn['id'], 'role': turn['role'], 'wording': text})
    if not records:
        return ''
    return '\n\nDiscussion background (linguistic context only, NOT current map facts or new authorization):\n' + json.dumps(records, ensure_ascii=False) + '\nUse this to resolve references and continue the conversation. Assistant suggestions are not designer goals. Historical user wording may explain a goal; new permission/state decisions require the latest user evidence. Current map facts come only from Current Stage Snapshot.'

def _latest_user_text(conversation):
    return next((str(message.get('content') or '').strip() for message in reversed(conversation or []) if message.get('role') == 'user' and str(message.get('content') or '').strip()), '')

def _safe_validation_reason(exception):
    issues = safe_issues(exception)
    if issues:
        return '; '.join((f"{item['fieldPath']}:{item['reasonCode']}" for item in issues[:4]))
    if isinstance(exception, json.JSONDecodeError):
        return 'The response was not a complete valid JSON object.'
    if isinstance(exception, SemanticConstraintError):
        failed = [f"{item.get('kind')}:{item.get('reason')}" for item in exception.results if item.get('passed') is False]
        return 'Semantic contract failed (' + ', '.join(failed[:4]) + ').'
    if isinstance(exception, (ValueError, TypeError, KeyError)):
        reason = ' '.join(str(exception).split())[:300]
        return reason or 'The response did not match the required JSON contract.'
    return None

async def _request_completion(*args, **kwargs):
    return await _current_service().request_completion(*args, **kwargs)


async def _default_request_completion(*args, **kwargs):
    task = kwargs.get('task') or 'unspecified'
    stage = 'body_generation' if task == 'plain_chat' else 'model_' + task
    trace_reply(stage, 'started', modelCalls=1)
    proposal_tasks = {'turn_understanding', 'turn_understanding_review', 'proposal_clarification', 'proposal_clarification_review', 'revision_requirements', 'revision_requirement_review', 'revision_plan', 'map_operations', 'map_proposal', 'challenge_reason', 'challenge_reason_classification', 'proposal_failure_analysis'}
    proposal_task = task.removesuffix('_patch') in proposal_tasks
    if proposal_task:
        trace_proposal('model_generation', task, 'started')
    try:
        result = await _request_completion_impl(*args, **kwargs)
        trace_reply(stage, 'passed')
        if proposal_task:
            trace_proposal('model_generation', task, 'passed')
        return result
    except BaseException as error:
        trace_reply(stage, 'failed', failure_reason(error))
        if proposal_task:
            trace_proposal('model_generation', task, 'failed', reason_code(error))
        raise


async def _request_completion_impl(api_key, base_url, model, messages, max_completion_tokens, timeout_seconds, structured=True, task=None, manual_edit_direction_count=None, response_format_override=None, temperature=0.6):
    svc = _current_service()
    client = svc.create_async_client(api_key, base_url, timeout_seconds)
    request_options = {'model': model, 'messages': messages, 'temperature': temperature, 'max_completion_tokens': max_completion_tokens, 'stream': False, 'extra_body': {'thinking': {'type': 'disabled'}}}
    if response_format_override is not None:
        request_options['response_format'] = response_format_override
    elif structured:
        request_options['response_format'] = _structured_response_format(task, manual_edit_direction_count=manual_edit_direction_count)

    async def create_completion():
        trace_reply('provider_call', 'started', modelCalls=1)
        try:
            result = await client.chat.completions.create(**request_options)
            trace_reply('provider_call', 'passed')
            return result
        except BaseException as error:
            trace_reply('provider_call', 'failed', failure_reason(error))
            raise
    try:
        try:
            return await create_completion()
        except APIStatusError as exception:
            status_code = getattr(exception, 'status_code', None)
            if structured and str(model).lower() == svc.config.model.lower() and (status_code in {400, 404, 422}):
                request_options['response_format'] = {'type': 'json_object'}
                return await create_completion()
            raise
    finally:
        await client.close()

def _content_block_sentences(value):
    return [sentence.strip() for sentence in re.split('(?<=[!?\\u3002\\uff01\\uff1f])\\s*|(?<=\\.)(?!\\d)\\s*|[\\r\\n]+', str(value or '')) if sentence.strip()]

def _strip_unreferenced_current_map_claims(value):
    """Keep legacy analysis, but never display its free-form current map facts.

    The v2 content-block path renders those facts from StageSnapshot.  This
    conservative compatibility filter covers direct entity coordinates and
    present-tense spatial relations while preserving future/hypothetical route
    discussion and ordinary design interpretation.
    """
    current_fact = re.compile('(?:(?<![A-Za-z0-9_])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9_]).{0,36}?(?:\\b(?:is|are|at|in|on|located|occupies|sits)\\b|\\u4f4d\\u4e8e|\\u5728\\u7b2c|\\u5750\\u6807).{0,40}?[\\(\\uff08]\\s*\\d{1,2}\\s*[,\\uff0c]\\s*\\d{1,2}\\s*[\\)\\uff09]|[\\(\\uff08]\\s*\\d{1,2}\\s*[,\\uff0c]\\s*\\d{1,2}\\s*[\\)\\uff09].{0,28}?(?:\\b(?:is|are)\\b|\\u662f).{0,12}?(?<![A-Za-z0-9_])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9_]))', flags=re.IGNORECASE)
    relation = re.compile('(?<![A-Za-z0-9_])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9_]).{0,42}?\\b(?:near|close\\s+to|adjacent\\s+to|beside|next\\s+to)\\b.{0,42}?(?<![A-Za-z0-9_])(?:P|B\\d+|T\\d+)(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?:B\\d+|T\\d+|P)(?![A-Za-z0-9_]).{0,24}?(?:\\u9760\\u8fd1|\\u76f8\\u90bb|\\u65c1\\u8fb9|\\u7d27\\u6328).{0,24}?(?<![A-Za-z0-9_])(?:B\\d+|T\\d+|P)(?![A-Za-z0-9_])', flags=re.IGNORECASE)
    future = re.compile('(?:\\b(?:if|when|would|will|move|push|from|toward|through|via)\\b|\\u5982\\u679c|\\u82e5|\\u5c06|\\u4f1a|\\u79fb\\u52a8|\\u63a8\\u5230|\\u4ece.*(?:\\u5230|\\u5411))', flags=re.IGNORECASE)
    kept = []
    for sentence in _content_block_sentences(value):
        if not future.search(sentence) and (current_fact.search(sentence) or relation.search(sentence)):
            continue
        kept.append(sentence)
    separator = '' if re.search('[\\u3400-\\u9fff]', str(value or '')) else ' '
    return separator.join(kept).strip()

def _sanitize_provider_message(value):
    """Return a short provider diagnostic without reflecting request secrets."""
    text = ' '.join(str(value or '').split())
    if not text:
        return ''
    text = re.sub('(?i)(authorization\\s*:\\s*bearer\\s+|api[_ -]?key\\s*[:=]\\s*)\\S+', '\\1[redacted]', text)
    text = re.sub('(?i)\\b(?:sk|key)-[A-Za-z0-9_-]{12,}\\b', '[redacted]', text)
    text = re.sub('https?://\\S+', '[url redacted]', text)
    return text[:_PROVIDER_DETAIL_LIMIT]

def _provider_error_details(exception):
    """Extract only the provider error allowlist from an APIStatusError."""
    if not isinstance(exception, APIStatusError):
        return {}
    body = getattr(exception, 'body', None)
    if not isinstance(body, dict):
        return {}
    error = body.get('error') if isinstance(body.get('error'), dict) else body
    if not isinstance(error, dict):
        return {}
    details = {'providerStatus': int(getattr(exception, 'status_code', 0) or 0) or None, 'providerErrorType': _sanitize_provider_message(error.get('type')), 'providerErrorCode': _sanitize_provider_message(error.get('code')), 'providerParam': _sanitize_provider_message(error.get('param')), 'providerMessage': _sanitize_provider_message(error.get('message'))}
    return {key: value for key, value in details.items() if value not in (None, '')}

def _provider_error_fields(error):
    """Map an LLMServiceError's safe provider details into audit fields."""
    return {'providerStatus': getattr(error, 'provider_status', None), 'providerErrorType': getattr(error, 'provider_error_type', '') or None, 'providerErrorCode': getattr(error, 'provider_error_code', '') or None, 'providerParam': getattr(error, 'provider_param', '') or None, 'providerMessage': getattr(error, 'provider_message', '') or None}

def _visible_internal_identifier(token):
    return token not in _VISIBLE_CHINESE_LATIN_ALLOWLIST and bool('_' in token or re.search('[a-z][A-Z]|[A-Z][a-z]+[A-Z]', token))

def _is_length_failure(exception, validation_feedback=None):
    text = ' '.join((str(value or '') for value in (exception, validation_feedback))).casefold()
    return 'token limit' in text or 'too long' in text or 'cut off' in text

@proposal_phase('component_repair', 'patch')
async def _request_component_patch(api_key, base_url, patch_request, messages, request_id, deadline, task, max_tokens=1600, timeout_limit=20.0):
    """Use one existing corrective slot, never open a new retry loop."""
    svc = _current_service()
    diagnostic = CURRENT_PROPOSAL_DIAGNOSTIC.get()
    if diagnostic is None or patch_request.component not in diagnostic.pending_repairs:
        trace_repair(patch_request.component, 'proposed')
    remaining = min(timeout_limit, svc.remaining_until(deadline))
    if remaining <= 0:
        raise asyncio.TimeoutError()
    patch_messages = [dict(message) for message in messages]
    patch_messages.append({'role': 'system', 'content': 'This corrective attempt returns ONLY the component patch schema; it supersedes earlier full-envelope output instructions. All original evidence, semantic scope, permissions and map rules still apply. Frozen candidate text is not new designer evidence. Preserve locked content exactly. Return exactly one patches item for EVERY targets entry, including targets whose repaired value is an empty list. field must be the exact target name, valueJson a JSON-encoded value of its targetTypes, and evidenceRefs a nonempty list of keys from evidence. A patch is only a candidate repair, never proof, consent or a committed action.'})
    patch_messages.append({'role': 'user', 'content': json.dumps(patch_request.prompt(), ensure_ascii=False)})
    response = await asyncio.wait_for(svc.request_completion(api_key, base_url, svc.config.model, patch_messages, max_tokens, remaining, task=task + '_patch'), timeout=remaining)
    if not getattr(response, 'choices', None):
        raise PatchError('PATCH_EMPTY_RESPONSE')
    choice = response.choices[0]
    if str(getattr(choice, 'finish_reason', '')) == 'length':
        raise PatchError('PATCH_TRUNCATED')
    merged = patch_request.merge(parse_json_object(choice.message.content))
    trace_repair(patch_request.component, 'merged')
    svc.log_event('llm_component_patched', requestId=request_id, component=patch_request.component, fields=list(patch_request.targets), patchVersion=PATCH_VERSION, invalidatedComponents=dependent_components(patch_request.component))
    return merged

def _is_map_grounding_failure(exception, validation_feedback=None):
    text = ' '.join((str(value or '') for value in (exception, validation_feedback))).casefold()
    markers = ('conflicts with deterministic map facts', 'executionbrief is invalid for the saved stage', 'executionbrief coordinate conflict', 'revisionplan is invalid for the saved stage', 'outside the saved stage', 'deterministic map facts say', 'map-fact cleanup', 'does not match the saved map', 'places b', 'places t', 'places p', 'saved map does not support', 'is beside water', 'historical stage map claim', 'current box is close to a target', 'current player is close to a target', 'current box touches water')
    return any((marker in text for marker in markers))

def _user_states_first_person_view(message):
    text = str(message or '').strip()
    lowered = text.casefold()
    if not text:
        return False
    return any((marker in text for marker in ('我认为', '我觉得', '我感觉', '在我看来', '我不认同', '我不同意'))) or bool(re.search('\\b(?:i think|i feel|i believe|in my view|from my perspective|i disagree)\\b', lowered))

def _latest_role_content(messages, role):
    return next((str(message.get('content') or '').strip() for message in reversed(messages) if message.get('role') == role), '')

def _latest_user_explicitly_agrees(message):
    text = str(message or '').strip().casefold()
    english = re.search("\\b(?:yes|okay|ok|sounds good|go ahead|do (?:it|that)|let'?s do (?:it|that))\\b", text)
    chinese = any((marker in text for marker in ('可以', '好', '行', '同意', '就这样', '做吧', '改吧', '试试', '做点')))
    return english is not None or chinese

def _latest_user_confirms_revision(message):
    """Recognize a short confirmation of the immediately preceding concrete plan."""
    text = str(message or '').strip().casefold()
    if not text:
        return False
    english = re.fullmatch("(?:yes|okay|ok|sounds good|go ahead|do (?:it|that)|let'?s do (?:it|that))[.!?]*", text)
    chinese = re.fullmatch('(?:好(?:的)?|可以|行|同意|就这样|按这个来|照这个来|按这个做|照这个做|(?:[一二三四五六七八九十\\d]+)格(?:可以|就行|行))\\s*[。！!？?]*', text)
    return english is not None or chinese is not None

def _latest_user_explicitly_rejects(message):
    text = str(message or '').strip().casefold()
    english = re.search("\\b(?:no|do not|don't|keep the original|without relaxing|not acceptable)\\b", text)
    chinese = any((marker in text for marker in ('不可以', '不同意', '不要降级', '不能降级', '保持原要求', '按原要求', '不接受放宽', '不要放宽')))
    return english is not None or chinese

def _first_declarative_sentence(message):
    for sentence in re.split('(?<=[.!?。！？])\\s*|[\\r\\n]+', str(message or '')):
        cleaned = sentence.strip()
        if cleaned and (not cleaned.endswith(('?', '？'))) and (re.sub('[\\s，,。.!！]', '', cleaned).casefold() not in {'当然', '可以', '好的', '好', '同意', '没问题', 'ok', 'okay', 'yes'}):
            return cleaned
    return ''

def _questionless_body(message):
    declarative = []
    for paragraph in (part.strip() for part in str(message or '').split('\n\n')):
        sentences = [sentence.strip() for sentence in re.split('(?<=[.!?。！？])\\s*', paragraph) if sentence.strip()]
        kept = [sentence for sentence in sentences if not sentence.endswith(('?', '？'))]
        if kept:
            separator = '' if re.search('[\\u3400-\\u9fff]', paragraph) else ' '
            declarative.append(separator.join(kept))
    return '\n\n'.join(declarative)

def _is_stage_one(stage_context):
    return int((stage_context or {}).get('stageNumber') or 0) == 1

def _is_human_edit_stage_opening(assessment_only, stage_context):
    return assessment_only and (not _is_stage_one(stage_context)) and ((stage_context or {}).get('source') == 'human_edit')

def classify_exception(exception, request_id, attempts_used):
    if isinstance(exception, LLMServiceError):
        return exception
    if isinstance(exception, EmptyModelResponse):
        return LLMServiceError('MODEL_EMPTY_RESPONSE', 'The LLM returned an empty response.', request_id, True, attempts_used, 502)
    if isinstance(exception, LowQualityModelResponse):
        return LLMServiceError('MODEL_LOW_QUALITY_RESPONSE', 'The LLM returned only a low-information question.', request_id, True, attempts_used, 502)
    if isinstance(exception, APITimeoutError):
        return LLMServiceError('UPSTREAM_TIMEOUT', 'Kimi did not respond before the timeout.', request_id, True, attempts_used, 504)
    if isinstance(exception, RateLimitError):
        return LLMServiceError('UPSTREAM_RATE_LIMIT', 'Kimi rate limited the request.', request_id, True, attempts_used, 503)
    if isinstance(exception, APIConnectionError):
        return LLMServiceError('UPSTREAM_CONNECTION_ERROR', 'The prototype could not connect to Kimi.', request_id, True, attempts_used, 502)
    if isinstance(exception, APIStatusError):
        upstream_status = int(getattr(exception, 'status_code', 0) or 0)
        retryable = upstream_status == 429 or upstream_status >= 500
        provider_details = _provider_error_details(exception)
        provider_message = provider_details.get('providerMessage', '')
        safe_message = f"Kimi returned HTTP {upstream_status or 'error'}: {provider_message}" if provider_message else f"Kimi returned HTTP {upstream_status or 'error'}."
        return LLMServiceError('UPSTREAM_SERVER_ERROR' if retryable else 'UPSTREAM_REQUEST_REJECTED', safe_message, request_id, retryable, attempts_used, 503 if upstream_status == 429 else 502, provider_status=provider_details.get('providerStatus', upstream_status or None), provider_error_type=provider_details.get('providerErrorType'), provider_error_code=provider_details.get('providerErrorCode'), provider_param=provider_details.get('providerParam'), provider_message=provider_message)
    if isinstance(exception, (json.JSONDecodeError, ValueError, TypeError, KeyError)):
        error = LLMServiceError('MODEL_RESPONSE_INVALID', 'The LLM returned an invalid response.', request_id, True, attempts_used, 502)
        issues = safe_issues(exception)
        diagnostic = (getattr(exception, 'details', {}) or {}).get('proposalDiagnostic')
        if issues or diagnostic:
            error.details = {'reasonCode': reason_code(exception), 'issues': issues}
            if diagnostic:
                error.details['proposalDiagnostic'] = diagnostic
                failure = diagnostic.get('finalFailure') or {}
                if failure.get('stage'):
                    error.details['failureStage'] = failure['stage']
                if failure.get('stage') == 'clarification':
                    error.details.update(task='proposal_clarification', failureStage='clarification', failureKind='schema_or_model_validation')
        return error
    return LLMServiceError('INTERNAL_ERROR', 'An unexpected LLM error occurred.', request_id, False, attempts_used, 500)

def _plain_fallback_allowed(error):
    return error.code in {'MODEL_EMPTY_RESPONSE', 'MODEL_RESPONSE_INVALID', 'MODEL_LOW_QUALITY_RESPONSE', 'UPSTREAM_TIMEOUT', 'UPSTREAM_CONNECTION_ERROR'}

def _response_diagnostics(response, choice):
    usage = getattr(response, 'usage', None)
    content = str(getattr(getattr(choice, 'message', None), 'content', '') or '')
    return {'finishReason': str(getattr(choice, 'finish_reason', '') or ''), 'responseChars': len(content), 'promptTokens': getattr(usage, 'prompt_tokens', None), 'completionTokens': getattr(usage, 'completion_tokens', None), 'totalTokens': getattr(usage, 'total_tokens', None)}

def _empty_response_diagnostics():
    return {'finishReason': None, 'responseChars': None, 'promptTokens': None, 'completionTokens': None, 'totalTokens': None}

def _clean_text(value, field_name, maximum=CHAT_RESPONSE_MAX_LENGTH):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field_name} must be a non-empty string.')
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f'{field_name} is too long.')
    return cleaned

def _clean_list(value, field_name):
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError(f'{field_name} must be a non-empty list.')
    return [_clean_text(item, field_name) for item in value]

def _clean_optional_text(value, field_name):
    if value is None:
        return None
    return _clean_text(value, field_name)

def _default_create_async_client(api_key, base_url, timeout_seconds):
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=0)


def _create_async_client(api_key, base_url, timeout_seconds):
    return _current_service().create_async_client(api_key, base_url, timeout_seconds)


def _default_log_llm_event(clock: Clock, event: str, **fields):
    payload = {'timestamp': clock.utc_now(), 'level': 'INFO', 'event': event, **fields}
    print(json.dumps(payload, ensure_ascii=True, separators=(',', ':')), flush=True)


def _log_llm_event(event, **fields):
    _current_service().log_event(event, **fields)
