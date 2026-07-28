import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError


BASE_DIR = Path(__file__).resolve().parent
OPERATIONS_LOG_DIR = BASE_DIR / "logs"
OPERATIONS_LOG_FILE = OPERATIONS_LOG_DIR / "backend.log"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_RESPONSE_EXCERPT_CHARS = 1000
DEFAULT_RETRY_DELAY_SECONDS = 0.5

_client_lock = threading.Lock()
_clients = {}


def _build_logger():
    logger = logging.getLogger("sokoban.backend")

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, os.getenv("BACKEND_LOG_LEVEL", "INFO").upper(), logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter("%(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        OPERATIONS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            OPERATIONS_LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exception:
        stream_handler.handle(
            logging.LogRecord(
                logger.name,
                logging.ERROR,
                __file__,
                0,
                json.dumps(
                    {
                        "timestamp": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(),
                        ),
                        "level": "ERROR",
                        "event": "operations_log_unavailable",
                        "exceptionType": type(exception).__name__,
                        "errorMessage": str(exception),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                (),
                None,
            )
        )

    return logger


LOGGER = _build_logger()


def new_request_id(value=""):
    candidate = "".join(
        character
        for character in str(value or "").strip()
        if character.isalnum() or character in {"-", "_"}
    )[:64]
    return candidate or uuid.uuid4().hex


def log_event(level, event, **fields):
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": str(level or "INFO").upper(),
        "event": str(event or "backend-event"),
    }
    payload.update(
        {
            key: value
            for key, value in fields.items()
            if value is not None and value != ""
        }
    )
    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    log_method = getattr(LOGGER, payload["level"].lower(), LOGGER.info)
    log_method(message)


def safe_log_text(value, limit=1000):
    text = str(value or "")
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if api_key:
        text = text.replace(api_key, "[redacted-api-key]")

    text = re.sub(r"https?://[^\s\"']+", "[redacted-url]", text)
    return text[: max(0, int(limit))]


@dataclass
class LLMExecutionResult:
    value: object
    attempts_used: int
    request_id: str


class LLMServiceError(Exception):
    def __init__(
        self,
        code,
        stage,
        message,
        request_id,
        retryable,
        attempts_used,
        status_code,
    ):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.safe_message = message
        self.request_id = request_id
        self.retryable = bool(retryable)
        self.attempts_used = int(attempts_used)
        self.status_code = int(status_code)

    def to_detail(self):
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.safe_message,
            "requestId": self.request_id,
            "retryable": self.retryable,
            "attemptsUsed": self.attempts_used,
        }


def validate_english_only_payload(value, path="$"):
    if isinstance(value, str):
        invalid_character = next(
            (
                character
                for character in value
                if ord(character) > 127
                or (ord(character) < 32 and character not in {"\t", "\n", "\r"})
            ),
            None,
        )

        if invalid_character is not None:
            raise ValueError(
                "Every LLM JSON string value must use English ASCII text only; "
                f"non-English or unsupported characters were found at {path}"
            )

        return

    if isinstance(value, dict):
        for key, child in value.items():
            validate_english_only_payload(child, f"{path}.{key}")
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            validate_english_only_payload(child, f"{path}[{index}]")


def execute_json_request(
    *,
    task,
    messages,
    validator,
    temperature,
    timeout_seconds,
    max_attempts=2,
    request_id="",
    validation_stage="model_validation",
):
    request_id = new_request_id(request_id)
    max_attempts = max(1, min(2, int(max_attempts or 1)))
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        error = LLMServiceError(
            "CONFIGURATION_ERROR",
            "configuration",
            "DEEPSEEK_API_KEY is missing.",
            request_id,
            False,
            0,
            503,
        )
        log_event(
            "ERROR",
            "llm_request_failed",
            requestId=request_id,
            task=task,
            stage=error.stage,
            errorCode=error.code,
            attemptsUsed=0,
        )
        raise error

    client = _get_client(api_key, base_url, timeout_seconds)
    current_messages = [dict(message) for message in messages]
    overall_started = time.perf_counter()

    for attempt in range(1, max_attempts + 1):
        attempt_started = time.perf_counter()
        content = ""

        try:
            log_event(
                "INFO",
                "llm_attempt_started",
                requestId=request_id,
                task=task,
                model=model,
                attempt=attempt,
                maxAttempts=max_attempts,
                timeoutSeconds=timeout_seconds,
            )
            response = client.chat.completions.create(
                model=model,
                messages=current_messages,
                response_format={"type": "json_object"},
                temperature=temperature,
                stream=False,
            )
            content = str(response.choices[0].message.content or "")
            parsed = json.loads(content)
            validate_english_only_payload(parsed)
            value = validator(parsed)
            elapsed_ms = round((time.perf_counter() - overall_started) * 1000)
            usage = getattr(response, "usage", None)
            log_event(
                "INFO",
                "llm_request_succeeded",
                requestId=request_id,
                task=task,
                model=model,
                attempt=attempt,
                attemptsUsed=attempt,
                elapsedMs=elapsed_ms,
                promptTokens=getattr(usage, "prompt_tokens", None),
                completionTokens=getattr(usage, "completion_tokens", None),
            )
            return LLMExecutionResult(value, attempt, request_id)
        except Exception as exception:
            error = _classify_exception(
                exception,
                request_id=request_id,
                attempts_used=attempt,
                validation_stage=validation_stage,
            )
            excerpt = _response_excerpt(content) if content else ""
            log_event(
                "ERROR",
                "llm_attempt_failed",
                requestId=request_id,
                task=task,
                model=model,
                attempt=attempt,
                maxAttempts=max_attempts,
                elapsedMs=round((time.perf_counter() - attempt_started) * 1000),
                stage=error.stage,
                errorCode=error.code,
                retryable=error.retryable,
                exceptionType=type(exception).__name__,
                errorMessage=safe_log_text(exception),
                responseExcerpt=excerpt,
                responseHash=_response_hash(content) if content else "",
                traceback=safe_log_text(traceback.format_exc()[-4000:], 4000),
            )

            if not error.retryable or attempt >= max_attempts:
                raise error from exception

            if error.code in {"MODEL_JSON_INVALID", "MODEL_VALIDATION_FAILED"}:
                current_messages = _build_repair_messages(
                    messages,
                    content,
                    error.safe_message,
                )

            time.sleep(DEFAULT_RETRY_DELAY_SECONDS)

    raise LLMServiceError(
        "INTERNAL_ERROR",
        "llm_runtime",
        "The LLM request ended unexpectedly.",
        request_id,
        False,
        max_attempts,
        500,
    )


def readiness_payload():
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    configured = bool(api_key and api_key != "your_deepseek_api_key_here")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    model_configured = bool(model)
    writable = False

    try:
        OPERATIONS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(OPERATIONS_LOG_DIR, os.W_OK)
    except OSError:
        writable = False

    return {
        "status": (
            "ready"
            if configured and model_configured and writable
            else "not-ready"
        ),
        "apiKeyConfigured": configured,
        "modelConfigured": model_configured,
        "model": model,
        "logDirectoryWritable": writable,
        "singleWorkerRequired": True,
    }


def _get_client(api_key, base_url, timeout_seconds):
    key = (base_url, float(timeout_seconds))

    with _client_lock:
        client = _clients.get(key)

        if client is None:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=float(timeout_seconds),
                max_retries=0,
            )
            _clients[key] = client

        return client


def _classify_exception(exception, request_id, attempts_used, validation_stage):
    if isinstance(exception, APITimeoutError):
        return LLMServiceError(
            "UPSTREAM_TIMEOUT",
            "deepseek_request",
            "DeepSeek did not respond before the attempt timeout.",
            request_id,
            True,
            attempts_used,
            504,
        )

    if isinstance(exception, RateLimitError):
        return LLMServiceError(
            "UPSTREAM_RATE_LIMIT",
            "deepseek_request",
            "DeepSeek rate limited the request.",
            request_id,
            True,
            attempts_used,
            503,
        )

    if isinstance(exception, APIConnectionError):
        return LLMServiceError(
            "UPSTREAM_CONNECTION_ERROR",
            "deepseek_request",
            "The backend could not connect to DeepSeek.",
            request_id,
            True,
            attempts_used,
            502,
        )

    if isinstance(exception, APIStatusError):
        upstream_status = int(getattr(exception, "status_code", 0) or 0)

        if upstream_status in {401, 403}:
            return LLMServiceError(
                "UPSTREAM_AUTHENTICATION_FAILED",
                "deepseek_authentication",
                "DeepSeek rejected the configured credentials.",
                request_id,
                False,
                attempts_used,
                503,
            )

        retryable = upstream_status == 429 or upstream_status >= 500
        code = "UPSTREAM_SERVER_ERROR" if retryable else "UPSTREAM_REQUEST_REJECTED"
        status_code = 503 if upstream_status == 429 else 502
        return LLMServiceError(
            code,
            "deepseek_request",
            f"DeepSeek returned HTTP {upstream_status or 'error'}.",
            request_id,
            retryable,
            attempts_used,
            status_code,
        )

    if isinstance(exception, json.JSONDecodeError):
        return LLMServiceError(
            "MODEL_JSON_INVALID",
            "json_parse",
            f"Model output was not valid JSON: {exception.msg}.",
            request_id,
            True,
            attempts_used,
            502,
        )

    if isinstance(exception, (ValueError, TypeError, KeyError)):
        return LLMServiceError(
            "MODEL_VALIDATION_FAILED",
            validation_stage,
            str(exception)[:1000] or "Model output failed validation.",
            request_id,
            True,
            attempts_used,
            502,
        )

    return LLMServiceError(
        "INTERNAL_ERROR",
        "internal",
        f"Unexpected backend error: {type(exception).__name__}.",
        request_id,
        False,
        attempts_used,
        500,
    )


def _build_repair_messages(original_messages, content, validation_error):
    repair_prompt = (
        "Your previous JSON response failed validation. Correct only the invalid "
        "format or fields while preserving the requested intent. Return JSON only. "
        f"Validation error: {str(validation_error)[:1000]}"
    )
    repaired = [dict(message) for message in original_messages]

    if content:
        repaired.append({"role": "assistant", "content": content})

    repaired.append({"role": "user", "content": repair_prompt})
    return repaired


def _response_excerpt(content):
    limit_text = os.getenv(
        "LLM_LOG_RESPONSE_EXCERPT_CHARS",
        str(DEFAULT_RESPONSE_EXCERPT_CHARS),
    )

    try:
        limit = max(0, min(4000, int(limit_text)))
    except ValueError:
        limit = DEFAULT_RESPONSE_EXCERPT_CHARS

    return str(content or "")[:limit]


def _response_hash(content):
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:16]
