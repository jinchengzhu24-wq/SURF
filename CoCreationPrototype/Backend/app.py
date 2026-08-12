import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import quote

import uvicorn
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from level_validation import (
    HEIGHT,
    WIDTH,
    LevelValidationError,
    describe_diff,
    summarize_stage_changes,
    validate_and_solve,
)
from llm_client import (
    LLMServiceError,
    PROMPT_VERSION,
    generate_chat_reply,
    generate_stage_assessment,
)
from repository import (
    DATABASE_PATH,
    connect,
    dump_json,
    get_current_version,
    get_session,
    get_version,
    load_json,
    next_stage_number,
    next_turn_sequence,
    record_event,
    serialize_session,
)


HOST = "127.0.0.1"
PORT = 8010
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "Frontend"
MAX_MESSAGE_LENGTH = 2000
MAX_INTENTION_LENGTH = 4000
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SESSION_COOKIE_NAME = "sokoban_cocreation_access"
PLAY_TICKET_LIFETIME = timedelta(minutes=5)
INTERRUPTED_AFTER = timedelta(minutes=30)
_message_locks = {}
_message_locks_guard = Lock()

SAMPLE_ROWS = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]
SAMPLE_LEGEND = {
    "#": "wall",
    ".": "floor",
    "@": "water",
    "p": "player",
    "s": "box",
    "t": "target",
}

load_dotenv(BACKEND_DIR / ".env")
PUBLIC_BASE_URL = os.getenv(
    "COCREATION_PUBLIC_BASE_URL",
    "http://111.231.136.4:8010",
).rstrip("/")
WEBGL_BASE_URL = os.getenv(
    "COCREATION_WEBGL_BASE_URL",
    "http://111.231.136.4:8000/game/",
)
TOKEN_SECRET = os.getenv(
    "COCREATION_TOKEN_SECRET",
    "development-only-change-before-deployment",
).encode("utf-8")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "COCREATION_ALLOWED_ORIGINS",
        "http://111.231.136.4:8000,http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if origin.strip()
]


class ApiError(Exception):
    def __init__(self, status_code, code, message, retryable=False, details=None):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.details = details


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str


class LegacyChatRequest(StrictModel):
    messages: list[ChatMessage]


class CreateSessionRequest(StrictModel):
    rows: list[str]
    initialDraftMethod: Literal["partial_completion", "description_generation"]
    language: Literal["en", "zh-CN"] = "zh-CN"
    idempotencyKey: str
    matchId: str | None = None
    playerNumber: int | None = None


class BrowserAccessRequest(StrictModel):
    bootstrapToken: str


class LanguageRequest(StrictModel):
    language: Literal["en", "zh-CN"]


class VersionRequest(StrictModel):
    rows: list[str]
    baseVersionId: str
    idempotencyKey: str
    summary: str = ""


class RestoreRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str


class AssessmentRequest(StrictModel):
    idempotencyKey: str


class MessageRequest(StrictModel):
    content: str
    baseVersionId: str
    idempotencyKey: str


class ProposalDecisionRequest(StrictModel):
    decision: Literal["accept", "reject"]
    baseVersionId: str
    idempotencyKey: str
    reason: str = ""


class FinalizeRequest(StrictModel):
    baseVersionId: str
    idempotencyKey: str


class IntentionRequest(StrictModel):
    content: str
    idempotencyKey: str


class PlayAttemptRequest(StrictModel):
    idempotencyKey: str


class PlayBootstrapRequest(StrictModel):
    ticket: str


class PlayMetricsRequest(StrictModel):
    attemptToken: str
    durationSeconds: float = 0
    moveCount: int = 0
    pushCount: int = 0
    restartCount: int = 0
    minimumMoves: int = -1
    minimumPushes: int = -1


app = FastAPI(
    title="Sokoban Co-Creation Prototype",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID", "X-Session-Token"],
)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "").strip()
    request_id = (
        supplied_request_id
        if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        else uuid.uuid4().hex
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(request: Request, exception):
    return error_response(
        400,
        "INVALID_REQUEST",
        "The request body does not match the API contract.",
        request.state.request_id,
        False,
    )


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exception: ApiError):
    return error_response(
        exception.status_code,
        exception.code,
        exception.message,
        request.state.request_id,
        exception.retryable,
        exception.details,
    )


@app.exception_handler(LLMServiceError)
async def handle_llm_service_error(request: Request, exception: LLMServiceError):
    response = error_response(
        exception.status_code,
        exception.code,
        exception.safe_message,
        exception.request_id,
        exception.retryable,
    )
    response.headers["X-LLM-Attempts-Used"] = str(exception.attempts_used)
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    database_ready = DATABASE_PATH.parent.exists()
    return {
        "status": "ready" if database_ready else "not_ready",
        "database": str(DATABASE_PATH),
        "tokenSecretConfigured": TOKEN_SECRET != b"development-only-change-before-deployment",
    }


@app.get("/api/sample")
def get_sample():
    validation = validate_and_solve(SAMPLE_ROWS)
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "rows": list(SAMPLE_ROWS),
        "legend": dict(SAMPLE_LEGEND),
        "validation": validation.as_dict(),
    }


@app.post("/api/chat")
def legacy_chat(payload: LegacyChatRequest, request: Request, response: Response):
    messages = [message.model_dump() for message in payload.messages]
    validate_legacy_conversation(messages)
    validation = validate_and_solve(SAMPLE_ROWS)
    execution = generate_chat_reply(
        messages,
        SAMPLE_ROWS,
        request.state.request_id,
        solver_metrics=validation.as_dict(),
    )
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)
    return {
        "assistantMessage": execution.assistant_message,
        "requestId": execution.request_id,
    }


@app.post("/api/sessions")
def create_session(payload: CreateSessionRequest):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    validation = _solve_or_api_error(payload.rows)
    created_at = utc_now()

    with connect(immediate=True) as database:
        existing = database.execute(
            "SELECT * FROM design_sessions WHERE creation_key = ?",
            (payload.idempotencyKey,),
        ).fetchone()

        if existing is None:
            session_id = uuid.uuid4().hex
            version_id = uuid.uuid4().hex
            access_token = derive_token("access", session_id)
            integration_token = derive_token("integration", session_id)
            bootstrap_token = derive_token("bootstrap", session_id)
            database.execute(
                """
                INSERT INTO design_sessions(
                    id, creation_key, access_hash, integration_hash,
                    bootstrap_hash, match_id, player_number,
                    initial_draft_method, language, status,
                    current_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    session_id,
                    payload.idempotencyKey,
                    hash_token(access_token),
                    hash_token(integration_token),
                    hash_token(bootstrap_token),
                    clean_optional(payload.matchId, 128),
                    payload.playerNumber if payload.playerNumber in (1, 2) else None,
                    payload.initialDraftMethod,
                    payload.language,
                    version_id,
                    created_at,
                    created_at,
                ),
            )
            database.execute(
                """
                INSERT INTO level_versions(
                    id, session_id, stage_number, parent_version_id, source,
                    rows_json, summary, diff_json, validation_json,
                    idempotency_key, created_at
                ) VALUES (?, ?, 1, NULL, 'initial', ?, ?, '[]', ?, ?, ?)
                """,
                (
                    version_id,
                    session_id,
                    dump_json(list(validation.rows)),
                    "Initial draft from Unity",
                    dump_json(validation.as_dict()),
                    "initial:" + payload.idempotencyKey,
                    created_at,
                ),
            )
            record_event(
                database,
                session_id,
                "session_created",
                {
                    "versionId": version_id,
                    "initialDraftMethod": payload.initialDraftMethod,
                },
                created_at,
            )
        else:
            session_id = existing["id"]
            existing_version = database.execute(
                """
                SELECT * FROM level_versions
                WHERE session_id = ? AND stage_number = 1
                """,
                (session_id,),
            ).fetchone()

            if (
                existing["initial_draft_method"] != payload.initialDraftMethod
                or existing_version is None
                or load_json(existing_version["rows_json"]) != list(validation.rows)
            ):
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The session creation key was already used for different input.",
                )

            access_token = derive_token("access", session_id)
            integration_token = derive_token("integration", session_id)
            bootstrap_token = derive_token("bootstrap", session_id)

    return {
        "sessionId": session_id,
        "launchUrl": build_launch_url(session_id, bootstrap_token),
        "integrationToken": integration_token,
    }


@app.post("/api/sessions/{session_id}/browser-access")
def exchange_browser_access(
    session_id: str,
    payload: BrowserAccessRequest,
    response: Response,
):
    with connect(immediate=True) as database:
        session = get_session(database, session_id)

        if session is None or not token_matches(
            payload.bootstrapToken,
            session["bootstrap_hash"],
        ):
            raise ApiError(401, "INVALID_BOOTSTRAP_TOKEN", "The session link is invalid.")

        if session["bootstrap_used_at"] is not None:
            raise ApiError(409, "BOOTSTRAP_TOKEN_USED", "The session link was already used.")

        now = utc_now()
        database.execute(
            "UPDATE design_sessions SET bootstrap_used_at = ? WHERE id = ?",
            (now, session_id),
        )
        record_event(database, session_id, "browser_access_granted", {}, now)

    response.set_cookie(
        SESSION_COOKIE_NAME,
        derive_token("access", session_id),
        httponly=True,
        samesite="lax",
        secure=PUBLIC_BASE_URL.startswith("https://"),
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return {"sessionId": session_id}


@app.get("/api/sessions/{session_id}")
def read_session(
    session_id: str,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
    session_token: str | None = Header(None, alias="X-Session-Token"),
):
    with connect(immediate=True) as database:
        session = require_browser_session(
            database,
            session_id,
            access_cookie or session_token,
        )
        expire_interrupted_attempts(database, session_id)
        return serialize_session(database, session["id"])


@app.patch("/api/sessions/{session_id}/language")
def change_language(
    session_id: str,
    payload: LanguageRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        now = utc_now()
        database.execute(
            "UPDATE design_sessions SET language = ?, updated_at = ? WHERE id = ?",
            (payload.language, now, session_id),
        )
        record_event(
            database,
            session_id,
            "language_changed",
            {"language": payload.language},
            now,
        )
        return serialize_session(database, session["id"])


@app.post("/api/sessions/{session_id}/versions")
def create_manual_version(
    session_id: str,
    payload: VersionRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    validation = _solve_or_api_error(payload.rows)

    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing = database.execute(
            """
            SELECT * FROM level_versions
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if existing is not None:
            if (
                load_json(existing["rows_json"]) != list(validation.rows)
                or existing["parent_version_id"] != payload.baseVersionId
            ):
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The Stage save key was already used for different input.",
                )
            return serialize_session(database, session_id)

        require_current_base(session, payload.baseVersionId)
        current = get_current_version(database, session)
        current_rows = load_json(current["rows_json"])

        if list(validation.rows) == current_rows:
            raise ApiError(400, "UNCHANGED_LEVEL", "Save requires at least one tile change.")

        _insert_version(
            database,
            session,
            validation,
            "human_edit",
            clean_optional(payload.summary, 1000) or "Designer saved an edited stage",
            payload.idempotencyKey,
            current,
        )
        return serialize_session(database, session_id)


@app.post("/api/sessions/{session_id}/versions/{version_id}/restore")
def restore_version(
    session_id: str,
    version_id: str,
    payload: RestoreRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing_decision = database.execute(
            """
            SELECT id FROM designer_decisions
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if existing_decision is not None:
            return serialize_session(database, session_id)

        require_current_base(session, payload.baseVersionId)
        source = get_version(database, session_id, version_id)

        if source is None:
            raise ApiError(404, "VERSION_NOT_FOUND", "The selected Stage was not found.")

        validation = _solve_or_api_error(load_json(source["rows_json"]))
        current = get_current_version(database, session)
        new_version_id = _insert_version(
            database,
            session,
            validation,
            "restored",
            f"Continued from Stage {source['stage_number']}",
            "restore:" + payload.idempotencyKey,
            current,
            parent_version_id=source["id"],
        )
        _insert_decision(
            database,
            session_id,
            new_version_id,
            None,
            "restore",
            "",
            payload.idempotencyKey,
        )
        return serialize_session(database, session_id)


@app.post("/api/sessions/{session_id}/versions/{version_id}/assessments")
def assess_version(
    session_id: str,
    version_id: str,
    payload: AssessmentRequest,
    request: Request,
    response: Response,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    with connect() as database:
        session = require_browser_session(database, session_id, access_cookie)
        version = get_version(database, session_id, version_id)

        if version is None:
            raise ApiError(404, "VERSION_NOT_FOUND", "The selected Stage was not found.")

        existing = database.execute(
            "SELECT id FROM llm_assessments WHERE version_id = ?",
            (version_id,),
        ).fetchone()

        if existing is not None:
            return serialize_session(database, session_id)

        accepted_opening = database.execute(
            """
            SELECT decision.id
            FROM designer_decisions AS decision
            JOIN change_proposals AS proposal
              ON proposal.id = decision.proposal_id
            WHERE decision.session_id = ?
              AND decision.version_id = ?
              AND decision.decision_type = 'accept'
              AND proposal.assistant_turn_id IS NOT NULL
            LIMIT 1
            """,
            (session_id, version_id),
        ).fetchone()

        if accepted_opening is not None:
            return serialize_session(database, session_id)

        context = build_llm_context(database, session_id, version)
        session_language = session["language"]

    execution = generate_stage_assessment(
        context["conversation"],
        context["rows"],
        session_language,
        context["validation"],
        context["playSummary"],
        request.state.request_id,
        stage_context=context["stageContext"],
    )
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)

    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        existing = database.execute(
            "SELECT id FROM llm_assessments WHERE version_id = ?",
            (version_id,),
        ).fetchone()

        if existing is None:
            turn_id = insert_turn(
                database,
                session,
                "assistant",
                execution.assistant_message,
                version_id,
                execution.request_id,
                execution,
            )
            assessment_id = uuid.uuid4().hex
            database.execute(
                """
                INSERT INTO llm_assessments(
                    id, session_id, version_id, assistant_turn_id,
                    payload_json, prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id,
                    session_id,
                    version_id,
                    turn_id,
                    dump_json(execution.assessment),
                    PROMPT_VERSION,
                    utc_now(),
                ),
            )

        return serialize_session(database, session_id)


@app.post("/api/sessions/{session_id}/messages")
def send_message(
    session_id: str,
    payload: MessageRequest,
    request: Request,
    response: Response,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    content = payload.content.strip()

    if not content or len(content) > MAX_MESSAGE_LENGTH:
        raise ApiError(400, "INVALID_MESSAGE", "The message must contain 1 to 2000 characters.")

    with message_request_lock(session_id, payload.idempotencyKey):
        return _send_message_locked(
            session_id,
            payload,
            content,
            request,
            response,
            access_cookie,
        )


def _send_message_locked(
    session_id,
    payload,
    content,
    request,
    response,
    access_cookie,
):
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        prior_user = database.execute(
            """
            SELECT * FROM conversation_turns
            WHERE session_id = ? AND request_id = ? AND role = 'user'
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if prior_user is not None and (
            prior_user["content"] != content
            or prior_user["version_id"] != payload.baseVersionId
        ):
            raise ApiError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "The message key was already used for different content or Stage.",
            )

        prior_assistant = database.execute(
            """
            SELECT * FROM conversation_turns
            WHERE session_id = ? AND request_id = ? AND role = 'assistant'
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if prior_assistant is not None:
            return serialize_session(database, session_id)

        require_current_base(session, payload.baseVersionId)

        if prior_user is None:
            insert_turn(
                database,
                session,
                "user",
                content,
                payload.baseVersionId,
                payload.idempotencyKey,
                None,
            )

        current = get_current_version(database, session)
        context = build_llm_context(database, session_id, current)
        language = session["language"]

    execution = generate_chat_reply(
        context["conversation"],
        context["rows"],
        request.state.request_id,
        language=language,
        solver_metrics=context["validation"],
        play_summary=context["playSummary"],
        proposal_validator=validate_and_solve,
        stage_context=context["stageContext"],
    )
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)

    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, payload.baseVersionId)
        existing = database.execute(
            """
            SELECT * FROM conversation_turns
            WHERE session_id = ? AND request_id = ? AND role = 'assistant'
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if existing is None:
            assistant_turn_id = insert_turn(
                database,
                session,
                "assistant",
                execution.assistant_message,
                payload.baseVersionId,
                payload.idempotencyKey,
                execution,
            )

            if execution.proposed_rows is not None:
                proposal_validation = validate_and_solve(execution.proposed_rows)
                proposal_id = uuid.uuid4().hex
                database.execute(
                    """
                    INSERT INTO change_proposals(
                        id, session_id, base_version_id, proposed_rows_json,
                        summary, diff_json, validation_json, status,
                        assistant_turn_id, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        session_id,
                        payload.baseVersionId,
                        dump_json(list(proposal_validation.rows)),
                        execution.modification_summary,
                        dump_json(describe_diff(context["rows"], proposal_validation.rows)),
                        dump_json(proposal_validation.as_dict()),
                        assistant_turn_id,
                        payload.idempotencyKey,
                        utc_now(),
                    ),
                )

        return serialize_session(database, session_id)


@contextmanager
def message_request_lock(session_id, idempotency_key):
    key = (session_id, idempotency_key)

    with _message_locks_guard:
        entry = _message_locks.get(key)

        if entry is None:
            entry = {"lock": Lock(), "users": 0, "error": None}
            _message_locks[key] = entry

        is_leader = entry["users"] == 0
        entry["users"] += 1

    entry["lock"].acquire()

    try:
        if not is_leader and entry["error"] is not None:
            raise entry["error"]

        yield
    except Exception as error:
        if is_leader:
            entry["error"] = error
        raise
    finally:
        entry["lock"].release()

        with _message_locks_guard:
            entry["users"] -= 1

            if entry["users"] == 0:
                _message_locks.pop(key, None)


@app.post("/api/sessions/{session_id}/proposals/{proposal_id}/decision")
def decide_proposal(
    session_id: str,
    proposal_id: str,
    payload: ProposalDecisionRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        existing_decision = database.execute(
            """
            SELECT id FROM designer_decisions
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if existing_decision is not None:
            return serialize_session(database, session_id)

        require_current_base(session, payload.baseVersionId)
        proposal = database.execute(
            """
            SELECT * FROM change_proposals WHERE id = ? AND session_id = ?
            """,
            (proposal_id, session_id),
        ).fetchone()

        if proposal is None:
            raise ApiError(404, "PROPOSAL_NOT_FOUND", "The proposal was not found.")

        if proposal["status"] != "pending":
            raise ApiError(409, "PROPOSAL_ALREADY_DECIDED", "The proposal was already decided.")

        if proposal["base_version_id"] != session["current_version_id"]:
            raise ApiError(409, "VERSION_CONFLICT", "The proposal belongs to an older Stage.")

        now = utc_now()
        new_version_id = None

        if payload.decision == "accept":
            proposed_rows = load_json(proposal["proposed_rows_json"])
            validation = _solve_or_api_error(proposed_rows)
            current = get_current_version(database, session)
            new_version_id = _insert_version(
                database,
                session,
                validation,
                "llm_accepted",
                proposal["summary"] or "Accepted LLM map proposal",
                "proposal:" + payload.idempotencyKey,
                current,
            )

        database.execute(
            """
            UPDATE change_proposals SET status = ?, decided_at = ? WHERE id = ?
            """,
            ("accepted" if payload.decision == "accept" else "rejected", now, proposal_id),
        )
        _insert_decision(
            database,
            session_id,
            new_version_id,
            proposal_id,
            payload.decision,
            clean_optional(payload.reason, 2000) or "",
            payload.idempotencyKey,
        )
        return serialize_session(database, session_id)


@app.post("/api/sessions/{session_id}/versions/{version_id}/play-attempts")
def create_play_attempt(
    session_id: str,
    version_id: str,
    payload: PlayAttemptRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        version = get_version(database, session_id, version_id)

        if version is None:
            raise ApiError(404, "VERSION_NOT_FOUND", "The selected Stage was not found.")

        existing = database.execute(
            """
            SELECT * FROM play_attempts
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if existing is None:
            attempt_id = uuid.uuid4().hex
            ticket = derive_token("play-ticket", attempt_id)
            now_value = datetime.now(timezone.utc)
            issued_at = iso_time(now_value)
            expires_at = iso_time(now_value + PLAY_TICKET_LIFETIME)
            validation = load_json(version["validation_json"])
            database.execute(
                """
                INSERT INTO play_attempts(
                    id, session_id, version_id, initial_draft_method,
                    language, status, ticket_hash, ticket_expires_at,
                    issued_at, minimum_moves, minimum_pushes, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    session_id,
                    version_id,
                    session["initial_draft_method"],
                    session["language"],
                    hash_token(ticket),
                    expires_at,
                    issued_at,
                    validation.get("solutionSteps", -1),
                    validation.get("solutionPushes", -1),
                    payload.idempotencyKey,
                ),
            )
            record_event(
                database,
                session_id,
                "play_attempt_issued",
                {"attemptId": attempt_id, "versionId": version_id},
                issued_at,
            )
        else:
            if existing["version_id"] != version_id:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The Play key was already used for another Stage.",
                )
            attempt_id = existing["id"]
            ticket = derive_token("play-ticket", attempt_id)

    return {
        "attemptId": attempt_id,
        "playUrl": build_play_url(attempt_id, ticket),
    }


@app.post("/api/play-attempts/{attempt_id}/bootstrap")
def bootstrap_play_attempt(attempt_id: str, payload: PlayBootstrapRequest):
    with connect(immediate=True) as database:
        attempt = database.execute(
            "SELECT * FROM play_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()

        if attempt is None or not token_matches(payload.ticket, attempt["ticket_hash"]):
            raise ApiError(401, "INVALID_PLAY_TICKET", "The Play ticket is invalid.")

        if attempt["ticket_used_at"] is not None:
            raise ApiError(409, "PLAY_TICKET_USED", "The Play ticket was already used.")

        if parse_time(attempt["ticket_expires_at"]) < datetime.now(timezone.utc):
            raise ApiError(410, "PLAY_TICKET_EXPIRED", "The Play ticket has expired.")

        version = get_version(database, attempt["session_id"], attempt["version_id"])
        session = get_session(database, attempt["session_id"])
        attempt_token = derive_token("play-attempt", attempt_id)
        now = utc_now()
        database.execute(
            """
            UPDATE play_attempts
            SET ticket_used_at = ?, attempt_token_hash = ?, loaded_at = ?, status = 'loaded'
            WHERE id = ?
            """,
            (now, hash_token(attempt_token), now, attempt_id),
        )
        return {
            "attemptId": attempt_id,
            "sessionId": attempt["session_id"],
            "versionId": attempt["version_id"],
            "rows": load_json(version["rows_json"]),
            "initialDraftMethod": attempt["initial_draft_method"],
            "language": attempt["language"],
            "attemptToken": attempt_token,
            "returnUrl": build_return_url(session["id"], version["id"]),
        }


@app.post("/api/play-attempts/{attempt_id}/start")
def start_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, "started")


@app.post("/api/play-attempts/{attempt_id}/progress")
def progress_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, "started")


@app.post("/api/play-attempts/{attempt_id}/complete")
def complete_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, "completed")


@app.post("/api/play-attempts/{attempt_id}/abandon")
def abandon_play_attempt(attempt_id: str, payload: PlayMetricsRequest):
    return update_play_attempt(attempt_id, payload, "abandoned")


@app.post("/api/sessions/{session_id}/finalize")
def finalize_session(
    session_id: str,
    payload: FinalizeRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, payload.baseVersionId)
        pending = database.execute(
            """
            SELECT COUNT(*) FROM change_proposals
            WHERE session_id = ? AND status = 'pending'
            """,
            (session_id,),
        ).fetchone()[0]

        if pending:
            raise ApiError(409, "PENDING_PROPOSAL", "Decide the pending proposal first.")

        existing = database.execute(
            """
            SELECT id FROM designer_decisions
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, payload.idempotencyKey),
        ).fetchone()

        if existing is None:
            now = utc_now()
            database.execute(
                """
                UPDATE design_sessions
                SET status = 'awaiting_intention', final_version_id = ?,
                    finalized_at = ?, updated_at = ? WHERE id = ?
                """,
                (payload.baseVersionId, now, now, session_id),
            )
            _insert_decision(
                database,
                session_id,
                payload.baseVersionId,
                None,
                "finalize",
                "",
                payload.idempotencyKey,
            )

        return serialize_session(database, session_id)


@app.post("/api/sessions/{session_id}/intention")
def submit_intention(
    session_id: str,
    payload: IntentionRequest,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    _validate_identifier(payload.idempotencyKey, "idempotencyKey")
    content = payload.content.strip()

    if not content or len(content) > MAX_INTENTION_LENGTH:
        raise ApiError(400, "INVALID_INTENTION", "The intention response is required.")

    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)

        if session["status"] == "completed":
            return serialize_session(database, session_id)

        if session["status"] != "awaiting_intention":
            raise ApiError(409, "SESSION_NOT_FINALIZED", "Finalize a Stage first.")

        now = utc_now()
        database.execute(
            """
            INSERT INTO designer_intentions(
                id, session_id, content, language, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                session_id,
                content,
                session["language"],
                payload.idempotencyKey,
                now,
            ),
        )
        database.execute(
            """
            UPDATE design_sessions
            SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?
            """,
            (now, now, session_id),
        )
        record_event(database, session_id, "intention_submitted", {}, now)
        return serialize_session(database, session_id)


@app.get("/api/integrations/sessions/{session_id}")
def integration_status(
    session_id: str,
    authorization: str | None = Header(None, alias="Authorization"),
):
    token = ""

    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()

    with connect() as database:
        session = get_session(database, session_id)

        if session is None or not token_matches(token, session["integration_hash"]):
            raise ApiError(401, "INVALID_INTEGRATION_TOKEN", "Integration access was denied.")

        payload = {
            "sessionId": session_id,
            "status": session["status"],
            "finalVersionId": session["final_version_id"],
            "finalRows": None,
        }

        if session["status"] == "completed" and session["final_version_id"]:
            version = get_version(database, session_id, session["final_version_id"])
            payload["finalRows"] = load_json(version["rows_json"])

        return payload


def update_play_attempt(attempt_id, payload, requested_status):
    _validate_metrics(payload)

    with connect(immediate=True) as database:
        attempt = database.execute(
            "SELECT * FROM play_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()

        if attempt is None or not token_matches(
            payload.attemptToken,
            attempt["attempt_token_hash"],
        ):
            raise ApiError(401, "INVALID_ATTEMPT_TOKEN", "Play result access was denied.")

        if attempt["status"] in {"completed", "abandoned", "interrupted"}:
            return {"attemptId": attempt_id, "status": attempt["status"]}

        now = utc_now()
        first_move_at = attempt["first_move_at"]

        if first_move_at is None and payload.moveCount > 0:
            first_move_at = now

        terminal = requested_status in {"completed", "abandoned"}
        database.execute(
            """
            UPDATE play_attempts SET
                status = ?, first_move_at = ?, finished_at = ?,
                duration_seconds = ?, move_count = ?, push_count = ?,
                restart_count = ?, minimum_moves = ?, minimum_pushes = ?
            WHERE id = ?
            """,
            (
                requested_status,
                first_move_at,
                now if terminal else None,
                payload.durationSeconds,
                payload.moveCount,
                payload.pushCount,
                payload.restartCount,
                payload.minimumMoves,
                payload.minimumPushes,
                attempt_id,
            ),
        )
        record_event(
            database,
            attempt["session_id"],
            "play_attempt_" + requested_status,
            {
                "attemptId": attempt_id,
                "versionId": attempt["version_id"],
                "moveCount": payload.moveCount,
                "pushCount": payload.pushCount,
                "restartCount": payload.restartCount,
                "durationSeconds": payload.durationSeconds,
            },
            now,
        )
        return {"attemptId": attempt_id, "status": requested_status}


def _insert_version(
    database,
    session,
    validation,
    source,
    summary,
    idempotency_key,
    current,
    parent_version_id=None,
):
    version_id = uuid.uuid4().hex
    now = utc_now()
    current_rows = load_json(current["rows_json"])
    database.execute(
        """
        INSERT INTO level_versions(
            id, session_id, stage_number, parent_version_id, source,
            rows_json, summary, diff_json, validation_json,
            idempotency_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            session["id"],
            next_stage_number(database, session["id"]),
            parent_version_id or current["id"],
            source,
            dump_json(list(validation.rows)),
            summary,
            dump_json(describe_diff(current_rows, validation.rows)),
            dump_json(validation.as_dict()),
            idempotency_key,
            now,
        ),
    )
    database.execute(
        """
        UPDATE design_sessions SET current_version_id = ?, updated_at = ? WHERE id = ?
        """,
        (version_id, now, session["id"]),
    )
    record_event(
        database,
        session["id"],
        "version_created",
        {"versionId": version_id, "source": source},
        now,
    )
    return version_id


def _insert_decision(
    database,
    session_id,
    version_id,
    proposal_id,
    decision_type,
    reason,
    idempotency_key,
):
    now = utc_now()
    database.execute(
        """
        INSERT INTO designer_decisions(
            id, session_id, version_id, proposal_id, decision_type,
            reason, idempotency_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            session_id,
            version_id,
            proposal_id,
            decision_type,
            reason,
            idempotency_key,
            now,
        ),
    )
    record_event(
        database,
        session_id,
        "designer_decision",
        {"decision": decision_type, "proposalId": proposal_id, "versionId": version_id},
        now,
    )


def insert_turn(database, session, role, content, version_id, request_id, execution):
    turn_id = uuid.uuid4().hex
    now = utc_now()
    database.execute(
        """
        INSERT INTO conversation_turns(
            id, session_id, sequence_number, role, content, language,
            version_id, request_id, model, attempts_used, latency_ms,
            guidance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            session["id"],
            next_turn_sequence(database, session["id"]),
            role,
            content,
            session["language"],
            version_id,
            request_id,
            execution.model if execution else None,
            execution.attempts_used if execution else None,
            execution.latency_ms if execution else None,
            (
                dump_json(execution.guidance)
                if execution and execution.guidance
                else None
            ),
            now,
        ),
    )
    record_event(
        database,
        session["id"],
        "conversation_turn",
        {"turnId": turn_id, "role": role, "versionId": version_id},
        now,
    )
    return turn_id


def build_llm_context(database, session_id, version):
    session = database.execute(
        "SELECT initial_draft_method FROM design_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    turns = database.execute(
        """
        SELECT id, role, content FROM conversation_turns
        WHERE session_id = ? AND version_id = ?
        ORDER BY sequence_number DESC LIMIT 24
        """,
        (session_id, version["id"]),
    ).fetchall()
    accepted_opening = database.execute(
        """
        SELECT proposal.id AS proposal_id, proposal.assistant_turn_id,
               turn.role, turn.content
        FROM designer_decisions AS decision
        JOIN change_proposals AS proposal
          ON proposal.id = decision.proposal_id
        JOIN conversation_turns AS turn
          ON turn.id = proposal.assistant_turn_id
        WHERE decision.session_id = ?
          AND decision.version_id = ?
          AND decision.decision_type = 'accept'
        ORDER BY decision.created_at LIMIT 1
        """,
        (session_id, version["id"]),
    ).fetchone()
    superseded_assessment_turn_ids = set()

    if accepted_opening is not None:
        superseded_assessment_turn_ids = {
            row["assistant_turn_id"]
            for row in database.execute(
                """
                SELECT assistant_turn_id FROM llm_assessments
                WHERE session_id = ? AND version_id = ?
                """,
                (session_id, version["id"]),
            ).fetchall()
        }
    latest_play = database.execute(
        """
        SELECT status, duration_seconds, move_count, push_count, restart_count,
               minimum_moves, minimum_pushes, finished_at
        FROM play_attempts
        WHERE session_id = ? AND version_id = ?
          AND status IN ('completed', 'abandoned')
        ORDER BY issued_at DESC LIMIT 1
        """,
        (session_id, version["id"]),
    ).fetchone()
    current_rows = load_json(version["rows_json"])
    parent = (
        get_version(database, session_id, version["parent_version_id"])
        if version["parent_version_id"]
        else None
    )
    change_summary = (
        summarize_stage_changes(load_json(parent["rows_json"]), current_rows)
        if parent is not None
        else None
    )
    conversation = [
        {"role": turn["role"], "content": turn["content"]}
        for turn in reversed(turns)
        if turn["id"] not in superseded_assessment_turn_ids
    ]

    if accepted_opening is not None and not any(
        turn["content"] == accepted_opening["content"]
        and turn["role"] == accepted_opening["role"]
        for turn in conversation
    ):
        conversation.insert(
            0,
            {
                "role": accepted_opening["role"],
                "content": accepted_opening["content"],
            },
        )

    return {
        "rows": current_rows,
        "validation": load_json(version["validation_json"]),
        "conversation": conversation,
        "stageContext": {
            "stageNumber": version["stage_number"],
            "source": version["source"],
            "initialDraftMethod": (
                session["initial_draft_method"] if session is not None else None
            ),
            "summary": version["summary"],
            "parentVersionId": version["parent_version_id"],
            "diff": load_json(version["diff_json"]),
            "changeSummary": change_summary,
            "openingTurnId": (
                accepted_opening["assistant_turn_id"]
                if accepted_opening is not None
                else None
            ),
            "openingProposalId": (
                accepted_opening["proposal_id"]
                if accepted_opening is not None
                else None
            ),
        },
        "playSummary": (
            {
                "status": latest_play["status"],
                "durationSeconds": latest_play["duration_seconds"],
                "moveCount": latest_play["move_count"],
                "pushCount": latest_play["push_count"],
                "restartCount": latest_play["restart_count"],
                "minimumMoves": latest_play["minimum_moves"],
                "minimumPushes": latest_play["minimum_pushes"],
                "finishedAt": latest_play["finished_at"],
            }
            if latest_play is not None
            else None
        ),
    }


def require_browser_session(database, session_id, token):
    session = get_session(database, session_id)

    if session is None:
        raise ApiError(404, "SESSION_NOT_FOUND", "The co-creation session was not found.")

    if not token_matches(token, session["access_hash"]):
        raise ApiError(401, "SESSION_ACCESS_DENIED", "Session access was denied.")

    return session


def require_active_session(database, session_id, token):
    session = require_browser_session(database, session_id, token)

    if session["status"] != "active":
        raise ApiError(409, "SESSION_LOCKED", "This co-creation session is no longer editable.")

    return session


def require_current_base(session, base_version_id):
    if session["current_version_id"] != base_version_id:
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            "The current Stage changed. Refresh before continuing.",
            details={"currentVersionId": session["current_version_id"]},
        )


def expire_interrupted_attempts(database, session_id):
    cutoff = iso_time(datetime.now(timezone.utc) - INTERRUPTED_AFTER)
    database.execute(
        """
        UPDATE play_attempts SET status = 'interrupted', finished_at = ?
        WHERE session_id = ? AND status IN ('issued', 'loaded', 'started')
          AND issued_at < ?
        """,
        (utc_now(), session_id, cutoff),
    )


def validate_legacy_conversation(messages):
    if not messages:
        raise ApiError(400, "EMPTY_CONVERSATION", "At least one chat message is required.")

    if messages[-1]["role"] != "user":
        raise ApiError(400, "LAST_MESSAGE_MUST_BE_USER", "The final message must be from the user.")

    if len(messages) > 20:
        raise ApiError(400, "TOO_MANY_MESSAGES", "A request may contain at most 20 messages.")

    total = 0

    for message in messages:
        if not message["content"].strip():
            raise ApiError(400, "EMPTY_MESSAGE", "Messages must not be empty.")

        if len(message["content"]) > MAX_MESSAGE_LENGTH:
            raise ApiError(400, "MESSAGE_TOO_LONG", "A message exceeds 2000 characters.")

        total += len(message["content"])

    if total > 12000:
        raise ApiError(400, "CONVERSATION_TOO_LONG", "Conversation text is too long.")


def _solve_or_api_error(rows):
    try:
        return validate_and_solve(rows)
    except LevelValidationError as error:
        raise ApiError(400, error.code, str(error), details=error.details) from error


def _validate_identifier(value, field_name):
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.fullmatch(value):
        raise ApiError(400, "INVALID_IDENTIFIER", f"{field_name} is invalid.")


def _validate_metrics(payload):
    if (
        payload.durationSeconds < 0
        or payload.durationSeconds > 86_400
        or min(payload.moveCount, payload.pushCount, payload.restartCount) < 0
        or max(payload.moveCount, payload.pushCount, payload.restartCount) > 1_000_000
        or payload.pushCount > payload.moveCount
    ):
        raise ApiError(400, "INVALID_PLAY_METRICS", "Play metrics are invalid.")


def derive_token(kind, identifier):
    digest = hmac.new(
        TOKEN_SECRET,
        f"{kind}:{identifier}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def token_matches(token, expected_hash):
    if not token or not expected_hash:
        return False

    return secrets.compare_digest(hash_token(token), expected_hash)


def build_launch_url(session_id, bootstrap_token):
    return (
        f"{PUBLIC_BASE_URL}/#session={quote(session_id)}"
        f"&bootstrap={quote(bootstrap_token)}"
    )


def build_return_url(session_id, version_id):
    return (
        f"{PUBLIC_BASE_URL}/#session={quote(session_id)}"
        f"&stage={quote(version_id)}"
    )


def build_play_url(attempt_id, ticket):
    separator = "&" if "?" in WEBGL_BASE_URL else "?"
    return (
        f"{WEBGL_BASE_URL}{separator}cocreationAttempt={quote(attempt_id)}"
        f"&cocreationPlay={quote(ticket)}"
    )


def clean_optional(value, maximum_length):
    if value is None:
        return None

    return str(value).strip()[:maximum_length]


def utc_now():
    return iso_time(datetime.now(timezone.utc))


def iso_time(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def error_response(status_code, code, message, request_id, retryable, details=None):
    payload = {
        "code": str(code),
        "message": str(message),
        "requestId": str(request_id),
        "retryable": bool(retryable),
    }

    if details is not None:
        payload["details"] = details

    return JSONResponse(status_code=status_code, content=payload)


app.mount(
    "/",
    StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
    name="prototype-frontend",
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
