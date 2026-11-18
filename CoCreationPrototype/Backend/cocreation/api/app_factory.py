import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import quote
import httpx
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
    build_entity_bindings,
    build_untrusted_entity_bindings,
    build_map_facts,
    build_stage_snapshot,
    analyze_user_map_claims,
    format_map_claim_correction,
    derive_entity_transitions,
    entity_binding_fingerprint,
    entity_bindings_match_rows,
    LevelValidationError,
    describe_diff,
    summarize_verified_diff,
    summarize_stage_changes,
    validate_and_solve,
)
from demo_level_generator import generate_demo_level
from conversation_evidence import build_dialogue_evidence, with_latest
import component_recovery
import proposal_options
import binary_discovery
import binary_routes
from reply_reliability import CURRENT_REPLY_TRACE, REPLY_PIPELINE_VERSION, ReplyTrace, trace_reply, failure_reason
from proposal_diagnostics import (ProposalDiagnostic, CURRENT_PROPOSAL_DIAGNOSTIC, proposal_phase, trace_proposal)
from llm_client import validate_ai_edit_diff
from llm_client import (
    LLMExecutionResult,
    LLMServiceError,
    LLM_INTERNAL_DEADLINE_SECONDS,
    PROMPT_VERSION,
    PROPOSAL_GENERATION_ATTEMPTS,
    classify_challenge_reason,
    challenge_choice_card_copy,
    classify_revision_request,
    classify_turn_understanding,
    recover_human_edit_engagement,
    _dialogue_background_prompt,
    _strip_unverified_state_claims,
    review_intent_feedback,
    review_question_answers,
    rewrite_intent_progress,
    _contains_user_design_direction,
    _guidance_advice_request,
    _guidance_confusion_request,
    execute_revision_operations,
    generate_chat_reply,
    generate_stage_assessment,
    normalize_revision_plan,
    translate_turns,
    validate_execution_brief,
    _repair_stage_one_opening_display,
    _log_llm_event,
)
from repository import (
    DATABASE_PATH,
    connect,
    delete_demo_sessions,
    dump_json,
    get_current_version,
    get_session,
    get_version,
    load_entity_bindings,
    load_design_context,
    load_json,
    map_fingerprint,
    next_stage_number,
    next_turn_sequence,
    record_event,
    save_design_context,
    serialize_session,
)
from design_context import (
    add_confirmed_decision,
    add_open_question,
    apply_question_answer_review,
    apply_question_feedback,
    add_rejected_decision,
    clone_design_context,
    design_level_open_questions,
    evaluator_projection,
    empty_design_context,
    extract_explicit_user_memory,
    infer_intent_topic,
    is_design_level_question,
    merge_intent_hypothesis,
    merge_chat_update,
    resolve_intent_hypothesis,
    question_dedup_key,
    revision_projection,
    set_active_disagreement,
    sanitize_user_design_text,
)
from design_requirements import protected_change_requested, tradeoff_message
from proposal_understanding import bind_latest, protected_request_message, validate_topic, designer_sources, reusable_understanding
from revision_workflow import (
    SemanticConstraintError,
    validate_semantic_constraints,
)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import routes_messages_and_proposals, routes_sessions
from .config_models import (
    ALLOWED_ORIGINS,
    ApiError,
    BACKEND_DIR,
    FRONTEND_DIR,
    HOST,
    PORT,
    REQUEST_ID_PATTERN,
    _llm_failure_details,
    error_response,
)
from .online_sync_and_dashboard import _dashboard_node_entry
from .session_persistence_helpers import _draft_regeneration_sweeper


@asynccontextmanager
async def app_lifespan(_app):
    sweeper = asyncio.create_task(_draft_regeneration_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(
    title="Sokoban Co-Creation Prototype",
    version="1.0.0",
    lifespan=app_lifespan,
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
    request.state.llm_started_at = time.monotonic()
    request.state.llm_deadline = request.state.llm_started_at + LLM_INTERNAL_DEADLINE_SECONDS
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Reply-Pipeline-Version"] = REPLY_PIPELINE_VERSION
    if request.url.path in {"/", "/index.html", "/cocreation/", "/cocreation/index.html"}:
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
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
        request.state.request_id,
        exception.retryable,
        _llm_failure_details(exception),
    )
    response.headers["X-LLM-Attempts-Used"] = str(exception.attempts_used)
    return response


app.include_router(routes_sessions.router)
app.include_router(routes_messages_and_proposals.router)
app.mount(
    "/",
    StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
    name="prototype-frontend",
)

from . import (
    config_models,
    context_and_discovery,
    online_sync_and_dashboard,
    proposal_and_challenge,
    session_persistence_helpers,
)
for _mod in (
    config_models,
    session_persistence_helpers,
    online_sync_and_dashboard,
    context_and_discovery,
    proposal_and_challenge,
    routes_sessions,
    routes_messages_and_proposals,
):
    for _k, _v in vars(_mod).items():
        if not (_k.startswith('__') and _k.endswith('__')) and _k not in {'app', 'router'}:
            globals()[_k] = _v
del _mod, _k, _v
