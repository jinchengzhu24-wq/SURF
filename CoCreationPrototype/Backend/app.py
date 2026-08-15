import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
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
    build_map_facts,
    LevelValidationError,
    describe_diff,
    summarize_verified_diff,
    summarize_stage_changes,
    validate_and_solve,
)
from llm_client import (
    LLMExecutionResult,
    LLMServiceError,
    PROMPT_VERSION,
    PROPOSAL_GENERATION_ATTEMPTS,
    classify_revision_request,
    generate_chat_reply,
    generate_stage_assessment,
    translate_turns,
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


class TranslationRequest(StrictModel):
    turnIds: list[str]


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


@app.post("/api/sessions/{session_id}/translations/{language}")
def translate_session_turns(
    session_id: str,
    language: Literal["en", "zh-CN"],
    payload: TranslationRequest,
    request: Request,
    access_cookie: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
):
    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)
        pending = _pending_assistant_translations(
            database,
            session_id,
            language,
            payload.turnIds,
        )

        if not pending:
            return serialize_session(database, session["id"])

    execution = translate_turns(pending, language, request.state.request_id)

    with connect(immediate=True) as database:
        session = require_browser_session(database, session_id, access_cookie)

        for translated in execution.translations:
            source = next(item for item in pending if item["turnId"] == translated["turnId"])
            guidance = _translated_guidance(source["guidance"], translated)
            database.execute(
                """
                INSERT OR IGNORE INTO turn_translations(
                    id, session_id, turn_id, language, body, guidance_json,
                    proposal_summary, model, attempts_used, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    session_id,
                    translated["turnId"],
                    language,
                    translated["body"],
                    dump_json(guidance) if guidance else None,
                    translated["proposalSummary"],
                    execution.model,
                    execution.attempts_used,
                    execution.latency_ms,
                    utc_now(),
                ),
            )

        record_event(
            database,
            session_id,
            "turn_translations_created",
            {
                "language": language,
                "turnIds": [item["turnId"] for item in execution.translations],
                "model": execution.model,
            },
            utc_now(),
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

        retrying_failed_message = prior_user is not None
        current = get_current_version(database, session)
        context = build_llm_context(database, session_id, current)
        language = session["language"]

    revision_state, revision_brief = classify_revision_request(
        context["conversation"],
        context["stageContext"],
    )
    if revision_state == "relaxation_confirmed":
        execution = _relaxed_revision_suggestion_execution(
            context["stageContext"],
            language,
            request.state.request_id,
        )
    else:
        try:
            execution = generate_chat_reply(
                context["conversation"],
                context["rows"],
                request.state.request_id,
                language=language,
                solver_metrics=context["validation"],
                play_summary=context["playSummary"],
                proposal_validator=lambda proposed_rows: _validate_changed_proposal(
                    context["rows"],
                    proposed_rows,
                ),
                stage_context=context["stageContext"],
            )
        except LLMServiceError as exception:
            execution = _proposal_relaxation_fallback_after_failure(
                session_id=session_id,
                access_cookie=access_cookie,
                base_version_id=payload.baseVersionId,
                idempotency_key=payload.idempotencyKey,
                revision_state=revision_state,
                revision_brief=revision_brief,
                language=language,
                exception=exception,
            )
            if execution is None:
                if not retrying_failed_message:
                    with connect(immediate=True) as database:
                        active_session = require_active_session(
                            database,
                            session_id,
                            access_cookie,
                        )
                        require_current_base(active_session, payload.baseVersionId)
                        record_event(
                            database,
                            session_id,
                            "message_generation_failed",
                            {
                                "baseVersionId": payload.baseVersionId,
                                "messageKey": payload.idempotencyKey,
                                "code": exception.code,
                                "attemptsUsed": exception.attempts_used,
                            },
                            utc_now(),
                        )
                    raise
                execution = _retry_exhausted_execution(language, exception)
                with connect(immediate=True) as database:
                    active_session = require_active_session(
                        database,
                        session_id,
                        access_cookie,
                    )
                    require_current_base(active_session, payload.baseVersionId)
                    record_event(
                        database,
                        session_id,
                        "message_retry_exhausted",
                        {
                            "baseVersionId": payload.baseVersionId,
                            "messageKey": payload.idempotencyKey,
                            "code": exception.code,
                            "attemptsUsed": exception.attempts_used,
                        },
                        utc_now(),
                    )
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)

    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, payload.baseVersionId)
        current = get_current_version(database, session)
        proposal_validation = None

        if execution.proposed_rows is not None:
            proposal_validation = _solve_changed_proposal_or_api_error(
                load_json(current["rows_json"]),
                execution.proposed_rows,
            )
            current_rows = load_json(current["rows_json"])
            verified_summary = summarize_verified_diff(
                current_rows,
                proposal_validation.rows,
                language,
            )
            execution = replace(
                execution,
                assistant_message=_verified_proposal_message(language),
                modification_summary=verified_summary,
                guidance=_proposal_review_guidance(execution.guidance, language),
            )

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

            if execution.revision_plan and execution.proposed_rows is not None:
                record_event(
                    database,
                    session_id,
                    "proposal_search_completed",
                    {
                        "baseVersionId": payload.baseVersionId,
                        "messageKey": payload.idempotencyKey,
                        "revisionPlan": execution.revision_plan,
                        "search": execution.proposal_diagnostics,
                    },
                    utc_now(),
                )

            if execution.proposed_rows is not None:
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
                        dump_json(describe_diff(current_rows, proposal_validation.rows)),
                        dump_json(proposal_validation.as_dict()),
                        assistant_turn_id,
                        payload.idempotencyKey,
                        utc_now(),
                    ),
                )

        return serialize_session(database, session_id)


def _retry_exhausted_execution(language, exception):
    if language == "zh-CN":
        message = (
            "我注意到你尝试了一次重新生成，非常抱歉，由于我的能力不足，我可能无法帮助你进行这个修改，"
            "请你根据我们商量好的方案进行自主修改。"
        )
    else:
        message = (
            "I noticed that you tried generating the revision again. I am sorry, but this "
            "request appears to be beyond what I can reliably produce. Please make the change "
            "yourself using the direction we discussed."
        )
    return LLMExecutionResult(
        assistant_message=message,
        attempts_used=exception.attempts_used,
        request_id=exception.request_id,
        model="deterministic-retry-exhausted-guidance",
        latency_ms=0,
        guidance={
            "move": "offer_perspective",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "uiCues": [],
        },
    )


def _verified_proposal_message(language):
    if language == "zh-CN":
        return (
            "我把这次地图提案整理好了，也逐格核对了前后的真实变化。它现在仍是一份"
            "等你审查的方案；我更想让你先看高亮位置是否真的回应了刚才的方向，再决定"
            "要不要接受。"
        )

    return (
        "I have organized this map proposal and checked its real before/after tile changes. "
        "It is still yours to review; I would first look at whether the highlighted cells "
        "really answer the direction we discussed before deciding whether to accept it."
    )


def _legacy_proposal_relaxation_fallback_after_failure(
    *,
    session_id,
    access_cookie,
    base_version_id,
    idempotency_key,
    revision_state,
    revision_brief,
    language,
    exception,
):
    if (
        revision_state != "authorized"
        or exception.code != "MODEL_RESPONSE_INVALID"
        or exception.attempts_used < PROPOSAL_GENERATION_ATTEMPTS
        or not str(revision_brief or "").strip()
    ):
        return None

    original_brief = str(revision_brief).strip()
    brief_hash = hashlib.sha256(original_brief.encode("utf-8")).hexdigest()[:20]
    failure_payload = {
        "baseVersionId": base_version_id,
        "messageKey": idempotency_key,
        "briefHash": brief_hash,
        "code": exception.code,
        "attemptsUsed": exception.attempts_used,
    }

    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, base_version_id)
        already_offered = database.execute(
            """
            SELECT 1 FROM audit_events
            WHERE session_id = ? AND event_type = 'proposal_relaxation_offered'
              AND json_extract(payload_json, '$.baseVersionId') = ?
              AND json_extract(payload_json, '$.messageKey') = ?
              AND json_extract(payload_json, '$.briefHash') = ?
            LIMIT 1
            """,
            (session_id, base_version_id, idempotency_key, brief_hash),
        ).fetchone()

        if already_offered is not None:
            return None

        record_event(
            database,
            session_id,
            "proposal_generation_failed",
            failure_payload,
            utc_now(),
        )
        record_event(
            database,
            session_id,
            "proposal_relaxation_offered",
            {
                **failure_payload,
                "failedRequests": 1,
                "internalAttempts": exception.attempts_used,
            },
            utc_now(),
        )

    relaxed_brief = _build_relaxed_revision_brief(original_brief)
    if language == "zh-CN":
        message = (
            "我已经在这一次请求里自动完成了三轮地图生成，但仍没有一份方案"
            "同时产生符合原要求的真实改动并通过可解性检查。你不需要连续点击重试，我也不想"
            "为了通过校验偷偷改一个无关格子。\n\n"
            "我可以启用一次后备标准：你的核心修改方向、可解性、地图外壳和未涉及区域全部"
            "保持不变，只把“一份方案必须同时兑现所有预期效果”放宽为“先兑现其中一个可以"
            "通过游玩验证的局部效果”。你愿意让我按这个后备标准再生成一次吗？"
        )
        warning = (
            "这一次请求内部的三轮生成都没有得到同时符合原要求、包含真实格子变化且可解的地图。继续原样"
            "重试很可能只会重复失败；这是生成可靠性风险，不代表你的设计要求本身有问题。"
        )
    else:
        message = (
            "I automatically completed three map-generation attempts inside this one request, "
            "and still do not have a proposal that both makes the requested real changes and "
            "passes solvability validation. You do not need to click retry repeatedly, and I do not want to "
            "quietly change an unrelated tile just to pass validation.\n\n"
            "I can use one fallback standard: keep your core direction, solvability, the outer "
            "shell, and every unrelated area unchanged, while relaxing only the requirement that "
            "one proposal realize every expected effect at once. May I try once more by realizing "
            "one local, play-testable effect first?"
        )
        warning = (
            "All three internal generation attempts produced no map that both made real cell changes and met the "
            "original requirement while remaining solvable. Repeating it unchanged is likely to "
            "fail again; this is a generation-reliability risk, not evidence that your design "
            "request is wrong."
        )
    return LLMExecutionResult(
        assistant_message=message,
        attempts_used=exception.attempts_used,
        request_id=exception.request_id,
        model="deterministic-relaxation-offer",
        latency_ms=0,
        guidance={
            "move": "challenge_tradeoff",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "uiCues": [{"type": "warning", "text": warning}],
            "relaxationOffer": {
                "status": "awaiting_confirmation",
                "originalBrief": original_brief,
                "relaxedBrief": relaxed_brief,
                "baseVersionId": base_version_id,
                "briefHash": brief_hash,
            },
        },
    )


def _proposal_relaxation_fallback_after_failure(
    *,
    session_id,
    access_cookie,
    base_version_id,
    idempotency_key,
    revision_state,
    revision_brief,
    language,
    exception,
):
    if (
        revision_state != "authorized"
        or exception.code != "PROPOSAL_SEARCH_EXHAUSTED"
        or not str(revision_brief or "").strip()
    ):
        return None

    original_brief = str(revision_brief).strip()
    brief_hash = hashlib.sha256(original_brief.encode("utf-8")).hexdigest()[:20]
    revision_plan = getattr(exception, "revision_plan", {}) or {}
    diagnostics = getattr(exception, "proposal_diagnostics", {}) or {}
    failure_payload = {
        "baseVersionId": base_version_id,
        "messageKey": idempotency_key,
        "briefHash": brief_hash,
        "code": exception.code,
        "attemptsUsed": exception.attempts_used,
        "revisionPlan": revision_plan,
        "search": diagnostics,
    }

    with connect(immediate=True) as database:
        session = require_active_session(database, session_id, access_cookie)
        require_current_base(session, base_version_id)
        already_offered = database.execute(
            """
            SELECT 1 FROM audit_events
            WHERE session_id = ? AND event_type = 'proposal_relaxation_offered'
              AND json_extract(payload_json, '$.baseVersionId') = ?
              AND json_extract(payload_json, '$.messageKey') = ?
              AND json_extract(payload_json, '$.briefHash') = ?
            LIMIT 1
            """,
            (session_id, base_version_id, idempotency_key, brief_hash),
        ).fetchone()
        if already_offered is not None:
            return None
        now = utc_now()
        record_event(database, session_id, "proposal_search_failed", failure_payload, now)
        record_event(
            database,
            session_id,
            "proposal_relaxation_offered",
            {**failure_payload, "failedRequests": 1},
            now,
        )

    relaxed_brief = _build_relaxed_revision_brief(original_brief)
    constructed = int(diagnostics.get("constructedCandidates") or 0)
    if language == "zh-CN":
        message = (
            f"我已经按照刚才确认的方向完成了语义修改搜索，并检查了{constructed}个局部候选，"
            "但还没有找到一份既满足全部要求、包含真实变化又能通过求解验证的地图。"
            "当前 Stage 没有被修改，你也不需要原样反复点击重试。\n\n"
            "我可以采用一次后备标准：保留你的核心方向、可解性、地图外壳、明确禁止事项和"
            "未涉及区域，只把“同时实现全部次要效果”放宽为“先实现一个连贯、可试玩的局部效果”。"
            "你愿意让我按这个边界继续吗？"
        )
        warning = (
            "确定性搜索没有找到同时满足当前全部条件的可解修改。这表示当前修改范围内的可行候选不足，"
            "不代表你的设计方向有问题，也不会影响原有可解 Stage。"
        )
    else:
        message = (
            f"I completed the semantic revision search and checked {constructed} local candidates, "
            "but none both satisfied the complete request, made a real change, and passed the solver. "
            "The current Stage is unchanged, and repeating the same request is unnecessary.\n\n"
            "I can use one fallback standard: preserve your core direction, solvability, outer shell, "
            "explicit prohibitions, and unrelated areas, while realizing one coherent, play-testable "
            "local effect instead of every secondary effect at once. May I continue on that basis?"
        )
        warning = (
            "Deterministic search found no solvable revision satisfying every current condition. "
            "This indicates a narrow feasible search space, not a problem with your design direction, "
            "and the original solvable Stage remains unchanged."
        )
    return LLMExecutionResult(
        assistant_message=message,
        attempts_used=exception.attempts_used,
        request_id=exception.request_id,
        model="deterministic-search-relaxation-offer",
        latency_ms=0,
        guidance={
            "move": "challenge_tradeoff",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
            "uiCues": [{"type": "warning", "text": warning}],
            "relaxationOffer": {
                "status": "awaiting_confirmation",
                "originalBrief": original_brief,
                "relaxedBrief": relaxed_brief,
                "baseVersionId": base_version_id,
                "briefHash": brief_hash,
            },
        },
        revision_plan=revision_plan,
        proposal_diagnostics=diagnostics,
    )


def _build_relaxed_revision_brief(original_brief):
    return (
        f"Preserve this designer-authored core direction exactly: {original_brief!r}. "
        "Implement one coherent, play-testable local effect that advances that direction. "
        "It is not necessary for this single proposal to realize every secondary route, rhythm, "
        "or difficulty effect at once. Do not change an unrelated component or area."
    )


def _relaxed_revision_suggestion_execution(stage_context, language, request_id):
    previous = (
        ((stage_context or {}).get("recentGuidance") or {}).get("relaxationOffer") or {}
    )
    original_brief = str(previous.get("originalBrief") or "").strip()
    relaxed_brief = str(previous.get("relaxedBrief") or "").strip()
    if not relaxed_brief:
        raise ApiError(409, "RELAXATION_OFFER_EXPIRED", "The fallback offer is no longer available.")

    if language == "zh-CN":
        summary = "先落实一个可试玩的局部效果"
        rationale = (
            "降低后的修改要求：保留你原来的核心方向、地图可解性、外壳、明确禁止事项和未涉及区域；"
            "这一版不再要求同时实现全部次要路线、节奏或难度效果，只先完成其中一个连贯、可试玩、"
            "能够通过实际游玩判断的局部效果。"
        )
        message = (
            "可以，我们先把后备方案说清楚，但我现在还不会直接改图。\n\n"
            f"原始方向仍然保留：{original_brief}\n\n"
            f"{rationale} 如果这个边界符合你的意思，可以点击下方“请助手具体生成这个方案”；"
            "只有那一步之后，我才会生成并展示具体的修改地图。"
        )
    else:
        summary = "Realize one local, play-testable effect first"
        rationale = (
            "Relaxed requirement: preserve your original core direction, solvability, outer shell, "
            "explicit prohibitions, and every unrelated area. This proposal no longer has to realize "
            "all secondary route, rhythm, or difficulty effects at once; it should first implement one "
            "coherent local effect that can be judged through play."
        )
        message = (
            "Agreed. Let us define the fallback proposal first; I will not change the map yet.\n\n"
            f"Your original direction remains: {original_brief}\n\n"
            f"{rationale} If that boundary matches what you mean, use “Ask the assistant to draft this ”"
            "option below. Only that separate request will generate and display a concrete map."
        )

    return LLMExecutionResult(
        assistant_message=message,
        attempts_used=0,
        request_id=request_id,
        model="deterministic-relaxed-suggestion",
        latency_ms=0,
        guidance={
            "move": "offer_revision",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": {"summary": summary, "rationale": rationale},
            "uiCues": [],
            "relaxationOffer": {
                "status": "suggestion_ready",
                "originalBrief": original_brief,
                "relaxedBrief": relaxed_brief,
                "baseVersionId": previous.get("baseVersionId"),
                "briefHash": previous.get("briefHash"),
            },
        },
    )


def _proposal_review_guidance(guidance, language):
    source = dict(guidance or {})
    warning = next(
        (
            dict(cue)
            for cue in source.get("uiCues") or []
            if cue.get("type") in {"warning", "tradeoff"} and cue.get("text")
        ),
        None,
    )
    manual_text = (
        "这份提案会先保持待审查状态。如果你更想亲手微调，可以先拒绝它，再在右侧编辑器"
        "沿着高亮区域继续改；我会把你的实际修改当作下一轮共同判断的依据。"
        if language == "zh-CN"
        else (
            "This proposal stays pending while you review it. If you would rather tune it "
            "yourself, reject it first and continue around the highlighted area in the "
            "right-hand editor; I will use your actual edit as our next shared evidence."
        )
    )
    # A first-person designer stance is still useful while reviewing a concrete
    # proposal.  Keep its correctable intent card instead of hiding it merely
    # because the proposal also has a manual-review card.
    source["intentHypothesis"] = source.get("intentHypothesis") or None
    source["intentConfidence"] = (
        source.get("intentConfidence") if source.get("intentHypothesis") else None
    )
    source["followUpQuestion"] = None
    source["proposalOffer"] = None
    source["uiCues"] = [
        {"type": "manual_edit", "text": manual_text},
        *([warning] if warning is not None else []),
    ]
    return source


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
        verified_summary = None

        if payload.decision == "accept":
            proposed_rows = load_json(proposal["proposed_rows_json"])
            current = get_current_version(database, session)
            current_rows = load_json(current["rows_json"])
            validation = _solve_changed_proposal_or_api_error(
                current_rows,
                proposed_rows,
            )
            verified_summary = summarize_verified_diff(
                current_rows,
                validation.rows,
                session["language"],
            )
            new_version_id = _insert_version(
                database,
                session,
                validation,
                "llm_accepted",
                verified_summary,
                "proposal:" + payload.idempotencyKey,
                current,
            )

        database.execute(
            """
            UPDATE change_proposals
            SET status = ?, decided_at = ?, summary = COALESCE(?, summary)
            WHERE id = ?
            """,
            (
                "accepted" if payload.decision == "accept" else "rejected",
                now,
                verified_summary,
                proposal_id,
            ),
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
            "designerIntention": None,
        }

        if session["status"] == "completed" and session["final_version_id"]:
            version = get_version(database, session_id, session["final_version_id"])
            payload["finalRows"] = load_json(version["rows_json"])
            intention = database.execute(
                "SELECT content FROM designer_intentions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            payload["designerIntention"] = intention["content"] if intention else None

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


def _pending_assistant_translations(database, session_id, language, turn_ids):
    unique_turn_ids = list(dict.fromkeys(turn_ids))

    if not unique_turn_ids or len(unique_turn_ids) > 8:
        raise ApiError(
            400,
            "INVALID_TRANSLATION_REQUEST",
            "Translation requests require between 1 and 8 unique turnIds.",
        )

    for turn_id in unique_turn_ids:
        _validate_identifier(turn_id, "turnId")

    placeholders = ",".join("?" for _ in unique_turn_ids)
    rows = database.execute(
        f"""
        SELECT turn.id, turn.content, turn.guidance_json, proposal.summary
        FROM conversation_turns AS turn
        LEFT JOIN change_proposals AS proposal
          ON proposal.assistant_turn_id = turn.id
        LEFT JOIN turn_translations AS translation
          ON translation.turn_id = turn.id AND translation.language = ?
        WHERE turn.session_id = ?
          AND turn.role = 'assistant'
          AND turn.language != ?
          AND translation.id IS NULL
          AND turn.id IN ({placeholders})
        ORDER BY turn.sequence_number
        """,
        (language, session_id, language, *unique_turn_ids),
    ).fetchall()
    items = []

    for row in rows:
        guidance = load_json(row["guidance_json"]) or {}
        proposal_offer = guidance.get("proposalOffer") or {}
        ui_cues = guidance.get("uiCues") or []
        excluded_suffixes = [
            *[str(cue.get("text") or "").strip() for cue in ui_cues],
            str(guidance.get("followUpQuestion") or "").strip(),
        ]
        body = str(row["content"] or "").rstrip()

        for suffix in reversed([value for value in excluded_suffixes if value]):
            if body.endswith(suffix):
                body = body[: -len(suffix)].rstrip()

        items.append(
            {
                "turnId": row["id"],
                "body": body,
                "followUpQuestion": guidance.get("followUpQuestion"),
                "intentHypothesis": guidance.get("intentHypothesis"),
                "proposalOfferSummary": proposal_offer.get("summary"),
                "proposalOfferRationale": proposal_offer.get("rationale"),
                "uiCueTexts": [cue.get("text") for cue in ui_cues],
                "proposalSummary": row["summary"],
                "guidance": guidance,
            }
        )

    return items


def _translated_guidance(source_guidance, translated):
    guidance = dict(source_guidance or {})
    guidance["followUpQuestion"] = translated["followUpQuestion"]
    guidance["intentHypothesis"] = translated["intentHypothesis"]
    source_offer = guidance.get("proposalOffer")

    if source_offer:
        guidance["proposalOffer"] = {
            **source_offer,
            "summary": translated["proposalOfferSummary"],
            "rationale": translated["proposalOfferRationale"],
        }

    translated_cues = translated["uiCueTexts"]
    guidance["uiCues"] = [
        {**cue, "text": translated_cues[index]}
        for index, cue in enumerate(guidance.get("uiCues") or [])
    ]
    return guidance


def build_llm_context(database, session_id, version):
    session = database.execute(
        "SELECT initial_draft_method, language FROM design_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    turns = database.execute(
        """
        SELECT id, role, content, guidance_json FROM conversation_turns
        WHERE session_id = ? AND version_id = ?
        ORDER BY sequence_number DESC LIMIT 24
        """,
        (session_id, version["id"]),
    ).fetchall()
    accepted_opening = database.execute(
        """
        SELECT proposal.id AS proposal_id, proposal.assistant_turn_id,
               turn.role, turn.content, turn.guidance_json
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
    parent_rows = load_json(parent["rows_json"]) if parent is not None else None
    change_summary = (
        summarize_stage_changes(parent_rows, current_rows)
        if parent_rows is not None
        else None
    )
    conversation = [
        {"role": turn["role"], "content": turn["content"]}
        for turn in reversed(turns)
        if turn["id"] not in superseded_assessment_turn_ids
    ]
    recent_guidance = {
        "discussionFocus": None,
        "discussionFocusHistory": [],
        "intentHypothesis": None,
        "proposalOffer": None,
        "relaxationOffer": next(
            (
                (load_json(turn["guidance_json"]) or {}).get("relaxationOffer")
                for turn in turns
                if turn["role"] == "assistant"
            ),
            None,
        ),
        "uiCues": {},
    }
    guidance_sources = [
        load_json(turn["guidance_json"]) or {}
        for turn in turns
        if turn["id"] not in superseded_assessment_turn_ids
    ]

    if accepted_opening is not None:
        guidance_sources.append(load_json(accepted_opening["guidance_json"]) or {})

    for guidance in guidance_sources:
        discussion_focus = guidance.get("followUpQuestion")
        if discussion_focus:
            if recent_guidance["discussionFocus"] is None:
                recent_guidance["discussionFocus"] = discussion_focus
            if (
                len(recent_guidance["discussionFocusHistory"]) < 3
                and discussion_focus not in recent_guidance["discussionFocusHistory"]
            ):
                recent_guidance["discussionFocusHistory"].append(discussion_focus)
        if (
            recent_guidance["intentHypothesis"] is None
            and guidance.get("intentHypothesis")
        ):
            recent_guidance["intentHypothesis"] = guidance["intentHypothesis"]
        if recent_guidance["proposalOffer"] is None and guidance.get("proposalOffer"):
            recent_guidance["proposalOffer"] = guidance["proposalOffer"]
        for cue in guidance.get("uiCues") or []:
            cue_type = cue.get("type")
            if cue_type == "tradeoff":
                cue_type = "warning"
            if (
                cue_type in {"warning", "manual_edit"}
                and cue_type not in recent_guidance["uiCues"]
            ):
                recent_guidance["uiCues"][cue_type] = {
                    "text": cue.get("text"),
                    "evidenceSignature": guidance.get("evidenceSignature"),
                }
        if (
            recent_guidance["intentHypothesis"]
            and recent_guidance["proposalOffer"]
            and len(recent_guidance["uiCues"]) == 2
        ):
            break

    latest_user_direction = _latest_substantive_design_direction(turns)
    evidence_payload = {
        "versionId": version["id"],
        "play": dict(latest_play) if latest_play is not None else None,
        "direction": latest_user_direction,
    }
    guidance_evidence_signature = hashlib.sha256(
        dump_json(evidence_payload).encode("utf-8")
    ).hexdigest()[:16]

    if accepted_opening is not None and not any(
        turn["content"] == _verified_proposal_message(session["language"])
        and turn["role"] == accepted_opening["role"]
        for turn in conversation
    ):
        conversation.insert(
            0,
            {
                "role": accepted_opening["role"],
                "content": _verified_proposal_message(session["language"]),
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
            "mapFacts": build_map_facts(current_rows, parent_rows),
            "recentGuidance": recent_guidance,
            "guidanceEvidenceSignature": guidance_evidence_signature,
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


def _latest_substantive_design_direction(turns):
    design_terms = (
        "水",
        "箱",
        "目标",
        "墙",
        "通道",
        "路线",
        "推动",
        "绕行",
        "难度",
        "节奏",
        "water",
        "box",
        "crate",
        "target",
        "wall",
        "corridor",
        "route",
        "push",
        "difficulty",
        "pacing",
    )
    generic_continuations = {
        "继续",
        "展开",
        "展开讲讲",
        "可以",
        "好的",
        "好",
        "行",
        "试试",
        "continue",
        "go on",
        "okay",
        "ok",
        "yes",
    }

    for turn in turns:
        if turn["role"] != "user":
            continue
        content = turn["content"].strip().casefold()
        normalized = re.sub(r"[\s,.!?。！？]+", "", content)
        if not content or normalized in generic_continuations:
            continue
        if any(term in content for term in design_terms):
            return content
    return ""


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


def _validate_changed_proposal(base_rows, proposed_rows):
    validation = validate_and_solve(proposed_rows)

    if not describe_diff(base_rows, validation.rows):
        raise LevelValidationError(
            "UNCHANGED_PROPOSAL",
            "The proposed map must change at least one tile from the current Stage.",
        )

    return validation


def _solve_changed_proposal_or_api_error(base_rows, proposed_rows):
    try:
        return _validate_changed_proposal(base_rows, proposed_rows)
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
