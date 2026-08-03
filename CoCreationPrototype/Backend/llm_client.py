import json
import os
import time
from dataclasses import dataclass
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
CHAT_RESPONSE_MAX_LENGTH = 2000
RETRY_DELAY_SECONDS = 0.25

_clients = {}
_client_lock = Lock()


@dataclass(frozen=True)
class LLMExecutionResult:
    assistant_message: str
    attempts_used: int
    request_id: str


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


def build_chat_messages(conversation, rows):
    serialized_map = "\n".join(rows)
    system_prompt = (
        "You are a neutral Sokoban level-design discussion partner. "
        "Discuss the fixed current map with the player, respond to their ideas, "
        "and offer grounded design observations or suggestions. The map is "
        "read-only in this prototype: never claim that you changed it, generated "
        "a replacement, or performed an action that the interface cannot perform. "
        "Do not invent a verified solution path. Ask a useful follow-up question "
        "when it helps the conversation continue. Use concise English ASCII text. "
        "Return JSON only in exactly this shape: "
        '{"assistantMessage":"your natural conversational response"}.\n\n'
        "Fixed current map (12 columns by 10 rows):\n"
        f"{serialized_map}\n\n"
        "Legend: # wall, . floor, @ water, p player, s box, t target."
    )
    return [
        {"role": "system", "content": system_prompt},
        *[
            {"role": message["role"], "content": message["content"]}
            for message in conversation
        ],
    ]


def generate_chat_reply(conversation, rows, request_id):
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
    messages = build_chat_messages(conversation, rows)

    for attempt in range(1, CHAT_MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.5,
                stream=False,
            )
            content = str(response.choices[0].message.content or "")
            payload = json.loads(content)
            assistant_message = validate_chat_response(payload)
            return LLMExecutionResult(
                assistant_message=assistant_message,
                attempts_used=attempt,
                request_id=request_id,
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


def validate_chat_response(payload):
    if not isinstance(payload, dict):
        raise ValueError("The model response must be a JSON object.")

    assistant_message = payload.get("assistantMessage")

    if not isinstance(assistant_message, str) or not assistant_message.strip():
        raise ValueError("assistantMessage must be a non-empty string.")

    assistant_message = assistant_message.strip()

    if len(assistant_message) > CHAT_RESPONSE_MAX_LENGTH:
        raise ValueError(
            f"assistantMessage must not exceed {CHAT_RESPONSE_MAX_LENGTH} characters."
        )

    if any(ord(character) > 127 for character in assistant_message):
        raise ValueError("assistantMessage must contain English ASCII text only.")

    return assistant_message


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

