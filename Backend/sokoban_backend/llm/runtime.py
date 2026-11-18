from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import threading
import traceback
from typing import Any
import uuid

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from ..config import (
    BackendSettings,
    Clock,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_LLM_RESPONSE_EXCERPT_CHARS,
    DEFAULT_LLM_RETRY_DELAY_SECONDS,
)


DEFAULT_MODEL = DEFAULT_DEEPSEEK_MODEL
DEFAULT_BASE_URL = DEFAULT_DEEPSEEK_BASE_URL
DEFAULT_RESPONSE_EXCERPT_CHARS = DEFAULT_LLM_RESPONSE_EXCERPT_CHARS
DEFAULT_RETRY_DELAY_SECONDS = DEFAULT_LLM_RETRY_DELAY_SECONDS
MAX_CACHED_CLIENTS = 16
ASCII_PUNCTUATION_TRANSLATION = str.maketrans({
    "\u00a0": " ",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u2022": "-",
})

_DEFAULT_STREAM = object()


def new_request_id(value: object = "") -> str:
    candidate = "".join(
        character
        for character in str(value or "").strip()
        if character.isalnum() or character in {"-", "_"}
    )[:64]
    return candidate or uuid.uuid4().hex


def redact_log_text(value: object, limit: int = 1000, api_key: str = "") -> str:
    text = str(value or "")
    clean_key = str(api_key or "").strip()
    if clean_key:
        text = text.replace(clean_key, "[redacted-api-key]")
    text = re.sub(r"https?://[^\s\"']+", "[redacted-url]", text)
    return text[: max(0, int(limit))]


class EventLogger:
    def __init__(
        self,
        settings: BackendSettings,
        clock: Clock | None = None,
        *,
        stream: Any = _DEFAULT_STREAM,
        enable_file_handler: bool = True,
    ) -> None:
        self.settings = settings
        self.clock = clock or Clock()
        self._stream = stream
        self._enable_file_handler = bool(enable_file_handler)
        self._lock = threading.Lock()
        self._logger: logging.Logger | None = None

    def _get_logger(self) -> logging.Logger:
        with self._lock:
            if self._logger is not None:
                return self._logger

            logger = logging.Logger(f"sokoban.backend.{id(self)}")
            logger.setLevel(
                getattr(
                    logging,
                    str(self.settings.log_level or "INFO").upper(),
                    logging.INFO,
                )
            )
            logger.propagate = False
            formatter = logging.Formatter("%(message)s")

            stream_handler = None
            target_stream = sys.stdout if self._stream is _DEFAULT_STREAM else self._stream
            if target_stream is not None:
                stream_handler = logging.StreamHandler(target_stream)
                stream_handler.setFormatter(formatter)
                logger.addHandler(stream_handler)

            if self._enable_file_handler and self.settings.operations_log_file is not None:
                try:
                    self.settings.operations_log_file.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    file_handler = RotatingFileHandler(
                        self.settings.operations_log_file,
                        maxBytes=5 * 1024 * 1024,
                        backupCount=5,
                        encoding="utf-8",
                    )
                    file_handler.setFormatter(formatter)
                    logger.addHandler(file_handler)
                except OSError as exception:
                    if stream_handler is not None:
                        stream_handler.handle(
                            logging.LogRecord(
                                logger.name,
                                logging.ERROR,
                                __file__,
                                0,
                                json.dumps(
                                    {
                                        "timestamp": self.clock.utc_timestamp(),
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

            self._logger = logger
            return logger

    def safe_log_text(self, value: object, limit: int = 1000) -> str:
        return redact_log_text(
            value,
            limit=limit,
            api_key=self.settings.deepseek_api_key,
        )

    def _build_payload(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": self.clock.utc_timestamp(),
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
        return payload

    def log_event(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        payload = self._build_payload(level, event, **fields)
        logger = self._get_logger()
        if logger.handlers:
            message = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            log_method = getattr(logger, payload["level"].lower(), logger.info)
            log_method(message)
        return payload

    def close(self) -> None:
        with self._lock:
            if self._logger is None:
                return
            for handler in list(self._logger.handlers):
                self._logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
            self._logger = None


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


def normalize_ascii_punctuation(value):
    """Normalize common typography while preserving the ASCII-only contract."""
    if isinstance(value, str):
        return value.translate(ASCII_PUNCTUATION_TRANSLATION)
    if isinstance(value, dict):
        return {key: normalize_ascii_punctuation(child) for key, child in value.items()}
    if isinstance(value, list):
        return [normalize_ascii_punctuation(child) for child in value]
    return value


def default_openai_client_factory(
    api_key: str,
    base_url: str,
    timeout_seconds: float,
) -> Any:
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=float(timeout_seconds),
        max_retries=0,
    )


def classify_llm_exception(exception, request_id, attempts_used, validation_stage):
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

    if isinstance(exception, (ValueError, TypeError, KeyError, IndexError, AttributeError)):
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


_classify_exception = classify_llm_exception


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


def _response_excerpt(content: object, limit: int = DEFAULT_RESPONSE_EXCERPT_CHARS) -> str:
    clamped = max(0, min(4000, int(limit)))
    return str(content or "")[:clamped]


def _response_hash(content: object) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:16]


class LLMGateway:
    def __init__(
        self,
        settings: BackendSettings,
        client_factory: Callable[[str, str, float], Any] = default_openai_client_factory,
        clock: Clock | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory
        self.clock = clock or Clock()
        self.event_logger = event_logger or EventLogger(
            settings=settings,
            clock=self.clock,
        )
        self._client_lock = threading.Lock()
        self._clients: OrderedDict[tuple[str, str, float], Any] = OrderedDict()

    @property
    def cached_client_count(self) -> int:
        with self._client_lock:
            return len(self._clients)

    def get_client(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
    ) -> Any:
        key = (str(api_key or ""), str(base_url or ""), float(timeout_seconds))

        with self._client_lock:
            client = self._clients.get(key)
            if client is not None:
                self._clients.move_to_end(key)
                return client

            client = self.client_factory(
                api_key,
                base_url,
                float(timeout_seconds),
            )
            self._clients[key] = client
            while len(self._clients) > MAX_CACHED_CLIENTS:
                _, evicted = self._clients.popitem(last=False)
                close = getattr(evicted, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

            return client

    def close(self) -> None:
        with self._client_lock:
            while self._clients:
                _, client = self._clients.popitem(last=False)
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    def execute_json_request(
        self,
        *,
        task: str,
        messages: list[dict[str, Any]],
        validator: Callable[[Any], Any],
        temperature: float,
        timeout_seconds: float,
        max_attempts: int = 2,
        thinking_mode: str | None = None,
        retry_error_codes: set[str] | None = None,
        request_id: str = "",
        validation_stage: str = "model_validation",
    ) -> LLMExecutionResult:
        request_id = new_request_id(request_id)
        max_attempts = max(1, min(2, int(max_attempts or 1)))
        allowed_retry_codes = (
            None if retry_error_codes is None else set(retry_error_codes)
        )
        model = str(self.settings.deepseek_model or "").strip() or DEFAULT_MODEL
        base_url = str(self.settings.deepseek_base_url or "").strip() or DEFAULT_BASE_URL
        api_key = str(self.settings.deepseek_api_key or "").strip()

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
            self.event_logger.log_event(
                "ERROR",
                "llm_request_failed",
                requestId=request_id,
                task=task,
                stage=error.stage,
                errorCode=error.code,
                attemptsUsed=0,
            )
            raise error

        client = self.get_client(api_key, base_url, timeout_seconds)
        current_messages = [dict(message) for message in messages]
        overall_started = self.clock.perf_counter()

        for attempt in range(1, max_attempts + 1):
            attempt_started = self.clock.perf_counter()
            content = ""

            try:
                self.event_logger.log_event(
                    "INFO",
                    "llm_attempt_started",
                    requestId=request_id,
                    task=task,
                    model=model,
                    attempt=attempt,
                    maxAttempts=max_attempts,
                    timeoutSeconds=timeout_seconds,
                )
                request_options: dict[str, Any] = {
                    "model": model,
                    "messages": current_messages,
                    "response_format": {"type": "json_object"},
                    "temperature": temperature,
                    "stream": False,
                }

                if thinking_mode in {"enabled", "disabled"}:
                    request_options["extra_body"] = {
                        "thinking": {"type": thinking_mode}
                    }

                response = client.chat.completions.create(**request_options)
                choices = getattr(response, "choices", None) or []
                if not choices:
                    raise ValueError("Model returned no completion choices.")
                message = getattr(choices[0], "message", None)
                content = str(getattr(message, "content", None) or "")
                parsed = normalize_ascii_punctuation(json.loads(content))
                if not isinstance(parsed, (dict, list)):
                    raise ValueError("Model JSON root must be an object or array.")
                validate_english_only_payload(parsed)
                value = validator(parsed)
                elapsed_ms = round((self.clock.perf_counter() - overall_started) * 1000)
                usage = getattr(response, "usage", None)
                self.event_logger.log_event(
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
                error = classify_llm_exception(
                    exception,
                    request_id=request_id,
                    attempts_used=attempt,
                    validation_stage=validation_stage,
                )
                excerpt = (
                    _response_excerpt(
                        content,
                        self.settings.llm_response_excerpt_chars,
                    )
                    if content
                    else ""
                )
                self.event_logger.log_event(
                    "ERROR",
                    "llm_attempt_failed",
                    requestId=request_id,
                    task=task,
                    model=model,
                    attempt=attempt,
                    maxAttempts=max_attempts,
                    elapsedMs=round((self.clock.perf_counter() - attempt_started) * 1000),
                    stage=error.stage,
                    errorCode=error.code,
                    retryable=error.retryable,
                    exceptionType=type(exception).__name__,
                    errorMessage=self.event_logger.safe_log_text(exception),
                    responseExcerpt=excerpt,
                    responseHash=_response_hash(content) if content else "",
                    traceback=self.event_logger.safe_log_text(
                        traceback.format_exc()[-4000:],
                        4000,
                    ),
                )

                retry_is_allowed = (
                    allowed_retry_codes is None
                    or error.code in allowed_retry_codes
                )

                if (
                    not error.retryable
                    or not retry_is_allowed
                    or attempt >= max_attempts
                ):
                    raise error from exception

                if error.code in {"MODEL_JSON_INVALID", "MODEL_VALIDATION_FAILED"}:
                    current_messages = _build_repair_messages(
                        messages,
                        content,
                        error.safe_message,
                    )

                self.clock.sleep(self.settings.llm_retry_delay_seconds)

        raise LLMServiceError(
            "INTERNAL_ERROR",
            "llm_runtime",
            "The LLM request ended unexpectedly.",
            request_id,
            False,
            max_attempts,
            500,
        )

    def readiness_payload(self) -> dict[str, Any]:
        api_key = str(self.settings.deepseek_api_key or "").strip()
        configured = bool(api_key and api_key != "your_deepseek_api_key_here")
        model = str(self.settings.deepseek_model or "").strip() or DEFAULT_MODEL
        model_configured = bool(model)
        writable = False

        try:
            log_dir = self.settings.operations_log_file.parent
            log_dir.mkdir(parents=True, exist_ok=True)
            writable = os.access(log_dir, os.W_OK)
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
