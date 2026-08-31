import asyncio
from difflib import SequenceMatcher
import hashlib
import json
import os
import re
import time
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
    ProposalSearchExhausted,
    parse_revision_plan,
    search_revision_plan,
    validate_revision_plan_against_map,
)
from level_validation import build_map_facts, validate_and_solve


DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROPOSAL_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
PRIMARY_ATTEMPT_TIMEOUT_SECONDS = 40.0
CHAT_TIMEOUT_SECONDS = 60.0
CHAT_MAX_ATTEMPTS = 2
PROPOSAL_GENERATION_ATTEMPTS = 2
PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS = 18.0
PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS = 8.0
PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS = 26.0
PROPOSAL_SEARCH_DEADLINE_SECONDS = 55.0
# The authorized revision now has two bounded LLM phases: a semantic plan and
# concrete operation candidates.  Both use the existing proposal model config.
REVISION_CONTRACT_SCHEMA_VERSION = 1
REVISION_MIN_CHANGED_CELLS = 1
REVISION_MAX_CHANGED_CELLS = 12
# Compatibility for older diagnostics and integrations that imported these names.
PROPOSAL_ATTEMPT_TIMEOUT_SECONDS = PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS
CHAT_MAX_TOKENS = 1400
PLAIN_CHAT_TIMEOUT_SECONDS = 25.0
PLAIN_PRIMARY_TIMEOUT_SECONDS = 15.0
PLAIN_CHAT_MAX_TOKENS = 900
PROPOSAL_MAX_TOKENS = 2400
PROPOSAL_PLAN_MAX_TOKENS = 1400
PROPOSAL_OPERATION_MAX_TOKENS = 1400
PROPOSAL_CANDIDATE_LIMIT = 3
PROPOSAL_OPERATION_LIMIT = 24
TRANSLATION_MAX_TOKENS = 3200
CHAT_RESPONSE_MAX_LENGTH = 4000
PROMPT_VERSION = "cocreation-v33-two-agent-revision"

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
UI_CUE_TYPES = {"manual_edit", "warning", "tradeoff"}
GUIDANCE_REQUEST_MODES = {"revision_advice", "discussion", "none"}
DISAGREEMENT_STATUSES = {"active", "resolved"}
DISAGREEMENT_SUBJECTS = {"ai_revision", "human_edit", "user_request"}
DISAGREEMENT_RESOLUTIONS = {"user", "ai", "compromise", "retain_current"}


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
    ):
        super().__init__(message)
        self.code = str(code)
        self.safe_message = str(message)
        self.request_id = str(request_id)
        self.retryable = bool(retryable)
        self.attempts_used = int(attempts_used)
        self.status_code = int(status_code)


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
    serialized_map = "\n".join(rows)
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    solver_metrics = _llm_solver_evidence(solver_metrics or {})
    play_summary = play_summary or {}
    stage_context = stage_context or {}
    map_facts = _map_facts_for_prompt(rows, stage_context)
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
        "Usually write two to four compact paragraphs and at most one central question. A "
        "simple factual answer may be shorter, but a design response should normally include "
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
        "a conceptual purple REVISION card. Ordinary chat must never generate map rows. "
        "Only the structured execute_revision card action authorizes the two-agent revision "
        "pipeline and proposedRows. You may "
        "proactively offer one concrete revision "
        "direction and rationale, but that offer must not contain proposedRows. Never "
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
        f"Write all new natural-language fields in {response_language}. {task} "
        f"{revision_contract}\n\n"
        f"Draft provenance and attribution rules: {provenance_guidance}\n\n"
        "Return JSON only with exactly these keys:\n"
        '{"assistantMessage":"...","guidance":'
        '{"move":"observe_stage","intentHypothesis":null,'
        '"intentConfidence":null,"followUpQuestion":null,'
        '"proposalOffer":null,"disagreement":null,"uiCues":[]},"assessment":null,"proposedRows":null,'
        '"modificationSummary":""}.\n'
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
        'an object with summary, rationale, and optional executionBrief. executionBrief is a '
        'machine-only object with schemaVersion 1, effect, anchors, focus, requiredTransitions, '
        'allowedOperators, preserve, and playObjective. If the exchange names an exact coordinate '
        'or from/to tile change, include it in requiredTransitions exactly; never substitute a nearby '
        'cell. The server will reject a brief whose coordinate or from tile conflicts with the saved map. '
        "uiCues must be an array of at most two unique objects with exactly type and "
        "text; type must be manual_edit or warning. The legacy tradeoff type is accepted "
        "by the application for historical data but must not be generated. "
        "assistantMessage must not repeat followUpQuestion; when non-null, the application "
        "appends it in a separate discussion card. The card must not merely lift a sentence "
        "from assistantMessage: distill a sharper discussion focus or expand it with the "
        "playable judgment and the next design decision it informs. At a genuine decision "
        "point, use either "
        "one concrete question or one independent first-person insight instead of ending the "
        "exchange passively. Do not reuse the preceding card's judgment or wording.\n"
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
        f"Current saved stage (12 x 10):\n{serialized_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
        f"Saved Stage context: {json.dumps(stage_context, ensure_ascii=False)}\n"
        f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}\n"
        f"Latest optional play evidence: {json.dumps(play_summary, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        *[
            {"role": message["role"], "content": message["content"]}
            for message in conversation
            if message.get("role") in {"user", "assistant"}
        ],
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
    serialized_map = "\n".join(rows)
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    solver_metrics = _llm_solver_evidence(solver_metrics or {})
    play_summary = play_summary or {}
    stage_context = stage_context or {}
    map_facts = _map_facts_for_prompt(rows, stage_context)
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
    guidance_instruction = (
        "Do not output a GUIDANCE block for a Stage opening. "
        if stage_opening
        else (
            "After the visible reply, you may append one optional machine-readable block "
            "as one final line using exactly this compact form:\n"
            "<GUIDANCE>DISCUSS: ... || WARNING: ... || MANUAL_EDIT: ... || INTENT: ... || "
            "PROPOSAL_SUMMARY: ... || PROPOSAL_RATIONALE: ... || EXECUTION_BRIEF: {JSON} || DISAGREEMENT: {JSON}</GUIDANCE>\n"
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
            "Use MANUAL_EDIT alone when the designer's direction is too unclear to turn into a "
            "proposal, or pair it with a concrete proposal so designer and LLM can compare the "
            "same local idea. Name the area, what to observe, and why, without prescribing exact "
            "coordinates or implying manual editing is required. These metadata requirements do not require a "
            "question. Do not repeat an unchanged card "
            "listed in Saved Stage context.recentGuidance. The visible reply must stand on "
            "its own and must not mention these tags or mechanically repeat their text. "
        )
    )
    system_prompt = (
        "You are a thoughtful, equal Sokoban co-creation partner speaking like a rational, "
        "warm friend. Prefer first-person observations and opinions. Sound personally engaged, "
        "kind, and clear-headed; avoid stiff transitions, workflow announcements, bureaucratic "
        "notices, and canned service phrasing. Write only the visible "
        f"reply to the designer in {response_language}; do not output JSON, analysis, or "
        "formatting instructions. The only permitted metadata is the optional trailing "
        "GUIDANCE block described below. "
        f"{opening_instruction}{revision_instruction}"
        "Usually use two to four compact paragraphs, varying their rhythm and opening. A "
        "very simple answer may be shorter. Give observations room to breathe: connect a "
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
        f"{guidance_mode_instruction}\n{action_instruction}\n{guidance_instruction}\n"
        f"Draft provenance and attribution rules: {provenance_guidance}\n\n"
        f"{_map_grounding_contract()}\n\n"
        f"Deterministic Map Facts (authoritative):\n{map_facts}\n\n"
        f"Current saved Stage (12 x 10):\n{serialized_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
        f"Saved Stage context: {json.dumps(stage_context, ensure_ascii=False)}\n"
        f"Deterministic solver evidence: {json.dumps(solver_metrics, ensure_ascii=False)}\n"
        f"Latest optional play evidence: {json.dumps(play_summary, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        *[
            {"role": message["role"], "content": message["content"]}
            for message in conversation
            if message.get("role") in {"user", "assistant"}
        ],
    ]


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


def _map_facts_for_prompt(rows, stage_context):
    facts = (stage_context or {}).get("mapFacts")
    if not isinstance(facts, dict):
        try:
            facts = build_map_facts(rows)
        except ValueError:
            # Production Stages are validated before reaching the model.  Keeping this
            # fallback lets isolated prompt/timeout tests use intentionally incomplete grids.
            facts = {
                "available": False,
                "reason": "The supplied rows are not a complete validated Stage.",
            }
    return json.dumps(facts, ensure_ascii=False, separators=(",", ":"))


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
        "future play moment rather than as a fact about the saved map."
    )


def _validate_map_grounding_texts(texts, rows):
    """Reject a small set of high-impact, checkable spatial hallucinations.

    Natural prose is intentionally not parsed wholesale.  This guard covers the claims that
    most often made the visible feedback misleading: assigning an entity to a wrong corner,
    saying that a box currently touches water, and saying that a current box/player is close
    to a target when no such pair exists.  The prompt remains the primary grounding layer.
    """
    text = "\n".join(str(value or "") for value in texts).casefold()
    if not text:
        return

    _validate_coordinate_claims(text, rows)

    try:
        facts = build_map_facts(rows)
    except ValueError:
        return
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


def _validate_coordinate_claims(text, rows):
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


def _execution_brief_from_text(text, rows):
    """Recover a precise brief only when the visible text states one explicitly."""
    source = str(text or "")
    transitions = []
    pattern = re.compile(
        r"\(\s*(\d+)\s*[,，]\s*(\d+)\s*\)\s*"
        r".{0,18}?(?:从|由|from)\s*(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|"
        r"wall|floor|ground|water|box|crate|target|goal|player|start|#|\.|@|p|s|t)\s*"
        r"(?:变成|改成|换成|变为|to|into|becomes?)\s*"
        r"(墙体?|墙|地板|空地|水域?|水|箱子?|目标点?|玩家|起点|"
        r"wall|floor|ground|water|box|crate|target|goal|player|start|#|\.|@|p|s|t)",
        flags=re.IGNORECASE,
    )
    operator_map = {
        (".", "#"): "add_wall", ("#", "."): "remove_wall",
        (".", "@"): "add_water", ("@", "."): "remove_water",
        ("p", "."): "move_player", ("s", "."): "move_box", ("t", "."): "move_target",
    }
    for match in pattern.finditer(source):
        row, column = int(match.group(1)), int(match.group(2))
        before = _tile_from_claim(match.group(3))
        after = _tile_from_claim(match.group(4))
        operator = operator_map.get((before, after))
        if operator is None:
            continue
        transitions.append({"row": row, "column": column, "from": before, "to": after})
    if not transitions:
        return None
    changed_components = {
        "add_wall": "walls", "remove_wall": "walls",
        "add_water": "water", "remove_water": "water",
        "move_player": "player", "move_box": "boxes", "move_target": "targets",
    }
    operators = list(dict.fromkeys(operator_map.get((item["from"], item["to"])) for item in transitions))
    preserve = ["outer_shell", "unrelated_areas", "boxes", "targets", "player", "water", "walls"]
    for operator in operators:
        component = changed_components[operator]
        if component in preserve:
            preserve.remove(component)
    first = transitions[0]
    return _validate_execution_brief({
        "schemaVersion": 1,
        "effect": (
            "open_route" if any(item in {"remove_wall", "remove_water"} for item in operators)
            else "narrow_route" if any(item in {"add_wall", "add_water"} for item in operators)
            else "relocate_start" if "move_player" in operators
            else "relocate_box" if "move_box" in operators
            else "relocate_target"
        ),
        "anchors": [
            anchor for anchor in ("P", "B1", "B2", "T1", "T2")
            if re.search(rf"\b{re.escape(anchor)}\b", source, re.IGNORECASE)
        ],
        "focus": {"row": first["row"], "column": first["column"], "radius": 1},
        "requiredTransitions": transitions,
        "allowedOperators": operators,
        "preserve": preserve,
        "playObjective": "route_choice",
    }, rows)


def generate_stage_assessment(
    conversation,
    rows,
    language,
    solver_metrics,
    play_summary,
    request_id,
    stage_context=None,
):
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
            _max_attempts=1,
        )
    except LLMServiceError as exception:
        if exception.code not in {
            "MODEL_EMPTY_RESPONSE",
            "MODEL_RESPONSE_INVALID",
            "UPSTREAM_TIMEOUT",
            "UPSTREAM_CONNECTION_ERROR",
        }:
            raise

        _log_llm_event(
            "llm_stage_opening_fallback",
            requestId=request_id,
            fromCode=exception.code,
            responseMode="plain_text",
        )
        return _generate_plain_chat_sync(
            conversation=conversation,
            rows=rows,
            request_id=request_id,
            language=language,
            solver_metrics=solver_metrics,
            play_summary=play_summary,
            stage_context=stage_context,
            stage_opening=True,
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
):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise LLMServiceError(
            "CONFIGURATION_ERROR",
            "DEEPSEEK_API_KEY is missing.",
            request_id,
            False,
            0,
            503,
        )

    revision_state, revision_brief = _classify_revision_request(
        conversation,
        stage_context,
    )
    effective_stage_context = dict(stage_context or {})
    effective_stage_context["responseLanguage"] = language
    explicit_action = effective_stage_context.get("explicitAction") or "none"

    if explicit_action == "execute_revision":
        revision_state = "authorized"
        source_offer = effective_stage_context.get("sourceProposalOffer") or {}
        revision_brief = " ".join(
            str(source_offer.get(field) or "").strip()
            for field in ("summary", "rationale")
        ).strip() or revision_brief
        if source_offer.get("executionBrief"):
            effective_stage_context["authorizedExecutionBrief"] = source_offer[
                "executionBrief"
            ]
    elif explicit_action in {"challenge_revision", "alternative_revision"}:
        revision_state, revision_brief = "not_request", None

    if not assessment_only and revision_state != "not_request":
        effective_stage_context["revisionRequestState"] = revision_state
    if revision_brief:
        effective_stage_context["authorizedRevisionBrief"] = revision_brief

    proposal_request = not assessment_only and (
        explicit_action == "execute_revision"
        or (
            not effective_stage_context.get("deferRevisionExecution")
            and revision_state in {"authorized", "authorized_relaxed"}
        )
    )

    if revision_state == "authorized_relaxed":
        effective_stage_context["revisionRelaxed"] = True
        effective_stage_context["relaxationOriginalBrief"] = str(
            (((stage_context or {}).get("recentGuidance") or {}).get("relaxationOffer") or {}).get(
                "originalBrief"
            )
            or ""
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
    default_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    proposal_model = (
        os.getenv("DEEPSEEK_PROPOSAL_MODEL", DEFAULT_PROPOSAL_MODEL).strip()
        or DEFAULT_PROPOSAL_MODEL
    )
    configured_fallback = os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip()
    primary_model = proposal_model if proposal_request else default_model
    fallback_model = configured_fallback or (
        default_model if proposal_request else proposal_model
    )
    models = [primary_model]

    if fallback_model != primary_model:
        models.append(fallback_model)

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
        fallbackModel=models[1] if len(models) > 1 else None,
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
                    max_attempts=_max_attempts,
                ),
                timeout=CHAT_TIMEOUT_SECONDS,
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
            "DeepSeek did not complete the request before the 60 second limit.",
            request_id,
            True,
            min(len(models), _max_attempts),
            504,
        ) from exception


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
):
    started_at = time.monotonic()
    proposal_model = (
        os.getenv("DEEPSEEK_PROPOSAL_MODEL", DEFAULT_PROPOSAL_MODEL).strip()
        or DEFAULT_PROPOSAL_MODEL
    )
    default_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback_model = os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip() or default_model
    models = [proposal_model]
    if fallback_model != proposal_model:
        models.append(fallback_model)
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
        fallbackModel=models[1] if len(models) > 1 else None,
        timeoutSeconds=CHAT_TIMEOUT_SECONDS,
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
                ),
                timeout=PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS,
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
            "DeepSeek did not compile the revision plan before the proposal time limit.",
            request_id,
            True,
            PROPOSAL_GENERATION_ATTEMPTS,
            504,
        ) from exception
    except LLMServiceError as exception:
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="revision_plan",
            outcome="error",
            code=exception.code,
            attemptsUsed=exception.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="revision_plan",
        )
        raise

    plan = _bind_execution_brief_to_plan(
        plan,
        stage_context.get("authorizedExecutionBrief") if stage_context else None,
    )
    try:
        revision_contract = _build_revision_execution_contract(
            plan,
            stage_context.get("authorizedRevisionBrief") if stage_context else "",
            stage_context,
        )
    except ValueError as exception:
        error = LLMServiceError(
            "REVISION_CONTRACT_INVALID",
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
        validate_revision_plan_against_map(rows, plan)
    except ValueError as exception:
        error = LLMServiceError(
            "REVISION_CONTRACT_INVALID",
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
    remaining_seconds = CHAT_TIMEOUT_SECONDS - (time.monotonic() - started_at)
    if remaining_seconds <= 0:
        error = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not create executable revision operations before the 60 second limit.",
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
                    proposal_validator=proposal_validator,
                    revision_contract=revision_contract,
                    baseline_metrics=baseline_metrics,
                    started_at=started_at,
                ),
                timeout=remaining_seconds,
            )
        )
    except asyncio.TimeoutError as exception:
        error = LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not create executable revision operations before the 60 second limit.",
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
        try:
            fallback = _deterministic_revision_fallback(
                rows=rows,
                plan=plan,
                revision_contract=revision_contract,
                request_id=request_id,
                language=language,
                proposal_validator=proposal_validator,
                baseline_metrics=baseline_metrics,
                movement_requirement=movement_requirement,
                preserved_components=preserved_components,
                started_at=started_at,
            )
        except ProposalSearchExhausted as search_exception:
            diagnostics = dict(getattr(exception, "proposal_diagnostics", {}) or {})
            diagnostics["deterministicFallback"] = search_exception.diagnostics
            exception.proposal_diagnostics = diagnostics
            raise exception
        fallback_diagnostics = dict(fallback.proposal_diagnostics or {})
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
        required_transitions=transitions or first.required_transitions,
        anchor_entities=tuple(execution_brief.get("anchors") or first.anchor_entities),
        play_objective=execution_brief.get("playObjective") or first.play_objective,
    )
    return replace(plan, strategies=(bound_first, *plan.strategies[1:]))


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
    execution_brief = stage_context.get("authorizedExecutionBrief") or {}
    original_brief = str(stage_context.get("relaxationOriginalBrief") or "").strip()
    relaxation_rule = (
        "This is a designer-approved fallback. Preserve the original core direction, but one "
        "coherent local effect is sufficient; do not weaken solvability, the outer shell, explicit "
        "prohibitions, or protection of unrelated areas."
        if stage_context.get("revisionRelaxed")
        else "Do not weaken or reinterpret the authorized direction."
    )
    movement_rule = _movement_requirement_prompt(movement_requirement)
    preservation_rule = _preserved_components_prompt(preserved_components)
    map_facts = _map_facts_for_prompt(rows, stage_context)
    edit_facts = _editable_focus_facts(rows, execution_brief.get("focus"))
    numbered_map = "\n".join(
        f"row {index + 1:02d}: {row}" for index, row in enumerate(rows)
    )
    transcript = [
        {
            "role": message.get("role"),
            "content": str(message.get("content") or "")[:2000],
        }
        for message in conversation[-12:]
        if message.get("role") in {"user", "assistant"}
    ]
    system_prompt = (
        "You are the Sokoban co-creation chat assistant. Compile a designer-authorized revision "
        "for one saved 12x10 Stage into a detailed, executable semantic RevisionPlan. Do not "
        "generate map rows, coordinates to edit, or tile operations; a separate level revision "
        "assistant will execute this plan. The application owns all cell changes, structural "
        "validation, and solvability. Preserve the authorized direction and every explicit "
        "prohibition. Treat unmentioned areas as protected. Return JSON only with exactly one key, strategies, "
        "containing one to three objects. Every strategy has exactly: effect, focus, operators, "
        "preserve, editBudget, metricGoals. effect is one of open_route, narrow_route, "
        "adjust_internal_walls, relocate_start, relocate_box, relocate_target, reshape_water, "
        "change_box_order. focus is null or {row,column,radius}; coordinates are one-based, row "
        "1..10, column 1..12, radius 1..3. operators contains one to three distinct values from "
        "add_wall, remove_wall, move_player, move_box, move_target, add_water, remove_water. "
        "preserve contains distinct values from outer_shell, player, boxes, targets, water, "
        "walls, unrelated_areas. Never list an operator that edits a preserved component. "
        "Each strategy may also include requiredTransitions (a list of exact one-based "
        "row/column/from/to changes), anchorEntities (P, B1, B2, T1, T2), and playObjective. "
        "When the authorized brief names an exact coordinate or tile transition, requiredTransitions "
        "is mandatory and hard: do not replace it with a nearby cell or another operator. "
        "focus must contain every required transition. editBudget is an integer 1..12; a single "
        "structural tile change may use budget 1, while moving a player, box, or target requires "
        "two paired cells. Set the budget to the smallest honest upper bound, never a range that "
        "cannot be satisfied. "
        "entity. metricGoals is an empty list or up to three distinct objects with metric "
        "solutionSteps, solutionPushes, or searchedStates and direction increase, decrease, or "
        "preserve. Use objective metrics only when the designer's direction clearly implies them. "
        "Always preserve outer_shell and unrelated_areas. Choose a concrete focus for a local "
        "request, select operators that can realize the effect, and use metricGoals when the "
        "designer clearly requests a measurable change. The first strategy is preferred and any "
        "later strategies are strict alternatives, not permission to weaken the request. Natural-language reasoning is internal. "
        + _map_grounding_contract() + " "
        f"Interpret conversation in {response_language}."
    )
    user_prompt = (
        f"Authorized revision brief: {revision_brief!r}. "
        f"Structured execution brief (authoritative when present): "
        f"{json.dumps(execution_brief, ensure_ascii=False, separators=(',', ':'))}. "
        f"Original pre-fallback brief: {original_brief!r}. {relaxation_rule} {movement_rule} "
        f"{preservation_rule}\n\n"
        "Column ruler (one-based): 123456789012\n"
        f"Deterministic Map Facts (authoritative):\n{map_facts}\n\n"
        f"Focus editable-cell facts (authoritative):\n{edit_facts}\n\n"
        f"Current saved Stage:\n{numbered_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
        f"Recent Stage conversation JSON: {json.dumps(transcript, ensure_ascii=False)}"
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
    max_attempts=PROPOSAL_GENERATION_ATTEMPTS,
    initial_validation_feedback=None,
    first_attempt_timeout=PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS,
):
    last_error = None
    validation_feedback = initial_validation_feedback
    first_failure_code = None
    for attempt in range(1, max_attempts + 1):
        if attempt == 1 or first_failure_code == "MODEL_RESPONSE_INVALID":
            model = models[0]
        else:
            model = models[1] if len(models) > 1 else models[0]
        remaining = PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS - (time.monotonic() - started_at)
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
                "DeepSeek did not respond before the revision-plan attempt timeout.",
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
            **response_fields,
        }
        if failure_reason:
            fields["validationReason"] = failure_reason
        _log_llm_event("llm_attempt_failed", **fields)
        if not last_error.retryable:
            raise last_error
        if attempt == 1 and last_error.code not in {
            "MODEL_RESPONSE_INVALID",
            "MODEL_EMPTY_RESPONSE",
            "UPSTREAM_TIMEOUT",
            "UPSTREAM_CONNECTION_ERROR",
        }:
            raise last_error
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
        "brief, explicit prohibitions, and preserve-unlisted contract unchanged. Do not return "
        "map rows or tile operations."
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
    proposal_model = (
        os.getenv("DEEPSEEK_PROPOSAL_MODEL", DEFAULT_PROPOSAL_MODEL).strip()
        or DEFAULT_PROPOSAL_MODEL
    )
    default_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    configured_fallback = os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip()
    fallback_model = configured_fallback or default_model
    models = [proposal_model]
    if fallback_model != proposal_model:
        models.append(fallback_model)
    legacy_contract = _build_legacy_revision_execution_contract(stage_context)
    messages = _build_map_operation_messages(
        legacy_contract,
        rows,
        language,
        stage_context,
    )
    started_at = time.monotonic()
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task="map_proposal",
        primaryModel=models[0],
        fallbackModel=models[1] if len(models) > 1 else None,
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
                ),
                timeout=CHAT_TIMEOUT_SECONDS,
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
            "DeepSeek did not complete the request before the 60 second limit.",
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
    numbered_map = "\n".join(
        f"row {index + 1:02d}: {row}" for index, row in enumerate(rows)
    )
    map_facts = _map_facts_for_prompt(rows, stage_context)
    focus_facts = [
        {
            "strategyIndex": strategy.get("strategyIndex"),
            "cells": json.loads(
                _editable_focus_facts(rows, strategy.get("focus"))
            ).get("cells", []),
        }
        for strategy in revision_contract.get("strategies") or []
        if strategy.get("focus") is not None
    ]
    solver_evidence = _llm_solver_evidence(baseline_metrics or {})
    system_prompt = (
        "You are the Sokoban co-creation level revision assistant. The saved 12x10 Stage is "
        "immutable input. Execute only the supplied execution contract; do not reinterpret the "
        "designer's request, invent a broader goal, or use any conversation outside the contract. "
        "Return only concrete cell-operation candidates. The application constructs the complete "
        "map, enforces the contract, checks structure, and runs the deterministic solver. "
        "Every candidate must make a meaningful, coherent local change within the contract. Do "
        "not add unrelated cells just to make a diff. Never edit void cells or the connected outer "
        "shell. Never modify preserved components or cells outside the strategy focus. Keep the "
        "map structurally valid with exactly one player and matching box/target pairs. Produce up "
        "to three distinct candidates, each tagged with its strategyIndex. Coordinates are one-based. "
        "Each operation must contain row, column, and to, and may include from as a claim that the "
        "server will verify against the real before tile. Required transitions are hard constraints: "
        "implement every one exactly before considering optional edits. Never replace a required "
        "remove_wall with a box/player move. "
        "A moved player, box, or target requires paired operations that clear the old cell and place "
        "the entity on a current floor cell. Return JSON only with exactly this shape: "
        "{\"candidates\":[{\"strategyIndex\":1,\"operations\":[{\"row\":5,"
        "\"column\":6,\"from\":\".\",\"to\":\"#\"}]}]}. Each operations array contains one to 24 unique "
        "cells. Allowed destination tiles are space, #, ., @, p, s, and t; space edits are forbidden. "
        f"Natural-language reasoning is internal; any unavoidable text must use {response_language}."
    )
    user_prompt = (
        "The following contract is authoritative and is the only revision instruction:\n"
        f"{json.dumps(revision_contract, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Column ruler (one-based): 123456789012\n"
        f"Deterministic Map Facts (authoritative):\n{map_facts}\n\n"
        f"Focus editable-cell facts (authoritative):\n{json.dumps(focus_facts, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"Current saved Stage:\n{numbered_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
        f"Deterministic solver evidence (authoritative): {json.dumps(solver_evidence, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


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
):
    last_error = None
    validation_feedback = None
    attempted_candidate_count = 0
    attempted_models = list(models[:PROPOSAL_GENERATION_ATTEMPTS])
    while len(attempted_models) < PROPOSAL_GENERATION_ATTEMPTS:
        attempted_models.append(models[0])

    for attempt, configured_model in enumerate(attempted_models, start=1):
        model = models[0] if validation_feedback is not None else configured_model
        remaining = CHAT_TIMEOUT_SECONDS - (time.monotonic() - started_at)
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
                "DeepSeek did not respond before the attempt timeout.",
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
        failure_fields = {
            "requestId": request_id,
            "task": "map_proposal",
            "model": model,
            "attempt": attempt,
            "code": last_error.code,
            "retryable": last_error.retryable,
            "latencyMs": int((time.monotonic() - started_at) * 1000),
            "responseMode": "operation_candidates",
            **response_fields,
        }
        if failure_reason:
            failure_fields["validationReason"] = failure_reason[:1200]
        _log_llm_event("llm_attempt_failed", **failure_fields)
        if not last_error.retryable:
            raise last_error

    _log_llm_event(
        "llm_request_completed",
        requestId=request_id,
        task="map_proposal",
        outcome="error",
        code=last_error.code,
        attemptsUsed=last_error.attempts_used,
        latencyMs=int((time.monotonic() - started_at) * 1000),
        responseMode="operation_candidates",
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
):
    """Use the bounded local search when the modifier model cannot supply a valid candidate."""
    result = search_revision_plan(
        rows,
        plan,
        proposal_validator or validate_and_solve,
        baseline_metrics=baseline_metrics,
        deadline=started_at + PROPOSAL_SEARCH_DEADLINE_SECONDS,
        movement_requirement=movement_requirement,
        preserved_components=preserved_components,
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
):
    if not isinstance(payload, dict):
        raise ValueError("The map proposal must be a JSON object.")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= PROPOSAL_CANDIDATE_LIMIT:
        raise ValueError("candidates must contain one to three operation candidates.")
    failures = []
    canonical = set()
    strategies = revision_contract.get("strategies") or []
    if not strategies:
        raise ValueError("The revision contract must contain at least one strategy.")
    for index, candidate in enumerate(candidates, start=1):
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
            rows = execute_revision_operations(
                base_rows,
                operations,
                revision_contract,
                strategy_index,
            )
            signature = tuple(rows)
            if signature in canonical:
                raise ValueError("candidate duplicates an earlier operation result")
            canonical.add(signature)
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
            return rows, index, len(candidates), list(operations)
        except Exception as exception:
            failures.append(f"candidate {index}: {_safe_validation_reason(exception)}")
    raise ValueError("; ".join(failures)[:1200])


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
            raise ValueError("operation coordinate is outside the 12x10 Stage")
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
        if not required.issubset(observed_transitions):
            raise ValueError("operations do not implement every required tile transition")


def _validate_metric_goals(goals, baseline_metrics, validation):
    values = {
        "solutionSteps": getattr(validation, "solution_steps", None),
        "solutionPushes": getattr(validation, "solution_pushes", None),
        "searchedStates": getattr(validation, "searched_states", None),
    }
    for goal in goals:
        metric = goal.get("metric")
        before = baseline_metrics.get(metric)
        after = values.get(metric)
        if before is None or after is None:
            raise ValueError(f"metric goal {metric} cannot be verified")
        direction = goal.get("direction")
        matched = {
            "increase": after > before,
            "decrease": after < before,
            "preserve": after == before,
        }.get(direction, False)
        if not matched:
            raise ValueError(f"metric goal {metric} {direction} was not met")


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
):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise LLMServiceError(
            "CONFIGURATION_ERROR",
            "DEEPSEEK_API_KEY is missing.",
            request_id,
            False,
            0,
            503,
        )

    default_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback_model = (
        os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip()
        or os.getenv("DEEPSEEK_PROPOSAL_MODEL", DEFAULT_PROPOSAL_MODEL).strip()
        or DEFAULT_PROPOSAL_MODEL
    )
    models = [default_model]

    if fallback_model != default_model:
        models.append(fallback_model)

    messages = build_plain_chat_messages(
        conversation,
        rows,
        language,
        solver_metrics,
        play_summary,
        stage_context,
        stage_opening=stage_opening,
    )
    guidance_mode = classify_guidance_request(
        conversation,
        stage_context,
        stage_opening=stage_opening,
    )
    task = "stage_assessment_fallback" if stage_opening else "chat"
    started_at = time.monotonic()
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task=task,
        primaryModel=models[0],
        fallbackModel=models[1] if len(models) > 1 else None,
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
                    stage_context=stage_context,
                    guidance_mode=guidance_mode,
                    started_at=started_at,
                ),
                timeout=PLAIN_CHAT_TIMEOUT_SECONDS,
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
            "DeepSeek did not complete the request before the 25 second limit.",
            request_id,
            True,
            min(len(models), CHAT_MAX_ATTEMPTS),
            504,
        ) from exception


def translate_turns(items, target_language, request_id):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    if not api_key or api_key == "your_deepseek_api_key_here":
        raise LLMServiceError(
            "CONFIGURATION_ERROR",
            "DEEPSEEK_API_KEY is missing.",
            request_id,
            False,
            0,
            503,
        )

    target_name = "Simplified Chinese" if target_language == "zh-CN" else "English"
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
                "coreDisagreement, and nextQuestion fields."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"items": items}, ensure_ascii=False),
        },
    ]
    default_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback_model = (
        os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip()
        or os.getenv("DEEPSEEK_PROPOSAL_MODEL", DEFAULT_PROPOSAL_MODEL).strip()
        or DEFAULT_PROPOSAL_MODEL
    )
    models = [default_model]

    if fallback_model != default_model:
        models.append(fallback_model)

    started_at = time.monotonic()
    _log_llm_event(
        "llm_request_started",
        requestId=request_id,
        task="translation",
        primaryModel=models[0],
        fallbackModel=models[1] if len(models) > 1 else None,
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
                    request_id=request_id,
                    started_at=started_at,
                ),
                timeout=CHAT_TIMEOUT_SECONDS,
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
            "DeepSeek did not complete the translation before the 60 second limit.",
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
    request_id,
    started_at,
):
    last_error = None
    validation_feedback = None

    for attempt, model in enumerate(models[:CHAT_MAX_ATTEMPTS], start=1):
        remaining = CHAT_TIMEOUT_SECONDS - (time.monotonic() - started_at)
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
            maxAttempts=len(models[:CHAT_MAX_ATTEMPTS]),
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
            translations = validate_translation_response(payload, items)
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
                "DeepSeek did not respond before the translation attempt timeout.",
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
            **response_fields,
        }

        if failure_reason:
            failure_fields["validationReason"] = failure_reason

        _log_llm_event("llm_attempt_failed", **failure_fields)

        if not last_error.retryable:
            raise last_error

    if last_error is not None:
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task="translation",
            outcome="error",
            code=last_error.code,
            attemptsUsed=last_error.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="json_object",
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
    started_at,
):
    last_error = None
    validation_feedback = None

    for attempt, model in enumerate(models[:CHAT_MAX_ATTEMPTS], start=1):
        remaining = PLAIN_CHAT_TIMEOUT_SECONDS - (time.monotonic() - started_at)

        if remaining <= 0:
            raise asyncio.TimeoutError()

        attempt_timeout = min(
            PLAIN_PRIMARY_TIMEOUT_SECONDS if attempt == 1 else remaining,
            remaining,
        )
        response_fields = _empty_response_diagnostics()
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
            )
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    attempt_messages,
                    PLAIN_CHAT_MAX_TOKENS,
                    attempt_timeout,
                    structured=False,
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            response_fields = _response_diagnostics(response, choice)
            content = str(choice.message.content or "")

            if not content.strip():
                raise EmptyModelResponse("The model returned an empty response.")

            if len(content.strip()) > CHAT_RESPONSE_MAX_LENGTH:
                raise ValueError("The model response is too long.")

            content = _normalize_unsaved_change_claims(
                _normalize_single_level_language(content.strip()),
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
            )
            disagreement = _extract_plain_disagreement(
                content,
                language,
                stage_context,
            )
            intent_hypothesis, proposal_offer, ui_cues, guidance_fallback_used = (
                _apply_deterministic_guidance_fallback(
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
                    guidance_mode=guidance_mode,
                    allow_required_fallback=attempt >= len(models[:CHAT_MAX_ATTEMPTS]),
                )
            )
            if proposal_offer is not None:
                proposal_offer = _distill_proposal_offer(
                    proposal_offer,
                    visible_content,
                    _latest_role_content(messages[:-1], "assistant"),
                    language,
                )
                if proposal_offer is not None and not proposal_offer.get("executionBrief"):
                    inferred_brief = _execution_brief_from_text(
                        " ".join(
                            str(proposal_offer.get(field) or "")
                            for field in ("summary", "rationale")
                        )
                        + " "
                        + visible_content
                        + " "
                        + _latest_role_content(messages, "user"),
                        rows,
                    )
                    if inferred_brief:
                        proposal_offer["executionBrief"] = inferred_brief
            if (
                guidance_mode == "revision_advice"
                and proposal_offer is None
                and attempt < len(models[:CHAT_MAX_ATTEMPTS])
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

            if discussion_focus is not None:
                question = discussion_focus

            if pure_low_quality:
                raise LowQualityModelResponse(
                    "The model returned only a low-information question."
                )

            if question and _question_repeats_recent_judgment(question, messages):
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
                        user_position=_latest_role_content(messages, "user"),
                    )

            if (stage_context or {}).get("revisionRequestState") == "needs_direction":
                body = _unclear_revision_reply(language)
                question = None
                intent_hypothesis = _unclear_revision_intent(language)
                proposal_offer = None
                ui_cues = []
                guidance_fallback_used = True

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
            }
            guidance = _apply_guidance_card_policy(guidance)
            guidance = _ensure_required_guidance_card(
                guidance,
                messages,
                language,
                rows,
                stage_opening,
                stage_context,
                visible_content=visible_content,
                guidance_mode=guidance_mode,
            )
            body = _deduplicate_assistant_body(body)
            body = _remove_guidance_from_body(
                body,
                guidance.get("followUpQuestion"),
            )
            proposal_binding_issue = _proposal_offer_binding_issue(
                guidance.get("proposalOffer"),
                body,
                messages,
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
                    messages,
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
            if guidance.get("proposalOffer") and not guidance["proposalOffer"].get(
                "executionBrief"
            ):
                # Binding/repair can replace an abstract model offer. Re-attach any
                # exact transition stated by the user's request after that repair so
                # the hidden execution contract cannot lose its hard coordinate.
                inferred_brief = _execution_brief_from_text(
                    " ".join(
                        str(guidance["proposalOffer"].get(field) or "")
                        for field in ("summary", "rationale")
                    )
                    + " "
                    + visible_content
                    + " "
                    + _latest_role_content(messages, "user"),
                    rows,
                )
                if inferred_brief:
                    guidance["proposalOffer"]["executionBrief"] = inferred_brief
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
            _validate_map_grounding_texts(grounding_texts, rows)
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
            result = LLMExecutionResult(
                assistant_message=_compose_assistant_message(
                    _format_stage_opening_paragraphs(body) if stage_opening else body,
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
                **response_fields,
            )
            return result
        except asyncio.TimeoutError:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "DeepSeek did not respond before the attempt timeout.",
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
        }
        if failure_reason:
            failure_fields["validationReason"] = failure_reason
        _log_llm_event(
            "llm_attempt_failed",
            **failure_fields,
        )

        if attempt == 1 and not _plain_fallback_allowed(last_error):
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
        )
        grounding_error = " ".join(
            str(value or "")
            for value in (last_error.safe_message, validation_feedback)
        ).casefold()
        if rows is not None and (
            "executionbrief" in grounding_error
            or "coordinate" in grounding_error
            or "deterministic map facts" in grounding_error
        ):
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
):
    last_error = None
    validation_feedback = None

    for attempt, configured_model in enumerate(models[:max_attempts], start=1):
        model = (
            models[0]
            if task == "map_proposal" and validation_feedback is not None
            else configured_model
        )
        elapsed = time.monotonic() - started_at
        remaining = CHAT_TIMEOUT_SECONDS - elapsed
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

            payload = json.loads(content)
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
            if validated[4].get("proposalOffer") is not None and not validated[4][
                "proposalOffer"
            ].get("executionBrief"):
                inferred_brief = _execution_brief_from_text(
                    " ".join(
                        str(validated[4]["proposalOffer"].get(field) or "")
                        for field in ("summary", "rationale")
                    )
                    + " "
                    + validated[0]
                    + " "
                    + _latest_role_content(messages, "user"),
                    rows,
                )
                if inferred_brief:
                    validated[4]["proposalOffer"]["executionBrief"] = inferred_brief

            if task == "map_proposal" and validated[2] is None:
                raise ValueError(
                    "An explicitly requested map proposal requires proposedRows."
                )

            if validated[2] is not None and proposal_validator is not None:
                proposal_validator(validated[2])

            latency_ms = int((time.monotonic() - started_at) * 1000)
            result = LLMExecutionResult(
                assistant_message=_compose_assistant_message(
                    (
                        _ensure_stage_one_orientation(validated[0], rows, language)
                        if assessment_only and _is_stage_one(stage_context)
                        else validated[0]
                    ),
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
            _log_llm_event(
                "llm_request_completed",
                requestId=request_id,
                task=task,
                outcome="success",
                model=model,
                attemptsUsed=attempt,
                latencyMs=latency_ms,
                responseMode="json_object",
                **response_fields,
            )
            return result
        except asyncio.TimeoutError as exception:
            failure_reason = None
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "DeepSeek did not respond before the attempt timeout.",
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
        }

        failure_fields.update(response_fields)

        if failure_reason:
            failure_fields["validationReason"] = failure_reason

        _log_llm_event(
            "llm_attempt_failed",
            **failure_fields,
        )

        if not last_error.retryable:
            raise last_error

    if last_error is not None:
        _log_llm_event(
            "llm_request_completed",
            requestId=request_id,
            task=task,
            outcome="error",
            code=last_error.code,
            attemptsUsed=last_error.attempts_used,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="json_object",
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


def _plain_messages_with_validation_feedback(messages, validation_feedback):
    if not validation_feedback:
        return messages

    corrected = [dict(message) for message in messages]
    if "REVISION_ADVICE" in validation_feedback or "proposalOffer" in validation_feedback:
        instruction = (
            "Your previous reply for this same request was rejected because it omitted the "
            f"required guidance card: {validation_feedback} Write a fresh reply with a "
            "substantive PROPOSAL_SUMMARY and PROPOSAL_RATIONALE in the trailing GUIDANCE "
            "block, plus the required MANUAL_EDIT companion card. Keep it conceptual: do not "
            "output map rows, tile operations, or claim that the map changed. Do not mention "
            "this correction to the designer."
        )
    else:
        instruction = (
            "Your previous reply for this same request was rejected because it made a spatial "
            f"claim that conflicts with deterministic map facts: {validation_feedback} Write a "
            "fresh grounded reply. Use only verified entity IDs or coordinates for current map "
            "relations, and do not mention this correction to the designer."
        )
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


def validate_translation_response(payload, source_items):
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
        item_expected_fields = expected_fields | (
            {"disagreement"} if "disagreement" in source_items[index] else set()
        )
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
    max_tokens,
    timeout_seconds,
    structured=True,
):
    client = _create_async_client(api_key, base_url, timeout_seconds)
    request_options = {
        "model": model,
        "messages": messages,
        "temperature": 0.45,
        "max_tokens": max_tokens,
        "stream": False,
        "extra_body": {"thinking": {"type": "disabled"}},
    }

    if structured:
        request_options["response_format"] = {"type": "json_object"}

    try:
        return await client.chat.completions.create(**request_options)
    finally:
        await client.close()


def validate_chat_response(
    payload,
    assessment_only=False,
    language="en",
    stage_context=None,
    rows=None,
):
    if not isinstance(payload, dict):
        raise ValueError("The model response must be a JSON object.")

    if set(payload) != {
        "assistantMessage",
        "guidance",
        "assessment",
        "proposedRows",
        "modificationSummary",
    }:
        raise ValueError("The model response contains unexpected or missing fields.")

    assistant_message = _normalize_single_level_language(
        _clean_text(payload.get("assistantMessage"), "assistantMessage")
    )
    guidance = _validate_guidance(
        payload.get("guidance"),
        assessment_only,
        language,
        stage_context,
        rows=rows,
    )
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
        if guidance["proposalOffer"] is not None and not guidance["proposalOffer"].get(
            "executionBrief"
        ) and rows is not None:
            inferred_brief = _execution_brief_from_text(
                " ".join(
                    str(guidance["proposalOffer"].get(field) or "")
                    for field in ("summary", "rationale")
                )
                + " "
                + assistant_message,
                rows,
            )
            if inferred_brief:
                guidance["proposalOffer"]["executionBrief"] = inferred_brief
    stage_one_opening = assessment_only and _is_stage_one(stage_context)
    deterministic_opening_question = None

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
    else:
        assistant_message, extracted_question = _extract_message_question(
            assistant_message,
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

    if assessment_only:
        assistant_message = _format_stage_opening_paragraphs(assistant_message)
    assessment_payload = payload.get("assessment")

    if assessment_payload is None:
        if assessment_only:
            raise ValueError("An assessment-only response requires assessment.")
        assessment = {}
    elif not isinstance(assessment_payload, dict):
        raise ValueError("assessment must be an object.")
    else:
        assessment = {
            "solutionSummary": _normalize_single_level_language(_clean_text(
                assessment_payload.get("solutionSummary"),
                "assessment.solutionSummary",
            )),
            "difficultyOpinion": _normalize_single_level_language(_clean_text(
                assessment_payload.get("difficultyOpinion"),
                "assessment.difficultyOpinion",
            )),
            "features": [
                _normalize_single_level_language(item)
                for item in _clean_list(assessment_payload.get("features"), "features")
            ],
            "suggestions": [
                _normalize_single_level_language(item)
                for item in _clean_list(
                    assessment_payload.get("suggestions"),
                    "suggestions",
                )
            ],
            "satisfactionQuestion": _normalize_single_level_language(
                _clean_optional_text(
                    assessment_payload.get("satisfactionQuestion"),
                    "assessment.satisfactionQuestion",
                )
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
    proposed_rows = payload.get("proposedRows")

    if assessment_only and proposed_rows is not None:
        raise ValueError("An assessment-only response cannot propose a map.")

    if guidance["move"] == "offer_revision" and proposed_rows is not None:
        raise ValueError("A revision offer cannot include proposedRows.")

    if proposed_rows is not None and guidance["move"] != "deliver_revision":
        raise ValueError("proposedRows requires the deliver_revision move.")

    if guidance["move"] == "deliver_revision" and proposed_rows is None:
        raise ValueError("The deliver_revision move requires proposedRows.")

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
    assistant_message = _deduplicate_assistant_body(assistant_message)
    assistant_message = _remove_guidance_from_body(
        assistant_message,
        guidance.get("followUpQuestion"),
    )

    modification_summary = payload.get("modificationSummary", "")

    if not isinstance(modification_summary, str):
        raise ValueError("modificationSummary must be a string.")

    if rows is not None:
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
        _validate_map_grounding_texts(grounding_texts, rows)

    return (
        assistant_message,
        assessment,
        proposed_rows,
        _normalize_single_level_language(modification_summary.strip())[:1000],
        guidance,
    )


def _extract_message_question(message):
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

    if len(questions) > 1:
        raise ValueError("assistantMessage can contain at most one question.")

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
            r"^(WARNING|MANUAL_EDIT|INTENT|PROPOSAL_SUMMARY|PROPOSAL_RATIONALE|EXECUTION_BRIEF)\s*:\s*(.+?)\s*$",
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
    if fields.get("EXECUTION_BRIEF"):
        try:
            execution_brief = _validate_execution_brief(
                json.loads(fields["EXECUTION_BRIEF"]),
                rows,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exception:
            if rows is not None:
                raise ValueError(
                    f"executionBrief is invalid for the saved Stage: {exception}"
                ) from exception
            execution_brief = None
    proposal_offer = (
        {
            "summary": summary[:600],
            "rationale": rationale[:1000],
            **({"executionBrief": execution_brief} if execution_brief else {}),
        }
        if summary and rationale
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

    for field_name, cue_type in (("WARNING", "warning"), ("MANUAL_EDIT", "manual_edit")):
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
        return (
            "CARD ACTION: alternative_revision. The cited proposal is "
            f"{offer_text}. Offer a different conceptual local treatment with a different summary and "
            "playable rationale. Do not emit map rows or a disagreement."
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
    fallback_offer = (
        _deterministic_revision_advice_offer(messages, visible_content, language)
        if _has_revision_material(messages, visible_content)
        else None
    )
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
    latest_user = _latest_role_content(messages, "user")
    question = _friendly_default_discussion_focus(
        rows,
        language,
        latest_user,
        ((stage_context or {}).get("recentGuidance") or {}).get(
            "discussionFocusHistory",
            [],
        ),
    )
    return {
        "body": cleaned_body,
        "proposalOffer": None,
        "followUpQuestion": question,
        "uiCues": warnings,
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

    if (
        guidance_mode == "revision_advice"
        and proposal_offer is None
        and allow_required_fallback
    ):
        proposal_offer = _deterministic_revision_advice_offer(
            messages,
            visible_content,
            language,
        )
        fallback_used = proposal_offer is not None

    difficulty_reframe = _user_reframes_difficulty_judgment(messages)
    intent_hypothesis = _replace_echoed_intent_hypothesis(
        intent_hypothesis,
        latest_user,
        language,
    )
    if intent_hypothesis is None and (
        difficulty_reframe
        or (explicit_direction and not recent.get("intentHypothesis"))
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

    if (
        proposal_offer is None
        and explicit_agreement
        and not recent.get("proposalOffer")
    ):
        previous_assistant = _latest_role_content(messages[:-1], "assistant")
        summary_source = (
            _revision_direction_sentence(visible_content)
            or _first_declarative_sentence(visible_content)
        )
        rationale_source = _first_declarative_sentence(previous_assistant)

        if language == "zh-CN":
            summary = summary_source or "把刚才讨论的方向落实为一个可审查的局部修改"
            rationale = rationale_source or "把玩家已经确认的方向转化为可验证的地图变化"
        else:
            summary = summary_source or "Turn the agreed direction into a reviewable local revision"
            rationale = rationale_source or (
                "Translate the direction you confirmed into a map change that can be validated"
            )

        candidate_offer = {
            "summary": summary[:600],
            "rationale": rationale[:1000],
        }
        previous_offer = recent.get("proposalOffer") or {}

        if not _guidance_text_matches(
            f"{candidate_offer['summary']} {candidate_offer['rationale']}",
            f"{previous_offer.get('summary', '')} {previous_offer.get('rationale', '')}",
        ):
            proposal_offer = candidate_offer
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


def _deterministic_revision_advice_offer(messages, visible_content, language):
    """Create a safe conceptual offer when the model omitted the required advice card."""
    if not _has_revision_material(messages, visible_content):
        return None

    latest_user = _latest_role_content(messages, "user")
    corpus = " ".join(
        part for part in (latest_user, visible_content) if str(part or "").strip()
    ).casefold()
    chinese = language == "zh-CN" or re.search(r"[\u3400-\u9fff]", corpus)

    if chinese:
        if any(marker in corpus for marker in ("时间", "游玩", "停留", "延长", "慢下来", "更久", "太快")):
            summary = "让关键箱子路线形成绕行选择"
            rationale = (
                "围绕当前最直接的箱子路线做局部调整，让玩家在第一次推进前比较直接路线和绕行路线；"
                "试玩时确认游玩时间的增加来自路线判断，而不只是增加移动步数。"
            )
        elif any(marker in corpus for marker in ("绕过障碍", "绕行", "障碍物")):
            summary = "让障碍形成需要绕行的路线选择"
            rationale = (
                "围绕当前关卡的可通行区域做局部调整，让玩家在第一次接近障碍时需要比较绕行路线；"
                "再通过试玩判断它带来的是实际路线选择，而不只是增加移动距离。"
            )
        elif any(marker in corpus for marker in ("水域", "水边", "水") ) and any(
            marker in corpus for marker in ("路线", "通道", "推进", "箱")
        ):
            summary = "让水域参与箱子的路线判断"
            rationale = (
                "把调整集中在水域与相关路线的局部关系上，让箱子第一次经过这里时必须重新读取绕行"
                "空间与推进顺序；试玩时确认水域是否真正改变了选择。"
            )
        elif any(marker in corpus for marker in ("难度", "挑战", "压力", "更难")):
            summary = "把难度落到一次真实的路线取舍"
            rationale = (
                "优先调整会影响关键推动顺序的局部空间，而不是单纯增加障碍数量；试玩时观察玩家是否"
                "在第一次关键推动前停下来判断，并确认难度来自取舍而不是误读。"
            )
        elif any(marker in corpus for marker in ("箱子", "目标", "推动", "顺序")):
            summary = "重新组织箱子与目标的推进顺序"
            rationale = (
                "围绕相关箱子接近目标前的局部路线进行调整，让玩家需要判断先后顺序；试玩时确认"
                "这个变化确实影响推箱关系，而不是只改变位置。"
            )
        else:
            summary = "把当前方向落实为可比较的局部调整"
            rationale = (
                "把改动集中在当前讨论的局部，并用第一次路线选择或关键推动来判断体验是否真的改变，"
                "而不是只产生视觉差异。"
            )
    else:
        if any(marker in corpus for marker in ("time", "playtime", "stay", "longer", "slow", "too quickly")):
            summary = "Make the key box route create a detour choice"
            rationale = (
                "Keep the change local around the most direct box route so the first push makes the player"
                " compare a direct path with a detour; play should confirm that extra time comes from route judgment."
            )
        elif any(marker in corpus for marker in ("detour", "obstacle", "绕行")):
            summary = "Make the obstacles create a meaningful detour choice"
            rationale = (
                "Keep the change local around the traversable space so the first approach to the obstacle"
                " requires a route comparison; play should confirm a real choice rather than extra walking."
            )
        elif "water" in corpus and any(
            marker in corpus for marker in ("route", "corridor", "push", "box")
        ):
            summary = "Make water shape the box's route choice"
            rationale = (
                "Keep the adjustment local to the water and its neighboring route so the first pass makes"
                " the player reread detour space and push order; play should confirm that water changes choice."
            )
        elif any(marker in corpus for marker in ("difficulty", "challenge", "pressure")):
            summary = "Put difficulty into one real route trade-off"
            rationale = (
                "Adjust the local space that affects a key push order instead of merely adding obstacles;"
                " play should show a meaningful pause and choice rather than confusion."
            )
        elif any(marker in corpus for marker in ("box", "crate", "target", "push order")):
            summary = "Reframe the box-to-target push order"
            rationale = (
                "Keep the change around the relevant approach so the player must judge which push comes"
                " first; play should confirm a changed push relationship rather than a visual move."
            )
        else:
            summary = "Turn the direction into a comparable local revision"
            rationale = (
                "Keep the change local to the discussion and judge it through the first route choice or"
                " key push, so the result changes play rather than only appearance."
            )

    return {"summary": summary, "rationale": rationale}


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

    if guidance_mode == "revision_advice":
        proposal_offer = normalized.get("proposalOffer") or (
            _deterministic_revision_advice_offer(
                messages,
                visible_content,
                language,
            )
        )
        if proposal_offer:
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
            "or plan. You must include both PROPOSAL_SUMMARY and PROPOSAL_RATIONALE, then include "
            "MANUAL_EDIT as the companion card. The proposal is conceptual and contains no map rows "
            "or tile operations; it must describe a concrete local design move and the playable effect "
            "to judge. Do not output DISCUSS or INTENT in this action family, and do not claim that "
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

    # A bare open question such as "How would you change the route?" asks for ideas but does
    # not establish a direction.  A statement plus a question, such as "I want water to shape
    # the route; how would you change it?", remains a concrete direction.
    has_question = "?" in text or "？" in text
    if has_question and not re.search(
        r"(?:我想|我希望|我更想|我的目标|希望让|想让|需要让|我倾向于|"
        r"\bi (?:want|would like|hope|prefer)|\bmy goal is\b|\bmake the player\b|"
        r"\blet the player\b)",
        text,
        re.IGNORECASE,
    ):
        return False

    if _contains_concrete_revision_direction(text):
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
    return f"{body}\n\n{compact_guidance}" if body else compact_guidance


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


def _validate_execution_brief(value, rows=None):
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
        if not isinstance(transition, dict) or set(transition) != {"row", "column", "from", "to"}:
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
        normalized_transitions.append({
            "row": row,
            "column": column,
            "from": before,
            "to": after,
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
        facts = build_map_facts(rows)
        known_anchors = {
            "P",
            *(box["id"] for box in facts.get("boxes") or []),
            *(target["id"] for target in facts.get("targets") or []),
        }
        if any(anchor not in known_anchors for anchor in anchors):
            raise ValueError("executionBrief names an entity that is not on the saved Stage.")
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
    allowed_fields = required_fields | {"uiCues", "disagreement"}

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
        if not set(proposal_offer).issubset({"summary", "rationale", "executionBrief"}):
            raise ValueError("proposalOffer contains an invalid field.")

        proposal_offer = {
            "summary": _normalize_single_level_language(
                _clean_text(proposal_offer.get("summary"), "proposalOffer.summary")
            ),
            "rationale": _normalize_single_level_language(_clean_text(
                proposal_offer.get("rationale"),
                "proposalOffer.rationale",
            )),
        }
        if "executionBrief" in payload.get("proposalOffer", {}):
            proposal_offer["executionBrief"] = _validate_execution_brief(
                payload["proposalOffer"]["executionBrief"],
                rows,
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

    return {
        "move": move,
        "intentHypothesis": intent_hypothesis,
        "intentConfidence": intent_confidence,
        "followUpQuestion": follow_up_question,
        "proposalOffer": proposal_offer,
        "disagreement": disagreement,
        "uiCues": normalized_cues,
    }


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


def _format_stage_opening_paragraphs(message):
    paragraphs = [part.strip() for part in message.split("\n\n") if part.strip()]

    if len(paragraphs) > 3:
        paragraphs = [paragraphs[0], paragraphs[1], " ".join(paragraphs[2:])]

    return "\n\n".join(paragraphs)


def _compose_assistant_message(
    message,
    guidance,
    language="en",
    assessment_only=False,
    stage_context=None,
):
    stage_context = stage_context or {}
    message = _deduplicate_assistant_body(message)
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
    additions = [cue["text"] for cue in ui_cues if cue.get("text")]
    follow_up = guidance.get("followUpQuestion")
    proposal_offer = guidance.get("proposalOffer") or {}

    for addition in [*additions, follow_up]:
        message = _remove_guidance_from_body(message, addition)

    for addition in [proposal_offer.get("summary"), proposal_offer.get("rationale")]:
        message = _remove_exact_guidance_from_body(message, addition)

    if follow_up:
        additions.append(follow_up)

    return "\n\n".join(part for part in [message, *additions] if part)


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
            "DeepSeek did not respond before the timeout.",
            request_id,
            True,
            attempts_used,
            504,
        )

    if isinstance(exception, RateLimitError):
        return LLMServiceError(
            "UPSTREAM_RATE_LIMIT",
            "DeepSeek rate limited the request.",
            request_id,
            True,
            attempts_used,
            503,
        )

    if isinstance(exception, APIConnectionError):
        return LLMServiceError(
            "UPSTREAM_CONNECTION_ERROR",
            "The prototype could not connect to DeepSeek.",
            request_id,
            True,
            attempts_used,
            502,
        )

    if isinstance(exception, APIStatusError):
        upstream_status = int(getattr(exception, "status_code", 0) or 0)
        retryable = upstream_status == 429 or upstream_status >= 500
        return LLMServiceError(
            "UPSTREAM_SERVER_ERROR" if retryable else "UPSTREAM_REQUEST_REJECTED",
            f"DeepSeek returned HTTP {upstream_status or 'error'}.",
            request_id,
            retryable,
            attempts_used,
            503 if upstream_status == 429 else 502,
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


def _clean_text(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")

    cleaned = value.strip()

    if len(cleaned) > CHAT_RESPONSE_MAX_LENGTH:
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
    marker_match = any(
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
