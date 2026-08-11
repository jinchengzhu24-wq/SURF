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
CHAT_TIMEOUT_SECONDS = 25.0
CHAT_MAX_ATTEMPTS = 2
CHAT_RESPONSE_MAX_LENGTH = 4000
RETRY_DELAY_SECONDS = 0.25
PROMPT_VERSION = "cocreation-v1"

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
):
    serialized_map = "\n".join(rows)
    response_language = "Simplified Chinese" if language == "zh-CN" else "English"
    solver_metrics = solver_metrics or {}
    play_summary = play_summary or {}
    task = (
        "Assess this saved stage now. Do not propose a changed map."
        if assessment_only
        else (
            "Respond to the designer. If they clearly request a map modification, "
            "you may provide one complete proposedRows map; otherwise use null."
        )
    )
    system_prompt = (
        "You are a neutral Sokoban co-creation partner. Work only with the exact "
        "saved stage below and the supplied conversation. Treat the saved stage "
        "as read-only until the designer explicitly accepts a proposal. Help the designer form "
        "and refine their own intention without assigning a predefined purpose. "
        "Never claim a proposal has been accepted or saved; the designer must "
        "accept it in the interface. Clearly treat difficulty statements as your "
        "opinion. Deterministic solver and play evidence are authoritative for the "
        "facts they report. Do not invent play results or a verified solution. "
        f"Write all new natural-language fields in {response_language}. {task}\n\n"
        "Return JSON only with exactly these keys:\n"
        '{"assistantMessage":"...","assessment":'
        '{"solutionSummary":"...","difficultyOpinion":"...",'
        '"features":["..."],"suggestions":["..."],'
        '"satisfactionQuestion":"..."},"proposedRows":null,'
        '"modificationSummary":""}.\n'
        "When proposedRows is present it must contain exactly 10 strings of 12 "
        "characters using only space, #, ., @, p, s, and t, with one p and one or "
        "two matching s/t pairs. Keep changes focused on the designer request.\n\n"
        f"Current saved stage (12 x 10):\n{serialized_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target.\n"
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
):
    return generate_chat_reply(
        conversation,
        rows,
        request_id,
        language=language,
        solver_metrics=solver_metrics,
        play_summary=play_summary,
        assessment_only=True,
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
                assistant_message=validated[0],
                assessment=validated[1],
                proposed_rows=validated[2],
                modification_summary=validated[3],
                attempts_used=attempt,
                request_id=request_id,
                model=model,
                latency_ms=int((time.monotonic() - started_at) * 1000),
            )
        except Exception as exception:
            error = classify_exception(exception, request_id, attempt)

            if not error.retryable or attempt >= CHAT_MAX_ATTEMPTS:
                raise error from exception

            time.sleep(RETRY_DELAY_SECONDS)

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

    assistant_message = _clean_text(payload.get("assistantMessage"), "assistantMessage")
    assessment_payload = payload.get("assessment")

    if assessment_payload is None:
        assessment_payload = {
            "solutionSummary": assistant_message,
            "difficultyOpinion": assistant_message,
            "features": [assistant_message],
            "suggestions": [assistant_message],
            "satisfactionQuestion": assistant_message,
        }
    elif not isinstance(assessment_payload, dict):
        raise ValueError("assessment must be an object.")

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
    )


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
