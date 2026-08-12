import json
import os
import time
from dataclasses import dataclass, field
from threading import Lock

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)


DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
CHAT_TIMEOUT_SECONDS = 60.0
CHAT_MAX_ATTEMPTS = 1
CHAT_RESPONSE_MAX_LENGTH = 4000
PROMPT_VERSION = "cocreation-v2-guided"

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

_clients = {}
_client_lock = Lock()


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
        "and invite correction. Never store or present an inferred intention as the "
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
        f"Write all new natural-language fields in {response_language}. {task}\n\n"
        "Return JSON only with exactly these keys:\n"
        '{"assistantMessage":"...","guidance":'
        '{"move":"observe_stage","intentHypothesis":null,'
        '"intentConfidence":null,"followUpQuestion":"...",'
        '"proposalOffer":null},"assessment":null,"proposedRows":null,'
        '"modificationSummary":""}.\n'
        "guidance.move must be one of observe_stage, clarify_intent, "
        "offer_perspective, challenge_tradeoff, reflect_on_play, offer_revision, "
        "or deliver_revision. intentConfidence must be null, low, medium, or high. "
        "followUpQuestion must be null or one question. proposalOffer must be null or "
        'an object with exactly {"summary":"...","rationale":"..."}. '
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
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
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

    client = _get_client(api_key, base_url)
    messages = build_chat_messages(
        conversation,
        rows,
        language,
        solver_metrics,
        play_summary,
        assessment_only,
        stage_context,
    )
    started_at = time.monotonic()

    for attempt in range(1, CHAT_MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.45,
                stream=False,
            )
            content = str(response.choices[0].message.content or "")
            payload = json.loads(content)
            validated = validate_chat_response(payload, assessment_only)

            if validated[2] is not None and proposal_validator is not None:
                try:
                    proposal_validator(validated[2])
                except ValueError as validation_error:
                    if attempt >= CHAT_MAX_ATTEMPTS:
                        raise

                    messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "system",
                            "content": (
                                "The proposedRows map failed deterministic validation: "
                                + str(validation_error)
                                + ". Return a corrected complete JSON response."
                            ),
                        },
                    ]
                    continue

            return LLMExecutionResult(
                assistant_message=_compose_assistant_message(validated[0], validated[4]),
                assessment=validated[1],
                proposed_rows=validated[2],
                modification_summary=validated[3],
                attempts_used=attempt,
                request_id=request_id,
                model=model,
                latency_ms=int((time.monotonic() - started_at) * 1000),
                guidance=validated[4],
            )
        except Exception as exception:
            error = classify_exception(exception, request_id, attempt)

            if not error.retryable or attempt >= CHAT_MAX_ATTEMPTS:
                raise error from exception

    raise LLMServiceError(
        "INTERNAL_ERROR",
        "The LLM request ended unexpectedly.",
        request_id,
        False,
        CHAT_MAX_ATTEMPTS,
        500,
    )


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

    if "?" in assistant_message or "？" in assistant_message:
        raise ValueError("Questions must be supplied through guidance.followUpQuestion.")
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

    if set(payload) != {
        "move",
        "intentHypothesis",
        "intentConfidence",
        "followUpQuestion",
        "proposalOffer",
    }:
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

    if assessment_only:
        if move != "observe_stage":
            raise ValueError("A Stage opening must use observe_stage.")
        if follow_up_question is None:
            raise ValueError("A Stage opening requires one follow-up question.")
        if intent_hypothesis is not None or proposal_offer is not None:
            raise ValueError("A Stage opening cannot infer intention or offer a revision.")

    return {
        "move": move,
        "intentHypothesis": intent_hypothesis,
        "intentConfidence": intent_confidence,
        "followUpQuestion": follow_up_question,
        "proposalOffer": proposal_offer,
    }


def _compose_assistant_message(message, guidance):
    follow_up = guidance.get("followUpQuestion")

    if not follow_up:
        return message

    return f"{message}\n\n{follow_up}"


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


def _get_client(api_key, base_url):
    cache_key = (base_url, CHAT_TIMEOUT_SECONDS)

    with _client_lock:
        client = _clients.get(cache_key)

        if client is None:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=CHAT_TIMEOUT_SECONDS,
                max_retries=0,
            )
            _clients[cache_key] = client

        return client
