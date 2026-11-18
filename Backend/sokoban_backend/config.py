import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException, Request


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

ONLINE_ROOM_TTL_SECONDS = 30 * 60
ONLINE_ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ONLINE_ROOM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6}$")
ONLINE_AI_ASSISTANT_MODES = {
    "description_generation",
}
AGENT_HANDOFF_SCHEMA_VERSION = 1

load_dotenv(BASE_DIR / ".env")
COCREATION_INTENTION_SYNC_SECRET = os.environ.get(
    "COCREATION_INTENTION_SYNC_SECRET",
    "",
).strip()


def env_float(name, default, minimum=None, maximum=None):
    raw = os.getenv(name)
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


def safe_compare_digest(left, right):
    left_bytes = str(left or "").encode("utf-8")
    right_bytes = str(right or "").encode("utf-8")
    return secrets.compare_digest(left_bytes, right_bytes)


def require_delete_password(request: Request):
    configured_password = os.environ.get("DASHBOARD_DELETE_PASSWORD", "").strip()

    if not configured_password:
        raise HTTPException(
            status_code=503,
            detail="Delete password is not configured",
        )

    supplied_password = request.headers.get("X-Delete-Password", "")

    if not safe_compare_digest(supplied_password, configured_password):
        raise HTTPException(status_code=401, detail="Incorrect delete password")
