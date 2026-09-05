import asyncio
from difflib import SequenceMatcher
import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from proposal_search import (
    Focus,
    MetricGoal,
    ProposalSearchExhausted,
    parse_revision_plan,
    search_revision_plan,
    validate_revision_plan_against_map,
)
from level_validation import (
    build_map_facts,
    build_stage_snapshot,
    minimum_pushes,
    validate_and_solve,
)
from design_context import validate_design_context_patch
from repository import map_fingerprint


KIMI_MODEL = "kimi-k2.6"
KIMI_BASE_URL = "https://api.moonshot.cn/v1"
# 8010 is intentionally a Kimi-only service.  The 8000 service has its own
# runtime and remains on DeepSeek; these compatibility names are retained for
# callers that import them, but they must never resolve 8010 credentials from
# the 8000 environment.
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
PROPOSAL_GENERATION_ATTEMPTS = 2
# Ordinary chat and Stage openings retain the 120-second public budget. A
# proposal is a multi-phase pipeline (RevisionPlan, operation candidates,
# deterministic validation), so it receives a separate, slightly longer
# request budget rather than making every chat wait longer.
PROPOSAL_REQUEST_TIMEOUT_SECONDS = 300.0
PROPOSAL_INTERNAL_DEADLINE_SECONDS = 296.0
# The semantic RevisionPlan gets a long first attempt and a compact corrective
# retry. The phase cap leaves the remainder for operation candidates and
# deterministic validation.
PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS = 140.0
PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS = 35.0
PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS = 180.0
PROPOSAL_SEARCH_DEADLINE_SECONDS = 56.0
# The authorized revision now has two bounded LLM phases: a semantic plan and
# concrete operation candidates.  Both use the existing proposal model config.
REVISION_CONTRACT_SCHEMA_VERSION = 1
REVISION_MIN_CHANGED_CELLS = 1
REVISION_MAX_CHANGED_CELLS = 12
# Compatibility for older diagnostics and integrations that imported this
# name. Operation candidates are a separate phase and must not inherit the
# longer semantic RevisionPlan timeout.
PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS = 30.0
PROPOSAL_ATTEMPT_TIMEOUT_SECONDS = PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS
CHAT_MAX_COMPLETION_TOKENS = 2600
# Compatibility alias for integrations that import the old constant name.
CHAT_MAX_TOKENS = CHAT_MAX_COMPLETION_TOKENS
PLAIN_CHAT_TIMEOUT_SECONDS = BACKEND_REQUEST_TIMEOUT_SECONDS
PLAIN_PRIMARY_TIMEOUT_SECONDS = 70.0
PLAIN_CHAT_MAX_COMPLETION_TOKENS = 2200
PLAIN_CHAT_MAX_TOKENS = PLAIN_CHAT_MAX_COMPLETION_TOKENS
PROPOSAL_MAX_COMPLETION_TOKENS = 2400
PROPOSAL_MAX_TOKENS = PROPOSAL_MAX_COMPLETION_TOKENS
# RevisionPlan uses direct JSON generation with thinking disabled. Keep enough
# room for the complete contract while preventing an unexpectedly verbose
# response from consuming the proposal phase.
PROPOSAL_PLAN_MAX_COMPLETION_TOKENS = 2400
PROPOSAL_PLAN_MAX_TOKENS = PROPOSAL_PLAN_MAX_COMPLETION_TOKENS
PROPOSAL_OPERATION_MAX_COMPLETION_TOKENS = 1400
PROPOSAL_OPERATION_MAX_TOKENS = PROPOSAL_OPERATION_MAX_COMPLETION_TOKENS
PROPOSAL_CANDIDATE_LIMIT = 3
PROPOSAL_OPERATION_LIMIT = 24
TRANSLATION_MAX_COMPLETION_TOKENS = 3200
TRANSLATION_MAX_TOKENS = TRANSLATION_MAX_COMPLETION_TOKENS
CHAT_RESPONSE_MAX_LENGTH = 8000
# This is a presentation budget, not a validity limit. Parsed replies above
# this size are compacted block-by-block so a verbose route trace never causes
# the whole designer-facing reply to disappear.
CHAT_RESPONSE_HARD_LENGTH = 24000
ROUTE_REASONING_PASSAGE_LIMIT = 2
PERSONAL_REFLECTION_SENTENCE_LIMIT = 2
COORDINATE_LINK_LIMIT = 12
CHAT_MAX_PARAGRAPHS = 6
CHAT_MAX_SENTENCES = 12
CHAT_PARAGRAPH_MAX_CHINESE_CHARS = 240
CHAT_PARAGRAPH_MAX_LATIN_WORDS = 160
PROMPT_VERSION = "cocreation-v48-objective-policy-candidates"


def _structured_response_format(task=None):
    """Build a compact schema contract that Kimi can follow reliably.

    The application still performs the authoritative validation after parsing.
    This schema exists to keep Kimi's JSON envelope stable and to avoid asking
    the model to infer the wire shape from the much larger design rules.
    """
    task = str(task or "chat")
    if task == "proposal_clarification":
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "body": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["body", "question"],
        }
        name = "cocreation_proposal_clarification"
    elif task == "translation":
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "translations": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["translations"],
        }
        name = "cocreation_translation"
    elif task == "revision_plan":
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schemaVersion": {"type": ["integer", "null"]},
                "strategies": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["strategies"],
        }
        name = "cocreation_revision_plan"
    elif task == "operation_candidates":
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidates": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["candidates"],
        }
        name = "cocreation_operation_candidates"
    else:
        stage_opening = task == "stage_assessment"
        guidance_properties = {
            "move": {
                "type": "string",
                "enum": ["observe_stage"] if stage_opening else sorted(GUIDANCE_MOVES),
            },
            "intentHypothesis": {"type": ["string", "null"]},
            "intentConfidence": {
                "type": ["string", "null"],
                "enum": ["low", "medium", "high", None],
            },
            "followUpQuestion": {"type": ["string", "null"]},
            "proposalOffer": {"type": ["object", "null"]},
            "disagreement": {"type": ["object", "null"]},
            "uiCues": {"type": "array", "items": {"type": "object"}},
            "coordinateLinks": {"type": "array", "items": {"type": "object"}},
            "designContextPatch": {"type": ["object", "null"]},
        }
        guidance_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": guidance_properties,
            "required": [
                "move",
                "intentHypothesis",
                "intentConfidence",
                "followUpQuestion",
                "proposalOffer",
                "disagreement",
                "uiCues",
                "coordinateLinks",
            ],
        }
        assessment_schema = {
            "type": ["object", "null"],
            "properties": {
                "solutionSummary": {"type": "string"},
                "difficultyOpinion": {"type": "string"},
                "features": {"type": "array", "items": {"type": "string"}},
                "suggestions": {"type": "array", "items": {"type": "string"}},
                "satisfactionQuestion": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "assistantMessage": {"type": "string"},
                # Optional v2 presentation blocks. Older cached responses can
                # still use assistantMessage and are converted server-side.
                "contentBlocks": {"type": ["array", "null"], "items": {"type": "object"}},
                "guidance": guidance_schema,
                "assessment": assessment_schema,
                "proposedRows": {"type": ["array", "null"], "items": {"type": "string"}},
                "modificationSummary": {"type": "string"},
                "designContextPatch": {"type": ["object", "null"]},
            },
            "required": [
                "assistantMessage",
                "guidance",
                "assessment",
                "proposedRows",
                "modificationSummary",
            ],
        }
        if stage_opening:
            schema["properties"]["assessment"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "solutionSummary": {"type": "string"},
                    "difficultyOpinion": {"type": "string"},
                    "features": {"type": "array", "items": {"type": "string"}},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                    "satisfactionQuestion": {"type": ["string", "null"]},
                },
                "required": [
                    "solutionSummary",
                    "difficultyOpinion",
                    "features",
                    "suggestions",
                    "satisfactionQuestion",
                ],
            }
        name = (
            "cocreation_stage_assessment"
            if task == "stage_assessment"
            else "cocreation_chat"
        )
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }

GUIDANCE_MOVES = {
    "observe_stage",
    "clarify_intent",
    "offer_perspective",
    "challenge_tradeoff",
    "reflect_on_play",
    "offer_revision",
    "deliver_revision",
}
INTENT_CONFIDENCE_LEVELS = {"low", "medium", "high"}
UI_CUE_TYPES = {"manual_edit", "warning", "tradeoff", "clarification"}
GUIDANCE_REQUEST_MODES = {
    "revision_advice", "discussion", "needs_clarification", "proposal_blocked", "none"
}
DISAGREEMENT_STATUSES = {"active", "resolved"}
DISAGREEMENT_SUBJECTS = {"ai_revision", "human_edit", "user_request"}
DISAGREEMENT_RESOLUTIONS = {"user", "ai", "compromise", "retain_current"}


def _unified_model_attempts(count):
    """Return bounded attempts for the one production model used by 8010."""
    # Do not allow a shared 8000 environment or an old model override to change
    # the provider for this service.  Explicit configuration is still read by
    # readiness/deployment checks, while every request remains Kimi K2.6.
    return [KIMI_MODEL] * max(1, int(count))


def _llm_credentials():
    """Resolve only the 8010 Kimi credentials."""
    provider = os.getenv("COCREATION_LLM_PROVIDER", "").strip().lower()
    generic_key = os.getenv("COCREATION_LLM_API_KEY", "").strip()
    kimi_key = os.getenv("KIMI_API_KEY", "").strip()
    if provider not in {"", "kimi"}:
        return "", KIMI_BASE_URL

    api_key = generic_key or kimi_key
    base_url = (
        os.getenv("COCREATION_LLM_BASE_URL", "").strip()
        or os.getenv("KIMI_BASE_URL", "").strip()
        or KIMI_BASE_URL
    )
    return api_key, base_url


def _request_deadline(started_at=None, budget_seconds=None):
    base = time.monotonic() if started_at is None else started_at
    budget = (
        LLM_INTERNAL_DEADLINE_SECONDS
        if budget_seconds is None
        else float(budget_seconds)
    )
    return base + budget


def _remaining_until(deadline):
    return max(0.0, float(deadline) - time.monotonic())


def _retry_budget_available(
    deadline,
    *,
    request_id,
    task,
    attempt,
    max_attempts,
    response_mode,
    fallback_reason=None,
    minimum_seconds=MIN_RETRY_BUDGET_SECONDS,
):
    """Prevent a retry that cannot realistically finish before the public deadline."""
    remaining = _remaining_until(deadline)
    if remaining >= minimum_seconds:
        return True

    _log_llm_event(
        "llm_retry_skipped",
        requestId=request_id,
        task=task,
        attempt=attempt,
        maxAttempts=max_attempts,
        responseMode=response_mode,
        remainingSeconds=round(remaining, 3),
        retrySkippedReason="insufficient_remaining_budget",
        fallbackReason=fallback_reason,
    )
    return False


@dataclass(frozen=True)
class LLMExecutionResult:
    assistant_message: str
    attempts_used: int
    request_id: str
    assessment: dict = field(default_factory=dict)
    proposed_rows: list[str] | None = None
    modification_summary: str = ""
    model: str = ""
    latency_ms: int = 0
    guidance: dict = field(default_factory=dict)
    revision_plan: dict = field(default_factory=dict)
    revision_contract: dict = field(default_factory=dict)
    revision_operations: list[dict] = field(default_factory=list)
    proposal_diagnostics: dict = field(default_factory=dict)
    proposal_binding: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TranslationExecutionResult:
    translations: list[dict]
    attempts_used: int
    request_id: str
    model: str = ""
    latency_ms: int = 0


class LLMServiceError(Exception):
    def __init__(
        self,
        code,
        message,
        request_id,
        retryable,
        attempts_used,
        status_code,
        provider_status=None,
        provider_error_type=None,
        provider_error_code=None,
        provider_param=None,
        provider_message=None,
    ):
        super().__init__(message)
        self.code = str(code)
        self.safe_message = str(message)
        self.request_id = str(request_id)
        self.retryable = bool(retryable)
        self.attempts_used = int(attempts_used)
        self.status_code = int(status_code)
        self.provider_status = (
            int(provider_status) if provider_status not in (None, "") else None
        )
        self.provider_error_type = str(provider_error_type or "")
        self.provider_error_code = str(provider_error_code or "")
        self.provider_param = str(provider_param or "")
        self.provider_message = str(provider_message or "")


class EmptyModelResponse(ValueError):
    pass


class LowQualityModelResponse(ValueError):
    pass


def build_chat_messages(
    conversation,
    rows,
    language="en",
    solver_metrics=None,
    play_summary=None,
    assessment_only=False,
    stage_context=None,
):
    # Kept as local compatibility variables for the compact prompt signature;
    # the snapshot helper below is the only map payload sent to the model.
    serialized_map = ""
    numbered_map = ""
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    solver_metrics = _llm_solver_evidence(solver_metrics or {})
    play_summary = play_summary or {}
    stage_context = stage_context or {}
    # Send one current Stage representation.  Multiple independently rendered
    # map blocks were allowing the model to select a stale or differently
    # numbered copy of the same Stage.
    map_facts = _stage_snapshot_for_prompt(rows, stage_context)
    prompt_stage_context = _prompt_stage_context(rows, stage_context)
    design_context_prompt = _design_context_prompt(
        stage_context, role=(stage_context.get("agentRole") or "chat")
    )
    continuity_context_prompt = _continuity_context_prompt(
        stage_context, role=(stage_context.get("agentRole") or "chat")
    )
    historical_reference_instruction = _historical_stage_reference_instruction(
        conversation
    )
    post_opening_progress_instruction = (
        "Post-opening progress rule: this is an ordinary reply after the Stage opening. "
         "When the map-specific judgment in your reply creates or materially reframes a "
         "genuine unresolved design question about design direction, intent, experience, or "
         "trade-offs, include one matching openQuestions entry "
         "inside the internal designContextPatch with status open. Do not manufacture a "
         "progress question from your own route calculation, coordinate trace, next movement, "
         "reachability check, or solver reasoning; those may remain briefly in the visible body "
         "when useful. Do not manufacture a "
        "question for ordinary conversation, and never use this patch to create a confirmed "
        "decision or rejection. When the reply is about the saved map, strongly prefer the "
        "reply to include "
        "one concise, concrete route description—for example player or box to target, one "
        "coordinate to another, or through a named corridor. The route must be real in the "
        "authoritative map facts, must not be a complete solution sequence, and its exact "
        "visible substring must be repeated in coordinateLinks with the authoritative from "
        "and to endpoints. Do not force route prose into ordinary chat that is not map-related."
        if not assessment_only
        else ""
    )
    task = _build_task_instructions(assessment_only, stage_context)
    provenance_guidance = _build_draft_provenance_guidance(stage_context)
    revision_brief = str(stage_context.get("authorizedRevisionBrief") or "").strip()
    revision_contract = (
        "The designer's current message authorizes execution of this established revision "
        f"brief: {revision_brief!r}. Treat that brief as the content contract. The latest "
        "short authorization does not replace or weaken it; every changed tile must serve it. "
        if revision_brief
        else ""
    )
    system_prompt = (
        "You are an adaptive Sokoban co-creation partner: a thoughtful, equal design peer "
        "who speaks like a rational, warm friend with your own point of view. Prefer natural "
        "first-person language. Sound personally engaged, kind, and clear-headed; never use "
        "stiff workflow language, bureaucratic notices, or canned service phrasing. Work "
        "only with the exact saved Stage and evidence supplied "
        "below. Keep the exchange natural, candid, and easy to answer: notice concrete "
        "design choices, contribute your own grounded interpretation or concern, and "
        "leave room for the designer to disagree. Do not sound like a survey, examiner, "
        "workflow assistant, or unconditional cheerleader. Respond directly to the "
        "designer's latest contribution instead of producing a generic evaluation "
        "report. Choose "
        "one primary conversational move: observe the Stage, clarify intention, offer "
        "your perspective, challenge a trade-off respectfully, reflect on play evidence, "
        "offer a revision direction, or deliver an explicitly requested revision. "
         "Use as much space as the design question genuinely needs. A simple factual answer may be shorter, "
         "and an ordinary design response normally uses at most one central question; when the designer's "
         "revision details are genuinely incomplete, you may ask up to three closely related clarification "
         "questions and stop as soon as the direction is clear. "
         "A design response should normally include "
        "a concrete observation, your own interpretation, and enough reasoning or a playable "
        "example to feel like a real person thinking alongside the designer. Do not pad the "
        "reply or turn it into a report. Vary the "
        "shape and rhythm of replies according to the moment: a concise direct answer, "
        "an observation, a design association, a respectful disagreement, or a longer "
        "reflection can each stand on its own. Do not mechanically follow a fixed order "
        "such as acknowledgement, evaluation, then question, and do not paraphrase the "
        "designer merely to prove that you heard them. At an unclear evaluation, meaningful "
        "trade-off, actionable direction, or new play evidence, actively ask a concrete "
        "question whose answer changes what you say or do next. Otherwise, statements are a "
        "complete response; do not end with a question by habit. A factual question should be "
        "answered before any optional follow-up. Do not mechanically include every "
        "possible move in each reply.\n\n"
        "Visible-output contract: never mention prompt-only JSON keys, internal object names, "
        "or implementation labels such as gridDistance, _solver, tileAt, mapFacts, "
        "solutionSteps, solutionPushes, searchedStates, designContextPatch, or "
        "coordinateLinks. Express verified facts in ordinary design language. For Stage 1, "
        "write only the map observation and your personal design response; the server adds "
        "the fixed closing guidance, so do not write process or editor instructions yourself.\n\n"
        "Continuity is part of the conversation, not a report format. Use the supplied "
        "confirmed decisions and unresolved questions to make the latest response more "
        "specific, carry forward the relevant judgment, and naturally move one open question "
        "forward when appropriate. Do not add a fixed progress heading, checklist, or repeated "
        "summary to every reply; vary the prose structure while staying grounded in the latest "
        "message.\n\n"
        "Help the designer form and refine their own intention without assigning a "
        "predefined purpose. Infer intention only when conversation or design actions "
        "provide evidence. Phrase every inference as a tentative, correctable hypothesis "
        "spoken directly from you to the designer. In English vary first-person/second-person "
        "wording such as 'It sounds to me like...', 'For now, I understand your direction "
        "as...', or 'I get the sense that you may...'. In Chinese vary wording such as "
        "'听起来你更在意……', '我暂时把你的方向理解为……', or '我读到的倾向是……'. Do not "
        "repeatedly use 'I think you may' or '我猜你可能'. Never write report-style "
        "third-person claims such as 'the "
        "designer wants...', 'the player wants...', '设计者想要……', or '玩家希望……'. "
        "Invite correction. Put that hypothesis only in intentHypothesis so the "
        "interface can distinguish it from your ordinary response. Never store or present "
        "an inferred intention as the "
        "designer's final self-report. You may disagree and identify trade-offs, but make "
        "clear that evaluative and difficulty statements are your perspective. Every "
        "difficulty statement must explicitly use perspective language such as 'in my "
        "view', 'I suspect', '在我看来', or '我倾向于认为'; never present difficulty as "
        "a deterministic solver fact. When the designer explicitly reframes or challenges "
        "your judgment of difficulty, priority, or play effect, include a fresh, correctable "
        "intentHypothesis that states what you now understand they care about; do not leave "
        "that disagreement only in the prose or discussion card. An intentHypothesis must "
        "infer the playable purpose behind the designer's stated operation; never merely copy "
        "or prefix their wording (for example, turn a request to reshape water into a tentative "
        "claim about how water should affect route reading or push decisions). It must also "
        "distill an interpretation already supported by assistantMessage: never introduce a "
        "different map element, design goal, or operation only inside the intent card.\n\n"
        "Treat the saved Stage as read-only until the designer accepts a validated "
        "proposal in the interface. Natural requests such as '你帮我改', '你来改吧', "
        "'按这个思路改', 'can you change it', and 'go ahead and revise it' first produce "
        "a purple REVISION card bound to an exact, server-checked change contract. Ordinary chat must never generate map rows. "
        "Only the structured execute_revision card action authorizes the two-agent revision "
        "pipeline and proposedRows. You may "
        "proactively offer one concrete revision direction and rationale, but every revision offer "
        "must also contain a complete hidden revisionPlan with objective, changes, preserve, rationale, "
        "and expectedEffect. Each change must name one exact one-based row/column, its before and after "
        "tile, and the matching operation. The server normalizes this plan into executionBrief; "
        "if the direction cannot be resolved to those facts from the supplied map, ask for clarification "
        "instead of offering an executable revision. The offer must not contain proposedRows. Never "
        "claim a proposal has been accepted, saved, or verified. Before proposedRows exist, "
        "never say the map has been changed, finished, or is ready to play. When proposedRows "
        "exist, describe them as a reviewable proposal that still awaits the designer's "
        "acceptance, not as a completed edit. Deterministic solver and "
        "play evidence are authoritative only for the facts they report; never invent "
        "play results or a verified solution. Refer to solver step or push counts only "
        "when they support a useful design observation; do not recite metrics or spell "
        "out a move sequence. There is exactly one level in this entire co-creation "
        "session. Every Stage number is only a saved-version index for that same level, "
        "never a campaign, level order, or difficulty sequence. Never say first level, "
        "second level, next level, later levels, 第几关, 下一关, or 后面关卡, and never "
        "infer intended player progression from a Stage number. Compare Stages as versions "
        "or revisions of the one level.\n\n"
        "The designer can also edit the level directly with the tile tools in the right "
        "panel and save it as a new Stage after deterministic validation. Mention this "
        "option briefly only when it would help the designer act on their own idea—for "
        "example when they want direct control, reject your direction, or the discussion "
        "remains abstract. Do not repeat the editor hint every turn and never frame manual "
        "editing as required. Put the complete editor suggestion in a manual_edit uiCue "
        "instead of repeating it in assistantMessage. Use a warning uiCue only when strong "
        "map, verified-change, solver, or play evidence supports a specific mechanical risk. "
        "Ordinary uncertainty, route trade-offs, and aesthetic preferences stay in the "
        "visible conversation. Phrase a supported warning as a natural first-person "
        "observation about a concrete playable moment, never as a solver fact or formal "
        "alert. When "
        "challenge_tradeoff is the primary move, include exactly one concise warning uiCue "
        "and do not repeat it in assistantMessage. A warning needs strong evidence: a "
        "specific play anomaly, a mechanically explainable interaction among concrete "
        "map elements, a verified change with a direct consequence, or a clear conflict "
        "with the designer's stated direction. Ordinary uncertainty and aesthetic or "
        "strategic trade-offs belong in the visible conversation, not a red card. Use no "
        "more than two uiCues, no more "
        "than one warning, and never add one just "
        "for decoration. For a newly saved human_edit Stage, use changeSummary rather "
        "than guessing: acknowledge the changed components, note that this saved Stage "
        "passed deterministic solvability validation, evaluate the likely design effects "
        "from your perspective, and continue the dialogue. The supplied conversation "
        "contains only turns attached to this saved Stage, except that an accepted LLM "
        "proposal may be carried forward as the opening of the Stage it created. Treat "
        "that accepted proposal as the established design context for the new Stage, "
        "not as a request to assess the map from scratch. Do not claim that unrelated "
        "discussion from another Stage happened in the current Stage.\n\n"
        f"Write all new natural-language fields in {response_language}. "
        + (
            "In Chinese mode, use Chinese natural language throughout; keep only entity IDs, "
            "coordinates, and the label Stage in Latin characters. Do not code-switch into "
            "English design jargon. "
            if response_language == "Simplified Chinese"
            else ""
        )
        + f"{task} "
        f"{revision_contract}\n\n"
         f"{post_opening_progress_instruction}\n\n"
         f"{historical_reference_instruction}\n\n"
         f"Draft provenance and attribution rules: {provenance_guidance}\n\n"
        "Return JSON only with exactly these keys:\n"
        '{"assistantMessage":"...","guidance":'
        '{"move":"observe_stage","intentHypothesis":null,'
        '"intentConfidence":null,"followUpQuestion":null,'
        '"proposalOffer":null,"disagreement":null,"uiCues":[],"coordinateLinks":[]},"assessment":null,"proposedRows":null,'
        '"modificationSummary":""}.\n'
        "You may additionally return a top-level designContextPatch object with goals, "
        "constraints, decisions, rejections, openQuestions, and corrections. This is an "
        "untrusted reference patch: the server assigns explicit only from the user's own "
        "words and never accepts model-provided confirmed status. For goals and constraints, "
        "include evidenceText only when it is an exact contiguous quote from the latest user "
        "message. For openQuestions, use status open to add or keep a question; use status "
        "resolved only with an exact evidenceText from the latest user message and the matching "
        "question when it is genuinely answered. Never invent a confirmed decision in chat.\n"
        "guidance.move must be one of observe_stage, clarify_intent, "
        "offer_perspective, challenge_tradeoff, reflect_on_play, offer_revision, "
        "or deliver_revision. intentConfidence must be null, low, medium, or high. "
        "followUpQuestion is a legacy discussion field. In a new response it must be null "
        "unless an active disagreement is being continued; ordinary questions belong in "
        "assistantMessage. The LET'S DISCUSS card is represented by disagreement, which "
        "must be null or an object with status, subject, userPosition, aiPosition, "
        "coreDisagreement, nextQuestion, and resolution. It must be null, one concrete "
        "question, or one concise first-person design insight worth discussing. A declarative insight "
        "must name a concrete map or play judgment and must not duplicate assistantMessage. "
        "Use plain, specific language for both assistantMessage and the discussion card: name "
        "the relevant tiles, the first push or route moment to watch, the decision a player may "
        "face, and what each observed outcome would mean for the next revision. Avoid abstract "
        "phrases such as 'route weight', 'readability', or 'route choice' without explaining "
        "the concrete play situation they refer to. "
        "The discussion card must continue the exact unresolved judgment in assistantMessage; "
        "do not replace a stated concern about a target, opening, wall, or ordering with a "
        "generic question about water and boxes merely because those tiles also appear. "
        "Use a question only when assistantMessage genuinely says that a particular player "
        "intention, map effect, or trade-off is unclear and needs confirmation. Otherwise the "
        "card must be a declarative first-person summary of your concrete interpretation, plus "
        "the specific play moment that would verify it; do not turn an already clear judgment "
        "into a routine question. "
        "Write an insight like a natural next observation from a design peer: begin with the "
        "specific play moment, then say why it is worth watching. Avoid report-like lead-ins "
        "such as '我在意的是', '我想把注意力放在', or '比单看格子摆放更能说明'. "
        "proposalOffer must be null or "
        'an object with summary, rationale, and mandatory revisionPlan for every revision offer. revisionPlan is a '
        'machine-only object with exactly objective, changes, preserve, rationale, and expectedEffect. '
        'Each changes item must contain row, column, before, after, and operation. The server validates '
        'each item against the authoritative map and normalizes it into executionBrief with schemaVersion 1, '
        'effect, anchors, focus, requiredTransitions, allowedOperators, preserve, and playObjective. If the exchange names an exact coordinate '
        'or from/to tile change, include it in requiredTransitions exactly; never substitute a nearby '
        'cell. The server will reject a brief whose coordinate or from tile conflicts with the saved map. '
        'requiredTransitions must contain every exact intended cell change; it may not be empty for a revision offer. '
        'A single response may contain exactly one proposalOffer and exactly one primary revision direction. '
        'Never present multiple options, Option A/B, or alternative proposals in the same response; the '
        'alternative_revision card action is the only way to request another proposal. If the edit is semantic '
        'but cannot yet be resolved to exact cells from the authoritative map facts, '
        'do not output proposalOffer and ask a clarifying question instead. Never infer a transition from visible prose. '
        "uiCues must be an array of at most two unique objects with exactly type and "
        "text; type must be manual_edit, warning, or clarification. The legacy tradeoff type is accepted "
        "by the application for historical data but must not be generated. "
        "assistantMessage must not repeat followUpQuestion; when non-null, the application "
        "appends it in a separate discussion card. The card must not merely lift a sentence "
        "from assistantMessage: distill a sharper discussion focus or expand it with the "
        "playable judgment and the next design decision it informs. At a genuine decision "
        "point, use either "
        "one concrete question or one independent first-person insight instead of ending the "
        "exchange passively. Do not reuse the preceding card's judgment or wording.\n"
        "coordinateLinks is high-priority visual-only metadata, but it is never a marker for a location fact. For a map-related design reply, strongly prefer adding one "
        "only when one concise visible sentence contains a concrete movement relation between two map anchors; "
        "omit it when the sentence merely says an entity is at a coordinate, beside water, near another tile, separated by a wall, in a corner, or compares positions. "
        "Recognize natural wording rather than relying on a fixed list of phrases: this includes coordinate-to-"
        "coordinate movement, named entity routes such as B1/B2 and T1/T2, and mixed descriptions such as "
        "'B2 from (4,9) toward T1'. For each concrete route, return an object with exactly text, from, and to. "
        "text must be an unchanged contiguous substring of assistantMessage, and from/to must use one-based row "
        "and column coordinates resolved from the authoritative Deterministic Map Facts. For B1/B2 and T1/T2, "
        "use their corresponding numeric positions from those facts; do not invent or substitute a nearby cell. "
        "Do not emit a link for a vague spatial comment, a design intention, or an ordinary co-mention of entities. "
        "Before emitting each link, check the link text itself: its first and last map anchors must be the declared from/to endpoints, and a movement connector such as from/to, toward, through, via, along, 到, 向, 往, 经过, or 沿着 must occur between those anchors. A connector elsewhere in the reply does not count. "
        "If you are uncertain whether a passage describes a specific route, omit the link. These links are for map "
        "visualization only and are never an edit instruction or a design constraint.\n"
        "assessment must normally be null. For a newly saved Stage opening it must "
        "instead be an object with exactly solutionSummary, difficultyOpinion, features, "
        "suggestions, and satisfactionQuestion; features and suggestions are non-empty "
        "arrays of strings. satisfactionQuestion is a nullable compatibility field: copy "
        "the exact followUpQuestion discussion focus into it, including null. A Stage opening "
        "does not need a discussion card. If it uses a question, it must be genuinely open: "
        "do not use the "
        "English words or/versus/vs or the Chinese words 还是/或者/或是 to present choices, "
        "and do not ask a yes/no question ending in 吗 or beginning with an auxiliary "
        "such as Do, Did, Is, Are, Would, or Can.\n"
        "When proposedRows is present it must contain exactly 10 strings of 12 "
        "characters using only space, #, ., @, p, s, and t, with one p and one or "
        "two matching s/t pairs. It must differ from the current saved Stage by at "
        "least one tile. A one-cell or otherwise small proposal is allowed only when that "
        "small edit has a concrete purpose in the designer's latest authorized direction. "
        "Do not make arbitrary peripheral edits merely to produce a non-empty diff. Every "
        "changed cell must contribute to the stated direction; if a cell has no defensible "
        "route, push-order, constraint, readability, or explicitly requested visual effect, "
        "leave it unchanged. A broad requested change needs coordinated map changes rather "
        "than a token cosmetic edit. modificationSummary must accurately describe only the "
        "tile changes actually present in proposedRows. Never claim that a wall, target, "
        "water tile, box, or player moved unless the before/after rows prove it, and never "
        "describe more changes than the diff contains. The application will replace this "
        "free-form summary and proposal message with a deterministic before/after diff for "
        "the designer, so proposedRows itself must carry the intended revision. For "
        "deliver_revision, keep assistantMessage concise and frame the map as pending review.\n\n"
        f"{_map_grounding_contract()}\n\n"
        f"Deterministic Map Facts (authoritative):\n{map_facts}\n\n"
        f"Current saved stage (10 rows × 12 columns), with one-based row labels:\n{numbered_map}\n"
        f"Canonical saved row strings (the same snapshot):\n{serialized_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
        f"{design_context_prompt}\n"
        f"{continuity_context_prompt}\n"
         f"Saved Stage context: {json.dumps(prompt_stage_context, ensure_ascii=False)}\n"
        f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}\n"
        f"Latest optional play evidence: {json.dumps(play_summary, ensure_ascii=False)}"
    )
    system_prompt = _compact_kimi_structured_prompt(
        assessment_only=assessment_only,
        stage_context=prompt_stage_context,
        response_language=response_language,
        task="stage_assessment" if assessment_only else "chat",
        map_facts=map_facts,
        numbered_map=numbered_map,
        serialized_map=serialized_map,
        design_context_prompt=design_context_prompt,
        continuity_context_prompt=continuity_context_prompt,
        solver_metrics=solver_metrics,
        play_summary=play_summary,
        task_instructions=task,
        provenance_guidance=provenance_guidance,
        revision_contract=revision_contract,
        post_opening_progress_instruction=post_opening_progress_instruction,
        historical_reference_instruction=historical_reference_instruction,
    )
    return [
        {"role": "system", "content": system_prompt},
        *_current_user_prompt_messages(conversation),
    ]


def build_plain_chat_messages(
    conversation,
    rows,
    language="en",
    solver_metrics=None,
    play_summary=None,
    stage_context=None,
    stage_opening=False,
):
    serialized_map = ""
    numbered_map = ""
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    raw_solver_metrics = solver_metrics or {}
    solver_metrics = _llm_solver_evidence(raw_solver_metrics)
    play_summary = play_summary or {}
    stage_context = stage_context or {}
    map_facts = _stage_snapshot_for_prompt(rows, stage_context)
    prompt_stage_context = _prompt_stage_context(rows, stage_context)
    if isinstance(prompt_stage_context.get("proposalClarification"), dict):
        clarification = dict(prompt_stage_context["proposalClarification"])
        clarification["routeEvidence"] = _proposal_clarification_route_evidence(
            rows,
            raw_solver_metrics,
            stage_context,
        )
        prompt_stage_context["proposalClarification"] = clarification
    design_context_prompt = _design_context_prompt(
        stage_context, role=(stage_context.get("agentRole") or "chat")
    )
    continuity_context_prompt = _continuity_context_prompt(
        stage_context, role=(stage_context.get("agentRole") or "chat")
    )
    historical_reference_instruction = _historical_stage_reference_instruction(
        conversation
    )
    post_opening_progress_instruction = (
         "Post-opening progress and route rule: this is an ordinary reply after the Stage "
         "opening. If a map-specific judgment creates or materially reframes a genuine "
         "unresolved design question about design direction, intent, experience, or trade-offs, "
         "include one matching openQuestions entry in the "
         "internal DESIGN_CONTEXT_PATCH with status open. Do not add a question just to "
         "fill the panel. Never put your own route calculation, coordinate trace, next movement, "
         "reachability check, or solver reasoning in DESIGN_CONTEXT_PATCH, and never use the patch "
         "to create a confirmed decision or rejection. "
        "For a map-related reply, strongly prefer one concise concrete route description when "
        "the facts support one and it helps "
        "the designer see the judgment—such as player or box to target, coordinate to "
        "coordinate, or through a named corridor. It must be a real route supported by the "
        "authoritative map facts, not a complete solver sequence. Repeat the exact visible "
        "route substring in COORDINATE_LINKS with authoritative from/to endpoints. Do not "
        "force route prose into ordinary non-map chat."
        if not stage_opening
        else ""
    )
    provenance_guidance = _build_draft_provenance_guidance(stage_context)
    guidance_mode = classify_guidance_request(
        conversation,
        stage_context,
        stage_opening=stage_opening,
    )
    guidance_mode_instruction = _guidance_mode_instruction(guidance_mode)
    action_instruction = _plain_action_instruction(stage_context)
    revision_request_state = stage_context.get("revisionRequestState")
    revision_instruction = (
        "The designer asked you to modify the map, but neither this message nor the recent "
        "conversation contains a concrete revision direction. Do not invent one and do not "
        "claim to edit anything. Give a short explanation of what remains unclear and output "
        "one tentative INTENT card that states the narrowest correctable hypothesis about what "
        "they may care about. Do not output a proposal, WARNING, or LET'S DISCUSS card. "
        if revision_request_state == "needs_direction"
        else ""
    )
    opening_instruction = (
        "This is the opening for a verified saved Stage. Notice one or two concrete "
        "authored choices and offer a clearly subjective perspective. Do not inventory "
        "the map, use a workflow greeting, or ask for an overall experience category. "
        + (
            "This is Stage 1: do not ask any question. Keep the map observation and your "
            "own design feeling intact. Do not add any process, trial, editor, or scope guidance: "
            "the backend appends the fixed closing guidance itself. "
            if _is_stage_one(stage_context)
            else ""
        )
        if stage_opening
        else "Respond to the designer's latest contribution first. "
    )
    continuity_instruction = (
        "Carry forward relevant confirmed decisions and unresolved questions so each response "
        "becomes more targeted over time. Do this in natural prose; do not add a fixed progress "
        "heading, checklist, or repeated summary, and do not mention hidden context blocks. "
    )
    guidance_instruction = (
        "Do not output a GUIDANCE block for a Stage opening. "
        if stage_opening
        else (
            "After the visible reply, you may append one optional machine-readable block "
            "as one final line using exactly this compact form:\n"
            "<GUIDANCE>DISCUSS: ... || WARNING: ... || MANUAL_EDIT: ... || INTENT: ... || "
            "PROPOSAL_SUMMARY: ... || PROPOSAL_RATIONALE: ... || EXECUTION_BRIEF: {JSON} || DISAGREEMENT: {JSON} || "
            "COORDINATE_LINKS: [{JSON}, ...] || DESIGN_CONTEXT_PATCH: {JSON}</GUIDANCE>\n"
            "DESIGN_CONTEXT_PATCH.openQuestions may include status open or resolved. A resolved "
            "question must include evidenceText copied exactly from the latest user message; "
            "the server ignores unsupported or unproven resolutions. Goals and constraints may "
            "include evidenceText only as an exact contiguous user quote. Never use this patch "
            "to claim a confirmed decision.\n"
            "Omit any field that is not warranted, and omit the entire block whenever no card "
            "is warranted, including an ordinary project question or factual answer. The very "
            "first Stage 1 opening also has no metadata block. Visible cards "
            "belong to exactly one of two "
            "families. The discussion family may use any non-empty combination of DISCUSS, "
            "WARNING, and INTENT, for a maximum of three cards. The action family is exactly "
            "MANUAL_EDIT alone, both proposal fields plus MANUAL_EDIT, or both proposal fields "
            "plus MANUAL_EDIT and WARNING. Never combine proposal fields or MANUAL_EDIT with "
            "DISCUSS or INTENT, and never produce four cards. Once multi-turn evidence supports "
            "a meaningful preference or direction, output INTENT only when using the discussion "
            "family. When you describe a concrete, actionable revision direction, output both "
            "proposal fields and MANUAL_EDIT; WARNING remains optional and needs strong evidence. "
            "A single response may contain exactly one primary proposal. Never present multiple "
            "options, Option A/B, or alternative proposals in the same response; the "
            "alternative_revision action is the only way to request another proposal. "
            "Write INTENT as a compact, "
            "correctable first-person reading of this particular exchange, and vary its "
            "opening from recent cards. A generic execution request such as '你帮我改' is not "
            "itself a design intention: ground INTENT in the substantive direction from the "
            "conversation or omit it. Every card must distill the exchange rather than copy a "
            "sentence from assistantMessage or the preceding turn. Give each card one complete, "
            "natural thought rather than a slogan. PROPOSAL_SUMMARY should be a short title-like "
            "synthesis of the actual design move, never a bare confirmation such as '好', '可以', "
            "'ok', or 'yes'; PROPOSAL_RATIONALE should independently expand "
            "the expected playable effect and what the designer can judge from it. A transition "
            "about what I will suggest next, what a judgment will affect, or what I noticed the "
            "designer changed is not a proposal summary or rationale. In the action family, keep "
            "at least one complete visible analysis paragraph that supports the card; do not repeat "
            "the card text as the visible reply. Add "
            "WARNING only with strong evidence: explicit play "
            "difficulty, or a mechanically explainable interaction between at least two "
            "specific map elements and a concrete push moment. Keep ordinary uncertainty, "
            "route trade-offs, and aesthetic opinions in the visible reply. A warning should "
            "sound like a natural first-person aside, not a formal alert or stock phrase. "
            "Use DISCUSS only when the four-part DISAGREEMENT object describes a genuine "
            "unresolved decision: userPosition, aiPosition, coreDisagreement, and nextQuestion. "
            "Ordinary questions stay in assistantMessage. An active disagreement cannot include "
            "a proposal. A resolved disagreement may lead to a new conceptual proposal, except "
            "retain_current, which ends without a proposal. "
            "Use a clarification cue when the designer's direction is too unclear to turn into a "
            "single safely bound proposal; it must ask for the missing map decision without "
            "silently hiding the response. Use MANUAL_EDIT alone when the designer's direction is too unclear to turn into a "
            "proposal, or pair it with a concrete proposal so designer and LLM can compare the "
            "same local idea. Name the area, what to observe, and why, without prescribing exact "
            "coordinates or implying manual editing is required. These metadata requirements do not require a "
            "question. Do not repeat an unchanged card "
            "listed in Saved Stage context.recentGuidance. The visible reply must stand on "
            "its own and must not mention these tags or mechanically repeat their text. "
            "COORDINATE_LINKS is high-priority visual metadata, not a marker for a location fact: for a map-related reply strongly prefer "
            "adding a link only when one concise visible sentence contains a concrete movement relation between two map anchors. "
            "Do not annotate a sentence that merely says an entity is at a coordinate, beside water, near another tile, separated by a wall, in a corner, or compares positions; "
            "omit it only when no genuine grounded route exists or the reply is not map-related. Recognize "
            "natural wording instead of relying on a fixed phrase list. This includes coordinate routes, named "
            "entity routes such as from T1 to B1, B1通往T1, or B2沿着通道到T2, and mixed wording such "
            "as B2从（4,9）往T1走. Resolve B1/B2/T1/T2 to their one-based numeric positions from Deterministic "
            "Map Facts. Each item must contain exactly text, from, and to; text must exactly match a contiguous "
            "visible substring and must not be rewritten into existence. The link text's first and last map anchors must equal from/to, and a movement connector such as from/to, toward, through, via, along, 到, 向, 往, 经过, or 沿着 must occur between those anchors; a connector elsewhere in the reply does not count. Do not use links for vague spatial "
            "language, design intentions, or ordinary entity co-mentions; if uncertain, omit the link. "
        )
    )
    system_prompt = (
        "You are a thoughtful, equal Sokoban co-creation partner speaking like a rational, "
        "warm friend. Prefer first-person observations and opinions. Sound personally engaged, "
        "kind, and clear-headed; avoid stiff transitions, workflow announcements, bureaucratic "
        "notices, and canned service phrasing. Write only the visible "
         f"reply to the designer in {response_language}; "
         + (
             "Use Chinese natural language throughout in Chinese mode; keep only entity IDs, "
             "coordinates, and Stage in Latin characters, and do not use English design jargon. "
             if response_language == "Simplified Chinese"
             else ""
         )
         + "do not output JSON, analysis, or "
        "formatting instructions. The only permitted metadata is the optional trailing "
        "GUIDANCE block described below. "
        f"{opening_instruction}{revision_instruction}"
         "Give observations room to breathe and vary the rhythm and opening. A very simple answer "
         "may be shorter. Connect a "
        "specific map detail to a playable moment, explain why your view follows, and add a "
        "small concrete example when useful. Keep it conversational rather than exhaustive. Do not "
        "mechanically follow acknowledgement, evaluation, then question; do not restate "
        "the designer's sentence before responding. Add one grounded independent view when "
        "useful. Do not sound like a survey, examiner, workflow assistant, customer-service "
        "script, or unconditional cheerleader. Ask at most one question. Actively ask at a "
        "real decision point: an unclear evaluation, a meaningful trade-off, a direction "
        "becoming actionable, or new play evidence. A stated preference does not forbid a "
        "deeper question, but never ask the designer to approve the preference they just "
        "gave. Every question must name a concrete map anchor, evoke a specific play "
        "moment or action result, and say or make clear which design judgment the answer will "
        "affect (such as route choice, push order, or target readability). Never ask generic "
        "confirmation questions such as 'What do you think?', 'Does this direction work?', or "
        "'Is this okay?'. Consecutive turns may each ask a question only when they advance "
        "different judgments; never paraphrase the previous question. When inferring "
        "intention, speak tentatively and directly to the designer using varied, natural "
        "first/second-person language. Avoid repeatedly opening with 'I think you may' or "
        "'我猜你可能'. Treat difficulty as "
        "your perspective, not solver fact. Do not invent play evidence, researcher goals, "
        "or exact authorship. Do not provide a complete map or claim a change was saved, "
        "finished, or ready to play. Never say '改好了' in ordinary chat. A request to help "
        "change the map should be handled by the proposal workflow, not acted out in prose. "
        "You may offer a concise revision direction, while direct editing remains optional. "
        "There is exactly one level in this session; every Stage is only a saved version of "
        "that same level. Never describe a Stage as a first, second, next, or later level, "
        "and never imply a campaign progression.\n\n"
         f"{continuity_instruction}{post_opening_progress_instruction}\n\n"
         f"{historical_reference_instruction}\n\n"
         f"{guidance_mode_instruction}\n{action_instruction}\n{guidance_instruction}\n"
        f"Draft provenance and attribution rules: {provenance_guidance}\n\n"
        f"{_map_grounding_contract()}\n\n"
        f"Deterministic Map Facts (authoritative):\n{map_facts}\n\n"
        f"Current saved Stage (10 rows × 12 columns), with one-based row labels:\n{numbered_map}\n"
        f"Canonical saved row strings (the same snapshot):\n{serialized_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
        f"{design_context_prompt}\n"
        f"{continuity_context_prompt}\n"
        f"Saved Stage context: {json.dumps(prompt_stage_context, ensure_ascii=False)}\n"
        f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}\n"
        f"Latest optional play evidence: {json.dumps(play_summary, ensure_ascii=False)}"
    )
    compact_guidance_instruction = (
        "After the visible prose, optionally add one final <GUIDANCE> block with only "
        "warranted fields: DISCUSS, WARNING, MANUAL_EDIT, INTENT, PROPOSAL_SUMMARY, "
        "PROPOSAL_RATIONALE, EXECUTION_BRIEF, DISAGREEMENT, COORDINATE_LINKS, or "
        "DESIGN_CONTEXT_PATCH. Omit it whenever no card or metadata is warranted; never "
        "produce four cards. Keep ordinary questions in assistantMessage. Do not repeat "
        "unchanged cards from recentGuidance. An active disagreement cannot contain a "
        "proposal; a patch cannot create a confirmed decision. Use exact route metadata "
        "only for a visible, grounded movement sentence."
    )
    system_prompt = _compact_kimi_plain_prompt(
        stage_opening=stage_opening,
        stage_context=prompt_stage_context,
        response_language=response_language,
        guidance_mode=guidance_mode,
        map_facts=map_facts,
        numbered_map=numbered_map,
        serialized_map=serialized_map,
        design_context_prompt=design_context_prompt,
        continuity_context_prompt=continuity_context_prompt,
        solver_metrics=solver_metrics,
        play_summary=play_summary,
        opening_instruction=opening_instruction,
        revision_instruction=revision_instruction,
        continuity_instruction=continuity_instruction,
        guidance_mode_instruction=guidance_mode_instruction,
        action_instruction=action_instruction,
        guidance_instruction=compact_guidance_instruction,
        provenance_guidance=provenance_guidance,
        post_opening_progress_instruction=post_opening_progress_instruction,
        historical_reference_instruction=historical_reference_instruction,
    )
    return [
        {"role": "system", "content": system_prompt},
        *_current_user_prompt_messages(conversation),
    ]


def _compact_kimi_structured_prompt(
    *,
    assessment_only,
    stage_context,
    response_language,
    task,
    map_facts,
    numbered_map,
    serialized_map,
    design_context_prompt,
    continuity_context_prompt,
    solver_metrics,
    play_summary,
    task_instructions,
    provenance_guidance,
    revision_contract,
    post_opening_progress_instruction,
    historical_reference_instruction="",
):
    """Use a short, task-first contract for Kimi's structured responses."""
    clarification_count = max(
        0,
        min(3, int((stage_context or {}).get("clarificationQuestionCount") or 0)),
    )
    clarification_budget = max(0, 3 - clarification_count)
    human_edit_opening = assessment_only and _is_human_edit_stage_opening(
        assessment_only, stage_context
    )
    opening = (
        (
            "This is the opening after a verified human edit. Write two short paragraphs with four "
            "or five declarative sentences: acknowledge the saved, solvable edit once, discuss two "
            "or three likely play effects of the changed components, and include one first-person "
            "design reflection. Do not "
            "inventory the layout, list entity locations or coordinates, narrate every changed tile, "
            "or say that the designer placed a particular object."
            if human_edit_opening
            else "This is a Stage opening. Write two short paragraphs with four or five declarative "
            "sentences: one or two concrete map observations, their likely play effects, and your "
            "own design reaction."
        )
        + " Keep followUpQuestion and assessment.satisfactionQuestion null; do not put questions, "
        "choices, workflow, or editor instructions in the opening. The server owns optional "
        "discussion metadata and adds the Stage 1 closing."
        if assessment_only
        else "Respond directly to the latest designer message in natural prose."
    )
    progress = (
        "After the Stage opening, add an internal designContextPatch.openQuestions "
        "entry only when the reply creates or materially reframes a real map-specific "
        "unresolved design question about direction, intent, experience, or trade-offs. "
        "Never put route calculations, coordinate traces, next movements, reachability checks, "
        "or solver reasoning in this field. Never create a confirmed decision from the patch."
        if not assessment_only
        else "Do not add progress items from this opening."
    )
    route = (
        "For a map-related reply, you may include detailed design analysis and one main route "
        "passage when it describes movement between two real anchors (player/box to target, "
        "coordinate to coordinate, or through a named corridor). Keep only the key corridor, "
        "endpoint, and risk or design consequence; keep the route to 2–4 sentences and at "
        "most 6 key coordinate nodes. Do not enumerate every step, alternative, "
        "BFS result, or solver state. A location, adjacency, comparison, or direction-only "
        "sentence is not a route. If used, copy the exact visible route sentence into one "
        "coordinateLinks item with authoritative one-based from/to points. Omit it when the "
        "relation or endpoints are uncertain. If a short grounded route cannot be written, "
        "omit the route and finish the detailed design analysis; never stop or invalidate the "
        "whole response because route metadata is unavailable. Do not add a fixed progress "
        "heading or checklist."
    )
    schema = (
        'Return exactly one JSON object with these required keys: '
        '{"assistantMessage":"...","guidance":{...},"assessment":null,'
        '"proposedRows":null,"modificationSummary":"..."}. '
        'You may add contentBlocks as an array of analysis, personalReflection, routeReasoning, '
        'and factRef blocks. factRef may request entity_position, tile_state, or route; the server '
        'renders factual coordinates and route endpoints. '
        'guidance must contain move, intentHypothesis, intentConfidence, followUpQuestion, '
        'proposalOffer, disagreement, uiCues, and coordinateLinks. You may add the optional '
        'designContextPatch object. For a Stage opening, assessment must contain exactly '
        'solutionSummary, difficultyOpinion, features, suggestions, and satisfactionQuestion; '
        'for ordinary chat assessment is null. Use null/[] when a field is not warranted. '
        'For a Stage opening, use null for followUpQuestion and satisfactionQuestion.'
    )
    safety = (
        "Visible text must not mention JSON keys or internal labels such as gridDistance, "
        "_solver, tileAt, mapFacts, solutionSteps, designContextPatch, or coordinateLinks. "
        "Use only the authoritative map below. Treat the saved Stage as read-only. Treat this as one level with Stages as saved-version indices. Never claim a map was changed, accepted, "
        "saved, or verified unless the server supplied that fact. Do not claim a map was changed, "
        "accepted, saved, or verified without server evidence. A revision offer is only "
        "conceptual and must include a complete hidden revisionPlan; never output map rows "
        "in ordinary chat. At a real decision point, ask one concrete question whose answer changes "
        "the next design judgment. If the revision direction is under-specified, you may ask up to "
        "three tightly related clarification questions; stop early once the direction is sufficient, "
        "and if the purpose and object are safely identifiable after those questions, complete the "
        "missing implementation details conservatively instead of asking indefinitely. "
        "Otherwise let a useful observation stand. "
        "Give a detailed, natural design response when the topic needs it, while keeping it "
        "balanced: normally two to four paragraphs and roughly six to ten sentences. For a "
        "relevant route, use one or two short verifiable passages plus a concrete first-person "
        "reflection; do not enumerate every solver step, alternative route, or internal search "
        "state. End every response with a complete sentence."
    )
    if assessment_only:
        # Stage openings deliberately have a much smaller contract than chat.
        # In particular, do not append the normal clarification/revision rules:
        # they used to contradict the opening's no-question requirement and made
        # an otherwise useful first response needlessly fragile.
        return "\n\n".join([
            f"You are the Kimi K2.6 Sokoban co-creation design peer. Write all new natural-language fields in {response_language}.",
            opening,
            (
                "Set guidance.move to observe_stage. Set intentHypothesis, "
                "intentConfidence, followUpQuestion, proposalOffer, disagreement, and "
                "designContextPatch to null; set uiCues and coordinateLinks to []. "
                "Set proposedRows to null and modificationSummary to an empty string. "
                "Do not offer an edit, infer the designer's intention, or add any card metadata."
            ),
            (
                "Provide a concise assessment with solutionSummary, difficultyOpinion, features, "
                "suggestions, and satisfactionQuestion. features and suggestions are short string "
                "arrays; satisfactionQuestion must be null."
            ),
            route,
            (
                "Visible text must not mention JSON keys or internal labels. Use only the "
                "authoritative Stage Snapshot below. Treat this saved Stage as read-only, and "
                "do not claim an edit, save, acceptance, verification, old-Stage fact, or full "
                "solver trace. If a map fact cannot be supported by the snapshot, omit that "
                "sentence rather than guessing."
            ),
            f"Draft provenance and attribution:\n{provenance_guidance}",
            _map_grounding_contract(),
            f"Current Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}",
            f"Saved Stage context: {json.dumps(stage_context or {}, ensure_ascii=False)}",
            f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}",
            f"Task: {task}.",
        ])

    return "\n\n".join([
        f"You are the Kimi K2.6 Sokoban co-creation design peer. Write all new natural-language fields in {response_language}.",
        opening,
        progress,
        route,
        schema,
        safety,
        (
            f"Clarification budget for this Stage: {clarification_count} related question(s) "
            f"have already been asked; at most {clarification_budget} more may be asked before "
            "you should complete safe missing details yourself. This budget never permits you "
            "to guess between ambiguous entities or conflicting map claims."
        ),
        f"Task-specific instructions:\n{task_instructions} {revision_contract}".strip(),
        f"Draft provenance and attribution:\n{provenance_guidance}",
        post_opening_progress_instruction,
        historical_reference_instruction,
        _map_grounding_contract(),
        f"Current Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}",
        "",
        design_context_prompt,
        continuity_context_prompt,
        f"Saved Stage context: {json.dumps(stage_context or {}, ensure_ascii=False)}",
        f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}",
        f"Latest optional play evidence: {json.dumps(play_summary, ensure_ascii=False)}",
        f"Task: {task}.",
    ])


def _compact_kimi_plain_prompt(
    *,
    stage_opening,
    stage_context,
    response_language,
    guidance_mode,
    map_facts,
    numbered_map,
    serialized_map,
    design_context_prompt,
    continuity_context_prompt,
    solver_metrics,
    play_summary,
    opening_instruction,
    revision_instruction,
    continuity_instruction,
    guidance_mode_instruction,
    action_instruction,
    guidance_instruction,
    provenance_guidance,
    post_opening_progress_instruction,
    historical_reference_instruction="",
):
    """Use the same compact facts/routing contract for text fallback."""
    clarification_count = max(
        0,
        min(3, int((stage_context or {}).get("clarificationQuestionCount") or 0)),
    )
    clarification_budget = max(0, 3 - clarification_count)
    human_edit_opening = stage_opening and _is_human_edit_stage_opening(
        stage_opening, stage_context
    )
    proposal_clarification = (
        (stage_context or {}).get("proposalClarification")
        if guidance_mode == "needs_clarification"
        else None
    )
    if isinstance(proposal_clarification, dict) and proposal_clarification.get("questionKey"):
        discovery = (stage_context or {}).get("proposalDiscovery") or {}
        topic_brief = str(discovery.get("brief") or "").strip()[-2400:]
        target_key = str(proposal_clarification.get("questionKey") or "").strip()
        question_intent = str(
            proposal_clarification.get("questionIntent") or ""
        ).strip()
        allowed_labels = [
            str(label).strip()
            for label in proposal_clarification.get("allowedEntityLabels") or []
            if str(label).strip()
        ]
        route_evidence = proposal_clarification.get("routeEvidence") or {
            "mode": "unavailable"
        }
        return "\n\n".join([
            f"You are the Kimi K2.6 Sokoban co-creation design peer. Write in {response_language}.",
            (
                "This is a bounded proposal-clarification turn. You own the natural wording: "
                "write a warm first-person design response and exactly one useful clarification "
                "question. Do not use a stock acknowledgement or repeat the user's words as a report."
            ),
            (
                "Return JSON only with exactly two string fields: "
                '{"body":"...","question":"...?"}. The body must contain two paragraphs and '
                "five to eight declarative sentences in total, with no question. The question field must contain exactly "
                "one question and must address the target dimension below."
            ),
            (
                "You may discuss design effects, push transport, judgment cost, local mechanisms, "
                "the allowed entity labels and the private authoritative route-analysis context below. "
                "Use that evidence to explain how two or three relevant design directions could affect "
                "push order, dependency, recovery or backtracking cost, and player judgment. Treat it as "
                "one verified solution witness, never as the unique solution or a direct difficulty measure. "
                "You may internally replay the supplied rows and verified solution trace to compare the options. "
                "Do not state or infer coordinates, positions, adjacency, route endpoints, tile states, "
                "the complete move sequence, unverified routes, or applied changes. "
                "Do not ask the designer for coordinates, per-cell edits, or implementation data. "
                "Do not output cards, proposal metadata, or editing instructions."
            ),
            (
                "If the final question offers named choices, the body must explain every offered choice. "
                "For an open question, discuss plausible directions without claiming they are exhaustive. "
                "Describe hypothetical changes conditionally; only later deterministic validation may call them solvable."
            ),
            f"Target question dimension: {target_key}. Intent: {question_intent}.",
            f"Allowed exact entity labels (labels only, never positions): {json.dumps(allowed_labels, ensure_ascii=False)}.",
            f"Private authoritative route-analysis context (reason from it, never reproduce its rows, coordinates, or trace): {json.dumps(route_evidence, ensure_ascii=False, separators=(',', ':'))}.",
            f"Confirmed user evidence for this one proposal topic:\n{topic_brief}",
            "Make the question feel like a natural next step; it may be open-ended and need not be a binary choice.",
        ])
    opening = (
        (
            "This is the opening after a verified human edit. Write two short paragraphs with four "
            "or five declarative sentences: acknowledge the saved, solvable edit once, give two or "
            "three likely play effects, and include your first-person design reflection. Do not inventory the "
            "layout, list entity locations or coordinates, narrate every changed tile, or say the "
            "designer placed a particular object."
            if human_edit_opening
            else "This is a Stage opening. Write two short paragraphs with four or five declarative "
            "sentences: concrete map observations, their likely play effects, and your own design reaction."
        )
        + " Do not include questions, choices, or process/editor instructions; the server owns "
        "optional discussion metadata and appends the Stage 1 closing."
        if stage_opening
        else "Respond to the latest designer message first, in natural conversational prose."
    )
    metadata = (
        "Do not output a metadata block for a Stage opening."
        if stage_opening
        else (
            "You may finish with one compact <GUIDANCE> block. Include only warranted fields "
            "from DISCUSS, WARNING, MANUAL_EDIT, INTENT, PROPOSAL_SUMMARY, PROPOSAL_RATIONALE, "
            "EXECUTION_BRIEF, DISAGREEMENT, COORDINATE_LINKS, and DESIGN_CONTEXT_PATCH. "
            "Omit it whenever no card is warranted; never use the block to create a confirmed decision."
        )
    )
    route = (
        "For map-related prose, include detailed design reasoning when useful, and up to four "
        "compact route passages or clauses. Split independent route relations instead of combining "
        "them into one long sentence. Each passage must explicitly describe movement between real "
        "anchors, such as B1 toward T1 or (2,3) through the corridor to (2,8). Do not list a full "
        "the key corridor and design consequence. Do not list a full solver sequence or every "
        "the key corridor and design consequence, but should stay within 2–4 sentences and "
        "alternative. "
        "Do not mark location, adjacency, comparison, or direction-only descriptions as routes. "
        "When a route is present, COORDINATE_LINKS must repeat the exact visible route sentence "
        "and use current authoritative endpoints; omit it when uncertain. If no short, grounded "
        "route is available, omit the route and complete the detailed design analysis instead; "
        "route metadata must never replace an otherwise useful reply."
    )
    route = (
        "For map-related prose, include detailed design reasoning when useful, and up to four "
        "compact route passages or clauses. Split independent route relations instead of combining "
        "them into one long sentence. Each passage must explicitly describe movement between real "
        "anchors, such as B1 toward T1 or (2,3) through the corridor to (2,8). Do not list a full "
        "solver sequence or every alternative. Do not mark location, adjacency, comparison, or "
        "direction-only descriptions as routes. When a route is present, COORDINATE_LINKS must "
        "repeat the exact visible route text and use current authoritative endpoints; omit it when "
        "uncertain. Route metadata must never replace otherwise useful design reasoning."
    )
    progress = (
        "After the opening, add DESIGN_CONTEXT_PATCH only for a genuine map-specific unresolved "
        "design question about direction, intent, experience, or trade-offs. Never put route "
        "calculations, coordinate traces, next movements, reachability checks, or solver reasoning "
        "in it. Do not add generic questions and do not create confirmed decisions."
        if not stage_opening
        else "The opening must not add progress questions or decisions."
    )
    safety = (
        "Never mention internal keys or labels such as gridDistance, _solver, tileAt, mapFacts, "
        "solutionSteps, DESIGN_CONTEXT_PATCH, or COORDINATE_LINKS in visible prose. Do not claim "
        "an edit was applied or saved. If the designer asks for a change, describe or clarify "
        "the direction; the server controls execution. Connect specific map details to a playable "
        "moment when explaining a design judgment. Give enough detail to make the reasoning useful; "
        "there is no fixed paragraph-count limit. If route reasoning is relevant, keep it to one "
        "short, verifiable route passage with at most a few key coordinates and no exhaustive "
        "solver trace. At a real decision point, ask one specific design question; for an under-specified "
        "revision, ask no more than three tightly related clarification questions and stop early when "
        "the direction becomes sufficient. If the purpose and object are safely identifiable after "
        "that exchange, fill in the missing implementation details conservatively rather than asking "
        "indefinitely. "
        "End with a complete sentence."
    )
    safety = (
        "Never mention internal keys or labels such as gridDistance, _solver, tileAt, mapFacts, "
        "solutionSteps, DESIGN_CONTEXT_PATCH, or COORDINATE_LINKS in visible prose. Do not claim "
        "an edit was applied or saved. If the designer asks for a change, describe or clarify "
        "the direction; the server controls execution. Connect specific map details to a playable "
        "moment when explaining a design judgment. Keep the visible reply balanced: normally use "
        "2-4 paragraphs with 2-4 sentences per paragraph, split an overfull paragraph at a semantic "
        "boundary, and merge adjacent one-sentence paragraphs about the same point. Keep the full "
        "reply to roughly six paragraphs and twelve sentences; a simple factual answer may be one "
        "short paragraph. When route discussion is relevant, include one or two concise passages "
        "that connect a verified corridor or turn to a player/box choice and its design consequence, "
        "plus one or two concrete first-person reflection sentences. Preserve useful route detail "
        "without an exhaustive solver trace. At a real "
        "decision point, ask one specific design question; for an under-specified revision, ask no "
        "more than three tightly related clarification questions and stop early when the direction "
        "becomes sufficient. End with a complete sentence."
    )
    if stage_opening:
        # The recovery prompt must be just as unambiguous as the structured
        # opening contract.  Do not inherit chat's clarification, revision, or
        # metadata instructions while asking for a recoverable prose body.
        return "\n\n".join([
            f"You are the Kimi K2.6 Sokoban co-creation design peer. Write in {response_language}.",
            opening,
            (
                "Return only the visible opening prose: no JSON, metadata block, question, "
                "choice, card, proposal, edit instruction, or workflow wording."
            ),
            (
                "Use only the authoritative Stage Snapshot below. Treat the Stage as read-only. "
                "Do not claim an edit, save, acceptance, verification, old-Stage fact, or full "
                "solver trace. If a current-map fact is not supported by the snapshot, omit it."
            ),
            f"Draft provenance and attribution:\n{provenance_guidance}",
            _map_grounding_contract(),
            f"Current Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}",
            f"Saved Stage context: {json.dumps(stage_context or {}, ensure_ascii=False)}",
            f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}",
        ])

    return "\n\n".join([
        f"You are the Kimi K2.6 Sokoban co-creation design peer. Write in {response_language}.",
        opening,
        progress,
        opening_instruction,
        revision_instruction,
        continuity_instruction,
        guidance_mode_instruction,
        action_instruction,
        guidance_instruction,
        f"Draft provenance and attribution:\n{provenance_guidance}",
        post_opening_progress_instruction,
        historical_reference_instruction,
        route,
        metadata,
        safety,
        (
            f"Clarification budget for this Stage: {clarification_count} related question(s) "
            f"have already been asked; at most {clarification_budget} more may be asked before "
            "you should complete safe missing details yourself. This budget never permits you "
            "to guess between ambiguous entities or conflicting map claims."
        ),
        _map_grounding_contract(),
        f"Current Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}",
        "",
        design_context_prompt,
        continuity_context_prompt,
        f"Saved Stage context: {json.dumps(stage_context or {}, ensure_ascii=False)}",
        f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}",
        f"Latest optional play evidence: {json.dumps(play_summary, ensure_ascii=False)}",
        f"Guidance mode: {guidance_mode}.",
    ])


def _llm_solver_evidence(solver_metrics):
    allowed_fields = (
        "valid",
        "solvable",
        "searchedStates",
        "solutionSteps",
        "solutionPushes",
    )
    return {
        field: solver_metrics[field]
        for field in allowed_fields
        if field in solver_metrics
    }


def _proposal_clarification_route_evidence(rows, solver_metrics, stage_context=None):
    """Derive a coordinate-free witness-route summary from server-verified data."""
    metrics = solver_metrics or {}
    summary = {
        key: metrics[key]
        for key in ("solutionSteps", "solutionPushes")
        if isinstance(metrics.get(key), int) and metrics.get(key) >= 0
    }
    solution = str(metrics.get("solution") or "").strip().upper()
    bindings = (stage_context or {}).get("entityBindings") or {}
    if not solution or bindings.get("identityStatus") != "exact":
        return {"mode": "totals_only", **summary} if summary else {"mode": "unavailable"}

    entities = bindings.get("entities") or []
    player_records = [item for item in entities if item.get("kind") == "player"]
    box_records = [item for item in entities if item.get("kind") == "box"]
    target_records = [item for item in entities if item.get("kind") == "target"]
    if len(player_records) != 1 or not box_records or len(box_records) != len(target_records):
        return {"mode": "totals_only", **summary} if summary else {"mode": "unavailable"}

    try:
        normalized = tuple(str(row) for row in rows)
        player = (int(player_records[0]["column"]) - 1, int(player_records[0]["row"]) - 1)
        boxes = {
            (int(item["column"]) - 1, int(item["row"]) - 1): str(item["label"])
            for item in box_records
        }
        targets = {
            (int(item["column"]) - 1, int(item["row"]) - 1): str(item["label"])
            for item in target_records
        }
        directions = {"U": (0, -1), "D": (0, 1), "L": (-1, 0), "R": (1, 0)}
        push_sequence = []
        push_counts = {str(item["label"]): 0 for item in box_records}
        for move in solution:
            if move not in directions:
                raise ValueError("invalid move")
            dx, dy = directions[move]
            destination = (player[0] + dx, player[1] + dy)
            if not (0 <= destination[0] < len(normalized[0]) and 0 <= destination[1] < len(normalized)):
                raise ValueError("move outside map")
            if normalized[destination[1]][destination[0]] in {"#", " ", "@"}:
                raise ValueError("move into blocked tile")
            if destination in boxes:
                box_destination = (destination[0] + dx, destination[1] + dy)
                if not (0 <= box_destination[0] < len(normalized[0]) and 0 <= box_destination[1] < len(normalized)):
                    raise ValueError("push outside map")
                if normalized[box_destination[1]][box_destination[0]] in {"#", " ", "@"} or box_destination in boxes:
                    raise ValueError("invalid push")
                label = boxes.pop(destination)
                boxes[box_destination] = label
                push_sequence.append(label)
                push_counts[label] += 1
            player = destination
        if set(boxes) != set(targets):
            raise ValueError("solution does not finish on targets")
        if "solutionSteps" in summary and summary["solutionSteps"] != len(solution):
            raise ValueError("step count mismatch")
        if "solutionPushes" in summary and summary["solutionPushes"] != len(push_sequence):
            raise ValueError("push count mismatch")

        compressed = []
        for label in push_sequence:
            if compressed and compressed[-1]["box"] == label:
                compressed[-1]["pushes"] += 1
            else:
                compressed.append({"box": label, "pushes": 1})
        return {
            "mode": "verified_route_summary",
            **summary,
            "pushesByBox": push_counts,
            "pushOrder": compressed,
            "boxAlternations": sum(
                left != right for left, right in zip(push_sequence, push_sequence[1:])
            ),
            "targetAssignments": {
                label: targets[position]
                for position, label in boxes.items()
            },
            "analysisContext": {
                "authoritativeRows": list(normalized),
                "legend": {"#": "wall", ".": "floor", "@": "water", "p": "player", "s": "box", "t": "target", " ": "void"},
                "exactEntities": [
                    {
                        "label": str(item.get("label") or ""),
                        "kind": str(item.get("kind") or ""),
                        "row": int(item.get("row")),
                        "column": int(item.get("column")),
                    }
                    for item in entities
                ],
                "verifiedSolutionTrace": solution,
            },
        }
    except (KeyError, TypeError, ValueError, IndexError):
        return {"mode": "totals_only", **summary} if summary else {"mode": "unavailable"}


def _map_facts_for_prompt(rows, stage_context):
    try:
        # Always derive the current entity and tile facts from the exact rows
        # passed to this agent.  A cached stageContext mapFacts object is only
        # allowed to contribute parent-diff annotations after its tileAt
        # snapshot matches; this prevents stale or hand-built context from
        # disagreeing with the numbered map in the same prompt.
        context = stage_context or {}
        facts = build_map_facts(
            rows,
            entity_bindings=context.get("entityBindings"),
        )
        supplied = (stage_context or {}).get("mapFacts")
        if (
            isinstance(supplied, dict)
            and supplied.get("tileAt") == facts.get("tileAt")
            and supplied.get("mapFingerprint") == facts.get("mapFingerprint")
            and supplied.get("entityBindingFingerprint") == facts.get(
                "entityBindingFingerprint"
            )
            and isinstance(supplied.get("verifiedEntityChangesFromParent"), dict)
        ):
            facts["verifiedEntityChangesFromParent"] = supplied[
                "verifiedEntityChangesFromParent"
            ]
    except ValueError:
        # Production Stages are validated before reaching the model.  Keeping this
        # fallback lets isolated prompt/timeout tests use intentionally incomplete grids.
        facts = {
            "available": False,
            "reason": "The supplied rows are not a complete validated Stage.",
        }
    return json.dumps(facts, ensure_ascii=False, separators=(",", ":"))


def _stage_snapshot_for_prompt(rows, stage_context):
    """Serialize exactly one server-derived current Stage snapshot.

    ``stage_context`` is an internal compatibility envelope and may contain
    stale parent annotations.  Those annotations are intentionally ignored for
    the map payload; the current rows and the current binding are rebuilt here.
    """
    context = stage_context or {}
    try:
        snapshot = build_stage_snapshot(
            rows,
            version_id=context.get("versionId"),
            stage_number=context.get("stageNumber"),
            entity_bindings=context.get("entityBindings"),
        )
        ordered_entities = sorted(
            snapshot.get("entities") or [],
            key=lambda item: (
                {"player": 0, "box": 1, "target": 2}.get(item.get("kind"), 9),
                item.get("id") or "",
            ),
        )
        snapshot["canonicalEntityTable"] = [
            (
                f"{item.get('id')} = {item.get('kind')} at row {item.get('row')}, "
                f"column {item.get('column')} (identity exact)"
            )
            if item.get("identityConfidence") == "exact" and item.get("id")
            else (
                f"{item.get('kind')} at row {item.get('row')}, column {item.get('column')} "
                "(identity unknown; do not use a historical label as a hard constraint)"
            )
            for item in ordered_entities
        ]
        # The UI may still display deterministic B1/B2 labels for an
        # ambiguous historical Stage, but the LLM must not mistake those
        # presentation labels for trusted cross-Stage identities.  Keep the
        # coordinates as neutral facts and redact labels/opaque IDs wherever
        # identity confidence is not exact.
        for collection_name in ("entities", "boxes", "targets"):
            for item in snapshot.get(collection_name) or []:
                if item.get("identityConfidence") != "exact":
                    item["id"] = None
                    item["entityId"] = None
        player = snapshot.get("player")
        if isinstance(player, dict) and player.get("identityConfidence") != "exact":
            player["id"] = None
            player["entityId"] = None
        claim_check = context.get("userMapClaims")
        if isinstance(claim_check, dict):
            snapshot["userMapClaimCheck"] = {
                "conflicts": [
                    {
                        key: value
                        for key, value in item.items()
                        if key != "sourceText"
                    }
                    for item in claim_check.get("conflicts", [])
                    if isinstance(item, dict)
                ],
                "instruction": (
                    "Correct conflicting present-tense user map claims in ordinary prose; "
                    "do not treat them as design constraints or proposal coordinates."
                ),
            }
    except (TypeError, ValueError):
        return json.dumps(
            {
                "available": False,
                "reason": "The supplied current Stage is not structurally complete.",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def _canonical_entity_table(rows, stage_context=None):
    """Render the server-owned entity binding in a stable prompt format."""
    try:
        facts = json.loads(_map_facts_for_prompt(rows, stage_context or {}))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "Authoritative entities: unavailable; use neutral coordinate descriptions."
    records = list(facts.get("entities") or [])
    order = {"player": 0, "box": 1, "target": 2}
    records.sort(key=lambda item: (order.get(item.get("kind"), 9), item.get("id") or ""))
    lines = [
        "Authoritative entities (server binding; never renumber from visual position):"
    ]
    for item in records:
        label = item.get("id")
        kind = item.get("kind") or "entity"
        row = item.get("row")
        column = item.get("column")
        confidence = item.get("identityConfidence") or "unknown"
        if confidence == "exact" and label:
            lines.append(
                f"{label} = {kind} at row {row}, column {column} (identity exact)"
            )
        else:
            lines.append(
                f"{kind} at row {row}, column {column} (identity unknown; "
                "do not use a historical B/T label as a hard constraint)"
            )
    if not records:
        lines.append("No stable entity binding is available; use neutral coordinates only.")
    return "\n".join(lines)


def _prompt_stage_context(rows, stage_context):
    """Expose only a consistent current snapshot in the chat prompt context."""
    context = dict(stage_context or {})
    # Map facts are sent through Current Stage Snapshot exactly once.  Keep
    # only non-map routing/memory fields in this compatibility context object.
    context.pop("mapFacts", None)
    context.pop("stageSnapshot", None)
    context.pop("entityBindings", None)
    context.pop("entityBindingFingerprint", None)
    context.pop("beforeRows", None)
    context.pop("afterRows", None)
    context.pop("diff", None)
    context.pop("changeSummary", None)
    # A present-tense user map claim is an untrusted input that has already
    # been checked by the application.  It must not be echoed beside the
    # authoritative snapshot, especially when it contains the wrong B/T
    # coordinate that prompted the check.
    context.pop("userMapClaims", None)

    # Keep continuity semantic.  A previous proposal's exact execution brief
    # is a contract for its own card, not a second current-map authority.  The
    # current snapshot is rebuilt below; only the short design rationale is
    # useful to ordinary chat.  Card-action instructions receive their source
    # proposal through the dedicated action instruction, so the raw contract
    # does not need to be serialized again here.
    recent = context.get("recentGuidance")
    if isinstance(recent, dict):
        projected_recent = {
            key: recent.get(key)
            for key in (
                "discussionFocus",
                "discussionFocusHistory",
                "intentHypothesis",
                "activeDisagreement",
                "uiCues",
            )
            if key in recent
        }
        proposal = recent.get("proposalOffer")
        if isinstance(proposal, dict):
            projected_recent["proposalOffer"] = {
                key: proposal.get(key)
                for key in ("summary", "rationale")
                if proposal.get(key) is not None
            }
        context["recentGuidance"] = projected_recent

    for key in (
        "sourceProposalOffer",
        "challengeContext",
        "challengeRevision",
        "alternativeRevision",
        "authorizedExecutionBrief",
        "proposalBindingFrozen",
        "deterministicExactExecution",
    ):
        context.pop(key, None)
    try:
        context["mapFingerprint"] = map_fingerprint(rows)
    except (TypeError, ValueError):
        context.pop("mapFingerprint", None)
    return context


def _current_user_prompt_messages(conversation):
    """Keep only the current user turn in the LLM transcript.

    Prior assistant prose is not an authority for map facts. DesignContext and
    the current StageSnapshot carry the safe continuity needed by the model;
    replaying historical assistant text here would give stale coordinates and
    entity labels a second, competing source of truth.
    """
    latest = next(
        (
            {
                "role": "user",
                "content": str(message.get("content") or ""),
            }
            for message in reversed(conversation or [])
            if message.get("role") == "user"
            and str(message.get("content") or "").strip()
        ),
        None,
    )
    return [latest] if latest is not None else []


def _design_context_prompt(stage_context, role="chat"):
    context = stage_context or {}
    if role == "revision":
        memory = context.get("revisionDesignContext") or {}
        rules = (
            "This is the minimal Revision projection. Only active explicit/confirmed goals, "
            "constraints, confirmed decisions, relevant rejections, and open questions are "
            "execution context. Inferred intent is deliberately excluded from hard constraints. "
            "Do not reconstruct intent from chat history."
        )
    elif role == "evaluator":
        memory = context.get("evaluatorDesignContext") or context.get("designContext") or {}
        rules = (
            "Separate user-authored goals from confirmed decisions, AI inferences, and actual "
            "map facts. Never turn an inference into a user goal or a warning without evidence."
        )
    else:
        memory = context.get("designContext") or {}
        rules = (
            "This is server-owned semantic memory inherited from the parent Stage. explicit is "
            "the user's own statement, confirmed is a user-confirmed decision, and inferred is "
            "only a tentative AI reading. Never promote inferred to confirmed, do not repeat a "
            "rejected direction, and invite correction when an explicit goal changes."
        )
    return (
        "DesignContext (shared semantic memory; provenance is authoritative):\n"
        f"{rules}\n"
        f"{json.dumps(memory, ensure_ascii=False, separators=(',', ':'))}"
    )


def _continuity_context_prompt(stage_context, role="chat"):
    """Render the compact cross-turn and cross-Stage context explicitly."""
    context = stage_context or {}
    progress = context.get("progressContext") or {}
    if role == "revision":
        progress = {
            "currentStage": progress.get("currentStage"),
            "confirmedDecisions": progress.get("confirmedDecisions", []),
            "unresolvedQuestions": progress.get("unresolvedQuestions", []),
            "rejectedDecisions": progress.get("rejectedDecisions", []),
        }
        rule = (
            "Use confirmed decisions and unresolved questions as constraints for the authorized "
            "revision only. Never turn a question or an inferred reading into an execution requirement."
        )
    else:
        rule = (
            "Use this as living conversation context: connect the latest user message to relevant "
            "confirmed decisions and unresolved questions, while treating model readings as tentative."
        )
    return (
        "Continuous progress context (server-assembled; do not quote this block or expose its labels):\n"
        f"{rule}\n"
        f"{json.dumps(progress, ensure_ascii=False, separators=(',', ':'))}"
    )


def _editable_focus_facts(rows, focus):
    if not isinstance(focus, dict):
        return json.dumps({"focus": None, "cells": []}, ensure_ascii=False)
    center_row = int(focus.get("row", 0))
    center_column = int(focus.get("column", 0))
    radius = int(focus.get("radius", 0))
    allowed_destinations = {
        ".": ["#", "@", "p", "s", "t"],
        "#": ["."],
        "@": ["."],
        "p": ["."],
        "s": ["."],
        "t": ["."],
    }
    cells = []
    for row_index, row in enumerate(rows, start=1):
        for column_index, tile in enumerate(row, start=1):
            if tile == " " or max(
                abs(center_row - row_index), abs(center_column - column_index)
            ) > radius:
                continue
            cells.append({
                "row": row_index,
                "column": column_index,
                "tile": tile,
                "allowedTo": allowed_destinations.get(tile, []),
            })
    return json.dumps(
        {
            "focus": {"row": center_row, "column": center_column, "radius": radius},
            "cells": cells,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _map_grounding_contract():
    return (
        "Map-grounding rule: Deterministic Map Facts are authoritative. Use B1/B2, T1/T2, "
        "P, or one-based (row,column) when identifying a particular entity. Do not invent "
        "a current entity position or relation from a vague visual impression. In particular, "
        "do not call a box/player/target upper-right, lower-left, near another entity, beside "
        "water, or in a narrow passage unless the facts explicitly support that exact current "
        "claim. gridDistance is Manhattan distance only, not a traversable route or a push "
        "solution. If the facts do not establish a relation, state it as a question about a "
        "future play moment rather than as a fact about the saved map. The JSON field names "
        "in Deterministic Map Facts are prompt-only; never repeat them in assistantMessage, "
        "assessment, guidance, or any other user-visible text."
    )


def _latest_user_text(conversation):
    return next(
        (
            str(message.get("content") or "").strip()
            for message in reversed(conversation or [])
            if message.get("role") == "user"
            and str(message.get("content") or "").strip()
        ),
        "",
    )


def _has_historical_stage_reference(text):
    lowered = str(text or "").casefold()
    return any(
        marker in lowered
        for marker in (
            "第一版",
            "原版",
            "之前的版本",
            "上一版",
            "旧版本",
            "旧版",
            "之前的stage",
            "上一个stage",
            "first version",
            "original version",
            "previous version",
            "prior version",
            "earlier version",
            "previous stage",
            "prior stage",
            "earlier stage",
        )
    )


def _historical_stage_reference_instruction(conversation):
    if not _has_historical_stage_reference(_latest_user_text(conversation)):
        return ""
    return (
        "Historical Stage reference rule: the designer is referring to an older version. "
        "The numbered map and Deterministic Map Facts describe only the current saved Stage. "
        "Discuss the older version conceptually unless a historical snapshot is explicitly "
        "supplied; do not invent, copy, or transfer its coordinates or entity positions into "
        "current-map claims, and do not treat that reflection as an edit authorization."
    )


def _contains_unverified_historical_map_claim(text):
    coordinate = re.compile(
        r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]"
    )
    entity_position = re.compile(
        r"\b(?:P|B\d+|T\d+)\b.{0,28}?(?:at|in|on|located|position|coordinate)|"
        r"(?:玩家|箱子|目标|起点|B\d+|T\d+|P).{0,18}?(?:位于|坐标|在)"
    , flags=re.IGNORECASE)
    for sentence in re.split(r"(?<=[.!?。！？])\s*", str(text or "")):
        if _has_historical_stage_reference(sentence) and (
            coordinate.search(sentence) or entity_position.search(sentence)
        ):
            return True
    return False


def _validate_named_entity_relations(text, facts):
    """Validate only explicit present-tense relations involving named entities."""
    records = {
        item.get("id"): item
        for item in facts.get("entities") or []
        if item.get("id") and item.get("identityConfidence") == "exact"
    }
    if not records:
        return

    def point(label):
        item = records.get(label)
        return (item.get("row"), item.get("column")) if item else None

    relation = re.compile(
        r"(?:\bnear\b|\bclose\s+to\b|\badjacent\s+to\b|\bbeside\b|"
        r"\bnext\s+to\b|\u9760\u8fd1|\u76f8\u90bb|\u65c1\u8fb9|\u7d27\u6328)",
        flags=re.IGNORECASE,
    )
    future = re.compile(
        r"(?:\bif\b|\bwould\b|\bwill\b|\bmove(?:s|d|ing)?\b|\bpush(?:es|ed|ing)?\b|"
        r"\btoward(?:s)?\b|\bfrom\b|\u5982\u679c|\u82e5|\u5c06|\u4f1a|\u79fb\u52a8|"
        r"\u63a8\u5230|\u63a8\u5411)",
        flags=re.IGNORECASE,
    )
    labels = re.compile(
        r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+)(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )
    for sentence in re.split(r"(?<=[.!?\u3002\uff01\uff1f])|[\r\n]+", str(text or "")):
        if not relation.search(sentence) or future.search(sentence):
            continue
        named = list(dict.fromkeys(match.group(0).upper() for match in labels.finditer(sentence)))
        if len(named) >= 2:
            first, second = named[0], named[1]
            first_point, second_point = point(first), point(second)
            if first_point and second_point:
                distance = abs(first_point[0] - second_point[0]) + abs(first_point[1] - second_point[1])
                if distance > 2:
                    raise ValueError(
                        f"The reply claims {first} is close to {second}, but the saved map does not support that relation."
                    )
        if re.search(
            r"(?:\b(?:B\d+)\b).{0,28}(?:\b(?:water)\b|\u6c34\u57df|\u6c34\u8fb9)",
            sentence,
            flags=re.IGNORECASE,
        ):
            for label in named:
                item = records.get(label)
                if item and item.get("kind") == "box":
                    adjacent = any(
                        cell["row"] == item["row"] + row_delta
                        and cell["column"] == item["column"] + column_delta
                        for cell in facts.get("waterCells") or []
                        for row_delta, column_delta in ((-1, 0), (1, 0), (0, -1), (0, 1))
                    )
                    if not adjacent:
                        raise ValueError(
                            f"The reply claims {label} is beside water, but the saved map does not support that relation."
                        )


def _validate_map_grounding_texts(
    texts,
    rows,
    *,
    historical_reference=False,
    entity_bindings=None,
):
    """Reject a small set of high-impact, checkable spatial hallucinations.

    Natural prose is intentionally not parsed wholesale.  This guard covers the claims that
    most often made the visible feedback misleading: assigning an entity to a wrong corner,
    saying that a box currently touches water, and saying that a current box/player is close
    to a target when no such pair exists.  The prompt remains the primary grounding layer.
    """
    text = "\n".join(str(value or "") for value in texts).casefold()
    if not text:
        return

    if historical_reference and _contains_unverified_historical_map_claim(text):
        raise ValueError(
            "The reply makes a concrete historical Stage map claim without a supplied historical snapshot."
        )

    _validate_coordinate_claims(text, rows, entity_bindings=entity_bindings)

    try:
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
    except ValueError:
        return
    _validate_named_entity_relations(text, facts)
    height = len(rows)
    width = len(rows[0]) if rows else 0
    entities = {
        "box": facts["boxes"],
        "target": facts["targets"],
        "player": [facts["player"]],
    }
    regions = (
        ("upper_right", ("右上角", "右上", "upper-right", "upper right"),
         lambda item: item["row"] <= (height + 1) // 2 and item["column"] > width // 2),
        ("upper_left", ("左上角", "左上", "upper-left", "upper left"),
         lambda item: item["row"] <= (height + 1) // 2 and item["column"] <= width // 2),
        ("lower_right", ("右下角", "右下", "lower-right", "lower right"),
         lambda item: item["row"] > (height + 1) // 2 and item["column"] > width // 2),
        ("lower_left", ("左下角", "左下", "lower-left", "lower left"),
         lambda item: item["row"] > (height + 1) // 2 and item["column"] <= width // 2),
    )
    entity_patterns = {
        "box": r"(?:箱子|箱|box(?:es)?|crates?)",
        "target": r"(?:目标点|目标|终点|targets?|goals?)",
        "player": r"(?:玩家起点|玩家|起点|players?)",
    }

    for entity_name, pattern in entity_patterns.items():
        for _, labels, in_region in regions:
            if any(in_region(item) for item in entities[entity_name]):
                continue
            for label in labels:
                escaped = re.escape(label)
                before = (
                    rf"{escaped}(?:的)?(?:那(?:个)?|这(?:个)?|一(?:个)?)?\s*{pattern}"
                )
                after = (
                    rf"{pattern}(?:在|位于|处在|挪到(?:了)?|移到(?:了)?|到了|"
                    rf"in|at|is in)?\s*{escaped}"
                )
                if re.search(before, text) or re.search(after, text):
                    raise ValueError(
                        f"The reply assigns a {entity_name} to {label}, which conflicts with deterministic map facts."
                    )

    if not any(box["orthogonallyAdjacentToWater"] for box in facts["boxes"]):
        current_box_water_patterns = (
            r"(?:现在|当前).{0,18}(?:箱子|箱|它).{0,12}(?:贴着|紧贴|紧邻|挨着|靠着)(?:水|水边|水域)",
            r"(?:箱子|箱).{0,16}(?:贴着|紧贴|紧邻|挨着|靠着)(?:水|水边|水域)",
            r"(?:box(?:es)?|crate).{0,28}(?:next to|adjacent to|beside|against).{0,18}water",
        )
        if any(re.search(pattern, text) for pattern in current_box_water_patterns):
            raise ValueError(
                "The reply claims that a current box touches water, which conflicts with deterministic map facts."
            )

    target_positions = facts["targets"]
    close_box_target = any(
        abs(box["row"] - target["row"]) + abs(box["column"] - target["column"]) <= 2
        for box in facts["boxes"] for target in target_positions
    )
    close_player_target = any(
        abs(facts["player"]["row"] - target["row"])
        + abs(facts["player"]["column"] - target["column"]) <= 2
        for target in target_positions
    )
    closeness = r"(?:很近|靠近|紧挨|相邻|close to|near|adjacent to)"
    if not close_box_target and (
        re.search(rf"(?:箱子|箱).{{0,18}}(?:目标点|目标|终点).{{0,18}}{closeness}", text)
        or re.search(rf"(?:目标点|目标|终点).{{0,18}}(?:箱子|箱).{{0,18}}{closeness}", text)
    ):
        raise ValueError(
            "The reply claims that a current box is close to a target, which conflicts with deterministic map facts."
        )
    if not close_player_target and (
        re.search(rf"(?:玩家|起点).{{0,18}}(?:目标点|目标|终点).{{0,18}}{closeness}", text)
        or re.search(rf"(?:目标点|目标|终点).{{0,18}}(?:玩家|起点).{{0,18}}{closeness}", text)
    ):
        raise ValueError(
            "The reply claims that the current player is close to a target, which conflicts with deterministic map facts."
        )


def _strip_invalid_stage_grounding_sentences(
    text,
    rows,
    *,
    historical_reference=False,
    entity_bindings=None,
):
    """Drop only checkable map-claim sentences that contradict this Stage.

    Stage openings are explanatory prose, not execution contracts.  A single
    hallucinated coordinate should therefore not erase every useful observation
    in a fallback response.  Formal proposals continue to use the strict whole-
    payload validation paths.
    """
    if not text or not rows:
        return str(text or "").strip(), []

    sentences = re.split(
        r"\n{2,}|(?<=[.!?。！？])(?:[ \t]+|(?=[\u3400-\u9fff]))",
        str(text),
    )
    kept = []
    removed = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        try:
            _validate_map_grounding_texts(
                [sentence],
                rows,
                historical_reference=historical_reference,
                entity_bindings=entity_bindings,
            )
        except ValueError as exception:
            removed.append({"text": sentence, "reason": str(exception)[:300]})
            continue
        kept.append(sentence)

    if not removed:
        return str(text or "").strip(), removed
    return "\n\n".join(kept).strip(), removed


def _strip_invalid_grounding_sentences(
    text,
    rows,
    *,
    historical_reference=False,
    entity_bindings=None,
):
    """Shared sentence-level recovery for non-executable visible replies."""
    return _strip_invalid_stage_grounding_sentences(
        text,
        rows,
        historical_reference=historical_reference,
        entity_bindings=entity_bindings,
    )


def _sanitize_ordinary_grounding_metadata(
    guidance,
    rows,
    *,
    historical_reference=False,
    entity_bindings=None,
):
    """Keep a useful ordinary reply when optional model metadata is not grounded.

    A normal design discussion is not an edit contract. Its visible body is validated on
    its own, while optional cards/questions are discarded individually when they contain a
    stale coordinate or spatial claim. This prevents one bad Kimi tag from replacing the
    whole conversation with an edit-oriented clarification.
    """
    sanitized = dict(guidance or {})

    def valid_text(value):
        if not value:
            return True
        try:
            _validate_map_grounding_texts(
                [value],
                rows,
                historical_reference=historical_reference,
                entity_bindings=entity_bindings,
            )
        except ValueError:
            return False
        return True

    for field in ("followUpQuestion", "intentHypothesis"):
        if not valid_text(sanitized.get(field)):
            sanitized[field] = None

    offer = sanitized.get("proposalOffer")
    if isinstance(offer, dict):
        offer_values = [offer.get("summary"), offer.get("rationale")]
        execution_brief = offer.get("executionBrief")
        if execution_brief:
            offer_values.append(json.dumps(execution_brief, ensure_ascii=False))
        if not all(valid_text(value) for value in offer_values):
            sanitized["proposalOffer"] = None
            if sanitized.get("move") == "offer_revision":
                sanitized["move"] = "offer_perspective"

    disagreement = sanitized.get("disagreement")
    if isinstance(disagreement, dict):
        fields = (
            "userPosition",
            "aiPosition",
            "coreDisagreement",
            "nextQuestion",
        )
        if not all(valid_text(disagreement.get(field)) for field in fields):
            sanitized["disagreement"] = None

    cues = []
    for cue in sanitized.get("uiCues") or []:
        if not isinstance(cue, dict) or valid_text(cue.get("text")):
            cues.append(cue)
    sanitized["uiCues"] = cues[:2]
    return sanitized


def _chat_validation_mode(
    conversation,
    stage_context=None,
    *,
    stage_opening=False,
    guidance_mode=None,
):
    """Classify how strictly current-map claims should affect a chat response."""
    if stage_opening:
        return "stage_opening"

    context = stage_context or {}
    explicit_action = context.get("explicitAction") or "none"
    if explicit_action in {
        "execute_revision",
        "challenge_revision",
        "alternative_revision",
    }:
        return "edit_request"
    if guidance_mode == "revision_advice":
        return "edit_request"
    if context.get("revisionRequestState") in {
        "authorized",
        "authorized_relaxed",
        "needs_direction",
    }:
        return "edit_request"

    latest = _latest_user_text(conversation).casefold()
    route_markers = (
        "route",
        "path",
        "corridor",
        "路线",
        "路径",
        "通道",
        "绕路",
        "推到",
        "移动",
        "从",
        "到",
    )
    if any(marker in latest for marker in route_markers):
        return "route_discussion"
    return "ordinary_chat"


def _tile_from_claim(value):
    value = str(value or "").casefold()
    return {
        "#": "#", "墙": "#", "墙体": "#", "wall": "#",
        ".": ".", "地板": ".", "空地": ".", "floor": ".", "ground": ".",
        "@": "@", "水": "@", "水域": "@", "water": "@",
        "p": "p", "玩家": "p", "起点": "p", "player": "p", "start": "p",
        "s": "s", "箱": "s", "箱子": "s", "box": "s", "crate": "s",
        "t": "t", "目标": "t", "目标点": "t", "target": "t", "goal": "t",
    }.get(value)


def _validate_coordinate_claims(text, rows, *, entity_bindings=None):
    """Check explicit coordinate/tile statements before they reach a proposal card."""
    if not rows:
        return
    claim_pattern = re.compile(
        r"\(\s*(\d+)\s*[,，]\s*(\d+)\s*\)\s*"
        r"(?:是|为|属于|is|was|contains|contains the|has)\s*"
        r"(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|"
        r"wall|floor|ground|water|box|crate|target|goal|player|start|#|\.|@|p|s|t)",
        flags=re.IGNORECASE,
    )
    transition_pattern = re.compile(
        r"\(\s*(\d+)\s*[,，]\s*(\d+)\s*\)\s*"
        r".{0,18}?(?:从|由|from)\s*(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|"
        r"wall|floor|ground|water|box|crate|target|goal|player|start|#|\.|@|p|s|t)\s*"
        r"(?:变成|改成|换成|变为|to|into|becomes?)\s*"
        r"(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|"
        r"wall|floor|ground|water|box|crate|target|goal|player|start|#|\.|@|p|s|t)",
        flags=re.IGNORECASE,
    )
    height = len(rows)
    for match in claim_pattern.finditer(text):
        if _entity_claim_is_non_current(text, match.start(), match.end()):
            continue
        row, column = int(match.group(1)), int(match.group(2))
        expected = _tile_from_claim(match.group(3))
        if not (1 <= row <= height and 1 <= column <= len(rows[row - 1])):
            raise ValueError("The reply refers to a coordinate outside the saved Stage.")
        actual = rows[row - 1][column - 1]
        if expected is not None and actual != expected:
            raise ValueError(
                f"The reply claims row {row}, column {column} is {expected!r}, "
                f"but deterministic map facts say it is {actual!r}."
            )
    for match in transition_pattern.finditer(text):
        row, column = int(match.group(1)), int(match.group(2))
        before = _tile_from_claim(match.group(3))
        after = _tile_from_claim(match.group(4))
        if not (1 <= row <= height and 1 <= column <= len(rows[row - 1])):
            raise ValueError("The reply refers to a coordinate outside the saved Stage.")
        actual = rows[row - 1][column - 1]
        if before is not None and actual != before:
            raise ValueError(
                f"The reply claims row {row}, column {column} starts as {before!r}, "
                f"but deterministic map facts say it is {actual!r}."
            )

    _validate_entity_coordinate_claims(text, rows, entity_bindings=entity_bindings)


def _map_entity_coordinates(rows, entity_bindings=None):
    """Return the stable entity labels used by the map-facts contract."""
    facts = build_map_facts(rows, entity_bindings=entity_bindings)
    result = {item["id"]: item for item in facts.get("entities") or []}
    if "P" not in result:
        result["P"] = facts["player"]
    return {
        key: {"row": value["row"], "column": value["column"]}
        for key, value in result.items()
    }


def _entity_claim_is_non_current(text, start, end):
    """Ignore future or hypothetical entity coordinates during grounding."""
    window = str(text or "")[max(0, start - 36):end]
    return bool(re.search(
        r"(?:\bif\b|\bwhen\b|\bwould\b|\bwill\b|\bmove(?:s|d|ing)?\b|"
        r"\bpush(?:es|ed|ing)?\b|\bto\b|\btoward(?:s)?\b|\bfrom\b|"
        r"\u5982\u679c|\u82e5|\u5047\u8bbe|\u5c06|\u4f1a|\u79fb\u52a8|\u63a8\u5230|"
        r"\u63a8\u5411|\u6539\u5230|\u53d8\u6210)",
        window,
        flags=re.IGNORECASE,
    ))


def _entity_coordinate_claims(text, rows):
    """Yield explicit entity-to-coordinate claims in either writing order."""
    coordinate = r"[\(\uFF08]\s*(\d{1,2})\s*[,，]\s*(\d{1,2})\s*[\)\uFF09]"
    # ``\b`` does not create a boundary between an ASCII entity id and a
    # Chinese character, so ``B1在(6,3)`` used to evade validation.  Boundary
    # only against ASCII identifier characters while allowing natural Chinese
    # prose to touch the id.
    entity = r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+|玩家|起点)(?![A-Za-z0-9])"
    marker = (
        r"(?:at|located\s+at|is\s+at|in|on|位于|在|处于|坐落于|起点为)"
    )
    patterns = (
        re.compile(
            rf"(?P<entity>{entity}).{{0,24}}?{marker}\s*{coordinate}",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"{coordinate}.{{0,24}}?{marker}\s*(?P<entity>{entity})",
            flags=re.IGNORECASE,
        ),
    )
    # The original compact matcher is retained for legacy wording.  These
    # explicit forms cover the common English/Chinese current-fact statements
    # without treating a future route or an ``if`` clause as a saved fact.
    stable_entity = (
        r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+|"
        r"\u73a9\u5bb6|\u8d77\u70b9)(?![A-Za-z0-9])"
    )
    current_marker = re.compile(
        r"(?:\bis\b|\bwas\b|\boccup(?:y|ies|ied)\b|\bsits?\b|"
        r"\blocated\b|\bpositioned\b|\bplaced\b|"
        r"\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|\u5904\u5728|\u662f)",
        flags=re.IGNORECASE,
    )
    future_marker = re.compile(
        r"(?:\bif\b|\bwould\b|\bwill\b|\bmove(?:s|d|ing)?\b|"
        r"\bpush(?:es|ed|ing)?\b|\bto\b|\btoward(?:s)?\b|\bfrom\b|"
        r"\u5982\u679c|\u82e5|\u5c06|\u4f1a|\u79fb\u52a8|\u63a8\u5230|\u63a8\u5411|"
        r"\u6539\u5230|\u53d8\u6210)",
        flags=re.IGNORECASE,
    )
    stable_patterns = (
        re.compile(
            rf"(?P<entity>{stable_entity})(?P<between>.{{0,60}}?)"
            rf"[\(\uFF08]\s*(?P<row>\d{{1,2}})\s*[,，]\s*(?P<column>\d{{1,2}})\s*[\)\uFF09]",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"(?P<entity>{stable_entity})(?P<between>.{{0,60}}?)"
            rf"(?:row|line)\s*(?P<row>\d{{1,2}})\s*,?\s*(?:column|col)\s*(?P<column>\d{{1,2}})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"(?P<entity>{stable_entity})(?P<between>.{{0,60}}?)"
            rf"\u7b2c\s*(?P<row>\d{{1,2}})\s*\u884c\s*\u7b2c\s*(?P<column>\d{{1,2}})\s*\u5217",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"[\(\uFF08]\s*(?P<row>\d{{1,2}})\s*[,，]\s*(?P<column>\d{{1,2}})\s*[\)\uFF09]"
            rf"(?P<between>.{{0,60}}?)(?P<entity>{stable_entity})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"(?:row|line)\s*(?P<row>\d{{1,2}})\s*,?\s*(?:column|col)\s*(?P<column>\d{{1,2}})"
            rf"(?P<between>.{{0,60}}?)(?P<entity>{stable_entity})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"\u7b2c\s*(?P<row>\d{{1,2}})\s*\u884c\s*\u7b2c\s*(?P<column>\d{{1,2}})\s*\u5217"
            rf"(?P<between>.{{0,60}}?)(?P<entity>{stable_entity})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"\u7b2c\s*(?P<row>\d{{1,2}})\s*\u884c\s*\u7b2c\s*(?P<column>\d{{1,2}})\s*\u5217"
            rf"(?P<between>.{{0,60}}?)(?:\u662f|\u4e3a|\u5c5e\u4e8e)\s*(?P<entity>{stable_entity})",
            flags=re.IGNORECASE,
        ),
    )
    seen_claims = set()
    for stable_pattern in stable_patterns:
        for match in stable_pattern.finditer(str(text or "")):
            between = match.group("between")
            if not current_marker.search(between) or future_marker.search(between):
                continue
            entity_name = match.group("entity").upper()
            if entity_name in {"玩家", "起点"}:
                entity_name = "P"
            claim = (
                entity_name,
                int(match.group("row")),
                int(match.group("column")),
            )
            if claim in seen_claims:
                continue
            seen_claims.add(claim)
            yield claim[0], {"row": claim[1], "column": claim[2]}

    for pattern in patterns:
        for match in pattern.finditer(str(text or "")):
            if _entity_claim_is_non_current(str(text or ""), match.start(), match.end()):
                continue
            entity_name = match.group("entity").upper()
            if entity_name in {"玩家", "起点"}:
                entity_name = "P"
            coordinate_match = re.search(coordinate, match.group(0))
            if coordinate_match is None:
                continue
            yield entity_name, {
                "row": int(coordinate_match.group(1)),
                "column": int(coordinate_match.group(2)),
            }


def _validate_entity_coordinate_claims(text, rows, *, entity_bindings=None):
    try:
        expected = _map_entity_coordinates(rows, entity_bindings)
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
        uncertain = {
            item.get("id")
            for item in facts.get("entities") or []
            if item.get("id") and item.get("identityConfidence") != "exact"
        }
    except (TypeError, ValueError, KeyError):
        return
    for entity_name, claimed in _entity_coordinate_claims(text, rows):
        if entity_name in {"\u73a9\u5bb6", "\u8d77\u70b9"}:
            entity_name = "P"
        if entity_name in uncertain:
            raise ValueError(
                f"The saved Stage cannot verify the identity of {entity_name}; use a coordinate instead."
            )
        actual = expected.get(entity_name)
        if actual is None:
            raise ValueError(f"The reply refers to an unknown map entity {entity_name}.")
        if actual != claimed:
            raise ValueError(
                f"The reply places {entity_name} at row {claimed['row']}, column "
                f"{claimed['column']}, but deterministic map facts place it at "
                f"row {actual['row']}, column {actual['column']}."
            )


def generate_stage_assessment(
    conversation,
    rows,
    language,
    solver_metrics,
    play_summary,
    request_id,
    stage_context=None,
):
    deadline = _request_deadline()
    try:
        return generate_chat_reply(
            conversation,
            rows,
            request_id,
            language=language,
            solver_metrics=solver_metrics,
            play_summary=play_summary,
            assessment_only=True,
            stage_context=stage_context,
            # Keep the primary retries in the structured, snapshot-bound
            # contract.  A later bounded prose recovery is independently
            # revalidated against the same snapshot.
            _max_attempts=2,
            _deadline=deadline,
        )
    except LLMServiceError as exception:
        if exception.code not in {
            "MODEL_EMPTY_RESPONSE",
            "MODEL_RESPONSE_INVALID",
            "UPSTREAM_TIMEOUT",
            "UPSTREAM_CONNECTION_ERROR",
        }:
            raise

        can_attempt_plain_recovery = (
            _remaining_until(deadline) >= MIN_RETRY_BUDGET_SECONDS
        )
        _log_llm_event(
            (
                "llm_stage_opening_structured_failed"
                if can_attempt_plain_recovery
                else "llm_stage_opening_fallback"
            ),
            requestId=request_id,
            fromCode=exception.code,
            responseMode=(
                "plain_text_recovery"
                if can_attempt_plain_recovery
                else "server_snapshot"
            ),
            remainingSeconds=round(_remaining_until(deadline), 3),
            fallbackReason=exception.code,
        )
        # Formatting-only structured failures should not immediately turn a
        # usable opening into mechanical server copy.  A single bounded prose
        # recovery is still snapshot-grounded and cannot authorize map edits.
        if can_attempt_plain_recovery:
            try:
                recovered = _generate_plain_chat_sync(
                    conversation=conversation,
                    rows=rows,
                    request_id=request_id,
                    language=language,
                    solver_metrics=solver_metrics,
                    play_summary=play_summary,
                    stage_context=stage_context,
                    stage_opening=True,
                    deadline=deadline,
                    max_attempts=1,
                )
                _log_llm_event(
                    (
                        "llm_stage_opening_recovered"
                        if recovered.model != "kimi-k2.6-safe-opening"
                        else "llm_stage_opening_recovery_exhausted"
                    ),
                    requestId=request_id,
                    fromCode=exception.code,
                    responseMode=(
                        "plain_text"
                        if recovered.model != "kimi-k2.6-safe-opening"
                        else "server_snapshot"
                    ),
                    finalDisplayMode=(
                        "plain_text_recovery"
                        if recovered.model != "kimi-k2.6-safe-opening"
                        else "server_snapshot"
                    ),
                    bodyPreserved=bool(recovered.assistant_message.strip()),
                    droppedSentenceCount=0,
                    remainingSeconds=round(_remaining_until(deadline), 3),
                )
                return recovered
            except LLMServiceError as recovery_exception:
                _log_llm_event(
                    "llm_stage_opening_recovery_failed",
                    requestId=request_id,
                    fromCode=exception.code,
                    recoveryCode=recovery_exception.code,
                    remainingSeconds=round(_remaining_until(deadline), 3),
                )
        _log_llm_event(
            "llm_stage_opening_fallback",
            requestId=request_id,
            fromCode=exception.code,
            responseMode="server_snapshot",
            remainingSeconds=round(_remaining_until(deadline), 3),
            fallbackReason=exception.code,
        )
        return _stage_opening_safe_execution(
            language=language,
            rows=rows,
            request_id=request_id,
            attempts_used=exception.attempts_used,
            started_at=deadline - LLM_INTERNAL_DEADLINE_SECONDS,
            fallback_reason=exception.code,
            stage_context=stage_context,
            solver_metrics=solver_metrics,
        )


def generate_chat_reply(
    conversation,
    rows,
    request_id,
    language="en",
    solver_metrics=None,
    play_summary=None,
    assessment_only=False,
    proposal_validator=None,
    stage_context=None,
    _max_attempts=CHAT_MAX_ATTEMPTS,
    _deadline=None,
):
    request_started_at = time.monotonic() if _deadline is None else None
    deadline = _deadline or _request_deadline(request_started_at)
    effective_stage_context = dict(stage_context or {})
    explicit_action = effective_stage_context.get("explicitAction") or "none"

    # The web server marks a validated exact contract as deterministic.  This
    # path intentionally runs before API-key/model setup: the model is not
    # allowed to reinterpret a frozen coordinate transition.
    if (
        explicit_action == "execute_revision"
        and effective_stage_context.get("deterministicExactExecution")
        and isinstance(effective_stage_context.get("authorizedExecutionBrief"), dict)
    ):
        try:
            exact = _deterministic_exact_revision(
                rows,
                effective_stage_context["authorizedExecutionBrief"],
                request_id,
                language,
                proposal_validator,
                effective_stage_context.get("entityBindings"),
            )
        except LLMServiceError:
            raise
        except Exception as exception:
            error = LLMServiceError(
                "PROPOSAL_SEARCH_EXHAUSTED",
                "The frozen execution contract could not produce a verified proposal.",
                request_id,
                False,
                0,
                422,
            )
            error.proposal_diagnostics = {
                "source": "deterministic_contract",
                "category": "exact_contract_execution",
            }
            raise error from exception
        if exact is not None:
            return exact

    api_key, base_url = _llm_credentials()

    if not api_key or api_key in {
        "your_kimi_api_key_here",
        "your_llm_api_key_here",
    }:
        raise LLMServiceError(
            "CONFIGURATION_ERROR",
            "The configured LLM API key is missing.",
            request_id,
            False,
            0,
            503,
        )

    revision_state, revision_brief = _classify_revision_request(
        conversation,
        stage_context,
    )
    effective_stage_context["responseLanguage"] = language

    if explicit_action == "execute_revision":
        revision_state = "authorized"
        source_offer = effective_stage_context.get("sourceProposalOffer") or {}
        revision_brief = " ".join(
            str(source_offer.get(field) or "").strip()
            for field in ("summary", "rationale")
        ).strip() or revision_brief
        if source_offer.get("executionBrief") and not effective_stage_context.get(
            "proposalBindingFrozen"
        ):
            effective_stage_context["authorizedExecutionBrief"] = source_offer[
                "executionBrief"
            ]
    elif explicit_action == "challenge_revision":
        revision_state, revision_brief = "not_request", None
    elif explicit_action == "alternative_revision":
        source_offer = effective_stage_context.get("sourceProposalOffer") or {}
        revision_state = "proposal_requested"
        revision_brief = " ".join(
            str(source_offer.get(field) or "").strip()
            for field in ("summary", "rationale")
        ).strip() or revision_brief

    if explicit_action == "alternative_revision":
        cited_offer = effective_stage_context.get("sourceProposalOffer") or {}
        cited_summary = str(cited_offer.get("summary") or "").strip()
        cited_rationale = str(cited_offer.get("rationale") or "").strip()
        effective_stage_context["alternativeRevisionBrief"] = (
            "Generate one materially different proposal for the same designer direction. "
            f"The cited proposal was {cited_summary!r}: {cited_rationale!r}. "
            "Do not reuse its exact required transition set; choose a different local treatment "
            "that remains grounded in the current saved Stage."
        )

    if not assessment_only and revision_state != "not_request":
        effective_stage_context["revisionRequestState"] = revision_state
    if revision_brief:
        effective_stage_context["authorizedRevisionBrief"] = revision_brief

    automatic_proposal = effective_stage_context.get("revisionRouting") in {
        "proposal", "proposal_conservative"
    }
    proposal_request = not assessment_only and (
        explicit_action == "execute_revision"
        or explicit_action == "alternative_revision"
        or automatic_proposal
        or (
            not effective_stage_context.get("deferRevisionExecution")
            and (
                revision_state in {"authorized", "authorized_relaxed"}
                # The API has already classified this latest turn against the
                # current StageSnapshot and clarification budget. A designer
                # asking for an actionable proposal must use the constrained
                # RevisionPlan path, not the permissive plain-chat parser.
                or automatic_proposal
            )
        )
    )

    # Proposals need a longer end-to-end budget than ordinary chat, but do not
    # change the timeout used by openings or normal discussion. Keep an
    # explicitly supplied deadline authoritative for tests and callers that
    # already own request budgeting.
    if proposal_request and _deadline is None:
        deadline = _request_deadline(
            request_started_at,
            budget_seconds=PROPOSAL_INTERNAL_DEADLINE_SECONDS,
        )

    if revision_state == "authorized_relaxed":
        effective_stage_context["revisionRelaxed"] = True
        effective_stage_context["relaxationOriginalBrief"] = str(
            (((stage_context or {}).get("recentGuidance") or {}).get("relaxationOffer") or {}).get(
                "originalBrief"
            )
            or ""
        )

    if automatic_proposal and not proposal_request:
        raise LLMServiceError(
            "PROPOSAL_ROUTING_INVARIANT",
            "A ready automatic proposal cannot enter the plain-chat path.",
            request_id,
            False,
            0,
            500,
        )

    if not assessment_only and not proposal_request:
        return _generate_plain_chat_sync(
            conversation=conversation,
            rows=rows,
            request_id=request_id,
            language=language,
            solver_metrics=solver_metrics,
            play_summary=play_summary,
            stage_context=effective_stage_context,
            deadline=deadline,
        )

    if proposal_request:
        return _generate_revision_search_proposal_sync(
            api_key=api_key,
            base_url=base_url,
            conversation=conversation,
            rows=rows,
            request_id=request_id,
            language=language,
            proposal_validator=proposal_validator,
            stage_context=effective_stage_context,
            baseline_metrics=solver_metrics,
            deadline=deadline,
        )

    messages = build_chat_messages(
        conversation,
        rows,
        language,
        solver_metrics,
        play_summary,
        assessment_only,
        effective_stage_context,
    )
    models = _unified_model_attempts(_max_attempts)
    primary_model = models[0]

    task = "stage_assessment" if assessment_only else (
        "map_proposal" if proposal_request else "chat"
    )
    max_tokens = PROPOSAL_MAX_TOKENS if proposal_request else CHAT_MAX_TOKENS
    started_at = time.monotonic()
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task=task,
        primaryModel=primary_model,
        fallbackModel=None,
        timeoutSeconds=CHAT_TIMEOUT_SECONDS,
        responseMode="json_object",
    )

    try:
        return asyncio.run(
            asyncio.wait_for(
                _generate_with_model_fallback(
                    api_key=api_key,
                    base_url=base_url,
                    models=models,
                    messages=messages,
                    rows=rows,
                    max_tokens=max_tokens,
                    request_id=request_id,
                    task=task,
                    language=language,
                    assessment_only=assessment_only,
                    proposal_validator=proposal_validator,
                    stage_context=effective_stage_context,
                    started_at=started_at,
                    deadline=deadline,
                    max_attempts=_max_attempts,
                ),
                timeout=min(CHAT_TIMEOUT_SECONDS, _remaining_until(deadline)),
            )
        )
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task=task,
            outcome="error",
            code="UPSTREAM_TIMEOUT",
            attemptsUsed=min(len(models), _max_attempts),
            latencyMs=elapsed_ms,
            responseMode="json_object",
        )
        raise LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not complete the request before the 120 second limit.",
            request_id,
            True,
            min(len(models), _max_attempts),
            504,
        ) from exception


class ObjectiveEvidenceError(ValueError):
    def __init__(self, evidence):
        super().__init__(
            "soft objective evidence missing: "
            + ", ".join(evidence.get("missing") or ["no verified mechanism delta"])
        )
        self.evidence = evidence


class HardObjectiveError(ValueError):
    def __init__(self, metric, direction, before, after, minimum_delta, verifiable=True):
        self.metric = metric
        self.direction = direction
        self.before = before
        self.after = after
        self.minimum_delta = minimum_delta
        self.verifiable = verifiable
        if verifiable:
            message = (
                f"metric goal {metric} {direction} was not met "
                f"(baseline={before}, candidate={after}, minimumDelta={minimum_delta})"
            )
        else:
            message = f"metric goal {metric} cannot be verified"
        super().__init__(message)


def _proposal_objective_policy(conversation, stage_context):
    """Derive metric authority from designer text; model metrics are never hard by default."""
    user_text = " ".join(
        str(item.get("content") or "")
        for item in (conversation or [])[-12:]
        if item.get("role") == "user"
    )
    brief = str((stage_context or {}).get("authorizedRevisionBrief") or "")
    text = " ".join((brief, user_text)).strip()
    lowered = text.casefold()
    mandatory_pattern = re.compile(
        r"(?:必须|至少|不得少于|务必|minimum|at least|must|required)"
    )
    clauses = [
        item.strip()
        for item in re.split(r"[。！？!?；;\n]+", lowered)
        if item.strip()
    ]

    def requested_delta(keywords):
        matching = [
            clause
            for clause in clauses
            if mandatory_pattern.search(clause)
            and any(word in clause for word in keywords)
        ]
        if not matching:
            return None
        numbers = re.findall(r"\d+", matching[-1])
        return max(1, int(numbers[-1])) if numbers else 1

    hard_metrics = []
    step_delta = requested_delta(("最短解", "最短路径", "最短路线", "solution steps", "shortest solution"))
    push_delta = requested_delta(("最少推动", "最小推动", "推动次数", "推箱次数", "minimum pushes", "push count"))
    if step_delta is not None:
        hard_metrics.append({
            "metric": "solutionSteps",
            "direction": "increase",
            "minimumDelta": step_delta,
            "source": "explicit_user",
        })
    if push_delta is not None:
        hard_metrics.append({
            "metric": "minimumPushes",
            "direction": "increase",
            "minimumDelta": push_delta,
            "source": "explicit_user",
        })

    objective_class = "general"
    if any(word in lowered for word in ("运输", "长距离推", "推箱深度", "transport")):
        objective_class = "longer_transport"
    elif any(word in lowered for word in ("互相牵制", "顺序依赖", "互相阻挡", "先后顺序", "dependency", "interlock")):
        objective_class = "box_dependency"
    elif any(word in lowered for word in ("共享通道", "共享走廊", "局部阻挡", "shared corridor", "shared passage")):
        objective_class = "shared_blocking"
    elif any(word in lowered for word in ("绕行", "更长规划", "花更多时间", "宏观规划", "规划再执行", "detour", "planning")):
        objective_class = "planning_depth"
    elif any(word in lowered for word in ("空间压迫", "路线选择", "扩展探索", "space pressure", "route choice")):
        objective_class = "space_route_choice"

    exact_labels = {
        str(item.get("label") or item.get("id") or "").upper()
        for item in ((stage_context or {}).get("entityBindings") or {}).get("entities") or []
        if item.get("identityConfidence") == "exact"
    }
    target_entities = sorted({
        label.upper()
        for label in re.findall(r"\b(?:B\d+|T\d+|P)\b", text, flags=re.IGNORECASE)
        if label.upper() in exact_labels
    })

    return {
        "schemaVersion": 1,
        "hardMetricGoals": hard_metrics,
        "softObjectiveClass": objective_class,
        "targetEntities": target_entities,
        "requiresMechanismEvidence": objective_class != "general",
    }


def _apply_objective_policy_to_plan(plan, policy, preserved_components=None):
    hard_goals = tuple(
        MetricGoal(
            item["metric"],
            item["direction"],
            int(item.get("minimumDelta") or 1),
        )
        for item in policy.get("hardMetricGoals") or []
    )
    # Metric directions emitted by Kimi are hypotheses. Only server-derived,
    # explicit user requirements are allowed to reject an otherwise valid map.
    return replace(plan, strategies=tuple(
        replace(
            strategy,
            metric_goals=hard_goals,
            preserve=frozenset(strategy.preserve).union(preserved_components or ()),
        )
        for strategy in plan.strategies
    ))


def _proposal_route_features(rows, validation, entity_bindings=None):
    metrics = validation.as_dict() if hasattr(validation, "as_dict") else {}
    evidence = _proposal_clarification_route_evidence(
        rows,
        metrics,
        {"entityBindings": entity_bindings or {}},
    )
    try:
        minimum = minimum_pushes(rows)
    except Exception:
        minimum = None
    push_order = evidence.get("pushOrder") or []
    return {
        "solutionSteps": metrics.get("solutionSteps"),
        "solutionPushes": metrics.get("solutionPushes"),
        "minimumPushes": minimum,
        "pushBlocks": len(push_order),
        "boxAlternations": evidence.get("boxAlternations"),
        "pushesByBox": evidence.get("pushesByBox") or {},
        "routeMode": evidence.get("mode"),
        "solution": metrics.get("solution") or "",
    }


def _trace_cells_for_solution(rows, solution):
    player = next(
        ((x, y) for y, row in enumerate(rows) for x, tile in enumerate(row) if tile == "p"),
        None,
    )
    if player is None:
        return set()
    boxes = {(x, y) for y, row in enumerate(rows) for x, tile in enumerate(row) if tile == "s"}
    traced = {player, *boxes}
    for move in str(solution or "").upper():
        delta = {"U": (0, -1), "D": (0, 1), "L": (-1, 0), "R": (1, 0)}.get(move)
        if delta is None:
            return set()
        destination = (player[0] + delta[0], player[1] + delta[1])
        traced.add(destination)
        if destination in boxes:
            box_destination = (destination[0] + delta[0], destination[1] + delta[1])
            boxes.remove(destination)
            boxes.add(box_destination)
            traced.add(box_destination)
        player = destination
    return traced


def _objective_mechanism_evidence(
    base_rows,
    candidate_rows,
    baseline_features,
    candidate_features,
    policy,
):
    changed = {
        (x, y)
        for y, (before_row, after_row) in enumerate(zip(base_rows, candidate_rows))
        for x, (before, after) in enumerate(zip(before_row, after_row))
        if before != after
    }
    trace = _trace_cells_for_solution(candidate_rows, candidate_features.get("solution"))
    route_affected = any(
        abs(cx - tx) + abs(cy - ty) <= 1
        for cx, cy in changed
        for tx, ty in trace
    )
    deltas = {}
    for key in ("solutionSteps", "solutionPushes", "minimumPushes", "pushBlocks", "boxAlternations"):
        before = baseline_features.get(key)
        after = candidate_features.get(key)
        if isinstance(before, int) and isinstance(after, int):
            deltas[key] = after - before
    both_boxes = sum(
        value > 0 for value in (candidate_features.get("pushesByBox") or {}).values()
    ) >= 2
    objective = policy.get("softObjectiveClass") or "general"
    passed = True
    missing = []
    if objective == "longer_transport":
        target_boxes = [
            item for item in policy.get("targetEntities") or []
            if str(item).startswith("B")
        ]
        if target_boxes:
            before_pushes = baseline_features.get("pushesByBox") or {}
            after_pushes = candidate_features.get("pushesByBox") or {}
            passed = any(
                int(after_pushes.get(label) or 0) > int(before_pushes.get(label) or 0)
                for label in target_boxes
            )
        else:
            passed = any(
                deltas.get(key, 0) > 0
                for key in ("minimumPushes", "solutionPushes", "pushBlocks")
            )
        missing = [] if passed else ["longer_transport_delta"]
    elif objective == "box_dependency":
        passed = both_boxes and route_affected and any(
            deltas.get(key, 0) > 0 for key in ("pushBlocks", "boxAlternations")
        )
        missing = [] if passed else ["box_dependency_delta"]
    elif objective == "shared_blocking":
        passed = both_boxes and route_affected and any(
            deltas.get(key, 0) != 0 for key in ("pushBlocks", "boxAlternations", "minimumPushes")
        )
        missing = [] if passed else ["shared_blocking_route_effect"]
    elif objective == "planning_depth":
        passed = route_affected and any(
            deltas.get(key, 0) > 0
            for key in ("solutionSteps", "minimumPushes", "pushBlocks", "boxAlternations")
        )
        missing = [] if passed else ["planning_depth_delta"]
    elif objective == "space_route_choice":
        structural_change = any(
            base_rows[y][x] in {".", "#", "@"}
            and candidate_rows[y][x] in {".", "#", "@"}
            for x, y in changed
        )
        passed = route_affected and structural_change
        missing = [] if passed else ["route_relevant_space_change"]
    return {
        "passed": bool(passed),
        "objectiveClass": objective,
        "routeAffected": route_affected,
        "bothBoxesUsed": both_boxes,
        "metricDeltas": deltas,
        "missing": missing,
    }


def _objective_validating_proposal_validator(
    proposal_validator,
    base_rows,
    baseline_validation,
    policy,
    entity_bindings,
    evidence_by_fingerprint,
):
    baseline_features = _proposal_route_features(
        base_rows,
        baseline_validation,
        entity_bindings,
    )

    def validate(candidate_rows):
        validation = (
            proposal_validator(candidate_rows)
            if proposal_validator is not None
            else validate_and_solve(candidate_rows)
        )
        features = _proposal_route_features(candidate_rows, validation, entity_bindings)
        evidence = _objective_mechanism_evidence(
            base_rows,
            candidate_rows,
            baseline_features,
            features,
            policy,
        )
        evidence_by_fingerprint[map_fingerprint(candidate_rows)] = evidence
        if policy.get("requiresMechanismEvidence") and not evidence["passed"]:
            raise ObjectiveEvidenceError(evidence)
        return validation

    return validate


def _attempt_semantic_revision_replan(
    *,
    api_key,
    base_url,
    model,
    original_messages,
    rows,
    request_id,
    language,
    stage_context,
    objective_policy,
    objective_validator,
    baseline_metrics,
    movement_requirement,
    preserved_components,
    started_at,
    deadline,
    first_failure,
    evidence_by_fingerprint,
    excluded_map_fingerprints=None,
):
    if _remaining_until(deadline) < MIN_RETRY_BUDGET_SECONDS:
        raise first_failure
    messages = [dict(item) for item in original_messages]
    failure_summary = json.dumps(
        (getattr(first_failure, "proposal_diagnostics", {}) or {}).get("rejectionRecords", [])[-3:],
        ensure_ascii=False,
        separators=(",", ":"),
    )[:1000]
    messages[0]["content"] += (
        "\n\nThe previous semantic plan produced no admissible candidate. Create one materially "
        "different local strategy while preserving the authorized direction, explicit anchors, "
        "prohibitions, and objectivePolicy. Change focus, effect, or allowed operators as needed. "
        "Do not repeat the previous concrete treatment and keep requiredTransitions empty unless "
        "the supplied execution brief itself froze exact transitions. Previous safe rejection summary: "
        + failure_summary
    )
    replan_deadline = min(deadline, time.monotonic() + PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS)
    plan, plan_attempts, _ = asyncio.run(asyncio.wait_for(
        _compile_revision_plan(
            api_key=api_key,
            base_url=base_url,
            models=[model],
            messages=messages,
            request_id=request_id,
            started_at=started_at,
            deadline=replan_deadline,
        ),
        timeout=max(0.001, _remaining_until(replan_deadline)),
    ))
    plan = _apply_objective_policy_to_plan(
        plan,
        objective_policy,
        preserved_components,
    )
    validate_revision_plan_against_map(
        rows,
        plan,
        stage_context.get("entityBindings"),
    )
    _validate_revision_plan_entities(plan, rows, stage_context.get("entityBindings"))
    contract = _build_revision_execution_contract(
        plan,
        stage_context.get("authorizedRevisionBrief") or "",
        stage_context,
    )
    contract["objectivePolicy"] = objective_policy
    operation_messages = _build_map_operation_messages(
        contract,
        rows,
        language,
        stage_context,
        baseline_metrics,
    )
    try:
        result = asyncio.run(asyncio.wait_for(
            _generate_map_operation_candidates(
                api_key=api_key,
                base_url=base_url,
                models=[model],
                messages=operation_messages,
                base_rows=rows,
                request_id=request_id,
                language=language,
                proposal_validator=objective_validator,
                revision_contract=contract,
                baseline_metrics=baseline_metrics,
                started_at=started_at,
                deadline=deadline,
                candidate_evidence=evidence_by_fingerprint,
                excluded_map_fingerprints=excluded_map_fingerprints,
            ),
            timeout=max(0.001, _remaining_until(deadline)),
        ))
    except LLMServiceError as operation_error:
        if operation_error.code != "PROPOSAL_SEARCH_EXHAUSTED":
            raise
        result = _deterministic_revision_fallback(
            rows=rows,
            plan=plan,
            revision_contract=contract,
            request_id=request_id,
            language=language,
            proposal_validator=objective_validator,
            baseline_metrics=baseline_metrics,
            movement_requirement=movement_requirement,
            preserved_components=preserved_components,
            entity_bindings=stage_context.get("entityBindings"),
            started_at=started_at,
            deadline=deadline,
            excluded_map_fingerprints=excluded_map_fingerprints,
        )
    diagnostics = dict(result.proposal_diagnostics or {})
    diagnostics.update({"replanAttempted": True, "replanSucceeded": True})
    return replace(result, proposal_diagnostics=diagnostics), plan, contract, plan_attempts


def _generate_revision_search_proposal_sync(
    *,
    api_key,
    base_url,
    conversation,
    rows,
    request_id,
    language,
    proposal_validator,
    stage_context,
    baseline_metrics=None,
    deadline=None,
):
    started_at = time.monotonic()
    deadline = deadline or _request_deadline(started_at)
    objective_policy = _proposal_objective_policy(conversation, stage_context)
    stage_context = dict(stage_context or {})
    stage_context["objectivePolicy"] = objective_policy
    excluded_candidate_fingerprints = {
        str(stage_context.get("excludedProposalCandidateFingerprint") or "").strip()
    } - {""}
    baseline_metrics = dict(baseline_metrics or {})
    if "minimumPushes" not in baseline_metrics:
        try:
            baseline_metrics["minimumPushes"] = minimum_pushes(rows)
        except Exception:
            baseline_metrics["minimumPushes"] = None
    models = _unified_model_attempts(PROPOSAL_GENERATION_ATTEMPTS)
    movement_requirement = _authorized_movement_requirement(conversation)
    preserved_components = _authorized_preserved_components(
        conversation,
        stage_context,
    )
    messages = _build_revision_plan_messages(
        conversation,
        rows,
        language,
        stage_context,
        movement_requirement,
        preserved_components,
    )
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task="revision_plan",
        primaryModel=models[0],
        fallbackModel=None,
        timeoutSeconds=PROPOSAL_REQUEST_TIMEOUT_SECONDS,
        internalDeadlineSeconds=PROPOSAL_INTERNAL_DEADLINE_SECONDS,
        revisionPlanPhaseSeconds=PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS,
        revisionPlanTokenLimit=PROPOSAL_PLAN_MAX_COMPLETION_TOKENS,
        operationAttemptSeconds=PROPOSAL_OPERATION_ATTEMPT_TIMEOUT_SECONDS,
        deterministicSearchSeconds=PROPOSAL_SEARCH_DEADLINE_SECONDS,
        responseMode="revision_plan",
    )
    try:
        plan, attempts_used, model = asyncio.run(
            asyncio.wait_for(
                _compile_revision_plan(
                    api_key=api_key,
                    base_url=base_url,
                    models=models,
                    messages=messages,
                    request_id=request_id,
                    started_at=started_at,
                    deadline=min(
                        deadline,
                        started_at + PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS,
                    ),
                ),
                timeout=min(
                    PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS,
                    _remaining_until(deadline),
                ),
            )
        )
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="revision_plan",
            outcome="error",
            code="UPSTREAM_TIMEOUT",
            attemptsUsed=PROPOSAL_GENERATION_ATTEMPTS,
            latencyMs=elapsed_ms,
            responseMode="revision_plan",
        )
        raise LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not compile the revision plan before the proposal time limit.",
            request_id,
            True,
            PROPOSAL_GENERATION_ATTEMPTS,
            504,
        ) from exception
    except LLMServiceError as exception:
        if exception.code in {
            "MODEL_RESPONSE_INVALID",
            "MODEL_EMPTY_RESPONSE",
            "MODEL_LOW_QUALITY_RESPONSE",
        }:
            exception.code = "REVISION_PLAN_INVALID"
            exception.safe_message = (
                "Kimi did not return a complete valid RevisionPlan after the bounded repair attempt."
            )
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="revision_plan",
            outcome="error",
            code=exception.code,
            attemptsUsed=exception.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="revision_plan",
            **_provider_error_fields(exception),
        )
        raise

    plan = _bind_execution_brief_to_plan(
        plan,
        stage_context.get("authorizedExecutionBrief") if stage_context else None,
    )
    plan = _apply_objective_policy_to_plan(
        plan,
        objective_policy,
        preserved_components,
    )
    try:
        revision_contract = _build_revision_execution_contract(
            plan,
            stage_context.get("authorizedRevisionBrief") if stage_context else "",
            stage_context,
        )
        revision_contract["objectivePolicy"] = objective_policy
    except ValueError as exception:
        error = LLMServiceError(
            "REVISION_CONTRACT_CONFLICT",
            str(exception),
            request_id,
            False,
            attempts_used,
            422,
        )
        error.revision_plan = plan.as_dict()
        error.revision_contract = {}
        error.proposal_diagnostics = {"category": "contract_conflict"}
        raise error from exception
    try:
        validate_revision_plan_against_map(
            rows,
            plan,
            (stage_context or {}).get("entityBindings"),
        )
        _validate_revision_plan_entities(
            plan,
            rows,
            (stage_context or {}).get("entityBindings"),
        )
    except ValueError as exception:
        error = LLMServiceError(
            "REVISION_CONTRACT_CONFLICT",
            str(exception),
            request_id,
            False,
            attempts_used,
            422,
        )
        error.revision_plan = plan.as_dict()
        error.revision_contract = revision_contract
        error.proposal_diagnostics = {"category": "coordinate_or_contract_conflict"}
        raise error from exception
    evidence_by_fingerprint = {}
    baseline_validation = validate_and_solve(rows)
    objective_validator = _objective_validating_proposal_validator(
        proposal_validator,
        rows,
        baseline_validation,
        objective_policy,
        stage_context.get("entityBindings"),
        evidence_by_fingerprint,
    )
    exact_strategies = [
        item for item in revision_contract.get("strategies") or []
        if item.get("requiredTransitions")
    ]
    can_execute_exactly = (
        exact_strategies
        and len(exact_strategies) == len(revision_contract.get("strategies") or [])
        and (
            exact_strategies[0].get("effect")
            not in {"relocate_start", "relocate_box", "relocate_target"}
            or bool(stage_context.get("entityBindings"))
        )
    )
    if can_execute_exactly:
        strategy = exact_strategies[0]
        exact_brief = {
            "schemaVersion": 1,
            "effect": strategy.get("effect"),
            "anchors": strategy.get("anchorEntities") or [],
            "focus": strategy.get("focus"),
            "requiredTransitions": strategy.get("requiredTransitions") or [],
            "allowedOperators": strategy.get("allowedOperators") or [],
            "preserve": strategy.get("preserve") or [],
            "playObjective": strategy.get("playObjective"),
        }
        try:
            exact = _deterministic_exact_revision(
                rows,
                exact_brief,
                request_id,
                language,
                objective_validator,
                stage_context.get("entityBindings"),
            )
        except Exception as exception:
            error = LLMServiceError(
                "EXACT_TRANSITION_INFEASIBLE",
                "The frozen tile transitions could not produce a valid solvable proposal.",
                request_id,
                False,
                attempts_used,
                422,
            )
            error.revision_plan = plan.as_dict()
            error.revision_contract = revision_contract
            error.proposal_diagnostics = {
                "category": "exact_transition_infeasible",
                "reason": _safe_validation_reason(exception),
            }
            raise error from exception
        if exact is not None:
            exact_fingerprint = map_fingerprint(exact.proposed_rows or [])
            if exact_fingerprint in excluded_candidate_fingerprints:
                error = LLMServiceError(
                    "CANDIDATE_DUPLICATED",
                    "The exact alternative repeats the candidate stored in the cited purple card.",
                    request_id,
                    False,
                    attempts_used,
                    422,
                )
                error.revision_plan = plan.as_dict()
                error.revision_contract = revision_contract
                error.proposal_diagnostics = {
                    "category": "candidate_duplicated",
                    "excludedCandidateCount": len(excluded_candidate_fingerprints),
                }
                raise error
            evidence = evidence_by_fingerprint.get(exact_fingerprint)
            return replace(
                exact,
                attempts_used=attempts_used,
                revision_plan=plan.as_dict(),
                revision_contract=revision_contract,
                proposal_diagnostics={
                    **(exact.proposal_diagnostics or {}),
                    "selectedStrategyIndex": 1,
                    "planAttempts": attempts_used,
                    "modifierAttempts": 0,
                    "objectivePolicy": objective_policy,
                    "mechanismEvidence": evidence,
                },
            )
    remaining_seconds = _remaining_until(deadline)
    if remaining_seconds <= 0:
        error = LLMServiceError(
            "GLOBAL_PROPOSAL_TIMEOUT",
            "The complete proposal pipeline exhausted its global time budget.",
            request_id,
            True,
            attempts_used,
            504,
        )
        error.revision_plan = plan.as_dict()
        error.revision_contract = revision_contract
        raise error

    operation_messages = _build_map_operation_messages(
        revision_contract,
        rows,
        language,
        stage_context,
        baseline_metrics,
    )
    try:
        operation_result = asyncio.run(
            asyncio.wait_for(
                _generate_map_operation_candidates(
                    api_key=api_key,
                    base_url=base_url,
                    models=models,
                    messages=operation_messages,
                    base_rows=rows,
                    request_id=request_id,
                    language=language,
                    proposal_validator=objective_validator,
                    revision_contract=revision_contract,
                    baseline_metrics=baseline_metrics,
                    started_at=started_at,
                    deadline=deadline,
                    candidate_evidence=evidence_by_fingerprint,
                    excluded_map_fingerprints=excluded_candidate_fingerprints,
                ),
                timeout=remaining_seconds,
            )
        )
    except asyncio.TimeoutError as exception:
        exhausted_global_budget = _remaining_until(deadline) <= 0.05
        error = LLMServiceError(
            "GLOBAL_PROPOSAL_TIMEOUT" if exhausted_global_budget else "UPSTREAM_TIMEOUT",
            (
                "The complete proposal pipeline exhausted its global time budget."
                if exhausted_global_budget
                else "Kimi did not create executable revision operations before the operation-phase time limit."
            ),
            request_id,
            True,
            attempts_used + PROPOSAL_GENERATION_ATTEMPTS,
            504,
        )
        error.revision_plan = plan.as_dict()
        error.revision_contract = revision_contract
        raise error from exception
    except LLMServiceError as exception:
        exception.attempts_used += attempts_used
        exception.revision_plan = plan.as_dict()
        exception.revision_contract = revision_contract
        if exception.code != "PROPOSAL_SEARCH_EXHAUSTED":
            raise
        fallback = None
        replan_failure = None
        semantic_contract = not any(
            item.get("requiredTransitions")
            for item in revision_contract.get("strategies") or []
        )
        if semantic_contract:
            try:
                fallback, plan, revision_contract, replan_attempts = (
                    _attempt_semantic_revision_replan(
                        api_key=api_key,
                        base_url=base_url,
                        model=models[0],
                        original_messages=messages,
                        rows=rows,
                        request_id=request_id,
                        language=language,
                        stage_context=stage_context,
                        objective_policy=objective_policy,
                        objective_validator=objective_validator,
                        baseline_metrics=baseline_metrics,
                        movement_requirement=movement_requirement,
                        preserved_components=preserved_components,
                        started_at=started_at,
                        deadline=deadline,
                        first_failure=exception,
                        evidence_by_fingerprint=evidence_by_fingerprint,
                        excluded_map_fingerprints=excluded_candidate_fingerprints,
                    )
                )
                attempts_used += replan_attempts
            except Exception as replan_exception:
                replan_failure = _safe_validation_reason(replan_exception)
        try:
            if fallback is None:
                fallback = _deterministic_revision_fallback(
                    rows=rows,
                    plan=plan,
                    revision_contract=revision_contract,
                    request_id=request_id,
                    language=language,
                    proposal_validator=objective_validator,
                    baseline_metrics=baseline_metrics,
                    movement_requirement=movement_requirement,
                    preserved_components=preserved_components,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                    started_at=started_at,
                    deadline=deadline,
                    excluded_map_fingerprints=excluded_candidate_fingerprints,
                )
        except ProposalSearchExhausted as search_exception:
            diagnostics = dict(getattr(exception, "proposal_diagnostics", {}) or {})
            diagnostics["deterministicFallback"] = search_exception.diagnostics
            diagnostics["replanAttempted"] = semantic_contract
            diagnostics["replanSucceeded"] = False
            if replan_failure:
                diagnostics["replanFailure"] = replan_failure[:500]
            exception.proposal_diagnostics = diagnostics
            rejection_categories = {
                item.get("category")
                for item in diagnostics.get("rejectionRecords") or []
                if isinstance(item, dict)
            }
            fallback_reasons = set(
                (search_exception.diagnostics.get("failureReasons") or {}).keys()
            )
            if (
                rejection_categories
                and rejection_categories <= {"soft_objective_evidence_missing", "candidate_duplicated"}
                and fallback_reasons <= {"ObjectiveEvidenceError"}
            ):
                exception.code = "SOFT_OBJECTIVE_EVIDENCE_MISSING"
                exception.safe_message = (
                    "Solvable local candidates were found, but none produced verified evidence for the requested play mechanism."
                )
            elif (
                "hard_objective_not_met" in rejection_categories
                or "metric_goal_not_met" in fallback_reasons
            ):
                exception.code = "HARD_OBJECTIVE_NOT_MET"
                exception.safe_message = (
                    "No candidate satisfied the designer's explicit measurable requirement."
                )
            elif (
                rejection_categories
                and rejection_categories <= {"candidate_duplicated"}
                and fallback_reasons <= {"CANDIDATE_DUPLICATED"}
            ):
                exception.code = "CANDIDATE_DUPLICATED"
                exception.safe_message = "Every candidate repeated a previously rejected or cited proposal."
            elif (
                rejection_categories
                and rejection_categories <= {"candidate_unsolvable", "candidate_duplicated"}
                and fallback_reasons <= {"UNSOLVABLE_LEVEL", "CANDIDATE_DUPLICATED"}
            ):
                exception.code = "CANDIDATE_UNSOLVABLE"
                exception.safe_message = "Every distinct candidate failed deterministic solvability validation."
            else:
                exception.code = "DETERMINISTIC_SEARCH_EXHAUSTED"
                exception.safe_message = (
                    "The modifier and bounded deterministic search found no admissible solvable candidate."
                )
            raise exception
        fallback_diagnostics = dict(fallback.proposal_diagnostics or {})
        fallback_diagnostics.setdefault("replanAttempted", semantic_contract)
        fallback_diagnostics.setdefault("replanSucceeded", bool(semantic_contract and not replan_failure))
        fallback_diagnostics["modifierFailure"] = (
            getattr(exception, "proposal_diagnostics", {}) or {}
        ).get("modifierFailure")
        operation_result = replace(
            fallback,
            attempts_used=exception.attempts_used,
            revision_plan=plan.as_dict(),
            revision_contract=revision_contract,
            proposal_diagnostics=fallback_diagnostics,
        )

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    guidance = dict(operation_result.guidance)
    latest_user = _latest_role_content(conversation, "user")
    intent_hypothesis = (
        _replace_echoed_intent_hypothesis(
            _natural_intent_candidate(
                latest_user,
                language,
                _latest_user_explicitly_agrees(latest_user),
                difficulty_reframe=_user_reframes_difficulty_judgment(conversation),
            ),
            latest_user,
            language,
        )
        if _user_states_first_person_view(latest_user)
        else None
    )
    guidance["intentHypothesis"] = intent_hypothesis
    guidance["intentConfidence"] = "medium" if intent_hypothesis else None
    diagnostics = dict(operation_result.proposal_diagnostics)
    selected_evidence = evidence_by_fingerprint.get(
        map_fingerprint(operation_result.proposed_rows or [])
    )
    diagnostics["objectivePolicy"] = objective_policy
    diagnostics["mechanismEvidence"] = selected_evidence
    diagnostics["planAttempts"] = attempts_used
    diagnostics["modifierAttempts"] = operation_result.attempts_used
    diagnostics["revisionContract"] = revision_contract
    _log_llm_event(
        "llm_request_completed",
        requestId=request_id,
        task="map_proposal",
        outcome="success",
        model=operation_result.model,
        attemptsUsed=attempts_used + operation_result.attempts_used,
        latencyMs=elapsed_ms,
        responseMode="two_agent_revision",
        changedCellCount=diagnostics.get("changedCellCount"),
        candidateCount=diagnostics.get("candidateCount"),
    )
    return replace(
        operation_result,
        attempts_used=attempts_used + operation_result.attempts_used,
        latency_ms=elapsed_ms,
        guidance=guidance,
        revision_plan=plan.as_dict(),
        revision_contract=revision_contract,
        proposal_diagnostics=diagnostics,
    )


def _bind_execution_brief_to_plan(plan, execution_brief):
    """Make explicit card coordinates survive the semantic-agent handoff."""
    if not isinstance(execution_brief, dict):
        return plan
    transitions = tuple(
        (
            item["row"],
            item["column"],
            item["from"],
            item["to"],
        )
        for item in execution_brief.get("requiredTransitions") or []
    )
    transition_entities = tuple(
        item.get("anchorEntity")
        for item in execution_brief.get("requiredTransitions") or []
    )
    if not transitions and not execution_brief.get("effect"):
        return plan
    first = plan.strategies[0]
    focus_payload = execution_brief.get("focus")
    focus = first.focus
    if isinstance(focus_payload, dict):
        focus = Focus(
            focus_payload["row"],
            focus_payload["column"],
            focus_payload["radius"],
        )
    operators = tuple(execution_brief.get("allowedOperators") or first.operators)
    preserve = frozenset(
        set(execution_brief.get("preserve") or first.preserve)
        | {"outer_shell", "unrelated_areas"}
    )
    minimum = max(1, len(transitions))
    if transitions and any(
        (before, after) in {
            ("p", "."), (".", "p"), ("s", "."), (".", "s"),
            ("t", "."), (".", "t"),
        }
        for _, _, before, after in transitions
    ):
        minimum = max(2, minimum)
    edit_budget = max(first.edit_budget, minimum)
    bound_first = replace(
        first,
        effect=execution_brief.get("effect") or first.effect,
        focus=focus,
        operators=operators,
        preserve=preserve,
        edit_budget=min(12, edit_budget),
        required_transitions=transitions,
        required_transition_entities=transition_entities,
        anchor_entities=tuple(execution_brief.get("anchors") or first.anchor_entities),
        play_objective=execution_brief.get("playObjective") or first.play_objective,
    )
    # Once the server has frozen exact transitions, alternatives from the
    # semantic planner are not additional permission to edit other cells.
    return replace(plan, strategies=(bound_first,))


def _require_exact_revision_plan(plan):
    """Require every executable strategy to name its complete cell diff."""
    if not plan.strategies or any(
        not strategy.required_transitions for strategy in plan.strategies
    ):
        raise ValueError(
            "RevisionPlan must contain non-empty requiredTransitions for every strategy."
        )
    return plan


def _validate_revision_plan_entities(plan, rows, entity_bindings=None):
    """Apply the same current-stage identity rules to the planner output."""
    if entity_bindings is None:
        return
    for index, strategy in enumerate(plan.strategies, start=1):
        data = strategy.as_dict()
        brief = {
            "schemaVersion": 1,
            "effect": data.get("effect"),
            "anchors": data.get("anchorEntities") or [],
            "focus": data.get("focus"),
            "requiredTransitions": data.get("requiredTransitions") or [],
            "allowedOperators": data.get("operators") or [],
            "preserve": data.get("preserve") or [],
            "playObjective": data.get("playObjective"),
        }
        try:
            _validate_execution_brief(brief, rows, entity_bindings)
        except (TypeError, ValueError) as exception:
            raise ValueError(
                f"RevisionPlan strategy {index} is not bound to the current entity snapshot: {exception}"
            ) from exception


def _build_revision_plan_messages(
    conversation,
    rows,
    language,
    stage_context,
    movement_requirement=None,
    preserved_components=None,
):
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    revision_brief = str(stage_context.get("authorizedRevisionBrief") or "").strip()
    alternative_brief = str(
        stage_context.get("alternativeRevisionBrief") or ""
    ).strip()
    execution_brief = stage_context.get("authorizedExecutionBrief") or {}
    original_brief = str(stage_context.get("relaxationOriginalBrief") or "").strip()
    relaxation_rule = (
        "This is a designer-approved fallback. Preserve the original core direction, but one "
        "coherent local effect is sufficient; do not weaken solvability, the outer shell, explicit "
        "prohibitions, or protection of unrelated areas."
        if stage_context.get("revisionRelaxed")
        else "Do not weaken or reinterpret the authorized direction."
    )
    conservative_binding_rule = (
        "The designer confirmed the experience direction but did not name the exact map objects. "
        "Choose exactly one existing entity combination or one uniquely identifiable connected region from "
        "the current Stage Snapshot. Prefer the smallest local change, preserve every unrelated component, "
        "and treat that binding only as this reviewable candidate, never as a confirmed designer instruction."
        if stage_context.get("proposalBindingMode") == "conservative"
        else "If the direction is under-specified, do not guess; the caller will ask the designer for clarification."
    )
    movement_rule = _movement_requirement_prompt(movement_requirement)
    preservation_rule = _preserved_components_prompt(preserved_components)
    map_facts = _stage_snapshot_for_prompt(rows, stage_context)
    continuity_context = _continuity_context_prompt(stage_context, role="revision")
    # The modifier handoff must not receive a transcript or raw user claims.
    # The semantic Chat handoff has already distilled the authorized direction
    # into the brief and DesignContext projection.
    edit_facts = ""
    numbered_map = ""
    system_prompt = (
        "You are the Sokoban co-creation revision-planning assistant. Compile a designer-authorized semantic RevisionPlan "
        "for one saved 10-row × 12-column Stage into a detailed, executable RevisionPlan. Resolve the intended "
        "change into exact one-based coordinates and before/after tile states using only the authoritative "
        "map facts below. Do not generate map rows or tile operations. Do not generate full map rows; do not return map rows or tile operations; a separate level revision assistant may realize "
        "the frozen transitions, while the application owns all cell changes, structural "
        "validation, and solvability. Preserve the authorized direction and every explicit "
        "prohibition. Treat unmentioned areas as protected. Return JSON only with exactly one key, strategies, "
        "containing one or two objects. Every strategy has exactly: effect, focus, operators, "
        "preserve, editBudget, metricGoals, requiredTransitions, anchorEntities, and playObjective. "
        "effect is one of open_route, narrow_route, "
        "adjust_internal_walls, relocate_start, relocate_box, relocate_target, reshape_water, "
        "change_box_order. focus is null or {row,column,radius}; coordinates are one-based, row "
        "1..10, column 1..12, radius 1..3. operators contains one to three distinct values from "
        "add_wall, remove_wall, move_player, move_box, move_target, add_water, remove_water. "
        "preserve contains distinct values from outer_shell, player, boxes, targets, water, "
        "walls, unrelated_areas. Never list an operator that edits a preserved component. "
        "Each strategy must include requiredTransitions, anchorEntities (P, B1, B2, T1, T2), and "
        "playObjective. requiredTransitions must be the exact supplied transitions only when the "
        "structured execution brief already contains them; otherwise return an empty list so the "
        "modifier can explore concrete cells inside focus and the allowed operators. Never invent a "
        "hard coordinate binding from a qualitative experience goal. " + conservative_binding_rule + " "
        "focus must contain every required transition. editBudget is an integer 1..12; a single "
        "structural tile change may use budget 1, while moving a player, box, or target requires "
        "two paired cells. Set the budget to the smallest honest upper bound, never a range that "
        "cannot be satisfied. "
        "entity. metricGoals must be an empty list. The server separately derives hard measurable "
        "requirements from explicit designer wording; searchedStates is never a player-experience goal. "
        "Always preserve outer_shell and unrelated_areas. Choose a concrete focus for a local "
        "request, select operators that can realize the effect, and use metricGoals when the "
        "designer clearly requests a measurable change. The first strategy is preferred and any "
        "second strategy is a strict alternative, not permission to weaken the request. Do not output analysis, "
        "thinking, explanations, or markdown: emit the complete compact JSON object immediately. "
         "The current Stage Snapshot below is the only map source. Never use a coordinate from "
         "conversation text or an older Stage. "
        + _map_grounding_contract() + " "
        f"Interpret conversation in {response_language}."
    )
    user_prompt = (
        f"{_design_context_prompt(stage_context, role='revision')}\n\n"
        f"{continuity_context}\n\n"
        f"Authorized revision brief: {revision_brief!r}. "
        f"Alternative proposal constraint: {alternative_brief!r}. "
        f"Structured execution brief (authoritative when present): "
        f"{json.dumps(execution_brief, ensure_ascii=False, separators=(',', ':'))}. "
        f"Server objective policy (authoritative): {json.dumps(stage_context.get('objectivePolicy') or {}, ensure_ascii=False, separators=(',', ':'))}. "
        f"Original pre-fallback brief: {original_brief!r}. {relaxation_rule} {movement_rule} "
        f"{preservation_rule}\n\n"
        "Column ruler (one-based): 123456789012\n"
        f"Map snapshot fingerprint for this planning pass: {map_fingerprint(rows)}\n"
        f"Current Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}\n"
        ""
        f"Authorized direction (no chat transcript): {revision_brief!r}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _authorized_movement_requirement(conversation):
    messages = list(conversation or [])
    user_indexes = [
        index for index, message in enumerate(messages)
        if message.get("role") == "user"
    ]
    if not user_indexes:
        return None
    latest_user_index = user_indexes[-1]
    latest_user = str(messages[latest_user_index].get("content") or "")
    if not latest_user or not _is_explicit_revision_authorization(latest_user):
        return None
    direct_requirement = _movement_requirement_from_text(latest_user)
    if direct_requirement is not None:
        return direct_requirement
    direction_source = _latest_role_content(messages[:latest_user_index], "assistant")
    if not direction_source:
        return None
    return _movement_requirement_from_text(direction_source)


def _movement_requirement_from_text(value):
    source = str(value or "")
    lowered = source.casefold()
    if not source:
        return None
    if re.search(r"(?:不要|别|不(?:要|能)?|do not|don't)\s*.{0,12}(?:向|往|to |right|left|up|down)", lowered):
        return None
    operator = None
    if "目标" in source or "target" in lowered:
        operator = "move_target"
    elif "箱" in source or "box" in lowered:
        operator = "move_box"
    elif any(marker in source for marker in ("玩家", "出生", "起点")) or "player" in lowered:
        operator = "move_player"
    if operator is None:
        return None
    direction = _movement_direction_from_text(source, lowered)
    if direction is None:
        return None
    return {"operator": operator, "direction": direction}


def _authorized_preserved_components(conversation, stage_context):
    messages = list(conversation or [])
    # Preservation is a designer-owned hard constraint.  An authorized brief can
    # be distilled from an assistant proposal, so it must never be treated as a
    # preservation request by itself (for example, an assistant's alternative
    # "keep the target" must not make a later target move impossible).
    sources = tuple(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    )
    rules = {
        "water": ("水域", "水塘", "水面", "water", "pond"),
        "walls": ("墙", "墙体", "wall"),
        "player": ("玩家", "起点", "出生", "player", "start"),
        "boxes": ("箱子", "箱", "box", "crate"),
        "targets": ("目标", "终点", "目标点", "target", "goal"),
    }
    preserved = set()
    for component, markers in rules.items():
        if any(_text_explicitly_preserves_component(text, markers) for text in sources):
            preserved.add(component)
    return frozenset(preserved)


def _text_explicitly_preserves_component(text, markers):
    value = str(text or "")
    lowered = value.casefold()
    preservation_words = (
        "不要动", "别动", "不改", "不改变", "保留", "保持", "维持", "不变",
        "do not change", "don't change", "do not move", "don't move", "preserve",
        "keep", "leave unchanged", "remain unchanged",
    )
    for marker in markers:
        position = lowered.find(marker.casefold())
        while position >= 0:
            window = lowered[max(0, position - 18): position + len(marker) + 18]
            if any(word in window for word in preservation_words):
                return True
            position = lowered.find(marker.casefold(), position + len(marker))
    return False


def _is_explicit_revision_authorization(message):
    text = str(message or "").casefold()
    return any(marker in text for marker in (
        "请根据这个方向生成", "生成一份可供审查", "请助手具体生成", "帮我改", "你来改吧", "按这个思路改",
        "generate a reviewable", "draft this revision", "go ahead and revise", "change it",
    ))


def _movement_direction_from_text(source, lowered):
    # A location label such as "the upper-right target" is not a movement
    # instruction.  Hard direction constraints require an explicit destination
    # phrase, otherwise the semantic search remains free to select a legal local
    # move that realizes the approved effect.
    chinese_phrases = (
        (("向右上", "往右上", "移到右上", "移动到右上", "挪到右上", "放到右上"), "upper_right"),
        (("向左上", "往左上", "移到左上", "移动到左上", "挪到左上", "放到左上"), "upper_left"),
        (("向右下", "往右下", "移到右下", "移动到右下", "挪到右下", "放到右下"), "lower_right"),
        (("向左下", "往左下", "移到左下", "移动到左下", "挪到左下", "放到左下"), "lower_left"),
        (("向右", "往右", "移到右边", "移动到右边", "挪到右边", "放到右边"), "right"),
        (("向左", "往左", "移到左边", "移动到左边", "挪到左边", "放到左边"), "left"),
        (("向下", "往下", "移到下方", "移动到下方", "挪到下方", "放到下方"), "down"),
        (("向上", "往上", "移到上方", "移动到上方", "挪到上方", "放到上方"), "up"),
    )
    for markers, direction in chinese_phrases:
        if any(marker in source for marker in markers):
            return direction

    english_patterns = (
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?upper[- ]right\b", "upper_right"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?upper[- ]left\b", "upper_left"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?lower[- ]right\b", "lower_right"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?lower[- ]left\b", "lower_left"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\bup and right\b", "upper_right"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\bup and left\b", "upper_left"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\bdown and right\b", "lower_right"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\bdown and left\b", "lower_left"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?right\b", "right"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?left\b", "left"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?down\b", "down"),
        (r"\b(?:move|shift|relocate)\b.{0,48}\b(?:to|toward)\s+(?:the\s+)?up\b", "up"),
    )
    for pattern, direction in english_patterns:
        if re.search(pattern, lowered):
            return direction
    return None


def _movement_requirement_prompt(requirement):
    if not requirement:
        return ""
    entity = {
        "move_target": "target",
        "move_box": "box",
        "move_player": "player",
    }[requirement["operator"]]
    direction = requirement["direction"].replace("_", " ")
    return (
        f"Hard verified movement requirement: the {entity} must move {direction} relative to "
        "its current cell. Use the corresponding move operator and choose a focus containing "
        "that destination; no other operator may substitute for this requirement."
    )


def _preserved_components_prompt(components):
    if not components:
        return ""
    labels = {
        "water": "water",
        "walls": "internal walls",
        "player": "player",
        "boxes": "boxes",
        "targets": "targets",
    }
    named = ", ".join(labels[component] for component in sorted(components))
    return (
        f"Hard verified preservation requirement: do not change {named}, even if another "
        "strategy would make that easier."
    )


async def _compile_revision_plan(
    *,
    api_key,
    base_url,
    models,
    messages,
    request_id,
    started_at,
    deadline=None,
    max_attempts=PROPOSAL_GENERATION_ATTEMPTS,
    initial_validation_feedback=None,
    first_attempt_timeout=PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS,
):
    last_error = None
    attempt_failures = []
    validation_feedback = initial_validation_feedback
    first_failure_code = None
    deadline = deadline or min(
        _request_deadline(started_at),
        started_at + PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS,
    )
    for attempt in range(1, max_attempts + 1):
        if attempt == 1 or first_failure_code == "MODEL_RESPONSE_INVALID":
            model = models[0]
        else:
            model = models[1] if len(models) > 1 else models[0]
        remaining = _remaining_until(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError()
        attempt_timeout = min(
            first_attempt_timeout if attempt == 1 else PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS,
            remaining,
        )
        response_fields = _empty_response_diagnostics()
        _log_llm_event(
            "llm_attempt_started",
            requestId=request_id,
            task="map_proposal",
            model=model,
            attempt=attempt,
            maxAttempts=max_attempts,
            timeoutSeconds=round(attempt_timeout, 3),
            completionTokenLimit=PROPOSAL_PLAN_MAX_COMPLETION_TOKENS,
            deadlineRemainingSeconds=round(remaining, 3),
            responseMode="revision_plan",
        )
        try:
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    _revision_plan_messages_with_feedback(messages, validation_feedback),
                    PROPOSAL_PLAN_MAX_TOKENS,
                    attempt_timeout,
                    task="revision_plan",
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            response_fields = _response_diagnostics(response, choice)
            if str(getattr(choice, "finish_reason", "") or "") == "length":
                raise ValueError("The RevisionPlan output reached its token limit.")
            content = str(choice.message.content or "")
            if not content.strip():
                raise EmptyModelResponse("The model returned an empty response.")
            plan = parse_revision_plan(json.loads(content))
            return plan, attempt, model
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "Kimi did not respond before the revision-plan attempt timeout.",
                request_id,
                True,
                attempt,
                504,
            )
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == "MODEL_RESPONSE_INVALID" and failure_reason:
                last_error.safe_message = failure_reason[:1200]
                validation_feedback = failure_reason[:1200]
        if attempt == 1:
            first_failure_code = last_error.code
        fields = {
            "requestId": request_id,
            "task": "map_proposal",
            "model": model,
            "attempt": attempt,
            "code": last_error.code,
            "retryable": last_error.retryable,
            "latencyMs": int((time.monotonic() - started_at) * 1000),
            "responseMode": "revision_plan",
            "failureClass": _llm_failure_class(last_error, failure_reason),
            **response_fields,
            **_provider_error_fields(last_error),
        }
        if failure_reason:
            fields["validationReason"] = failure_reason
        _log_llm_event("llm_attempt_failed", **fields)
        attempt_failures.append({
            "attempt": attempt,
            "code": last_error.code,
            "failureClass": fields["failureClass"],
            "finishReason": response_fields.get("finishReason"),
            "completionTokens": response_fields.get("completionTokens"),
            "validationReason": failure_reason,
        })
        last_error.proposal_diagnostics = {
            "failureStage": "revision_plan",
            "attemptFailures": attempt_failures,
            "remainingSeconds": round(_remaining_until(deadline), 3),
        }
        if not last_error.retryable:
            raise last_error
        if attempt == 1 and last_error.code not in {
            "MODEL_RESPONSE_INVALID",
            "MODEL_EMPTY_RESPONSE",
            "UPSTREAM_TIMEOUT",
            "UPSTREAM_CONNECTION_ERROR",
        }:
            raise last_error
        if attempt < max_attempts and not _retry_budget_available(
            deadline,
            request_id=request_id,
            task="map_proposal",
            attempt=attempt,
            max_attempts=max_attempts,
            response_mode="revision_plan",
            fallback_reason=last_error.code,
        ):
            break
    raise last_error


def _revision_search_correction_reason(diagnostics):
    if diagnostics.get("constructedCandidates", 0) == 0:
        return (
            "The previous RevisionPlan could not produce any legal local edit: its requested "
            "operators and focus have no compatible editable cells. Choose a different supported "
            "operator and/or a focus that matches components visible in the numbered map."
        )
    return "The previous RevisionPlan produced no candidate that passed deterministic validation."


def _revision_plan_messages_with_feedback(messages, validation_feedback):
    if not validation_feedback:
        return messages
    corrected = [dict(message) for message in messages]
    instruction = (
        "The previous RevisionPlan was rejected for this safe reason: "
        f"{validation_feedback} Return a fresh RevisionPlan JSON object. Keep the authorized "
        "brief, explicit prohibitions, and preserve-unlisted contract unchanged. Return every "
        "intended edit as an exact requiredTransition. Do not return map rows or tile operations; do not return full map rows or leave the "
        "transition list empty. If the prior response reached the token limit, stop reasoning and emit "
        "the complete compact JSON immediately. Return exactly one strategy unless "
        "the authorized brief explicitly requires alternatives, and include no explanatory prose."
    )
    corrected[0]["content"] = f"{corrected[0]['content']}\n\n{instruction}"
    return corrected


def _generate_map_proposal_sync(
    *,
    api_key,
    base_url,
    conversation,
    rows,
    request_id,
    language,
    proposal_validator,
    stage_context,
):
    models = _unified_model_attempts(PROPOSAL_GENERATION_ATTEMPTS)
    legacy_contract = _build_legacy_revision_execution_contract(stage_context)
    messages = _build_map_operation_messages(
        legacy_contract,
        rows,
        language,
        stage_context,
    )
    started_at = time.monotonic()
    deadline = _request_deadline(started_at)
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task="map_proposal",
        primaryModel=models[0],
        fallbackModel=None,
        timeoutSeconds=CHAT_TIMEOUT_SECONDS,
        responseMode="operation_candidates",
    )
    try:
        return asyncio.run(
            asyncio.wait_for(
                _generate_map_operation_candidates(
                    api_key=api_key,
                    base_url=base_url,
                    models=models,
                    messages=messages,
                    base_rows=rows,
                    request_id=request_id,
                    language=language,
                    proposal_validator=proposal_validator,
                    revision_contract=legacy_contract,
                    baseline_metrics=None,
                    started_at=started_at,
                    deadline=deadline,
                ),
                timeout=min(CHAT_TIMEOUT_SECONDS, _remaining_until(deadline)),
            )
        )
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="map_proposal",
            outcome="error",
            code="UPSTREAM_TIMEOUT",
            attemptsUsed=PROPOSAL_GENERATION_ATTEMPTS,
            latencyMs=elapsed_ms,
            responseMode="operation_candidates",
        )
        raise LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not complete the legacy proposal request before its time limit.",
            request_id,
            True,
            PROPOSAL_GENERATION_ATTEMPTS,
            504,
        ) from exception


def _build_legacy_revision_execution_contract(stage_context=None):
    return {
        "schemaVersion": REVISION_CONTRACT_SCHEMA_VERSION,
        "authorizedBrief": str(
            (stage_context or {}).get("authorizedRevisionBrief") or ""
        ).strip()[:1200],
        "revisionPlan": {"strategies": []},
        "strategies": [{
            "strategyIndex": 1,
            "effect": "adjust_internal_walls",
            "focus": None,
            "allowedOperators": [
                "add_wall",
                "remove_wall",
                "move_player",
                "move_box",
                "move_target",
                "add_water",
                "remove_water",
            ],
            "preserve": ["outer_shell", "unrelated_areas"],
            "minimumChangedCells": 1,
            "maximumChangedCells": REVISION_MAX_CHANGED_CELLS,
            "metricGoals": [],
        }],
        "explicitlyRelaxedByDesigner": False,
    }


def _build_revision_execution_contract(plan, authorized_brief, stage_context=None):
    relocation_effects = {
        "relocate_start",
        "relocate_box",
        "relocate_target",
    }
    strategies = []
    for index, strategy in enumerate(plan.strategies, start=1):
        maximum_changed_cells = min(REVISION_MAX_CHANGED_CELLS, strategy.edit_budget)
        strategy_data = strategy.as_dict()
        entity_operators = {"move_player", "move_box", "move_target"}
        required_transitions = strategy_data.get("requiredTransitions") or []
        required_operators = {
            _operation_kind(item["from"], item["to"])
            for item in required_transitions
        }
        if required_transitions:
            minimum_changed_cells = (
                max(2, len(required_transitions))
                if required_operators.intersection(entity_operators)
                else len(required_transitions)
            )
        else:
            minimum_changed_cells = (
                2
                if strategy.effect in relocation_effects
                or set(strategy_data["operators"]).intersection(entity_operators)
                else REVISION_MIN_CHANGED_CELLS
            )
        strategies.append({
            "strategyIndex": index,
            "effect": strategy_data["effect"],
            "focus": strategy_data["focus"],
            "allowedOperators": strategy_data["operators"],
            "preserve": strategy_data["preserve"],
            "minimumChangedCells": minimum_changed_cells,
            "maximumChangedCells": maximum_changed_cells,
            "metricGoals": strategy_data["metricGoals"],
            "requiredTransitions": required_transitions,
            "anchorEntities": strategy_data.get("anchorEntities") or [],
            "playObjective": strategy_data.get("playObjective"),
        })
    contract = {
        "schemaVersion": REVISION_CONTRACT_SCHEMA_VERSION,
        "authorizedBrief": str(authorized_brief or "").strip()[:1200],
        "revisionPlan": plan.as_dict(),
        "strategies": strategies,
        "explicitlyRelaxedByDesigner": bool((stage_context or {}).get("revisionRelaxed")),
    }
    for strategy in contract["strategies"]:
        if strategy["minimumChangedCells"] > strategy["maximumChangedCells"]:
            raise ValueError(
                "revision contract has an impossible changed-cell range: "
                f"{strategy['minimumChangedCells']}..{strategy['maximumChangedCells']}"
            )
    return contract


def _build_map_operation_messages(
    revision_contract,
    rows,
    language,
    stage_context,
    baseline_metrics=None,
):
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    numbered_map = ""
    map_facts = _stage_snapshot_for_prompt(rows, stage_context)
    focus_facts = []
    solver_evidence = _llm_solver_evidence(baseline_metrics or {})
    modifier_contract = _modifier_contract_view(revision_contract)
    system_prompt = (
        "You are the Sokoban co-creation level revision assistant. The saved Stage has 10 rows × 12 columns and is "
        "immutable input. Execute only the supplied execution contract; do not reinterpret the "
        "designer's request, invent a broader goal, or use any conversation outside the contract. "
         "Return only concrete cell-operation candidates. The Stage Snapshot in this request is the only "
         "map source. The application constructs the complete "
        "map, enforces the contract, checks structure, and runs the deterministic solver. "
        "Every candidate must make a meaningful, coherent local change within the contract. Do "
        "not add unrelated cells just to make a diff. Never edit void cells or the connected outer "
        "shell. Never modify preserved components or cells outside the strategy focus. Keep the "
        "map structurally valid with exactly one player and matching box/target pairs. Produce up "
        "to three distinct candidates, each tagged with its strategyIndex. Coordinates are one-based. "
        "Each operation must contain row, column, and to, and may include from as a claim that the "
        "server will verify against the real before tile. Required transitions are the complete "
        "frozen edit set: when they are present, return exactly those transitions and do not add "
        "optional or compensating edits. Never replace a required remove_wall with a box/player "
        "move. "
        "The server objectivePolicy distinguishes hard metrics from a soft play objective. Hard "
        "metricGoals must be met. For a soft objective, create candidates whose changed area affects "
        "the verified route and changes the relevant push order, alternation, transport, or detour "
        "mechanism; do not add irrelevant cells merely to change a metric. "
        "A moved player, box, or target requires paired operations that clear the old cell and place "
        "the entity on a current floor cell. Return JSON only with exactly this shape: "
        "{\"candidates\":[{\"strategyIndex\":1,\"operations\":[{\"row\":5,"
        "\"column\":6,\"from\":\".\",\"to\":\"#\"}]}]}. Each operations array contains one to 24 unique "
        "cells. Allowed destination tiles are space, #, ., @, p, s, and t; space edits are forbidden. "
        f"Natural-language reasoning is internal; any unavoidable text must use {response_language}."
    )
    user_prompt = (
        "The following contract is authoritative and is the only revision instruction. The "
        "map rows and facts below are the exact snapshot for this planning pass:\n"
        f"{json.dumps(modifier_contract, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Column ruler (one-based): 123456789012\n"
        f"Map snapshot fingerprint for this execution pass: {map_fingerprint(rows)}\n"
         f"Current Stage Snapshot (authoritative; 10 rows x 12 columns):\n{map_facts}\n"
        f"Deterministic solver evidence (authoritative): {json.dumps(solver_evidence, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _modifier_contract_view(revision_contract):
    """Expose only the frozen, executable fields to the operation agent.

    The modifier is deliberately not given the Chat/RevisionPlan prose.  It
    receives the immutable cell contract and enough local constraints to
    produce an operation candidate that can be replayed by the server.
    """
    strategies = []
    for strategy in revision_contract.get("strategies") or []:
        strategies.append({
            "strategyIndex": strategy.get("strategyIndex"),
            "effect": strategy.get("effect"),
            "focus": strategy.get("focus"),
            "allowedOperators": list(strategy.get("allowedOperators") or []),
            "preserve": list(strategy.get("preserve") or []),
            "minimumChangedCells": strategy.get("minimumChangedCells"),
            "maximumChangedCells": strategy.get("maximumChangedCells"),
            "metricGoals": list(strategy.get("metricGoals") or []),
            "requiredTransitions": [
                {
                    key: item.get(key)
                    for key in ("row", "column", "from", "to", "anchorEntity")
                    if key in item
                }
                for item in strategy.get("requiredTransitions") or []
            ],
            "anchorEntities": list(strategy.get("anchorEntities") or []),
            "playObjective": strategy.get("playObjective"),
        })
    return {
        "schemaVersion": revision_contract.get("schemaVersion", 1),
        "strategies": strategies,
        "objectivePolicy": revision_contract.get("objectivePolicy") or {},
    }


async def _generate_map_operation_candidates(
    *,
    api_key,
    base_url,
    models,
    messages,
    base_rows,
    request_id,
    language,
    proposal_validator,
    revision_contract,
    baseline_metrics,
    started_at,
    deadline=None,
    candidate_evidence=None,
    excluded_map_fingerprints=None,
):
    last_error = None
    validation_feedback = None
    attempted_candidate_count = 0
    rejected_operation_signatures = set()
    rejected_map_signatures = set()
    rejection_records = []
    excluded_map_fingerprints = set(excluded_map_fingerprints or ())
    deadline = deadline or _request_deadline(started_at)
    attempted_models = list(models[:PROPOSAL_GENERATION_ATTEMPTS])
    while len(attempted_models) < PROPOSAL_GENERATION_ATTEMPTS:
        attempted_models.append(models[0])

    for attempt, configured_model in enumerate(attempted_models, start=1):
        model = models[0] if validation_feedback is not None else configured_model
        remaining = _remaining_until(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError()
        attempt_timeout = min(PROPOSAL_ATTEMPT_TIMEOUT_SECONDS, remaining)
        response_fields = _empty_response_diagnostics()
        _log_llm_event(
            "llm_attempt_started",
            requestId=request_id,
            task="map_proposal",
            model=model,
            attempt=attempt,
            maxAttempts=len(attempted_models),
            timeoutSeconds=round(attempt_timeout, 3),
            responseMode="operation_candidates",
        )
        try:
            attempt_messages = _operation_messages_with_feedback(
                messages,
                validation_feedback,
            )
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    attempt_messages,
                    PROPOSAL_OPERATION_MAX_TOKENS,
                    attempt_timeout,
                    task="operation_candidates",
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            if str(getattr(choice, "finish_reason", "") or "") == "length":
                raise ValueError("The operation-candidate output reached its token limit.")
            response_fields = _response_diagnostics(response, choice)
            content = str(choice.message.content or "")
            if not content.strip():
                raise EmptyModelResponse("The model returned an empty response.")
            payload = json.loads(content)
            if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
                attempted_candidate_count = max(
                    attempted_candidate_count,
                    len(payload["candidates"]),
                )
            selected_rows, selected_index, candidate_count, selected_operations = _select_operation_candidate(
                payload,
                base_rows,
                revision_contract,
                proposal_validator,
                baseline_metrics,
                rejected_operation_signatures,
                rejected_map_signatures,
                rejection_records,
                candidate_evidence,
                excluded_map_fingerprints,
            )
            latency_ms = int((time.monotonic() - started_at) * 1000)
            changed_cell_count = _changed_cell_count(base_rows, selected_rows)
            result = LLMExecutionResult(
                assistant_message=(
                    "我整理了一份等待你审查的地图提案。"
                    if language == "zh-CN"
                    else "I prepared a map proposal for your review."
                ),
                attempts_used=attempt,
                request_id=request_id,
                proposed_rows=list(selected_rows),
                modification_summary="",
                model=model,
                latency_ms=latency_ms,
                guidance={
                    "move": "deliver_revision",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": None,
                    "proposalOffer": None,
                    "uiCues": [],
                },
                revision_contract=revision_contract,
                revision_operations=selected_operations,
                proposal_diagnostics={
                    "candidateCount": candidate_count,
                    "validCandidates": 1,
                    "constructedCandidates": candidate_count,
                    "selectedCandidateIndex": selected_index,
                    "selectedStrategyIndex": _candidate_strategy_index(
                        payload, selected_index
                    ),
                    "changedCellCount": changed_cell_count,
                },
            )
            _log_llm_event(
                "llm_request_completed",
                requestId=request_id,
                task="map_proposal",
                outcome="success",
                model=model,
                attemptsUsed=attempt,
                latencyMs=latency_ms,
                responseMode="operation_candidates",
                candidateCount=candidate_count,
                selectedCandidateIndex=selected_index,
                changedCellCount=changed_cell_count,
                **response_fields,
            )
            return result
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "Kimi did not respond before the attempt timeout.",
                request_id,
                True,
                attempt,
                504,
            )
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == "MODEL_RESPONSE_INVALID" and failure_reason:
                last_error.safe_message = failure_reason[:1200]
                validation_feedback = json.dumps(
                    rejection_records[-3:] or [{"reason": failure_reason[:500]}],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )[:1200]
        failure_fields = {
            "requestId": request_id,
            "task": "map_proposal",
            "model": model,
            "attempt": attempt,
            "code": last_error.code,
            "retryable": last_error.retryable,
            "latencyMs": int((time.monotonic() - started_at) * 1000),
            "responseMode": "operation_candidates",
            "failureClass": _llm_failure_class(last_error, failure_reason),
            **response_fields,
            **_provider_error_fields(last_error),
        }
        if failure_reason:
            failure_fields["validationReason"] = failure_reason[:1200]
        _log_llm_event("llm_attempt_failed", **failure_fields)
        if not last_error.retryable:
            raise last_error
        if attempt < len(attempted_models) and not _retry_budget_available(
            deadline,
            request_id=request_id,
            task="map_proposal",
            attempt=attempt,
            max_attempts=len(attempted_models),
            response_mode="operation_candidates",
            fallback_reason=last_error.code,
        ):
            break

    _log_llm_event(
        "llm_request_completed",
        requestId=request_id,
        task="map_proposal",
        outcome="error",
        code=last_error.code,
        attemptsUsed=last_error.attempts_used,
        latencyMs=int((time.monotonic() - started_at) * 1000),
        responseMode="operation_candidates",
        **_provider_error_fields(last_error),
    )
    if last_error.code == "MODEL_RESPONSE_INVALID":
        error = LLMServiceError(
            "PROPOSAL_SEARCH_EXHAUSTED",
            "The level revision assistant produced no executable candidate satisfying the revision contract.",
            request_id,
            False,
            last_error.attempts_used,
            502,
        )
        error.proposal_diagnostics = {
            "modifierFailure": str(last_error.safe_message)[:1200],
            "attempts": last_error.attempts_used,
            "constructedCandidates": attempted_candidate_count,
            "validCandidates": 0,
            "candidateCount": attempted_candidate_count,
            "rejectionRecords": rejection_records[-12:],
            "uniqueRejectedOperationSets": len(rejected_operation_signatures),
            "uniqueRejectedMaps": len(rejected_map_signatures),
        }
        error.revision_contract = revision_contract
        raise error from last_error
    raise last_error


def _deterministic_revision_fallback(
    *,
    rows,
    plan,
    revision_contract,
    request_id,
    language,
    proposal_validator,
    baseline_metrics,
    movement_requirement,
    preserved_components,
    started_at,
    entity_bindings=None,
    deadline=None,
    excluded_map_fingerprints=None,
):
    """Use the bounded local search when the modifier model cannot supply a valid candidate."""
    result = search_revision_plan(
        rows,
        plan,
        proposal_validator or validate_and_solve,
        baseline_metrics=baseline_metrics,
        # Search is a bounded fallback *phase*. Its budget starts when the
        # modifier phase hands control to deterministic search, rather than
        # being consumed by the preceding RevisionPlan request.
        deadline=min(
            deadline or _request_deadline(started_at),
            time.monotonic() + PROPOSAL_SEARCH_DEADLINE_SECONDS,
        ),
        movement_requirement=movement_requirement,
        preserved_components=preserved_components,
        entity_bindings=entity_bindings,
        excluded_map_fingerprints=excluded_map_fingerprints,
    )
    strategy_index = result.strategy_index
    selected_contract = (revision_contract.get("strategies") or [])[result.strategy_index - 1]
    operations = _operations_from_diff(
        rows,
        result.rows,
        include_from=bool(selected_contract.get("requiredTransitions")),
    )
    if not operations:
        raise ProposalSearchExhausted({
            **result.diagnostics,
            "failureReasons": {"empty_diff": 1},
        })
    # Re-run the public operation path so a search primitive can never bypass
    # the same contract that protects an LLM candidate.
    verified_rows = execute_revision_operations(
        rows,
        operations,
        revision_contract,
        strategy_index,
    )
    if tuple(verified_rows) != tuple(result.rows):
        raise ProposalSearchExhausted({
            **result.diagnostics,
            "failureReasons": {"contract_replay_mismatch": 1},
        })
    diagnostics = dict(result.diagnostics or {})
    diagnostics.update({
        "source": "deterministic_search",
        "selectedCandidateIndex": None,
        "selectedStrategyIndex": strategy_index,
        "changedCellCount": _changed_cell_count(rows, result.rows),
        "candidateCount": diagnostics.get("constructedCandidates", 0),
    })
    return LLMExecutionResult(
        assistant_message=(
            "我整理了一份等待你审查的地图提案。"
            if language == "zh-CN"
            else "I prepared a map proposal for your review."
        ),
        attempts_used=0,
        request_id=request_id,
        proposed_rows=list(result.rows),
        modification_summary="",
        model="deterministic-revision-search",
        latency_ms=diagnostics.get("elapsedMs", 0),
        guidance={
            "move": "deliver_revision",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "uiCues": [],
        },
        revision_contract=revision_contract,
        revision_operations=operations,
        proposal_diagnostics=diagnostics,
    )


def _operations_from_diff(before_rows, after_rows, include_from=False):
    operations = []
    for y, (before_row, after_row) in enumerate(zip(before_rows, after_rows)):
        for x, (before, after) in enumerate(zip(before_row, after_row)):
            if before == after:
                continue
            operation = {
                "row": y + 1,
                "column": x + 1,
                "to": after,
            }
            if include_from:
                operation["from"] = before
            operations.append(operation)
    return operations


def language_from_context(stage_context):
    return "zh-CN" if stage_context.get("responseLanguage") == "zh-CN" else "en"


def _operation_messages_with_feedback(messages, validation_feedback):
    if not validation_feedback:
        return messages
    corrected = [dict(message) for message in messages]
    instruction = (
        "Every candidate in the previous attempt was rejected for these safe reasons: "
        f"{validation_feedback} Return a fresh candidates JSON object. Do not reuse a rejected "
        "operation set, do not return the original map unchanged, and keep the authorized brief "
        "and preserve-unlisted contract unchanged."
    )
    corrected[0]["content"] = f"{corrected[0]['content']}\n\n{instruction}"
    return corrected


def _select_operation_candidate(
    payload,
    base_rows,
    revision_contract,
    proposal_validator,
    baseline_metrics=None,
    rejected_operation_signatures=None,
    rejected_map_signatures=None,
    rejection_records=None,
    candidate_evidence=None,
    excluded_map_fingerprints=None,
):
    if not isinstance(payload, dict):
        raise ValueError("The map proposal must be a JSON object.")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= PROPOSAL_CANDIDATE_LIMIT:
        raise ValueError("candidates must contain one to three operation candidates.")
    failures = []
    rejected_operation_signatures = (
        rejected_operation_signatures
        if rejected_operation_signatures is not None
        else set()
    )
    rejected_map_signatures = (
        rejected_map_signatures if rejected_map_signatures is not None else set()
    )
    rejection_records = rejection_records if rejection_records is not None else []
    candidate_evidence = candidate_evidence if candidate_evidence is not None else {}
    excluded_map_fingerprints = set(excluded_map_fingerprints or ())
    valid = []
    strategies = revision_contract.get("strategies") or []
    if not strategies:
        raise ValueError("The revision contract must contain at least one strategy.")
    for index, candidate in enumerate(candidates, start=1):
        operation_hash = None
        map_hash = None
        try:
            if not isinstance(candidate, dict) or set(candidate) != {"strategyIndex", "operations"}:
                raise ValueError("candidate must be an object")
            strategy_index = candidate["strategyIndex"]
            if (
                isinstance(strategy_index, bool)
                or not isinstance(strategy_index, int)
                or not 1 <= strategy_index <= len(strategies)
            ):
                raise ValueError("candidate strategyIndex is invalid")
            strategy = strategies[strategy_index - 1]
            operations = candidate.get("operations")
            operation_signature = _canonical_operation_signature(operations)
            operation_hash = hashlib.sha256(
                repr(operation_signature).encode("utf-8")
            ).hexdigest()[:16]
            if operation_signature in rejected_operation_signatures:
                raise ValueError("candidate repeats an operation set rejected in an earlier attempt")
            rejected_operation_signatures.add(operation_signature)
            rows = execute_revision_operations(
                base_rows,
                operations,
                revision_contract,
                strategy_index,
            )
            signature = tuple(rows)
            full_map_hash = map_fingerprint(rows)
            map_hash = full_map_hash[:16]
            if full_map_hash in excluded_map_fingerprints:
                raise ValueError("candidate duplicates the cited proposal result")
            if signature in rejected_map_signatures:
                raise ValueError("candidate duplicates an earlier operation result")
            rejected_map_signatures.add(signature)
            changed_cells = _changed_cell_count(base_rows, rows)
            if not (
                strategy["minimumChangedCells"]
                <= changed_cells
                <= strategy["maximumChangedCells"]
            ):
                raise ValueError(
                    "candidate changed "
                    f"{changed_cells} cells; expected "
                    f"{strategy['minimumChangedCells']}..{strategy['maximumChangedCells']}"
                )
            if proposal_validator is not None:
                validation = proposal_validator(rows)
            else:
                validation = validate_and_solve(rows)
            _validate_metric_goals(
                strategy.get("metricGoals") or [],
                baseline_metrics or {},
                validation,
            )
            evidence = candidate_evidence.get(full_map_hash) or {}
            positive_deltas = [
                value
                for value in (evidence.get("metricDeltas") or {}).values()
                if isinstance(value, int) and value > 0
            ]
            valid.append((
                (
                    int(bool(evidence.get("passed", True))),
                    len(positive_deltas),
                    sum(positive_deltas),
                    -changed_cells,
                    tuple(rows),
                ),
                rows,
                index,
                list(operations),
            ))
        except Exception as exception:
            reason = _safe_validation_reason(exception)
            failures.append(f"candidate {index}: {reason}")
            metric_detail = {}
            if isinstance(exception, HardObjectiveError):
                metric_detail = {
                    "metric": exception.metric,
                    "direction": exception.direction,
                    "baselineValue": exception.before,
                    "candidateValue": exception.after,
                    "minimumDelta": exception.minimum_delta,
                    "metricVerifiable": exception.verifiable,
                }
            rejection_records.append({
                "candidate": index,
                "category": _candidate_rejection_category(exception),
                "reason": reason[:500],
                **({"operationSignature": operation_hash} if operation_hash else {}),
                **({"mapFingerprint": map_hash} if map_hash else {}),
                **metric_detail,
            })
    if valid:
        _, rows, index, operations = max(valid, key=lambda item: item[0])
        return rows, index, len(candidates), operations
    raise ValueError("; ".join(failures)[:1200])


def _canonical_operation_signature(operations):
    if not isinstance(operations, list):
        return ("invalid", repr(operations)[:200])
    normalized = []
    for item in operations:
        if not isinstance(item, dict):
            normalized.append(("invalid", repr(item)[:120]))
            continue
        normalized.append((
            item.get("row"),
            item.get("column"),
            item.get("from"),
            item.get("to"),
        ))
    return tuple(sorted(normalized, key=repr))


def _candidate_rejection_category(exception):
    if isinstance(exception, ObjectiveEvidenceError):
        return "soft_objective_evidence_missing"
    if isinstance(exception, HardObjectiveError):
        return "hard_objective_not_met"
    code = str(getattr(exception, "code", "") or "")
    if code:
        if code == "UNSOLVABLE_LEVEL":
            return "candidate_unsolvable"
        if "BUDGET" in code:
            return "solver_budget_exhausted"
        return code.casefold()
    text = _safe_validation_reason(exception).casefold()
    if "metric goal" in text:
        return "hard_objective_not_met"
    if "duplicate" in text or "repeats" in text:
        return "candidate_duplicated"
    if "contract" in text or "outside" in text or "preserved" in text:
        return "contract_mismatch"
    return "candidate_invalid"


def _candidate_strategy_index(payload, candidate_index):
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not 1 <= candidate_index <= len(candidates):
        return None
    candidate = candidates[candidate_index - 1]
    return candidate.get("strategyIndex") if isinstance(candidate, dict) else None


def execute_revision_operations(base_rows, operations, revision_contract, strategy_index):
    """Apply one modifier-agent candidate under the immutable execution contract."""
    if not isinstance(operations, list) or any(
        not isinstance(operation, dict)
        or set(operation) not in (
            {"row", "column", "to"},
            {"row", "column", "from", "to"},
        )
        for operation in operations
    ):
        raise ValueError(
            "modifier operations must contain only row, column, and to, with optional from"
        )
    strategies = (revision_contract or {}).get("strategies") or []
    if (
        isinstance(strategy_index, bool)
        or not isinstance(strategy_index, int)
        or not 1 <= strategy_index <= len(strategies)
    ):
        raise ValueError("the selected revision strategy is invalid")
    strategy = strategies[strategy_index - 1]
    rows = _apply_map_operations(
        base_rows,
        operations,
        strategy_contract=strategy,
    )
    changed_cells = _changed_cell_count(base_rows, rows)
    minimum = int(strategy.get("minimumChangedCells") or 0)
    maximum = int(strategy.get("maximumChangedCells") or REVISION_MAX_CHANGED_CELLS)
    if not minimum <= changed_cells <= maximum:
        raise ValueError(
            f"candidate changed {changed_cells} cells; expected {minimum}..{maximum}"
        )
    return rows


def _deterministic_exact_revision(
    rows,
    execution_brief,
    request_id,
    language,
    proposal_validator,
    entity_bindings=None,
):
    """Execute a frozen transition contract without asking an LLM to guess cells."""
    brief = _validate_execution_brief(execution_brief, rows, entity_bindings)
    transitions = brief["requiredTransitions"]
    if not transitions:
        return None

    operator_by_transition = {
        (".", "#"): "add_wall",
        ("#", "."): "remove_wall",
        (".", "@"): "add_water",
        ("@", "."): "remove_water",
        ("p", "."): "move_player",
        (".", "p"): "move_player",
        ("s", "."): "move_box",
        (".", "s"): "move_box",
        ("t", "."): "move_target",
        (".", "t"): "move_target",
    }
    operators = list(dict.fromkeys(
        operator_by_transition[(item["from"], item["to"])]
        for item in transitions
    ))
    if len(operators) > 3:
        raise LLMServiceError(
            "PROPOSAL_SEARCH_EXHAUSTED",
            "The frozen transition contract is too broad for one local proposal.",
            request_id,
            False,
            0,
            422,
        )

    effect = brief.get("effect")
    effect_by_operator = {
        "add_wall": "adjust_internal_walls",
        "remove_wall": "open_route",
        "add_water": "reshape_water",
        "remove_water": "open_route",
        "move_player": "relocate_start",
        "move_box": "relocate_box",
        "move_target": "relocate_target",
    }
    if effect is None:
        effect = effect_by_operator[operators[0]]
    focus = brief.get("focus")
    if focus is None:
        first = transitions[0]
        focus = {"row": first["row"], "column": first["column"], "radius": 1}
    entity_operator = any(operator.startswith("move_") for operator in operators)
    edit_budget = max(len(transitions), 2 if entity_operator else 1)
    plan = parse_revision_plan({
        "strategies": [{
            "effect": effect,
            "focus": focus,
            "operators": operators,
            "preserve": brief.get("preserve") or [],
            "editBudget": edit_budget,
            "metricGoals": [],
            "requiredTransitions": transitions,
            "anchorEntities": brief.get("anchors") or [],
            "playObjective": brief.get("playObjective"),
        }],
    })
    validate_revision_plan_against_map(rows, plan, entity_bindings)
    _validate_revision_plan_entities(plan, rows, entity_bindings)
    contract = _build_revision_execution_contract(plan, "", {})
    operations = [
        {
            "row": item["row"],
            "column": item["column"],
            "from": item["from"],
            "to": item["to"],
        }
        for item in transitions
    ]
    proposed_rows = execute_revision_operations(rows, operations, contract, 1)
    if proposal_validator is not None:
        proposal_validator(proposed_rows)
    changed = _changed_cell_count(rows, proposed_rows)
    return LLMExecutionResult(
        assistant_message="",
        attempts_used=0,
        request_id=request_id,
        model="deterministic-exact-contract",
        proposed_rows=proposed_rows,
        revision_plan=plan.as_dict(),
        revision_contract=contract,
        revision_operations=operations,
        proposal_diagnostics={
            "source": "deterministic_contract",
            "changedCellCount": changed,
            "candidateCount": 1,
            "modifierAttempts": 0,
        },
        guidance={
            "move": "deliver_revision",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "disagreement": None,
            "uiCues": [],
        },
    )


def _apply_map_operations(
    base_rows,
    operations,
    revision_brief=None,
    strategy_contract=None,
):
    if not isinstance(operations, list) or not 1 <= len(operations) <= PROPOSAL_OPERATION_LIMIT:
        raise ValueError("operations must contain one to 24 cell changes")
    allowed_tiles = set(" #.@pst")
    mutable = [list(row) for row in base_rows]
    seen = {}
    shell = _connected_outer_shell(base_rows)
    normalized = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) not in (
            {"row", "column", "to"},
            {"row", "column", "from", "to"},
        ):
            raise ValueError("each operation must contain row, column, and to")
        row = operation["row"]
        column = operation["column"]
        after = operation["to"]
        if isinstance(row, bool) or isinstance(column, bool) or not isinstance(row, int) or not isinstance(column, int):
            raise ValueError("operation coordinates must be integers")
        y, x = row - 1, column - 1
        if not (0 <= y < len(base_rows) and 0 <= x < len(base_rows[y])):
            raise ValueError("operation coordinate is outside the 10-row × 12-column Stage")
        before = base_rows[y][x]
        declared_before = operation.get("from")
        if declared_before is not None and declared_before != before:
            raise ValueError(f"row {row}, column {column} does not match its declared from tile")
        if before not in allowed_tiles or after not in allowed_tiles or len(before) != 1 or len(after) != 1:
            raise ValueError("operation contains an unsupported tile")
        if (x, y) in seen:
            if seen[(x, y)] == after:
                continue
            raise ValueError("duplicate operation coordinates conflict")
        seen[(x, y)] = after
        if before == after:
            continue
        if before == " " or after == " ":
            raise ValueError("void cells cannot be edited")
        if (x, y) in shell:
            raise ValueError("the connected outer shell cannot be edited")
        mutable[y][x] = after
        normalized.append((x, y, before, after))
    if not normalized:
        raise ValueError("operations must contain at least one real tile change")
    if strategy_contract is not None:
        _validate_operation_contract(normalized, strategy_contract)
    else:
        _validate_operation_intent_scope(normalized, revision_brief)
    return ["".join(row) for row in mutable]


def _operation_kind(before, after):
    return {
        (".", "#"): "add_wall",
        ("#", "."): "remove_wall",
        ("p", "."): "move_player",
        (".", "p"): "move_player",
        ("s", "."): "move_box",
        (".", "s"): "move_box",
        ("t", "."): "move_target",
        (".", "t"): "move_target",
        (".", "@"): "add_water",
        ("@", "."): "remove_water",
    }.get((before, after))


def _changed_cell_count(before_rows, after_rows):
    return sum(
        before != after
        for before_row, after_row in zip(before_rows, after_rows)
        for before, after in zip(before_row, after_row)
    )


def _validate_operation_contract(operations, strategy):
    effect_operators = {
        "open_route": {"remove_wall", "remove_water"},
        "narrow_route": {"add_wall", "add_water"},
        "adjust_internal_walls": {"add_wall", "remove_wall"},
        "relocate_start": {"move_player"},
        "relocate_box": {"move_box"},
        "relocate_target": {"move_target"},
        "reshape_water": {"add_water", "remove_water"},
        "change_box_order": {"move_box", "move_target", "add_wall", "remove_wall"},
    }
    component_by_operator = {
        "move_player": "player",
        "move_box": "boxes",
        "move_target": "targets",
        "add_water": "water",
        "remove_water": "water",
    }
    allowed = set(strategy.get("allowedOperators") or [])
    preserved = set(strategy.get("preserve") or [])
    observed = set()
    positions = []
    operator_counts = {}
    for x, y, before, after in operations:
        operator = _operation_kind(before, after)
        if operator is None:
            raise ValueError(f"unsupported tile transition {before!r} to {after!r}")
        if operator not in allowed:
            raise ValueError(f"operation {operator} is outside the revision contract")
        component = component_by_operator.get(operator)
        if component and component in preserved:
            raise ValueError(f"operation changes preserved component {component}")
        if operator in {"add_wall", "remove_wall"} and "walls" in preserved:
            raise ValueError("operation changes preserved walls")
        focus = strategy.get("focus")
        if focus is not None:
            if max(
                abs((focus["column"] - 1) - x),
                abs((focus["row"] - 1) - y),
            ) > focus["radius"]:
                raise ValueError("operation is outside the revision focus")
        observed.add(operator)
        positions.append((x, y))
        operator_counts[operator] = operator_counts.get(operator, 0) + 1

    expected = effect_operators.get(strategy.get("effect"), set())
    if not observed.intersection(expected):
        raise ValueError("operations do not realize the planned effect")
    for operator in ("move_player", "move_box", "move_target"):
        if operator_counts.get(operator, 0) % 2:
            raise ValueError(f"{operator} requires paired source and destination operations")
    if len(set(positions)) != len(positions):
        raise ValueError("the revision contract cannot contain duplicate changed cells")
    required = {
        (item.get("row"), item.get("column"), item.get("from"), item.get("to"))
        for item in strategy.get("requiredTransitions") or []
    }
    if required:
        observed_transitions = {
            (y + 1, x + 1, before, after)
            for x, y, before, after in operations
        }
        if observed_transitions != required:
            raise ValueError(
                "operations must exactly match the frozen required tile transitions"
            )


def _validate_metric_goals(goals, baseline_metrics, validation):
    values = {
        "solutionSteps": getattr(validation, "solution_steps", None),
        "solutionPushes": getattr(validation, "solution_pushes", None),
        "minimumPushes": None,
        "searchedStates": getattr(validation, "searched_states", None),
    }
    for goal in goals:
        metric = goal.get("metric")
        if metric == "minimumPushes":
            try:
                values[metric] = minimum_pushes(validation.rows)
            except Exception:
                values[metric] = None
        before = baseline_metrics.get(metric)
        after = values.get(metric)
        if before is None or after is None:
            raise HardObjectiveError(
                metric,
                goal.get("direction"),
                before,
                after,
                int(goal.get("minimumDelta") or 1),
                verifiable=False,
            )
        direction = goal.get("direction")
        minimum_delta = int(goal.get("minimumDelta") or 1)
        matched = {
            "increase": after - before >= minimum_delta,
            "decrease": before - after >= minimum_delta,
            "preserve": after == before,
        }.get(direction, False)
        if not matched:
            raise HardObjectiveError(
                metric,
                direction,
                before,
                after,
                minimum_delta,
            )


def _connected_outer_shell(rows):
    pending = []
    visited = set()
    height = len(rows)
    width = len(rows[0]) if rows else 0
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if tile == "#" and (x in {0, width - 1} or y in {0, height - 1}):
                pending.append((x, y))
    while pending:
        position = pending.pop()
        if position in visited:
            continue
        visited.add(position)
        x, y = position
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and rows[ny][nx] == "#":
                pending.append((nx, ny))
    return visited


def _validate_operation_intent_scope(operations, revision_brief):
    text = str(revision_brief or "").casefold()
    touched = {tile for _, _, before, after in operations for tile in (before, after)}
    component_markers = (
        (("目标", "落点", "target", "goal"), "t", "target"),
        (("箱", "box", "crate"), "s", "box"),
        (("水", "water", "pond"), "@", "water"),
        (("墙", "wall"), "#", "wall"),
        (("玩家", "起点", "player start"), "p", "player"),
    )
    for markers, tile, label in component_markers:
        if _brief_requests_component_change(text, markers) and tile not in touched:
            raise ValueError(f"the operations do not change the explicitly requested {label}")
    positions = [(x, y) for x, y, _, _ in operations]
    region_rules = (
        (("下半", "下方", "底部", "lower", "bottom"), lambda x, y: y >= 5, "lower area"),
        (("上半", "上方", "顶部", "upper", "top"), lambda x, y: y < 5, "upper area"),
        (("左侧", "左边", "left"), lambda x, y: x < 6, "left area"),
        (("右侧", "右边", "right"), lambda x, y: x >= 6, "right area"),
    )
    matched_regions = [
        (predicate, label)
        for markers, predicate, label in region_rules
        if any(marker in text for marker in markers)
    ]
    if len(matched_regions) == 1:
        predicate, label = matched_regions[0]
        if not any(predicate(x, y) for x, y in positions):
            raise ValueError(f"the operations do not touch the explicitly requested {label}")


def _brief_requests_component_change(text, markers):
    action_pattern = re.compile(
        r"移动|调整|重排|增加|减少|移除|改变|拉开|收紧|添加|删除|改动|修改|"
        r"move|shift|adjust|rearrange|add|remove|reduce|increase|change|reshape|revise"
    )
    preserve_pattern = re.compile(r"保持|保留|不动|不改|preserve|keep|unchanged")
    for marker in markers:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            window = text[max(0, index - 10): index + len(marker) + 10]
            if action_pattern.search(window) and not preserve_pattern.search(window):
                return True
            start = index + len(marker)
    return False


def _generate_plain_chat_sync(
    *,
    conversation,
    rows,
    request_id,
    language,
    solver_metrics,
    play_summary,
    stage_context,
    stage_opening=False,
    deadline=None,
    max_attempts=None,
):
    api_key, base_url = _llm_credentials()

    if not api_key or api_key in {
        "your_kimi_api_key_here",
        "your_llm_api_key_here",
    }:
        raise LLMServiceError(
            "CONFIGURATION_ERROR",
            "The configured LLM API key is missing.",
            request_id,
            False,
            0,
            503,
        )

    effective_stage_context = dict(stage_context or {})
    if isinstance(effective_stage_context.get("proposalClarification"), dict):
        clarification = dict(effective_stage_context["proposalClarification"])
        clarification["routeEvidence"] = _proposal_clarification_route_evidence(
            rows,
            solver_metrics or {},
            effective_stage_context,
        )
        effective_stage_context["proposalClarification"] = clarification

    effective_max_attempts = (
        1 if stage_opening else CHAT_MAX_ATTEMPTS
    ) if max_attempts is None else max(1, int(max_attempts))
    models = _unified_model_attempts(effective_max_attempts)

    messages = build_plain_chat_messages(
        conversation,
        rows,
        language,
        solver_metrics,
        play_summary,
        effective_stage_context,
        stage_opening=stage_opening,
    )
    guidance_mode = classify_guidance_request(
        conversation,
        effective_stage_context,
        stage_opening=stage_opening,
    )
    task = "stage_assessment_fallback" if stage_opening else "chat"
    validation_mode = _chat_validation_mode(
        conversation,
        effective_stage_context,
        stage_opening=stage_opening,
        guidance_mode=guidance_mode,
    )
    historical_reference = _has_historical_stage_reference(
        _latest_user_text(conversation)
    )
    started_at = time.monotonic()
    deadline = deadline or _request_deadline(started_at)
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task=task,
        primaryModel=models[0],
        fallbackModel=None,
        timeoutSeconds=PLAIN_CHAT_TIMEOUT_SECONDS,
        responseMode="plain_text",
        guidanceMode=guidance_mode,
    )

    try:
        return asyncio.run(
            asyncio.wait_for(
                _generate_plain_with_model_fallback(
                    api_key=api_key,
                    base_url=base_url,
                    models=models,
                    messages=messages,
                    request_id=request_id,
                    task=task,
                    language=language,
                    solver_metrics=solver_metrics,
                    rows=rows,
                    play_summary=play_summary,
                    stage_opening=stage_opening,
                    stage_context=effective_stage_context,
                    guidance_mode=guidance_mode,
                    validation_mode=validation_mode,
                    historical_reference=historical_reference,
                    semantic_messages=conversation,
                    started_at=started_at,
                    deadline=deadline,
                ),
                timeout=min(PLAIN_CHAT_TIMEOUT_SECONDS, _remaining_until(deadline)),
            )
        )
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task=task,
            outcome="error",
            code="UPSTREAM_TIMEOUT",
            attemptsUsed=min(len(models), CHAT_MAX_ATTEMPTS),
            latencyMs=elapsed_ms,
            responseMode="plain_text",
        )
        raise LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not complete the request before the 120 second limit.",
            request_id,
            True,
            min(len(models), CHAT_MAX_ATTEMPTS),
            504,
        ) from exception


def translate_turns(items, target_language, request_id):
    api_key, base_url = _llm_credentials()

    if not api_key or api_key in {
        "your_kimi_api_key_here",
        "your_llm_api_key_here",
    }:
        raise LLMServiceError(
            "CONFIGURATION_ERROR",
            "The configured LLM API key is missing.",
            request_id,
            False,
            0,
            503,
        )

    target_name = "Simplified Chinese" if target_language == "zh-CN" else "English"
    # Snapshot fields are intentionally retained for local response validation,
    # but translations must never receive a second unstructured map payload.
    prompt_items = [
        {
            key: value
            for key, value in item.items()
            if key not in {"stageRows", "entityBindings", "guidance"}
        }
        for item in items
    ]
    messages = [
        {
            "role": "system",
            "content": (
                f"Translate the supplied Sokoban co-design UI text into {target_name}. "
                "Return one complete JSON object only. Preserve meaning, uncertainty, "
                "direct first/second-person voice, paragraph breaks, and question marks. "
                "Do not add advice, questions, claims, or explanations. Keep null values "
                "null and keep every turnId unchanged. Translate each uiCueTexts item in "
                "the same order. Return exactly {\"translations\":[{\"turnId\":\"...\","
                "\"body\":\"...\",\"followUpQuestion\":null,"
                "\"intentHypothesis\":null,\"proposalOfferSummary\":null,"
                "\"proposalOfferRationale\":null,\"uiCueTexts\":[],"
                "\"proposalSummary\":null,\"disagreement\":null}]}."
                " When the source item has a disagreement object, preserve its status, subject, "
                "and resolution, and translate only its userPosition, aiPosition, "
                "coreDisagreement, and nextQuestion fields. When a source item includes "
                "coordinateLinks, return coordinateLinkTexts in the same order and translate "
                "only their text values; omit coordinateLinkTexts for items without coordinateLinks."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"items": prompt_items}, ensure_ascii=False),
        },
    ]
    models = _unified_model_attempts(CHAT_MAX_ATTEMPTS)

    started_at = time.monotonic()
    deadline = _request_deadline(started_at)
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task="translation",
        primaryModel=models[0],
        fallbackModel=None,
        timeoutSeconds=CHAT_TIMEOUT_SECONDS,
        responseMode="json_object",
    )

    try:
        return asyncio.run(
            asyncio.wait_for(
                _translate_with_model_fallback(
                    api_key=api_key,
                    base_url=base_url,
                    models=models,
                    messages=messages,
                    items=items,
                    target_language=target_language,
                    request_id=request_id,
                    started_at=started_at,
                    deadline=deadline,
                ),
                timeout=min(CHAT_TIMEOUT_SECONDS, _remaining_until(deadline)),
            )
        )
    except asyncio.TimeoutError as exception:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="translation",
            outcome="error",
            code="UPSTREAM_TIMEOUT",
            attemptsUsed=min(len(models), CHAT_MAX_ATTEMPTS),
            latencyMs=elapsed_ms,
            responseMode="json_object",
        )
        raise LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not complete the translation before the 120 second limit.",
            request_id,
            True,
            min(len(models), CHAT_MAX_ATTEMPTS),
            504,
        ) from exception


async def _translate_with_model_fallback(
    *,
    api_key,
    base_url,
    models,
    messages,
    items,
    target_language,
    request_id,
    started_at,
    deadline=None,
):
    last_error = None
    validation_feedback = None
    deadline = deadline or _request_deadline(started_at)

    max_attempts = len(models)
    for attempt, model in enumerate(models[:max_attempts], start=1):
        remaining = _remaining_until(deadline)
        response_fields = _empty_response_diagnostics()

        if remaining <= 0:
            raise asyncio.TimeoutError()

        attempt_timeout = min(
            PRIMARY_ATTEMPT_TIMEOUT_SECONDS if attempt == 1 else remaining,
            remaining,
        )
        _log_llm_event(
            "llm_attempt_started",
            requestId=request_id,
            task="translation",
            model=model,
            attempt=attempt,
            maxAttempts=max_attempts,
            timeoutSeconds=round(attempt_timeout, 3),
            responseMode="json_object",
        )

        try:
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    _messages_with_validation_feedback(
                        messages,
                        validation_feedback,
                        "translation",
                    ),
                    TRANSLATION_MAX_TOKENS,
                    attempt_timeout,
                    task="translation",
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            response_fields = _response_diagnostics(response, choice)

            if str(getattr(choice, "finish_reason", "") or "") == "length":
                raise ValueError("The model output reached its token limit.")

            content = str(choice.message.content or "")

            if not content.strip():
                raise EmptyModelResponse("The model returned an empty response.")

            payload = json.loads(content)
            translations = validate_translation_response(
                payload,
                items,
                target_language=target_language,
            )
            latency_ms = int((time.monotonic() - started_at) * 1000)
            _log_llm_event(
                "llm_request_completed",
                requestId=request_id,
                task="translation",
                outcome="success",
                model=model,
                attemptsUsed=attempt,
                latencyMs=latency_ms,
                responseMode="json_object",
                **response_fields,
            )
            return TranslationExecutionResult(
                translations=translations,
                attempts_used=attempt,
                request_id=request_id,
                model=model,
                latency_ms=latency_ms,
            )
        except asyncio.TimeoutError as exception:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "Kimi did not respond before the translation attempt timeout.",
                request_id,
                True,
                attempt,
                504,
            )
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)

            if last_error.code == "MODEL_RESPONSE_INVALID" and failure_reason:
                validation_feedback = failure_reason

        failure_fields = {
            "requestId": request_id,
            "task": "translation",
            "model": model,
            "attempt": attempt,
            "code": last_error.code,
            "retryable": last_error.retryable,
            "latencyMs": int((time.monotonic() - started_at) * 1000),
            "responseMode": "json_object",
            "failureClass": _llm_failure_class(last_error, failure_reason),
            **response_fields,
            **_provider_error_fields(last_error),
        }

        if failure_reason:
            failure_fields["validationReason"] = failure_reason

        _log_llm_event("llm_attempt_failed", **failure_fields)

        if not last_error.retryable:
            raise last_error

        if attempt < max_attempts and not _retry_budget_available(
            deadline,
            request_id=request_id,
            task=task,
            attempt=attempt,
            max_attempts=max_attempts,
            response_mode="json_object",
            fallback_reason=last_error.code,
        ):
            break

    if last_error is not None:
        failure_class = _llm_failure_class(last_error, validation_feedback)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="translation",
            outcome="error",
            code=last_error.code,
            attemptsUsed=last_error.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="json_object",
            failureClass=failure_class,
            salvageAction="none",
            remainingSeconds=round(_remaining_until(deadline), 3),
            **_provider_error_fields(last_error),
        )
        raise last_error

    raise LLMServiceError(
        "INTERNAL_ERROR",
        "The translation request ended unexpectedly.",
        request_id,
        False,
        0,
        500,
    )


async def _generate_plain_with_model_fallback(
    *,
    api_key,
    base_url,
    models,
    messages,
    request_id,
    task,
    language,
    solver_metrics,
    rows,
    play_summary,
    stage_opening,
    stage_context,
    guidance_mode,
    validation_mode="ordinary_chat",
    historical_reference=False,
    semantic_messages=None,
    started_at,
    deadline=None,
):
    last_error = None
    validation_feedback = None
    deadline = deadline or _request_deadline(started_at)
    # The model receives only the current user turn plus the authoritative
    # StageSnapshot.  Deterministic card/memory helpers may still use the
    # server-owned semantic conversation (never as map facts) so a prior
    # design judgment can be recognized without re-sending old prose to Kimi.
    semantic_messages = semantic_messages or messages
    proposal_clarification = (
        (stage_context or {}).get("proposalClarification")
        if guidance_mode == "needs_clarification"
        else None
    )
    clarification_active = bool(
        isinstance(proposal_clarification, dict)
        and proposal_clarification.get("questionKey")
    )
    clarification_body_candidate = ""

    max_attempts = len(models)
    for attempt, model in enumerate(models[:max_attempts], start=1):
        remaining = _remaining_until(deadline)

        if remaining <= 0:
            raise asyncio.TimeoutError()

        attempt_timeout = min(
            PLAIN_PRIMARY_TIMEOUT_SECONDS if attempt == 1 else remaining,
            remaining,
        )
        response_fields = _empty_response_diagnostics()
        grounding_dropped_count = 0
        clarification_fallback_used = False
        _log_llm_event(
            "llm_attempt_started",
            requestId=request_id,
            task=task,
            model=model,
            attempt=attempt,
            maxAttempts=len(models[:CHAT_MAX_ATTEMPTS]),
            timeoutSeconds=round(attempt_timeout, 3),
            responseMode="plain_text",
        )

        try:
            attempt_messages = _plain_messages_with_validation_feedback(
                messages,
                validation_feedback,
                validation_mode=validation_mode,
                rows=rows,
                stage_context=stage_context,
            )
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    attempt_messages,
                    PLAIN_CHAT_MAX_TOKENS,
                    attempt_timeout,
                    structured=clarification_active,
                    task=(
                        "proposal_clarification"
                        if clarification_active
                        else "stage_assessment_fallback" if stage_opening else "plain_chat"
                    ),
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            response_fields = _response_diagnostics(response, choice)
            if str(getattr(choice, "finish_reason", "") or "") == "length":
                raise ValueError("The model output reached its token limit.")
            content = str(choice.message.content or "")

            if not content.strip():
                raise EmptyModelResponse("The model returned an empty response.")

            if len(content.strip()) > CHAT_RESPONSE_HARD_LENGTH:
                raise ValueError("The model response exceeded the transport safety limit.")

            clarification_question = ""
            clarification_question_issue = ""
            clarification_question_repair_attempts = max(0, attempt - 1)
            clarification_body_dropped = 0
            clarification_author = None
            clarification_fallback_reason = None
            if clarification_active:
                (
                    parsed_body,
                    clarification_question,
                    clarification_question_issue,
                    clarification_body_dropped,
                ) = _parse_proposal_clarification_payload(
                    content,
                    language,
                    stage_context,
                )
                grounding_dropped_count += clarification_body_dropped
                if parsed_body:
                    clarification_body_candidate = parsed_body
                if clarification_question_issue and attempt < max_attempts:
                    raise ValueError(clarification_question_issue)
                if clarification_question_issue:
                    clarification_question = _proposal_clarification_fallback_question(
                        language,
                        stage_context,
                    )
                    clarification_fallback_reason = clarification_question_issue
                content = parsed_body or clarification_body_candidate
                if not content:
                    content = _proposal_clarification_fallback_message(
                        language,
                        stage_context,
                    )
                    clarification_fallback_used = True
                clarification_author = (
                    "server_fallback"
                    if clarification_fallback_used and clarification_question_issue
                    else "mixed"
                    if clarification_fallback_used or clarification_question_issue
                    else "kimi"
                )

            content = _sanitize_visible_model_text(
                _normalize_unsaved_change_claims(
                    _normalize_single_level_language(content.strip()),
                    language,
                ),
                language,
            )
            discussion_focus = _extract_plain_discussion_focus(
                content,
                language,
                stage_opening,
                stage_context,
            )
            visible_content, intent_hypothesis, proposal_offer, ui_cues = _extract_plain_guidance(
                content,
                language,
                stage_context,
                stage_opening,
                rows=rows,
                strict_metadata=validation_mode not in {"ordinary_chat", "route_discussion"},
            )
            if clarification_active:
                # The proposal topic and its one next question are server-owned.
                # Model questions and metadata cannot advance or redirect it.
                visible_content = _questionless_body(visible_content)
                intent_hypothesis = None
                proposal_offer = None
                ui_cues = []
                discussion_focus = None
            proposal_text = " ".join(
                part for part in (
                    visible_content,
                    (proposal_offer or {}).get("summary")
                    if isinstance(proposal_offer, dict)
                    else "",
                    (proposal_offer or {}).get("rationale")
                    if isinstance(proposal_offer, dict)
                    else "",
                )
                if str(part or "").strip()
            )
            if guidance_mode == "revision_advice" and _response_contains_multiple_proposals(proposal_text):
                if attempt < max_attempts:
                    raise ValueError(
                        "A single response may contain only one primary proposal."
                    )
                visible_content = _multiple_proposal_fallback_reply(language)
                intent_hypothesis = None
                proposal_offer = None
                ui_cues = []
            if stage_opening:
                visible_content, removed_grounding = _strip_invalid_stage_grounding_sentences(
                    visible_content,
                    rows,
                    historical_reference=historical_reference,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                )
                if removed_grounding:
                    grounding_dropped_count += len(removed_grounding)
                    _log_llm_event(
                        "llm_grounding_sentences_dropped",
                        requestId=request_id,
                        task=task,
                        sentenceCount=len(removed_grounding),
                        reasonCodes=[
                            item["reason"].split(":", 1)[0]
                            for item in removed_grounding
                        ],
                    )
                if not visible_content:
                    visible_content = _server_snapshot_fallback_message(
                        rows,
                        language,
                        stage_context=stage_context,
                        stage_opening=True,
                    )
                discussion_focus = _extract_plain_discussion_focus(
                    visible_content,
                    language,
                    stage_opening,
                    stage_context,
                )
            if rows and validation_mode in {"ordinary_chat", "route_discussion"}:
                visible_content, removed_grounding = _strip_invalid_grounding_sentences(
                    visible_content,
                    rows,
                    historical_reference=historical_reference,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                )
                if removed_grounding:
                    grounding_dropped_count += len(removed_grounding)
                    _log_llm_event(
                        "llm_grounding_sentences_dropped",
                        requestId=request_id,
                        task=task,
                        sentenceCount=len(removed_grounding),
                        reasonCodes=[
                            item["reason"].split(":", 1)[0]
                            for item in removed_grounding
                        ],
                    )
                if not visible_content:
                    clarification_fallback_used = clarification_active
                    visible_content = (
                        _proposal_clarification_fallback_message(language, stage_context)
                        if clarification_active
                        else _server_snapshot_fallback_message(
                            rows,
                            language,
                            stage_context=stage_context,
                        )
                    )

            # The plain compatibility path has no structured factRef envelope.
            # Never let it become a second authority for current coordinates or
            # entity relations; routes remain available only through the later
            # deterministic endpoint and BFS checks.
            visible_content = _strip_unreferenced_current_map_claims(visible_content)
            if not visible_content:
                clarification_fallback_used = clarification_active
                visible_content = (
                    _proposal_clarification_fallback_message(language, stage_context)
                    if clarification_active
                    else _server_snapshot_fallback_message(
                        rows,
                        language,
                        stage_context=stage_context,
                        stage_opening=stage_opening,
                    )
                )

            visible_content, route_recovery = _recover_overlong_content(
                visible_content,
                rows,
                language=language,
            )
            route_recovery["droppedSentenceCount"] += grounding_dropped_count
            if route_recovery["changed"]:
                discussion_focus = _extract_plain_discussion_focus(
                    visible_content,
                    language,
                    stage_opening,
                    stage_context,
                )
            coordinate_links = _extract_plain_coordinate_links(content, rows)
            design_context_patch, design_context_patch_error = (
                _extract_plain_design_context_patch(content)
            )
            if clarification_active:
                coordinate_links = []
                design_context_patch = None
                design_context_patch_error = None
            disagreement = _extract_plain_disagreement(
                content,
                language,
                stage_context,
            )
            proposal_binding_downgraded = False
            intent_hypothesis, proposal_offer, ui_cues, guidance_fallback_used = (
                _apply_deterministic_guidance_fallback(
                    semantic_messages,
                    visible_content,
                    language,
                    stage_context,
                    stage_opening,
                    intent_hypothesis,
                    proposal_offer,
                    ui_cues,
                    rows,
                    play_summary,
                    guidance_mode=guidance_mode,
                    allow_required_fallback=attempt >= max_attempts,
                )
            )
            if stage_opening:
                # Opening envelopes are observations only.  Keep the visible
                # analysis and any human-edit review metadata, but never allow
                # ordinary-chat intent/proposal/patch fields to escape here.
                intent_hypothesis = None
                proposal_offer = None
                design_context_patch = None
                design_context_patch_error = None
                if not _is_human_edit_stage_opening(True, stage_context):
                    ui_cues = []
            if proposal_offer is not None:
                proposal_offer = _distill_proposal_offer(
                    proposal_offer,
                    visible_content,
                    _latest_role_content(semantic_messages[:-1], "assistant"),
                    language,
                )
                if proposal_offer is not None and proposal_offer_requires_execution_brief(
                    proposal_offer,
                    visible_content,
                    proposal_offer.get("summary"),
                    proposal_offer.get("rationale"),
                    _latest_role_content(semantic_messages, "user"),
                ):
                    # Keep the model's visible prose, but do not expose a
                    # purple card that cannot be tied to a saved tile state. A
                    # conceptual chat reply must not spend its remaining wall
                    # clock on a second model call just to repair optional metadata.
                    proposal_offer = None
                    proposal_binding_downgraded = True
                    guidance_fallback_used = True
            if (
                guidance_mode == "revision_advice"
                and proposal_offer is None
                and attempt < max_attempts
                and not proposal_binding_downgraded
            ):
                raise ValueError(
                    "REVISION_ADVICE requires a substantive proposalOffer card."
                )
            visible_content = _remove_extracted_warning_sentence(
                visible_content,
                ui_cues,
            )
            visible_content_had_question = "?" in visible_content or "？" in visible_content
            body, question, pure_low_quality = _extract_plain_message_question(
                visible_content,
                language,
            )

            if clarification_active:
                body = _questionless_body(body)
                question = clarification_question
                pure_low_quality = False
                if not body:
                    clarification_fallback_used = True
                    body = _proposal_clarification_fallback_message(
                        language,
                        stage_context,
                    )
                    clarification_author = (
                        "server_fallback"
                        if clarification_question_issue
                        else "mixed"
                    )
                # Proposal clarification is visible conversation prose, never
                # a discussion card. Keep the private field only in diagnostics
                # so the server can count the question it actually displayed.
                body = "\n\n".join(
                    part for part in (body.strip(), question.strip()) if part
                )
                question = None

            if discussion_focus is not None:
                question = discussion_focus

            if pure_low_quality:
                raise LowQualityModelResponse(
                    "The model returned only a low-information question."
                )

            if question and _question_repeats_recent_judgment(question, semantic_messages):
                question = None

            if stage_opening and _is_stage_one(stage_context):
                body = _questionless_body(visible_content)
                question = None
                if not body:
                    raise LowQualityModelResponse(
                        "The Stage 1 opening contained only questions."
                    )
                body = _ensure_stage_one_orientation(body, rows, language)
            elif stage_opening and question is None:
                # A later saved Stage may expose a concrete uncertainty or
                # first-person judgment in ordinary prose.  Distill that
                # actual point; never substitute a stock water/box question.
                question = _perspective_discussion_focus(body, language)
            elif stage_opening and question is not None:
                try:
                    question = _normalize_opening_question(question)
                except ValueError:
                    body = visible_content
                    question = None

            if not stage_opening and question is not None:
                question = _refine_discussion_focus(
                    question,
                    visible_content,
                    language,
                )

            if (stage_context or {}).get("discussionCardMode") == "disagreement_only":
                if disagreement is None and not (stage_context or {}).get("activeDisagreement"):
                    if question:
                        body = "\n\n".join(
                            part for part in (body.strip(), question.strip()) if part
                        )
                    elif visible_content_had_question and body.strip() != visible_content.strip():
                        # New ordinary questions belong in the visible prose. The legacy
                        # extractor intentionally drops a low-information question from a
                        # declarative paragraph; restore that question when no structured
                        # disagreement is active instead of silently losing the turn's ask.
                        body = visible_content.strip()
                    question = None
                elif disagreement is not None and disagreement.get("status") == "active":
                    question = None

            active_context_disagreement = (stage_context or {}).get("activeDisagreement")
            if (
                disagreement is None
                and isinstance(active_context_disagreement, dict)
                and active_context_disagreement.get("status") == "active"
                and proposal_offer is None
            ):
                disagreement = active_context_disagreement

            if (
                disagreement is None
                and (stage_context or {}).get("discussionCardMode") == "disagreement_only"
                and (stage_context or {}).get("revisionRequestState") in {
                    "authorized", "needs_direction"
                }
            ):
                warning_text = next(
                    (
                        cue.get("text")
                        for cue in ui_cues
                        if cue.get("type") == "warning"
                    ),
                    None,
                )
                if warning_text:
                    disagreement = _disagreement_from_warning(
                        warning_text,
                        language,
                        stage_context,
                        user_position=_latest_role_content(semantic_messages, "user"),
                    )

            if (stage_context or {}).get("revisionRequestState") == "needs_direction":
                body = _unclear_revision_reply(language)
                question = None
                intent_hypothesis = _unclear_revision_intent(language)
                proposal_offer = None
                ui_cues = []
                guidance_fallback_used = True

            body = _normalize_response_paragraphs(body)

            guidance = {
                "move": _plain_guidance_move(
                    stage_opening,
                    intent_hypothesis,
                    proposal_offer,
                ),
                "intentHypothesis": intent_hypothesis,
                "intentConfidence": (
                    "low"
                    if (stage_context or {}).get("revisionRequestState") == "needs_direction"
                    else "medium" if intent_hypothesis else None
                ),
                "followUpQuestion": question,
                "proposalOffer": proposal_offer,
                "disagreement": disagreement,
                "uiCues": ui_cues[:2],
                "coordinateLinks": coordinate_links,
            }
            guidance = _sanitize_visible_guidance(guidance, language)
            if design_context_patch is not None:
                guidance["designContextPatch"] = design_context_patch
            if design_context_patch_error:
                guidance["designContextPatchError"] = design_context_patch_error
            if stage_opening:
                guidance = _sanitize_ordinary_grounding_metadata(
                    guidance,
                    rows,
                    historical_reference=historical_reference,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                )
                guidance["move"] = "observe_stage"
                guidance["intentHypothesis"] = None
                guidance["intentConfidence"] = None
                guidance["proposalOffer"] = None
                guidance.pop("designContextPatch", None)
                guidance.pop("designContextPatchError", None)
            guidance = _apply_guidance_card_policy(guidance)
            guidance = _ensure_required_guidance_card(
                guidance,
                semantic_messages,
                language,
                rows,
                stage_opening,
                stage_context,
                visible_content=visible_content,
                guidance_mode=guidance_mode,
            )
            body = _deduplicate_assistant_body(body)
            if (
                guidance_mode in {"revision_advice", "needs_clarification"}
                and not guidance.get("proposalOffer")
                and not re.search(r"[?？]", body)
            ):
                # A missing binding is resolved in the visible conversation,
                # never by manufacturing a failure card.
                body = "\n\n".join(
                    part for part in (
                        body,
                        _exact_revision_clarification(language),
                    ) if str(part or "").strip()
                )
            body = _remove_guidance_from_body(
                body,
                guidance.get("followUpQuestion"),
            )
            original_coordinate_link_count = len(guidance.get("coordinateLinks") or [])
            guidance["coordinateLinks"] = _filter_coordinate_links(
                guidance.get("coordinateLinks"),
                body,
                rows,
                (stage_context or {}).get("entityBindings"),
            )
            guidance["coordinateLinks"] = _recover_coordinate_links(
                body,
                rows,
                guidance["coordinateLinks"],
                (stage_context or {}).get("entityBindings"),
            )
            route_recovery["coordinateLinksDropped"] = (
                original_coordinate_link_count > len(guidance["coordinateLinks"])
            )
            proposal_binding_issue = _proposal_offer_binding_issue(
                guidance.get("proposalOffer"),
                body,
                semantic_messages,
                language,
            )
            # In the explicit advice route this is a hard contract. Ordinary chat may still
            # expose a model-authored proposal for backwards compatibility; the distiller above
            # already removes bare confirmations and transition metadata from that family.
            binding_is_required = guidance_mode == "revision_advice"
            if proposal_binding_issue and binding_is_required:
                if attempt < len(models[:CHAT_MAX_ATTEMPTS]):
                    raise ValueError(
                        f"proposalOffer binding failed: {proposal_binding_issue}"
                    )

                repaired = _repair_conceptual_proposal_binding(
                    semantic_messages,
                    body,
                    guidance.get("proposalOffer"),
                    visible_content,
                    rows,
                    language,
                    stage_context,
                )
                if repaired["proposalOffer"] is not None:
                    body = repaired["body"]
                    guidance["move"] = "offer_revision"
                    guidance["intentHypothesis"] = None
                    guidance["intentConfidence"] = None
                    guidance["followUpQuestion"] = None
                    guidance["proposalOffer"] = repaired["proposalOffer"]
                    guidance["uiCues"] = repaired["uiCues"]
                else:
                    body = repaired["body"]
                    guidance["move"] = "clarify_intent"
                    guidance["intentHypothesis"] = None
                    guidance["intentConfidence"] = None
                    guidance["proposalOffer"] = None
                    guidance["followUpQuestion"] = repaired["followUpQuestion"]
                    guidance["uiCues"] = repaired["uiCues"]
                guidance_fallback_used = True
            if validation_mode in {"ordinary_chat", "route_discussion"}:
                _validate_map_grounding_texts(
                    [body],
                    rows,
                    historical_reference=historical_reference,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                )
                guidance = _sanitize_ordinary_grounding_metadata(
                    guidance,
                    rows,
                    historical_reference=historical_reference,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                )
            else:
                grounding_texts = [
                    body,
                    guidance.get("followUpQuestion"),
                    guidance.get("intentHypothesis"),
                ]
                if guidance.get("proposalOffer"):
                    grounding_texts.extend(guidance["proposalOffer"].values())
                grounding_texts.extend(
                    cue.get("text") for cue in guidance.get("uiCues") or []
                )
                _validate_map_grounding_texts(
                    grounding_texts,
                    rows,
                    historical_reference=historical_reference,
                    entity_bindings=(stage_context or {}).get("entityBindings"),
                )
            evidence_signature = (stage_context or {}).get("guidanceEvidenceSignature")
            if evidence_signature and guidance["uiCues"]:
                guidance["evidenceSignature"] = evidence_signature
            assessment = (
                _build_minimal_stage_assessment(
                    body,
                    question,
                    language,
                    solver_metrics,
                )
                if stage_opening
                else {}
            )
            latency_ms = int((time.monotonic() - started_at) * 1000)
            opening_body = body
            if stage_opening and _is_human_edit_stage_opening(True, stage_context):
                opening_body = _compact_human_edit_opening_inventory(body, language)
            result = LLMExecutionResult(
                assistant_message=_compose_assistant_message(
                    _format_stage_opening_paragraphs(opening_body) if stage_opening else body,
                    guidance,
                    language,
                    stage_opening,
                    stage_context,
                ),
                assessment=assessment,
                proposed_rows=None,
                modification_summary="",
                attempts_used=attempt,
                request_id=request_id,
                model=model,
                latency_ms=latency_ms,
                guidance=guidance,
                proposal_diagnostics=(
                    {
                        "groundingSentencesDropped": grounding_dropped_count,
                        "clarificationRecoveryMode": (
                            "server_fallback"
                            if clarification_fallback_used and clarification_question_issue
                            else "question_replaced"
                            if clarification_question_issue
                            else "body_recovered"
                            if clarification_fallback_used
                            else "kimi_complete"
                        ),
                        "clarificationAuthor": (
                            "server_fallback"
                            if clarification_fallback_used and clarification_question_issue
                            else "mixed"
                            if clarification_fallback_used or clarification_question_issue
                            else "kimi"
                        ),
                        "clarificationTargetDimension": (
                            proposal_clarification or {}
                        ).get("questionKey"),
                        "clarificationQuestion": clarification_question,
                        "clarificationQuestionValidated": not bool(
                            clarification_question_issue
                        ),
                        "clarificationQuestionRepairAttempts": clarification_question_repair_attempts,
                        "clarificationFallbackReason": clarification_fallback_reason,
                        **_proposal_clarification_observability(
                            content,
                            clarification_question,
                            stage_context,
                        ),
                    }
                    if clarification_active
                    else {}
                ),
            )
            _log_llm_event(
                "llm_request_completed",
                requestId=request_id,
                task=task,
                outcome="success",
                model=model,
                attemptsUsed=attempt,
                latencyMs=latency_ms,
                responseMode="plain_text",
                guidanceMode=guidance_mode,
                guidanceFallbackUsed=guidance_fallback_used,
                intentCard=bool(guidance.get("intentHypothesis")),
                questionCard=bool(guidance.get("followUpQuestion")),
                proposalCard=bool(guidance.get("proposalOffer")),
                warningCard=any(
                    cue.get("type") in {"warning", "tradeoff"}
                    for cue in guidance.get("uiCues", [])
                ),
                manualEditCard=any(
                    cue.get("type") == "manual_edit"
                    for cue in guidance.get("uiCues", [])
                ),
                failureClass=None,
                salvageAction=route_recovery["salvageAction"],
                routeSentenceCount=route_recovery["routeSentenceCount"],
                routeCoordinateCount=route_recovery["routeCoordinateCount"],
                droppedSentenceCount=route_recovery["droppedSentenceCount"],
                coordinateLinksDropped=route_recovery["coordinateLinksDropped"],
                clarificationAuthor=(
                    "server_fallback"
                    if clarification_active and clarification_fallback_used and clarification_question_issue
                    else "mixed"
                    if clarification_active and (clarification_fallback_used or clarification_question_issue)
                    else "kimi"
                    if clarification_active
                    else None
                ),
                clarificationTargetDimension=(
                    (proposal_clarification or {}).get("questionKey")
                    if clarification_active
                    else None
                ),
                clarificationQuestionValidated=(
                    not bool(clarification_question_issue)
                    if clarification_active
                    else None
                ),
                clarificationQuestionRepairAttempts=(
                    clarification_question_repair_attempts
                    if clarification_active
                    else None
                ),
                clarificationFallbackReason=(
                    clarification_fallback_reason if clarification_active else None
                ),
                **(
                    _proposal_clarification_observability(
                        content,
                        clarification_question,
                        stage_context,
                    )
                    if clarification_active
                    else {}
                ),
                remainingSeconds=round(_remaining_until(deadline), 3),
                **response_fields,
            )
            return result
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "Kimi did not respond before the attempt timeout.",
                request_id,
                True,
                attempt,
                504,
            )
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)
            if last_error.code == "MODEL_RESPONSE_INVALID" and failure_reason:
                validation_feedback = failure_reason

        failure_fields = {
            "requestId": request_id,
            "task": task,
            "model": model,
            "attempt": attempt,
            "code": last_error.code,
            "retryable": last_error.retryable,
            "latencyMs": int((time.monotonic() - started_at) * 1000),
            "responseMode": "plain_text",
            **response_fields,
            **_provider_error_fields(last_error),
        }
        if failure_reason:
            failure_fields["validationReason"] = failure_reason
        failure_fields["failureClass"] = _llm_failure_class(
            last_error,
            validation_feedback,
        )
        failure_fields["remainingSeconds"] = round(_remaining_until(deadline), 3)
        failure_fields["salvageAction"] = "pending_retry_or_fallback"
        _log_llm_event(
            "llm_attempt_failed",
            **failure_fields,
        )

        if attempt >= max_attempts or not _plain_fallback_allowed(last_error):
            break

        if not _retry_budget_available(
            deadline,
            request_id=request_id,
            task=task,
            attempt=attempt,
            max_attempts=max_attempts,
            response_mode="plain_text",
            fallback_reason=last_error.code,
        ):
            break

    if last_error is not None:
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task=task,
            outcome="error",
            code=last_error.code,
            attemptsUsed=last_error.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="plain_text",
            failureClass=_llm_failure_class(last_error, validation_feedback),
            salvageAction="pending_fallback",
            routeSentenceCount=0,
            routeCoordinateCount=0,
            droppedSentenceCount=0,
            coordinateLinksDropped=False,
            remainingSeconds=round(_remaining_until(deadline), 3),
            **_provider_error_fields(last_error),
        )
        if stage_opening:
            # A Stage opening has already spent its one structured attempt and
            # one plain-text fallback attempt.  Do not turn a malformed fallback
            # into another Kimi request; return a complete, orientation-safe
            # opening instead.
            return _stage_opening_safe_execution(
                language=language,
                rows=rows,
                request_id=request_id,
                attempts_used=last_error.attempts_used,
                started_at=started_at,
                fallback_reason=last_error.code,
                stage_context=stage_context,
                solver_metrics=solver_metrics,
            )
        if _is_length_failure(last_error, validation_feedback):
            fallback_message = (
                _proposal_clarification_fallback_message(language, stage_context)
                if clarification_active
                else _safe_incomplete_chat_reply(
                    language,
                    stage_opening=stage_opening,
                    rows=rows,
                )
            )
            return LLMExecutionResult(
                assistant_message=fallback_message,
                assessment={},
                proposed_rows=None,
                modification_summary="",
                attempts_used=last_error.attempts_used,
                request_id=request_id,
                model="kimi-k2.6",
                latency_ms=int((time.monotonic() - started_at) * 1000),
                guidance={
                    "move": "observe_stage" if stage_opening else "clarify_intent",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": None,
                    "proposalOffer": None,
                    "disagreement": None,
                    "uiCues": [],
                    "coordinateLinks": [],
                },
                proposal_diagnostics=(
                    {
                        "groundingSentencesDropped": 0,
                        "clarificationRecoveryMode": "deterministic_clarification",
                    }
                    if clarification_active
                    else {}
                ),
            )
        if rows is not None and _is_map_grounding_failure(
            last_error.safe_message,
            validation_feedback,
        ):
            clarification = _safe_grounding_chat_reply(
                language,
                rows=rows,
                stage_context=stage_context,
            )
            return LLMExecutionResult(
                assistant_message=clarification,
                assessment={},
                proposed_rows=None,
                modification_summary="",
                attempts_used=last_error.attempts_used,
                request_id=request_id,
                # Keep the historical model label for integrations that use it
                # as a presentation hint; the actual UI cue is now the
                # neutral clarification card above.
                model="grounding-safe-chat-fallback",
                latency_ms=int((time.monotonic() - started_at) * 1000),
                guidance={
                    "move": "offer_perspective",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": None,
                    "proposalOffer": None,
                    "disagreement": None,
                    # Grounding recovery is ordinary body prose.  It must not
                    # manufacture the legacy "cannot form a verifiable
                    # proposal" card.
                    "uiCues": [],
                    "coordinateLinks": [],
                },
                proposal_diagnostics=(
                    {
                        "groundingSentencesDropped": 0,
                        "clarificationRecoveryMode": "deterministic_clarification",
                    }
                    if _has_proposal_clarification(stage_context)
                    else {}
                ),
            )
            if validation_mode in {"ordinary_chat", "route_discussion"}:
                return LLMExecutionResult(
                    assistant_message=_safe_grounding_chat_reply(
                        language,
                        rows=rows,
                        stage_context=stage_context,
                    ),
                    assessment={},
                    proposed_rows=None,
                    modification_summary="",
                    attempts_used=last_error.attempts_used,
                    request_id=request_id,
                    model="grounding-safe-chat-fallback",
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    guidance={
                        "move": "offer_perspective",
                        "intentHypothesis": None,
                        "intentConfidence": None,
                        "followUpQuestion": None,
                        "proposalOffer": None,
                        "disagreement": None,
                        "uiCues": [],
                        "coordinateLinks": [],
                    },
                )
            # A repeated coordinate/brief conflict is a grounding problem, not a
            # reason to manufacture a proposal card from an abstract fallback.
            # Return ordinary clarification prose so the designer can correct the
            # location or tile state explicitly.
            return LLMExecutionResult(
                assistant_message=(
                    "我发现刚才提到的具体坐标或格子状态与当前已保存地图对不上。"
                    "我不会猜测邻近格，也不会改动当前地图；请重新确认要修改的位置，"
                    "以及它当前是墙、地板、水域还是其他元素。"
                    if language == "zh-CN"
                    else "The precise coordinate or tile state in that suggestion does not match "
                    "the saved map. I will not guess a neighboring cell or change the map; please "
                    "confirm the location and whether it is currently a wall, floor, water, or entity."
                ),
                attempts_used=last_error.attempts_used,
                request_id=request_id,
                model="execution-brief-grounding-guard",
                latency_ms=int((time.monotonic() - started_at) * 1000),
                guidance={
                    "move": "clarify_intent",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": None,
                    "proposalOffer": None,
                    "disagreement": None,
                    "uiCues": [],
                },
            )
        raise last_error

    raise LLMServiceError(
        "INTERNAL_ERROR",
        "The LLM request ended unexpectedly.",
        request_id,
        False,
        0,
        500,
    )


async def _generate_with_model_fallback(
    *,
    api_key,
    base_url,
    models,
    messages,
    rows,
    max_tokens,
    request_id,
    task,
    language,
    assessment_only,
    proposal_validator,
    stage_context,
    started_at,
    max_attempts,
    deadline=None,
):
    last_error = None
    validation_feedback = None
    deadline = deadline or _request_deadline(started_at)

    for attempt, configured_model in enumerate(models[:max_attempts], start=1):
        model = (
            models[0]
            if task == "map_proposal" and validation_feedback is not None
            else configured_model
        )
        elapsed = time.monotonic() - started_at
        remaining = _remaining_until(deadline)
        response_fields = _empty_response_diagnostics()
        route_recovery = {
            "changed": False,
            "salvageAction": "none",
            "routeSentenceCount": 0,
            "routeCoordinateCount": 0,
            "droppedSentenceCount": 0,
            "coordinateLinksDropped": False,
        }

        if remaining <= 0:
            raise asyncio.TimeoutError()

        attempt_timeout = min(
            PRIMARY_ATTEMPT_TIMEOUT_SECONDS if attempt == 1 else remaining,
            remaining,
        )
        _log_llm_event(
            "llm_attempt_started",
            requestId=request_id,
            task=task,
            model=model,
            attempt=attempt,
            maxAttempts=len(models[:max_attempts]),
            timeoutSeconds=round(attempt_timeout, 3),
            responseMode="json_object",
        )

        try:
            attempt_messages = _messages_with_validation_feedback(
                messages,
                validation_feedback,
                task,
            )
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    attempt_messages,
                    max_tokens,
                    attempt_timeout,
                    task=task,
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            finish_reason = str(getattr(choice, "finish_reason", "") or "")

            if finish_reason == "length":
                raise ValueError("The model output reached its token limit.")

            response_fields = _response_diagnostics(response, choice)
            content = str(choice.message.content or "")

            if not content.strip():
                raise EmptyModelResponse("The model returned an empty response.")

            if len(content) > CHAT_RESPONSE_HARD_LENGTH * 2:
                raise ValueError("The model response exceeded the transport safety limit.")

            payload = json.loads(content)
            if assessment_only:
                payload = _canonicalize_stage_assessment_payload(payload, stage_context)
                if isinstance(payload.get("assistantMessage"), str):
                    payload = dict(payload)
                    payload["assistantMessage"], route_recovery = (
                        _recover_overlong_content(
                            payload["assistantMessage"],
                            rows,
                            language=language,
                        )
                    )
                    if route_recovery["changed"]:
                        _log_llm_event(
                            "llm_route_salvaged",
                            requestId=request_id,
                            task=task,
                            salvageAction=route_recovery["salvageAction"],
                            routeSentenceCount=route_recovery["routeSentenceCount"],
                            routeCoordinateCount=route_recovery["routeCoordinateCount"],
                            droppedSentenceCount=route_recovery["droppedSentenceCount"],
                        )
            validated = validate_chat_response(
                payload,
                assessment_only,
                language,
                stage_context,
                rows,
            )

            # The model-facing JSON may still describe a precise user request only in
            # the latest chat turn. Preserve that hard coordinate transition before
            # the conceptual offer is persisted, just as the plain-text path does.
            if validated[4].get("proposalOffer") is not None and proposal_offer_requires_execution_brief(
                validated[4]["proposalOffer"],
                validated[0],
                validated[4]["proposalOffer"].get("summary"),
                validated[4]["proposalOffer"].get("rationale"),
                _latest_role_content(messages, "user"),
            ):
                if attempt < len(models[:max_attempts]):
                    raise ValueError(
                        "An exact coordinate edit requires a server-validated executionBrief."
                    )
                validated[4]["proposalOffer"] = None
                if validated[4].get("move") == "offer_revision":
                    validated[4]["move"] = "offer_perspective"

            if task == "map_proposal" and validated[2] is None:
                raise ValueError(
                    "An explicitly requested map proposal requires proposedRows."
                )

            if validated[2] is not None and proposal_validator is not None:
                proposal_validator(validated[2])

            route_recovery["coordinateLinksDropped"] = (
                isinstance(payload.get("guidance"), dict)
                and len(payload["guidance"].get("coordinateLinks") or [])
                > len(validated[4].get("coordinateLinks") or [])
            )

            latency_ms = int((time.monotonic() - started_at) * 1000)
            result = LLMExecutionResult(
                assistant_message=_compose_assistant_message(
                    validated[0],
                    validated[4],
                    language,
                    assessment_only,
                    stage_context,
                ),
                assessment=validated[1],
                proposed_rows=validated[2],
                modification_summary=validated[3],
                attempts_used=attempt,
                request_id=request_id,
                model=model,
                latency_ms=latency_ms,
                guidance=validated[4],
            )
            opening_recovery = validated[4].get("openingRecovery") or []
            opening_presentation = dict(
                validated[4].get("openingPresentation") or {}
            )
            if assessment_only:
                opening_presentation["sentenceCount"] = len([
                    item.strip()
                    for item in re.split(
                        r"(?<=[.!?。！？])\s*", result.assistant_message or ""
                    )
                    if item.strip()
                ])
            _log_llm_event(
                "llm_request_completed",
                requestId=request_id,
                task=task,
                outcome="success",
                model=model,
                attemptsUsed=attempt,
                latencyMs=latency_ms,
                responseMode="json_object",
                failureClass=None,
                salvageAction=route_recovery["salvageAction"],
                routeSentenceCount=route_recovery["routeSentenceCount"],
                routeCoordinateCount=route_recovery["routeCoordinateCount"],
                droppedSentenceCount=route_recovery["droppedSentenceCount"],
                coordinateLinksDropped=route_recovery["coordinateLinksDropped"],
                bodyPreserved=bool(validated[0].strip()),
                openingRecoveryActions=opening_recovery,
                openingPresentation=opening_presentation,
                remainingSeconds=round(_remaining_until(deadline), 3),
                **response_fields,
            )
            return result
        except asyncio.TimeoutError as exception:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "Kimi did not respond before the attempt timeout.",
                request_id,
                True,
                attempt,
                504,
            )
        except Exception as exception:
            failure_reason = _safe_validation_reason(exception)
            last_error = classify_exception(exception, request_id, attempt)

            if last_error.code == "MODEL_RESPONSE_INVALID" and failure_reason:
                validation_feedback = failure_reason

        failure_fields = {
            "requestId": request_id,
            "task": task,
            "model": model,
            "attempt": attempt,
            "code": last_error.code,
            "retryable": last_error.retryable,
            "latencyMs": int((time.monotonic() - started_at) * 1000),
            "responseMode": "json_object",
            **_provider_error_fields(last_error),
        }

        failure_fields.update(response_fields)

        if failure_reason:
            failure_fields["validationReason"] = failure_reason
        failure_fields["failureClass"] = _llm_failure_class(
            last_error,
            validation_feedback,
        )
        failure_fields["salvageAction"] = "pending_retry"
        failure_fields["remainingSeconds"] = round(_remaining_until(deadline), 3)

        _log_llm_event(
            "llm_attempt_failed",
            **failure_fields,
        )

        if not last_error.retryable:
            raise last_error

        if attempt < max_attempts and not _retry_budget_available(
            deadline,
            request_id=request_id,
            task=task,
            attempt=attempt,
            max_attempts=max_attempts,
            response_mode="json_object",
            fallback_reason=last_error.code,
        ):
            break

    if last_error is not None:
        failure_class = _llm_failure_class(last_error, validation_feedback)
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task=task,
            outcome="error",
            code=last_error.code,
            attemptsUsed=last_error.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="json_object",
            failureClass=failure_class,
            salvageAction="none",
            routeSentenceCount=0,
            routeCoordinateCount=0,
            droppedSentenceCount=0,
            coordinateLinksDropped=False,
            remainingSeconds=round(_remaining_until(deadline), 3),
            **_provider_error_fields(last_error),
        )
        raise last_error

    raise LLMServiceError(
        "INTERNAL_ERROR",
        "The LLM request ended unexpectedly.",
        request_id,
        False,
        0,
        500,
    )


def _messages_with_validation_feedback(messages, validation_feedback, task=None):
    if not validation_feedback:
        return messages

    corrected = [dict(message) for message in messages]
    instruction = (
        "Your previous response for this same request was rejected for this structural "
        f"reason: {validation_feedback} Return a fresh complete JSON object that corrects "
        "that reason while following every original content and safety rule. Do not "
        "mention the retry or validation error to the designer."
    )
    if any(marker in str(validation_feedback).casefold() for marker in ("token limit", "too long", "cut off")):
        instruction += (
            " Preserve the detailed map/design analysis, but compress only the route reasoning "
            "to one short, verifiable passage; do not enumerate a full solver trace. End the "
            "assistantMessage with a complete sentence."
        )

    if task == "map_proposal":
        instruction += (
            " The designer explicitly authorized a complete map proposal, so keep "
            "guidance.move as deliver_revision, return non-null proposedRows and a "
            "truthful modificationSummary, and do not downgrade the response to an "
            "offer_revision or a text-only suggestion."
        )

    for message in corrected:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content', '')}\n\n{instruction}"
            return corrected

    corrected.insert(0, {"role": "system", "content": instruction})
    return corrected


def _plain_messages_with_validation_feedback(
    messages,
    validation_feedback,
    *,
    validation_mode="ordinary_chat",
    rows=None,
    stage_context=None,
):
    if not validation_feedback:
        return messages

    corrected = [dict(message) for message in messages]
    feedback_text = str(validation_feedback)
    feedback_lower = feedback_text.casefold()
    if _has_proposal_clarification(stage_context):
        specification = (stage_context or {}).get("proposalClarification") or {}
        instruction = (
            "Your previous private proposal-clarification envelope was rejected: "
            f"{feedback_text} Return fresh JSON with exactly body and question. Preserve "
            "the useful design interpretation, write exactly one question for target dimension "
            f"{specification.get('questionKey')}, and do not mention this repair. Do not ask "
            "for coordinates, cells, positions, adjacency, routes, or implementation details."
        )
    elif "token limit" in feedback_lower or "too long" in feedback_lower:
        instruction = (
            "Your previous reply for this same request was cut off or exceeded the visible "
            "response limit. Write a fresh, complete reply rather than continuing the partial "
            "text. Preserve the useful design observations, causes, consequences, and trade-offs; "
            "only compress the route reasoning into one short verifiable passage, without an "
            "exhaustive solver trace. Do not mention this correction to the designer. End with "
            "a complete conclusion."
        )
    elif "route reasoning" in feedback_lower:
        instruction = (
            "Your previous reply used an over-expanded route trace. Write a fresh reply that "
            "keeps the detailed design analysis, but reduces route reasoning to one short, "
            "grounded passage with only the key corridor, endpoint, and design consequence. "
            "Do not enumerate every coordinate, movement, alternative, BFS result, or solver "
            "state. Do not mention this correction to the designer."
        )
    elif "REVISION_ADVICE" in validation_feedback or "proposalOffer" in validation_feedback:
        instruction = (
            "Your previous reply for this same request was rejected because it omitted the "
            f"required guidance card: {validation_feedback} Write a fresh reply with a "
            "substantive PROPOSAL_SUMMARY and PROPOSAL_RATIONALE in the trailing GUIDANCE "
            "block, plus the required MANUAL_EDIT companion card. Keep it conceptual: do not "
            "output map rows, tile operations, or claim that the map changed. Do not mention "
            "this correction to the designer."
        )
    elif validation_mode in {"ordinary_chat", "route_discussion"}:
        instruction = (
            "Your previous reply contained a spatial claim that conflicts with deterministic map facts "
            "and is not verified for the current Stage. Write a fresh, complete "
            "design discussion that preserves the useful interpretation and removes only the unsupported "
            "coordinate, position, or optional card. Do not ask the designer to confirm a modification "
            "unless they explicitly requested an edit. If the designer referred to an older version, "
            "discuss it conceptually unless a historical snapshot is supplied. Do not mention this "
            "correction to the designer."
        )
    else:
        instruction = (
            "Your previous reply for this same request was rejected because it made a spatial "
            f"claim that conflicts with deterministic map facts: {validation_feedback} Write a "
            "fresh grounded reply. Use only verified entity IDs or coordinates for current map "
            "relations, and do not mention this correction to the designer."
        )
    if rows is not None and any(
        marker in feedback_lower
        for marker in (
            "spatial",
            "grounding",
            "map facts",
            "does not match",
            "saved map",
        )
    ):
        try:
            facts = json.loads(_map_facts_for_prompt(rows, stage_context))
            entities = list(facts.get("entities") or [])
            table = "\n".join(
                (
                    f"{item.get('id')}: row {item.get('row')}, column {item.get('column')} "
                    f"(identity {item.get('identityConfidence') or 'unknown'})"
                    if item.get("identityConfidence") == "exact"
                    else (
                        f"unlabelled {item.get('kind') or 'entity'} at row {item.get('row')}, "
                        f"column {item.get('column')} (identity unknown; do not use a historical label)"
                    )
                )
                for item in entities
                if item.get("row") and item.get("column")
            )
            if table:
                instruction += (
                    "\n\nGrounding failure type/details (use only for correction, do not mention it): "
                    f"{feedback_text[:500]}\n"
                    "Authoritative current entity table for this retry (do not reorder or infer it):\n"
                    f"{table}\n"
                    f"Current map fingerprint: {facts.get('mapFingerprint')}\n"
                    "Recheck every related entity and rewrite the complete reply using these exact bindings."
                )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    for message in corrected:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content', '')}\n\n{instruction}"
            return corrected
    corrected.insert(0, {"role": "system", "content": instruction})
    return corrected


def _safe_validation_reason(exception):
    if isinstance(exception, json.JSONDecodeError):
        return "The response was not a complete valid JSON object."

    if isinstance(exception, (ValueError, TypeError, KeyError)):
        reason = " ".join(str(exception).split())[:300]
        return reason or "The response did not match the required JSON contract."

    return None


def validate_translation_response(payload, source_items, target_language="en"):
    if not isinstance(payload, dict) or set(payload) != {"translations"}:
        raise ValueError("Translation output must contain only translations.")

    translations = payload["translations"]

    if not isinstance(translations, list) or len(translations) != len(source_items):
        raise ValueError("Translation output must contain one item for every source turn.")

    source_by_id = {item["turnId"]: item for item in source_items}
    expected_fields = {
        "turnId",
        "body",
        "followUpQuestion",
        "intentHypothesis",
        "proposalOfferSummary",
        "proposalOfferRationale",
        "uiCueTexts",
        "proposalSummary",
    }
    translated_by_id = {}

    for index, translation in enumerate(translations):
        source_links = source_items[index].get("coordinateLinks") or []
        item_expected_fields = expected_fields | (
            {"disagreement"} if "disagreement" in source_items[index] else set()
        ) | ({"coordinateLinkTexts"} if source_links else set())
        if not isinstance(translation, dict) or set(translation) != item_expected_fields:
            raise ValueError(f"Translation item {index} does not match the required fields.")

        turn_id = translation["turnId"]

        if turn_id not in source_by_id or turn_id in translated_by_id:
            raise ValueError("Translation turnIds must match the requested turns exactly.")

        source = source_by_id[turn_id]
        normalized = {"turnId": turn_id}
        normalized["body"] = _validate_translated_text(
            translation["body"],
            source["body"],
            f"translations[{index}].body",
            allow_empty=True,
        )
        normalized["body"] = _sanitize_visible_model_text(
            normalized["body"],
            target_language,
        )
        normalized["body"], _dropped_grounding = _strip_invalid_stage_grounding_sentences(
            normalized["body"],
            source.get("stageRows"),
            entity_bindings=source.get("entityBindings"),
        )
        if not normalized["body"] and source.get("body"):
            normalized["body"] = (
                "\u6211\u4f1a\u4ee5\u5f53\u524d\u4fdd\u5b58\u7684 Stage \u4e3a\u51c6\u7ee7\u7eed\u5206\u6790\u3002"
                if target_language == "zh-CN"
                else "I will continue from the current saved Stage."
            )

        for field_name in (
            "followUpQuestion",
            "intentHypothesis",
            "proposalOfferSummary",
            "proposalOfferRationale",
            "proposalSummary",
        ):
            normalized[field_name] = _validate_translated_text(
                translation[field_name],
                source[field_name],
                f"translations[{index}].{field_name}",
            )
            normalized[field_name] = _sanitize_visible_model_text(
                normalized[field_name],
                target_language,
            )

        source_cues = source["uiCueTexts"]
        translated_cues = translation["uiCueTexts"]

        if not isinstance(translated_cues, list) or len(translated_cues) != len(source_cues):
            raise ValueError("Translated uiCueTexts must preserve the source item count.")

        normalized["uiCueTexts"] = [
            _validate_translated_text(
                translated_text,
                source_cues[cue_index],
                f"translations[{index}].uiCueTexts[{cue_index}]",
            )
            for cue_index, translated_text in enumerate(translated_cues)
        ]
        normalized["uiCueTexts"] = [
            _sanitize_visible_model_text(text, target_language)
            for text in normalized["uiCueTexts"]
        ]
        if source_links:
            translated_link_texts = translation["coordinateLinkTexts"]
            if (
                not isinstance(translated_link_texts, list)
                or len(translated_link_texts) != len(source_links)
            ):
                raise ValueError(
                    "Translated coordinateLinkTexts must preserve the source item count."
                )
            normalized["coordinateLinkTexts"] = [
                _validate_translated_text(
                    translated_text,
                    source_links[link_index]["text"],
                    f"translations[{index}].coordinateLinkTexts[{link_index}]",
                )
                for link_index, translated_text in enumerate(translated_link_texts)
            ]
            normalized["coordinateLinkTexts"] = [
                _sanitize_visible_model_text(
                    text,
                    target_language,
                )
                for text in normalized["coordinateLinkTexts"]
            ]
            if any(
                link_text not in (normalized["body"] or "")
                for link_text in normalized["coordinateLinkTexts"]
            ):
                raise ValueError(
                    f"translations[{index}].coordinateLinkTexts must remain exact body substrings."
                )
        if "disagreement" in item_expected_fields:
            source_disagreement = source.get("disagreement")
            translated_disagreement = translation.get("disagreement")
            if source_disagreement is None:
                if translated_disagreement is not None:
                    raise ValueError(
                        f"translations[{index}].disagreement must remain null."
                    )
            else:
                normalized["disagreement"] = _validate_translated_disagreement(
                    translated_disagreement,
                    source_disagreement,
                    f"translations[{index}].disagreement",
                )
                for field_name in (
                    "userPosition",
                    "aiPosition",
                    "coreDisagreement",
                    "nextQuestion",
                ):
                    normalized["disagreement"][field_name] = _sanitize_visible_model_text(
                        normalized["disagreement"][field_name],
                        target_language,
                    )
        translated_by_id[turn_id] = normalized

    if set(translated_by_id) != set(source_by_id):
        raise ValueError("Translation output omitted one or more requested turns.")

    return [translated_by_id[item["turnId"]] for item in source_items]


def _validate_translated_disagreement(value, source, field_name):
    if not isinstance(value, dict) or set(value) != set(source):
        raise ValueError(f"{field_name} must preserve the disagreement fields.")
    for field in ("status", "subject", "resolution"):
        if value.get(field) != source.get(field):
            raise ValueError(f"{field_name}.{field} must remain unchanged.")
    result = {field: value[field] for field in ("status", "subject", "resolution")}
    for field in ("userPosition", "aiPosition", "coreDisagreement", "nextQuestion"):
        result[field] = _validate_translated_text(
            value.get(field),
            source.get(field),
            f"{field_name}.{field}",
        )
    return result


def _validate_translated_text(value, source_value, field_name, allow_empty=False):
    if source_value is None:
        if value is not None:
            raise ValueError(f"{field_name} must remain null.")

        return None

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    normalized = value.strip()

    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must not be empty.")

    maximum_length = max(400, len(str(source_value)) * 4 + 200)

    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name} is unexpectedly long.")

    return _normalize_single_level_language(normalized)


async def _request_completion(
    api_key,
    base_url,
    model,
    messages,
    max_completion_tokens,
    timeout_seconds,
    structured=True,
    task=None,
):
    client = _create_async_client(api_key, base_url, timeout_seconds)
    request_options = {
        "model": model,
        "messages": messages,
        # Every 8010 task uses direct generation. In particular, structured
        # RevisionPlan and operation-candidate requests must not spend their
        # completion budget on hidden reasoning before emitting JSON.
        "temperature": 0.6,
        "max_completion_tokens": max_completion_tokens,
        "stream": False,
        "extra_body": {
            "thinking": {
                "type": "disabled"
            }
        },
    }

    if structured:
        request_options["response_format"] = _structured_response_format(task)

    try:
        try:
            return await client.chat.completions.create(**request_options)
        except APIStatusError as exception:
            # Kimi deployments can differ in their JSON-schema rollout.  A
            # schema rejection is safe to retry once in JSON-object mode; the
            # response is still validated by the application before storage.
            status_code = getattr(exception, "status_code", None)
            if (
                structured
                and str(model).lower() == KIMI_MODEL
                and status_code in {400, 404, 422}
            ):
                request_options["response_format"] = {"type": "json_object"}
                return await client.chat.completions.create(**request_options)
            raise
    finally:
        await client.close()


def _canonicalize_stage_assessment_payload(payload, stage_context=None):
    """Apply endpoint-owned Stage opening fields before validation.

    Stage assessment is an observation, not a proposal or an intent-confirmation
    step.  Kimi sometimes reuses the ordinary-chat envelope and fills optional
    intent/proposal fields anyway.  Those fields are independently disposable;
    rejecting the whole response would throw away otherwise useful map analysis.
    Human-edit openings may still retain their warning/disagreement fields.
    """
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    guidance = payload.get("guidance")
    if isinstance(guidance, dict):
        guidance = dict(guidance)
        guidance["move"] = "observe_stage"
        guidance["intentHypothesis"] = None
        guidance["intentConfidence"] = None
        guidance["proposalOffer"] = None
        guidance.pop("designContextPatch", None)
        if not _is_human_edit_stage_opening(True, stage_context):
            # Regular Stage openings do not create warning cards from model
            # metadata.  Keep the prose and assessment, but drop optional card
            # metadata that the ordinary-chat envelope may have leaked in.
            guidance["uiCues"] = []
        normalized["guidance"] = guidance
    if "designContextPatch" in normalized:
        normalized["designContextPatch"] = None
    return normalized


def _content_block_sentences(value):
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s*|[\r\n]+", str(value or ""))
        if sentence.strip()
    ]


def _strip_unreferenced_current_map_claims(value):
    """Keep legacy analysis, but never display its free-form current map facts.

    The v2 content-block path renders those facts from StageSnapshot.  This
    conservative compatibility filter covers direct entity coordinates and
    present-tense spatial relations while preserving future/hypothetical route
    discussion and ordinary design interpretation.
    """
    current_fact = re.compile(
        r"(?:\b(?:P|B\d+|T\d+)\b.{0,36}?(?:\b(?:is|are|at|in|on|located|occupies|sits)\b|"
        r"\u4f4d\u4e8e|\u5728\u7b2c|\u5750\u6807).{0,40}?[\(\uff08]\s*\d{1,2}\s*[,\uff0c]\s*\d{1,2}\s*[\)\uff09]|"
        r"[\(\uff08]\s*\d{1,2}\s*[,\uff0c]\s*\d{1,2}\s*[\)\uff09].{0,28}?"
        r"(?:\b(?:is|are)\b|\u662f).{0,12}?\b(?:P|B\d+|T\d+)\b)",
        flags=re.IGNORECASE,
    )
    relation = re.compile(
        r"\b(?:P|B\d+|T\d+)\b.{0,42}?\b(?:near|close\s+to|adjacent\s+to|beside|next\s+to)\b.{0,42}?\b(?:P|B\d+|T\d+)\b|"
        r"(?:B\d+|T\d+|P).{0,24}?(?:\u9760\u8fd1|\u76f8\u90bb|\u65c1\u8fb9|\u7d27\u6328).{0,24}?(?:B\d+|T\d+|P)",
        flags=re.IGNORECASE,
    )
    future = re.compile(
        r"(?:\b(?:if|when|would|will|move|push|from|toward|through|via)\b|"
        r"\u5982\u679c|\u82e5|\u5c06|\u4f1a|\u79fb\u52a8|\u63a8\u5230|\u4ece.*(?:\u5230|\u5411))",
        flags=re.IGNORECASE,
    )
    kept = []
    for sentence in _content_block_sentences(value):
        if not future.search(sentence) and (current_fact.search(sentence) or relation.search(sentence)):
            continue
        kept.append(sentence)
    separator = "" if re.search(r"[\u3400-\u9fff]", str(value or "")) else " "
    return separator.join(kept).strip()


def _snapshot_entity_records(rows, stage_context=None):
    try:
        snapshot = build_stage_snapshot(
            rows,
            version_id=(stage_context or {}).get("versionId"),
            stage_number=(stage_context or {}).get("stageNumber"),
            entity_bindings=(stage_context or {}).get("entityBindings"),
        )
    except (TypeError, ValueError):
        return {}
    return {
        item.get("id"): item
        for item in snapshot.get("entities") or []
        if item.get("id") and item.get("identityConfidence") == "exact"
    }


def _server_fact_text(block, rows, stage_context, language, records):
    fact_type = str(block.get("factType") or "").strip()
    if fact_type == "entity_position":
        item = records.get(str(block.get("entity") or "").upper())
        if not item:
            return None
        if language == "zh-CN":
            return f"{item['id']}\u5f53\u524d\u4f4d\u4e8e\u7b2c{item['row']}\u884c\u7b2c{item['column']}\u5217\u3002"
        return f"{item['id']} is currently at row {item['row']}, column {item['column']}."
    if fact_type == "tile_state":
        row = block.get("row")
        column = block.get("column")
        if not isinstance(row, int) or not isinstance(column, int):
            return None
        if not rows or not (1 <= row <= len(rows) and 1 <= column <= len(rows[row - 1])):
            return None
        tile = rows[row - 1][column - 1]
        labels = {
            "#": ("\u5899\u4f53", "a wall"),
            ".": ("\u5730\u677f", "floor"),
            "w": ("\u6c34\u57df", "water"),
            "p": ("\u73a9\u5bb6", "the player"),
            "s": ("\u7bb1\u5b50", "a box"),
            "t": ("\u76ee\u6807", "a target"),
        }
        label = labels.get(tile)
        if not label:
            return None
        if language == "zh-CN":
            return f"\u7b2c{row}\u884c\u7b2c{column}\u5217\u662f{label[0]}\u3002"
        return f"Row {row}, column {column} is {label[1]}."
    return None


_PROVIDER_DETAIL_LIMIT = 360


def _sanitize_provider_message(value):
    """Return a short provider diagnostic without reflecting request secrets."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    # Error messages should not normally contain credentials, but provider
    # gateways occasionally echo a rejected header or URL.  Redact those
    # patterns before putting the message in audit data or a failure turn.
    text = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+|api[_ -]?key\s*[:=]\s*)\S+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b", "[redacted]", text)
    text = re.sub(r"https?://\S+", "[url redacted]", text)
    return text[:_PROVIDER_DETAIL_LIMIT]


def _provider_error_details(exception):
    """Extract only the provider error allowlist from an APIStatusError."""
    if not isinstance(exception, APIStatusError):
        return {}
    body = getattr(exception, "body", None)
    if not isinstance(body, dict):
        return {}
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    if not isinstance(error, dict):
        return {}
    details = {
        "providerStatus": int(getattr(exception, "status_code", 0) or 0) or None,
        "providerErrorType": _sanitize_provider_message(error.get("type")),
        "providerErrorCode": _sanitize_provider_message(error.get("code")),
        "providerParam": _sanitize_provider_message(error.get("param")),
        "providerMessage": _sanitize_provider_message(error.get("message")),
    }
    return {key: value for key, value in details.items() if value not in (None, "")}


def _provider_error_fields(error):
    """Map an LLMServiceError's safe provider details into audit fields."""
    return {
        "providerStatus": getattr(error, "provider_status", None),
        "providerErrorType": getattr(error, "provider_error_type", "") or None,
        "providerErrorCode": getattr(error, "provider_error_code", "") or None,
        "providerParam": getattr(error, "provider_param", "") or None,
        "providerMessage": getattr(error, "provider_message", "") or None,
    }


def _render_content_blocks(content_blocks, fallback_message, rows, stage_context, language):
    """Render only server-verified map facts; preserve model design analysis.

    A block is optional for compatibility.  Untrusted analysis remains useful,
    but direct current-map claims are removed unless they arrive as a factRef.
    """
    if not isinstance(content_blocks, list):
        return _strip_unreferenced_current_map_claims(fallback_message), []
    records = _snapshot_entity_records(rows, stage_context)
    rendered = []
    route_links = []
    for block in content_blocks[:12]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or "").strip()
        if kind == "factRef":
            text = _server_fact_text(block, rows, stage_context, language, records)
            if text:
                rendered.append(text)
            continue
        text = str(block.get("text") or "").strip()
        if not text or len(text) > CHAT_RESPONSE_HARD_LENGTH:
            continue
        text = _strip_unreferenced_current_map_claims(text)
        if not text:
            continue
        if kind == "routeReasoning":
            source = records.get(str(block.get("fromEntity") or "").upper())
            destination = records.get(str(block.get("toEntity") or "").upper())
            if not source or not destination:
                continue
            prefix = (
                f"{source['id']} \u2192 {destination['id']}\uff1a"
                if language == "zh-CN"
                else f"{source['id']} \u2192 {destination['id']}: "
            )
            rendered_text = f"{prefix}{text}"
            candidate = {
                "text": rendered_text,
                "from": {"row": source["row"], "column": source["column"]},
                "to": {"row": destination["row"], "column": destination["column"]},
            }
            if _coordinate_link_is_grounded(
                candidate, rows, (stage_context or {}).get("entityBindings")
            ):
                rendered.append(rendered_text)
                route_links.append(candidate)
            continue
        if kind in {"analysis", "personalReflection"}:
            rendered.append(text)
    if not rendered:
        return _strip_unreferenced_current_map_claims(fallback_message), []
    return "\n\n".join(rendered), route_links


def _sanitize_assessment_grounding(assessment, rows, stage_context, language, historical_reference):
    """Drop only ungrounded archival assessment fragments, never the whole reply."""
    if not assessment or not rows:
        return assessment
    bindings = (stage_context or {}).get("entityBindings")
    fallback = (
        "\u8fd9\u4e2a\u5f53\u524d Stage \u4ecd\u7136\u53ef\u4ee5\u4ece\u73a9\u5bb6\u7684\u7b2c\u4e00\u4e2a\u9009\u62e9\u7ee7\u7eed\u89c2\u5bdf\u3002"
        if language == "zh-CN"
        else "This current Stage can still be observed through the player's first meaningful choice."
    )
    result = dict(assessment)
    for key in ("solutionSummary", "difficultyOpinion"):
        value = str(result.get(key) or "").strip()
        if not value:
            result[key] = fallback
            continue
        cleaned, _removed = _strip_invalid_stage_grounding_sentences(
            value,
            rows,
            historical_reference=historical_reference,
            entity_bindings=bindings,
        )
        result[key] = cleaned or fallback
    for key in ("features", "suggestions"):
        kept = []
        for item in result.get(key) or []:
            cleaned, _removed = _strip_invalid_stage_grounding_sentences(
                item,
                rows,
                historical_reference=historical_reference,
                entity_bindings=bindings,
            )
            if cleaned:
                kept.append(cleaned)
        result[key] = kept or [fallback]
    question = result.get("satisfactionQuestion")
    if question:
        cleaned, _removed = _strip_invalid_stage_grounding_sentences(
            question,
            rows,
            historical_reference=historical_reference,
            entity_bindings=bindings,
        )
        result["satisfactionQuestion"] = cleaned or None
    return result


def _stage_opening_assessment_defaults(language):
    """Provide archival fields without making display metadata a hard failure."""
    if language == "zh-CN":
        observation = "这个当前 Stage 可以继续从第一次推动时的选择来观察。"
        difficulty = "在我看来，还需要结合实际试玩来判断它的体验难度。"
        feature = "当前布局中的首次推进判断"
        suggestion = "留意玩家最先读到的通道如何影响推动顺序"
    else:
        observation = "This current Stage can be observed through the first push choice."
        difficulty = "In my view, play evidence is still needed to judge the experienced difficulty."
        feature = "The first-push judgment in the current layout"
        suggestion = "Watch how the first readable corridor affects push order"
    return {
        "solutionSummary": observation,
        "difficultyOpinion": difficulty,
        "features": [feature],
        "suggestions": [suggestion],
        "satisfactionQuestion": None,
    }


def _opening_discussion_candidate(value, language):
    """Return a safe optional opening focus, never a leading question."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    question_marks = text.count("?") + text.count("？")
    if question_marks > 1:
        return None
    if question_marks == 1:
        try:
            return _normalize_opening_question(text)
        except ValueError:
            return None
    return text if _discussion_insight_is_useful(text, language) else None


def _split_opening_questions(message, follow_up_question, language, stage_one):
    """Keep declarative opening prose when question placement is imperfect.

    Questions in a Stage opening are presentation metadata, not map evidence.
    A leading, duplicated, or misplaced question must therefore be dropped (or
    moved to the discussion field), never make an otherwise grounded body fail.
    """
    paragraphs = []
    body_questions = []
    for paragraph in (part.strip() for part in str(message or "").split("\n\n")):
        if not paragraph:
            continue
        declarative = []
        for sentence in (
            part.strip()
            for part in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if part.strip()
        ):
            if sentence.endswith(("?", "？")):
                body_questions.append(sentence)
            else:
                declarative.append(sentence)
        if declarative:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            paragraphs.append(separator.join(declarative))

    body = "\n\n".join(paragraphs).strip()
    if stage_one:
        return body, None, ["opening_questions_removed"] if body_questions else []

    candidate = _opening_discussion_candidate(follow_up_question, language)
    actions = []
    if follow_up_question and candidate is None:
        actions.append("invalid_opening_focus_dropped")
    if candidate is None:
        for question in body_questions:
            candidate = _opening_discussion_candidate(question, language)
            if candidate is not None:
                actions.append("body_question_moved_to_discussion")
                break
    if body_questions and candidate is None:
        actions.append("leading_or_extra_questions_removed")
    elif len(body_questions) > 1:
        actions.append("extra_opening_questions_removed")
    return body, candidate, actions


def _recover_stage_opening_payload(payload, stage_context, language):
    """Normalize recoverable opening envelope errors before strict validation.

    This deliberately leaves map-grounding, revision execution, and human-edit
    risk checks to their existing validators.  It only makes display-only
    fields non-fatal so useful prose reaches those validators first.
    """
    if not isinstance(payload, dict):
        return payload, []

    actions = []
    allowed = {
        "assistantMessage", "contentBlocks", "guidance", "assessment",
        "proposedRows", "modificationSummary", "designContextPatch",
    }
    normalized = {key: value for key, value in payload.items() if key in allowed}
    if set(payload) != set(normalized):
        actions.append("unexpected_opening_fields_dropped")

    raw_guidance = payload.get("guidance")
    if not isinstance(raw_guidance, dict):
        raw_guidance = {}
        actions.append("malformed_opening_guidance_replaced")
    human_edit = _is_human_edit_stage_opening(True, stage_context)
    follow_up = _opening_discussion_candidate(
        raw_guidance.get("followUpQuestion"), language
    )
    if raw_guidance.get("followUpQuestion") and follow_up is None:
        actions.append("invalid_opening_focus_dropped")
    guidance = {
        "move": "observe_stage",
        "intentHypothesis": None,
        "intentConfidence": None,
        "followUpQuestion": follow_up,
        "proposalOffer": None,
        "disagreement": raw_guidance.get("disagreement") if human_edit else None,
        "uiCues": raw_guidance.get("uiCues") if human_edit else [],
        "coordinateLinks": [],
    }
    if (
        any(raw_guidance.get(key) is not None for key in (
            "intentHypothesis", "proposalOffer", "designContextPatch"
        ))
        or payload.get("designContextPatch") is not None
    ):
        actions.append("opening_metadata_dropped")

    if human_edit and (
        raw_guidance.get("uiCues") is not None
        or raw_guidance.get("disagreement") is not None
    ):
        # A verified human edit still receives its deterministic risk review,
        # but an unusable model card must not discard the opening body.  Keep
        # only a card that passes the same strict validator it would face
        # later; otherwise omit that optional presentation metadata.
        candidate = dict(guidance)
        candidate["followUpQuestion"] = None
        candidate["uiCues"] = raw_guidance.get("uiCues") or []
        candidate["disagreement"] = raw_guidance.get("disagreement")
        try:
            checked = _validate_guidance(
                candidate,
                True,
                language,
                stage_context,
                rows=(stage_context or {}).get("snapshotRows"),
            )
            guidance["uiCues"] = checked["uiCues"]
            guidance["disagreement"] = checked["disagreement"]
        except (TypeError, ValueError):
            guidance["uiCues"] = []
            guidance["disagreement"] = None
            actions.append("invalid_human_edit_risk_metadata_dropped")
    normalized["guidance"] = guidance
    normalized["proposedRows"] = None
    normalized["modificationSummary"] = ""
    normalized["designContextPatch"] = None

    defaults = _stage_opening_assessment_defaults(language)
    raw_assessment = payload.get("assessment")
    if isinstance(raw_assessment, dict):
        assessment = dict(defaults)
        for key in ("solutionSummary", "difficultyOpinion"):
            value = raw_assessment.get(key)
            if isinstance(value, str) and value.strip():
                assessment[key] = value
            elif value is not None:
                actions.append("opening_assessment_fields_normalized")
        for key in ("features", "suggestions"):
            value = raw_assessment.get(key)
            if isinstance(value, list) and value and all(
                isinstance(item, str) and item.strip() for item in value
            ):
                assessment[key] = value
            elif value is not None:
                actions.append("opening_assessment_fields_normalized")
        if set(raw_assessment) != set(assessment):
            actions.append("opening_assessment_fields_normalized")
    else:
        assessment = defaults
        actions.append("opening_assessment_rebuilt")
    normalized["assessment"] = assessment
    return normalized, actions


def validate_chat_response(
    payload,
    assessment_only=False,
    language="en",
    stage_context=None,
    rows=None,
    validation_mode="strict",
    historical_reference=False,
):
    opening_recovery_actions = []
    if assessment_only:
        payload = _canonicalize_stage_assessment_payload(payload, stage_context)
        payload, opening_recovery_actions = _recover_stage_opening_payload(
            payload, stage_context, language
        )
    if not isinstance(payload, dict):
        raise ValueError("The model response must be a JSON object.")

    allowed_payload_fields = {
        "assistantMessage",
        "contentBlocks",
        "guidance",
        "assessment",
        "proposedRows",
        "modificationSummary",
        "designContextPatch",
    }
    required_payload_fields = allowed_payload_fields - {"designContextPatch", "contentBlocks"}
    if not required_payload_fields.issubset(payload) or set(payload) - allowed_payload_fields:
        raise ValueError("The model response contains unexpected or missing fields.")

    raw_assistant_message = _clean_text(
        payload.get("assistantMessage"),
        "assistantMessage",
        maximum=CHAT_RESPONSE_HARD_LENGTH,
    )
    assistant_message, server_route_links = _render_content_blocks(
        payload.get("contentBlocks"),
        raw_assistant_message,
        rows,
        stage_context,
        language,
    )
    assistant_message = _sanitize_visible_model_text(
        _normalize_single_level_language(
            assistant_message
        ),
        language,
    )
    if assessment_only and rows:
        assistant_message, _removed_grounding = _strip_invalid_stage_grounding_sentences(
            assistant_message,
            rows,
            historical_reference=historical_reference,
            entity_bindings=(stage_context or {}).get("entityBindings"),
        )
        if not assistant_message:
            raise ValueError("A Stage opening has no grounded visible analysis left.")
    if assessment_only:
        stage_one = _is_stage_one(stage_context)
        assistant_message, recovered_focus, question_actions = _split_opening_questions(
            assistant_message,
            (payload.get("guidance") or {}).get("followUpQuestion"),
            language,
            stage_one,
        )
        opening_recovery_actions.extend(question_actions)
        payload = dict(payload)
        payload_guidance = dict(payload.get("guidance") or {})
        payload_guidance["followUpQuestion"] = recovered_focus
        payload["guidance"] = payload_guidance
        if not assistant_message:
            raise ValueError("A Stage opening has no grounded visible analysis left.")

    guidance = _validate_guidance(
        payload.get("guidance"),
        assessment_only,
        language,
        stage_context,
        rows=rows,
    )
    guidance = _sanitize_visible_guidance(guidance, language)
    if server_route_links:
        guidance["coordinateLinks"] = [
            *server_route_links,
            *(guidance.get("coordinateLinks") or []),
        ][:COORDINATE_LINK_LIMIT]
    patch_value = payload.get("designContextPatch")
    if patch_value is not None:
        try:
            guidance["designContextPatch"] = validate_design_context_patch(patch_value)
        except (TypeError, ValueError) as exception:
            guidance["designContextPatchError"] = str(exception)[:500]
    if (
        assessment_only
        and _is_human_edit_stage_opening(assessment_only, stage_context)
        and guidance.get("disagreement") is None
    ):
        warning_text = next(
            (
                cue.get("text")
                for cue in guidance.get("uiCues") or []
                if cue.get("type") in {"warning", "tradeoff"}
            ),
            None,
        )
        if warning_text:
            guidance["disagreement"] = _disagreement_from_warning(
                warning_text,
                language,
                stage_context,
            )
    if guidance.get("proposalOffer") is not None:
        guidance["proposalOffer"] = _distill_proposal_offer(
            guidance["proposalOffer"],
            assistant_message,
            "",
            language,
        )
    stage_one_opening = assessment_only and _is_stage_one(stage_context)
    deterministic_opening_question = None
    extracted_question = None

    if stage_one_opening:
        assistant_message = _questionless_body(assistant_message)
        if not assistant_message:
            raise ValueError("A Stage 1 opening must contain guidance outside its questions.")
        assistant_message = _ensure_stage_one_orientation(
            assistant_message,
            None,
            language,
        )
        guidance["followUpQuestion"] = None
        extracted_question = None
    elif not assessment_only:
        clarification_mode = classify_guidance_request(
            [
                {"role": "user", "content": ""},
            ],
            stage_context,
            stage_opening=assessment_only,
        )
        assistant_message, extracted_question = _extract_message_question(
            assistant_message,
            max_questions=(
                3 if clarification_mode == "needs_clarification" else 1
            ),
        )

    if extracted_question is not None:
        if guidance["followUpQuestion"] is None:
            if assessment_only:
                raise ValueError(
                    "A Stage opening question must use guidance.followUpQuestion."
                )

            guidance["followUpQuestion"] = extracted_question

    if guidance["followUpQuestion"] is not None:
        if not (
            _is_human_edit_stage_opening(assessment_only, stage_context)
            and _is_human_edit_intent_question(
                guidance["followUpQuestion"],
                language,
                stage_context,
            )
        ):
            guidance["followUpQuestion"] = _refine_discussion_focus(
                guidance["followUpQuestion"],
                assistant_message,
                language,
            )
            if _discussion_focus_repeats_recent(
                guidance["followUpQuestion"],
                stage_context,
            ):
                guidance["followUpQuestion"] = None

    if assessment_only and not stage_one_opening and guidance["followUpQuestion"] is None:
        guidance["followUpQuestion"] = _perspective_discussion_focus(
            assistant_message,
            language,
        )

    if _is_human_edit_stage_opening(assessment_only, stage_context):
        guidance["followUpQuestion"] = _human_edit_intent_discussion_focus(
            guidance.get("followUpQuestion"),
            stage_context,
            language,
        )

    if (
        assessment_only
        and (stage_context or {}).get("discussionCardMode") == "disagreement_only"
        and not (
            isinstance(guidance.get("disagreement"), dict)
            and guidance["disagreement"].get("status") == "active"
        )
    ):
        guidance["followUpQuestion"] = None

    opening_presentation = {}
    if assessment_only:
        opening_before_sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?。！？])\s*", assistant_message or "")
            if item.strip()
        ]
        if _is_human_edit_stage_opening(assessment_only, stage_context):
            assistant_message = _compact_human_edit_opening_inventory(
                assistant_message, language
            )
        opening_after_sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?。！？])\s*", assistant_message or "")
            if item.strip()
        ]
        compacted_inventory_count = max(
            0, len(opening_before_sentences) - len(opening_after_sentences)
        )
        if compacted_inventory_count:
            opening_recovery_actions.append("human_edit_inventory_compacted")
        assistant_message = _format_stage_opening_paragraphs(assistant_message)
        opening_presentation = {
            "sentenceCount": len(opening_after_sentences),
            "confirmationSentenceCount": sum(
                1
                for item in opening_after_sentences
                if re.search(r"(?:已保存|可解|确认|saved|solvable|confirmed)", item, re.IGNORECASE)
            ),
            "compactedInventorySentenceCount": compacted_inventory_count,
            "displayMode": "human_edit_balanced"
            if _is_human_edit_stage_opening(assessment_only, stage_context)
            else "stage_opening_balanced",
        }
    assessment_payload = payload.get("assessment")

    if assessment_payload is None:
        if assessment_only:
            raise ValueError("An assessment-only response requires assessment.")
        assessment = {}
    elif not isinstance(assessment_payload, dict):
        raise ValueError("assessment must be an object.")
    else:
        assessment = {
            "solutionSummary": _sanitize_visible_model_text(
                _normalize_single_level_language(_clean_text(
                    assessment_payload.get("solutionSummary"),
                    "assessment.solutionSummary",
                )),
                language,
            ),
            "difficultyOpinion": _sanitize_visible_model_text(
                _normalize_single_level_language(_clean_text(
                    assessment_payload.get("difficultyOpinion"),
                    "assessment.difficultyOpinion",
                )),
                language,
            ),
            "features": [
                _sanitize_visible_model_text(
                    _normalize_single_level_language(item),
                    language,
                )
                for item in _clean_list(assessment_payload.get("features"), "features")
            ],
            "suggestions": [
                _sanitize_visible_model_text(
                    _normalize_single_level_language(item),
                    language,
                )
                for item in _clean_list(
                    assessment_payload.get("suggestions"),
                    "suggestions",
                )
            ],
            "satisfactionQuestion": _sanitize_visible_model_text(
                _normalize_single_level_language(
                    _clean_optional_text(
                        assessment_payload.get("satisfactionQuestion"),
                        "assessment.satisfactionQuestion",
                    )
                ),
                language,
            ),
        }

        if stage_one_opening:
            assessment["satisfactionQuestion"] = None
        elif deterministic_opening_question is not None:
            assessment["satisfactionQuestion"] = deterministic_opening_question

        if assessment_only:
            # Persist the normalized card, not the model's pre-normalization wording.
            assessment["satisfactionQuestion"] = guidance["followUpQuestion"]

        if assessment_only and assessment["satisfactionQuestion"] is not None:
            satisfaction_marks = (
                assessment["satisfactionQuestion"].count("?")
                + assessment["satisfactionQuestion"].count("？")
            )
            if satisfaction_marks == 1:
                assessment["satisfactionQuestion"] = _normalize_opening_question(
                    assessment["satisfactionQuestion"]
                )
            elif satisfaction_marks > 1 or not _discussion_insight_is_useful(
                assessment["satisfactionQuestion"],
                language,
            ):
                raise ValueError(
                    "assessment.satisfactionQuestion must be one useful discussion focus."
                )

        if (
            assessment_only
            and assessment["satisfactionQuestion"] != guidance["followUpQuestion"]
        ):
            raise ValueError(
                "assessment.satisfactionQuestion must match guidance.followUpQuestion."
            )
    assessment = _sanitize_assessment_grounding(
        assessment,
        rows,
        stage_context,
        language,
        historical_reference,
    )
    if assessment_only and opening_recovery_actions:
        guidance["openingRecovery"] = sorted(set(opening_recovery_actions))
    if assessment_only:
        guidance["openingPresentation"] = opening_presentation
    proposed_rows = payload.get("proposedRows")

    if assessment_only and proposed_rows is not None:
        raise ValueError("An assessment-only response cannot propose a map.")

    if guidance["move"] == "offer_revision" and proposed_rows is not None:
        raise ValueError("A revision offer cannot include proposedRows.")

    if proposed_rows is not None and guidance["move"] != "deliver_revision":
        raise ValueError("proposedRows requires the deliver_revision move.")

    if guidance["move"] == "deliver_revision" and proposed_rows is None:
        raise ValueError("The deliver_revision move requires proposedRows.")

    proposal_text = " ".join(
        part for part in (
            assistant_message,
            (guidance.get("proposalOffer") or {}).get("summary")
            if isinstance(guidance.get("proposalOffer"), dict)
            else "",
            (guidance.get("proposalOffer") or {}).get("rationale")
            if isinstance(guidance.get("proposalOffer"), dict)
            else "",
        )
        if str(part or "").strip()
    )
    if guidance.get("proposalOffer") is not None and _response_contains_multiple_proposals(
        proposal_text
    ):
        raise ValueError("A single response may contain only one primary proposal.")

    if proposed_rows is not None:
        if not isinstance(proposed_rows, list) or not all(
            isinstance(row, str) for row in proposed_rows
        ):
            raise ValueError("proposedRows must be null or a list of strings.")

        proposed_rows = list(proposed_rows)

    assistant_message = _normalize_change_claims_for_proposal(
        assistant_message,
        language,
        proposed_rows is not None,
    )
    assistant_message, _content_recovery = _recover_overlong_content(
        assistant_message,
        rows,
        language=language,
    )
    assistant_message = _normalize_response_paragraphs(assistant_message)
    assistant_message = _remove_guidance_from_body(
        assistant_message,
        guidance.get("followUpQuestion"),
    )
    guidance["coordinateLinks"] = _filter_coordinate_links(
        guidance.get("coordinateLinks"),
        assistant_message,
        rows,
        (stage_context or {}).get("entityBindings"),
    )
    guidance["coordinateLinks"] = _recover_coordinate_links(
        assistant_message,
        rows,
        guidance["coordinateLinks"],
        (stage_context or {}).get("entityBindings"),
    )

    modification_summary = payload.get("modificationSummary", "")

    if not isinstance(modification_summary, str):
        raise ValueError("modificationSummary must be a string.")

    if rows is not None:
        if validation_mode in {"ordinary_chat", "route_discussion"}:
            _validate_map_grounding_texts(
                [assistant_message],
                rows,
                historical_reference=historical_reference,
                entity_bindings=(stage_context or {}).get("entityBindings"),
            )
            guidance = _sanitize_ordinary_grounding_metadata(
                guidance,
                rows,
                historical_reference=historical_reference,
                entity_bindings=(stage_context or {}).get("entityBindings"),
            )
        else:
            grounding_texts = [
                assistant_message,
                guidance.get("followUpQuestion"),
                guidance.get("intentHypothesis"),
                modification_summary,
            ]
            if guidance.get("proposalOffer"):
                grounding_texts.extend(guidance["proposalOffer"].values())
            if guidance.get("disagreement"):
                grounding_texts.extend(
                    guidance["disagreement"].get(field)
                    for field in (
                        "userPosition",
                        "aiPosition",
                        "coreDisagreement",
                        "nextQuestion",
                    )
                )
            grounding_texts.extend(
                cue.get("text") for cue in guidance.get("uiCues") or []
            )
            for value in assessment.values():
                grounding_texts.extend(value if isinstance(value, list) else [value])
            _validate_map_grounding_texts(
                grounding_texts,
                rows,
                historical_reference=historical_reference,
                entity_bindings=(stage_context or {}).get("entityBindings"),
            )

    return (
        assistant_message,
        assessment,
        proposed_rows,
        _sanitize_visible_model_text(
            _normalize_single_level_language(modification_summary.strip()),
            language,
        )[:1000],
        guidance,
    )


def _extract_message_question(message, max_questions=1):
    if "?" not in message and "？" not in message:
        return message, None

    declarative_paragraphs = []
    questions = []

    for paragraph in (part.strip() for part in message.split("\n\n")):
        if not paragraph:
            continue

        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if part.strip()
        ]
        declarative_sentences = []

        for sentence in sentences:
            if sentence.endswith(("?", "？")):
                questions.append(sentence)
            else:
                declarative_sentences.append(sentence)

        if declarative_sentences:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            declarative_paragraphs.append(separator.join(declarative_sentences))

    try:
        max_questions = max(1, int(max_questions))
    except (TypeError, ValueError):
        max_questions = 1

    if len(questions) > max_questions:
        raise ValueError(
            f"assistantMessage can contain at most {max_questions} question(s)."
        )

    if len(questions) > 1:
        # A clarification turn may need two or three independent missing inputs.
        # Keep those questions in ordinary body prose instead of forcing them
        # into the single legacy followUpQuestion/card field.
        return message, None

    if not declarative_paragraphs:
        raise ValueError(
            "assistantMessage must contain a declarative response outside its questions."
        )

    return "\n\n".join(declarative_paragraphs), questions[0] if questions else None


def _extract_plain_message_question(message, language="en"):
    """Extract one useful question and discard low-information questions beside prose."""
    if "?" not in message and "？" not in message:
        return message, None, False

    declarative_paragraphs = []
    questions = []

    for paragraph in (part.strip() for part in message.split("\n\n")):
        if not paragraph:
            continue

        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if part.strip()
        ]
        declarative_sentences = []

        for sentence in sentences:
            if sentence.endswith(("?", "？")):
                questions.append(sentence)
            else:
                declarative_sentences.append(sentence)

        if declarative_sentences:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            declarative_paragraphs.append(separator.join(declarative_sentences))

    question_marks = message.count("?") + message.count("？")

    if len(questions) == 1 and question_marks == 1:
        question = questions[0]
        useful = _question_is_specific_and_vivid(question, language)

        if not declarative_paragraphs:
            return ("", question, False) if useful else (message, None, True)

        if useful:
            return "\n\n".join(declarative_paragraphs), question, False

        return "\n\n".join(declarative_paragraphs), None, False

    if declarative_paragraphs and questions and all(
        not _question_is_specific_and_vivid(question, language)
        for question in questions
    ):
        return "\n\n".join(declarative_paragraphs), None, False

    return message, None, False


def _question_is_specific_and_vivid(question, language="en"):
    text = str(question or "").strip().casefold()

    if not text:
        return False

    generic_patterns = (
        r"^(?:你)?(?:觉得|认为)(?:这个|这条|该)?(?:方向|思路|方案)(?:如何|怎么样|可行吗)[？?]?$",
        r"^(?:你)?怎么看[？?]?$",
        r"^(?:这样|这)(?:可以|行|好吗|合适吗)[？?]?$",
        r"^(?:what do you think|does (?:this|that) (?:direction )?(?:work|seem good)|"
        r"is (?:this|that) (?:okay|ok|good|feasible))[?]?$",
    )

    if any(re.match(pattern, text, re.IGNORECASE) for pattern in generic_patterns):
        return False

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        anchors = ("水", "箱", "目标", "墙", "通道", "路线", "入口", "出口", "角落", "边缘", "水边")
        moments = ("推", "绕", "进入", "经过", "贴着", "到达", "转向", "第一次", "当", "卡住", "退回")
        judgments = ("选择", "顺序", "辨认", "理解", "注意", "判断", "读懂", "可读", "退路", "预期", "意识", "感到")
    else:
        anchors = ("water", "box", "crate", "target", "wall", "corridor", "route", "entrance", "corner", "edge")
        moments = ("push", "move", "enter", "pass", "along", "reach", "turn", "first", "when", "stuck", "return")
        judgments = ("choice", "order", "read", "notice", "judge", "understand", "clarity", "recognize", "expect", "realize")

    return (
        any(word in text for word in anchors)
        and any(word in text for word in moments)
        and any(word in text for word in judgments)
    )


def _question_repeats_recent_judgment(question, messages):
    previous_assistant = _latest_role_content(messages[:-1], "assistant")
    previous_questions = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", previous_assistant)
        if sentence.strip().endswith(("?", "？"))
    ]
    return any(_guidance_text_matches(question, previous) for previous in previous_questions)


def _normalize_single_level_language(value):
    """Keep model prose aligned with the one-level, multi-version study structure."""
    if value is None:
        return None

    text = str(value)
    chinese_ordinal = r"[零〇一二三四五六七八九十百两\d]+"
    text = re.sub(
        rf"第{chinese_ordinal}关(?:卡)?该有的",
        "这个版本现在呈现出的",
        text,
    )
    text = re.sub(r"(?:后面|之后|后续|未来)(?:的)?关卡", "后续版本", text)
    text = re.sub(r"(?:下一个|下一)关(?:卡)?", "下一个版本", text)
    text = re.sub(r"(?:上一个|上一)关(?:卡)?", "上一个版本", text)
    text = re.sub(r"前面(?:的)?关卡", "之前的版本", text)
    text = re.sub(rf"第{chinese_ordinal}关(?:卡)?", "这个 Stage", text)

    english_ordinal = (
        r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
        r"tenth|\d+(?:st|nd|rd|th))"
    )
    text = re.sub(
        rf"\b(?:the\s+)?{english_ordinal}\s+levels?\b",
        "this Stage",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:the\s+)?(?:next|following)\s+level\b",
        "the next version",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:later|future|subsequent|following)\s+levels\b",
        "later versions",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:the\s+)?previous\s+level\b",
        "the previous version",
        text,
        flags=re.IGNORECASE,
    )
    return text


_VISIBLE_INTERNAL_TERM_REPLACEMENTS = {
    "gridDistance": ("曼哈顿距离", "Manhattan distance"),
    "_solver": ("确定性求解器", "deterministic solver"),
    "solutionSteps": ("求解步数", "solution steps"),
    "solutionPushes": ("推动次数", "pushes"),
    "searchedStates": ("搜索状态数", "searched states"),
    "tileAt": ("当前格子", "current tile"),
    "mapFacts": ("当前地图事实", "current map facts"),
    "mapFingerprint": ("当前地图", "current map"),
    "orthogonallyAdjacentToWater": ("与水域正交相邻", "orthogonally adjacent to water"),
    "entityCoordinates": ("实体坐标", "entity coordinates"),
    "verifiedEntityChangesFromParent": ("已核对的版本变化", "verified version changes"),
    "designContextPatch": ("设计进度", "design progress"),
    "coordinateLinks": ("路线端点", "route endpoints"),
}


_VISIBLE_CHINESE_DESIGN_TERM_REPLACEMENTS = (
    (r"\bpeninsula\b", "\u534a\u5c9b"),
    (r"\bchoke\s+point\b", "\u74f6\u9888\u70b9"),
    (r"\bbottleneck\b", "\u74f6\u9888\u70b9"),
    (r"\bplayable\s+moment\b", "\u5173\u952e\u64cd\u4f5c\u65f6\u523b"),
    (r"\broute\s+choice\b", "\u8def\u7ebf\u9009\u62e9"),
    (r"\btrade[- ]off\b", "\u8bbe\u8ba1\u53d6\u820d"),
    (r"\bcorridor\b", "\u901a\u9053"),
    (r"\breadability\b", "\u53ef\u8bfb\u6027"),
    (r"\bpush\s+order\b", "\u63a8\u7bb1\u987a\u5e8f"),
    (r"\bdeadlock\b", "\u6b7b\u5c40"),
    (r"\bcluster\b", "\u805a\u96c6\u533a"),
)


def _sanitize_visible_model_text(value, language="en"):
    """Remove implementation vocabulary that must never become visible prose."""
    if value is None:
        return None

    text = str(value)
    text = re.sub(r"```(?:json|text|plaintext)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"`([^`\r\n]+)`", r"\1", text)
    language_index = 0 if language == "zh-CN" else 1
    for token, replacements in _VISIBLE_INTERNAL_TERM_REPLACEMENTS.items():
        replacement = replacements[language_index]
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
            replacement,
            text,
        )
    if language == "zh-CN":
        for pattern, replacement in _VISIBLE_CHINESE_DESIGN_TERM_REPLACEMENTS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()


def _sanitize_visible_guidance(guidance, language="en"):
    """Sanitize public guidance while preserving backend-only context patches."""
    result = dict(guidance or {})
    for field_name in ("intentHypothesis", "followUpQuestion"):
        if result.get(field_name) is not None:
            result[field_name] = _sanitize_visible_model_text(
                result[field_name], language
            )

    offer = result.get("proposalOffer")
    if isinstance(offer, dict):
        offer = dict(offer)
        for field_name in ("summary", "rationale"):
            if offer.get(field_name) is not None:
                offer[field_name] = _sanitize_visible_model_text(
                    offer[field_name], language
                )
        result["proposalOffer"] = offer

    disagreement = result.get("disagreement")
    if isinstance(disagreement, dict):
        disagreement = dict(disagreement)
        for field_name in (
            "userPosition",
            "aiPosition",
            "coreDisagreement",
            "nextQuestion",
        ):
            if disagreement.get(field_name) is not None:
                disagreement[field_name] = _sanitize_visible_model_text(
                    disagreement[field_name], language
                )
        result["disagreement"] = disagreement

    cues = result.get("uiCues")
    if isinstance(cues, list):
        result["uiCues"] = [
            {
                **cue,
                "text": _sanitize_visible_model_text(cue.get("text"), language),
            }
            if isinstance(cue, dict)
            else cue
            for cue in cues
        ]

    links = result.get("coordinateLinks")
    if isinstance(links, list):
        result["coordinateLinks"] = [
            {
                **link,
                "text": _sanitize_visible_model_text(link.get("text"), language),
            }
            if isinstance(link, dict)
            else link
            for link in links
        ]

    # designContextPatch and its error are intentionally untouched: they are
    # consumed by the server and removed by app._public_guidance before storage.
    return result


def _normalize_unsaved_change_claims(value, language="en"):
    text = str(value or "")
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        text = re.sub(
            r"(?:^|(?<=[。！？\n]))\s*(?:好[，,。！!]?\s*)?"
            r"(?:我)?(?:已经|已)?(?:帮你)?(?:改好了|改完了|修改完成了)"
            r"[，,。！!]*(?:你)?(?:可以)?(?:去|先)?试试(?:看)?[。！!]?",
            "我先把这个修改方向说具体，生成可审查的方案后再由你决定是否采用。",
            text,
        )
        text = re.sub(
            r"(?:^|(?<=[。！？\n]))\s*(?:我)?(?:已经|已)(?:帮你)?(?:改好|修改|调整)了[。！!]?",
            "我现在说的是修改方向，还没有替你保存地图。",
            text,
        )
        text = re.sub(
            r"(?:^|(?<=[。！？\n]))\s*(?:好[，,。！!]?\s*)?(?:那)?我(?:就)?(?:按[^。！？\n]{0,48})?"
            r"把[^。！？\n]{0,72}(?:改成|改为|变成|连成|移到|挪到)[^。！？\n]*[。！？!]?",
            "我已经理解这个修改方向；生成出可审查地图前，它还没有实际落到地图上。",
            text,
        )
        text = re.sub(
            r"(?:这份|这个)?(?:提案|方案)(?:会|将)?(?:保持|处于)[^。！？\n]{0,24}"
            r"(?:待审查|审查状态)[。！？!]?",
            "如果你明确授权生成，它会先以待审查提案的形式出现。",
            text,
        )
    else:
        text = re.sub(
            r"(?:^|(?<=[.!?\n]))\s*(?:done[.!]?\s*)?(?:i(?:'ve| have)?|we(?:'ve| have)?)"
            r"\s+(?:changed|modified|revised|updated|finished)\s+(?:it|this|the map|the level)"
            r"[.!]*(?:\s+(?:go ahead and )?(?:try|play)(?: it)?[.!]?)?",
            "I am describing the revision direction for now; I have not saved a map change.",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"(?:^|(?<=[.!?\n]))\s*(?:okay[, ]+)?i(?: will|'ll)?\s+"
            r"(?:change|modify|revise|update)\b[^.!?\n]*",
            "I understand the revision direction; no map has been generated or saved yet.",
            text,
            flags=re.IGNORECASE,
        )
    return text.strip()


def _normalize_change_claims_for_proposal(value, language, has_proposed_rows):
    if not has_proposed_rows:
        return _normalize_unsaved_change_claims(value, language)

    text = str(value or "")
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        text = re.sub(
            r"我按(.{0,80}?)(?:方向|思路|方案)?改[：:]",
            r"我按\1方向做了一份修改提案：",
            text,
        )
        text = re.sub(
            r"(?:我)?(?:已经|已)?(?:帮你)?(?:改好了|改完了|修改完成了)",
            "我做了一份可审查的修改提案",
            text,
        )
        text = re.sub(r"改完的版本", "这份待审查提案", text)
        text = re.sub(r"修改后的版本", "这份待审查提案", text)
        text = re.sub(
            r"(?:你)?(?:可以)?(?:去|先)?试试(?:看)?",
            "你可以先看提案的实际差异，再决定是否接受",
            text,
        )
    else:
        text = re.sub(
            r"\bI (?:changed|revised|updated) it (?:as|by|to)\b",
            "I made a revision proposal by",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+"
            r"(?:changed|modified|revised|updated|finished)\s+(?:it|this|the map|the level)\b",
            "I made a reviewable revision proposal",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:the )?(?:changed|revised|updated|finished) version\b",
            "this pending proposal",
            text,
            flags=re.IGNORECASE,
        )
    return text.strip()


def _deterministic_key_question(messages, language, rows):
    latest = _latest_role_content(messages, "user")
    lowered = latest.casefold()
    if not latest or "?" in latest or "？" in latest:
        return None

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", latest):
        evaluation = any(
            marker in latest
            for marker in ("我觉得", "我认为", "感觉", "显得", "只是", "太", "不够", "像")
        )
        direction = _latest_user_states_direction(messages)
        if not (evaluation or direction):
            return None
        if "水" in latest and "箱" in latest:
            return "当箱子第一次贴着水边推进后，哪一段路线最该让玩家意识到水域正在影响判断？"
        if "水" in latest:
            return "当箱子第一次经过水域边缘时，哪条路线应该最先让玩家读出水域并不只是背景？"
        if "箱" in latest and "目标" in latest:
            return "当箱子第一次朝目标推进时，哪个落点最该帮助玩家读懂接下来的推动顺序？"
        if "通道" in latest or "路线" in latest:
            return "当箱子第一次进入这条通道时，哪个转折最该让玩家看清后续路线选择？"
        return None

    evaluation = bool(
        re.search(r"\b(?:i think|i feel|feels?|seems?|looks?|too|only|just|not enough)\b", lowered)
    )
    direction = _latest_user_states_direction(messages)
    if not (evaluation or direction):
        return None
    if any(word in lowered for word in ("water", "pond")) and any(
        word in lowered for word in ("box", "crate")
    ):
        return (
            "When the box first moves along the water edge, which part of the route should "
            "make the player notice that the water is shaping their decision?"
        )
    if any(word in lowered for word in ("water", "pond")):
        return (
            "When the box first passes the water edge, which route should make the player "
            "recognize that the water is more than scenery?"
        )
    if any(word in lowered for word in ("box", "crate")) and any(
        word in lowered for word in ("target", "goal")
    ):
        return (
            "When the box first moves toward the target, which landing point should help "
            "the player read the next push order?"
        )
    if any(word in lowered for word in ("corridor", "route")):
        return (
            "When the box first enters that corridor, which turn should help the player "
            "read the route choice ahead?"
        )
    return None


def _deterministic_reply_discussion_focus(visible_content, language):
    text = str(visible_content or "")
    lowered = text.casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        if "水" in text and "箱" in text:
            return (
                "试玩时，请直接看箱子第一次贴着调整后的水边推进：在推之前，玩家能不能看清该先向上绕开，"
                "还是沿水边继续下推？如果还是只有一条很显眼的走法，说明这次调整还没有带来真正需要判断的路线。"
            )
        if "目标" in text and "箱" in text:
            return (
                "试玩时，请看箱子第一次靠近目标的那一推：玩家能不能马上判断该先处理哪只箱子、从哪一侧接近目标？"
                "如果仍能不经思考地一路推到底，说明目标附近还需要补出一个会影响顺序的局部选择。"
            )
        if "墙" in text and any(word in text for word in ("通道", "路线", "转折")):
            return (
                "试玩时，请看箱子第一次进入这段墙边通道：玩家会不会在入口停下来确认下一步该往哪边走？"
                "如果只是多走几步却没有新的判断，说明墙的位置只增加了距离，没有改变路线。"
            )
        if "目标" in text and "箱" in text and any(
            word in text for word in ("下方", "下半", "底部", "右下", "空旷")
        ):
            return (
                "我想先陪你看下方箱子第一次朝新目标推进时，玩家会不会因为绕路自然停一下想一想。"
                "这个瞬间已经有意思，我们就保留大结构；如果还是太直白，再一起改路线。"
            )
        if "水" in text and "箱" in text:
            return (
                "我想先陪你看箱子第一次贴着水边推进时，会不会自然感到路线得重新想一下。"
                "这个小停顿能告诉我们，水域真的参与了判断，还是只是摆在旁边。"
            )
        if "目标" in text and any(word in text for word in ("移动", "挪", "落位", "落点")):
            return (
                "我想先看看箱子第一次靠近新目标时，玩家会不会自然读到新的推进顺序。"
            )
        if "墙" in text and any(word in text for word in ("通道", "路线", "绕", "转折")):
            return (
                "我想先陪你看箱子第一次走进这段墙边通道时，会不会自然停下来重新想路线。"
                "这能告诉我们，墙是真的让路线更有意思，还是只是多走了几步。"
            )
        return None

    if "target" in lowered and any(word in lowered for word in ("box", "crate")) and any(
        word in lowered for word in ("lower", "bottom", "open area")
    ):
        return (
            "When the lower box first moves toward the new target, which step best reveals the "
            "new detour burden and the rhythm difference between the upper and lower areas? "
            "I would use that moment to judge whether to tune the route or preserve the space."
        )
    if any(word in lowered for word in ("water", "pond")) and any(
        word in lowered for word in ("box", "crate")
    ):
        return (
            "When the box first moves beside the water, which turn should make the player "
            "realize that it is changing the route? I would use that moment to judge whether "
            "the linkage is actually working."
        )
    if "target" in lowered and any(word in lowered for word in ("move", "shift", "position")):
        return (
            "When the box first approaches the adjusted target, which move should make the new "
            "push order readable?"
        )
    if "wall" in lowered and any(word in lowered for word in ("corridor", "route", "turn")):
        return (
            "When the box first passes the adjusted wall corridor, I would watch where the player "
            "pauses to reread the route."
        )
    return None


def _discussion_focus_is_grounded_in_reply(focus, visible_content, language):
    value = str(focus or "").strip()
    content = str(visible_content or "").strip()
    if not value or not content:
        return False

    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", f"{value}{content}")
    if chinese:
        components = ("水", "墙", "箱", "目标", "玩家", "通道", "路线", "开局", "区域")
        moments = (
            "第一次", "推", "绕", "入口", "进入", "靠近", "顺序", "停", "选择", "往哪", "哪边",
            "判断", "处理", "放下",
        )
        minimum_length = 16
    else:
        components = ("water", "wall", "box", "crate", "target", "goal", "player", "corridor", "route", "opening", "area")
        moments = ("first", "push", "turn", "approach", "order", "pause", "choose", "which", "where")
        minimum_length = 36

    lowered_value = value.casefold()
    lowered_content = content.casefold()
    shared_components = [
        component for component in components
        if component in lowered_value and component in lowered_content
    ]
    return (
        len(value) >= minimum_length
        and bool(shared_components)
        and any(moment in lowered_value for moment in moments)
    )


def _perspective_discussion_focus(visible_content, language):
    """Distill a concrete judgment or uncertainty already present in the reply."""
    sentences = [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", str(visible_content or ""))
        if sentence.strip()
    ]
    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", str(visible_content or ""))

    if chinese:
        uncertainty = re.compile(
            r"(?:我唯一有点拿不准|我有点拿不准|我不太确定|我还不确定|我担心|不清楚|难以判断)"
        )
        anchors = ("水", "墙", "箱", "目标", "T1", "T2", "通道", "路线", "入口", "推")
        for index, sentence in enumerate(sentences):
            if not uncertainty.search(sentence) or not any(anchor in sentence for anchor in anchors):
                continue
            related = [sentence.rstrip("。！!？?")]
            if index + 1 < len(sentences) and any(
                anchor in sentences[index + 1] for anchor in anchors
            ):
                related.append(sentences[index + 1].rstrip("。！!？?"))
            claim = "。".join(related)
            return (
                f"我想先把这点看清：{claim}。试玩时，重点看它是否真的让玩家改变推箱顺序；"
                "这个结果会决定下一步该保留当前入口，还是继续调整附近的局部空间。"
            )[:1000]

        marker = re.compile(
            r"(?:我(?:更)?倾向于认为|我倾向于觉得|在我看来|我觉得|我感觉|"
            r"我(?:比较|更)?在意的是|我(?:更)?喜欢的是|我(?:更)?喜欢)"
        )
        anchors = ("水", "墙", "箱", "目标", "通道", "路线", "开局", "推")
        for sentence in sentences:
            match = marker.search(sentence)
            if not match or not any(anchor in sentence for anchor in anchors):
                continue
            claim = sentence[match.end():].strip(" ：:，,").rstrip("。！!？?")
            if len(claim) < 12:
                continue
            return (
                f"我会先把这版理解为：{claim}。试玩时，重点看玩家第一次把箱子推到相关位置时，"
                "会不会因为这个变化停下来重新判断顺序；这能验证上面的取舍是否真的落在操作上。"
            )[:1000]
        for sentence in sentences:
            if (
                not any(anchor in str(visible_content or "") for anchor in anchors)
                or "还是" not in sentence
                or not any(marker in sentence for marker in ("这个选择", "这次改动", "这个改动", "这会", "取舍"))
            ):
                continue
            claim = sentence.rstrip("。！!？?")
            return (
                f"我会先把这版的重点放在这个取舍上：{claim}。试玩时，重点看玩家第一次把箱子推到"
                "调整区域附近时，会不会因为水域、墙缝或目标位置停下来重新判断顺序；这能看出它是在形成"
                "有效的中段判断，还是只增加了绕路。"
            )[:1000]
        return None

    marker = re.compile(
        r"\b(?:I tend to think|I am inclined to think|I think|I feel|in my view)\b",
        re.IGNORECASE,
    )
    anchors = ("water", "wall", "box", "crate", "target", "goal", "corridor", "route", "opening", "push")
    uncertainty = re.compile(
        r"\b(?:i am not sure|i'm not sure|i am uncertain|i worry|i am concerned|unclear|hard to tell)\b",
        re.IGNORECASE,
    )
    for sentence in sentences:
        if not uncertainty.search(sentence) or not any(anchor in sentence.casefold() for anchor in anchors):
            continue
        claim = sentence.rstrip(".!?")
        return (
            f"I want to keep this uncertainty in view: {claim}. During play, watch whether it "
            "actually changes the push order; that tells us whether to preserve the current "
            "opening or adjust the nearby space."
        )[:1000]

    for sentence in sentences:
        match = marker.search(sentence)
        if not match or not any(anchor in sentence.casefold() for anchor in anchors):
            continue
        claim = sentence[match.end():].strip(" :,").rstrip(".!?")
        if len(claim) < 18:
            continue
        return (
            f"My reading of this version is: {claim}. During play, watch whether the first push "
            "at that changed area makes the player stop and reconsider the order; that will show "
            "whether this trade-off is actually present in play."
        )[:1000]

    for sentence in sentences:
        lowered_sentence = sentence.casefold()
        if (
            not any(anchor in lowered_sentence for anchor in anchors)
            or not any(marker in lowered_sentence for marker in ("this trade-off", "this choice", "this change"))
            or " or " not in lowered_sentence
        ):
            continue
        claim = sentence.rstrip(".!?")
        return (
            f"I would keep the focus on this trade-off: {claim}. During play, watch whether the "
            "first push near the changed area makes the player reconsider the order; that will show "
            "whether it creates a meaningful mid-game judgment rather than only a detour."
        )[:1000]
    return None


def _reply_needs_clarifying_question(visible_content, language):
    text = str(visible_content or "").casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        uncertainty_markers = (
            "不清楚", "不确定", "看不出", "难以判断", "需要确认", "还不够明确",
            "会不会", "是否", "我担心", "拿不准", "不太确定",
        )
    else:
        uncertainty_markers = (
            "unclear", "uncertain", "not sure", "hard to tell", "need to confirm",
            "i worry", "i am concerned", "whether", "not yet clear",
        )
    return any(marker in text for marker in uncertainty_markers)


def _refine_discussion_focus(focus, visible_content, language):
    value = re.sub(r"\s+", " ", str(focus or "")).strip()
    if not value:
        return value

    perspective_focus = _perspective_discussion_focus(visible_content, language)
    if perspective_focus:
        return perspective_focus

    question_marks = value.count("?") + value.count("？")
    if question_marks and not _reply_needs_clarifying_question(visible_content, language):
        return None

    if _discussion_focus_is_grounded_in_reply(value, visible_content, language):
        return value[:1000]

    # A card may be more specific than the body, but it must remain visibly
    # grounded in this reply.  Do not manufacture a generic water/box prompt
    # merely to fill the blue card slot.
    return None


def _extract_plain_discussion_focus(
    content,
    language,
    stage_opening=False,
    stage_context=None,
):
    if stage_opening:
        return None

    value = str(content or "")
    marker_index = value.find("<GUIDANCE>")
    closing_index = value.find("</GUIDANCE>", marker_index + 10)
    if marker_index < 0 or closing_index < 0:
        return None

    focus = None
    block = value[marker_index + len("<GUIDANCE>"):closing_index]
    for raw_line in re.split(r"\s*\|\|\s*|[\r\n]+", block):
        match = re.match(r"^DISCUSS\s*:\s*(.+?)\s*$", raw_line.strip(), re.IGNORECASE)
        if match:
            focus = _normalize_single_level_language(match.group(1).strip())[:1000]
            break
    if not focus:
        return None

    question_marks = focus.count("?") + focus.count("？")
    useful = (
        _question_is_specific_and_vivid(focus, language)
        if question_marks == 1
        else question_marks == 0 and _discussion_insight_is_useful(focus, language)
    )
    visible = value[:marker_index].strip()
    if (
        not useful
        or _discussion_focus_repeats_recent(focus, stage_context)
        or _guidance_reuses_visible_sentence(focus, visible)
    ):
        return None
    return focus


def _discussion_insight_is_useful(text, language="en"):
    value = str(text or "").strip()
    if not 18 <= len(value) <= 320 or "?" in value or "？" in value:
        return False

    lowered = value.casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value):
        first_person = any(marker in value for marker in ("我", "在我看来", "对我来说"))
        anchors = ("水", "箱", "目标", "墙", "通道", "路线", "推动", "落点", "转折", "玩家")
        judgments = ("选择", "顺序", "读", "判断", "节奏", "犹豫", "取舍", "注意", "自由", "压力")
    else:
        first_person = bool(re.search(r"\b(?:i|my|me)\b", lowered)) or "to me" in lowered
        anchors = (
            "water", "box", "crate", "target", "wall", "corridor", "route",
            "push", "landing", "turn", "player",
        )
        judgments = (
            "choice", "order", "read", "judg", "rhythm", "hesitat", "trade-off",
            "notice", "freedom", "pressure",
        )
    return (
        first_person
        and any(anchor in lowered for anchor in anchors)
        and any(judgment in lowered for judgment in judgments)
    )


def _extract_plain_guidance(
    content,
    language,
    stage_context,
    stage_opening=False,
    rows=None,
    strict_metadata=True,
):
    marker = "<GUIDANCE>"
    marker_index = content.find(marker)

    if marker_index < 0:
        return content.strip(), None, None, []

    visible = content[:marker_index].strip()

    if not visible:
        raise ValueError("The natural-language reply must contain visible text.")

    if stage_opening:
        return visible, None, None, []

    block_tail = content[marker_index + len(marker):]
    closing_index = block_tail.find("</GUIDANCE>")

    if closing_index < 0:
        return visible, None, None, []

    fields = {}

    for raw_line in re.split(r"\s*\|\|\s*|[\r\n]+", block_tail[:closing_index]):
        match = re.match(
            r"^(WARNING|MANUAL_EDIT|CLARIFICATION|INTENT|PROPOSAL_SUMMARY|PROPOSAL_RATIONALE|EXECUTION_BRIEF|REVISION_PLAN)\s*:\s*(.+?)\s*$",
            raw_line.strip(),
        )

        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()

    intent = fields.get("INTENT")

    if intent:
        intent = _normalize_intent_hypothesis(intent[:1000], language)
        if _intent_is_only_execution_authorization(intent, language):
            intent = None

    summary = fields.get("PROPOSAL_SUMMARY")
    rationale = fields.get("PROPOSAL_RATIONALE")
    execution_brief = None
    revision_plan = None
    proposal_metadata_invalid = False
    if fields.get("REVISION_PLAN"):
        try:
            revision_plan = _validate_revision_plan_payload(
                json.loads(fields["REVISION_PLAN"]),
                rows,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exception:
            if rows is not None and strict_metadata:
                raise ValueError(
                    f"revisionPlan is invalid for the saved Stage: {exception}"
                ) from exception
            proposal_metadata_invalid = True
            revision_plan = None
    if fields.get("EXECUTION_BRIEF"):
        try:
            execution_brief = _validate_execution_brief(
                json.loads(fields["EXECUTION_BRIEF"]),
                rows,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exception:
            if rows is not None and strict_metadata:
                raise ValueError(
                    f"executionBrief is invalid for the saved Stage: {exception}"
                ) from exception
            proposal_metadata_invalid = True
            execution_brief = None
    if revision_plan is not None:
        execution_brief = revision_plan
    proposal_offer = (
        {
            "summary": summary[:600],
            "rationale": rationale[:1000],
            **({"executionBrief": execution_brief} if execution_brief else {}),
        }
        if summary and rationale and not proposal_metadata_invalid
        else None
    )
    recent_guidance = (stage_context or {}).get("recentGuidance") or {}
    evidence_signature = (stage_context or {}).get("guidanceEvidenceSignature")

    if intent and _guidance_text_matches(
        intent,
        recent_guidance.get("intentHypothesis"),
    ):
        intent = None

    previous_offer = recent_guidance.get("proposalOffer") or {}

    if proposal_offer and _guidance_text_matches(
        f"{proposal_offer['summary']} {proposal_offer['rationale']}",
        f"{previous_offer.get('summary', '')} {previous_offer.get('rationale', '')}",
    ):
        proposal_offer = None

    ui_cues = []
    previous_cues = recent_guidance.get("uiCues") or {}

    for field_name, cue_type in (
        ("WARNING", "warning"),
        ("MANUAL_EDIT", "manual_edit"),
        ("CLARIFICATION", "clarification"),
    ):
        cue_text = str(fields.get(field_name) or "").strip()[:1000]
        previous = previous_cues.get(cue_type) or {}

        if not cue_text:
            continue
        if cue_type == "warning" and not _warning_text_is_evidence_grounded(cue_text, language):
            continue
        if cue_type == "manual_edit" and not _manual_edit_text_is_contextual(cue_text, language):
            continue
        if (
            _guidance_text_matches(cue_text, previous.get("text"))
            and previous.get("evidenceSignature") == evidence_signature
        ):
            continue
        ui_cues.append({"type": cue_type, "text": cue_text})

    return visible, intent, proposal_offer, ui_cues[:2]


def _extract_plain_coordinate_links(content, rows=None):
    """Parse optional route annotations without changing visible plain-text replies."""
    marker = "<GUIDANCE>"
    marker_index = str(content or "").find(marker)
    if marker_index < 0:
        return []

    block_tail = str(content)[marker_index + len(marker):]
    closing_index = block_tail.find("</GUIDANCE>")
    if closing_index < 0:
        return []

    for raw_line in re.split(r"\s*\|\|\s*|[\r\n]+", block_tail[:closing_index]):
        match = re.match(
            r"^COORDINATE_LINKS\s*:\s*(.+?)\s*$",
            raw_line.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return _normalize_coordinate_links(value, rows=rows)

    return []


def _extract_plain_design_context_patch(content):
    """Read the optional internal patch from the plain-text metadata line."""
    marker = re.search(
        r"DESIGN_CONTEXT_PATCH\s*:\s*(\{.*?\})(?:\s*\|\||\s*</GUIDANCE>)",
        str(content or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if marker is None:
        return None, None
    try:
        return validate_design_context_patch(json.loads(marker.group(1))), None
    except (TypeError, ValueError, json.JSONDecodeError) as exception:
        return None, str(exception)[:500]


def _extract_plain_disagreement(content, language, stage_context=None):
    """Parse the optional structured disagreement from the legacy plain-text path."""
    marker = re.search(
        r"DISAGREEMENT\s*:\s*(\{.*?\})(?:\s*\|\||\s*</GUIDANCE>)",
        str(content or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if marker is None:
        return None
    try:
        value = json.loads(marker.group(1))
    except (TypeError, json.JSONDecodeError):
        return None
    try:
        return _validate_disagreement(value, language)
    except ValueError:
        return None


def _warning_text_is_evidence_grounded(text, language):
    lowered = str(text or "").casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", lowered):
        anchors = ("水", "箱", "目标", "墙", "通道", "路线", "入口", "退路", "推动", "绕行")
        risks = (
            "可能", "担心", "风险", "值得", "我有点在意", "我不太放心",
            "死锁", "卡住", "误读", "重复", "顺序", "退路",
        )
    else:
        anchors = (
            "water", "box", "crate", "target", "wall", "corridor", "route",
            "entrance", "escape", "push", "player", "opening", "position",
            "only", "block",
        )
        risks = (
            "may", "might", "concern", "risk", "worth", "i notice", "i am uneasy",
            "deadlock", "stuck", "misread", "repeat", "order", "escape",
            "close", "block", "trap", "lock",
        )
    return any(word in lowered for word in anchors) and any(word in lowered for word in risks)


def _manual_edit_text_is_contextual(text, language):
    lowered = str(text or "").casefold()
    exact_coordinate = re.search(
        r"(?:第\s*\d+\s*(?:行|列|格)|\b(?:row|column|col)\s*\d+\b|\bx\s*=\s*\d+|\by\s*=\s*\d+)",
        lowered,
    )
    if exact_coordinate:
        return False
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", lowered):
        anchors = ("水", "箱", "目标", "墙", "通道", "路线", "区域", "边缘", "编辑器")
        experiments = ("尝试", "实验", "调整", "移动", "改动", "试玩")
        observations = ("观察", "比较", "确认", "看看", "判断", "是否")
    else:
        anchors = ("water", "box", "crate", "target", "wall", "corridor", "route", "area", "edge", "editor")
        experiments = ("try", "experiment", "adjust", "move", "edit", "playtest")
        observations = ("observe", "watch", "compare", "confirm", "judge", "whether")
    return (
        any(word in lowered for word in anchors)
        and any(word in lowered for word in experiments)
        and any(word in lowered for word in observations)
    )


def _remove_extracted_warning_sentence(visible_content, ui_cues):
    warning_texts = [
        cue.get("text", "")
        for cue in ui_cues
        if cue.get("type") == "warning"
    ]
    result = str(visible_content or "")
    for warning in warning_texts:
        candidates = [warning]
        if ":" in warning or "：" in warning:
            candidates.append(re.split(r"[:：]", warning, maxsplit=1)[1].strip())
        for candidate in candidates:
            if candidate and candidate in result:
                result = result.replace(candidate, "", 1)
                break
    result = re.sub(r"[ \t]+\n", "\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _deduplicate_assistant_body(value):
    """Keep one natural copy of a repeated model paragraph or sentence."""
    paragraphs = []

    for raw_paragraph in str(value or "").split("\n\n"):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if sentence.strip()
        ]
        kept_sentences = []

        for sentence in sentences:
            if not any(_guidance_text_matches(sentence, kept) for kept in kept_sentences):
                kept_sentences.append(sentence)

        separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
        candidate = separator.join(kept_sentences).strip()

        if candidate and not any(
            _guidance_text_matches(candidate, previous)
            for previous in paragraphs
        ):
            paragraphs.append(candidate)

    return "\n\n".join(paragraphs)


def _legacy_normalize_response_paragraphs(value):
    """Balance visible prose without dropping sentences or route details."""
    body = _deduplicate_assistant_body(value)
    if not body:
        return body

    def sentences(paragraph):
        return [
            sentence.strip()
            for sentence in re.split(
                r"(?<=[.!?銆傦紒锛焆])\s*|(?<=[;；])\s+|[\r\n]+",
                paragraph,
            )
            if sentence.strip()
        ]

    def is_cjk(text):
        return bool(re.search(r"[\u3400-\u9fff]", text))

    def too_large(items):
        text = "".join(items) if is_cjk("".join(items)) else " ".join(items)
        if is_cjk(text):
            return len(text) > CHAT_PARAGRAPH_MAX_CHINESE_CHARS
        return len(re.findall(r"\b\w+\b", text)) > CHAT_PARAGRAPH_MAX_LATIN_WORDS

    groups = []
    pending = []

    def flush():
        nonlocal pending
        if pending:
            groups.append(list(pending))
            pending = []

    for raw_paragraph in body.split("\n\n"):
        paragraph_sentences = sentences(raw_paragraph.strip())
        if not paragraph_sentences:
            continue
        for sentence in paragraph_sentences:
            if pending and (
                len(pending) >= 4
                or too_large(pending + [sentence])
            ):
                flush()
            pending.append(sentence)
        # Keep multi-sentence source paragraphs semantically separate. Single-sentence
        # paragraphs remain pending so a reply made of many short fragments is regrouped.
        if len(paragraph_sentences) > 1:
            flush()
    flush()

    def render(items):
        separator = "" if is_cjk("".join(items)) else " "
        return separator.join(items).strip()

    rendered = [render(group) for group in groups if render(group)]
    while len(rendered) > CHAT_MAX_PARAGRAPHS:
        merge_index = next(
            (
                index for index in range(len(rendered) - 1)
                if len(sentences(rendered[index])) == 1
                or len(sentences(rendered[index + 1])) == 1
            ),
            None,
        )
        if merge_index is None:
            break
        merged = [rendered[merge_index], rendered[merge_index + 1]]
        separator = "" if is_cjk("".join(merged)) else " "
        rendered[merge_index:merge_index + 2] = [separator.join(merged)]

    return "\n\n".join(rendered).strip()


def _normalize_response_paragraphs(value):
    """Balance visible prose without dropping sentences or route details."""
    body = _deduplicate_assistant_body(value)
    if not body:
        return body

    def sentences(paragraph):
        return [
            sentence.strip()
            for sentence in re.split(
                r"(?<=[.!?\u3002\uff01\uff1f])\s*|(?<=[;\uff1b])\s*|[\r\n]+",
                paragraph,
            )
            if sentence.strip()
        ]

    def is_cjk(text):
        return bool(re.search(r"[\u3400-\u9fff]", text))

    def too_large(items):
        text = "".join(items) if is_cjk("".join(items)) else " ".join(items)
        if is_cjk(text):
            return len(text) > CHAT_PARAGRAPH_MAX_CHINESE_CHARS
        return len(re.findall(r"\b\w+\b", text)) > CHAT_PARAGRAPH_MAX_LATIN_WORDS

    groups = []
    pending = []

    def flush():
        nonlocal pending
        if pending:
            groups.append(list(pending))
            pending = []

    for raw_paragraph in body.split("\n\n"):
        paragraph_sentences = sentences(raw_paragraph.strip())
        if not paragraph_sentences:
            continue
        for sentence in paragraph_sentences:
            if pending and (
                len(pending) >= 4
                or too_large(pending + [sentence])
            ):
                flush()
            pending.append(sentence)
        if len(paragraph_sentences) > 1:
            flush()
    flush()

    def render(items):
        separator = "" if is_cjk("".join(items)) else " "
        return separator.join(items).strip()

    rendered = [render(group) for group in groups if render(group)]
    while len(rendered) > CHAT_MAX_PARAGRAPHS:
        merge_index = next(
            (
                index for index in range(len(rendered) - 1)
                if len(sentences(rendered[index])) + len(sentences(rendered[index + 1])) <= 4
            ),
            None,
        )
        if merge_index is None:
            merge_index = min(
                range(len(rendered) - 1),
                key=lambda index: len(sentences(rendered[index]))
                + len(sentences(rendered[index + 1])),
            )
        merged = [rendered[merge_index], rendered[merge_index + 1]]
        separator = "" if is_cjk("".join(merged)) else " "
        rendered[merge_index:merge_index + 2] = [separator.join(merged)]

    return "\n\n".join(rendered).strip()


def _remove_guidance_from_body(value, guidance_text):
    """Cards are rendered separately, so their thought must not remain in the body."""
    body = _deduplicate_assistant_body(value)
    guidance = str(guidance_text or "").strip()

    if not body or not guidance:
        return body

    paragraphs = []

    for paragraph in body.split("\n\n"):
        if _guidance_text_matches(paragraph, guidance):
            continue
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if sentence.strip()
        ]
        kept = [
            sentence for sentence in sentences
            if not _guidance_text_matches(sentence, guidance)
        ]
        if kept:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            paragraphs.append(separator.join(kept))

    return "\n\n".join(paragraphs).strip() or body


def _recent_discussion_focuses(stage_context):
    recent_guidance = ((stage_context or {}).get("recentGuidance") or {})
    history = recent_guidance.get("discussionFocusHistory") or []

    if not isinstance(history, (list, tuple)):
        history = []

    values = [str(item).strip() for item in history if str(item or "").strip()]
    latest = str(recent_guidance.get("discussionFocus") or "").strip()

    if latest and not any(_guidance_text_matches(latest, item) for item in values):
        values.insert(0, latest)

    return values[:3]


def _discussion_focus_repeats_recent(focus, stage_context):
    return any(
        _guidance_text_matches(focus, previous)
        for previous in _recent_discussion_focuses(stage_context)
    )


def _guidance_text_matches(current, previous):
    def normalize(value):
        return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "").casefold())

    current_text = normalize(current)
    previous_text = normalize(previous)

    if not current_text or not previous_text:
        return False

    return (
        current_text == previous_text
        or SequenceMatcher(None, current_text, previous_text).ratio() >= 0.88
    )


def _guidance_reuses_visible_sentence(card_text, visible_content):
    card = re.sub(r"\s+", " ", str(card_text or "")).strip()
    if not card:
        return False
    sentences = [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", str(visible_content or ""))
        if sentence.strip()
    ]
    normalized_card = re.sub(r"[^\w\u3400-\u9fff]+", "", card.casefold())
    for sentence in sentences:
        normalized_sentence = re.sub(
            r"[^\w\u3400-\u9fff]+",
            "",
            sentence.casefold(),
        )
        if not normalized_sentence:
            continue
        if normalized_card in normalized_sentence or normalized_sentence in normalized_card:
            return True
        if SequenceMatcher(None, normalized_card, normalized_sentence).ratio() >= 0.76:
            return True
    return False


def _proposal_card_is_meta_language(value):
    text = " ".join(str(value or "").split()).strip().casefold()
    if not text:
        return False

    chinese_patterns = (
        r"这个判断.{0,30}(?:影响|决定).{0,35}(?:接下来|下一步).{0,30}(?:建议|方案|调整|怎么)",
        r"(?:接下来|下一步)(?:我|我们)?(?:会|将|再)?(?:建议|调整|讨论)",
        r"(?:我会|我将)根据(?:这个|该)(?:判断|结论|想法).{0,50}(?:建议|方案|调整|修改|路线|怎么)",
        r"我注意到你.{0,35}(?:进行|做)了?(?:修改|调整)",
        r"这个改动.{0,30}(?:影响|决定)我(?:接下来|下一步)",
    )
    if any(re.search(pattern, text) for pattern in chinese_patterns):
        return True

    english_patterns = (
        r"\bthis judgment.{0,40}(?:affect|determine).{0,35}(?:next|following).{0,30}(?:suggest|recommend|adjust)",
        r"\b(?:next|then)\s+(?:i|we)\s+(?:will|would|can)\s+(?:suggest|recommend|adjust|discuss)",
        r"\b(?:i|we)\s+will\s+base\s+(?:my|our)\s+(?:next|following)\s+"
        r"(?:suggestion|recommendation|adjustment)",
        r"\bi noticed that you (?:changed|modified|adjusted)",
        r"\bthis change.{0,30}(?:affect|determine)\s+(?:my|our)\s+(?:next|following)",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in english_patterns)


def _proposal_summary_is_substantive(summary, language):
    value = re.sub(r"\s+", "", str(summary or "")).casefold()
    if not value:
        return False

    if _proposal_card_is_meta_language(summary):
        return False

    confirmations = {
        "好", "好的", "可以", "行", "同意", "就这样", "没问题", "嗯",
        "ok", "okay", "yes", "yep", "soundsgood", "agreed",
    }
    if value in confirmations:
        return False

    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value)
    anchors = (
        ("水", "箱", "目标", "墙", "通道", "路线", "推进", "区域", "位置")
        if chinese
        else ("water", "box", "crate", "target", "goal", "wall", "corridor", "route", "push", "area", "position")
    )
    return len(value) >= (4 if chinese else 8) and any(anchor in value for anchor in anchors)


def _distill_proposal_offer(proposal_offer, visible_content, previous_content, language):
    if not proposal_offer:
        return None

    original_summary = str(proposal_offer.get("summary") or "").strip()
    original_rationale = str(proposal_offer.get("rationale") or "").strip()
    execution_brief = proposal_offer.get("executionBrief")
    summary_was_meta_language = _proposal_card_is_meta_language(original_summary)
    rationale_was_meta_language = _proposal_card_is_meta_language(original_rationale)
    if summary_was_meta_language:
        original_summary = ""
    if rationale_was_meta_language:
        original_rationale = ""
    visible_action = _revision_direction_sentence(visible_content)
    # A transition sentence is not a proposal. If the visible reply has no concrete
    # design sentence to replace it, let the caller choose a discussion fallback instead
    # of inventing a revision card from metadata.
    if (summary_was_meta_language or rationale_was_meta_language) and not visible_action:
        return None
    if visible_action and not _proposal_summary_has_concrete_action(original_summary, language):
        original_summary = visible_action
    if _proposal_summary_is_substantive(original_summary, language):
        grounded_offer = _semantics_preserving_proposal_offer(
            original_summary,
            original_rationale,
            visible_content,
            language,
        )
        if grounded_offer is not None:
            return _attach_execution_brief(
                _ensure_proposal_offer_explanation(
                grounded_offer,
                visible_content,
                language,
                ),
                execution_brief,
            )
    corpus = " ".join(
        part for part in (visible_content, previous_content, original_summary, original_rationale)
        if part
    )
    lowered = corpus.casefold()
    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", corpus)

    if chinese:
        has_water = "水" in corpus
        has_target = "目标" in corpus
        has_box = "箱" in corpus
        has_wall = "墙" in corpus
        lower_area = any(word in corpus for word in ("下方", "下半", "右下", "底部", "空旷"))
        route = any(word in corpus for word in ("路线", "通道", "绕", "推进", "推动"))
        box_spacing = has_box and any(
            word in corpus for word in ("错开", "挪", "旁边", "一格", "对着")
        )

        if box_spacing:
            summary = "错开相邻箱子的推进位置"
            rationale = (
                "我会只调整其中一只箱子的相对位置，让另一只箱子先获得可读的推进空间；"
                "然后观察这个错开是否真的带来路线选择，而不是只改变画面。"
            )
        elif has_target and lower_area:
            summary = "重排下半区的目标落点与推进路线"
            rationale = (
                "我会把判断重点放在目标下移后的第一次推进：它是否让下方箱子承担新的绕行，"
                "同时让上下区域的操作密度更均衡，而不是只把空地填满。"
            )
        elif has_water and has_target:
            summary = "让目标落点与水域形成路线联动"
            rationale = (
                "我想让玩家在箱子第一次靠近目标时就感到水域正在改变进入方向；"
                "这样既保留空间感，也能用实际推动判断水域是否真正参与了解法。"
            )
        elif has_water and (has_box or route):
            summary = "让水域参与箱子的首次路线判断"
            rationale = (
                "这一步关注的不是水域面积本身，而是箱子贴近水边推进时，玩家是否必须重新读取"
                "绕行空间与推动顺序，从而让水域从背景变成有作用的路径条件。"
            )
        elif has_wall and route:
            summary = "调整内部墙体形成的通道节奏"
            rationale = (
                "我会观察箱子第一次进入调整区域时，墙体是否制造了清楚但不唯一的转折；"
                "这能帮助我们判断路线选择变丰富了，还是只是增加了额外移动。"
            )
        elif has_box and has_target:
            summary = "重新组织箱子与目标之间的推进关系"
            rationale = (
                "我想用第一次接近目标的推动来检验这项调整：玩家应该能读出新的先后关系，"
                "但仍保留一点需要亲手确认的路线犹豫。"
            )
        else:
            summary = "把当前方向落实为可比较的局部调整"
            rationale = (
                "我会让改动集中在刚才讨论的区域，并用第一次推动时的路线选择来判断它是否"
                "真正改变了体验，而不是只在视觉上显得不同。"
            )
        title_limit = 42
    else:
        has_water = any(word in lowered for word in ("water", "pond"))
        has_target = any(word in lowered for word in ("target", "goal"))
        has_box = any(word in lowered for word in ("box", "crate"))
        has_wall = "wall" in lowered
        lower_area = any(word in lowered for word in ("lower", "bottom", "bottom-right", "open area"))
        route = any(word in lowered for word in ("route", "corridor", "detour", "push", "path"))
        box_spacing = has_box and any(
            word in lowered for word in ("separate", "space apart", "shift", "move over", "one tile", "beside")
        )

        if box_spacing:
            summary = "Separate the adjacent boxes' push positions"
            rationale = (
                "I would move only one box relative to the other so the remaining box has a "
                "legible first push, then judge whether that separation creates a real route choice "
                "rather than only a visual change."
            )
        elif has_target and lower_area:
            summary = "Rebalance the lower targets and push routes"
            rationale = (
                "I would judge this through the first push after the target moves: whether it "
                "gives the lower box a meaningful detour and balances activity across the map, "
                "rather than merely filling empty floor."
            )
        elif has_water and has_target:
            summary = "Link the target approach to the water"
            rationale = (
                "I want the water to change how the player enters the target route on the first "
                "approach, so play can tell us whether it has become a real path condition."
            )
        elif has_water and (has_box or route):
            summary = "Make water shape the box's first route choice"
            rationale = (
                "The useful test is not the water's size but whether pushing beside it makes the "
                "player reread detour space and push order, turning scenery into a path condition."
            )
        elif has_wall and route:
            summary = "Reshape the corridor rhythm around the inner walls"
            rationale = (
                "I would watch the box's first entry into this area to see whether the wall creates "
                "a legible but non-obvious turn instead of merely adding movement."
            )
        elif has_box and has_target:
            summary = "Reframe the push relationship between box and target"
            rationale = (
                "I would use the first target approach to judge whether the new order reads clearly "
                "while still leaving a route decision worth testing by hand."
            )
        else:
            summary = "Turn the direction into a comparable local revision"
            rationale = (
                "I would keep the change around the area we discussed and judge it through the first "
                "route decision, so the result changes play rather than only appearance."
            )
        title_limit = 90

    summary_needs_distilling = (
        len(original_summary) > title_limit
        or _guidance_reuses_visible_sentence(original_summary, visible_content)
        or _guidance_reuses_visible_sentence(original_summary, previous_content)
    )
    rationale_needs_expansion = (
        len(original_rationale) < (28 if chinese else 60)
        or _guidance_reuses_visible_sentence(original_rationale, visible_content)
        or _guidance_reuses_visible_sentence(original_rationale, previous_content)
        or _guidance_text_matches(original_summary, original_rationale)
    )
    return _attach_execution_brief(
        _ensure_proposal_offer_explanation({
            "summary": (summary if summary_needs_distilling else original_summary)[:600],
            "rationale": (rationale if rationale_needs_expansion else original_rationale)[:1000],
        }, visible_content, language),
        execution_brief,
    )


def _attach_execution_brief(offer, execution_brief):
    if offer is None or execution_brief is None:
        return offer
    result = dict(offer)
    result["executionBrief"] = execution_brief
    return result


def _proposal_summary_has_concrete_action(summary, language):
    value = str(summary or "").strip()
    if not value:
        return False
    if _proposal_card_is_meta_language(value):
        return False
    lowered = value.casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value):
        return bool(re.search(r"\(\s*\d+\s*,\s*\d+\s*\)", value)) or any(
            word in value for word in (
                "移", "挪", "调", "改", "加", "减", "删", "缩", "拉开", "收紧", "打开", "封住",
                "让", "形成", "制造",
            )
        )
    return bool(re.search(r"\b(?:row|column|tile)\s*\d+", lowered)) or any(
        word in lowered for word in (
            "move", "shift", "adjust", "add", "remove", "reduce", "expand", "tighten", "open", "close",
        )
    )


def _ensure_proposal_offer_explanation(offer, visible_content, language):
    summary = _strip_revision_summary_leadin(offer.get("summary") or "")[:600]
    rationale = str(offer.get("rationale") or "").strip()
    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", f"{summary}{rationale}{visible_content}")
    visible_action = _revision_direction_sentence(visible_content)
    if (
        visible_action
        and re.search(r"\(\s*\d+\s*,\s*\d+\s*\)", visible_action)
        and not re.search(r"\(\s*\d+\s*,\s*\d+\s*\)", summary)
    ):
        summary = _strip_revision_summary_leadin(visible_action)[:600]
    generic_markers = (
        ("唯一改动方向", "实际格子变化", "只调整", "是否成立")
        if chinese
        else ("only revision direction", "verified diff", "only local cells", "whether it works")
    )
    should_expand = (
        not rationale
        or any(marker in rationale.casefold() for marker in generic_markers)
        or _guidance_text_matches(rationale, summary)
    )
    if should_expand:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[。！？!?])\s*", str(visible_content or ""))
            if sentence.strip()
        ]
        explanation_markers = (
            ("让", "使", "这样", "玩家", "推", "路线", "选择", "顺序")
            if chinese
            else ("make", "let", "so that", "player", "push", "route", "choice", "order")
        )
        explanation = next(
            (
                sentence for sentence in sentences
                if any(marker in sentence.casefold() for marker in explanation_markers)
                and summary not in sentence
                and sentence not in summary
            ),
            "",
        )
        if chinese:
            rationale = (
                f"具体做法是：{summary.rstrip('。.!！')}。{explanation or '这样做是为了让相关箱子的第一次推进出现新的路线判断；试玩时需要确认它带来的是选择，而不只是外观变化。'}"
            )
        else:
            rationale = (
                f"Concrete change: {summary}. {explanation or 'The goal is to make the first relevant push create a new route judgment; play should confirm that it creates a choice rather than only a visual difference.'}"
            )
    elif chinese and not rationale.startswith("具体做法是："):
        rationale = f"具体做法是：{summary}。{rationale}"
    elif not chinese and not rationale.lower().startswith("concrete change:"):
        rationale = f"Concrete change: {summary}. {rationale}"
    return {"summary": summary, "rationale": rationale[:1000]}


def _proposal_rationale_has_playable_effect(rationale, language):
    text = str(rationale or "").strip().casefold()
    if not text or _proposal_card_is_meta_language(text):
        return False

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        markers = (
            "玩家", "游玩", "试玩", "观察", "判断", "路线", "推箱", "推动",
            "选择", "顺序", "可读", "体验", "停留", "时间",
        )
    else:
        markers = (
            "player", "play", "playtest", "observe", "judge", "route", "push",
            "choice", "order", "read", "experience", "time",
        )
    return any(marker in text for marker in markers)


def _proposal_body_has_playable_support(body, language):
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", str(body or ""))
        if sentence.strip() and not _proposal_card_is_meta_language(sentence)
    ]


def _proposal_offer_has_exact_execution_plan(proposal_offer):
    """Return whether a proposal is bound to at least one exact tile transition."""
    if not isinstance(proposal_offer, dict):
        return False
    brief = proposal_offer.get("executionBrief")
    return (
        isinstance(brief, dict)
        and isinstance(brief.get("requiredTransitions"), list)
        and bool(brief["requiredTransitions"])
    )


def _response_contains_multiple_proposals(text):
    """Reject explicit multi-option answers; one purple card represents one plan."""
    value = str(text or "")
    option_markers = re.findall(
        r"(?:\b(?:option|plan|proposal)\s*(?:[a-d]|[1-4]|one|two|three|four|"
        r"first|second|third|fourth)\b|"
        r"\b(?:first|second|third|fourth)\s+(?:option|plan|proposal)\b|"
        r"方案\s*(?:[A-D一二三四]|[1-4]))",
        value,
        flags=re.IGNORECASE,
    )
    return len(option_markers) >= 2


def proposal_offer_requires_execution_brief(proposal_offer, *texts):
    """Reject every proposal card that would require a downstream agent to guess cells."""
    return isinstance(proposal_offer, dict) and not _proposal_offer_has_exact_execution_plan(
        proposal_offer
    )


def _plain_action_instruction(stage_context):
    context = stage_context or {}
    action = context.get("explicitAction") or "none"
    offer = context.get("sourceProposalOffer") or {}
    offer_text = " ".join(
        str(offer.get(field) or "").strip()
        for field in ("summary", "rationale")
    ).strip()
    if action == "challenge_revision":
        return (
            "CARD ACTION: challenge_revision. This is only the first invitation to discuss the cited "
            f"proposal ({offer_text}). Write ordinary prose that restates it, explains its reason and "
            "target play moment, and asks the designer to give a concrete objection. Do not emit any "
            "GUIDANCE fields, including DISCUSS, WARNING, or proposal fields."
        )
    if action == "alternative_revision":
        alternative_brief = str(context.get("alternativeRevisionBrief") or "").strip()
        return (
            "CARD ACTION: alternative_revision. The cited proposal is "
            f"{offer_text}. Offer a different conceptual local treatment with a different summary and "
            "playable rationale. Return exactly one proposalOffer with a complete, current-Stage-"
            "validated executionBrief containing every required transition and anchorEntity for any "
            "entity movement. Do not emit map rows or a disagreement. If the current facts cannot "
            "support a different exact proposal, ask for clarification and omit proposalOffer. "
            f"{alternative_brief}"
        )
    if context.get("challengeContext"):
        return (
            "CARD STATE: the designer has now supplied a reason in response to a prior purple-card "
            "challenge. Judge that reason against the cited proposal and the concrete map evidence. "
            "If you still disagree, emit an active DISAGREEMENT object and no proposal. If the "
            "designer's reason persuades you, or the exchange reaches an AI-led or compromise "
            "resolution, emit a resolved DISAGREEMENT and a new conceptual proposal. If both sides "
            "retain the current map, use resolution retain_current and omit the proposal."
        )
    if context.get("activeDisagreement"):
        return (
            "CARD STATE: an unresolved disagreement is active. Use DISAGREEMENT as an object with "
            "status active/resolved, subject, userPosition, aiPosition, coreDisagreement, "
            "nextQuestion, and resolution. Keep it active unless the latest reason truly resolves "
            "the issue; do not use an ordinary DISCUSS field as a substitute."
        )
    if context.get("discussionCardMode") == "disagreement_only":
        return (
            "CARD STATE: there is no active disagreement. Keep ordinary questions in the visible "
            "reply and omit DISCUSS/followUpQuestion. Only emit DISAGREEMENT when you can state a "
            "specific unresolved design decision supported by map evidence."
        )
    return ""
    text = " ".join(sentences).strip().casefold()
    if len(text) < (18 if language == "zh-CN" else 35):
        return False

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        anchors = (
            "水", "箱", "目标", "墙", "通道", "路线", "推动", "推箱", "空间",
            "难度", "时间", "游玩", "障碍",
        )
        actions = (
            "移动", "调整", "挪", "改", "增加", "减少", "绕", "形成", "让", "使",
            "重排", "保留", "延长", "停留", "多花",
        )
        effects = (
            "玩家", "游玩", "试玩", "观察", "判断", "路线", "选择", "顺序", "体验",
            "时间", "停留", "推",
        )
    else:
        anchors = (
            "water", "box", "crate", "target", "wall", "corridor", "route", "push",
            "space", "difficulty", "time", "play", "obstacle",
        )
        actions = (
            "move", "adjust", "shift", "change", "add", "remove", "detour", "make",
            "let", "rearrange", "preserve", "extend", "slow",
        )
        effects = (
            "player", "play", "playtest", "observe", "judge", "route", "choice", "order",
            "experience", "time", "push",
        )
    return any(marker in text for marker in anchors) and any(
        marker in text for marker in actions
    ) and any(marker in text for marker in effects)


def _proposal_offer_binding_issue(proposal_offer, body, messages, language):
    if not proposal_offer:
        return None

    if not _proposal_offer_has_exact_execution_plan(proposal_offer):
        return (
            "the proposal has no complete executionBrief with exact requiredTransitions; "
            "the assistant must ask for clarification instead of making the executor infer cells"
        )

    summary = str(proposal_offer.get("summary") or "").strip()
    rationale = str(proposal_offer.get("rationale") or "").strip()
    if _proposal_card_is_meta_language(summary):
        return "the summary is transition language rather than a design action"
    if not _proposal_summary_is_substantive(summary, language):
        return "the summary does not state a substantive map or play direction"
    if not _proposal_summary_has_concrete_action(summary, language):
        return "the summary does not state a concrete design action"
    if not _proposal_rationale_has_playable_effect(rationale, language):
        return "the rationale does not state a playable effect or observation"
    if _guidance_reuses_visible_sentence(summary, body):
        return "the summary repeats a sentence from the visible reply"
    if _guidance_reuses_visible_sentence(rationale, body):
        return "the rationale repeats a sentence from the visible reply"
    if not _proposal_body_has_playable_support(body, language):
        return "the visible reply contains no complete supporting design analysis"
    return None


def _is_length_failure(exception, validation_feedback=None):
    text = " ".join(
        str(value or "") for value in (exception, validation_feedback)
    ).casefold()
    return "token limit" in text or "too long" in text or "cut off" in text


def _legacy_route_sentence_profile(value):
    text = str(value or "")
    coordinate_pattern = re.compile(
        r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]"
    )
    movement_pattern = re.compile(
        r"(?:from|toward|through|via|along|then|next|move(?:s|d)? to|go(?:es)? to|"
        r"reach(?:es|ed)?|push(?:es|ed)?|\u4ece|\u5230|\u524d\u5f80|\u7ecf\u8fc7|"
        r"\u6cbf\u7740|\u8d70\u5230|\u63a8\u5230|\u79fb\u52a8\u5230|\u5148.*\u518d)",
        flags=re.IGNORECASE,
    )
    coordinates = coordinate_pattern.findall(text)
    movement_matches = movement_pattern.findall(text)
    arrow_count = text.count("→") + text.count("->")
    return {
        "coordinateCount": len(coordinates),
        "movementCount": len(movement_matches),
        "arrowCount": arrow_count,
        "routeLike": len(coordinates) >= 2
        and (bool(movement_matches) or arrow_count > 0),
    }


def _route_reasoning_profile(value):
    """Describe route density without treating it as a response-validity error."""
    text = str(value or "")
    route_sentence_count = 0
    route_coordinate_count = 0
    overlong = False
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph_profile = _route_sentence_profile(paragraph)
        route_coordinate_count += paragraph_profile["coordinateCount"]
        route_sentence_count += sum(
            1
            for sentence in re.split(
                r"(?<=[.!?\u3002\uff01\uff1f])\s*|[\r\n]+",
                paragraph,
            )
            if _route_sentence_profile(sentence)["routeLike"]
        )
        if paragraph_profile["coordinateCount"] > 6 and (
            paragraph_profile["movementCount"] >= 2
            or paragraph_profile["arrowCount"] >= 2
        ):
            overlong = True
        if paragraph_profile["arrowCount"] >= 5:
            overlong = True
    return {
        "overlong": overlong,
        "routeSentenceCount": route_sentence_count,
        "routeCoordinateCount": route_coordinate_count,
    }


def _route_reasoning_is_overlong(value):
    """Compatibility predicate for tests and diagnostics.

    Callers that process a response should use _recover_overlong_route_reasoning
    instead of turning this quality signal into a validation exception.
    """
    return _route_reasoning_profile(value)["overlong"]


def _legacy_compact_arrow_route_sentence(sentence):
    """Collapse an explicit arrow chain while preserving its endpoints and prose."""
    coordinate = r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]"
    chain_pattern = re.compile(
        rf"(?P<chain>{coordinate}(?:\s*(?:→|->)\s*{coordinate}){{4,}})"
    )
    match = chain_pattern.search(str(sentence or ""))
    if match is None:
        return None

    points = re.findall(coordinate, match.group("chain"))
    if len(points) < 2:
        return None
    compact_chain = f"{points[0]} → … → {points[-1]}"
    return (
        str(sentence)[:match.start()]
        + compact_chain
        + str(sentence)[match.end():]
    ).strip()


def _recover_overlong_route_reasoning(value, rows=None, *, language="en"):
    """Salvage design prose when route tracing exceeds the visible-route budget.

    Route verbosity is a presentation concern, not a safety failure. Keep ordinary
    design sentences, keep one concise route sentence when it can be identified, and
    discard only later route-trace sentences. Formal proposal validation does not use
    this helper.
    """
    original = str(value or "").strip()
    profile = _route_reasoning_profile(original)
    result = {
        "changed": False,
        "salvageAction": "none",
        "routeSentenceCount": profile["routeSentenceCount"],
        "routeCoordinateCount": profile["routeCoordinateCount"],
        "droppedSentenceCount": 0,
        "coordinateLinksDropped": False,
    }
    if not original or not rows or not profile["overlong"]:
        return original, result

    kept_paragraphs = []
    route_kept = 0
    for raw_paragraph in re.split(r"\n\s*\n", original):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        kept_sentences = []
        sentences = [
            sentence.strip()
            for sentence in re.split(
                r"(?<=[.!?\u3002\uff01\uff1f])\s*|[\r\n]+",
                paragraph,
            )
            if sentence.strip()
        ]
        for sentence in sentences:
            sentence_profile = _route_sentence_profile(sentence)
            if not sentence_profile["routeLike"]:
                kept_sentences.append(sentence)
                continue

            if route_kept >= ROUTE_REASONING_PASSAGE_LIMIT:
                result["droppedSentenceCount"] += 1
                continue

            candidate = sentence
            if (
                sentence_profile["coordinateCount"] > 6
                or sentence_profile["arrowCount"] >= 5
            ):
                candidate = _compact_arrow_route_sentence(sentence)
                if not candidate or _route_reasoning_profile(candidate)["overlong"]:
                    candidate = None

            if candidate:
                kept_sentences.append(candidate)
                route_kept += 1
            else:
                result["droppedSentenceCount"] += 1
                route_kept += 1

        if kept_sentences:
            kept_paragraphs.append(" ".join(kept_sentences))

    recovered = "\n\n".join(kept_paragraphs).strip()
    result["changed"] = recovered != original
    if result["changed"]:
        result["salvageAction"] = (
            "route_compacted" if result["droppedSentenceCount"] == 0
            else "route_compacted_and_sentences_dropped"
        )
    return recovered, result


def _personal_reflection_sentences(value):
    first_person = re.compile(
        r"(?:\b(?:i|i'm|i\u2019m|my|personally)\b|\u6211\u89c9\u5f97|\u6211\u8ba4\u4e3a|"
        r"\u8ba9\u6211\u611f\u5230|\u5bf9\u6211\u6765\u8bf4|\u6211\u4f1a\u62c5\u5fc3|\u6211\u559c\u6b22)",
        flags=re.IGNORECASE,
    )
    reflection = re.compile(
        r"(?:\b(?:feel|felt|think|read|find|worry|like|prefer|notice)\b|\u611f\u89c9|\u4f53\u4f1a|"
        r"\u8bfb\u8d77\u6765|\u62c5\u5fc3|\u559c\u6b22|\u6709\u8da3|\u7d27\u5f20)",
        flags=re.IGNORECASE,
    )
    return [
        sentence
        for sentence in _content_block_sentences(value)
        if first_person.search(sentence) and reflection.search(sentence)
    ]


def _recover_overlong_personal_reflection(value, *, language="en"):
    """Compress only repetitive first-person reflection, never the analysis body."""
    original = str(value or "").strip()
    reflections = _personal_reflection_sentences(original)
    result = {
        "changed": False,
        "reflectionSentenceCount": len(reflections),
        "reflectionDroppedSentenceCount": 0,
    }
    if len(reflections) <= PERSONAL_REFLECTION_SENTENCE_LIMIT:
        return original, result

    kept_reflections = []
    for sentence in reflections:
        normalized = re.sub(r"\W+", "", sentence).casefold()
        if any(
            SequenceMatcher(None, normalized, re.sub(r"\W+", "", item).casefold()).ratio() >= 0.8
            for item in kept_reflections
        ):
            continue
        kept_reflections.append(sentence)
        if len(kept_reflections) >= PERSONAL_REFLECTION_SENTENCE_LIMIT:
            break

    reflection_set = set(reflections)
    kept_set = set(kept_reflections)
    kept_sentences = []
    for sentence in _content_block_sentences(original):
        if sentence not in reflection_set or sentence in kept_set:
            kept_sentences.append(sentence)
        else:
            result["reflectionDroppedSentenceCount"] += 1
    separator = "" if re.search(r"[\u3400-\u9fff]", original) else " "
    recovered = separator.join(kept_sentences).strip()
    result["changed"] = recovered != original
    return recovered, result


def _recover_overlong_content(value, rows=None, *, language="en"):
    """Apply presentation budgets only to route and reflection content blocks."""
    recovered, route_result = _recover_overlong_route_reasoning(
        value,
        rows,
        language=language,
    )
    recovered, reflection_result = _recover_overlong_personal_reflection(
        recovered,
        language=language,
    )
    result = dict(route_result)
    result["changed"] = bool(route_result["changed"] or reflection_result["changed"])
    result["reflectionSentenceCount"] = reflection_result["reflectionSentenceCount"]
    result["reflectionDroppedSentenceCount"] = reflection_result[
        "reflectionDroppedSentenceCount"
    ]
    if reflection_result["changed"] and result["salvageAction"] == "none":
        result["salvageAction"] = "personal_reflection_compacted"
    elif reflection_result["changed"]:
        result["salvageAction"] = f"{result['salvageAction']}_and_personal_reflection_compacted"
    return recovered, result


def _route_sentence_profile(value):
    """Profile one visible sentence using escaped Unicode route markers."""
    text = str(value or "")
    coordinate_pattern = re.compile(
        r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]"
    )
    movement_pattern = re.compile(
        r"(?:from|toward|through|via|along|then|next|move(?:s|d)? to|go(?:es)? to|"
        r"reach(?:es|ed)?|push(?:es|ed)?|\u4ECE|\u5230|\u524D\u5F80|\u7ECF\u8FC7|"
        r"\u6CBF\u7740|\u8D70\u5230|\u63A8\u5230|\u79FB\u52A8\u5230|\u5148.*\u518D)",
        flags=re.IGNORECASE,
    )
    coordinates = coordinate_pattern.findall(text)
    movement_matches = movement_pattern.findall(text)
    arrow_count = text.count("\u2192") + text.count("->")
    return {
        "coordinateCount": len(coordinates),
        "movementCount": len(movement_matches),
        "arrowCount": arrow_count,
        "routeLike": len(coordinates) >= 2
        and (bool(movement_matches) or arrow_count > 0),
    }


def _compact_arrow_route_sentence(sentence):
    """Collapse a long explicit arrow chain while preserving its endpoints."""
    coordinate = r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]"
    chain_pattern = re.compile(
        rf"(?P<chain>{coordinate}(?:\s*(?:\u2192|->)\s*{coordinate}){{4,}})"
    )
    match = chain_pattern.search(str(sentence or ""))
    if match is None:
        return None
    points = re.findall(coordinate, match.group("chain"))
    if len(points) < 2:
        return None
    compact_chain = f"{points[0]} \u2192 \u2026 \u2192 {points[-1]}"
    return (
        str(sentence)[:match.start()]
        + compact_chain
        + str(sentence)[match.end():]
    ).strip()


def _is_route_compaction_failure(exception, validation_feedback=None):
    text = " ".join(
        str(value or "") for value in (exception, validation_feedback)
    ).casefold()
    return "route reasoning" in text and "detailed" in text


def _llm_failure_class(exception, validation_feedback=None):
    code = str(getattr(exception, "code", "") or "")
    text = " ".join(
        str(value or "") for value in (exception, validation_feedback)
    ).casefold()
    if "route reasoning" in text or "route" in text and "detailed" in text:
        return "route_quality"
    if "token limit" in text or "cut off" in text or code == "MODEL_TOO_LONG":
        return "truncated_output"
    if code == "MODEL_EMPTY_RESPONSE":
        return "empty_output"
    if (
        "map facts" in text
        or "map-fact" in text
        or "places b" in text
        or "places t" in text
    ):
        return "map_grounding"
    if code == "UPSTREAM_TIMEOUT":
        return "upstream_timeout"
    if code == "UPSTREAM_REQUEST_REJECTED":
        return "upstream_request_rejected"
    if code.startswith("UPSTREAM_"):
        return "upstream_transport"
    return "schema_or_model_validation"


def _safe_incomplete_chat_reply(language, stage_opening=False, rows=None):
    if rows:
        return _server_snapshot_fallback_message(
            rows,
            language,
            stage_opening=stage_opening,
        )
    if language == "zh-CN":
        return "\u6211\u4f1a\u7ee7\u7eed\u4ece\u5f53\u524d\u5df2\u786e\u8ba4\u7684\u8bbe\u8ba1\u4fe1\u606f\u51fa\u53d1\u3002"
    return "I will continue from the currently confirmed design information."
    if language == "zh-CN":
        message = (
            "这次分析没有完整生成，我先不保留半截结论；请再发送一次，我会保留关键设计判断，"
            "并把路径推演收束得更清楚。"
        )
    else:
        message = (
            "This analysis did not finish cleanly, so I will not keep the partial conclusion. "
            "Please send it once more; I will preserve the key design judgment and keep the "
            "route reasoning tighter."
        )
    if stage_opening:
        return _ensure_stage_one_orientation(message, rows, language)
    return message


def _server_snapshot_fallback_message(rows, language, *, stage_context=None, stage_opening=False):
    """Produce a useful body from server facts when model prose is unusable."""
    if stage_opening:
        if language == "zh-CN":
            message = (
                "我先从这个版本的第一次推动来观察：最先被读到的通道，"
                "会不会让推动顺序显得清楚而自然。"
            )
        else:
            message = (
                "I would begin with this version's first push: whether the first readable "
                "corridor makes the push order feel clear and natural."
            )
        return (
            _ensure_stage_one_orientation(message, rows, language)
            if _is_stage_one(stage_context)
            else message
        )
    records = _snapshot_entity_records(rows, stage_context)
    boxes = [item for item in records.values() if item.get("kind") == "box"]
    targets = [item for item in records.values() if item.get("kind") == "target"]
    boxes.sort(key=lambda item: item.get("id") or "")
    targets.sort(key=lambda item: item.get("id") or "")
    facts = []
    if boxes:
        box = boxes[0]
        facts.append((box["id"], box["row"], box["column"]))
    if targets:
        target = targets[0]
        facts.append((target["id"], target["row"], target["column"]))

    if language == "zh-CN":
        if facts:
            detail = "\u3001".join(
                f"{label}\u4f4d\u4e8e\u7b2c{row}\u884c\u7b2c{column}\u5217"
                for label, row, column in facts
            )
            first = f"\u6211\u4f1a\u4ee5\u5f53\u524d\u4fdd\u5b58\u7684 Stage \u4e3a\u51c6\u7ee7\u7eed\u5206\u6790\u3002\u5f53\u524d\u53ef\u786e\u8ba4\uff1a{detail}\u3002"
        else:
            first = "\u6211\u4f1a\u4ee5\u5f53\u524d\u4fdd\u5b58\u7684 Stage \u4e3a\u51c6\u7ee7\u7eed\u5206\u6790\u3002"
        second = (
            "\u5bf9\u6211\u6765\u8bf4\uff0c\u8fd9\u4e2a\u5e03\u5c40\u66f4\u503c\u5f97\u4ece\u7b2c\u4e00\u6b21\u63a8\u7bb1\u65f6\u7684\u9009\u62e9\u5f00\u59cb\u770b\uff1a"
            "\u54ea\u4e2a\u901a\u9053\u5148\u88ab\u8bfb\u5230\uff0c\u4ee5\u53ca\u5b83\u662f\u5426\u8ba9\u63a8\u52a8\u987a\u5e8f\u53d8\u5f97\u6e05\u695a\u3002"
        )
    else:
        if facts:
            detail = ", ".join(
                f"{label} at row {row}, column {column}"
                for label, row, column in facts
            )
            first = f"I will continue from the current saved Stage. The verified facts include {detail}."
        else:
            first = "I will continue from the current saved Stage."
        second = (
            "Personally, I would begin with the first push choice: which corridor is legible first, "
            "and whether that makes the push order feel deliberate rather than arbitrary."
        )
    message = f"{first}\n\n{second}"
    return _ensure_stage_one_orientation(message, rows, language) if stage_opening else message


def _stage_opening_safe_execution(
    *,
    language,
    rows,
    request_id,
    attempts_used,
    started_at,
    fallback_reason,
    stage_context=None,
    solver_metrics=None,
):
    """Return a complete opening without spending another upstream request."""
    fallback_message = _server_snapshot_fallback_message(
        rows,
        language,
        stage_opening=True,
        stage_context=stage_context,
    )
    _log_llm_event(
        "llm_fallback_returned",
        requestId=request_id,
        task="stage_assessment_fallback",
        fallbackReason=fallback_reason,
        finalDisplayMode="server_snapshot",
        bodyPreserved=bool(fallback_message.strip()),
        droppedSentenceCount=0,
        remainingSeconds=round(_remaining_until(_request_deadline(started_at)), 3),
    )
    return LLMExecutionResult(
        assistant_message=fallback_message,
        assessment=_build_minimal_stage_assessment(
            fallback_message,
            None,
            language,
            solver_metrics or {},
        ),
        proposed_rows=None,
        modification_summary="",
        attempts_used=attempts_used,
        request_id=request_id,
        model="kimi-k2.6-safe-opening",
        latency_ms=int((time.monotonic() - started_at) * 1000),
        guidance={
            "move": "observe_stage",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "disagreement": None,
            "uiCues": [],
            "coordinateLinks": [],
        },
    )


def _safe_grounding_chat_reply(language, rows=None, stage_context=None):
    if _has_proposal_clarification(stage_context):
        return _proposal_clarification_fallback_message(language, stage_context)
    if rows:
        return _server_snapshot_fallback_message(
            rows,
            language,
            stage_context=stage_context,
        )
    if language == "zh-CN":
        return (
            "我会以当前保存的地图为准继续分析；刚才回复中的一处具体地图事实没有被可靠确认，"
            "所以我不会把未经验证的坐标当成结论。我们仍然可以继续讨论空间分布、节奏和引导效果。"
        )
    return (
        "I understand that you are discussing the design feel of the current layout. One concrete map fact "
        "in the previous reply could not be verified, so I will not treat an unverified coordinate as fact; "
        "we can still continue from the layout, rhythm, and guidance effect."
    )


def _has_proposal_clarification(stage_context):
    specification = (stage_context or {}).get("proposalClarification")
    return bool(
        isinstance(specification, dict)
        and str(specification.get("questionKey") or "").strip()
    )


def _proposal_clarification_fallback_message(language, stage_context=None):
    """Recover a proposal discussion without restating the Stage snapshot."""
    specification = (stage_context or {}).get("proposalClarification") or {}
    acknowledgement = str(
        specification.get("fallbackAcknowledgement") or ""
    ).strip()
    if acknowledgement:
        return acknowledgement
    if language == "zh-CN":
        return "我会沿着你刚才确认的设计方向继续收敛这份方案。"
    return "I will keep narrowing the proposal around the design direction you just confirmed."


def _proposal_clarification_fallback_question(language, stage_context=None):
    specification = (stage_context or {}).get("proposalClarification") or {}
    configured = str(specification.get("fallbackQuestion") or "").strip()
    if configured:
        return configured
    key = str(specification.get("questionKey") or "").strip()
    chinese = language == "zh-CN"
    questions = {
        "experience_goal": (
            "你希望额外的解题时间主要消耗在哪种判断或操作上？"
            if chinese else
            "What kind of judgment or action should account for the extra solving time?"
        ),
        "mechanism": (
            "你希望通过哪种局部机制增加实际推箱次数？"
            if chinese else
            "What local mechanism should create the additional box pushes?"
        ),
        "binding": (
            "你希望先围绕哪个箱子或局部区域增加运输长度？"
            if chinese else
            "Which box or local area should carry the longer transport first?"
        ),
        "preserve": (
            "增加这部分挑战时，当前体验中的哪一点必须保持不变？"
            if chinese else
            "Which part of the current play experience must remain unchanged while adding this challenge?"
        ),
    }
    return questions.get(
        key,
        "你还希望为这份方案补充哪项关键限制？"
        if chinese else
        "What remaining constraint should guide this proposal?",
    )


def _proposal_clarification_dimension_matches(question, question_key):
    text = str(question or "").casefold()
    patterns = {
        "experience_goal": (
            r"(?:时间|难度|体验|节奏|压力|思考|判断|操作|"
            r"time|difficulty|experience|pacing|pressure|judg|action|thinking)"
        ),
        "mechanism": (
            r"(?:机制|推箱|推动|运输|顺序|陷阱|绕行|试错|误导|死角|"
            r"mechanism|push|transport|order|trap|detour|mistake|deadlock)"
        ),
        "binding": (
            r"(?:哪个(?:箱子|对象|区域)|哪(?:个|一)(?:箱子|对象|区域|片局部)|箱子|区域|局部|对象|B\d+|"
            r"which|what (?:box|area|region)|box|crate|area|region|local)"
        ),
        "preserve": (
            r"(?:保留|保持|不变|不能改变|约束|"
            r"preserve|keep|remain|unchanged|constraint|must not change)"
        ),
    }
    pattern = patterns.get(str(question_key or ""))
    return True if pattern is None else bool(re.search(pattern, text, re.IGNORECASE))


def _proposal_clarification_forbidden_detail(value):
    text = str(value or "")
    return bool(re.search(
        r"(?:\(\s*\d+\s*[,，]\s*\d+\s*\)|第\s*\d+\s*行|第\s*\d+\s*列|"
        r"坐标|逐格|每个格子|格子修改|具体格子|"
        r"\brow\s*\d+\b|\bcol(?:umn)?\s*\d+\b|coordinates?|per[- ]cell|"
        r"each cell|exact cells?|located at|positioned at|adjacent to|next to|"
        r"route endpoint|路线端点|相邻|位于)",
        text,
        re.IGNORECASE,
    ))


def _proposal_clarification_allowed_labels(value, stage_context):
    specification = (stage_context or {}).get("proposalClarification") or {}
    allowed = {
        str(label).upper()
        for label in specification.get("allowedEntityLabels") or []
        if str(label).strip()
    }
    mentioned = {
        label.upper()
        for label in re.findall(r"\b(?:P|B\d+|T\d+)\b", str(value or ""), re.IGNORECASE)
    }
    return mentioned.issubset(allowed)


def _clean_proposal_clarification_body(body, stage_context):
    kept = []
    dropped = 0
    for sentence in _content_block_sentences(body):
        if (
            sentence.endswith(("?", "？"))
            or _proposal_clarification_forbidden_detail(sentence)
            or not _proposal_clarification_allowed_labels(sentence, stage_context)
            or re.search(
                r"(?:请.{0,12}(?:提供|指出|说明).{0,12}(?:坐标|格子|修改)|"
                r"please.{0,16}(?:provide|specify|list).{0,16}(?:coordinate|cell|edit))",
                sentence,
                re.IGNORECASE,
            )
        ):
            dropped += 1
            continue
        kept.append(sentence)
    separator = "" if re.search(r"[\u3400-\u9fff]", str(body or "")) else " "
    return separator.join(kept).strip(), dropped


def _parse_proposal_clarification_payload(content, language, stage_context):
    """Parse Kimi's private body/question envelope and validate only the question."""
    try:
        payload = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", "", "The clarification response must be valid JSON.", 0
    if not isinstance(payload, dict) or set(payload) != {"body", "question"}:
        return "", "", "The clarification response must contain only body and question.", 0
    raw_body = payload.get("body")
    raw_question = payload.get("question")
    if not isinstance(raw_body, str) or not isinstance(raw_question, str):
        return "", "", "The clarification body and question must be strings.", 0
    body = _sanitize_visible_model_text(raw_body.strip(), language)
    question = _sanitize_visible_model_text(raw_question.strip(), language)
    body, dropped = _clean_proposal_clarification_body(body, stage_context)
    issue = ""
    if not question or sum(question.count(mark) for mark in ("?", "？")) != 1:
        issue = "The clarification must contain exactly one question."
    elif not question.endswith(("?", "？")):
        issue = "The clarification question must end with a question mark."
    elif _proposal_clarification_forbidden_detail(question):
        issue = "The clarification question asks for or asserts forbidden map details."
    elif not _proposal_clarification_allowed_labels(question, stage_context):
        issue = "The clarification question uses an entity label not supplied by the server."
    else:
        key = ((stage_context or {}).get("proposalClarification") or {}).get(
            "questionKey"
        )
        if not _proposal_clarification_dimension_matches(question, key):
            issue = f"The clarification question does not address the target dimension {key}."
    return body, question if not issue else "", issue, dropped


def _proposal_clarification_observability(body, question, stage_context):
    specification = (stage_context or {}).get("proposalClarification") or {}
    route_evidence = specification.get("routeEvidence") or {}
    choice_question = bool(re.search(
        r"(?:\bor\b|\bversus\b|\bvs\.?\b|还是|或者|或是)",
        str(question or ""),
        re.IGNORECASE,
    ))
    question_labels = set(re.findall(
        r"\b(?:P|B\d+|T\d+)\b", str(question or ""), re.IGNORECASE
    ))
    body_labels = set(re.findall(
        r"\b(?:P|B\d+|T\d+)\b", str(body or ""), re.IGNORECASE
    ))
    return {
        "clarificationRouteEvidenceMode": route_evidence.get("mode", "unavailable"),
        "clarificationBodySentenceCount": len(_content_block_sentences(body)),
        "clarificationAnalysisOptionCount": 2 if choice_question else None,
        "clarificationQuestionForm": "choice" if choice_question else "open",
        "clarificationOptionsCovered": (
            question_labels.issubset(body_labels) if choice_question and question_labels else None
        ),
    }


def _safe_grounding_clarification(language):
    """Neutral user-facing recovery after bounded grounding retries."""
    if language == "zh-CN":
        return (
            "我会以当前保存的地图为准继续分析；刚才回复中的具体位置还需要重新确认。"
            "请指出你要讨论的实体或格子，我会直接按当前 Stage 的事实继续。"
        )
    return (
        "I will continue from the current saved map. The specific location in the previous reply needs "
        "to be rechecked; please name the entity or cell so I can use the current Stage facts."
    )


def _is_map_grounding_failure(exception, validation_feedback=None):
    text = " ".join(
        str(value or "") for value in (exception, validation_feedback)
    ).casefold()
    markers = (
        "conflicts with deterministic map facts",
        "executionbrief is invalid for the saved stage",
        "executionbrief coordinate conflict",
        "revisionplan is invalid for the saved stage",
        "outside the saved stage",
        "deterministic map facts say",
        "map-fact cleanup",
        "does not match the saved map",
        "places b",
        "places t",
        "places p",
        "saved map does not support",
        "is beside water",
        "historical stage map claim",
        "current box is close to a target",
        "current player is close to a target",
        "current box touches water",
    )
    return any(marker in text for marker in markers)


def _remove_proposal_text_from_body(body, proposal_offer):
    result = str(body or "").strip()
    for field in ("summary", "rationale"):
        card_text = str((proposal_offer or {}).get(field) or "").strip()
        if card_text and _guidance_reuses_visible_sentence(card_text, result):
            result = _remove_exact_guidance_from_body(result, card_text)
    return result


def _remove_exact_guidance_from_body(value, guidance_text):
    """Remove only a complete duplicated card sentence or paragraph.

    Similarity checks intentionally accept a card that is a shortened form of a sentence,
    but rendering cleanup must not delete the surrounding analysis in that case.
    """
    body = _deduplicate_assistant_body(value)
    guidance = str(guidance_text or "").strip()
    if not body or not guidance:
        return body

    def normalize(text):
        return re.sub(r"[^\w\u3400-\u9fff]+", "", str(text or "").casefold())

    normalized_guidance = normalize(guidance)
    paragraphs = []
    for paragraph in body.split("\n\n"):
        if normalize(paragraph) == normalized_guidance:
            continue
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", paragraph)
            if sentence.strip()
        ]
        kept = [
            sentence
            for sentence in sentences
            if normalize(sentence) != normalized_guidance
        ]
        if kept:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            paragraphs.append(separator.join(kept))

    return "\n\n".join(paragraphs).strip() or body


def _has_revision_material(messages, visible_content):
    user_messages = [
        str(message.get("content") or "").strip()
        for message in reversed(messages or [])
        if message.get("role") == "user" and str(message.get("content") or "").strip()
    ][:3]
    return any(
        _contains_user_design_direction(message)
        for message in user_messages
    ) or bool(_revision_direction_sentence(visible_content))


def _deterministic_revision_advice_body(messages, visible_content, language):
    corpus = " ".join(
        part for part in (
            _latest_role_content(messages, "user"),
            visible_content,
        ) if str(part or "").strip()
    ).casefold()
    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", corpus)

    if chinese:
        if any(marker in corpus for marker in ("时间", "游玩", "停留", "延长", "慢下来", "更久", "太快")):
            return (
                "我会把重点放在关键箱子的第一次推进：让玩家需要在直接路线和绕行路线之间做一次判断。"
                "这样游玩时间的增加来自路线和推箱顺序，而不只是多走几步；试玩时可以观察这次停顿是否真的出现。"
            )
        if any(marker in corpus for marker in ("障碍", "绕过", "绕行", "路线")):
            return (
                "我会让关键障碍附近出现一次需要比较的路线选择；试玩时看玩家是否先读出绕行关系，"
                "而不是只因为路变长才多走几步。"
            )
        if "水" in corpus:
            return (
                "我会让箱子靠近水域时必须重新读取可站位置和绕行空间；试玩时观察水是否改变了推进选择，"
                "而不是只增加水面的面积。"
            )
        if any(marker in corpus for marker in ("箱", "目标", "推动", "顺序")):
            return (
                "我会把调整集中在相关箱子接近目标前的推进关系上，让玩家需要判断先后顺序；"
                "试玩时确认变化真正影响了推箱，而不是只改变位置。"
            )
        return (
            "我会把刚才的体验目标落到当前路线的一次局部取舍上；试玩时观察玩家第一次推动是否需要比较两种走法。"
        )

    if any(marker in corpus for marker in ("time", "play", "stay", "longer", "slow", "too quickly")):
        return (
            "I would focus on the first push of the key box and make the player compare a direct route"
            " with a detour. The extra time should come from judging route and push order, not only from walking farther."
        )
    if any(marker in corpus for marker in ("obstacle", "detour", "route")):
        return (
            "I would make the space around the key obstacle create one route comparison; play should show"
            " the player reading the detour rather than merely walking farther."
        )
    if "water" in corpus:
        return (
            "I would make the box reread standing space and detour room beside the water; play should show"
            " whether water changes the push choice rather than only increasing its area."
        )
    if any(marker in corpus for marker in ("box", "target", "push", "order")):
        return (
            "I would focus the change on the box's approach to the target so the player must judge push order;"
            " play should confirm a changed relationship rather than a visual move."
        )
    return (
        "I would turn the stated experience goal into one local route trade-off; play should show whether"
        " the first push requires a comparison between two choices."
    )


def _repair_conceptual_proposal_binding(
    messages,
    body,
    proposal_offer,
    visible_content,
    rows,
    language,
    stage_context,
):
    cleaned_body = _remove_proposal_text_from_body(body, proposal_offer)
    # A deterministic prose fallback cannot know which exact cells the designer
    # intends.  Keep the reply conversational, but never manufacture a proposal
    # card that would force the executor to infer coordinates later.
    fallback_offer = None
    recent_cues = ((stage_context or {}).get("recentGuidance") or {}).get("uiCues") or {}
    if isinstance(recent_cues, dict):
        recent_cues = recent_cues.values()
    warnings = [
        dict(cue)
        for cue in recent_cues
        if isinstance(cue, dict)
        if cue.get("type") in {"warning", "tradeoff"} and cue.get("text")
    ][:1]

    if fallback_offer is not None:
        if not _proposal_body_has_playable_support(cleaned_body, language):
            supporting_body = _deterministic_revision_advice_body(
                messages,
                visible_content,
                language,
            )
            cleaned_body = "\n\n".join(
                part for part in (cleaned_body, supporting_body) if part
            ).strip()
        return {
            "body": cleaned_body,
            "proposalOffer": fallback_offer,
            "followUpQuestion": None,
            "uiCues": [
                {"type": "manual_edit", "text": _contextual_manual_edit(rows, language)},
                *warnings,
            ][:2],
        }

    if not cleaned_body:
        cleaned_body = (
            "我还没有找到足够可靠的具体修改方向，先从一个可观察的游玩瞬间开始。"
            if language == "zh-CN"
            else "I have not found a reliable concrete revision direction yet, so I would start from one observable play moment."
        )
    question = _exact_revision_clarification(language)
    return {
        "body": cleaned_body,
        "proposalOffer": None,
        "followUpQuestion": None,
        "uiCues": [],
    }


def _semantics_preserving_proposal_offer(summary, rationale, visible_content, language):
    summary = _strip_revision_summary_leadin(summary)
    source = " ".join(part for part in (summary, rationale) if part).strip()
    if not source:
        return None
    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", source)
    title_limit = 42 if chinese else 90
    chosen_summary = summary
    if not chosen_summary:
        chosen_summary = _first_declarative_sentence(rationale)
    if len(chosen_summary) > title_limit or _guidance_reuses_visible_sentence(
        chosen_summary,
        visible_content,
    ):
        clauses = [
            clause.strip(" ，,。.!！")
            for clause in re.split(r"[；;。.!！]|(?:，|,)(?=.{8,})", source)
            if clause.strip()
        ]
        action_words = (
            ("移动", "调整", "重排", "增加", "减少", "拉开", "收紧", "改变", "让", "保留")
            if chinese
            else ("move", "adjust", "rearrange", "add", "reduce", "separate", "tighten", "change", "make", "keep")
        )
        action_clause = next(
            (clause for clause in clauses if any(word in clause.casefold() for word in action_words)),
            clauses[0] if clauses else chosen_summary,
        )
        chosen_summary = action_clause[:title_limit].rstrip(" ，,。.!！")
    chosen_summary = chosen_summary or (
        "落实刚才确认的局部修改" if chinese else "Apply the agreed local revision"
    )
    chosen_rationale = rationale
    if (
        not chosen_rationale
        or len(chosen_rationale) < (28 if chinese else 60)
        or _guidance_text_matches(chosen_summary, chosen_rationale)
        or _guidance_reuses_visible_sentence(chosen_rationale, visible_content)
    ):
        chosen_rationale = (
            f"我会把“{chosen_summary}”作为唯一改动方向，只调整实现它所必需的局部，并用实际格子变化与游玩结果判断它是否成立。"
            if chinese
            else f"I will treat “{chosen_summary}” as the only revision direction, change only the local cells needed to realize it, and judge it through the verified diff and play."
        )
    return {
        "summary": chosen_summary[:600],
        "rationale": chosen_rationale[:1000],
    }


def _strip_revision_summary_leadin(summary):
    value = str(summary or "").strip()
    if not value:
        return value
    value = re.sub(
        r"^(?:(?:如果是我|如果让我来|我的想法是|我倾向于)[，,:：\s]*)?"
        r"(?:我会(?:考虑|想先)?|我想(?:先)?|建议(?:先)?|可以(?:先)?)?[，,:：\s]*",
        "",
        value,
    ).strip()
    value = re.sub(
        r"^(?:(?:if it were me|my suggestion is|my thought is)[,:\s]*)?"
        r"(?:i would(?: consider)?|i would suggest|i suggest|we could)[,:\s]*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return value


def _plain_guidance_move(stage_opening, intent_hypothesis, proposal_offer):
    if stage_opening:
        return "observe_stage"
    if proposal_offer:
        return "offer_revision"
    if intent_hypothesis:
        return "clarify_intent"
    return "offer_perspective"


def _apply_deterministic_guidance_fallback(
    messages,
    visible_content,
    language,
    stage_context,
    stage_opening,
    intent_hypothesis,
    proposal_offer,
    ui_cues,
    rows,
    play_summary,
    guidance_mode=None,
    allow_required_fallback=False,
):
    if stage_opening:
        return intent_hypothesis, proposal_offer, [], False

    guidance_mode = guidance_mode or classify_guidance_request(
        messages,
        stage_context,
    )
    latest_user = _latest_role_content(messages, "user")
    explicit_direction = _latest_user_states_direction(messages)
    explicit_agreement = _latest_user_explicitly_agrees(latest_user)
    recent = (stage_context or {}).get("recentGuidance") or {}
    evidence_signature = (stage_context or {}).get("guidanceEvidenceSignature")
    fallback_used = False

    difficulty_reframe = _user_reframes_difficulty_judgment(messages)
    intent_hypothesis = _replace_echoed_intent_hypothesis(
        intent_hypothesis,
        latest_user,
        language,
    )
    if intent_hypothesis is None and (
        difficulty_reframe
        or (
            explicit_direction
            and (
                not recent.get("intentHypothesis")
                or _latest_direction_is_new(messages)
            )
        )
    ):
        candidate = _natural_intent_candidate(
            latest_user,
            language,
            explicit_agreement,
            difficulty_reframe=difficulty_reframe,
        )

        if not _guidance_text_matches(candidate, recent.get("intentHypothesis")):
            intent_hypothesis = candidate
            fallback_used = True

    cue_by_type = {
        cue.get("type"): cue
        for cue in ui_cues
        if cue.get("type") in {"warning", "manual_edit"} and cue.get("text")
    }

    warning_cue = cue_by_type.get("warning")
    if warning_cue and not _warning_has_strong_evidence(
        warning_cue["text"],
        language,
        rows,
        stage_context,
        play_summary,
    ):
        cue_by_type.pop("warning", None)

    if "warning" not in cue_by_type:
        warning = _deterministic_play_warning(
            language,
            play_summary,
        )
        if warning and not _cue_repeats_current_evidence(
            "warning", warning, recent, evidence_signature
        ):
            cue_by_type["warning"] = {"type": "warning", "text": warning}
            fallback_used = True

    needs_direction = (stage_context or {}).get("revisionRequestState") == "needs_direction"
    needs_action_companion = proposal_offer is not None

    if "manual_edit" not in cue_by_type and needs_action_companion:
        manual_edit = _contextual_manual_edit(rows, language)
        if manual_edit and not _cue_repeats_current_evidence(
            "manual_edit", manual_edit, recent, evidence_signature
        ):
            cue_by_type["manual_edit"] = {
                "type": "manual_edit",
                "text": manual_edit,
            }
            fallback_used = True

    ordered_cues = [
        cue_by_type[cue_type]
        for cue_type in ("warning", "manual_edit")
        if cue_type in cue_by_type
    ][:2]
    return intent_hypothesis, proposal_offer, ordered_cues, fallback_used


def _apply_guidance_card_policy(guidance):
    """Normalize guidance into one of the approved one-to-three card families."""
    normalized = dict(guidance or {})
    disagreement = normalized.get("disagreement")
    if isinstance(disagreement, dict) and disagreement.get("status") == "active":
        normalized["proposalOffer"] = None
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        normalized["followUpQuestion"] = None
    cues = [
        dict(cue)
        for cue in normalized.get("uiCues") or []
        if cue.get("type") in {"warning", "tradeoff", "manual_edit"}
        and cue.get("text")
    ]
    warning = next(
        (cue for cue in cues if cue.get("type") in {"warning", "tradeoff"}),
        None,
    )
    manual = next(
        (cue for cue in cues if cue.get("type") == "manual_edit"),
        None,
    )
    clarification = None

    if isinstance(disagreement, dict) and disagreement.get("status") == "active":
        normalized["uiCues"] = [cue for cue in (warning, manual) if cue is not None]
        return normalized

    if normalized.get("proposalOffer") is not None:
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        normalized["followUpQuestion"] = None
        normalized["uiCues"] = [
            cue for cue in (manual, warning) if cue is not None
        ]
        return normalized

    if manual is not None:
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        normalized["followUpQuestion"] = None
        normalized["uiCues"] = [manual]
        return normalized

    normalized["uiCues"] = [warning] if warning is not None else []
    return normalized


def _legacy_exact_revision_clarification(language):
    if language == "zh-CN":
        return (
            "为了避免按猜测改图，请明确指出要修改的坐标，并说明每个格子修改前是什么、修改后要变成什么。"
        )
    return (
        "To keep the revision precise, please specify the cells to change and state the tile before and after each change."
    )


def _legacy_multiple_proposal_fallback_reply(language):
    if language == "zh-CN":
        return (
            "我会一次只提出一个可执行方案。刚才的回复包含了多个候选方向，"
            "请点击重新生成方案，或先说明你希望保留的那一个方向。"
        )
    return (
        "I will present one executable proposal at a time. The previous reply contained multiple "
        "candidate directions, so please request a regenerated proposal or tell me which direction to keep."
    )


def _exact_revision_clarification(language):
    if language == "zh-CN":
        return (
            "\u4e3a\u4e86\u907f\u514d\u6309\u731c\u6d4b\u6539\u56fe，\u8bf7\u660e\u786e\u6307\u51fa\u8981\u4fee\u6539\u7684\u5750\u6807，\u5e76\u8bf4\u660e\u6bcf\u4e2a\u683c\u5b50\u4fee\u6539\u524d\u662f\u4ec0\u4e48\u3001\u4fee\u6539\u540e\u8981\u53d8\u6210\u4ec0\u4e48\u3002"
        )
    return (
        "To keep the revision precise, please specify the cells to change and state the tile before and after each change."
    )


def _multiple_proposal_fallback_reply(language):
    if language == "zh-CN":
        return (
            "\u6211\u4f1a\u4e00\u6b21\u53ea\u63d0\u51fa\u4e00\u4e2a\u53ef\u6267\u884c\u65b9\u6848\u3002\u521a\u624d\u7684\u56de\u590d\u5305\u542b\u4e86\u591a\u4e2a\u5019\u9009\u65b9\u5411\uff0c\u8bf7\u70b9\u51fb\u91cd\u65b0\u751f\u6210\u65b9\u6848\uff0c\u6216\u5148\u8bf4\u660e\u4f60\u5e0c\u671b\u4fdd\u7559\u7684\u90a3\u4e00\u4e2a\u65b9\u5411\u3002"
        )
    return (
        "I will present one executable proposal at a time. The previous reply contained multiple "
        "candidate directions, so please request a regenerated proposal or tell me which direction to keep."
    )


def _ensure_required_guidance_card(
    guidance,
    messages,
    language,
    rows,
    stage_opening,
    stage_context,
    visible_content="",
    guidance_mode=None,
):
    normalized = dict(guidance or {})
    latest_user = _latest_role_content(messages, "user")
    guidance_mode = guidance_mode or classify_guidance_request(
        messages,
        stage_context,
        stage_opening=stage_opening,
    )

    active_disagreement = (stage_context or {}).get("activeDisagreement")
    if (
        normalized.get("disagreement") is None
        and isinstance(active_disagreement, dict)
        and active_disagreement.get("status") == "active"
        and not normalized.get("proposalOffer")
    ):
        normalized["disagreement"] = active_disagreement

    if _is_stage_one(stage_context) and stage_opening:
        return normalized

    if _is_human_edit_stage_opening(stage_opening, stage_context):
        normalized["followUpQuestion"] = _human_edit_intent_discussion_focus(
            normalized.get("followUpQuestion"),
            stage_context,
            language,
        )
        return normalized

    if _user_explicitly_off_topic(latest_user):
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        normalized["followUpQuestion"] = None
        normalized["proposalOffer"] = None
        normalized["uiCues"] = []
        return normalized

    if guidance_mode == "needs_clarification":
        normalized["move"] = "clarify_intent"
        normalized["proposalOffer"] = None
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        normalized["followUpQuestion"] = None
        normalized["uiCues"] = []
        return normalized

    if guidance_mode == "revision_advice":
        proposal_offer = normalized.get("proposalOffer")
        if _proposal_offer_has_exact_execution_plan(proposal_offer):
            normalized["move"] = "offer_revision"
            normalized["proposalOffer"] = proposal_offer
            normalized["intentHypothesis"] = None
            normalized["intentConfidence"] = None
            normalized["followUpQuestion"] = None
            cues = [
                dict(cue)
                for cue in normalized.get("uiCues") or []
                if cue.get("type") in {"warning", "tradeoff", "manual_edit"}
                and cue.get("text")
            ]
            if not any(cue.get("type") == "manual_edit" for cue in cues):
                cues.append({
                    "type": "manual_edit",
                    "text": _contextual_manual_edit(rows, language),
                })
            normalized["uiCues"] = [
                cue for cue in cues
                if cue.get("type") in {"warning", "tradeoff", "manual_edit"}
            ][:2]
            return normalized

        normalized["move"] = "clarify_intent"
        normalized["proposalOffer"] = None
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        clarification = normalized.get("followUpQuestion") or _exact_revision_clarification(language)
        # Missing/invalid binding is a normal clarification turn, not a
        # visible failure card.  The caller keeps the question in prose.
        normalized["followUpQuestion"] = None
        normalized["uiCues"] = []
        return normalized

    if guidance_mode == "discussion":
        normalized["proposalOffer"] = None
        if (stage_context or {}).get("discussionCardMode") == "disagreement_only":
            normalized["followUpQuestion"] = None
            normalized["uiCues"] = [
                dict(cue)
                for cue in normalized.get("uiCues") or []
                if cue.get("type") in {"warning", "tradeoff"} and cue.get("text")
            ][:1]
            return normalized
        normalized["followUpQuestion"] = normalized.get("followUpQuestion") or (
            _friendly_default_discussion_focus(
                rows,
                language,
                latest_user,
                ((stage_context or {}).get("recentGuidance") or {}).get(
                    "discussionFocusHistory",
                    [],
                ),
            )
        )
        normalized["move"] = "clarify_intent"
        normalized["uiCues"] = [
            dict(cue)
            for cue in normalized.get("uiCues") or []
            if cue.get("type") in {"warning", "tradeoff"} and cue.get("text")
        ][:1]
        return normalized

    if _user_states_first_person_view(latest_user):
        hypothesis = normalized.get("intentHypothesis") or _natural_intent_candidate(
            latest_user,
            language,
            _latest_user_explicitly_agrees(latest_user),
            difficulty_reframe=_user_reframes_difficulty_judgment(messages),
        )
        normalized["intentHypothesis"] = _replace_echoed_intent_hypothesis(
            hypothesis,
            latest_user,
            language,
        )
        normalized["intentConfidence"] = "medium"

    card_count = (
        int(bool(normalized.get("intentHypothesis")))
        + int(bool(normalized.get("followUpQuestion")))
        + int(bool(normalized.get("proposalOffer")))
        + len(normalized.get("uiCues") or [])
    )
    if card_count:
        return normalized

    return normalized


def _user_states_first_person_view(message):
    text = str(message or "").strip()
    lowered = text.casefold()
    if not text:
        return False
    return (
        any(marker in text for marker in (
            "我认为", "我觉得", "我感觉", "在我看来", "我不认同", "我不同意",
        ))
        or bool(re.search(
            r"\b(?:i think|i feel|i believe|in my view|from my perspective|i disagree)\b",
            lowered,
        ))
    )


def classify_guidance_request(conversation, stage_context=None, stage_opening=False):
    """Classify when the visible guidance should prefer action or discussion.

    This is intentionally a small deterministic gate around the model's prose.  It does not
    decide the user's intention or authorize a revision; it only selects the existing card
    family for an obvious request for help.
    """
    if stage_opening:
        return "none"

    context = stage_context or {}
    explicit_action = context.get("explicitAction") or "none"
    if explicit_action == "challenge_revision":
        return "none"
    if explicit_action == "alternative_revision":
        return "revision_advice"
    if context.get("activeDisagreement"):
        return "disagreement"
    if context.get("challengeContext"):
        return "disagreement"
    if context.get("deferRevisionExecution") and context.get("revisionRequestState") in {
        "authorized", "authorized_relaxed"
    }:
        return "revision_advice"
    routing = context.get("revisionRouting")
    if routing in {"confused", "needs_clarification"}:
        return "needs_clarification"
    if routing == "proposal_blocked":
        return "proposal_blocked"
    if routing in {"proposal", "proposal_conservative"}:
        return "revision_advice"
    if context.get("revisionRequestState") not in (None, "not_request"):
        return "none"

    latest_user = _latest_role_content(conversation, "user")
    if not latest_user or _user_explicitly_off_topic(latest_user):
        return "none"

    # Keep the existing authorization/needs-direction path authoritative. A directionless
    # change request is handled as a tentative intent, not as an unsolicited plan.
    revision_state, _ = _classify_revision_request(conversation, context)
    if revision_state != "not_request":
        return "none"

    user_messages = [
        str(message.get("content") or "").strip()
        for message in reversed(conversation)
        if message.get("role") == "user" and str(message.get("content") or "").strip()
    ][:3]
    advice_request = any(
        _guidance_advice_request(message)
        for message in user_messages
    )
    has_direction = any(
        _contains_user_design_direction(message)
        for message in user_messages
    )

    if advice_request and has_direction:
        return "revision_advice"
    if advice_request and (
        _guidance_confusion_request(latest_user) or not has_direction
    ):
        return "discussion"
    return "none"


def _guidance_mode_instruction(guidance_mode):
    if guidance_mode == "revision_advice":
        return (
            "Deterministic request routing classified the latest user request as REVISION_ADVICE: "
            "the designer has stated a meaningful design direction and is asking for a suggestion "
            "or plan. You must include PROPOSAL_SUMMARY, PROPOSAL_RATIONALE, and an EXECUTION_BRIEF "
            "with at least one exact required transition containing row, column, from, and to. "
            "Resolve every intended edit from the authoritative map facts; if that is not possible, "
            "ask for clarification and do not output a proposalOffer. The proposal may not contain "
            "map rows or an execution result, but its machine contract must be exact. Do not output "
            "DISCUSS or INTENT in this action family, and do not claim that "
            "the map was changed."
        )
    if guidance_mode == "discussion":
        return (
            "Deterministic request routing classified the latest user request as DISCUSSION: the "
            "designer is asking for ideas without stating a concrete direction. Keep the answer in "
            "ordinary assistantMessage prose and do not create a new blue DISCUSS card or legacy "
            "followUpQuestion for this ordinary request. Use only observable map facts and "
            "playable moments. Do not output proposal fields or MANUAL_EDIT, and do not decide a map "
            "change on the designer's behalf. A blue card requires a separate structured active "
            "disagreement about a concrete decision."
        )
    if guidance_mode == "needs_clarification":
        return (
            "Deterministic routing found that the latest direction is either unclear, conflicted, "
            "or addressed to more than one possible map object. Keep the response in ordinary "
            "assistantMessage prose. Ask exactly one open, high-value question about the single "
            "missing input; never present a menu of choices or add a second question. Stop early as soon as the user "
            "has supplied enough information. If the purpose and object are safely identifiable "
            "after the clarification exchange, complete the missing implementation details "
            "conservatively and generate one complete proposal. Do not output proposal fields "
            "while the object or direction is still ambiguous, and never output MANUAL_EDIT, a "
            "clarification card, or a failure card. Do not guess coordinates or map identities."
        )
    if guidance_mode == "proposal_blocked":
        return (
            "The bounded proposal clarification budget is exhausted, but the current map object "
            "is still ambiguous. Give one concise, neutral explanation of the exact missing binding "
            "and do not ask another question, create a proposal, emit a card, or guess coordinates."
        )
    if guidance_mode == "disagreement":
        return (
            "Deterministic routing found an unresolved design disagreement. Use the four-field "
            "DISAGREEMENT object to summarize the user's position, your current position, the "
            "core disagreement, and the next question. Keep status active until the latest user "
            "reason genuinely resolves the issue. Do not output a proposalOffer while active; "
            "ordinary questions without a real disagreement stay in assistantMessage."
        )
    return ""


def _guidance_advice_request(message):
    text = str(message or "").strip().casefold()
    if not text:
        return False

    if re.search(
        r"(?:\u7ed9\u6211(?:\u4e00\u4e2a|\u4e00\u70b9)?\u65b9\u6848|\u6211\u60f3\u8981(?:\u4e00\u4e2a)?\u65b9\u6848|"
        r"\u6211\u5e0c\u671b\u6709\u4e00\u4e2a\u65b9\u6848|\u8bf7\u7ed9\u6211\u4e00\u4e2a\u65b9\u6848|"
        r"\u7ed9\u6211\u5efa\u8bae|\u7ed9\u6211\u601d\u8def|\u8bf7\u5efa\u8bae|\u8bf7\u63d0\u4f9b\u65b9\u6848|"
        r"\u600e\u4e48\u6539|\u5982\u4f55\u6539|\u600e\u4e48\u8c03\u6574|\u5982\u4f55\u8c03\u6574|"
        r"\u4f60\u4f1a\u600e\u4e48\u505a|\u8bf7\u4f60\u5206\u6790)",
        text,
    ):
        return True

    chinese_markers = (
        "建议", "方案", "思路", "怎么改", "如何改", "怎么调整", "如何调整",
        "有什么办法", "帮我想", "给我点想法", "给点思路", "你会怎么",
        "请你分析", "说说修改", "怎么开始",
    )
    if any(marker in text for marker in chinese_markers):
        return True

    return bool(re.search(
        r"\b(?:suggest(?:ion)?s?|recommend(?:ation)?s?|advice|plan|idea(?:s)?|brainstorm|"
        r"how (?:to|should|would|could)|what (?:would|could|do) you suggest|"
        r"what would you change|help me think|give me (?:an? )?(?:idea|direction|plan)|"
        r"(?:a|an|the|some)\s+(?:concrete\s+)?(?:revision|change|direction|plan|edit))\b",
        text,
    ))


def _guidance_confusion_request(message):
    text = str(message or "").strip().casefold()
    if not text:
        return False

    if re.search(
        r"(?:\u56f0\u60d1|\u4e0d\u77e5\u9053|\u6ca1\u60f3\u6cd5|\u6ca1\u6709\u60f3\u6cd5|"
        r"\u6ca1\u601d\u8def|\u6ca1\u6709\u601d\u8def|\u6ca1\u6709\u65b9\u5411|"
        r"\u4e0d\u786e\u5b9a\u4ece\u54ea\u91cc|\u4e0d\u77e5\u9053\u4ece\u54ea\u91cc\u5f00\u59cb)",
        text,
    ):
        return True

    chinese_markers = (
        "迷茫", "不知道", "没想法", "没有想法", "没思路", "没有思路",
        "没有方向", "不知道从哪里", "不确定从哪里", "怎么开始", "不知该",
    )
    if any(marker in text for marker in chinese_markers):
        return True

    return bool(re.search(
        r"\b(?:i(?:'m| am) )?(?:not sure|uncertain|confused|lost|stuck|no idea)|"
        r"\b(?:i )?don'?t know where to start\b",
        text,
    ))


def _contains_user_design_direction(message):
    text = str(message or "").strip().casefold()
    if not text:
        return False

    actual_chinese_direction = bool(re.search(
        r"(?:\u6211\u60f3(?:\u8981|\u8ba9|\u628a)|\u6211\u5e0c\u671b|"
        r"\u6211\u503e\u5411\u4e8e|\u6211\u66f4\u503e\u5411\u4e8e|\u8bf7\u4fdd\u6301|"
        r"\u4fdd\u6301|\u4e0d\u8981|\u5fc5\u987b|\u8ba9\u73a9\u5bb6|\u4f7f\u73a9\u5bb6|"
        r"\u589e\u52a0|\u51cf\u5c11|\u5f3a\u5316|\u5f31\u5316|\u8c03\u6574|\u4fee\u6539|"
        r"\u6539\u6210).{0,100}(?:\u6c34\u57df|\u6c34|\u7bb1\u5b50|\u76ee\u6807|"
        r"\u8def\u7ebf|\u901a\u9053|\u969c\u788d|\u7a7a\u95f4|\u96be\u5ea6|\u9009\u62e9|"
        r"\u8282\u594f|\u53ef\u8bfb|\u63a8\u7bb1|\u63a8\u52a8)",
        text,
    ))

    # A bare open question such as "How would you change the route?" asks for ideas but does
    # not establish a direction.  A statement plus a question, such as "I want water to shape
    # the route; how would you change it?", remains a concrete direction.
    has_question = "?" in text or "？" in text
    if has_question and not actual_chinese_direction and not re.search(
        r"(?:我想|我希望|我更想|我的目标|希望让|想让|需要让|我倾向于|"
        r"\bi (?:want|would like|hope|prefer)|\bmy goal is\b|\bmake the player\b|"
        r"\blet the player\b)",
        text,
        re.IGNORECASE,
    ):
        return False

    if actual_chinese_direction or _contains_concrete_revision_direction(text):
        return True

    chinese_direction_leads = (
        "我想", "我希望", "我更想", "我的目标", "希望让", "想让", "需要让",
        "我倾向于", "强调", "突出", "增强", "强化", "弱化", "改善",
    )
    chinese_effects = (
        "绕过障碍", "绕行", "路线", "通道", "推箱", "推动顺序", "推进顺序",
        "难度", "节奏", "选择", "空间", "水域", "墙", "箱子", "目标", "压力",
        "犹豫", "停顿", "挑战", "更难", "更简单", "可读", "时间", "游玩", "停留", "延长",
    )
    if any(marker in text for marker in chinese_direction_leads) and any(
        marker in text for marker in chinese_effects
    ):
        return True

    english_leads = re.search(
        r"\b(?:i want|i would like|i hope|i prefer|my goal is|make the player|"
        r"let the player|increase|reduce|strengthen|weaken|preserve|avoid)\b",
        text,
        re.IGNORECASE,
    )
    english_effects = (
        "detour", "obstacle", "route", "corridor", "push order", "difficulty", "challenge",
        "rhythm", "choice", "space", "water", "wall", "box", "target", "pressure",
        "hesitation",
    )
    if english_leads and any(marker in text for marker in english_effects):
        return True

    return not has_question and bool(re.search(
        r"\b(?:add|remove|move|change|adjust|reshape|rearrange)\b.{0,40}\b(?:wall|water|"
        r"box|target|route|corridor|obstacle|space)\b",
        text,
        re.IGNORECASE,
    ))


def _discussion_follow_up_is_needed(messages, language):
    latest_user = _latest_role_content(messages, "user")
    lowered = latest_user.casefold()
    if not latest_user or _user_states_first_person_view(latest_user):
        return False
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", latest_user):
        return any(marker in latest_user for marker in (
            "你觉得", "怎么改", "如何改", "要不要", "是否", "会不会", "哪个",
        ))
    return bool(re.search(
        r"\b(?:what do you think|how (?:would|should|could)|which|whether|should we)\b",
        lowered,
    ))


def _user_explicitly_off_topic(message):
    text = str(message or "").strip().casefold()
    if not text:
        return False
    chinese = (
        "换个话题", "题外话", "与这个项目无关", "和这个项目无关", "与地图无关",
        "和地图无关", "先不聊地图", "不谈这个关卡", "聊点别的", "说点别的",
    )
    english = (
        "change the subject", "off topic", "unrelated to this project",
        "unrelated to the map", "let's talk about something else",
        "stop talking about the map",
    )
    return any(marker in text for marker in (*chinese, *english))


def _friendly_default_discussion_focus(rows, language, latest_user, recent_focuses):
    serialized = "".join(str(row) for row in (rows or []))
    seed = sum(ord(character) for character in f"{serialized}{latest_user}")
    has_water = "@" in serialized
    has_boxes = serialized.count("s") > 0
    has_multiple_boxes = serialized.count("s") > 1
    has_targets = serialized.count("t") > 0
    if language == "zh-CN":
        options = (
            ("我会留意第一次推箱时，玩家会不会自然停下来读一眼路线；这能帮我们判断附近该微调，还是该留出空间。", True),
            ("箱子第一次靠近目标时，玩家会不会停一下想下一步？我想先看这个，因为它最能看出路线有没有形成真实选择。", has_boxes and has_targets),
            ("玩家第一次决定先推哪个箱子时，心里会不会有一点犹豫？这个瞬间能告诉我们下一步只动局部，还是回头整理路线。", has_multiple_boxes),
            ("水边第一次推进时，箱子还有没有回旋余地？看这里就能知道水是在参与路线，还是只停在视觉上。", has_water and has_boxes),
            ("玩家走进调整区域的第一步，会不会重新想一下推箱顺序？如果会，这次变化就真的参与到路线里了。", has_boxes),
        )
    else:
        options = (
            ("I would watch whether the first push makes the player pause to read the route; that moment tells us whether to tune nearby space or preserve it.", True),
            ("To me, the hesitation before a box first approaches its target is worth watching; it says more about route weight than the tile arrangement alone.", has_boxes and has_targets),
            ("I would focus on the first moment the player chooses which box to push; that judgment decides whether the next revision stays local or revisits the route.", has_multiple_boxes),
            ("The first push beside the water is the moment I would watch; it shows whether water is shaping the route or remaining visual scenery.", has_water and has_boxes),
            ("I would look at whether the first step into the adjusted area makes the player reread the order; that matters more than merely adding movement.", has_boxes),
        )
    available_options = [candidate for candidate, available in options if available]
    # Isolated prompt tests and development fallbacks sometimes use an intentionally
    # incomplete grid.  Preserve the original variety there; production Stages have map
    # facts and are filtered to avoid mentioning unavailable entities.
    if not (has_water or has_boxes or has_targets):
        available_options = [candidate for candidate, _ in options]
    if not available_options:
        available_options = [options[0][0]]
    for offset in range(len(options)):
        candidate = available_options[(seed + offset) % len(available_options)]
        if not any(
            _guidance_text_matches(candidate, recent_focus)
            for recent_focus in (recent_focuses or [])
        ):
            return candidate
    return available_options[seed % len(available_options)]


def _unclear_revision_intent(language):
    if language == "zh-CN":
        return (
            "我暂时理解为你想让关卡的某个局部更符合你的预期，但还不能确定是路线、"
            "推箱顺序还是空间关系；这个理解可以由你纠正。"
        )
    return (
        "For now, I understand that you want some local part of the level to better match "
        "your expectation, but I cannot yet tell whether you mean the route, push order, "
        "or spatial relationship; please correct this hypothesis."
    )


def _unclear_revision_reply(language):
    if language == "zh-CN":
        return (
            "可以，我愿意和你一起改。只是我还没读准你最想动的是哪一块；如果现在直接替你"
            "改地图，我其实是在替你猜，这样不可靠。\n\n"
            "你可以先在右侧编辑器标出最在意的局部，哪怕只动一格也行。等真实变化保存下来，"
            "我就能沿着它继续判断，而不是把自己的想法冒充成你的要求。"
        )
    return (
        "Yes, I am happy to work on it with you. I just have not understood which part you "
        "most want changed; if I edited the map now, I would be guessing on your behalf, "
        "and that would not be reliable.\n\n"
        "You can mark the area that matters most in the editor, even with a one-tile change. "
        "Once that real change is saved, I can continue from it without presenting my own "
        "idea as your request."
    )


def _natural_intent_candidate(
    latest_user,
    language,
    explicit_agreement,
    *,
    difficulty_reframe=False,
):
    source = re.sub(r"\s+", " ", str(latest_user or "")).strip()
    variant = sum(ord(character) for character in source) % 3

    if difficulty_reframe:
        stance = _difficulty_stance(source)
        if language == "zh-CN":
            if stance == "easy":
                options = (
                    "我暂时把你的方向理解为：你要的不是多走几步，而是让关键推动前真的出现需要停下来读路线的判断；如果我理解偏了，请纠正我。",
                    "听起来你更在意的是，玩家不能只顺着流程把箱子推到底，而要在关键节点承担一次真实的路线选择；这只是我目前的理解。",
                    "我读到的倾向是，你希望这张图的难度来自推动顺序和后果，而不只是表面上多加一点障碍；若重点不是这里，你可以改正我。",
                )
            else:
                options = (
                    "我暂时把你的方向理解为：你更想减轻关键推动前的负担，让玩家能读懂路线而不是被无谓阻力拖住；如果我理解偏了，请纠正我。",
                    "听起来你在意的是保留判断感，但别让路线复杂到遮住真正的选择；这只是我目前的理解。",
                    "我读到的倾向是，你希望难度来自清楚的取舍，而不是让人因为读不清路线而受挫；若重点不是这里，你可以改正我。",
                )
        elif stance == "easy":
            options = (
                "For now, I understand your direction as wanting a real route judgment before a key push, not simply more walking; please correct me if I have that wrong.",
                "It sounds to me like you want the player to make a consequential choice rather than push straight through the route; that is only my current reading.",
                "I read your preference as difficulty coming from push order and consequences, not merely from adding obstacles; please correct me if that misses the point.",
            )
        else:
            options = (
                "For now, I understand your direction as keeping the key route readable instead of adding friction that hides the real choice; please correct me if I have that wrong.",
                "It sounds to me like you want to preserve meaningful judgment without making the route harder to read; that is only my current reading.",
                "I read your preference as wanting difficulty to come from clear trade-offs rather than confusing resistance; please correct me if that misses the point.",
            )
        return options[variant]

    if language == "zh-CN":
        if explicit_agreement:
            options = (
                "听起来你已经准备把刚才的想法放进一次具体尝试里；这是我目前的理解。",
                "我暂时把你的方向理解为：先让这个局部想法真正参与一次游玩判断。",
                "我读到的倾向是，你更想先做出一个能亲手比较的局部变化。",
            )
        else:
            options = _semantic_intent_options(source, "zh-CN")
    elif explicit_agreement:
        options = (
            "It sounds to me like you are ready to put that idea into a concrete trial.",
            "For now, I understand your direction as testing this idea through a local change.",
            "I read your preference as making this idea tangible enough to compare in play.",
        )
    else:
        options = _semantic_intent_options(source, "en")
    return options[variant]


def _replace_echoed_intent_hypothesis(hypothesis, latest_user, language):
    """Keep intent cards interpretive rather than a prefixed copy of user text."""
    if not hypothesis or not latest_user:
        return hypothesis

    source = _intent_comparison_text(latest_user, language)
    candidate = _intent_comparison_text(hypothesis, language)
    if len(source) < 5 or len(candidate) < 5:
        return hypothesis

    similarity = SequenceMatcher(None, source, candidate).ratio()
    if source in candidate or similarity >= 0.82:
        variant = sum(ord(character) for character in source) % 3
        return _semantic_intent_options(str(latest_user), language)[variant]
    return hypothesis


def _intent_comparison_text(text, language):
    value = re.sub(r"\s+", "", str(text or "")).strip("。！？?!：:”“\"' ")
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value):
        value = re.sub(
            r"^(?:我暂时把你的方向理解为|听起来你更在意的是|我读到的倾向是|我猜你可能|我觉得你可能|我感觉你(?:可能|似乎))[:：]?",
            "",
            value,
        )
    else:
        value = re.sub(
            r"^(?:For now, I understand your direction as(?: wanting)?|It sounds to me like you|I read your preference as(?: wanting)?)[: ]*",
            "",
            value,
            flags=re.IGNORECASE,
        )
    return value.casefold()


def _semantic_intent_options(source, language):
    value = str(source or "").casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", str(source or "")):
        if "水" in source and any(marker in source for marker in ("形", "改", "变", "水域")):
            return (
                "我暂时理解的是，你想让水域真正重写玩家读路线和推进时机的方式，而不是只补一个局部缺口；如果我抓错重点，请纠正我。",
                "听起来你更在意的是，水要从背景变成会迫使玩家重新判断绕行与推进的边界；这只是我当前的理解。",
                "我读到的倾向是，你希望水域的布局改变箱子经过时的选择感，而不只是换一个视觉形状；若重点不是这里，请改正我。",
            )
        if "墙" in source:
            return (
                "我暂时理解的是，你想让墙体改变路线的取舍，而不只是增加一块阻挡；如果我抓错重点，请纠正我。",
                "听起来你更在意的是，墙应当让玩家在推进前读出不同路径的后果；这只是我当前的理解。",
                "我读到的倾向是，你希望障碍承担选择压力，而不是单纯拉长路线；若重点不是这里，请改正我。",
            )
        return (
            "我暂时理解的是，你希望这次局部改动真的改变玩家读路线和作决定的方式，而不只是调整某个格子；如果我抓错重点，请纠正我。",
            "听起来你更在意的是，下一步要能在游玩中感到明确差异，而不是停留在表面的布局变化；这只是我当前的理解。",
            "我读到的倾向是，你希望这项调整影响实际的推进选择；若重点不是这里，请改正我。",
        )

    if "water" in value:
        return (
            "For now, I understand your direction as wanting water to reshape route reading and push timing, rather than merely change a local patch; please correct me if I have that wrong.",
            "It sounds to me like you want water to become a boundary that makes rerouting and pushing worth judging, not background decoration; that is only my current reading.",
            "I read your preference as wanting the water layout to change the choices around a box's passage, rather than only its visual shape; please correct me if that misses the point.",
        )
    return (
        "For now, I understand your direction as wanting this local change to alter how the player reads the route and makes a decision, rather than merely changing a tile; please correct me if I have that wrong.",
        "It sounds to me like the next change needs to create a felt difference in play, not just a surface-level layout adjustment; that is only my current reading.",
        "I read your preference as wanting this adjustment to affect a real push decision; please correct me if that misses the point.",
    )


def _difficulty_stance(text):
    value = str(text or "").casefold()
    if any(marker in value for marker in (
        "太简单", "太容易", "不够难", "没难度", "太顺", "一路平推",
        "too easy", "too simple", "not hard enough", "no challenge",
    )):
        return "easy"
    if any(marker in value for marker in (
        "太难", "太复杂", "太绕", "不够简单", "太卡",
        "too hard", "too difficult", "too complex", "too confusing",
    )):
        return "hard"
    return None


def _user_reframes_difficulty_judgment(messages):
    latest = _latest_role_content(messages, "user")
    if not latest or "?" in latest or "？" in latest:
        return False
    stance = _difficulty_stance(latest)
    if stance is None:
        return False
    previous_assistant = _latest_role_content(messages[:-1], "assistant")
    if not previous_assistant:
        return False
    lowered = latest.casefold()
    first_person_evaluation = (
        any(marker in latest for marker in ("我认为", "我觉得", "我感觉", "我不认同", "我不同意"))
        or bool(re.search(r"\b(?:i think|i feel|i disagree|to me)\b", lowered))
    )
    contrast = any(marker in lowered for marker in (
        "还是", "其实", "但", "不认同", "不同意",
        "still", "actually", "but", "however", "disagree",
    ))
    previous_stance = _difficulty_stance(previous_assistant)
    return first_person_evaluation and (
        contrast or previous_stance is not None
    )


def _intent_is_only_execution_authorization(text, language="en"):
    value = str(text or "").strip().casefold()
    if not value:
        return False

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value):
        execution = any(
            marker in value
            for marker in (
                "你帮我改", "帮我改一下", "你来改", "请你改", "交给你改",
                "我来帮你改", "把修改交给我", "由我来改", "帮我做",
                "就这么做", "照这个做", "按这个做",
            )
        )
        design_anchors = (
            "水", "箱", "目标", "墙", "通道", "路线", "推动", "落点", "节奏",
            "难度", "选择", "可读", "绕行", "空间",
        )
    else:
        execution = bool(
            re.search(
                r"\b(?:help me|you|assistant|i should)\s+"
                r"(?:change|modify|revise|edit|do)\b",
                value,
            )
        )
        design_anchors = (
            "water", "box", "crate", "target", "wall", "corridor", "route",
            "push", "rhythm", "difficulty", "choice", "readability", "space",
        )
    return execution and not any(anchor in value for anchor in design_anchors)


def _cue_repeats_current_evidence(cue_type, text, recent_guidance, evidence_signature):
    previous = ((recent_guidance or {}).get("uiCues") or {}).get(cue_type) or {}
    return (
        previous.get("evidenceSignature") == evidence_signature
        and _guidance_text_matches(text, previous.get("text"))
    )


def _warning_has_strong_evidence(text, language, rows, stage_context, play_summary):
    if not _warning_text_is_evidence_grounded(text, language):
        return False

    lowered = str(text or "").casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", lowered):
        categories = (
            ("water", ("水", "水边", "水域")),
            ("box", ("箱", "箱子")),
            ("target", ("目标", "目标点")),
            ("wall", ("墙", "通道", "角落")),
        )
        actions = ("推", "进入", "贴着", "绕", "到达", "退回", "卡住")
        consequences = ("退路", "顺序", "死锁", "软锁", "误读", "重复", "无法", "卡住")
        uncertainty = ("可能", "也许", "我有点", "我在意", "我不太放心", "值得")
    else:
        categories = (
            ("water", ("water", "water edge")),
            ("box", ("box", "crate")),
            ("target", ("target", "goal")),
            ("wall", ("wall", "corridor", "corner")),
        )
        actions = ("push", "enter", "along", "detour", "reach", "return", "stuck")
        consequences = ("escape", "order", "deadlock", "soft lock", "misread", "repeat", "unable", "stuck")
        uncertainty = ("may", "might", "I notice", "I am uneasy", "I am not sure", "worth")

    present_tiles = set("".join(str(row) for row in (rows or [])))
    tile_requirements = {"water": "@", "box": "s", "target": "t", "wall": "#"}
    mentioned = {
        name
        for name, words in categories
        if tile_requirements[name] in present_tiles and any(word.casefold() in lowered for word in words)
    }
    mechanical = (
        len(mentioned) >= 2
        and any(word.casefold() in lowered for word in actions)
        and any(word.casefold() in lowered for word in consequences)
        and any(word.casefold() in lowered for word in uncertainty)
    )
    return mechanical or _play_evidence_is_strong(play_summary)


def _play_evidence_is_strong(play_summary):
    evidence = play_summary or {}
    restarts = int(evidence.get("restartCount") or 0)
    moves = evidence.get("moveCount")
    minimum_moves = evidence.get("minimumMoves")
    pushes = evidence.get("pushCount")
    minimum_pushes = evidence.get("minimumPushes")
    excessive_moves = (
        isinstance(moves, int)
        and isinstance(minimum_moves, int)
        and minimum_moves > 0
        and moves - minimum_moves >= 8
        and moves / minimum_moves >= 1.5
    )
    excessive_pushes = (
        isinstance(pushes, int)
        and isinstance(minimum_pushes, int)
        and minimum_pushes > 0
        and pushes - minimum_pushes >= 2
        and pushes / minimum_pushes >= 1.35
    )
    return restarts > 0 or excessive_moves or excessive_pushes


def _deterministic_play_warning(language, play_summary):
    if not _play_evidence_is_strong(play_summary):
        return None
    evidence = play_summary or {}
    restarts = int(evidence.get("restartCount") or 0)
    if language == "zh-CN":
        if restarts:
            return f"我注意到这次试玩重开了 {restarts} 次；某个推动后的退路可能不够直观，我会把它当成值得继续观察的信号。"
        return "这次实际路线明显长于最短解，我会留意玩家是否在某个推动节点反复绕行，而不急着把它判定为难度。"
    if restarts:
        return (
            f"I notice this playthrough restarted {restarts} time(s); an escape route after "
            "one of the pushes may not read clearly, so I would keep an eye on that moment."
        )
    return (
        "This playthrough wandered well beyond the shortest route; I would watch for a push "
        "where the player circles back, without treating that alone as proof of difficulty."
    )


def _contextual_manual_edit(rows, language):
    rows = list(rows or [])
    water = []
    boxes = []
    targets = []
    for row_index, row in enumerate(rows):
        for column_index, tile in enumerate(str(row)):
            if tile == "@":
                water.append((row_index, column_index))
            elif tile == "s":
                boxes.append((row_index, column_index))
            elif tile == "t":
                targets.append((row_index, column_index))

    if water:
        anchor_zh = "水域边缘与相邻的推箱路线"
        anchor_en = "the water edge and its neighboring box route"
    elif boxes and targets:
        anchor_zh = "箱子进入目标区域前的通路"
        anchor_en = "the route before a box enters the target area"
    else:
        anchor_zh = "当前讨论的局部通路"
        anchor_en = "the local route under discussion"

    if language == "zh-CN":
        return (
            f"如果你想亲手验证，可以在右侧编辑器只围绕{anchor_zh}做一次小范围尝试，"
            "保存后观察第一次推动时路线选择是否更清楚；这只是用来比较体验，不必当作最终方案。"
        )
    return (
        f"If you want to test it directly, you could make one small experiment around "
        f"{anchor_en} in the editor, then save and watch whether the route choice is clearer "
        "at the first push; it is a comparison, not a required final layout."
    )


def _latest_role_content(messages, role):
    return next(
        (
            str(message.get("content") or "").strip()
            for message in reversed(messages)
            if message.get("role") == role
        ),
        "",
    )


def _latest_user_explicitly_agrees(message):
    text = str(message or "").strip().casefold()
    english = re.search(
        r"\b(?:yes|okay|ok|sounds good|go ahead|do (?:it|that)|let'?s do (?:it|that))\b",
        text,
    )
    chinese = any(
        marker in text
        for marker in ("可以", "好", "行", "同意", "就这样", "做吧", "改吧", "试试", "做点")
    )
    return english is not None or chinese


def _latest_user_confirms_revision(message):
    """Recognize a short confirmation of the immediately preceding concrete plan."""
    text = str(message or "").strip().casefold()
    if not text:
        return False

    english = re.fullmatch(
        r"(?:yes|okay|ok|sounds good|go ahead|do (?:it|that)|let'?s do (?:it|that))"
        r"[.!?]*",
        text,
    )
    chinese = re.fullmatch(
        r"(?:好(?:的)?|可以|行|同意|就这样|按这个来|照这个来|按这个做|照这个做|"
        r"(?:[一二三四五六七八九十\d]+)格(?:可以|就行|行))\s*[。！!？?]*",
        text,
    )
    return english is not None or chinese is not None


def _latest_user_explicitly_rejects(message):
    text = str(message or "").strip().casefold()
    english = re.search(
        r"\b(?:no|do not|don't|keep the original|without relaxing|not acceptable)\b",
        text,
    )
    chinese = any(
        marker in text
        for marker in (
            "不可以", "不同意", "不要降级", "不能降级", "保持原要求", "按原要求",
            "不接受放宽", "不要放宽",
        )
    )
    return english is not None or chinese


def _first_declarative_sentence(message):
    for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", str(message or "")):
        cleaned = sentence.strip()
        if (
            cleaned
            and not cleaned.endswith(("?", "？"))
            and re.sub(r"[\s，,。.!！]", "", cleaned).casefold()
            not in {"当然", "可以", "好的", "好", "同意", "没问题", "ok", "okay", "yes"}
        ):
            return cleaned
    return ""


def _revision_direction_sentence(message):
    anchors = (
        "水", "箱", "目标", "墙", "通道", "路线", "落点", "区域", "开局",
        "water", "box", "crate", "target", "goal", "wall", "corridor", "route", "opening",
    )
    actions = (
        "移动", "调整", "重排", "重组", "增加", "减少", "移除", "拉开", "收紧", "加一块", "绕", "改",
        "move", "adjust", "rearrange", "reorganize", "add", "reduce", "remove", "separate", "detour", "tighten", "change",
    )
    for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", str(message or "")):
        cleaned = sentence.strip()
        lowered = cleaned.casefold()
        if _proposal_card_is_meta_language(cleaned):
            continue
        if re.search(
            r"(?:把|将).{0,24}(?:水|墙|箱子?|目标|通道|路线|区域).{0,32}(?:挪|移|调|缩|拉|留出|打开|收紧|绕|改)",
            cleaned,
        ):
            return cleaned
        if (
            cleaned
            and not cleaned.endswith(("?", "？"))
            and any(anchor in lowered for anchor in anchors)
            and any(action in lowered for action in actions)
        ):
            return cleaned
    return ""


def _remove_questions_from_plain_reply(message):
    declarative = []

    for paragraph in (part.strip() for part in str(message or "").split("\n\n")):
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if sentence.strip()
        ]
        kept = [sentence for sentence in sentences if not sentence.endswith(("?", "？"))]

        if kept:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            declarative.append(separator.join(kept))

    return "\n\n".join(declarative) or message


def _questionless_body(message):
    declarative = []

    for paragraph in (part.strip() for part in str(message or "").split("\n\n")):
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if sentence.strip()
        ]
        kept = [sentence for sentence in sentences if not sentence.endswith(("?", "？"))]
        if kept:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            declarative.append(separator.join(kept))

    return "\n\n".join(declarative)


def _ensure_stage_one_orientation(message, rows, language):
    text = str(message or "").strip()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        process_markers = (
            "试玩", "编辑器", "右侧", "局部调整", "局部编辑", "第一反应", "直觉说起",
            "小调整", "小改", "小范围", "大改", "大幅", "可审查", "梳理思路", "亲手试",
        )
        map_markers = ("水", "墙", "箱", "目标", "玩家", "路线", "通道", "区域", "格")
        compact_guidance = (
            "你可以先说说你的第一反应或试玩当前关卡，之后不满意的话，你可以指出来，我们一起探讨商量然后制定方案，"
            "或者你自己在右侧面板进行局部编辑；我只能协助进行小范围更改、提供可审查的改动内容以及帮助你梳理思路，"
            "较大的改动我建议由你亲手试一试。"
        )
    else:
        process_markers = (
            "play the stage", "right panel", "editor", "local edit", "first impression",
            "small edit", "reviewable edit", "broad rebuild", "think through",
        )
        map_markers = ("water", "wall", "box", "target", "player", "route", "corridor", "area", "tile")
        compact_guidance = (
            "You can share a first reaction or play the Stage; I support only small, "
            "reviewable edits and thinking through them, while a broad rebuild stays designer-led."
        )

    kept = []
    for paragraph in (part.strip() for part in text.split("\n\n")):
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph)
            if sentence.strip()
        ]
        remaining = [
            sentence for sentence in sentences
            if not (
                any(marker in sentence.casefold() for marker in process_markers)
                and not any(marker in sentence.casefold() for marker in map_markers)
            )
        ]
        if remaining:
            separator = "" if re.search(r"[\u3400-\u9fff]", paragraph) else " "
            kept.append(separator.join(remaining))

    body = "\n\n".join(kept).strip()
    if compact_guidance.casefold() in body.casefold():
        return body
    return f"{body}\n\n{compact_guidance}" if body else compact_guidance


def _stage_one_coordinate_summary(rows, language):
    """Render one compact, current-snapshot entity position sentence."""
    if not rows:
        return ""
    try:
        snapshot = build_stage_snapshot(rows)
    except (TypeError, ValueError, KeyError):
        return ""
    entities = {
        str(item.get("id") or "").upper(): item
        for item in snapshot.get("entities") or []
        if isinstance(item, dict)
        and item.get("identityConfidence") == "exact"
        and item.get("id")
    }
    ordered = [label for label in ("P", "B1", "B2", "T1", "T2") if label in entities]
    if not ordered:
        return ""
    if language == "zh-CN":
        values = [
            f"{label}\u4f4d\u4e8e\u7b2c{entities[label]['row']}\u884c\u7b2c{entities[label]['column']}\u5217"
            for label in ordered
        ]
        return "\u5f53\u524d\u5feb\u7167\u4e2d\u7684\u5b9e\u4f53\u4f4d\u7f6e\u662f\uff1a" + "\uff1b".join(values) + "\u3002"
    values = [
        f"{label} is at row {entities[label]['row']}, column {entities[label]['column']}"
        for label in ordered
    ]
    return "Verified current entity positions: " + "; ".join(values) + "."


def _repair_stage_one_opening_display(message, rows, language):
    """Give legacy Stage 1 openings the current two-paragraph presentation.

    This is deliberately a read-time repair: it never rewrites the archived
    turn.  The operation guidance still appears exactly once, while old
    coordinate-heavy openings gain the same concise observation/reflection
    rhythm used for new Stage openings.
    """
    oriented = _ensure_stage_one_orientation(message, rows, language)
    paragraphs = [part.strip() for part in oriented.split("\n\n") if part.strip()]
    if not paragraphs:
        return oriented

    is_chinese = language == "zh-CN" or bool(re.search(r"[\u3400-\u9fff]", oriented))
    guidance_markers = (
        ("\u5c0f\u8303\u56f4\u66f4\u6539", "\u53ef\u5ba1\u67e5", "\u4eb2\u624b\u8bd5")
        if is_chinese
        else ("small, reviewable", "broad rebuild", "designer-led")
    )
    guidance_paragraphs = [
        item for item in paragraphs
        if any(marker.casefold() in item.casefold() for marker in guidance_markers)
    ]
    body = " ".join(
        item for item in paragraphs if item not in guidance_paragraphs
    ).strip()
    if not body:
        body = ""

    # Coordinates are current-map facts, not disposable presentation noise.
    # Older code removed every parenthesized coordinate here and left strings
    # such as "B2 \u4f4d\u4e8e\uff0c" behind. Replace position-inventory sentences
    # with one server-rendered snapshot summary instead.
    coordinate_summary = _stage_one_coordinate_summary(rows, language)
    if coordinate_summary:
        try:
            coordinate_snapshot = build_stage_snapshot(rows)
            coordinate_labels = [
                str(item.get("id")).upper()
                for item in coordinate_snapshot.get("entities") or []
                if isinstance(item, dict)
                and item.get("identityConfidence") == "exact"
                and item.get("id")
            ]
        except (TypeError, ValueError, KeyError):
            coordinate_labels = []
        entity_position = re.compile(
            r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+)(?![A-Za-z0-9]).{0,36}?"
            r"(?:\u4f4d\u4e8e|\u5728(?:\u7b2c)?|\u5750\u843d\u4e8e|"
            r"is\s+(?:at|located\s+at)|sits\s+at)",
            flags=re.IGNORECASE,
        )
        future = re.compile(
            r"(?:\u5c06|\u4f1a|\u5e0c\u671b|\u60f3(?:\u8981)?|\u63a8\u5230|\u79fb\u52a8|"
            r"\b(?:will|would|move|push|toward|from)\b)",
            flags=re.IGNORECASE,
        )
        complete_inventory = all(
            re.search(
                rf"(?<![A-Za-z0-9]){label}(?![A-Za-z0-9]).{{0,48}}"
                r"(?:\u4f4d\u4e8e|\u5728|\u5750\u843d\u4e8e|is\s+(?:at|located\s+at))"
                r".{0,32}(?:\(\s*\d+\s*[,，]\s*\d+\s*\)|"
                r"\u7b2c\s*\d+\s*\u884c\s*\u7b2c\s*\d+\s*\u5217)",
                body,
                flags=re.IGNORECASE,
            )
            for label in coordinate_labels
        )
        cleaned_sentences = []
        for sentence in re.split(
            r"(?<=[.!?\u3002\uff01\uff1f])\s*|[\r\n]+", body
        ):
            sentence = sentence.strip()
            if not sentence:
                continue
            if entity_position.search(sentence) and not future.search(sentence):
                malformed = re.search(
                    r"(?:\u4f4d\u4e8e|\u5728|\u5750\u843d\u4e8e|is\s+(?:at|located))"
                    r"\s*(?:[，,、]|\u548c\s*[，,。]|$)",
                    sentence,
                    flags=re.IGNORECASE,
                )
                # Preserve a complete, snapshot-validated model inventory;
                # otherwise remove all position inventory so the server can
                # provide one authoritative replacement.
                if malformed or not complete_inventory:
                    continue
            cleaned_sentences.append(sentence)
        body_parts = [" ".join(cleaned_sentences)]
        if not complete_inventory:
            body_parts.insert(0, coordinate_summary)
        body = "\n\n".join(part for part in body_parts if part).strip()

    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s*", body)
        if item.strip()
    ]
    reflections = (
        [
            "\u6211\u4f1a\u5148\u628a\u8fd9\u79cd\u5206\u9694\u5e26\u6765\u7684\u9996\u6b65\u9009\u62e9\uff0c\u5f53\u4f5c\u5173\u5361\u8282\u594f\u662f\u5426\u6e05\u6670\u7684\u4fe1\u53f7\u3002",
            "\u5728\u6211\u770b\u6765\uff0c\u5148\u89c2\u5bdf\u73a9\u5bb6\u5982\u4f55\u8bfb\u61c2\u7a7a\u95f4\uff0c\u6bd4\u7acb\u5373\u5224\u65ad\u8981\u6539\u54ea\u91cc\u66f4\u6709\u4ef7\u503c\u3002",
        ]
        if is_chinese
        else [
            "I would first treat that split as a signal of whether the opening rhythm reads clearly.",
            "For me, watching how a player reads the space matters before deciding what needs to change.",
        ]
    )
    for reflection in reflections:
        if len(sentences) >= 4:
            break
        sentences.append(reflection)
    body = " ".join(sentences[:5]).strip()
    body = _format_stage_opening_paragraphs(body) if body else ""
    guidance = " ".join(guidance_paragraphs).strip()
    if not guidance:
        return body
    if not body:
        return guidance
    body_paragraphs = [part.strip() for part in body.split("\n\n") if part.strip()]
    if len(body_paragraphs) == 1:
        return f"{body_paragraphs[0]}\n\n{guidance}"
    return f"{body_paragraphs[0]}\n\n{' '.join(body_paragraphs[1:])} {guidance}"


def _latest_user_states_direction(messages):
    latest = _latest_role_content(messages, "user")

    if not latest or "?" in latest or "？" in latest:
        return False

    english_direction = re.search(
        r"\b(?:i\s+(?:want|prefer|would like)|please|keep|make|change|remove|add|"
        r"preserve|reduce|increase|strengthen|weaken|go ahead|do (?:it|that)|let'?s)\b",
        latest,
        re.IGNORECASE,
    )
    chinese_direction = any(
        marker in latest
        for marker in (
            "我想",
            "我希望",
            "我更喜欢",
            "请",
            "做",
            "改",
            "保留",
            "让",
            "不要",
            "增加",
            "减少",
            "加强",
            "弱化",
        )
    )
    return english_direction is not None or chinese_direction


def _latest_direction_is_new(messages):
    """Detect a new user direction without suppressing later corrections."""
    user_messages = [
        str(message.get("content") or "").strip()
        for message in messages or []
        if message.get("role") == "user" and str(message.get("content") or "").strip()
    ]
    if not user_messages:
        return False
    latest = user_messages[-1]
    if _latest_user_explicitly_agrees(latest):
        # Acceptance of an existing proposal is not a new direction.  It must
        # not resurrect a duplicate intent card from the previous turn.
        return False
    previous_directions = [
        message for message in user_messages[:-1]
        if _latest_user_states_direction([{"role": "user", "content": message}])
    ]
    if not previous_directions:
        return True
    return not _guidance_text_matches(latest, previous_directions[-1])


def _build_minimal_stage_assessment(message, question, language, solver_metrics):
    solver_metrics = solver_metrics or {}
    steps = solver_metrics.get("solutionSteps")
    pushes = solver_metrics.get("solutionPushes")

    if language == "zh-CN":
        if isinstance(steps, int) and isinstance(pushes, int):
            solution_summary = f"确定性求解器已验证可解：{steps} 步，其中 {pushes} 次推动。"
        else:
            solution_summary = "这个已保存的 Stage 已通过确定性检查并确认可解。"
        difficulty_opinion = "在我看来，仅凭求解器验证还不足以确定实际游玩难度。"
        features = ["已验证可解的当前 Stage"]
        suggestions = ["结合设计者反馈或实际游玩证据继续判断设计效果"]
    else:
        if isinstance(steps, int) and isinstance(pushes, int):
            solution_summary = (
                f"The deterministic solver verified a solution in {steps} steps "
                f"with {pushes} pushes."
            )
        else:
            solution_summary = "This saved Stage passed deterministic validation and is solvable."
        difficulty_opinion = (
            "In my view, solver validation alone is not enough to determine the "
            "experienced difficulty."
        )
        features = ["A deterministically verified, solvable current Stage"]
        suggestions = ["Use designer feedback or play evidence to judge its design effect"]

    return {
        "solutionSummary": solution_summary,
        "difficultyOpinion": difficulty_opinion,
        "features": features,
        "suggestions": suggestions,
        "satisfactionQuestion": question,
    }


def _is_stage_one(stage_context):
    return int((stage_context or {}).get("stageNumber") or 0) == 1


def _is_human_edit_stage_opening(assessment_only, stage_context):
    return (
        assessment_only
        and not _is_stage_one(stage_context)
        and (stage_context or {}).get("source") == "human_edit"
    )


def _human_edit_intent_discussion_focus(existing_focus, stage_context, language):
    """Keep a human-edited Stage opening focused on the designer's own intention."""
    value = re.sub(r"\s+", " ", str(existing_focus or "")).strip()
    if _is_human_edit_intent_question(value, language, stage_context):
        return value

    components = ((stage_context or {}).get("changeSummary") or {}).get("components") or []
    labels = _localized_change_labels(components, language)
    signature = json.dumps(
        {
            "stage": (stage_context or {}).get("stageNumber"),
            "components": components,
            "diff": (stage_context or {}).get("diff"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    variant = int(hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8], 16) % 3

    if language == "zh-CN":
        subject = "、".join(labels) if labels else "这处布局"
        templates = (
            f"我有点好奇，你这次调整{subject}时，最希望玩家在什么时刻感到路线变了？",
            f"这处{subject}改动让我想多了解一点：你原本想让玩家的哪一次推箱判断发生变化？",
            f"我很想听听你的想法：你保存这次{subject}修改时，心里最想加强哪种游玩感受？",
        )
    else:
        subject = _join_english(labels) if labels else "this part of the layout"
        templates = (
            f"I’m curious: when you adjusted {subject}, what moment did you most want to change for the player?",
            f"This change to {subject} makes me want to hear your thinking: which push or route judgment did you hope would feel different?",
            f"I’d love to understand the thought behind this change to {subject}: what kind of play moment were you hoping to strengthen?",
        )
    return templates[variant]


def _is_human_edit_intent_question(value, language, stage_context=None):
    if value.count("?") + value.count("？") != 1:
        return False

    lowered = value.casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value):
        has_intent_marker = any(marker in value for marker in (
            "想让", "希望", "意图", "想表达", "想加强", "想保留", "想带来", "想改变",
        ))
        component_markers = {
            "outerShell": ("外壳", "边界"),
            "water": ("水",),
            "internalWalls": ("墙", "通道"),
            "boxes": ("箱",),
            "targets": ("目标",),
            "player": ("玩家", "起点"),
            "floorArea": ("地面", "空间", "布局"),
        }
    else:
        has_intent_marker = bool(re.search(
            r"\b(?:hope|hoped|want|wanted|intend|intended|aim|aimed|meaning|thinking|trying)\b",
            lowered,
        ))
        component_markers = {
            "outerShell": ("shell", "boundary", "edge"),
            "water": ("water",),
            "internalWalls": ("wall", "corridor", "route"),
            "boxes": ("box", "crate"),
            "targets": ("target", "goal"),
            "player": ("player", "start"),
            "floorArea": ("floor", "space", "layout"),
        }

    if not has_intent_marker:
        return False

    components = ((stage_context or {}).get("changeSummary") or {}).get("components") or []
    relevant_markers = [
        marker
        for component in components
        for marker in component_markers.get(component, ())
    ]
    return not relevant_markers or any(marker in lowered for marker in relevant_markers)


def _build_task_instructions(assessment_only, stage_context=None):
    if assessment_only:
        stage_one_instruction = (
            "This is Stage 1. Do not ask a question. Keep your concrete map observation and "
            "personal response at their natural length. Do not write process, trial, editor, or "
            "scope guidance: the backend appends the one fixed closing paragraph itself. "
            if _is_stage_one(stage_context)
            else ""
        )
        human_edit_opening_instruction = (
            "This is the first opening after a directly verified human edit. Ground the response in "
            "changeSummary, before/after map facts, solver evidence, and any supplied play evidence. "
            "A safe edit should be confirmed and analyzed in ordinary prose without manufacturing a "
            "disagreement or warning. If concrete evidence supports a mechanical risk or a conflict "
            "with the designer's stated goal, include exactly one evidence-backed warning and an active "
            "human_edit disagreement with all four summaries; never undo or overwrite the edit. Do not "
            "state the designer's intention as fact. A follow-up question is optional for this opening "
            "and must not be used as a blue card unless a structured disagreement is active. "
            if (stage_context or {}).get("source") == "human_edit" and not _is_stage_one(stage_context)
            else ""
        )
        return (
            "Open a friendly discussion for this newly saved Stage. Use observe_stage "
            "and write one to three short paragraphs, choosing the length and rhythm that "
            "fit what is worth saying. Select only one or two concrete "
            "map choices that are genuinely worth discussing; do not inventory the whole "
            "map. Offer at least one grounded personal perspective, design association, "
            "or gentle concern, clearly as your view rather than fact. Do not force a "
            "question or use one as a routine closing. When a question would materially "
            "help, ask one open question about a concrete choice the provenance rules say "
            "the designer actually controlled, or—when exact tiles were generated—about "
            "the designer's reaction to the generated outcome. Do not say Welcome "
            "to Stage, ask for the designer's overall intended player experience, offer "
            "preselected categories or an either-or choice, infer an intention, enumerate "
            "solver moves, or offer or generate a changed map. Include the grounded "
            "structured assessment only as archival research data, not as the prose style. "
            + stage_one_instruction
            + human_edit_opening_instruction
        )

    context = stage_context or {}
    explicit_action = context.get("explicitAction") or "none"
    action_instruction = ""
    if explicit_action == "challenge_revision":
        action_instruction = (
            "This is the first response after the designer clicked challenge_revision on a purple card. "
            "Use ordinary prose only: restate the cited proposal, explain why it was suggested and which "
            "play moment it targets, then invite the designer to state the precise disagreement and reason. "
            "Do not output proposalOffer, disagreement, followUpQuestion, uiCues, or map rows."
        )
    elif explicit_action == "alternative_revision":
        action_instruction = (
            "The designer requested an alternative to the supplied purple proposal. Return a conceptual "
            "offer_revision with a clearly different local treatment and playable rationale. Do not output "
            "map rows or a disagreement card. The alternative must not merely rename the original proposal."
        )
    elif explicit_action == "execute_revision":
        action_instruction = (
            "The designer explicitly authorized the cited purple proposal through execute_revision. "
            "The caller, not ordinary chat, controls the two-agent map execution and validation pipeline."
        )
    elif context.get("activeDisagreement"):
        action_instruction = (
            "An unresolved disagreement is active. Keep the disagreement summary current. Return an active "
            "disagreement unless the latest designer message gives a reason that resolves it. Do not output "
            "a purple proposal while status is active. If consensus is reached, use resolved with resolution "
            "user, ai, compromise, or retain_current; retain_current must not create a map proposal."
        )
    elif context.get("deferRevisionExecution"):
        action_instruction = (
            "This ordinary web chat request may describe a direct map change, but it is not an execution action. "
            "Analyze the direction and produce a conceptual REVISION card only; never return map rows."
        )

    return (
        "Continue as a rational, warm design friend with an independent first-person view. "
        "Address the latest message without restating it, vary your opening and paragraph "
        "rhythm, and do not fall into an acknowledgement-evaluation-question template. "
        "At an unclear evaluation, meaningful trade-off, actionable direction, or new play "
        "evidence, actively ask one concrete question; otherwise let a useful observation "
        "stand. Do not agree reflexively. assessment should "
        "normally be null. Use offer_revision without "
        "proposedRows for an unsolicited revision idea; use deliver_revision with a "
        "complete proposedRows map only after explicit designer authorization. "
        + action_instruction
    )


def _build_draft_provenance_guidance(stage_context):
    source = stage_context.get("source")
    initial_method = stage_context.get("initialDraftMethod")

    if source == "human_edit":
        return (
            "This saved Stage was directly edited by the designer in the workbench. "
            "Use changeSummary to attribute only the listed changed components to the "
            "designer; do not guess motivations, and continue to distinguish earlier "
            "generator-authored structure from these verified edits."
        )

    if source == "llm_accepted":
        return (
            "This Stage came from an LLM proposal that the designer explicitly accepted. "
            "Describe it as an accepted shared direction, not as tile placement performed "
            "by the designer and not as an autonomous change already made by the LLM."
        )

    if source == "restored":
        return (
            "This Stage restores an earlier saved version. Frame the conversation as "
            "revisiting that version; do not infer who originally placed a specific tile."
        )

    if source == "initial" and initial_method == "algorithm_demo":
        return (
            "This is a standalone algorithm-generated demo draft. The server algorithm "
            "created every exact visible tile placement, including the player, both boxes, "
            "both targets, walls, and water. Never attribute any visible tile or layout "
            "choice to the designer, and do not infer a hidden design intention. The first "
            "turn should discuss only observable structure, routes, and push relationships; "
            "treat these as generated outcomes and, when useful, invite the designer to say "
            "how the result feels or what they would like to explore next."
        )

    if source == "initial" and initial_method == "description_generation":
        return (
            "This is a DG initial draft. The designer supplied an upstream description "
            "and generation parameters, but the generator produced every exact tile "
            "placement. Never ask why the designer placed a particular wall, water tile, "
            "box, target, player, or corridor. Do not invent or quote parameter values "
            "because they are not supplied here. Discuss exact layout features as outcomes "
            "of the generated draft. Never say or imply that the designer intended, "
            "wanted, hoped for, chose, or authored a visible layout feature. If a question "
            "is genuinely useful, make it an open comparison about how the generated "
            "result relates to their expected parameter effect, what surprised them, or "
            "which observed effect they might want to strengthen or revise. A suitable "
            "form is: 'How does this generated result compare with what you expected from "
            "your settings?' Apply this "
            "attribution rule "
            "throughout conversation on this Stage, unless the designer explicitly adopts "
            "or discusses a generated feature."
        )

    if source == "initial" and initial_method == "partial_completion":
        return (
            "This is a PC initial draft. The designer authored a partial sketch, including "
            "the box starts, targets, and broad room/wall constraints; the completion "
            "system added the exact water, generated internal walls, player start, and "
            "remaining completed layout. Because the final map does not identify which "
            "individual wall cells came from the sketch, never attribute a particular "
            "internal wall to the designer. Do not ask why they placed water, a specific "
            "internal wall, or the player start, and never claim that the completion "
            "system produced or filled in all walls. It added some generated internal "
            "walls while other wall constraints came from the sketch. You may ask about "
            "the box-target "
            "relationship, the broad sketched playable boundary, or how the generated "
            "completion supports or conflicts with their sketch. Apply this attribution "
            "rule throughout conversation on this Stage, unless the designer explicitly "
            "adopts or discusses a generated feature."
        )

    return (
        "Authorship of exact initial tiles is unavailable. Discuss observable effects "
        "without claiming that the designer personally placed a specific tile."
    )


def _proposal_operation_family(operations):
    operation_set = set(operations)
    if operation_set.issubset({"add_wall", "remove_wall"}):
        return "adjust_internal_walls"
    if operation_set.issubset({"add_water", "remove_water"}):
        return "reshape_water"
    if operation_set == {"move_player"}:
        return "relocate_start"
    if operation_set == {"move_box"}:
        return "relocate_box"
    if operation_set == {"move_target"}:
        return "relocate_target"
    if operation_set.issubset({"move_box", "move_target", "add_wall", "remove_wall"}):
        return "change_box_order"
    raise ValueError(
        "revisionPlan.changes must use one supported operation family."
    )


def _validate_revision_plan_payload(value, rows=None, entity_bindings=None):
    """Normalize the Chat plan into the existing exact executionBrief shape."""
    if not isinstance(value, dict):
        raise ValueError("revisionPlan must be an object.")
    required = {"objective", "changes", "preserve", "rationale", "expectedEffect"}
    if set(value) != required:
        raise ValueError(
            "revisionPlan must contain objective, changes, preserve, rationale, and expectedEffect."
        )

    text_fields = {}
    for field in ("objective", "rationale", "expectedEffect"):
        item = value.get(field)
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 1000
            or "\n" in item
            or "\r" in item
        ):
            raise ValueError(f"revisionPlan.{field} is invalid.")
        text_fields[field] = item.strip()

    changes = value.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 12:
        raise ValueError("revisionPlan.changes must contain one to twelve exact cell changes.")

    transition_by_operation = {
        (".", "#"): "add_wall",
        ("#", "."): "remove_wall",
        ("p", "."): "move_player",
        (".", "p"): "move_player",
        ("s", "."): "move_box",
        (".", "s"): "move_box",
        ("t", "."): "move_target",
        (".", "t"): "move_target",
        (".", "@"): "add_water",
        ("@", "."): "remove_water",
    }
    transitions = []
    operations = []
    seen_coordinates = set()
    for index, change in enumerate(changes):
        if (
            not isinstance(change, dict)
            or not {
                "row", "column", "before", "after", "operation"
            }.issubset(change)
            or set(change) - {
                "row", "column", "before", "after", "operation", "anchorEntity"
            }
        ):
            raise ValueError(
                f"revisionPlan.changes[{index}] must contain row, column, before, after, and operation."
            )
        row = change["row"]
        column = change["column"]
        before = change["before"]
        after = change["after"]
        operation = change["operation"]
        anchor_entity = change.get("anchorEntity")
        if (
            isinstance(row, bool)
            or isinstance(column, bool)
            or not isinstance(row, int)
            or not isinstance(column, int)
            or not 1 <= row <= 10
            or not 1 <= column <= 12
            or before not in set("#.@pst")
            or after not in set("#.@pst")
            or before == after
            or (row, column) in seen_coordinates
            or operation not in {
                "add_wall", "remove_wall", "move_player", "move_box", "move_target",
                "add_water", "remove_water",
            }
            or transition_by_operation.get((before, after)) != operation
            or (
                anchor_entity is not None
                and anchor_entity not in {"P", "B1", "B2", "T1", "T2"}
            )
        ):
            raise ValueError(f"revisionPlan.changes[{index}] is not an exact supported transition.")
        seen_coordinates.add((row, column))
        transitions.append({
            "row": row,
            "column": column,
            "from": before,
            "to": after,
            **(
                {"anchorEntity": anchor_entity}
                if anchor_entity is not None
                else {}
            ),
        })
        operations.append(operation)

    preserve = value.get("preserve")
    valid_preserve = {
        "outer_shell", "player", "boxes", "targets", "water", "walls", "unrelated_areas",
    }
    if (
        not isinstance(preserve, list)
        or any(not isinstance(item, str) for item in preserve)
        or len(set(preserve)) != len(preserve)
        or any(item not in valid_preserve for item in preserve)
    ):
        raise ValueError("revisionPlan.preserve is invalid.")

    effect = _proposal_operation_family(operations)
    brief = {
        "schemaVersion": 1,
        "effect": effect,
        "anchors": [],
        "focus": None,
        "requiredTransitions": transitions,
        "allowedOperators": list(dict.fromkeys(operations)),
        "preserve": list(preserve),
        "playObjective": text_fields["objective"],
    }
    return _validate_execution_brief(brief, rows, entity_bindings)


def _validate_execution_brief(value, rows=None, entity_bindings=None):
    """Validate the hidden, machine-facing part of a conceptual revision card."""
    if not isinstance(value, dict):
        raise ValueError("executionBrief must be an object.")
    allowed = {
        "schemaVersion", "effect", "anchors", "focus", "requiredTransitions",
        "allowedOperators", "preserve", "playObjective",
    }
    if not set(value).issubset(allowed):
        raise ValueError("executionBrief contains unexpected fields.")
    if value.get("schemaVersion", 1) != 1:
        raise ValueError("executionBrief.schemaVersion is unsupported.")
    effect = value.get("effect")
    if effect is not None and effect not in {
        "open_route", "narrow_route", "adjust_internal_walls", "relocate_start",
        "relocate_box", "relocate_target", "reshape_water", "change_box_order",
    }:
        raise ValueError("executionBrief.effect is invalid.")
    anchors = value.get("anchors", [])
    if (
        not isinstance(anchors, list)
        or len(anchors) > 5
        or any(not isinstance(anchor, str) for anchor in anchors)
        or len(set(anchors)) != len(anchors)
        or any(anchor not in {"P", "B1", "B2", "T1", "T2"} for anchor in anchors)
    ):
        raise ValueError("executionBrief.anchors is invalid.")
    focus = value.get("focus")
    if focus is not None:
        if not isinstance(focus, dict) or set(focus) != {"row", "column", "radius"}:
            raise ValueError("executionBrief.focus is invalid.")
        if any(
            isinstance(focus[key], bool) or not isinstance(focus[key], int)
            for key in ("row", "column", "radius")
        ) or not 1 <= focus["row"] <= 10 or not 1 <= focus["column"] <= 12 or not 1 <= focus["radius"] <= 3:
            raise ValueError("executionBrief.focus is outside the Stage.")
    transitions = value.get("requiredTransitions", [])
    if not isinstance(transitions, list) or len(transitions) > 12:
        raise ValueError("executionBrief.requiredTransitions is invalid.")
    normalized_transitions = []
    seen_coordinates = set()
    allowed_tiles = set("#.@pst")
    for transition in transitions:
        if (
            not isinstance(transition, dict)
            or not {"row", "column", "from", "to"}.issubset(transition)
            or set(transition) - {"row", "column", "from", "to", "anchorEntity"}
        ):
            raise ValueError("executionBrief has an invalid required transition.")
        row, column = transition["row"], transition["column"]
        before, after = transition["from"], transition["to"]
        if (
            isinstance(row, bool) or isinstance(column, bool)
            or not isinstance(row, int) or not isinstance(column, int)
            or not 1 <= row <= 10 or not 1 <= column <= 12
            or before not in allowed_tiles or after not in allowed_tiles
            or before == after or (row, column) in seen_coordinates
        ):
            raise ValueError("executionBrief has an invalid required transition.")
        seen_coordinates.add((row, column))
        anchor_entity = transition.get("anchorEntity")
        if anchor_entity is not None and anchor_entity not in {"P", "B1", "B2", "T1", "T2"}:
            raise ValueError("executionBrief has an invalid anchorEntity.")
        normalized_transitions.append({
            "row": row,
            "column": column,
            "from": before,
            "to": after,
            **(
                {"anchorEntity": anchor_entity}
                if anchor_entity is not None
                else {}
            ),
        })
    operators = value.get("allowedOperators", [])
    valid_operators = {
        "add_wall", "remove_wall", "move_player", "move_box", "move_target",
        "add_water", "remove_water",
    }
    if (
        not isinstance(operators, list)
        or not 1 <= len(operators) <= 7
        or any(not isinstance(operator, str) for operator in operators)
        or len(set(operators)) != len(operators)
        or any(operator not in valid_operators for operator in operators)
    ):
        raise ValueError("executionBrief.allowedOperators is invalid.")
    preserve = value.get("preserve", [])
    valid_preserve = {
        "outer_shell", "player", "boxes", "targets", "water", "walls", "unrelated_areas",
    }
    if (
        not isinstance(preserve, list)
        or any(not isinstance(component, str) for component in preserve)
        or len(set(preserve)) != len(preserve)
        or any(component not in valid_preserve for component in preserve)
    ):
        raise ValueError("executionBrief.preserve is invalid.")
    objective = value.get("playObjective")
    if objective is not None and (
        not isinstance(objective, str) or not objective.strip() or len(objective) > 120
        or "\n" in objective or "\r" in objective
    ):
        raise ValueError("executionBrief.playObjective is invalid.")

    operator_for_transition = {
        (".", "#"): "add_wall", ("#", "."): "remove_wall",
        ("p", "."): "move_player", (".", "p"): "move_player",
        ("s", "."): "move_box", (".", "s"): "move_box",
        ("t", "."): "move_target", (".", "t"): "move_target",
        (".", "@"): "add_water", ("@", "."): "remove_water",
    }
    for transition in normalized_transitions:
        operator = operator_for_transition.get((transition["from"], transition["to"]))
        if operator is None or operator not in operators:
            raise ValueError("executionBrief.allowedOperators cannot realize a required transition.")
        if operator in {"add_wall", "remove_wall"} and "walls" in preserve:
            raise ValueError("executionBrief preserves walls but requires a wall edit.")
        component = {
            "move_player": "player", "move_box": "boxes", "move_target": "targets",
            "add_water": "water", "remove_water": "water",
        }.get(operator)
        if component and component in preserve:
            raise ValueError(f"executionBrief preserves {component} but requires an edit.")
        if focus is not None and max(
            abs(focus["column"] - transition["column"]),
            abs(focus["row"] - transition["row"]),
        ) > focus["radius"]:
            raise ValueError("executionBrief.focus does not contain every required transition.")
    if rows is not None:
        if len(rows) != 10 or any(len(row) != 12 for row in rows):
            raise ValueError("executionBrief cannot be checked against an invalid Stage.")
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
        fact_entities = list(facts.get("entities") or [])
        known_anchors = {
            item.get("id")
            for item in fact_entities
            if item.get("id")
        }
        if any(anchor not in known_anchors for anchor in anchors):
            raise ValueError("executionBrief names an entity that is not on the saved Stage.")
        entities_by_id = {
            item.get("id"): item
            for item in fact_entities
            if item.get("id")
        }
        if any(
            entities_by_id.get(anchor, {}).get("identityConfidence") != "exact"
            for anchor in anchors
        ):
            raise ValueError("executionBrief cannot use an entity with unknown identity.")
        entity_tiles = {"P": "p", "B1": "s", "B2": "s", "T1": "t", "T2": "t"}
        for label in anchors:
            entity = entities_by_id[label]
            tile = entity_tiles[label]
            source_indexes = [
                index
                for index, item in enumerate(normalized_transitions)
                if item.get("anchorEntity") is None
                and item["from"] == tile
                and item["to"] == "."
                and (item["row"], item["column"])
                == (entity["row"], entity["column"])
            ]
            destination_indexes = [
                index
                for index, item in enumerate(normalized_transitions)
                if item.get("anchorEntity") is None
                and item["from"] == "."
                and item["to"] == tile
            ]
            if len(source_indexes) == 1 and len(destination_indexes) == 1:
                normalized_transitions[source_indexes[0]] = {
                    **normalized_transitions[source_indexes[0]],
                    "anchorEntity": label,
                }
                normalized_transitions[destination_indexes[0]] = {
                    **normalized_transitions[destination_indexes[0]],
                    "anchorEntity": label,
                }
        tagged = {}
        for transition in normalized_transitions:
            label = transition.get("anchorEntity")
            if label is None:
                continue
            entity = entities_by_id.get(label)
            if entity is None:
                raise ValueError("executionBrief anchorEntity is not on the saved Stage.")
            if entity.get("identityConfidence") == "unknown":
                raise ValueError("executionBrief cannot use an entity with unknown identity.")
            tile = {"P": "p", "B1": "s", "B2": "s", "T1": "t", "T2": "t"}[label]
            coordinate = (transition["row"], transition["column"])
            entity_coordinate = (entity["row"], entity["column"])
            is_source = transition["from"] == tile and transition["to"] == "."
            is_destination = transition["from"] == "." and transition["to"] == tile
            if not (is_source or is_destination):
                raise ValueError("executionBrief anchorEntity does not match its tile transition.")
            if is_source and coordinate != entity_coordinate:
                raise ValueError(
                    f"executionBrief anchorEntity {label} does not match its source coordinate."
                )
            tagged.setdefault(label, {"sources": [], "destinations": []})[
                "sources" if is_source else "destinations"
            ].append(transition)

        for label, group in tagged.items():
            if len(group["sources"]) != 1 or len(group["destinations"]) != 1:
                raise ValueError(
                    f"executionBrief entity move for {label} requires one source and one destination."
                )

        moving_operators = {
            operator_for_transition[(item["from"], item["to"])]
            for item in normalized_transitions
            if (item["from"], item["to"]) in operator_for_transition
        } & {"move_player", "move_box", "move_target"}
        if (
            moving_operators
            and entity_bindings is not None
            and any(item.get("anchorEntity") is None for item in normalized_transitions)
        ):
            raise ValueError(
                "entity movement transitions must identify their anchorEntity."
            )
        if any(label not in anchors for label in tagged):
            raise ValueError("executionBrief anchors must include every transition entity.")
        if moving_operators and effect in {"relocate_start", "relocate_box", "relocate_target"}:
            expected_prefix = {
                "relocate_start": "P",
                "relocate_box": "B",
                "relocate_target": "T",
            }[effect]
            relevant_anchors = [
                anchor for anchor in anchors if anchor.startswith(expected_prefix)
            ]
            if len(relevant_anchors) != 1:
                raise ValueError(
                    f"{effect} requires exactly one explicitly bound entity anchor."
                )
            anchor = relevant_anchors[0]
            entity = entities_by_id[anchor]
            source_coordinates = {
                (item["row"], item["column"])
                for item in normalized_transitions
                if item["from"] == {"P": "p", "B1": "s", "B2": "s", "T1": "t", "T2": "t"}[anchor]
                and item["to"] == "."
            }
            if source_coordinates != {(entity["row"], entity["column"])}:
                raise ValueError(
                    f"{effect} transitions do not operate on anchor {anchor}."
                )

        for transition in normalized_transitions:
            current = rows[transition["row"] - 1][transition["column"] - 1]
            if current != transition["from"]:
                raise ValueError(
                    "executionBrief coordinate conflict: "
                    f"row {transition['row']}, column {transition['column']} is {current!r}, "
                    f"not {transition['from']!r}."
                )
    return {
        "schemaVersion": 1,
        "effect": effect,
        "anchors": list(anchors),
        "focus": dict(focus) if focus is not None else None,
        "requiredTransitions": normalized_transitions,
        "allowedOperators": list(operators),
        "preserve": list(preserve),
        "playObjective": objective.strip() if isinstance(objective, str) else None,
    }


def validate_execution_brief(value, rows=None, entity_bindings=None):
    """Public server-side entry point for validating a proposal execution brief."""
    return _validate_execution_brief(value, rows, entity_bindings)


def normalize_revision_plan(value, rows=None, entity_bindings=None):
    """Public server-side entry point for normalizing a Chat revision plan."""
    return _validate_revision_plan_payload(value, rows, entity_bindings)


def _validate_guidance(payload, assessment_only, language="en", stage_context=None, rows=None):
    if payload is None:
        raise ValueError("guidance is required.")

    if not isinstance(payload, dict):
        raise ValueError("guidance must be an object.")

    required_fields = {
        "move",
        "intentHypothesis",
        "intentConfidence",
        "followUpQuestion",
        "proposalOffer",
    }
    allowed_fields = required_fields | {
        "uiCues",
        "disagreement",
        "designContextPatch",
        "coordinateLinks",
    }

    if not required_fields.issubset(payload) or not set(payload).issubset(allowed_fields):
        raise ValueError("guidance contains unexpected or missing fields.")

    move = payload.get("move")

    if move not in GUIDANCE_MOVES:
        raise ValueError("guidance.move is invalid.")

    intent_hypothesis = _clean_optional_text(
        payload.get("intentHypothesis"),
        "guidance.intentHypothesis",
    )
    intent_suppressed = False

    if intent_hypothesis is not None:
        intent_hypothesis = _normalize_single_level_language(intent_hypothesis)
        intent_hypothesis = _normalize_intent_hypothesis(
            intent_hypothesis,
            language,
        )
        if _intent_is_only_execution_authorization(intent_hypothesis, language):
            intent_hypothesis = None
            intent_suppressed = True
    intent_confidence = payload.get("intentConfidence")

    if intent_hypothesis is None:
        if intent_confidence is not None and not intent_suppressed:
            raise ValueError("intentConfidence requires intentHypothesis.")
        intent_confidence = None
    elif intent_confidence not in INTENT_CONFIDENCE_LEVELS:
        raise ValueError("intentConfidence is invalid.")

    follow_up_question = _clean_optional_text(
        payload.get("followUpQuestion"),
        "guidance.followUpQuestion",
    )

    if follow_up_question is not None:
        follow_up_question = _normalize_single_level_language(follow_up_question)
        question_marks = follow_up_question.count("?") + follow_up_question.count("？")

        if question_marks > 1:
            raise ValueError("followUpQuestion must contain at most one question.")
        if question_marks == 0 and not _discussion_insight_is_useful(
            follow_up_question,
            language,
        ):
            raise ValueError(
                "A declarative followUpQuestion must be a concrete first-person design insight."
            )
        if assessment_only and question_marks == 1:
            follow_up_question = _normalize_opening_question(follow_up_question)
    proposal_offer = payload.get("proposalOffer")

    if proposal_offer is not None:
        if move != "offer_revision" or not isinstance(proposal_offer, dict):
            raise ValueError("proposalOffer requires the offer_revision move.")

        if not {"summary", "rationale"}.issubset(proposal_offer):
            raise ValueError("proposalOffer must contain summary and rationale.")
        if not set(proposal_offer).issubset({
            "summary", "rationale", "executionBrief", "revisionPlan",
        }):
            raise ValueError("proposalOffer contains an invalid field.")

        raw_proposal_offer = proposal_offer
        proposal_offer = {
            "summary": _normalize_single_level_language(
                _clean_text(proposal_offer.get("summary"), "proposalOffer.summary")
            ),
            "rationale": _normalize_single_level_language(_clean_text(
                proposal_offer.get("rationale"),
                "proposalOffer.rationale",
            )),
        }
        normalized_brief = None
        if "revisionPlan" in raw_proposal_offer:
            normalized_brief = _validate_revision_plan_payload(
                raw_proposal_offer["revisionPlan"],
                rows,
                (stage_context or {}).get("entityBindings"),
            )
        if "executionBrief" in raw_proposal_offer:
            execution_brief = _validate_execution_brief(
                raw_proposal_offer["executionBrief"],
                rows,
                (stage_context or {}).get("entityBindings"),
            )
            if normalized_brief is not None and execution_brief != normalized_brief:
                raise ValueError(
                    "proposalOffer executionBrief conflicts with revisionPlan."
                )
            normalized_brief = execution_brief
        if normalized_brief is not None:
            proposal_offer["executionBrief"] = normalized_brief
        if not _proposal_offer_has_exact_execution_plan(proposal_offer):
            raise ValueError(
                "proposalOffer requires exact requiredTransitions; ask for clarification when the "
                "intended cells cannot be resolved from the authoritative map facts."
            )
    elif move == "offer_revision":
        raise ValueError("The offer_revision move requires proposalOffer.")

    ui_cues = payload.get("uiCues", [])

    if not isinstance(ui_cues, list) or len(ui_cues) > 2:
        raise ValueError("guidance.uiCues must be an array with at most two items.")

    normalized_cues = []
    seen_cue_types = set()

    for index, cue in enumerate(ui_cues):
        if not isinstance(cue, dict) or set(cue) != {"type", "text"}:
            raise ValueError("Each guidance.uiCue must contain type and text.")

        cue_type = cue.get("type")

        if cue_type not in UI_CUE_TYPES:
            raise ValueError("guidance.uiCue.type is invalid.")

        if cue_type in seen_cue_types:
            raise ValueError("guidance.uiCues cannot repeat a type.")

        if (
            cue_type in {"warning", "tradeoff"}
            and (stage_context or {}).get("discussionCardMode") == "disagreement_only"
            and not _warning_text_is_evidence_grounded(
                str(cue.get("text") or ""),
                language,
            )
        ):
            raise ValueError(
                "A new warning must cite a concrete map or play interaction."
            )

        seen_cue_types.add(cue_type)
        normalized_cues.append(
            {
                "type": cue_type,
                "text": _normalize_single_level_language(
                    _clean_text(cue.get("text"), f"guidance.uiCues[{index}].text")
                ),
            }
        )

    risk_cue_types = seen_cue_types.intersection({"warning", "tradeoff"})

    if len(risk_cue_types) > 1:
        raise ValueError("guidance.uiCues can contain only one warning.")

    if move == "challenge_tradeoff" and not risk_cue_types:
        raise ValueError("challenge_tradeoff requires a warning uiCue.")

    disagreement = _validate_disagreement(payload.get("disagreement"), language)
    if disagreement and disagreement["status"] == "active":
        if proposal_offer is not None:
            raise ValueError("An active disagreement cannot contain a proposalOffer.")
        if move == "offer_revision":
            raise ValueError("An active disagreement cannot use offer_revision.")
        if not risk_cue_types and disagreement["subject"] in {
            "ai_revision", "human_edit", "user_request"
        }:
            # A blue card is the unresolved decision; a red card is optional for
            # ordinary disagreements, but risk claims must remain evidence-backed.
            pass
    if disagreement and disagreement["status"] == "resolved" and disagreement["resolution"] == "retain_current":
        if proposal_offer is not None:
            raise ValueError("retain_current cannot contain a proposalOffer.")

    if assessment_only:
        if move != "observe_stage":
            raise ValueError("A Stage opening must use observe_stage.")
        human_edit_disagreement = (
            disagreement is not None
            and disagreement["status"] == "active"
            and disagreement["subject"] == "human_edit"
        )
        human_edit_risk_review = (
            _is_human_edit_stage_opening(assessment_only, stage_context)
            and bool(risk_cue_types)
        )
        if intent_hypothesis is not None or proposal_offer is not None or (
            normalized_cues and not (human_edit_disagreement or human_edit_risk_review)
        ):
            raise ValueError("A Stage opening cannot infer intention or offer a revision.")
        if human_edit_disagreement and not risk_cue_types:
            raise ValueError("A human-edit disagreement opening requires a warning uiCue.")

    normalized_guidance = {
        "move": move,
        "intentHypothesis": intent_hypothesis,
        "intentConfidence": intent_confidence,
        "followUpQuestion": follow_up_question,
        "proposalOffer": proposal_offer,
        "disagreement": disagreement,
        "uiCues": normalized_cues,
        "coordinateLinks": _normalize_coordinate_links(
            payload.get("coordinateLinks"),
            rows=rows,
        ),
    }
    if "designContextPatch" in payload:
        try:
            normalized_guidance["designContextPatch"] = validate_design_context_patch(
                payload.get("designContextPatch")
            )
        except (TypeError, ValueError) as exception:
            normalized_guidance["designContextPatchError"] = str(exception)[:500]
    return normalized_guidance


def _normalize_coordinate_links(value, rows=None):
    """Keep only safe, map-bounded visual annotations from the model."""
    if not isinstance(value, list):
        return []

    height = len(rows) if rows is not None else 10
    width = len(rows[0]) if rows and isinstance(rows[0], str) else 12
    normalized = []
    seen = set()

    for item in value[:COORDINATE_LINK_LIMIT]:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        source = item.get("from")
        destination = item.get("to")
        if not isinstance(text, str) or not text.strip():
            continue
        if len(text.strip()) > 400:
            continue
        if not isinstance(source, dict) or not isinstance(destination, dict):
            continue

        coordinates = []
        valid = True
        for point in (source, destination):
            row = point.get("row")
            column = point.get("column")
            if (
                isinstance(row, bool)
                or not isinstance(row, int)
                or isinstance(column, bool)
                or not isinstance(column, int)
                or not (1 <= row <= height and 1 <= column <= width)
            ):
                valid = False
                break
            if rows is not None:
                if row > len(rows) or not isinstance(rows[row - 1], str):
                    valid = False
                    break
                if (
                    column > len(rows[row - 1])
                    or rows[row - 1][column - 1] not in {".", "p", "s", "t"}
                ):
                    valid = False
                    break
            coordinates.append((row, column))

        if not valid or coordinates[0] == coordinates[1]:
            continue

        link = {
            "text": text.strip(),
            "from": {
                "row": coordinates[0][0],
                "column": coordinates[0][1],
            },
            "to": {
                "row": coordinates[1][0],
                "column": coordinates[1][1],
            },
        }
        key = (
            link["text"],
            coordinates[0],
            coordinates[1],
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(link)

    return normalized


def _route_text_has_direction(text):
    # This is only a cheap pre-filter. The authoritative check below also
    # requires the connector to sit between the link's first and last anchors.
    return bool(re.search(
        r"(?:\u2192|->|\b(?:to|toward|towards|through|via|along|walk(?:ing)?|"
        r"move(?:s|d|ing)?|go(?:es|ing)?|lead(?:s|ing)?)\b|"
        r"到|向|往|朝向|通往|通向|经过|沿着|走到|走向|前往|抵达|"
        r"推向|推到|移动到|绕到|绕向)",
        str(text or ""),
        flags=re.IGNORECASE,
    ))


def _route_anchor_matches(text, rows, entity_bindings=None):
    """Return ordered entity/coordinate anchors with their visible spans."""
    try:
        entity_coordinates = _map_entity_coordinates(rows, entity_bindings)
        facts = build_map_facts(rows, entity_bindings=entity_bindings)
        uncertain = {
            item.get("id")
            for item in facts.get("entities") or []
            if item.get("id") and item.get("identityConfidence") != "exact"
        }
    except (TypeError, ValueError, KeyError):
        return None

    coordinate_pattern = re.compile(
        r"[\(\uFF08]\s*(\d{1,2})\s*[,\uFF0C]\s*(\d{1,2})\s*[\)\uFF09]"
    )
    entity_pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+|玩家|起点)(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )
    anchors = []
    for match in coordinate_pattern.finditer(str(text or "")):
        anchors.append({
            "start": match.start(),
            "end": match.end(),
            "point": {
                "row": int(match.group(1)),
                "column": int(match.group(2)),
            },
        })
    for match in entity_pattern.finditer(str(text or "")):
        label = match.group(0).upper()
        if label in {"玩家", "起点"}:
            label = "P"
        if label in uncertain:
            return None
        point = entity_coordinates.get(label)
        if point is None:
            return None
        anchors.append({
            "start": match.start(),
            "end": match.end(),
            "point": dict(point),
        })
    anchors.sort(key=lambda item: item["start"])
    return anchors


def _route_anchors(text, rows, entity_bindings=None):
    """Return ordered, authoritative coordinate anchors from route prose."""
    matches = _route_anchor_matches(text, rows, entity_bindings)
    if matches is None:
        return None
    anchors = []
    for item in matches:
        point = item["point"]
        if anchors and anchors[-1] == point:
            continue
        anchors.append(point)
    return anchors


def _route_text_is_concise(text):
    """Reject a whole evaluation/report being used as one route label."""
    value = str(text or "").strip()
    if not value or "\n" in value or "\r" in value or len(value) > 220:
        return False
    return len(re.findall(r"[.!?。！？]", value)) <= 1


def _legacy_route_relation_is_explicit(text, source, destination, rows):
    """Require a movement connector between the declared visible endpoints."""
    if not _route_text_is_concise(text):
        return False
    value = str(text or "")
    if re.search(
        r"(?:\b(?:extend(?:s|ed|ing)?|lie(?:s)?|located|situated|beside|near|adjacent|"
        r"separated|position(?:ed)?|at)\b|"
        r"延伸|分布|位于|坐落|紧挨|靠近|相邻|隔着|旁边|左侧|右侧|上方|下方|"
        r"距离|位置关系|坐标对应)",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    matches = _route_anchor_matches(text, rows)
    if not matches or len(matches) < 2:
        return False
    anchors = _route_anchors(text, rows)
    if not anchors or len(anchors) < 2:
        return False
    # A single visual link must describe one local movement relation.  When a
    # sentence names several independent entities/coordinates, taking its first
    # and last anchor creates exactly the misleading arrows seen in long map
    # evaluations.  One optional waypoint is enough for a concise route label;
    # longer reasoning stays visible prose without an arrow.
    if len(anchors) > 3:
        return False
    for point in anchors:
        row = point.get("row")
        column = point.get("column")
        if (
            not isinstance(row, int)
            or not isinstance(column, int)
            or not (1 <= row <= len(rows))
            or not (1 <= column <= len(rows[row - 1]))
            or rows[row - 1][column - 1] in {" ", "#", "@"}
        ):
            return False
    if anchors[0] != source or anchors[-1] != destination:
        return False
    first = next(item for item in matches if item["point"] == anchors[0])
    last = next(item for item in reversed(matches) if item["point"] == anchors[-1])
    between = value[first["end"]:last["start"]]
    return bool(re.search(
        r"(?:\u2192|->|\b(?:to|toward|towards|through|via|along|walk(?:ing)?|"
        r"move(?:s|d|ing)?|go(?:es|ing)?|lead(?:s|ing)?)\b|"
        r"到|向|往|朝向|通往|通向|经过|沿着|走到|走向|前往|抵达|"
        r"推向|推到|移动到|绕到|绕向)",
        between,
        flags=re.IGNORECASE,
    ))


def _coordinate_route_exists(rows, source, destination):
    """Use the same conservative walkability rule as the browser arrow renderer."""
    if not isinstance(rows, list) or not rows or not all(isinstance(row, str) for row in rows):
        return False
    height = len(rows)
    width = len(rows[0])
    for row in rows:
        if len(row) != width:
            return False

    def point(value):
        if not isinstance(value, dict):
            return None
        row = value.get("row")
        column = value.get("column")
        if (
            isinstance(row, bool)
            or not isinstance(row, int)
            or isinstance(column, bool)
            or not isinstance(column, int)
            or not (1 <= row <= height and 1 <= column <= width)
        ):
            return None
        return row - 1, column - 1

    start = point(source)
    end = point(destination)
    if start is None or end is None or start == end:
        return False

    start_key = f"{start[0]},{start[1]}"
    end_key = f"{end[0]},{end[1]}"

    def is_open(row, column):
        if not (0 <= row < height and 0 <= column < width):
            return False
        tile = rows[row][column]
        if tile in {" ", "#", "@"}:
            return False
        key = f"{row},{column}"
        return tile != "s" or key in {start_key, end_key}

    if not is_open(*start) or not is_open(*end):
        return False

    queue = deque([start])
    visited = {start}
    while queue:
        row, column = queue.popleft()
        if (row, column) == end:
            return True
        for row_delta, column_delta in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            next_point = (row + row_delta, column + column_delta)
            if next_point in visited or not is_open(*next_point):
                continue
            visited.add(next_point)
            queue.append(next_point)
    return False


def _coordinate_link_is_grounded(link, rows, entity_bindings=None):
    if rows is None:
        return True
    if not _route_relation_is_explicit(
        link.get("text"),
        link.get("from"),
        link.get("to"),
        rows,
        entity_bindings,
    ):
        return False
    if not _coordinate_route_exists(rows, link.get("from"), link.get("to")):
        return False
    return True


def _legacy_recover_coordinate_links(body, rows, existing=None, entity_bindings=None):
    """Recover links only from explicit, directional, map-grounded route sentences."""
    if not rows:
        return list(existing or [])
    links = list(existing or [])
    seen = {
        (item["text"], item["from"]["row"], item["from"]["column"],
         item["to"]["row"], item["to"]["column"])
        for item in links
    }
    segments = re.split(r"\s*(?:\n+|(?<=[.!?。！？]))\s*", str(body or ""))
    for segment in segments:
        text = segment.strip()
        if not text or not _route_text_has_direction(text):
            continue
        anchors = _route_anchors(text, rows, entity_bindings)
        if not anchors or len(anchors) < 2:
            continue
        candidate = {
            "text": text,
            "from": anchors[0],
            "to": anchors[-1],
        }
        if not _coordinate_link_is_grounded(candidate, rows, entity_bindings):
            continue
        key = (
            candidate["text"], candidate["from"]["row"], candidate["from"]["column"],
            candidate["to"]["row"], candidate["to"]["column"],
        )
        overlaps_existing = any(
            item["text"] in candidate["text"]
            or candidate["text"] in item["text"]
            for item in links
        )
        if key not in seen and not overlaps_existing:
            seen.add(key)
            links.append(candidate)
        if len(links) >= COORDINATE_LINK_LIMIT:
            break
    return links[:COORDINATE_LINK_LIMIT]


def _legacy_route_relation_clauses(text, rows=None, entity_bindings=None):
    """Split route prose at sentence and explicit relation boundaries."""
    sentences = re.split(
        r"(?<=[.!?\u3002\uff01\uff1f])\s*|[\r\n]+",
        str(text or ""),
    )
    clauses = []
    split_pattern = re.compile(
        r"\s*(?:[;；]+|\s+(?:and then|while|whereas)\s+|,(?=\s*(?:while|whereas|before|after|"
        r"or)\b)|，(?=\s*(?:同时|然后|随后|再|而|或者|之前|之后)))\s*",
        flags=re.IGNORECASE,
    )
    for sentence in sentences:
        for piece in split_pattern.split(sentence):
            piece = piece.strip()
            if not piece:
                continue
            comma_parts = re.split(
                r"(?<=\S),\s*(?=then\b)|(?<=\S)，\s*(?=然后|随后)",
                piece,
                flags=re.IGNORECASE,
            )
            if rows is not None and len(comma_parts) > 1:
                for left, right in zip(comma_parts, comma_parts[1:]):
                    if (
                        len(_route_anchor_matches(left, rows) or []) >= 2
                        and len(_route_anchor_matches(right, rows) or []) >= 2
                    ):
                        clauses.extend(
                            part.strip() for part in comma_parts if part.strip()
                        )
                        break
                else:
                    clauses.append(piece)
            else:
                clauses.append(piece)
    return clauses


def _route_relation_clauses(text, rows=None, entity_bindings=None):
    """Split route prose at sentence and explicit relation boundaries."""
    sentences = re.split(
        r"(?<=[.!?\u3002\uff01\uff1f])\s*|[\r\n]+",
        str(text or ""),
    )
    clauses = []
    split_pattern = re.compile(
        r"\s*(?:[;\u003b\uff1b]+|\s+(?:and then|while|whereas)\s+|"
        r",(?=\s*(?:while|whereas|before|after|or)\b)|"
        r"\uff0c(?=\s*(?:\u540c\u65f6|\u7136\u540e|\u968f\u540e|\u518d|\u800c|\u6216\u8005|\u4e4b\u524d|\u4e4b\u540e)))\s*",
        flags=re.IGNORECASE,
    )
    for sentence in sentences:
        for piece in split_pattern.split(sentence):
            piece = piece.strip()
            if not piece:
                continue
            comma_parts = re.split(
                r"(?<=\S),\s*(?=then\b)|(?<=\S)\uff0c\s*(?=\u7136\u540e|\u968f\u540e)",
                piece,
                flags=re.IGNORECASE,
            )
            if rows is not None and len(comma_parts) > 1:
                split_independent = any(
                    len(_route_anchor_matches(left, rows, entity_bindings) or []) >= 2
                    and len(_route_anchor_matches(right, rows, entity_bindings) or []) >= 2
                    for left, right in zip(comma_parts, comma_parts[1:])
                )
                if split_independent:
                    clauses.extend(part.strip() for part in comma_parts if part.strip())
                    continue
            clauses.append(piece)
    return clauses


def _route_relation_candidates(text, rows):
    """Return local, directional, map-grounded relation snippets."""
    candidates = []
    movement = re.compile(
        r"(?:\u2192|->|\b(?:to|toward|towards|through|via|along|walk(?:s|ed|ing)?|"
        r"move(?:s|d|ing)?|go(?:es|ing)?|lead(?:s|ing)?|reach(?:es|ed|ing)?)\b|"
        r"(?:\u5230|\u5411|\u671d\u5411|\u901a\u5f80|\u5f80|\u7ecf\u8fc7|\u6cbf\u7740|\u8d70|\u8d70\u5230|"
        r"\u79fb\u52a8\u5230|\u63a8\u5411|\u63a8\u5230|\u7ed5\u5411|\u7a7f\u8fc7))",
        flags=re.IGNORECASE,
    )
    for clause in _route_relation_clauses(text, rows):
        matches = _route_anchor_matches(clause, rows)
        if not matches or len(matches) < 2:
            continue
        anchors = []
        for match in matches:
            if anchors and anchors[-1]["point"] == match["point"]:
                anchors[-1]["end"] = max(anchors[-1]["end"], match["end"])
                continue
            anchors.append(dict(match))
        for source, destination in zip(anchors, anchors[1:]):
            between = clause[source["end"]:destination["start"]]
            if not movement.search(between):
                continue
            if (
                re.search(r"\b(?:go|walk|move)\w*\b.*\bfrom\b", between, re.IGNORECASE)
                and not re.search(r"\b(?:to|toward|towards|through|via)\b", between, re.IGNORECASE)
            ):
                continue
            candidate_text = (
                clause.strip()
                if len(anchors) == 2
                else clause[source["start"]:destination["end"]].strip(" ,，;；")
            )
            candidate = {
                "text": candidate_text,
                "from": dict(source["point"]),
                "to": dict(destination["point"]),
            }
            if not _route_text_is_concise(candidate_text):
                continue
            if not _coordinate_link_is_grounded(candidate, rows):
                continue
            candidates.append(candidate)
    return candidates


def _route_relation_is_explicit(text, source, destination, rows, entity_bindings=None):
    """Accept one local movement relation, not a location statement or route chain."""
    if not _route_text_is_concise(text):
        return False
    matches = _route_relation_anchor_matches(str(text or ""), rows, entity_bindings)
    if not matches:
        return False
    anchors = []
    for match in matches:
        point = match["point"]
        if anchors and anchors[-1] == point:
            continue
        anchors.append(point)
    if len(anchors) != 2 or anchors[0] != source or anchors[-1] != destination:
        return False
    return any(
        candidate["from"] == source
        and candidate["to"] == destination
        and candidate["text"] in str(text or "")
        for candidate in _route_relation_candidates_without_grounding(
            text,
            rows,
            entity_bindings,
        )
    )


def _route_relation_anchor_matches(text, rows, entity_bindings=None):
    """Ignore an entity's ``from`` coordinate qualifier, but keep real waypoints."""
    matches = _route_anchor_matches(text, rows, entity_bindings)
    if matches is None:
        return None

    filtered = []
    value = str(text or "")
    entity_before_coordinate = re.compile(
        r"(?<![A-Za-z0-9])(?:P|B\d+|T\d+)(?![A-Za-z0-9])"
        r"[^()\r\n]{0,32}(?:\bfrom\b|\u4ece)\s*$",
        flags=re.IGNORECASE,
    )
    for match in matches:
        is_coordinate = value[match["start"]:match["end"]].lstrip().startswith(("(", "（"))
        prefix = value[:match["start"]]
        if is_coordinate and entity_before_coordinate.search(prefix):
            continue
        filtered.append(match)
    return filtered


def _route_relation_candidates_without_grounding(text, rows, entity_bindings=None):
    """Build local relation snippets before the recursive grounding check."""
    candidates = []
    movement = re.compile(
        r"(?:\u2192|->|\b(?:to|toward|towards|through|via|along|walk(?:s|ed|ing)?|"
        r"move(?:s|d|ing)?|go(?:es|ing)?|lead(?:s|ing)?|reach(?:es|ed|ing)?)\b|"
        r"(?:\u5230|\u5411|\u671d\u5411|\u901a\u5f80|\u5f80|\u7ecf\u8fc7|\u6cbf\u7740|\u8d70|\u8d70\u5230|"
        r"\u79fb\u52a8\u5230|\u63a8\u5411|\u63a8\u5230|\u7ed5\u5411|\u7a7f\u8fc7))",
        flags=re.IGNORECASE,
    )
    for clause in _route_relation_clauses(text, rows, entity_bindings):
        matches = _route_relation_anchor_matches(clause, rows, entity_bindings)
        if not matches or len(matches) < 2:
            continue
        anchors = []
        for match in matches:
            if anchors and anchors[-1]["point"] == match["point"]:
                anchors[-1]["end"] = max(anchors[-1]["end"], match["end"])
                continue
            anchors.append(dict(match))
        for source, destination in zip(anchors, anchors[1:]):
            between = clause[source["end"]:destination["start"]]
            if re.search(
                r"\b(?:is|are|lies?|sits?|located|positioned|placed)\b[^.!?]{0,40}"
                r"\b(?:near|close\s+to|beside|adjacent|next\s+to|to\s+the\s+left|to\s+the\s+right|"
                r"above|below)\b|"
                r"(?:\u5728|\u4f4d\u4e8e|\u9760\u8fd1|\u65c1\u8fb9|\u76f8\u90bb|\u5de6\u4fa7|"
                r"\u53f3\u4fa7|\u4e0a\u65b9|\u4e0b\u65b9)",
                between,
                flags=re.IGNORECASE,
            ):
                continue
            if not movement.search(between):
                continue
            if (
                re.search(r"\b(?:go|walk|move)\w*\b.*\bfrom\b", between, re.IGNORECASE)
                and not re.search(r"\b(?:to|toward|towards|through|via)\b", between, re.IGNORECASE)
            ):
                continue
            candidates.append({
                "text": (
                    clause.strip()
                    if len(anchors) == 2
                    else clause[source["start"]:destination["end"]].strip(" ,，;；")
                ),
                "from": dict(source["point"]),
                "to": dict(destination["point"]),
            })
    return candidates


def _recover_coordinate_links(body, rows, existing=None, entity_bindings=None):
    """Recover up to twelve local route links from grounded visible prose."""
    if not rows:
        return list(existing or [])
    links = list(existing or [])
    seen = {
        (
            item["text"], item["from"]["row"], item["from"]["column"],
            item["to"]["row"], item["to"]["column"],
        )
        for item in links
    }
    for candidate in _route_relation_candidates_without_grounding(
        body,
        rows,
        entity_bindings,
    ):
        if not _coordinate_link_is_grounded(candidate, rows, entity_bindings):
            continue
        key = (
            candidate["text"], candidate["from"]["row"], candidate["from"]["column"],
            candidate["to"]["row"], candidate["to"]["column"],
        )
        if key in seen:
            continue
        overlaps_existing = any(
            item["text"] in candidate["text"]
            or candidate["text"] in item["text"]
            for item in links
        )
        if overlaps_existing:
            continue
        seen.add(key)
        links.append(candidate)
        if len(links) >= COORDINATE_LINK_LIMIT:
            break
    return links[:COORDINATE_LINK_LIMIT]


def _filter_coordinate_links(value, body, rows=None, entity_bindings=None):
    """Keep only LLM-authored annotations that survived visible-body cleanup."""
    visible_body = str(body or "")
    filtered = []
    occupied_ranges = []
    for link in _normalize_coordinate_links(value, rows=rows):
        if not _coordinate_link_text_is_complete_clause(link.get("text")):
            continue
        if not _coordinate_link_is_grounded(link, rows, entity_bindings):
            continue
        start = visible_body.find(link["text"])
        if start < 0:
            continue
        end = start + len(link["text"])
        if any(
            start < occupied_end and end > occupied_start
            for occupied_start, occupied_end in occupied_ranges
        ):
            continue
        occupied_ranges.append((start, end))
        filtered.append(link)
    return filtered


def _coordinate_link_text_is_complete_clause(value):
    """Avoid underlining a sentence fragment that leaves punctuation stranded."""
    text = str(value or "").strip()
    if not text:
        return False
    if text[0] in ",，、;；:：)）]}】":
        return False
    if text[-1] in ",，、;；:：(（[{【":
        return False
    return not re.search(
        r"(?:\b(?:and|or|while|then|because|so|but)\b|(?:以及|并且|同时|然后|因此|但是|而且))$",
        text,
        flags=re.IGNORECASE,
    )


def _validate_disagreement(value, language):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("guidance.disagreement must be an object or null.")

    required = {
        "status",
        "subject",
        "userPosition",
        "aiPosition",
        "coreDisagreement",
        "nextQuestion",
        "resolution",
    }
    if set(value) != required:
        raise ValueError("guidance.disagreement contains unexpected or missing fields.")
    status = value.get("status")
    subject = value.get("subject")
    resolution = value.get("resolution")
    if status not in DISAGREEMENT_STATUSES:
        raise ValueError("guidance.disagreement.status is invalid.")
    if subject not in DISAGREEMENT_SUBJECTS:
        raise ValueError("guidance.disagreement.subject is invalid.")
    if status == "active" and resolution is not None:
        raise ValueError("An active disagreement must have a null resolution.")
    if status == "resolved" and resolution not in DISAGREEMENT_RESOLUTIONS:
        raise ValueError("A resolved disagreement requires a valid resolution.")

    normalized = {"status": status, "subject": subject}
    for field_name in (
        "userPosition",
        "aiPosition",
        "coreDisagreement",
        "nextQuestion",
    ):
        normalized[field_name] = _normalize_single_level_language(
            _clean_text(value.get(field_name), f"guidance.disagreement.{field_name}")
        )[:1200]
    normalized["resolution"] = resolution
    return normalized


def _disagreement_from_warning(warning_text, language, stage_context, user_position=None):
    warning = str(warning_text or "").strip()
    context = stage_context or {}
    change_summary = context.get("changeSummary") or {}
    components = ", ".join(str(item) for item in change_summary.get("components") or [])
    if language == "zh-CN":
        user_position = user_position or (
            f"用户刚刚完成了{components or '这处地图'}的手动修改，并希望保留这次调整。"
        )
        ai_position = f"我担心这次修改在具体游玩时刻带来问题：{warning}"
        core = "是否保留当前手动修改，以及如何在不牺牲可解性和设计目标的前提下实现用户想要的效果。"
        next_question = "这次修改最想保留的游玩效果是什么？如果保留当前布局，如何处理我指出的具体风险？"
    else:
        user_position = user_position or (
            f"The designer just saved a manual change to {components or 'this part of the map'} and wants to keep it."
        )
        ai_position = f"I am concerned that the change creates a concrete play problem: {warning}"
        core = "Whether to keep the current manual edit and how to preserve its intended effect without sacrificing solvability or the stated design goal."
        next_question = "Which play effect is most important to preserve, and how should we address the concrete risk I observed?"
    return {
        "status": "active",
        "subject": "human_edit" if context.get("source") == "human_edit" else "user_request",
        "userPosition": user_position[:1200],
        "aiPosition": ai_position[:1200],
        "coreDisagreement": core[:1200],
        "nextQuestion": next_question[:1200],
        "resolution": None,
    }


def _normalize_intent_hypothesis(hypothesis, language):
    text = hypothesis.strip()

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        direct_prefixes = (
            "我猜你可能",
            "我觉得你可能",
            "我感觉你可能",
            "我感觉你似乎",
            "在我看来，你可能",
            "我倾向于认为你可能",
            "听起来你",
            "我暂时把你的方向理解为",
            "我读到的倾向是",
            "我在想，你可能",
        )

        if text.startswith(direct_prefixes):
            return text

        text = re.sub(r"^(?:这位)?(?:设计者|玩家)", "你", text)
        match = re.match(r"^你(?:想要|希望|想)(.*)$", text)

        if match:
            return f"听起来你更想要{match.group(1)}"

        if text.startswith("你可能"):
            return f"我觉得{text}"

        if text.startswith("你似乎"):
            return f"我感觉{text}"

        return f"我暂时把你的方向理解为：{text}"

    if re.match(
        r"^(?:I think you (?:may|might)|My guess is (?:that )?you (?:may|might)|"
        r"I (?:suspect|wonder whether) you (?:may|might)|"
        r"I get the sense that you (?:may|might)|"
        r"It seems to me that you (?:may|might)|It sounds to me like you|"
        r"For now, I understand your direction as|I read your preference as)\b",
        text,
        re.IGNORECASE,
    ):
        return text

    third_person_patterns = (
        (
            r"^(?:the )?(?:designer|player) wants? to\b(.*)$",
            "It sounds to me like you want to",
        ),
        (
            r"^(?:the )?(?:designer|player) wants?\b(.*)$",
            "I read your preference as wanting",
        ),
        (
            r"^(?:the )?(?:designer|player) (?:seems|appears) to want\b(.*)$",
            "I get the sense that you may want",
        ),
        (
            r"^(?:the )?(?:designer|player) may want\b(.*)$",
            "For now, I understand your direction as wanting",
        ),
        (
            r"^(?:the )?(?:designer|player) (?:is|seems to be) aiming to\b(.*)$",
            "I think you may be aiming to",
        ),
        (
            r"^(?:the )?(?:designer|player) (?:prefers|seems to prefer)\b(.*)$",
            "I think you may prefer",
        ),
    )

    for pattern, prefix in third_person_patterns:
        match = re.match(pattern, text, re.IGNORECASE)

        if match:
            return f"{prefix}{match.group(1)}"

    text = re.sub(r"^(?:the )?(?:designer|player)\b", "you", text, flags=re.IGNORECASE)
    patterns = (
        (r"^you (?:want|want to|hope|hope to)\b(.*)$", "It sounds to me like you want"),
        (r"^you (?:may|might) want\b(.*)$", "For now, I understand your direction as wanting"),
        (r"^you (?:seem|appear) to want\b(.*)$", "I get the sense that you may want"),
        (r"^you (?:are|seem to be) aiming to\b(.*)$", "I think you may be aiming to"),
        (r"^you (?:prefer|seem to prefer)\b(.*)$", "I think you may prefer"),
    )

    for pattern, prefix in patterns:
        match = re.match(pattern, text, re.IGNORECASE)

        if match:
            return f"{prefix}{match.group(1)}"

    if re.match(r"^you\b", text, re.IGNORECASE):
        return f"I think you may be signaling this direction: {text}"

    return f"For now, I understand your direction as: {text}"


def _normalize_opening_question(question):
    chinese_anchor = re.search(r"还是|或者|或是|抑或", question)
    english_anchor = re.search(r"\b(?:or|versus|vs\.?)\b", question, re.IGNORECASE)
    chinese_yes_no = re.search(r"吗[？?]\s*$", question)
    english_yes_no = re.match(
        r"^(?:do|does|did|is|are|was|were|would|could|can|will|have|has)\b",
        question,
        re.IGNORECASE,
    )

    if (
        chinese_anchor is None
        and english_anchor is None
        and chinese_yes_no is None
        and english_yes_no is None
    ):
        return question

    if chinese_anchor is not None or chinese_yes_no is not None:
        choice_intro = re.search(
            r"[，,](?:你)?(?:是|是否|主要是|更倾向于)",
            question,
        )

        if choice_intro is not None:
            subject = question[:choice_intro.start()].rstrip("，,。！？? ")

            if subject:
                time_suffix = "" if subject.endswith("时") else "时"
                return f"{subject}{time_suffix}，你最先考虑的是什么？"

    if english_anchor is not None:
        for separator in ("—", "--", " - "):
            if separator not in question:
                continue

            subject = question.split(separator, 1)[0].strip().rstrip(".?! ")

            if re.match(r"^(?:what|why|how|where|when|which)\b", subject, re.IGNORECASE):
                return f"{subject}?"

    raise ValueError("A Stage opening question cannot anchor the designer with choices.")


def _compact_human_edit_opening_inventory(message, language):
    """Prefer design effects over duplicate status and coordinate inventories."""
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？])\s*", str(message or ""))
        if item.strip()
    ]
    confirmation = re.compile(
        r"(?:已保存|已通过|确认可解|确定性检查|saved|solvable|verified)",
        flags=re.IGNORECASE,
    )
    coordinate = re.compile(r"(?:\(\s*\d+\s*[,，]\s*\d+\s*\)|第\s*\d+\s*行\s*第\s*\d+\s*列)")
    entity = re.compile(r"\b(?:P|B\d+|T\d+)\b", flags=re.IGNORECASE)
    non_inventory = [
        sentence for sentence in sentences
        if not (coordinate.search(sentence) or len(entity.findall(sentence)) >= 2)
    ]
    # Never turn a valid opening into an empty or mechanical fallback merely to
    # satisfy a presentation preference.  Inventory prose is removed only when
    # there is already enough grounded design material to retain.
    can_drop_inventory = len(non_inventory) >= 3
    kept = []
    seen_confirmation = False
    for sentence in sentences:
        if confirmation.search(sentence):
            if seen_confirmation:
                continue
            seen_confirmation = True
        if can_drop_inventory and (
            coordinate.search(sentence) or len(entity.findall(sentence)) >= 2
        ):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def _format_stage_opening_paragraphs(message):
    paragraphs = [part.strip() for part in message.split("\n\n") if part.strip()]

    if len(paragraphs) > 3:
        paragraphs = [paragraphs[0], paragraphs[1], " ".join(paragraphs[2:])]

    if len(paragraphs) == 1:
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?。！？])\s*", paragraphs[0])
            if item.strip()
        ]
        if len(sentences) >= 4:
            midpoint = 2 if len(sentences) <= 5 else len(sentences) // 2
            paragraphs = [
                " ".join(sentences[:midpoint]),
                " ".join(sentences[midpoint:]),
            ]

    return "\n\n".join(paragraphs)


def _compose_assistant_message(
    message,
    guidance,
    language="en",
    assessment_only=False,
    stage_context=None,
):
    stage_context = stage_context or {}
    message = _sanitize_visible_model_text(
        _deduplicate_assistant_body(message),
        language,
    )
    if assessment_only and _is_stage_one(stage_context):
        # This is the final visible-text gate.  Keeping it here makes the
        # fixed Stage 1 guidance apply to every structured/plain/fallback path
        # and gives short model replies the same balanced opening shape as
        # historical Stage 1 records.
        snapshot_rows = (stage_context.get("stageSnapshot") or {}).get("rows")
        message = _repair_stage_one_opening_display(
            message,
            snapshot_rows,
            language,
        )
    change_summary = stage_context.get("changeSummary") or {}
    components = change_summary.get("components") or []

    if assessment_only and stage_context.get("source") == "human_edit":
        labels = _localized_change_labels(components, language)

        if language == "zh-CN":
            subject = "、".join(labels) if labels else "地图布局"
            acknowledgement = (
                f"我注意到你对{subject}进行了修改。"
                "这个已保存的 Stage 已通过确定性检查并确认可解。"
            )
        else:
            subject = _join_english(labels) if labels else "the map layout"
            acknowledgement = (
                f"I noticed that you changed {subject}. "
                "This saved Stage passed deterministic validation and is solvable."
            )

        message = f"{acknowledgement}\n\n{message}"

    ui_cues = guidance.get("uiCues") or []
    additions = [
        _sanitize_visible_model_text(cue["text"], language)
        for cue in ui_cues
        if cue.get("text")
    ]
    follow_up = guidance.get("followUpQuestion")
    follow_up = _sanitize_visible_model_text(follow_up, language)
    proposal_offer = guidance.get("proposalOffer") or {}

    for addition in [*additions, follow_up]:
        message = _remove_guidance_from_body(message, addition)

    for addition in [
        _sanitize_visible_model_text(proposal_offer.get("summary"), language),
        _sanitize_visible_model_text(proposal_offer.get("rationale"), language),
    ]:
        message = _remove_exact_guidance_from_body(message, addition)

    if follow_up:
        additions.append(follow_up)

    return _sanitize_visible_model_text(
        "\n\n".join(part for part in [message, *additions] if part),
        language,
    )


def _localized_change_labels(components, language):
    labels = {
        "zh-CN": {
            "outerShell": "外壳",
            "water": "水域",
            "internalWalls": "内部墙体",
            "boxes": "箱子位置",
            "targets": "目标点",
            "player": "玩家位置",
            "floorArea": "可用地面",
        },
        "en": {
            "outerShell": "the outer shell",
            "water": "the water area",
            "internalWalls": "the internal walls",
            "boxes": "the box positions",
            "targets": "the target positions",
            "player": "the player position",
            "floorArea": "the usable floor area",
        },
    }
    selected = labels["zh-CN" if language == "zh-CN" else "en"]
    return [selected[component] for component in components if component in selected]


def _join_english(values):
    if len(values) < 2:
        return values[0] if values else ""
    if len(values) == 2:
        return " and ".join(values)
    return ", ".join(values[:-1]) + ", and " + values[-1]


def classify_exception(exception, request_id, attempts_used):
    if isinstance(exception, LLMServiceError):
        return exception

    if isinstance(exception, EmptyModelResponse):
        return LLMServiceError(
            "MODEL_EMPTY_RESPONSE",
            "The LLM returned an empty response.",
            request_id,
            True,
            attempts_used,
            502,
        )

    if isinstance(exception, LowQualityModelResponse):
        return LLMServiceError(
            "MODEL_LOW_QUALITY_RESPONSE",
            "The LLM returned only a low-information question.",
            request_id,
            True,
            attempts_used,
            502,
        )

    if isinstance(exception, APITimeoutError):
        return LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "Kimi did not respond before the timeout.",
            request_id,
            True,
            attempts_used,
            504,
        )

    if isinstance(exception, RateLimitError):
        return LLMServiceError(
            "UPSTREAM_RATE_LIMIT",
            "Kimi rate limited the request.",
            request_id,
            True,
            attempts_used,
            503,
        )

    if isinstance(exception, APIConnectionError):
        return LLMServiceError(
            "UPSTREAM_CONNECTION_ERROR",
            "The prototype could not connect to Kimi.",
            request_id,
            True,
            attempts_used,
            502,
        )

    if isinstance(exception, APIStatusError):
        upstream_status = int(getattr(exception, "status_code", 0) or 0)
        retryable = upstream_status == 429 or upstream_status >= 500
        provider_details = _provider_error_details(exception)
        provider_message = provider_details.get("providerMessage", "")
        safe_message = (
            f"Kimi returned HTTP {upstream_status or 'error'}: {provider_message}"
            if provider_message
            else f"Kimi returned HTTP {upstream_status or 'error'}."
        )
        return LLMServiceError(
            "UPSTREAM_SERVER_ERROR" if retryable else "UPSTREAM_REQUEST_REJECTED",
            safe_message,
            request_id,
            retryable,
            attempts_used,
            503 if upstream_status == 429 else 502,
            provider_status=provider_details.get("providerStatus", upstream_status or None),
            provider_error_type=provider_details.get("providerErrorType"),
            provider_error_code=provider_details.get("providerErrorCode"),
            provider_param=provider_details.get("providerParam"),
            provider_message=provider_message,
        )

    if isinstance(exception, (json.JSONDecodeError, ValueError, TypeError, KeyError)):
        return LLMServiceError(
            "MODEL_RESPONSE_INVALID",
            "The LLM returned an invalid response.",
            request_id,
            True,
            attempts_used,
            502,
        )

    return LLMServiceError(
        "INTERNAL_ERROR",
        "An unexpected LLM error occurred.",
        request_id,
        False,
        attempts_used,
        500,
    )


def _plain_fallback_allowed(error):
    return error.code in {
        "MODEL_EMPTY_RESPONSE",
        "MODEL_RESPONSE_INVALID",
        "MODEL_LOW_QUALITY_RESPONSE",
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_CONNECTION_ERROR",
    }


def _response_diagnostics(response, choice):
    usage = getattr(response, "usage", None)
    content = str(getattr(getattr(choice, "message", None), "content", "") or "")
    return {
        "finishReason": str(getattr(choice, "finish_reason", "") or ""),
        "responseChars": len(content),
        "promptTokens": getattr(usage, "prompt_tokens", None),
        "completionTokens": getattr(usage, "completion_tokens", None),
        "totalTokens": getattr(usage, "total_tokens", None),
    }


def _empty_response_diagnostics():
    return {
        "finishReason": None,
        "responseChars": None,
        "promptTokens": None,
        "completionTokens": None,
        "totalTokens": None,
    }


def _clean_text(value, field_name, maximum=CHAT_RESPONSE_MAX_LENGTH):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")

    cleaned = value.strip()

    if len(cleaned) > maximum:
        raise ValueError(f"{field_name} is too long.")

    return cleaned


def _clean_list(value, field_name):
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError(f"{field_name} must be a non-empty list.")

    return [_clean_text(item, field_name) for item in value]


def _clean_optional_text(value, field_name):
    if value is None:
        return None

    return _clean_text(value, field_name)


def _classify_revision_request(conversation, stage_context=None):
    latest_user_message = next(
        (
            str(message.get("content") or "").strip().casefold()
            for message in reversed(conversation)
            if message.get("role") == "user"
        ),
        "",
    )

    if not latest_user_message:
        return "not_request", None
    if _user_explicitly_off_topic(latest_user_message):
        return "not_request", None

    relaxation_offer = (
        ((stage_context or {}).get("recentGuidance") or {}).get("relaxationOffer") or {}
    )
    if relaxation_offer.get("status") == "awaiting_confirmation":
        if _latest_user_explicitly_agrees(latest_user_message):
            relaxed_brief = str(relaxation_offer.get("relaxedBrief") or "").strip()
            if relaxed_brief:
                return "relaxation_confirmed", relaxed_brief
        if _latest_user_explicitly_rejects(latest_user_message):
            return "not_request", None

    english_markers = (
        "map proposal",
        "concrete map",
        "draft this",
        "draft that",
        "draft the revision",
        "generate a map",
        "create a map",
        "revise the map",
        "rework the map",
    )
    chinese_markers = (
        "地图提案",
        "具体生成",
        "生成地图",
        "生成一份",
        "修改地图",
        "改一下地图",
        "把地图改",
        "你帮我改",
        "帮我改一下",
        "帮我改改",
        "你来改",
        "请你改",
        "交给你改",
        "你直接改",
        "帮我做",
        "帮我做个方案",
        "帮我做一个方案",
        "就这么做",
        "照这个做",
        "按这个做",
    )
    actual_direct_edit = bool(re.search(
        r"(?:\u8bf7|\u5e2e\u6211|\u4f60\u6765|\u76f4\u63a5)(?:\u628a|\u5c06).{0,100}"
        r"(?:\u6539|\u8c03\u6574|\u4fee\u6539|\u79fb\u52a8|\u589e\u52a0|\u51cf\u5c11|\u5220\u9664|\u4fdd\u7559)",
        latest_user_message,
    ))
    marker_match = actual_direct_edit or any(
        marker in latest_user_message
        for marker in (*english_markers, *chinese_markers)
    )
    chinese_polite_request = re.search(
        r"(?:可以|能|能不能|可不可以|请|麻烦)?(?:你)?(?:帮我|替我|给我)"
        r"(?:修改|改|调整)(?:一下|一版|这个|地图|关卡|布局)?",
        latest_user_message,
    )
    chinese_short_command = re.fullmatch(
        r"\s*(?:开始)?(?:修改|改|调整)(?:一下|吧)?\s*[。！!？?]*\s*",
        latest_user_message,
    )
    chinese_authorization = re.search(
        r"(?:按|照着|依照)(?:这个|这条|刚才的|上面的)?(?:方向|思路|方案)?"
        r"(?:帮我|你来|直接)?改(?:一下|改|吧)?",
        latest_user_message,
    )
    english_authorization = re.search(
        r"\b(?:(?:can|could|would|will) you(?: please)?|please|go ahead and|"
        r"you can|i want you to)\s+(?:change|modify|revise|rework|edit)"
        r"(?:\s+(?:it|this|that|the (?:map|level|layout|revision)))?\b",
        latest_user_message,
    )
    english_design_question = re.search(
        r"\b(?:how|what)\s+(?:would|could|should|can)\s+you\s+"
        r"(?:change|modify|revise|rework|edit)\b",
        latest_user_message,
    )
    chinese_design_question = re.search(
        r"(?:怎么|如何|怎样)(?:来)?改|改(?:成)?什么样",
        latest_user_message,
    )
    english_imperative = re.search(
        r"\b(?:please|go ahead and|then)\s+(?:change|modify|revise|rework|edit|do it)\b",
        latest_user_message,
    )
    chinese_imperative = re.search(
        r"(?:直接|就|按这个|照这个|帮我|请你)(?:方向|思路|方案)?改(?:一下|改|吧)?",
        latest_user_message,
    )
    if english_design_question is not None and english_imperative is None:
        english_authorization = None
        marker_match = False
    if chinese_design_question is not None and chinese_imperative is None:
        chinese_authorization = None
        marker_match = False
        chinese_polite_request = None
        chinese_short_command = None

    inherited_brief = _authorized_revision_brief(
        conversation,
        stage_context,
        latest_user_message,
    )
    requested = (
        marker_match
        or chinese_authorization is not None
        or chinese_polite_request is not None
        or chinese_short_command is not None
        or english_authorization is not None
        or (
            _latest_user_confirms_revision(latest_user_message)
            and inherited_brief is not None
        )
    )

    if not requested:
        return "not_request", None

    if relaxation_offer.get("status") == "suggestion_ready":
        relaxed_brief = str(relaxation_offer.get("relaxedBrief") or "").strip()
        if relaxed_brief:
            return "authorized_relaxed", relaxed_brief

    brief = inherited_brief
    return ("authorized", brief) if brief else ("needs_direction", None)


def _requests_complete_map(conversation, stage_context=None):
    state, _ = _classify_revision_request(conversation, stage_context)
    return state in {"authorized", "authorized_relaxed"}


def classify_revision_request(conversation, stage_context=None):
    """Expose the deterministic request state to the API orchestration layer."""
    return _classify_revision_request(conversation, stage_context)


def _authorized_revision_brief(conversation, stage_context, latest_user_message):
    if _contains_concrete_revision_direction(latest_user_message):
        return latest_user_message[:1200]

    recent_offer = ((stage_context or {}).get("recentGuidance") or {}).get(
        "proposalOffer"
    ) or {}
    offer_text = " ".join(
        str(recent_offer.get(field) or "").strip()
        for field in ("summary", "rationale")
    ).strip()

    if _contains_concrete_revision_direction(offer_text):
        return offer_text[:1200]

    skipped_latest_user = False
    for message in reversed(conversation):
        role = message.get("role")
        content = str(message.get("content") or "").strip()

        if role == "user" and not skipped_latest_user:
            skipped_latest_user = True
            continue
        if role == "assistant" and _contains_concrete_revision_direction(
            content,
            require_proposal_framing=True,
        ):
            return content[:1200]
        if role == "user" and _contains_concrete_revision_direction(
            content,
            require_proposal_framing=True,
        ):
            return content[:1200]

    return None


def _contains_concrete_revision_direction(value, require_proposal_framing=False):
    text = str(value or "").strip().casefold()
    if not text:
        return False

    if re.search(r"[\u3400-\u9fff]", text):
        anchors = (
            "水", "箱", "目标", "墙", "通道", "路线", "区域", "入口",
            "落点", "推动", "节奏", "左上", "右上", "左下", "右下",
        )
        actions = (
            "移", "挪", "调整", "重排", "保留", "减少", "增加", "改变",
            "集中", "连接", "连成", "缩短", "拉开", "让", "改动", "修改",
            "改成", "改为", "变成", "放", "设", "铺", "不动",
        )
    else:
        anchors = (
            "water", "box", "crate", "target", "goal", "wall", "corridor",
            "route", "area", "entrance", "landing", "push", "rhythm",
            "upper", "lower", "left", "right",
        )
        actions = (
            "move", "shift", "adjust", "rearrange", "keep", "preserve",
            "reduce", "increase", "change", "connect", "shorten", "separate",
            "make", "revise",
        )

    has_direction = any(anchor in text for anchor in anchors) and any(
        action in text for action in actions
    )
    if not has_direction or not require_proposal_framing:
        return has_direction

    if re.search(r"[\u3400-\u9fff]", text):
        framing = (
            "建议", "可以把", "不如", "试试", "我倾向", "我想改", "我的方案",
            "修改方案", "具体改", "改成", "把它", "把这个", "把那",
        )
        framed_action = re.search(
            r"把.{0,48}(?:移|挪|调整|重排|保留|减少|增加|改变|集中|连接|连成|缩短|拉开|改|变成|放|设|铺)",
            text,
        )
        explicit_operation = re.search(
            r"第\s*\d+\s*行.{0,32}(?:第\s*\d+\s*(?:到|至|-)?\s*第?\s*\d*\s*列|第\s*\d+\s*列)"
            r".{0,48}(?:水|箱|目标|墙|地面)",
            text,
        )
    else:
        framing = (
            "i suggest", "we could", "you could", "let's", "let us", "try ",
            "i would", "my proposal", "revision direction", "change it to",
        )
        framed_action = re.match(
            r"(?:move|shift|adjust|rearrange|keep|preserve|reduce|increase|change|connect|shorten|separate|revise)\b",
            text,
        )
        explicit_operation = re.search(
            r"\brow\s*\d+.{0,40}\b(?:col(?:umn)?\s*\d+|columns?\s*\d+)"
            r".{0,48}\b(?:water|box|crate|target|wall|floor)\b",
            text,
        )
    return (
        any(marker in text for marker in framing)
        or framed_action is not None
        or explicit_operation is not None
    )


def _create_async_client(api_key, base_url, timeout_seconds):
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        max_retries=0,
    )


def _log_llm_event(event, **fields):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": "INFO",
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)
