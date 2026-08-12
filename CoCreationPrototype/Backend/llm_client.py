import asyncio
import json
import os
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
PROPOSAL_MAX_TOKENS = 2400
CHAT_RESPONSE_MAX_LENGTH = 4000
PROMPT_VERSION = "cocreation-v8-nonthinking-normalized"

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
    solver_metrics = solver_metrics or {}
    play_summary = play_summary or {}
    stage_context = stage_context or {}
    task = _build_task_instructions(assessment_only)
    system_prompt = (
        "You are an adaptive Sokoban co-creation partner. Work only with the exact "
        "saved Stage and evidence supplied below. Respond directly to the designer's "
        "latest contribution instead of producing a generic evaluation report. Choose "
        "one primary conversational move: observe the Stage, clarify intention, offer "
        "your perspective, challenge a trade-off respectfully, reflect on play evidence, "
        "offer a revision direction, or deliver an explicitly requested revision. "
        "Use two to four short paragraphs and at most one central question. A factual "
        "question should be answered before any optional follow-up. Do not mechanically "
        "include every possible move in each reply.\n\n"
        "Help the designer form and refine their own intention without assigning a "
        "predefined purpose. Infer intention only when conversation or design actions "
        "provide evidence. Phrase every inference as a tentative, correctable hypothesis "
        "and invite correction. Put that hypothesis only in intentHypothesis so the "
        "interface can distinguish it from your ordinary response. Never store or present "
        "an inferred intention as the "
        "designer's final self-report. You may disagree and identify trade-offs, but make "
        "clear that evaluative and difficulty statements are your perspective. Every "
        "difficulty statement must explicitly use perspective language such as 'in my "
        "view', 'I suspect', '在我看来', or '我倾向于认为'; never present difficulty as "
        "a deterministic solver fact.\n\n"
        "Treat the saved Stage as read-only until the designer accepts a validated "
        "proposal in the interface. You may proactively offer one concrete revision "
        "direction and rationale, but that offer must not contain proposedRows. Generate "
        "proposedRows only when the designer's latest message explicitly requests a map "
        "change or explicitly agrees to a previously offered revision direction. Never "
        "claim a proposal has been accepted, saved, or verified. Deterministic solver and "
        "play evidence are authoritative only for the facts they report; never invent "
        "play results or a verified solution. A Stage number is a saved-version index, "
        "not a campaign or difficulty sequence. Never call Stage 1 the first level, "
        "assume it is a tutorial, or infer intended player progression from its number.\n\n"
        "The designer can also edit the level directly with the tile tools in the right "
        "panel and save it as a new Stage after deterministic validation. Mention this "
        "option briefly only when it would help the designer act on their own idea—for "
        "example when they want direct control, reject your direction, or the discussion "
        "remains abstract. Do not repeat the editor hint every turn and never frame manual "
        "editing as required. Put the complete editor suggestion in a manual_edit uiCue "
        "instead of repeating it in assistantMessage. Use a warning uiCue when current "
        "map structure, version changes, solver metrics, or play evidence indicates a "
        "reasonable potential risk. The threshold is intentionally moderate: warn before "
        "the risk is certain when it could affect playability, deadlocks or softlocks, "
        "push order, route readability, target comprehension, repetitive movement, or the "
        "designer's stated direction. Phrase uncertain risks with language such as may, "
        "I am concerned, or worth playtesting; do not present them as solver facts. Do not "
        "use warnings for unsupported guesses or purely aesthetic preferences. When "
        "challenge_tradeoff is the primary move, include exactly one concise warning uiCue "
        "and do not repeat it in assistantMessage. Use no more than two uiCues, no more "
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
        f"Write all new natural-language fields in {response_language}. {task}\n\n"
        "Return JSON only with exactly these keys:\n"
        '{"assistantMessage":"...","guidance":'
        '{"move":"observe_stage","intentHypothesis":null,'
        '"intentConfidence":null,"followUpQuestion":"...",'
        '"proposalOffer":null,"uiCues":[]},"assessment":null,"proposedRows":null,'
        '"modificationSummary":""}.\n'
        "guidance.move must be one of observe_stage, clarify_intent, "
        "offer_perspective, challenge_tradeoff, reflect_on_play, offer_revision, "
        "or deliver_revision. intentConfidence must be null, low, medium, or high. "
        "followUpQuestion must be null or one question. proposalOffer must be null or "
        'an object with exactly {"summary":"...","rationale":"..."}. '
        "uiCues must be an array of at most two unique objects with exactly type and "
        "text; type must be manual_edit or warning. The legacy tradeoff type is accepted "
        "by the application for historical data but must not be generated. "
        "assistantMessage must not repeat followUpQuestion; the application appends it.\n"
        "assessment must normally be null. For a newly saved Stage opening it must "
        "instead be an object with exactly solutionSummary, difficultyOpinion, features, "
        "suggestions, and satisfactionQuestion; features and suggestions are non-empty "
        "arrays of strings, and satisfactionQuestion must match the conversational focus.\n"
        "When proposedRows is present it must contain exactly 10 strings of 12 "
        "characters using only space, #, ., @, p, s, and t, with one p and one or "
        "two matching s/t pairs. Keep changes focused on the designer request.\n\n"
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


def generate_stage_assessment(
    conversation,
    rows,
    language,
    solver_metrics,
    play_summary,
    request_id,
    stage_context=None,
):
    return generate_chat_reply(
        conversation,
        rows,
        request_id,
        language=language,
        solver_metrics=solver_metrics,
        play_summary=play_summary,
        assessment_only=True,
        stage_context=stage_context,
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

    messages = build_chat_messages(
        conversation,
        rows,
        language,
        solver_metrics,
        play_summary,
        assessment_only,
        stage_context,
    )
    proposal_request = not assessment_only and _requests_complete_map(conversation)
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
                    stage_context=stage_context,
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
            task=task,
            outcome="error",
            code="UPSTREAM_TIMEOUT",
            attemptsUsed=min(len(models), CHAT_MAX_ATTEMPTS),
            latencyMs=elapsed_ms,
        )
        raise LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "DeepSeek did not complete the request before the 60 second limit.",
            request_id,
            True,
            min(len(models), CHAT_MAX_ATTEMPTS),
            504,
        ) from exception


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
):
    last_error = None

    for attempt, model in enumerate(models[:CHAT_MAX_ATTEMPTS], start=1):
        elapsed = time.monotonic() - started_at
        remaining = CHAT_TIMEOUT_SECONDS - elapsed

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
            maxAttempts=len(models[:CHAT_MAX_ATTEMPTS]),
            timeoutSeconds=round(attempt_timeout, 3),
        )

        try:
            response = await asyncio.wait_for(
                _request_completion(
                    api_key,
                    base_url,
                    model,
                    messages,
                    max_tokens,
                    attempt_timeout,
                ),
                timeout=attempt_timeout,
            )
            choice = response.choices[0]
            finish_reason = str(getattr(choice, "finish_reason", "") or "")

            if finish_reason == "length":
                raise ValueError("The model output reached its token limit.")

            content = str(choice.message.content or "")
            payload = json.loads(content)
            validated = validate_chat_response(payload, assessment_only)

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
            )
            return result
        except asyncio.TimeoutError as exception:
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


async def _request_completion(
    api_key,
    base_url,
    model,
    messages,
    max_tokens,
    timeout_seconds,
):
    client = _create_async_client(api_key, base_url, timeout_seconds)

    try:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.45,
            max_tokens=max_tokens,
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
    finally:
        await client.close()


def validate_chat_response(payload, assessment_only=False):
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

    assistant_message = _clean_text(payload.get("assistantMessage"), "assistantMessage")
    guidance = _validate_guidance(payload.get("guidance"), assessment_only)
    assistant_message = _remove_question_paragraphs(
        assistant_message,
        guidance["followUpQuestion"],
    )
    assessment_payload = payload.get("assessment")

    if assessment_payload is None:
        if assessment_only:
            raise ValueError("An assessment-only response requires assessment.")
        assessment = {}
    elif not isinstance(assessment_payload, dict):
        raise ValueError("assessment must be an object.")
    else:
        assessment = {
            "solutionSummary": _clean_text(
                assessment_payload.get("solutionSummary"),
                "assessment.solutionSummary",
            ),
            "difficultyOpinion": _clean_text(
                assessment_payload.get("difficultyOpinion"),
                "assessment.difficultyOpinion",
            ),
            "features": _clean_list(assessment_payload.get("features"), "features"),
            "suggestions": _clean_list(
                assessment_payload.get("suggestions"),
                "suggestions",
            ),
            "satisfactionQuestion": _clean_text(
                assessment_payload.get("satisfactionQuestion"),
                "assessment.satisfactionQuestion",
            ),
        }
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

    modification_summary = payload.get("modificationSummary", "")

    if not isinstance(modification_summary, str):
        raise ValueError("modificationSummary must be a string.")

    return (
        assistant_message,
        assessment,
        proposed_rows,
        modification_summary.strip()[:1000],
        guidance,
    )


def _remove_question_paragraphs(message, follow_up_question):
    if "?" not in message and "？" not in message:
        return message

    if follow_up_question is None:
        raise ValueError("Questions must be supplied through guidance.followUpQuestion.")

    paragraphs = [part.strip() for part in message.split("\n\n")]
    declarative_paragraphs = [
        part
        for part in paragraphs
        if part and "?" not in part and "？" not in part
    ]

    if not declarative_paragraphs:
        raise ValueError(
            "assistantMessage must contain a declarative response outside its questions."
        )

    return "\n\n".join(declarative_paragraphs)


def _build_task_instructions(assessment_only):
    if assessment_only:
        return (
            "Open discussion for this newly saved Stage. Use observe_stage, include a "
            "grounded structured assessment, do not infer an intention before the "
            "designer has supplied evidence, include exactly one open follow-up question, "
            "and do not offer or generate a changed map."
        )

    return (
        "Respond adaptively. assessment should normally be null. Use offer_revision "
        "without proposedRows for an unsolicited revision idea; use deliver_revision "
        "with a complete proposedRows map only after explicit designer authorization."
    )


def _validate_guidance(payload, assessment_only):
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
    intent_confidence = payload.get("intentConfidence")

    if intent_hypothesis is None:
        if intent_confidence is not None:
            raise ValueError("intentConfidence requires intentHypothesis.")
    elif intent_confidence not in INTENT_CONFIDENCE_LEVELS:
        raise ValueError("intentConfidence is invalid.")

    follow_up_question = _clean_optional_text(
        payload.get("followUpQuestion"),
        "guidance.followUpQuestion",
    )

    if follow_up_question is not None:
        question_marks = follow_up_question.count("?") + follow_up_question.count("？")

        if question_marks != 1:
            raise ValueError("followUpQuestion must contain exactly one question.")
    proposal_offer = payload.get("proposalOffer")

    if proposal_offer is not None:
        if move != "offer_revision" or not isinstance(proposal_offer, dict):
            raise ValueError("proposalOffer requires the offer_revision move.")

        if set(proposal_offer) != {"summary", "rationale"}:
            raise ValueError("proposalOffer must contain summary and rationale.")

        proposal_offer = {
            "summary": _clean_text(proposal_offer.get("summary"), "proposalOffer.summary"),
            "rationale": _clean_text(
                proposal_offer.get("rationale"),
                "proposalOffer.rationale",
            ),
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
                "text": _clean_text(cue.get("text"), f"guidance.uiCues[{index}].text"),
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
        if follow_up_question is None:
            raise ValueError("A Stage opening requires one follow-up question.")
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

    return "\n\n".join([message, *additions])


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


def _requests_complete_map(conversation):
    latest_user_message = next(
        (
            str(message.get("content") or "").strip().casefold()
            for message in reversed(conversation)
            if message.get("role") == "user"
        ),
        "",
    )

    if not latest_user_message:
        return False

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
    )
    return any(
        marker in latest_user_message
        for marker in (*english_markers, *chinese_markers)
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
