from collections.abc import Callable, Iterable, Mapping
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app import create_app  # noqa: E402
from sokoban_backend.dependencies import (  # noqa: E402
    AppContainer,
    BackendSettings,
    BlueprintPlannerService,
    Clock,
    DGGuideService,
    EventLogger,
    LLMGateway,
    OnlineRoomManager,
    PCLevelGenerationService,
    StudyLogPaths,
    StudyLogStore,
    build_container,
)
from sokoban_backend.llm.runtime import (  # noqa: E402
    LLMExecutionResult,
    LLMServiceError,
    classify_llm_exception,
    new_request_id,
    normalize_ascii_punctuation,
    validate_english_only_payload,
)


class FakeClock:
    def __init__(
        self,
        initial_time: float = 1_700_000_000.0,
        initial_perf: float = 100.0,
    ) -> None:
        self.current_time = float(initial_time)
        self.current_perf = float(initial_perf)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current_time

    def perf_counter(self) -> float:
        return self.current_perf

    def sleep(self, seconds: float) -> None:
        delay = float(seconds)
        self.sleeps.append(delay)
        self.current_perf += delay

    def set_time(self, timestamp: float) -> None:
        self.current_time = float(timestamp)

    def advance(self, seconds: float) -> float:
        delta = float(seconds)
        self.current_time += delta
        self.current_perf += delta
        return self.current_time

    def utc_timestamp(self) -> str:
        return self.as_clock().utc_timestamp()

    def as_clock(self) -> Clock:
        return Clock(
            now=self.now,
            perf_counter=self.perf_counter,
            sleep=self.sleep,
        )


class _FakeCompletions:
    def __init__(self, outcomes: Iterable[Any], calls: list[dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.calls = calls

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        attempt = len(self.calls)
        if not self.outcomes:
            raise AssertionError("FakeOpenAIClient has no queued outcomes remaining")

        outcome = self.outcomes.pop(0)
        if (
            callable(outcome)
            and not isinstance(outcome, type)
            and not isinstance(outcome, BaseException)
        ):
            outcome = outcome(kwargs, attempt)

        if isinstance(outcome, BaseException):
            raise outcome

        if hasattr(outcome, "choices"):
            return outcome

        if isinstance(outcome, (dict, list)):
            content = json.dumps(outcome, ensure_ascii=False)
        else:
            content = str(outcome)

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=18),
        )


class FakeOpenAIClient:
    def __init__(
        self,
        outcomes: Iterable[Any] = (),
        *,
        api_key: str = "test-key",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 25.0,
        max_retries: int = 0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.closed = False
        self.calls: list[dict[str, Any]] = []
        self.completions = _FakeCompletions(outcomes, self.calls)
        self.chat = SimpleNamespace(completions=self.completions)

    def close(self) -> None:
        self.closed = True

    def as_factory(self) -> Callable[[str, str, float], "FakeOpenAIClient"]:
        def _factory(api_key: str, base_url: str, timeout: float) -> FakeOpenAIClient:
            self.api_key = api_key
            self.base_url = base_url
            self.timeout = timeout
            return self

        return _factory


class RecordingEventLogger(EventLogger):
    def __init__(
        self,
        settings: BackendSettings | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(
            settings=settings or BackendSettings(),
            clock=clock or Clock(),
            stream=None,
            enable_file_handler=False,
        )
        self.events: list[dict[str, Any]] = []

    def log_event(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        payload = self._build_payload(level, event, **fields)
        self.events.append(payload)
        return payload

    def find_events(self, event_name: str) -> list[dict[str, Any]]:
        return [item for item in self.events if item.get("event") == event_name]


class FakeLLMGateway(LLMGateway):
    def __init__(
        self,
        outcomes: Iterable[Any] = (),
        *,
        settings: BackendSettings | None = None,
        clock: Clock | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        resolved_settings = settings or BackendSettings(deepseek_api_key="test-key")
        resolved_clock = clock or Clock()
        resolved_logger = event_logger or RecordingEventLogger(
            settings=resolved_settings,
            clock=resolved_clock,
        )
        super().__init__(
            settings=resolved_settings,
            client_factory=FakeOpenAIClient().as_factory(),
            clock=resolved_clock,
            event_logger=resolved_logger,
        )
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def queue(self, *outcomes: Any) -> None:
        self.outcomes.extend(outcomes)

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
        resolved_request_id = new_request_id(request_id)
        call_record = {
            "task": task,
            "messages": [dict(message) for message in messages],
            "validator": validator,
            "temperature": temperature,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "thinking_mode": thinking_mode,
            "retry_error_codes": retry_error_codes,
            "request_id": resolved_request_id,
            "validation_stage": validation_stage,
        }
        self.calls.append(call_record)

        if not self.outcomes:
            raise AssertionError(
                f"FakeLLMGateway received unexpected call for task={task!r}"
            )

        outcome = self.outcomes.pop(0)
        if (
            callable(outcome)
            and not isinstance(outcome, type)
            and not isinstance(outcome, BaseException)
        ):
            result = outcome(call_record)
            if isinstance(result, LLMExecutionResult):
                return result
            outcome = result

        if isinstance(outcome, BaseException):
            raise outcome

        if isinstance(outcome, LLMExecutionResult):
            return outcome

        try:
            if isinstance(outcome, str):
                parsed = normalize_ascii_punctuation(json.loads(outcome))
                validate_english_only_payload(parsed)
            else:
                parsed = normalize_ascii_punctuation(copy.deepcopy(outcome))
                validate_english_only_payload(parsed)
            validated = validator(parsed)
        except LLMServiceError:
            raise
        except Exception as exception:
            raise classify_llm_exception(
                exception,
                request_id=resolved_request_id,
                attempts_used=1,
                validation_stage=validation_stage,
            ) from exception

        return LLMExecutionResult(validated, 1, resolved_request_id)


class RecordingBlueprintPlannerService(BlueprintPlannerService):
    def __init__(
        self,
        llm_gateway: LLMGateway,
        *,
        settings: BackendSettings | None = None,
        clock: Clock | None = None,
        event_logger: EventLogger | None = None,
        level_plan_handler: Callable[[dict[str, Any], str, int | None], LLMExecutionResult] | None = None,
        clarity_check_handler: Callable[[str, str], LLMExecutionResult] | None = None,
    ) -> None:
        super().__init__(
            llm_gateway,
            settings=settings,
            clock=clock,
            event_logger=event_logger,
        )
        self.level_plan_handler = level_plan_handler
        self.clarity_check_handler = clarity_check_handler
        self.level_plan_calls: list[tuple[dict[str, Any], str, int | None]] = []
        self.clarity_check_calls: list[tuple[str, str]] = []

    def create_level_plan(
        self,
        creative_context: dict[str, Any] | None = None,
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        copied = copy.deepcopy(creative_context or {})
        self.level_plan_calls.append((copied, request_id, max_attempts))
        if self.level_plan_handler is not None:
            return self.level_plan_handler(copied, request_id, max_attempts)
        return super().create_level_plan(
            creative_context=creative_context,
            request_id=request_id,
            max_attempts=max_attempts,
        )

    def create_human_adjustment_clarity_check(
        self,
        adjustment_text: str,
        request_id: str = "",
        max_attempts: int | None = None,
    ) -> LLMExecutionResult:
        self.clarity_check_calls.append((adjustment_text, request_id))
        if self.clarity_check_handler is not None:
            return self.clarity_check_handler(adjustment_text, request_id)
        return super().create_human_adjustment_clarity_check(
            adjustment_text=adjustment_text,
            request_id=request_id,
            max_attempts=max_attempts,
        )


def create_test_settings(
    tmp_dir: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    **field_overrides: Any,
) -> BackendSettings:
    base_dir = Path(tmp_dir)
    if env is not None:
        env_map = dict(env)
        env_map.setdefault(
            "BACKEND_OPERATIONS_LOG_FILE",
            str(base_dir / "logs" / "backend.log"),
        )
        settings = BackendSettings.from_env(env_map)
    else:
        settings = BackendSettings(
            deepseek_api_key="test-api-key",
            operations_log_file=base_dir / "logs" / "backend.log",
        )
    if field_overrides:
        settings = replace(settings, **field_overrides)
    return settings


_SETTINGS_FIELD_NAMES = {
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model",
    "llm_timeout_seconds",
    "plan_llm_timeout_seconds",
    "llm_max_attempts",
    "llm_retry_delay_seconds",
    "llm_response_excerpt_chars",
    "deepseek_temperature",
    "deepseek_expansion_temperature",
    "deepseek_level_temperature",
    "deepseek_pc_level_temperature",
    "deepseek_pc_level_timeout_seconds",
    "dashboard_delete_password",
    "cocreation_intention_sync_secret",
    "pc_layout_prefilter_max_search_states",
    "pc_layout_prefilter_seconds",
    "online_room_ttl_seconds",
    "allowed_origins",
    "operations_log_file",
    "log_level",
}


def create_test_container(tmp_dir: Path | str, **overrides: Any) -> AppContainer:
    base_dir = Path(tmp_dir)
    env = overrides.pop("env", None)
    llm_outcomes = overrides.pop("llm_outcomes", ())
    settings_fields = {
        key: overrides.pop(key)
        for key in list(overrides)
        if key in _SETTINGS_FIELD_NAMES
    }

    settings: BackendSettings = overrides.pop("settings", None) or create_test_settings(
        base_dir,
        env=env,
        **settings_fields,
    )
    if settings_fields and "settings" not in overrides:
        settings = replace(settings, **settings_fields)

    raw_clock = overrides.pop("clock", None)
    if isinstance(raw_clock, FakeClock):
        resolved_clock = raw_clock.as_clock()
    elif isinstance(raw_clock, Clock):
        resolved_clock = raw_clock
    else:
        resolved_clock = Clock()

    study_log_dir = overrides.pop("study_log_dir", None)
    paths: StudyLogPaths = overrides.pop("paths", None) or StudyLogPaths(
        Path(study_log_dir) if study_log_dir is not None else base_dir / "study_logs"
    )
    event_logger: EventLogger = overrides.pop("event_logger", None) or RecordingEventLogger(
        settings=settings,
        clock=resolved_clock,
    )
    store: StudyLogStore = overrides.pop("store", None) or StudyLogStore(
        paths=paths,
        clock=resolved_clock,
    )
    room_manager: OnlineRoomManager = overrides.pop("room_manager", None) or OnlineRoomManager(
        store=store,
        settings=settings,
        clock=resolved_clock,
    )

    client_factory = overrides.pop("client_factory", None)
    llm_gateway = overrides.pop("llm_gateway", None)
    if llm_gateway is None:
        if client_factory is not None:
            llm_gateway = LLMGateway(
                settings=settings,
                client_factory=client_factory,
                clock=resolved_clock,
                event_logger=event_logger,
            )
        else:
            llm_gateway = FakeLLMGateway(
                outcomes=llm_outcomes,
                settings=settings,
                clock=resolved_clock,
                event_logger=event_logger,
            )

    dg_service: DGGuideService = overrides.pop("dg_service", None) or DGGuideService(
        llm_gateway,
        settings=settings,
    )
    blueprint_service: BlueprintPlannerService = overrides.pop(
        "blueprint_service",
        None,
    ) or BlueprintPlannerService(
        llm_gateway,
        settings=settings,
        clock=resolved_clock,
        event_logger=event_logger,
    )
    pc_service: PCLevelGenerationService = overrides.pop(
        "pc_service",
        None,
    ) or PCLevelGenerationService(
        blueprint_service,
        settings,
        llm_gateway=llm_gateway,
        clock=resolved_clock,
        event_logger=event_logger,
    )

    if overrides:
        unexpected = ", ".join(sorted(overrides))
        raise TypeError(f"Unexpected create_test_container overrides: {unexpected}")

    return build_container(
        settings=settings,
        paths=paths,
        clock=resolved_clock,
        event_logger=event_logger,
        store=store,
        room_manager=room_manager,
        llm_gateway=llm_gateway,
        dg_service=dg_service,
        blueprint_service=blueprint_service,
        pc_service=pc_service,
    )


def create_test_app(tmp_dir: Path | str, **overrides: Any):
    container = overrides.pop("container", None)
    if container is None:
        container = create_test_container(tmp_dir, **overrides)
    return create_app(container)
