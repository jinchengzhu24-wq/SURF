from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import time

from dotenv import load_dotenv
from fastapi import HTTPException


HOST = "127.0.0.1"
PORT = 8000
START_URL = f"http://{HOST}:{PORT}/generate-level-plan"
SHORT_LLM_TIMEOUT_SECONDS = 25.0
PLAN_LLM_TIMEOUT_SECONDS = 45.0
PC_LEVEL_LLM_TIMEOUT_SECONDS = 15.0
PC_FEASIBILITY_MAX_SEARCH_STATES = 120000
PC_DESIGN_MIN_ACTIVITY_AREA = 56
PC_PRIMARY_INTERNAL_WALLS = 5
PC_INTERMEDIATE_INTERNAL_WALLS = 4
PC_FALLBACK_MIN_INTERNAL_WALLS = 3
PC_MAX_WATER_AREA_CANDIDATES = 12
PC_LAYOUT_MAX_WATER_CHECKS = 6
PC_MAX_LAYOUT_CANDIDATES = 6
PC_LAYOUT_PREFILTER_SECONDS = 3.0
PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES = 300000
DEFAULT_LLM_MAX_ATTEMPTS = 2
DEFAULT_LLM_RETRY_DELAY_SECONDS = 0.5
DEFAULT_LLM_RESPONSE_EXCERPT_CHARS = 1000
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "Frontend"
WEBGL_BUILD_DIR = PROJECT_DIR / "WebGLBuild"
STUDY_LOG_DIR = BASE_DIR / "study_logs"
STUDY_LOG_FILE = STUDY_LOG_DIR / "level_records.jsonl"
SURVEY_LOG_FILE = STUDY_LOG_DIR / "survey_responses.jsonl"
CREATIVE_IDEA_LOG_FILE = STUDY_LOG_DIR / "creative_ideas.jsonl"
CREATIVE_EXPANSION_CHOICE_LOG_FILE = STUDY_LOG_DIR / "creative_expansion_choices.jsonl"
HA_PLAN_EVENT_LOG_FILE = STUDY_LOG_DIR / "ha_plan_events.jsonl"
JOURNEY_EVENT_LOG_FILE = STUDY_LOG_DIR / "journey_events.jsonl"
ONLINE_MATCH_LOG_FILE = STUDY_LOG_DIR / "online_match_events.jsonl"
ONLINE_MATCH_FLOW_DIR = STUDY_LOG_DIR / "matches"
OPERATIONS_LOG_DIR = BASE_DIR / "logs"
OPERATIONS_LOG_FILE = OPERATIONS_LOG_DIR / "backend.log"

ONLINE_ROOM_TTL_SECONDS = 30 * 60
ONLINE_ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ONLINE_ROOM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6}$")
ONLINE_AI_ASSISTANT_MODES = {
    "description_generation",
}
AGENT_HANDOFF_SCHEMA_VERSION = 1

load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Clock:
    now: Callable[[], float] = time.time
    perf_counter: Callable[[], float] = time.perf_counter
    sleep: Callable[[float], None] = time.sleep

    def utc_timestamp(self) -> str:
        return datetime.fromtimestamp(
            float(self.now()),
            tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class StudyLogPaths:
    study_log_dir: Path
    study_log_file: Path
    survey_log_file: Path
    creative_idea_log_file: Path
    creative_expansion_choice_log_file: Path
    ha_plan_event_log_file: Path
    journey_event_log_file: Path
    online_match_log_file: Path
    online_match_flow_dir: Path

    def __init__(
        self,
        study_log_dir: Path | str,
        *,
        study_log_file: Path | str | None = None,
        survey_log_file: Path | str | None = None,
        creative_idea_log_file: Path | str | None = None,
        creative_expansion_choice_log_file: Path | str | None = None,
        ha_plan_event_log_file: Path | str | None = None,
        journey_event_log_file: Path | str | None = None,
        online_match_log_file: Path | str | None = None,
        online_match_flow_dir: Path | str | None = None,
    ) -> None:
        base = Path(study_log_dir)
        object.__setattr__(self, "study_log_dir", base)
        object.__setattr__(
            self,
            "study_log_file",
            Path(study_log_file) if study_log_file is not None else base / "level_records.jsonl",
        )
        object.__setattr__(
            self,
            "survey_log_file",
            Path(survey_log_file) if survey_log_file is not None else base / "survey_responses.jsonl",
        )
        object.__setattr__(
            self,
            "creative_idea_log_file",
            Path(creative_idea_log_file) if creative_idea_log_file is not None else base / "creative_ideas.jsonl",
        )
        object.__setattr__(
            self,
            "creative_expansion_choice_log_file",
            Path(creative_expansion_choice_log_file)
            if creative_expansion_choice_log_file is not None
            else base / "creative_expansion_choices.jsonl",
        )
        object.__setattr__(
            self,
            "ha_plan_event_log_file",
            Path(ha_plan_event_log_file) if ha_plan_event_log_file is not None else base / "ha_plan_events.jsonl",
        )
        object.__setattr__(
            self,
            "journey_event_log_file",
            Path(journey_event_log_file) if journey_event_log_file is not None else base / "journey_events.jsonl",
        )
        object.__setattr__(
            self,
            "online_match_log_file",
            Path(online_match_log_file)
            if online_match_log_file is not None
            else base / "online_match_events.jsonl",
        )
        object.__setattr__(
            self,
            "online_match_flow_dir",
            Path(online_match_flow_dir) if online_match_flow_dir is not None else base / "matches",
        )


def env_float(
    name: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> float:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except ValueError:
            value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def env_int(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        value = int(default)
    else:
        try:
            value = int(str(raw).strip())
        except ValueError:
            value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _parse_allowed_origins(raw: str | None) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return ("*",)
    items = tuple(item.strip() for item in str(raw).split(",") if item.strip())
    return items or ("*",)


@dataclass(frozen=True)
class BackendSettings:
    deepseek_api_key: str = ""
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL
    llm_timeout_seconds: float = SHORT_LLM_TIMEOUT_SECONDS
    plan_llm_timeout_seconds: float = PLAN_LLM_TIMEOUT_SECONDS
    llm_max_attempts: int = DEFAULT_LLM_MAX_ATTEMPTS
    llm_retry_delay_seconds: float = DEFAULT_LLM_RETRY_DELAY_SECONDS
    llm_response_excerpt_chars: int = DEFAULT_LLM_RESPONSE_EXCERPT_CHARS
    deepseek_temperature: float = 0.9
    deepseek_expansion_temperature: float = 0.9
    deepseek_level_temperature: float = 0.5
    deepseek_pc_level_temperature: float = 0.15
    deepseek_pc_level_timeout_seconds: float = PC_LEVEL_LLM_TIMEOUT_SECONDS
    dashboard_delete_password: str = ""
    cocreation_intention_sync_secret: str = ""
    pc_layout_prefilter_max_search_states: int = PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES
    pc_layout_prefilter_seconds: float = PC_LAYOUT_PREFILTER_SECONDS
    online_room_ttl_seconds: float = float(ONLINE_ROOM_TTL_SECONDS)
    allowed_origins: tuple[str, ...] = ("*",)
    operations_log_file: Path = OPERATIONS_LOG_FILE
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BackendSettings":
        source = os.environ if env is None else env
        base_temp = env_float("DEEPSEEK_TEMPERATURE", 0.9, 0.0, 2.0, env=source)
        expansion_temp = env_float(
            "DEEPSEEK_EXPANSION_TEMPERATURE",
            base_temp,
            0.0,
            2.0,
            env=source,
        )
        level_temp = env_float("DEEPSEEK_LEVEL_TEMPERATURE", 0.5, 0.0, 2.0, env=source)
        pc_temp = env_float("DEEPSEEK_PC_LEVEL_TEMPERATURE", 0.15, 0.0, 2.0, env=source)
        pc_timeout = env_float(
            "DEEPSEEK_PC_LEVEL_TIMEOUT_SECONDS",
            PC_LEVEL_LLM_TIMEOUT_SECONDS,
            0.5,
            120.0,
            env=source,
        )
        raw_log_file = str(source.get("BACKEND_OPERATIONS_LOG_FILE") or "").strip()
        operations_log_file = Path(raw_log_file) if raw_log_file else OPERATIONS_LOG_FILE
        raw_origins = source.get("BACKEND_ALLOWED_ORIGINS") or source.get("ALLOWED_ORIGINS")
        return cls(
            deepseek_api_key=str(source.get("DEEPSEEK_API_KEY") or "").strip(),
            deepseek_base_url=(
                str(source.get("DEEPSEEK_BASE_URL") or "").strip()
                or DEFAULT_DEEPSEEK_BASE_URL
            ),
            deepseek_model=(
                str(source.get("DEEPSEEK_MODEL") or "").strip()
                or DEFAULT_DEEPSEEK_MODEL
            ),
            llm_timeout_seconds=env_float(
                "LLM_TIMEOUT_SECONDS",
                SHORT_LLM_TIMEOUT_SECONDS,
                0.5,
                300.0,
                env=source,
            ),
            plan_llm_timeout_seconds=env_float(
                "PLAN_LLM_TIMEOUT_SECONDS",
                PLAN_LLM_TIMEOUT_SECONDS,
                0.5,
                300.0,
                env=source,
            ),
            llm_max_attempts=env_int(
                "LLM_MAX_ATTEMPTS",
                DEFAULT_LLM_MAX_ATTEMPTS,
                1,
                2,
                env=source,
            ),
            llm_retry_delay_seconds=env_float(
                "LLM_RETRY_DELAY_SECONDS",
                DEFAULT_LLM_RETRY_DELAY_SECONDS,
                0.0,
                30.0,
                env=source,
            ),
            llm_response_excerpt_chars=env_int(
                "LLM_LOG_RESPONSE_EXCERPT_CHARS",
                DEFAULT_LLM_RESPONSE_EXCERPT_CHARS,
                0,
                4000,
                env=source,
            ),
            deepseek_temperature=base_temp,
            deepseek_expansion_temperature=expansion_temp,
            deepseek_level_temperature=level_temp,
            deepseek_pc_level_temperature=pc_temp,
            deepseek_pc_level_timeout_seconds=pc_timeout,
            dashboard_delete_password=str(
                source.get("DASHBOARD_DELETE_PASSWORD") or ""
            ).strip(),
            cocreation_intention_sync_secret=str(
                source.get("COCREATION_INTENTION_SYNC_SECRET") or ""
            ).strip(),
            pc_layout_prefilter_max_search_states=env_int(
                "PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES",
                PC_LAYOUT_PREFILTER_MAX_SEARCH_STATES,
                1,
                None,
                env=source,
            ),
            pc_layout_prefilter_seconds=env_float(
                "PC_LAYOUT_PREFILTER_SECONDS",
                PC_LAYOUT_PREFILTER_SECONDS,
                0.01,
                60.0,
                env=source,
            ),
            online_room_ttl_seconds=env_float(
                "ONLINE_ROOM_TTL_SECONDS",
                ONLINE_ROOM_TTL_SECONDS,
                1.0,
                None,
                env=source,
            ),
            allowed_origins=_parse_allowed_origins(raw_origins),
            operations_log_file=operations_log_file,
            log_level=(
                str(source.get("BACKEND_LOG_LEVEL") or "INFO").strip().upper()
                or "INFO"
            ),
        )


def safe_compare_digest(left: object, right: object) -> bool:
    left_bytes = str(left or "").encode("utf-8")
    right_bytes = str(right or "").encode("utf-8")
    return secrets.compare_digest(left_bytes, right_bytes)


def verify_delete_password_header(
    supplied_password: str | None,
    configured_password: str | None,
) -> None:
    configured = str(configured_password or "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="Delete password is not configured",
        )
    if not safe_compare_digest(supplied_password or "", configured):
        raise HTTPException(status_code=401, detail="Incorrect delete password")
