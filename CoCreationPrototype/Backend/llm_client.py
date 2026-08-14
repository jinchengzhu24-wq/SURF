import asyncio
from difflib import SequenceMatcher
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)


DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROPOSAL_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
PRIMARY_ATTEMPT_TIMEOUT_SECONDS = 40.0
CHAT_TIMEOUT_SECONDS = 60.0
CHAT_MAX_ATTEMPTS = 2
CHAT_MAX_TOKENS = 1400
PLAIN_CHAT_TIMEOUT_SECONDS = 25.0
PLAIN_PRIMARY_TIMEOUT_SECONDS = 15.0
PLAIN_CHAT_MAX_TOKENS = 900
PROPOSAL_MAX_TOKENS = 2400
TRANSLATION_MAX_TOKENS = 3200
CHAT_RESPONSE_MAX_LENGTH = 4000
PROMPT_VERSION = "cocreation-v25-unified-friendly-cards"

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
        "a deterministic solver fact.\n\n"
        "Treat the saved Stage as read-only until the designer accepts a validated "
        "proposal in the interface. Treat natural requests such as '你帮我改', '你来改吧', "
        "'按这个思路改', 'can you change it', and 'go ahead and revise it' as explicit "
        "authorization when the preceding conversation supplies the direction. You may "
        "proactively offer one concrete revision "
        "direction and rationale, but that offer must not contain proposedRows. Generate "
        "proposedRows only when the designer's latest message explicitly requests a map "
        "change or explicitly agrees to a previously offered revision direction. Never "
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
        '"proposalOffer":null,"uiCues":[]},"assessment":null,"proposedRows":null,'
        '"modificationSummary":""}.\n'
        "guidance.move must be one of observe_stage, clarify_intent, "
        "offer_perspective, challenge_tradeoff, reflect_on_play, offer_revision, "
        "or deliver_revision. intentConfidence must be null, low, medium, or high. "
        "followUpQuestion is the LET'S DISCUSS card. It must be null, one concrete question, "
        "or one concise first-person design insight worth discussing. A declarative insight "
        "must name a concrete map or play judgment and must not duplicate assistantMessage. "
        "proposalOffer must be null or "
        'an object with exactly {"summary":"...","rationale":"..."}. '
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
    provenance_guidance = _build_draft_provenance_guidance(stage_context)
    revision_request_state = stage_context.get("revisionRequestState")
    revision_instruction = (
        "The designer asked you to modify the map, but neither this message nor the recent "
        "conversation contains a concrete revision direction. Do not invent one and do not "
        "claim to edit anything. Give a short declarative explanation and output only one "
        "contextual MANUAL_EDIT card that helps the designer point out or try the unclear area; "
        "do not output DISCUSS, INTENT, WARNING, or proposal fields. "
        if revision_request_state == "needs_direction"
        else ""
    )
    opening_instruction = (
        "This is the opening for a verified saved Stage. Notice one or two concrete "
        "authored choices and offer a clearly subjective perspective. Do not inventory "
        "the map, use a workflow greeting, or ask for an overall experience category. "
        + (
            "This is Stage 1: do not ask any question. Naturally mention that the designer "
            "can share an impression, play the Stage, or try a local edit in the right panel; "
            "present these as possibilities, not a checklist. "
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
            "PROPOSAL_SUMMARY: ... || PROPOSAL_RATIONALE: ...</GUIDANCE>\n"
            "Omit any field that is not warranted, and omit the entire block when no card "
            "would improve the collaboration only when the designer explicitly changes to a "
            "topic unrelated to this project. Apart from that off-topic exception and the very "
            "first Stage 1 opening, every reply must produce at least one card. Visible cards "
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
            "synthesis of the actual design move; PROPOSAL_RATIONALE should independently expand "
            "the expected playable effect and what the designer can judge from it. Add "
            "WARNING only with strong evidence: explicit play "
            "difficulty, or a mechanically explainable interaction between at least two "
            "specific map elements and a concrete push moment. Keep ordinary uncertainty, "
            "route trade-offs, and aesthetic opinions in the visible reply. A warning should "
            "sound like a natural first-person aside, not a formal alert or stock phrase. "
            "Use DISCUSS at a useful later decision point for either one concrete, vivid "
            "question or one concise first-person design insight. A DISCUSS insight must add "
            "a judgment rather than repeat the visible reply. "
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
        f"{guidance_instruction}\n"
        f"Draft provenance and attribution rules: {provenance_guidance}\n\n"
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

    if not assessment_only and revision_state != "not_request":
        effective_stage_context["revisionRequestState"] = revision_state
    if revision_brief:
        effective_stage_context["authorizedRevisionBrief"] = revision_brief

    proposal_request = not assessment_only and revision_state == "authorized"

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
                "\"proposalSummary\":null}]}."
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
    started_at,
):
    last_error = None

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
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    messages,
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
                )
            )
            if proposal_offer is not None:
                proposal_offer = _distill_proposal_offer(
                    proposal_offer,
                    visible_content,
                    _latest_role_content(messages[:-1], "assistant"),
                    language,
                )
            visible_content = _remove_extracted_warning_sentence(
                visible_content,
                ui_cues,
            )
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

            if not stage_opening and question is None:
                question = _deterministic_key_question(
                    messages,
                    language,
                    rows,
                )
                if question is None and proposal_offer is not None:
                    question = _deterministic_reply_discussion_focus(
                        visible_content,
                        language,
                    )
                if question and _question_repeats_recent_judgment(question, messages):
                    question = None
                recent_focus = ((stage_context or {}).get("recentGuidance") or {}).get(
                    "discussionFocus"
                )
                if question and _guidance_text_matches(question, recent_focus):
                    question = None

            if stage_opening and _is_stage_one(stage_context):
                body = _questionless_body(visible_content)
                question = None
                if not body:
                    raise LowQualityModelResponse(
                        "The Stage 1 opening contained only questions."
                    )
                body = _ensure_stage_one_orientation(body, rows, language)
            elif stage_opening and question is not None:
                try:
                    question = _normalize_opening_question(question)
                except ValueError:
                    body = visible_content
                    question = None

            if stage_opening and question is None:
                question = _deterministic_stage_opening_question(
                    stage_context,
                    language,
                )

            if not stage_opening and question is not None:
                question = _refine_discussion_focus(
                    question,
                    visible_content,
                    language,
                )

            if (stage_context or {}).get("revisionRequestState") == "needs_direction":
                body = _unclear_revision_reply(language)
                question = None
                intent_hypothesis = None
                proposal_offer = None
                ui_cues = [{
                    "type": "manual_edit",
                    "text": _unclear_revision_manual_edit(language),
                }]
                guidance_fallback_used = True

            guidance = {
                "move": _plain_guidance_move(
                    stage_opening,
                    intent_hypothesis,
                    proposal_offer,
                ),
                "intentHypothesis": intent_hypothesis,
                "intentConfidence": "medium" if intent_hypothesis else None,
                "followUpQuestion": question,
                "proposalOffer": proposal_offer,
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
            last_error = LLMServiceError(
                "UPSTREAM_TIMEOUT",
                "DeepSeek did not respond before the attempt timeout.",
                request_id,
                True,
                attempt,
                504,
            )
        except Exception as exception:
            last_error = classify_exception(exception, request_id, attempt)

        _log_llm_event(
            "llm_attempt_failed",
            requestId=request_id,
            task=task,
            model=model,
            attempt=attempt,
            code=last_error.code,
            retryable=last_error.retryable,
            latencyMs=int((time.monotonic() - started_at) * 1000),
            responseMode="plain_text",
            **response_fields,
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
            )

            if task == "map_proposal" and validated[2] is None:
                raise ValueError(
                    "An explicitly requested map proposal requires proposedRows."
                )

            if validated[2] is not None and proposal_validator is not None:
                proposal_validator(validated[2])

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
        if not isinstance(translation, dict) or set(translation) != expected_fields:
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
        translated_by_id[turn_id] = normalized

    if set(translated_by_id) != set(source_by_id):
        raise ValueError("Translation output omitted one or more requested turns.")

    return [translated_by_id[item["turnId"]] for item in source_items]


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
    guidance = _validate_guidance(payload.get("guidance"), assessment_only, language)
    if guidance.get("proposalOffer") is not None:
        guidance["proposalOffer"] = _distill_proposal_offer(
            guidance["proposalOffer"],
            assistant_message,
            "",
            language,
        )
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

    if not assessment_only and guidance["followUpQuestion"] is not None:
        guidance["followUpQuestion"] = _refine_discussion_focus(
            guidance["followUpQuestion"],
            assistant_message,
            language,
        )

    if assessment_only and not stage_one_opening and guidance["followUpQuestion"] is None:
        deterministic_opening_question = _deterministic_stage_opening_question(
            stage_context,
            language,
        )
        guidance["followUpQuestion"] = deterministic_opening_question

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

    modification_summary = payload.get("modificationSummary", "")

    if not isinstance(modification_summary, str):
        raise ValueError("modificationSummary must be a string.")

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
    else:
        text = re.sub(
            r"(?:^|(?<=[.!?\n]))\s*(?:done[.!]?\s*)?(?:i(?:'ve| have)?|we(?:'ve| have)?)"
            r"\s+(?:changed|modified|revised|updated|finished)\s+(?:it|this|the map|the level)"
            r"[.!]*(?:\s+(?:go ahead and )?(?:try|play)(?: it)?[.!]?)?",
            "I am describing the revision direction for now; I have not saved a map change.",
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


def _deterministic_stage_opening_question(stage_context, language):
    """Ask about a verified edit without inventing a second level or exact coordinates."""
    context = stage_context or {}
    if _is_stage_one(context) or context.get("source") != "human_edit":
        return None

    components = set((context.get("changeSummary") or {}).get("components") or [])
    if not components:
        return None

    if language == "zh-CN":
        if "water" in components:
            return (
                "当箱子第一次贴着调整后的水域边缘推进时，你最想观察哪一处转折，"
                "来判断这个版本的路线选择是否更清楚？"
            )
        if "internalWalls" in components:
            return (
                "当箱子第一次穿过调整后的内部通道时，你最想观察哪个转折，"
                "来判断这个版本的推动顺序是否更容易读懂？"
            )
        if "targets" in components:
            return (
                "当箱子第一次接近调整后的目标点时，你最想观察哪一步停顿，"
                "来判断这个版本的目标关系是否更容易读懂？"
            )
        if "boxes" in components:
            return (
                "当调整后的箱子开始第一次推进时，你最想观察哪一步，"
                "来判断这个版本的推动顺序是否足够清楚？"
            )
        return (
            "当箱子第一次进入这次调整影响的区域时，你最想观察哪个动作，"
            "来判断这个版本的路线是否更容易读懂？"
        )

    if "water" in components:
        return (
            "When the box first moves along the adjusted water edge, which turn would you "
            "watch to judge whether this version makes the route choice clearer?"
        )
    if "internalWalls" in components:
        return (
            "When the box first passes through the adjusted inner passage, which turn would "
            "you watch to judge whether this version makes the push order easier to read?"
        )
    if "targets" in components:
        return (
            "When the box first approaches the adjusted target, which pause would you watch "
            "to judge whether this version makes the target relationship easier to read?"
        )
    if "boxes" in components:
        return (
            "When the adjusted box begins its first push, which step would you watch to judge "
            "whether this version makes the push order clear enough?"
        )
    return (
        "When the box first enters the area affected by this edit, which move would you "
        "watch to judge whether this version makes the route easier to read?"
    )


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


def _refine_discussion_focus(focus, visible_content, language):
    value = re.sub(r"\s+", " ", str(focus or "")).strip()
    if not value:
        return value

    combined = f"{visible_content} {value}".strip()
    synthesized = _deterministic_reply_discussion_focus(combined, language)
    if synthesized and not _guidance_reuses_visible_sentence(synthesized, visible_content):
        return synthesized[:1000]

    question_marks = value.count("?") + value.count("？")
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", combined):
        if question_marks == 1:
            core = value.rstrip()
            return (
                f"我想先和你盯住这个瞬间：{core}"
                "看完它，我们就知道是只动附近，还是该回头看看整条路线。"
            )[:1000]
        if len(value) < 72:
            return (
                f"我想先陪你验证一下：{value.rstrip('。')}。"
                "别急着动大结构，先看第一次推箱时玩家会不会自然停一下想路线。"
            )[:1000]
        return value[:1000]

    if question_marks == 1:
        return (
            f"I would focus our discussion on this playable judgment: {value} "
            "Your answer will tell me whether the next step should change the route relationship "
            "or preserve the current spatial structure."
        )[:1000]
    if len(value) < 150:
        return (
            f"I would keep this judgment separate for discussion: {value.rstrip('.')}. "
            "It is worth testing through the first push because the real comparison is how the "
            "route reads, not only how the map looks."
        )[:1000]
    return value[:1000]


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
    recent_focus = ((stage_context or {}).get("recentGuidance") or {}).get(
        "discussionFocus"
    )
    visible = value[:marker_index].strip()
    if (
        not useful
        or _guidance_text_matches(focus, recent_focus)
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


def _extract_plain_guidance(content, language, stage_context, stage_opening=False):
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
            r"^(WARNING|MANUAL_EDIT|INTENT|PROPOSAL_SUMMARY|PROPOSAL_RATIONALE)\s*:\s*(.+?)\s*$",
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
    proposal_offer = (
        {
            "summary": summary[:600],
            "rationale": rationale[:1000],
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


def _warning_text_is_evidence_grounded(text, language):
    lowered = str(text or "").casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", lowered):
        anchors = ("水", "箱", "目标", "墙", "通道", "路线", "入口", "退路", "推动", "绕行")
        risks = (
            "可能", "担心", "风险", "值得", "我有点在意", "我不太放心",
            "死锁", "卡住", "误读", "重复", "顺序", "退路",
        )
    else:
        anchors = ("water", "box", "crate", "target", "wall", "corridor", "route", "entrance", "escape", "push")
        risks = (
            "may", "might", "concern", "risk", "worth", "i notice", "i am uneasy",
            "deadlock", "stuck", "misread", "repeat", "order", "escape",
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


def _distill_proposal_offer(proposal_offer, visible_content, previous_content, language):
    if not proposal_offer:
        return None

    original_summary = str(proposal_offer.get("summary") or "").strip()
    original_rationale = str(proposal_offer.get("rationale") or "").strip()
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

        if has_target and lower_area:
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

        if has_target and lower_area:
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
    return {
        "summary": (summary if summary_needs_distilling else original_summary)[:600],
        "rationale": (rationale if rationale_needs_expansion else original_rationale)[:1000],
    }


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
):
    if stage_opening:
        return intent_hypothesis, proposal_offer, [], False

    latest_user = _latest_role_content(messages, "user")
    explicit_direction = _latest_user_states_direction(messages)
    explicit_agreement = _latest_user_explicitly_agrees(latest_user)
    recent = (stage_context or {}).get("recentGuidance") or {}
    evidence_signature = (stage_context or {}).get("guidanceEvidenceSignature")
    fallback_used = False

    if (
        intent_hypothesis is None
        and explicit_direction
        and not recent.get("intentHypothesis")
    ):
        candidate = _natural_intent_candidate(
            latest_user,
            language,
            explicit_agreement,
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
        summary_source = _first_declarative_sentence(visible_content)
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

    if "manual_edit" not in cue_by_type and (
        needs_direction or needs_action_companion
    ):
        manual_edit = (
            _unclear_revision_manual_edit(language)
            if needs_direction
            else _contextual_manual_edit(rows, language)
        )
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
):
    normalized = dict(guidance or {})
    latest_user = _latest_role_content(messages, "user")

    if _is_stage_one(stage_context) and stage_opening:
        return normalized

    if _user_explicitly_off_topic(latest_user):
        normalized["intentHypothesis"] = None
        normalized["intentConfidence"] = None
        normalized["followUpQuestion"] = None
        normalized["proposalOffer"] = None
        normalized["uiCues"] = []
        return normalized

    card_count = (
        int(bool(normalized.get("intentHypothesis")))
        + int(bool(normalized.get("followUpQuestion")))
        + int(bool(normalized.get("proposalOffer")))
        + len(normalized.get("uiCues") or [])
    )
    if card_count:
        return normalized

    normalized["followUpQuestion"] = _friendly_default_discussion_focus(
        rows,
        language,
        latest_user,
        ((stage_context or {}).get("recentGuidance") or {}).get("discussionFocus"),
    )
    return normalized


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


def _friendly_default_discussion_focus(rows, language, latest_user, recent_focus):
    serialized = "".join(str(row) for row in (rows or []))
    seed = sum(ord(character) for character in f"{serialized}{latest_user}")
    if language == "zh-CN":
        options = (
            "我想先陪你看看第一次推箱时，玩家会不会自然停下来想一想路线。这个瞬间舒服的话，我们就微调附近；如果还是太直白，再一起看整条路。",
            "我更想先看箱子第一次靠近目标前，玩家会不会有一点自然的犹豫。这个感觉对了，就别急着大改；不对，我们再一起找是哪里太顺了。",
            "我想先盯住玩家第一次得决定“先推哪个箱子”的瞬间。要是这里已经让人想一想，我们就只动局部；如果一眼看穿，再回头看路线本身。",
        )
    else:
        options = (
            "I want to keep the route reading at the first push separate for discussion; it will tell us whether to preserve the current space or clarify the key turn next.",
            "To me, the hesitation before a box first approaches its target is worth watching; it will shape whether we adjust the route relationship or preserve this structure.",
            "I would first watch the moment when the player has to plan the push order; that judgment can tell us whether the next revision should change local rhythm or the larger route.",
        )
    for offset in range(len(options)):
        candidate = options[(seed + offset) % len(options)]
        if not _guidance_text_matches(candidate, recent_focus):
            return candidate
    return options[seed % len(options)]


def _unclear_revision_manual_edit(language):
    if language == "zh-CN":
        return (
            "我还没读准你最想动哪一块。你可以先在右侧编辑器标出或轻微调整最在意的"
            "局部；保存后，我会沿着这个真实变化继续和你一起判断。"
        )
    return (
        "I have not yet understood which part you most want to change. You can mark or "
        "lightly adjust the area that concerns you most in the right-hand editor; after it "
        "is saved, I can continue from that actual change with you."
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


def _natural_intent_candidate(latest_user, language, explicit_agreement):
    source = re.sub(r"\s+", " ", str(latest_user or "")).strip()
    variant = sum(ord(character) for character in source) % 3

    if language == "zh-CN":
        if explicit_agreement:
            options = (
                "听起来你已经准备把刚才的想法放进一次具体尝试里；这是我目前的理解。",
                "我暂时把你的方向理解为：先让这个局部想法真正参与一次游玩判断。",
                "我读到的倾向是，你更想先做出一个能亲手比较的局部变化。",
            )
        else:
            excerpt = source[:120].rstrip("。！？?!")
            options = (
                f"我暂时把你的方向理解为：{excerpt}。",
                f"听起来你更在意的是“{excerpt}”带来的实际游玩感受。",
                f"我读到的倾向是，你希望后续设计真正回应“{excerpt}”。",
            )
    elif explicit_agreement:
        options = (
            "It sounds to me like you are ready to put that idea into a concrete trial.",
            "For now, I understand your direction as testing this idea through a local change.",
            "I read your preference as making this idea tangible enough to compare in play.",
        )
    else:
        excerpt = source[:160].rstrip(".!?")
        options = (
            f"For now, I understand your direction as: {excerpt}.",
            f"It sounds to me like the play effect behind “{excerpt}” matters most to you.",
            f"I read your preference as wanting the next design step to answer “{excerpt}”.",
        )
    return options[variant]


def _intent_is_only_execution_authorization(text, language="en"):
    value = str(text or "").strip().casefold()
    if not value:
        return False

    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", value):
        execution = any(
            marker in value
            for marker in (
                "你帮我改", "帮我改一下", "你来改", "请你改", "交给你改",
                "我来帮你改", "把修改交给我", "由我来改",
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


def _first_declarative_sentence(message):
    for sentence in re.split(r"(?<=[.!?。！？])\s*|[\r\n]+", str(message or "")):
        cleaned = sentence.strip()
        if cleaned and not cleaned.endswith(("?", "？")):
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
    lowered = text.casefold()
    if language == "zh-CN" or re.search(r"[\u3400-\u9fff]", text):
        has_orientation = (
            any(word in text for word in ("感受", "直觉", "反应", "想法"))
            and "试玩" in text
            and any(word in text for word in ("编辑", "调整", "修改"))
        )
        options = (
            "你可以先从直觉说起；想验证时，右侧的试玩和局部编辑都可以随时接上。",
            "这里不用先定好目标，先说哪处让你在意也行；之后可以试玩，或在右侧做一点局部调整。",
            "接下来可以先聊第一反应，也可以先试玩，再用右侧编辑器动一小块看看体验怎么变化。",
        )
    else:
        has_orientation = (
            any(word in lowered for word in ("impression", "instinct", "reaction", "idea"))
            and any(word in lowered for word in ("play", "try the stage"))
            and any(word in lowered for word in ("edit", "adjust", "right panel"))
        )
        options = (
            "You can start with your first impression; when you want to test it, play the Stage or try a small edit in the right panel.",
            "There is no need to settle on a goal yet—share what catches your attention, then play or make a small local adjustment when useful.",
            "From here, you can talk through your first reaction, play the Stage, or nudge one local area in the right-hand editor and compare the feel.",
        )
    if has_orientation:
        return text
    serialized_rows = "".join(str(row) for row in (rows or []))
    variant = sum(ord(character) for character in serialized_rows or text) % len(options)
    return f"{text}\n\n{options[variant]}"


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


def _build_task_instructions(assessment_only, stage_context=None):
    if assessment_only:
        stage_one_instruction = (
            "This is Stage 1. Do not ask a question. After your concrete observation, "
            "naturally let the designer know they can share an impression, play this Stage, "
            "or make a local edit in the right panel. Do not present those options as a list. "
            if _is_stage_one(stage_context)
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
        "complete proposedRows map only after explicit designer authorization."
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


def _validate_guidance(payload, assessment_only, language="en"):
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
    allowed_fields = required_fields | {"uiCues"}

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

        if set(proposal_offer) != {"summary", "rationale"}:
            raise ValueError("proposalOffer must contain summary and rationale.")

        proposal_offer = {
            "summary": _normalize_single_level_language(
                _clean_text(proposal_offer.get("summary"), "proposalOffer.summary")
            ),
            "rationale": _normalize_single_level_language(_clean_text(
                proposal_offer.get("rationale"),
                "proposalOffer.rationale",
            )),
        }
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

    if assessment_only:
        if move != "observe_stage":
            raise ValueError("A Stage opening must use observe_stage.")
        if intent_hypothesis is not None or proposal_offer is not None or normalized_cues:
            raise ValueError("A Stage opening cannot infer intention or offer a revision.")

    return {
        "move": move,
        "intentHypothesis": intent_hypothesis,
        "intentConfidence": intent_confidence,
        "followUpQuestion": follow_up_question,
        "proposalOffer": proposal_offer,
        "uiCues": normalized_cues,
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
        "帮我做个方案",
        "帮我做一个方案",
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

    requested = (
        marker_match
        or chinese_authorization is not None
        or chinese_polite_request is not None
        or chinese_short_command is not None
        or english_authorization is not None
    )

    if not requested:
        return "not_request", None

    brief = _authorized_revision_brief(
        conversation,
        stage_context,
        latest_user_message,
    )
    return ("authorized", brief) if brief else ("needs_direction", None)


def _requests_complete_map(conversation, stage_context=None):
    state, _ = _classify_revision_request(conversation, stage_context)
    return state == "authorized"


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
            "集中", "连接", "缩短", "拉开", "让", "改动", "修改",
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
            r"把.{0,40}(?:移|挪|调整|重排|保留|减少|增加|改变|集中|连接|缩短|拉开|改)",
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
    return any(marker in text for marker in framing) or framed_action is not None


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
