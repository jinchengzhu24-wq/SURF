"""Zero-paid-LLM tests for co-creation session, stage, read-model, and dialogue services."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from server.application.dialogue import (
    BinaryDiscoveryHandler,
    ChallengeHandler,
    IntentService,
    MessageOrchestrator,
    OrdinaryChatHandler,
    ProposalService,
    RecoveryService,
    RevisionCardHandler,
    TranslationService,
)
from server.application.rooms.service import OnlineRoomService
from server.application.sessions import (
    CoCreationAppSettings,
    SessionLifecycleService,
    SessionReadModelService,
    StageService,
)
from server.core.clock import FakeClock
from server.core.llm_client import TransportResponse, UpstreamLLMError
from server.domain.errors import ConflictError, IdempotencyConflictError, NotFoundError
from server.domain.memory import ensure_design_context, set_active_disagreement
from server.domain.proposals import BinaryDiscoveryState, ProposalSearchExhausted
from server.llm.pipelines.binary import BinaryDiscoveryPipeline
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.proposal import ProposalPipeline
from server.llm.pipelines.translation import TranslationPipeline
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.persistence import (
    DialogueRepository,
    OperationalRepository,
    RoomsRepository,
    SessionsRepository,
    SQLiteDatabase,
    dump_json,
)

BASE_ROWS: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@.....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

ALT_ROWS: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#..........#",
    "#....@.....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


class _FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> TransportResponse:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        content = item(kwargs) if callable(item) else item
        return TransportResponse(content=content, finish_reason="stop")


class _TxTrackingDB(SQLiteDatabase):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.in_write_tx = False

    @contextmanager
    def write_tx(self):
        self.in_write_tx = True
        try:
            with super().write_tx() as conn:
                yield conn
        finally:
            self.in_write_tx = False


def _build_env(
    tmp_path: Path,
    responses: list[Any] | None = None,
    *,
    token_secret: str = "test-strong-secret",
):
    clock = FakeClock()
    db = _TxTrackingDB(tmp_path / "sokoban.sqlite3")
    settings = CoCreationAppSettings(
        token_secret=token_secret,
        cocreation_deadline_seconds=900.0,
        demo_ttl_seconds=86400.0,
    )
    rooms_repo = RoomsRepository(db)
    sessions_repo = SessionsRepository(db)
    dialogue_repo = DialogueRepository(db)
    op_repo = OperationalRepository(db)
    room_sync = OnlineRoomService(rooms_repo, clock=clock)
    read_model = SessionReadModelService(
        sessions_repo, dialogue_repo, op_repo, clock=clock, settings=settings
    )
    transport = _FakeTransport(responses or [])
    chat_pipe = ChatPipeline(transport, model="kimi-fake", clock=clock)
    und_pipe = UnderstandingPipeline(transport, model="kimi-fake", clock=clock)
    bin_pipe = BinaryDiscoveryPipeline(transport, model="kimi-fake", clock=clock)
    prop_pipe = ProposalPipeline(transport, model="kimi-fake", clock=clock)
    tr_pipe = TranslationPipeline(transport, model="kimi-fake", clock=clock)

    lifecycle = SessionLifecycleService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
        demo_level_generator=lambda: SimpleNamespace(rows=tuple(BASE_ROWS), seed=42),
    )
    stage = StageService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        chat_pipeline=chat_pipe,
        understanding_pipeline=und_pipe,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
        demo_level_generator=lambda: SimpleNamespace(rows=tuple(ALT_ROWS), seed=99),
    )
    ordinary = OrdinaryChatHandler(db, chat_pipe, room_sync=room_sync, clock=clock)
    binary = BinaryDiscoveryHandler(
        db, op_repo, bin_pipe, prop_pipe, room_sync=room_sync, clock=clock
    )
    revision = RevisionCardHandler(db, prop_pipe, room_sync=room_sync, clock=clock)
    challenge = ChallengeHandler(db, und_pipe, room_sync=room_sync, clock=clock)
    orchestrator = MessageOrchestrator(
        db,
        sessions_repo,
        op_repo,
        read_model,
        ordinary_handler=ordinary,
        binary_handler=binary,
        revision_handler=revision,
        challenge_handler=challenge,
        understanding_pipeline=und_pipe,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
    )
    proposal_svc = ProposalService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        binary,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
    )
    intent_svc = IntentService(db, sessions_repo, read_model, clock=clock, settings=settings)
    translation_svc = TranslationService(
        sessions_repo, dialogue_repo, read_model, tr_pipe, clock=clock, settings=settings
    )
    recovery_svc = RecoveryService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        chat_pipeline=chat_pipe,
        room_sync=room_sync,
        clock=clock,
        settings=settings,
    )
    return {
        "clock": clock,
        "db": db,
        "rooms_repo": rooms_repo,
        "sessions_repo": sessions_repo,
        "dialogue_repo": dialogue_repo,
        "op_repo": op_repo,
        "room_sync": room_sync,
        "read_model": read_model,
        "transport": transport,
        "lifecycle": lifecycle,
        "stage": stage,
        "orchestrator": orchestrator,
        "proposal": proposal_svc,
        "intent": intent_svc,
        "translation": translation_svc,
        "recovery": recovery_svc,
    }


def _extract_hash_param(url: str, key: str) -> str:
    fragment = urlparse(url).fragment
    return parse_qs(fragment)[key][0]


def _extract_query_param(url: str, key: str) -> str:
    return parse_qs(urlparse(url).query)[key][0]


def _opening_json(msg: str = "Stage 1 ready.") -> str:
    return json.dumps(
        {
            "assistantMessage": msg,
            "move": "observe_stage",
            "followUpQuestion": None,
            "intentDecision": None,
            "intentConfidence": None,
            "revisionOffer": None,
            "disagreement": None,
            "uiCues": [],
            "coordinateLinks": [],
            "assessment": {
                "solutionSummary": "Direct push from (3,6) to (3,9).",
                "difficultyOpinion": "Straightforward layout.",
                "features": ["Open center"],
                "suggestions": ["Consider adding a small detour."],
                "satisfactionQuestion": None,
            },
            "designContextPatch": None,
        }
    )


def _routing_json(target: str = "ordinary", *, evidence_span: str = "the center") -> str:
    return json.dumps(
        {
            "acts": ["evaluation"],
            "elements": ["internal_walls"],
            "mapRelated": True,
            "directionSufficient": target != "binary_discovery",
            "routeTarget": target,
            "evidenceSpan": evidence_span,
            "changes": [],
            "humanEditEngagement": None,
        }
    )


def _chat_json(
    msg: str = "Let us keep the path clear.",
    *,
    hyp: str = "",
    evidence_span: str = "the center",
) -> str:
    return json.dumps(
        {
            "assistantMessage": msg,
            "move": "clarify_intent",
            "followUpQuestion": "Do you prefer a wider center?",
            "intentDecision": (
                {
                    "classification": "candidate",
                    "claims": [
                        {
                            "normalizedMeaning": "Widen the center area",
                            "subjectType": "space",
                            "subjectText": "center",
                            "attributeType": "clarity",
                            "attributeText": "openness",
                            "direction": "increase",
                            "degree": "neutral",
                            "aspect": "gameplay",
                            "scopeType": "region",
                            "scopeText": "center",
                            "evidenceSpan": evidence_span,
                        }
                    ],
                    "cardText": hyp,
                }
                if hyp
                else None
            ),
            "intentConfidence": "medium" if hyp else None,
            "revisionOffer": None,
            "disagreement": None,
            "uiCues": [],
            "coordinateLinks": [],
            "assessment": None,
            "designContextPatch": {
                "goals": [{"text": "Keep route brisk", "evidenceText": msg}],
                "constraints": [],
                "openQuestions": [
                    {
                        "question": "Should water stay centered?",
                        "status": "open",
                        "evidenceText": msg,
                    }
                ],
            },
        }
    )


async def test_formal_lifecycle_hashing_warning_and_atomic_room_sync(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        default_settings = CoCreationAppSettings()
        assert default_settings.token_secret_bytes
    assert any("TOKEN_SECRET" in rec.message for rec in caplog.records)

    env = _build_env(tmp_path)
    room_p1 = await env["room_sync"].create_room(study_session_id="s1", menu_started_flow=True)
    await env["room_sync"].join_room(
        room_code=room_p1["roomCode"], study_session_id="s2", menu_started_flow=True
    )
    match_id = room_p1["matchId"]

    created = await env["lifecycle"].create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="create-1",
        match_id=match_id,
        player_number=1,
    )
    sid = created["sessionId"]
    bootstrap = _extract_hash_param(created["launchUrl"], "bootstrap")
    int_token = created["integrationToken"]

    rec = env["sessions_repo"].get_session(sid)
    assert rec is not None
    assert rec.secret_hash != "" and rec.access_token_hash != ""
    assert bootstrap not in (rec.bootstrap_token_hash or "")
    assert int_token not in (rec.integration_token_hash or "")

    auth = await env["lifecycle"].authorize_browser_access(sid, bootstrap)
    access_token = auth["accessToken"]
    with pytest.raises(ConflictError):
        await env["lifecycle"].authorize_browser_access(sid, bootstrap)

    payload = await env["lifecycle"].update_language(sid, access_token, language="en")
    assert payload["languageLocked"] is True
    v1_id = payload["currentVersionId"]
    assert v1_id is not None

    # Read model works cleanly without touching LLM transport.
    rm_payload = await env["read_model"].get_session_payload(sid)
    assert rm_payload["sessionId"] == sid
    assert len(env["transport"].calls) == 0

    fin = await env["lifecycle"].finalize_session(
        sid, access_token, base_version_id=v1_id, idempotency_key="fin-1"
    )
    assert fin["status"] == "awaiting_intention"

    done = await env["lifecycle"].submit_intention(
        sid, access_token, content="Short brisk puzzle.", idempotency_key="int-1"
    )
    assert done["status"] == "completed"
    assert done["intention"]["content"] == "Short brisk puzzle."

    room_after = env["rooms_repo"].get_room(match_id)
    assert room_after is not None
    assert room_after.players[0]["designerIntention"] == "Short brisk puzzle."
    events = env["rooms_repo"].list_room_events(match_id)
    event_types = [ev.event_type for ev in events]
    assert "first_stage" in event_types
    assert "final" in event_types
    assert "designer_intention_synchronized" in event_types


async def test_demo_ttl_cleanup_preserves_active_and_prunes_expired(tmp_path: Path) -> None:
    env = _build_env(tmp_path)
    clock: FakeClock = env["clock"]

    d1 = await env["lifecycle"].create_demo_session(language="en", idempotency_key="demo-1")
    clock.advance(3600.0)  # +1h: d1 still fresh (TTL 24h)
    d2 = await env["lifecycle"].create_demo_session(language="zh-CN", idempotency_key="demo-2")

    assert env["sessions_repo"].get_session(d1["sessionId"]) is not None
    assert env["sessions_repo"].get_session(d2["sessionId"]) is not None

    clock.advance(86400.0)  # Past 24h for both d1 and d2
    d3 = await env["lifecycle"].create_demo_session(language="en", idempotency_key="demo-3")
    assert env["sessions_repo"].get_session(d1["sessionId"]) is None
    assert env["sessions_repo"].get_session(d2["sessionId"]) is None
    assert env["sessions_repo"].get_session(d3["sessionId"]) is not None
    with pytest.raises(NotFoundError):
        await env["read_model"].get_session_payload(d1["sessionId"])


async def test_stage_versions_outside_tx_play_attempts_and_draft_regen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_env(tmp_path, responses=[_opening_json()])
    db: _TxTrackingDB = env["db"]

    import server.application.sessions.config as sess_config

    orig_solve = sess_config.solve_stage_async
    tx_states_during_solve: list[bool] = []

    async def _checked_solve(rows: Any, *, clock: Any = None) -> Any:
        tx_states_during_solve.append(db.in_write_tx)
        return await orig_solve(rows, clock=clock)

    monkeypatch.setattr(sess_config, "solve_stage_async", _checked_solve)
    monkeypatch.setattr(
        "server.application.sessions.stage.service.solve_stage_async", _checked_solve
    )
    monkeypatch.setattr(
        "server.application.sessions.stage.stage_ops.solve_stage_async", _checked_solve
    )
    monkeypatch.setattr(
        "server.application.sessions.stage.draft_ops.solve_stage_async", _checked_solve
    )
    monkeypatch.setattr(
        "server.application.sessions.lifecycle.service.solve_stage_async", _checked_solve
    )
    monkeypatch.setattr(
        "server.application.sessions.lifecycle.language.solve_stage_async", _checked_solve
    )

    created = await env["lifecycle"].create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="f-stage",
    )
    sid = created["sessionId"]
    int_token = created["integrationToken"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]

    # Draft regeneration lifecycle (formal Unity mode: request -> claim -> complete & cancel/fail)
    req1 = await env["stage"].request_draft_regeneration(
        sid, access, idempotency_key="regen-1"
    )
    req_id = req1["draftPreview"]["regenerationRequestId"]
    await env["stage"].claim_draft_regeneration(sid, req_id, int_token)
    done_regen = await env["stage"].complete_draft_regeneration(
        sid, req_id, int_token, rows=ALT_ROWS
    )
    assert done_regen["status"] == "completed"

    env["clock"].advance(1.0)
    req2 = await env["stage"].request_draft_regeneration(
        sid, access, idempotency_key="regen-2"
    )
    req2_id = req2["draftPreview"]["regenerationRequestId"]
    await env["stage"].cancel_draft_regeneration(sid, req2_id, access)

    env["clock"].advance(1.0)
    req3 = await env["stage"].request_draft_regeneration(
        sid, access, idempotency_key="regen-3"
    )
    req3_id = req3["draftPreview"]["regenerationRequestId"]
    await env["stage"].fail_draft_regeneration(
        sid, req3_id, int_token, failure_code="unity_aborted"
    )

    # Lock language -> Stage 1, assess, save Stage 2, restore Stage 1 -> Stage 3
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]
    assessed = await env["stage"].assess_version(
        sid, v1, access, idempotency_key="assess-1"
    )
    assert any(t["role"] == "assistant" for t in assessed["turns"])

    p2 = await env["stage"].save_version(
        sid, access, base_version_id=v1, rows=BASE_ROWS, idempotency_key="save-2"
    )
    v2 = p2["currentVersionId"]
    assert v2 != v1

    p3 = await env["stage"].restore_version(
        sid, v1, access, base_version_id=v2, idempotency_key="restore-3"
    )
    v3 = p3["currentVersionId"]
    assert len(p3["versions"]) == 3
    assert v3 not in {v1, v2}
    assert tx_states_during_solve and all(not in_tx for in_tx in tx_states_during_solve)

    # Play attempts lifecycle
    pa = await env["stage"].create_play_attempt(sid, v3, access, idempotency_key="pa-1")
    attempt_id = pa["attemptId"]
    ticket = _extract_query_param(pa["playUrl"], "cocreationPlay")
    boot = await env["stage"].bootstrap_play_attempt(attempt_id, ticket)
    att_tok = boot["attemptToken"]
    await env["stage"].start_play_attempt(attempt_id, att_tok)
    await env["stage"].progress_play_attempt(attempt_id, att_tok, move_count=4, push_count=1)
    comp = await env["stage"].complete_play_attempt(
        attempt_id, att_tok, duration_seconds=6.5, move_count=8, push_count=2
    )
    assert comp["status"] == "completed"

    pa2 = await env["stage"].create_play_attempt(sid, v3, access, idempotency_key="pa-2")
    boot2 = await env["stage"].bootstrap_play_attempt(
        pa2["attemptId"], _extract_query_param(pa2["playUrl"], "cocreationPlay")
    )
    ab = await env["stage"].abandon_play_attempt(pa2["attemptId"], boot2["attemptToken"])
    assert ab["status"] == "abandoned"


def _extract_turn_id(kwargs: dict[str, Any], *, index: int = 0) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("initialDesignerTurns") or payload.get("designerTurns") or []
    return str(turns[index]["id"]) if turns else "turn-hint"


async def test_dialogue_idempotency_binary_proposal_intent_translation_recovery(
    tmp_path: Path,
) -> None:
    def _make_bin_ask(kwargs: dict[str, Any]) -> str:
        turn_id = _extract_turn_id(kwargs)
        return json.dumps(
            {
                "action": "ask",
                "question": "Which simplification works better?",
                "note": "Pick one obstacle change.",
                "branches": [
                    {
                        "label": "Remove internal wall",
                        "strategy": {
                            "effect": "adjust_internal_walls",
                            "focus": {"row": 4, "column": 6, "radius": 1},
                            "operators": ["remove_wall"],
                            "preserve": ["boxes", "targets", "player"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": {"row": 4, "column": 6, "radius": 1},
                        },
                    },
                    {
                        "label": "Remove water tile",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water"],
                            "preserve": ["boxes", "targets", "player"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": {
                    "requirements": [
                        {
                            "component": "wall",
                            "property": "count",
                            "relation": "decrease",
                            "strength": "goal",
                            "entities": [],
                            "value": None,
                            "unit": "none",
                            "priorityEvidenceSpan": "",
                            "focused": False,
                            "sourceTurnId": turn_id,
                            "evidenceSpan": "Make center simpler",
                            "statement": "Remove an obstacle in the center",
                            "scope": "all",
                        }
                    ],
                    "automaticBindings": [],
                    "exactTransitions": [],
                },
            }
        )

    bin_plan = json.dumps(
        {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [
                {
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["remove_wall"],
                    "preserve": ["boxes", "targets", "player"],
                    "editBudget": 1,
                }
            ],
            "protectedEntities": [],
            "initialRequirements": None,
        }
    )

    map_ops_json = json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove center wall",
                    "operations": [
                        {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                    ],
                }
            ]
        }
    )

    responses: list[Any] = [
        # 1. Ordinary chat + replay check
        _routing_json("ordinary"),
        _chat_json("Center route looks quick.", hyp="Designer prefers quick center access."),
        # 2. Binary discovery ask
        _make_bin_ask,
        # 3. Binary choice -> plan -> ProposalPipeline (uses precompiled strategy + map_operations)
        bin_plan,
        map_ops_json,
        # 4. Recovery retry (stage_opening) - translation responses queued dynamically below
        _opening_json("Recovered Stage opening."),
    ]
    env = _build_env(tmp_path, responses=responses)

    room = await env["room_sync"].create_room(study_session_id="sr1", menu_started_flow=True)
    await env["room_sync"].join_room(
        room_code=room["roomCode"], study_session_id="sr2", menu_started_flow=True
    )
    created = await env["lifecycle"].create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="c-dlg",
        match_id=room["matchId"],
        player_number=1,
    )
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    # Ordinary chat + DB idempotency replay & conflict
    after_chat = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="msg-1",
        content="What do you think of the center?",
    )
    calls_after_first = len(env["transport"].calls)
    replay = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="msg-1",
        content="What do you think of the center?",
    )
    assert len(env["transport"].calls) == calls_after_first
    assert len(replay["turns"]) == len(after_chat["turns"])
    with pytest.raises(IdempotencyConflictError):
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="msg-1",
            content="Different message body",
        )

    # Open question + intent feedback
    ctx = after_chat["progressContexts"][0]
    q_id = ctx["questionRecords"][0]["questionId"]
    after_q = await env["intent"].submit_question_feedback(
        sid, q_id, access, action="ignore", base_version_id=v1, idempotency_key="qf-1"
    )
    assert after_q["progressContexts"][0]["questionRecords"][0]["status"] == "ignored"
    hyp_turn = next(t for t in after_q["turns"] if t["guidance"].get("intentState"))
    hyp_id = hyp_turn["guidance"]["intentState"]["hypothesisId"]
    await env["intent"].submit_intent_feedback(
        sid, hyp_id, access, action="confirm", base_version_id=v1, idempotency_key="if-1"
    )

    # Binary discovery -> choice -> pending proposal -> accept
    asked = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="msg-bin",
        content="Make center simpler",
        request_proposal=True,
    )
    pq = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(asked["turns"])
        if t["guidance"].get("proposalQuestion")
    )
    planned = await env["proposal"].choose_binary_choice(
        sid,
        access,
        topic_id=pq["topicId"],
        question_id=pq["questionId"],
        choice_id=pq["choices"][0]["choiceId"],
        idempotency_key="bin-ch-1",
    )
    prop_id = planned["proposals"][0]["proposalId"]
    accepted = await env["proposal"].decide_proposal(
        sid, prop_id, access, decision="accept", base_version_id=v1, idempotency_key="dec-1"
    )
    assert accepted["currentVersionId"] != v1
    assert len(accepted["versions"]) == 2
    assert any(
        ev.event_type == "turn" for ev in env["rooms_repo"].list_room_events(room["matchId"])
    )
    assert any(
        ev.event_type == "stage" for ev in env["rooms_repo"].list_room_events(room["matchId"])
    )

    # Prepare valid per-turn translation responses (including malformed-JSON retry on turn 0)
    pending_turns = [t for t in accepted["turns"] if t["language"] != "zh-CN"]
    recovery_resp = env["transport"].responses.pop()
    for idx, t in enumerate(pending_turns):
        g = t["guidance"]
        raw_cues = [
            str(c.get("text") or "").strip()
            for c in (g.get("uiCues") or [])
            if isinstance(c, dict) and str(c.get("text") or "").strip()
        ]
        tr_item: dict[str, Any] = {
            "turnId": t["turnId"],
            "body": f"中文:{t['content'][:40]}",
            "uiCueTexts": [f"提示:{c}" for c in raw_cues],
        }
        if g.get("followUpQuestion"):
            tr_item["followUpQuestion"] = "中文追问？"
        if g.get("intentHypothesis"):
            tr_item["intentHypothesis"] = "中文意图"
        if isinstance(g.get("proposalOffer"), dict):
            offer = g["proposalOffer"]
            if offer.get("summary"):
                tr_item["proposalSummary"] = "中文提案摘要"
                tr_item["proposalOfferSummary"] = "中文提案摘要"
            if offer.get("rationale"):
                tr_item["proposalOfferRationale"] = "中文提案原因"
            pres = offer.get("proposalPresentation")
            if isinstance(pres, dict):
                if pres.get("schemaVersion") == 2 and pres.get("reason"):
                    tr_item["proposalPresentationReason"] = "中文理由"
                if pres.get("binaryCompact") and pres.get("verifiedActions"):
                    tr_item["proposalVerifiedActions"] = [
                        f"动作:{a}" for a in pres["verifiedActions"]
                    ]
        if g.get("proposalQuestion"):
            q = g["proposalQuestion"]
            tr_item["proposalQuestion"] = {
                "questionId": q["questionId"],
                "topicId": q["topicId"],
                "baseVersionId": q["baseVersionId"],
                "question": "中文问题？",
                "note": "说明",
                "number": q["number"],
                "choices": [
                    {
                        "choiceId": c["choiceId"],
                        "letter": c["letter"],
                        "label": f"选项{c['letter']}",
                    }
                    for c in q["choices"]
                ],
            }
        if idx == 0:
            env["transport"].responses.append("{not-json")
        env["transport"].responses.append(json.dumps({"translations": [tr_item]}))
        env["transport"].responses.append(json.dumps({"accepted": True, "reason": "Verified"}))
    env["transport"].responses.append(recovery_resp)

    translated = await env["translation"].translate_turns(
        sid, access, target_language="zh-CN"
    )
    assert all(
        "zh-CN" in t["translations"] for t in translated["turns"] if t["language"] != "zh-CN"
    )

    # Component recovery retry on Stage 2 opening
    v2 = accepted["currentVersionId"]
    env["op_repo"].upsert_task(
        "rec-1",
        sid,
        "stage_opening",
        v2,
        {
            "component": "stage_opening",
            "baseVersionId": v2,
            "status": "retry_pending",
            "attemptsUsed": 0,
            "maxAttempts": 3,
        },
        updated_at=env["clock"].utc_now(),
    )
    recovered = await env["recovery"].retry_recovery(
        sid, "rec-1", access, idempotency_key="rec-try-1"
    )
    assert env["op_repo"].get_task(sid, "rec-1").state["status"] == "completed"
    assert any(t["versionId"] == v2 and t["role"] == "assistant" for t in recovered["turns"])

    # Binary discovery cancel path on Stage 2
    def _make_bin_ask_v2(kwargs: dict[str, Any]) -> str:
        turn_id = _extract_turn_id(kwargs)
        return json.dumps(
            {
                "action": "ask",
                "question": "Which water or wall edit do you want?",
                "note": "Pick one direction.",
                "branches": [
                    {
                        "label": "Remove water tile",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water"],
                            "preserve": ["boxes", "targets", "player"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                        },
                    },
                    {
                        "label": "Reshape water nearby",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water", "add_water"],
                            "preserve": ["boxes", "targets", "player"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reshape_water",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 2},
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": {
                    "requirements": [
                        {
                            "component": "water",
                            "property": "count",
                            "relation": "decrease",
                            "strength": "goal",
                            "entities": [],
                            "value": None,
                            "unit": "none",
                            "priorityEvidenceSpan": "",
                            "focused": False,
                            "sourceTurnId": turn_id,
                            "evidenceSpan": "Adjust water",
                            "statement": "Adjust water obstacle",
                            "scope": "all",
                        }
                    ],
                    "automaticBindings": [],
                    "exactTransitions": [],
                },
            }
        )

    env["transport"].responses.append(_make_bin_ask_v2)
    asked2 = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v2,
        idempotency_key="msg-bin-2",
        content="Adjust water",
        request_proposal=True,
    )
    pq2 = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(asked2["turns"])
        if t["guidance"].get("proposalQuestion")
    )
    cancelled = await env["proposal"].cancel_binary_topic(
        sid, access, topic_id=pq2["topicId"], idempotency_key="bin-cancel-1"
    )
    assert env["op_repo"].get_topic(sid, pq2["topicId"]).state["status"] == "cancelled"
    assert cancelled["proposalFlowState"]["status"] != "clarifying"


def _make_single_bin_ask(turn_id: str, label: str = "Remove center wall") -> str:
    return json.dumps(
        {
            "action": "ask",
            "question": "Which obstacle should we simplify?",
            "note": "Pick A or B.",
            "branches": [
                {
                    "label": label,
                    "strategy": {
                        "effect": "adjust_internal_walls",
                        "focus": {"row": 4, "column": 6, "radius": 1},
                        "operators": ["remove_wall"],
                        "preserve": ["boxes", "targets", "player"],
                        "editBudget": 1,
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "internal_walls",
                        "focus": {"row": 4, "column": 6, "radius": 1},
                    },
                },
                {
                    "label": "Remove water tile",
                    "strategy": {
                        "effect": "reshape_water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": ["boxes", "targets", "player"],
                        "editBudget": 1,
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                    },
                },
            ],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": {
                "requirements": [
                    {
                        "component": "wall",
                        "property": "count",
                        "relation": "decrease",
                        "strength": "goal",
                        "entities": [],
                        "value": None,
                        "unit": "none",
                        "priorityEvidenceSpan": "",
                        "focused": False,
                        "sourceTurnId": turn_id,
                        "evidenceSpan": "Simplify the center",
                        "statement": "Remove an obstacle in the center",
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            },
        }
    )


async def test_shared_context_patch_resilience_evidence_trail_and_binary_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify invalid context patch tolerance, evidenceTrail, BinaryDiscoveryState & guards."""
    bad_patch_chat = json.dumps(
        {
            "assistantMessage": "Center opening helps push clarity.",
            "move": "clarify_intent",
            "followUpQuestion": "Confirm widening the center?",
            "intentDecision": {
                "classification": "candidate",
                "claims": [
                    {
                        "normalizedMeaning": "Keep the center open",
                        "subjectType": "space",
                        "subjectText": "center",
                        "attributeType": "clarity",
                        "attributeText": "openness",
                        "direction": "increase",
                        "degree": "neutral",
                        "aspect": "gameplay",
                        "scopeType": "region",
                        "scopeText": "center",
                        "evidenceSpan": "Keep the center open",
                    }
                ],
                "cardText": "Keep the center open",
            },
            "intentConfidence": "medium",
            "revisionOffer": None,
            "disagreement": None,
            "uiCues": [{"type": "clarification", "text": "Review intent card"}],
            "coordinateLinks": [],
            "assessment": None,
            "designContextPatch": {
                "goals": [{"text": "   ", "evidenceText": "Center opening"}],
                "constraints": [],
                "openQuestions": [],
            },
        }
    )
    bin_plan_ok = json.dumps(
        {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [
                {
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["remove_wall"],
                    "preserve": ["boxes", "targets", "player"],
                    "editBudget": 1,
                }
            ],
            "protectedEntities": [],
            "initialRequirements": None,
        }
    )
    valid_ops = json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove center wall",
                    "operations": [
                        {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                    ],
                }
            ]
        }
    )

    responses: list[Any] = [
        _routing_json("ordinary", evidence_span="Keep the center open"),
        bad_patch_chat,
        lambda kw: _make_single_bin_ask(_extract_turn_id(kw)),
        bin_plan_ok,
        json.dumps({"body": "No solvable edit found on that pass; click retry to try again."}),
        # Retry after retry_pending:
        bin_plan_ok,
        valid_ops,
    ]
    env = _build_env(tmp_path, responses=responses)
    prop_pipe = env["orchestrator"]._revision_handler.proposal_pipeline
    assert prop_pipe is not None
    orig_gen = prop_pipe.generate_proposal
    gen_calls = 0

    async def _gen_fail_once(**kwargs: Any) -> Any:
        nonlocal gen_calls
        gen_calls += 1
        if gen_calls == 1:
            raise ProposalSearchExhausted({"failureReasons": ["no_solvable_candidate"]})
        return await orig_gen(**kwargs)

    monkeypatch.setattr(prop_pipe, "generate_proposal", _gen_fail_once)
    created = await env["lifecycle"].create_demo_session(language="en", idempotency_key="demo-1")
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    # 1. Invalid optional designContextPatch does NOT roll back valid chat turn,
    #    and _intentReviewState + supporting_evidence_ids drive evidenceTrail.
    after_chat = await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="c1", content="Keep the center open"
    )
    assistant_turns = [t for t in after_chat["turns"] if t["role"] == "assistant"]
    assert any("Center opening helps push clarity." in t["content"] for t in assistant_turns)
    intent_turn = next(t for t in assistant_turns if t["guidance"].get("intentState"))
    hyp_id = intent_turn["guidance"]["intentState"]["hypothesisId"]
    confirmed = await env["intent"].submit_intent_feedback(
        sid,
        hyp_id,
        access,
        action="confirm",
        base_version_id=v1,
        source_turn_id=intent_turn["turnId"],
        idempotency_key="int-c1",
    )
    ctx_summary = confirmed["progressContexts"][0]
    inclination = next(
        item
        for item in ctx_summary["designInclinations"]
        if item["hypothesisId"] == hyp_id
    )
    assert len(inclination["evidenceTrail"]) >= 1
    assert inclination["evidenceTrail"][0]["kind"] == "expressed_direction"
    # IntentService DB idempotency replay + conflict
    replayed_int = await env["intent"].submit_intent_feedback(
        sid,
        hyp_id,
        access,
        action="confirm",
        base_version_id=v1,
        source_turn_id=intent_turn["turnId"],
        idempotency_key="int-c1",
    )
    assert replayed_int["sessionId"] == sid
    with pytest.raises(IdempotencyConflictError):
        await env["intent"].submit_intent_feedback(
            sid,
            hyp_id,
            access,
            action="reject",
            base_version_id=v1,
            source_turn_id=intent_turn["turnId"],
            idempotency_key="int-c1",
        )

    # 2. Policy 2 Binary Discovery: valid BinaryDiscoveryState, no _internalChoices leak,
    #    PROPOSAL_CHOICE_REQUIRED guard, ProposalSearchExhausted -> retry_pending -> retry.
    asked = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="b1",
        content="Simplify the center",
        request_proposal=True,
    )
    pq = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(asked["turns"])
        if t["guidance"].get("proposalQuestion")
    )
    assert pq["status"] == "pending"
    assert pq["selectedChoiceId"] is None
    assert "_internalChoices" not in pq
    assert "_internalChoices" not in json.dumps(asked["proposalFlowState"])

    stored_topic = env["op_repo"].get_topic(sid, pq["topicId"])
    assert stored_topic is not None
    validated_state = BinaryDiscoveryState.model_validate(stored_topic.state)
    assert validated_state.status == "clarifying"

    with pytest.raises(ConflictError) as blocked_chat:
        await env["orchestrator"].send_message(
            sid, access, base_version_id=v1, idempotency_key="blk-1", content="Hello"
        )
    assert blocked_chat.value.code == "PROPOSAL_CHOICE_REQUIRED"

    with pytest.raises(ConflictError) as blocked_exec:
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="blk-2",
            action="execute_revision",
            assistant_turn_id=intent_turn["turnId"],
        )
    assert blocked_exec.value.code == "PROPOSAL_CHOICE_REQUIRED"

    with pytest.raises(ConflictError) as stale_ver:
        await env["proposal"].choose_binary_choice(
            sid,
            access,
            topic_id=pq["topicId"],
            question_id=pq["questionId"],
            choice_id=pq["choices"][0]["choiceId"],
            base_version_id="wrong_vid",
            display_language="zh-CN",
            idempotency_key="ch-stale",
        )
    assert stale_ver.value.code == "VERSION_CONFLICT"

    failed_search = await env["proposal"].choose_binary_choice(
        sid,
        access,
        topic_id=pq["topicId"],
        question_id=pq["questionId"],
        choice_id=pq["choices"][0]["choiceId"],
        base_version_id=v1,
        display_language="zh-CN",
        idempotency_key="ch-fail",
    )
    topic_after_fail = env["op_repo"].get_topic(sid, pq["topicId"])
    assert topic_after_fail is not None
    assert topic_after_fail.state["status"] == "retry_pending"
    assert topic_after_fail.state["retryAction"] is not None
    assert "No solvable edit" in failed_search["turns"][-1]["content"]

    retried = await env["proposal"].choose_binary_choice(
        sid,
        access,
        topic_id=pq["topicId"],
        question_id=pq["questionId"],
        choice_id=pq["choices"][0]["choiceId"],
        base_version_id=v1,
        display_language="zh-CN",
        idempotency_key="ch-retry",
    )
    assert len(retried["proposals"]) == 1
    assert retried["proposals"][0]["status"] == "pending"


async def test_challenge_flow_revise_exit_and_revision_execution_outcome(
    tmp_path: Path,
) -> None:
    """Verify ChallengeHandler schema, DISAGREEMENT_ACTIVE, revise purple card, exit, & accept."""
    alt_req = json.dumps(
        {
            "requirements": [
                {
                    "component": "wall",
                    "property": "count",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "",
                    "focused": False,
                    "sourceTurnId": "t",
                    "evidenceSpan": "Maybe remove center wall",
                    "statement": "Remove wall at (4,6)",
                    "scope": "all",
                }
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
    )
    alt_plan = json.dumps(
        {
            "strategies": [
                {
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["remove_wall"],
                    "preserve": ["boxes", "targets", "player"],
                    "editBudget": 1,
                }
            ]
        }
    )
    alt_ops = json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove wall",
                    "operations": [
                        {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                    ],
                }
            ]
        }
    )
    review_reason_ok = json.dumps(
        {
            "relation": "different",
            "merit": "reasonable",
            "comparison": "Your idea protects central blocking while aiming for flow.",
        }
    )
    revise_req = json.dumps(
        {
            "requirements": [
                {
                    "component": "water",
                    "property": "count",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "",
                    "focused": False,
                    "sourceTurnId": "t",
                    "evidenceSpan": "Try removing water instead",
                    "statement": "Remove the water tile instead",
                    "scope": "all",
                }
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
    )
    revise_plan = json.dumps(
        {
            "strategies": [
                {
                    "effect": "reshape_water",
                    "focus": {"row": 5, "column": 6, "radius": 1},
                    "operators": ["remove_water"],
                    "preserve": ["boxes", "targets", "player"],
                    "editBudget": 1,
                }
            ]
        }
    )
    revise_ops = json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove water",
                    "operations": [
                        {"row": 5, "column": 6, "op": "remove_water", "from": "@", "to": "."}
                    ],
                }
            ]
        }
    )

    responses: list[Any] = [
        lambda kw: alt_req.replace('"t"', json.dumps(_extract_turn_id(kw, index=-1))),
        alt_plan,
        alt_ops,
        UpstreamLLMError("MODEL_TIMEOUT", "reason review timeout", retryable=True),
        UpstreamLLMError("MODEL_TIMEOUT", "reason review timeout", retryable=True),
        review_reason_ok,
        lambda kw: revise_req.replace('"t"', json.dumps(_extract_turn_id(kw, index=-1))),
        revise_plan,
        revise_ops,
    ]
    env = _build_env(tmp_path, responses=responses)
    room = await env["room_sync"].create_room(study_session_id="sr1", menu_started_flow=True)
    await env["room_sync"].join_room(
        room_code=room["roomCode"], study_session_id="sr2", menu_started_flow=True
    )
    created = await env["lifecycle"].create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="chg-sess",
        match_id=room["matchId"],
        player_number=1,
    )
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    # 1. Produce an offer turn via RevisionCardHandler (binding status == "active",
    #    no premature revisionExecutionOutcome on v1).
    with_offer = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="off-1",
        action="alternative_revision",
        content="Maybe remove center wall",
    )
    offer_turn = next(
        t for t in with_offer["turns"] if isinstance(t["guidance"].get("proposalOffer"), dict)
    )
    with env["db"].read_conn() as conn:
        raw_bind = conn.execute(
            "SELECT proposal_binding_json FROM conversation_turns WHERE turn_id = ?",
            (offer_turn["turnId"],),
        ).fetchone()[0]
    assert json.loads(raw_bind)["status"] == "active"
    with env["db"].read_conn() as conn:
        pre_outcomes = [
            (r[0], json.loads(r[1]))
            for r in conn.execute(
                "SELECT event_type, payload_json FROM audit_events WHERE session_id = ?",
                (sid,),
            ).fetchall()
        ]
    assert not any(
        ev_type == "revision_execution_outcome" and ev_payload.get("versionId") == v1
        for ev_type, ev_payload in pre_outcomes
    )

    prop_id = next(p["proposalId"] for p in with_offer["proposals"] if p["status"] == "pending")

    # 2. Start challenge -> revision_challenges columns + schema v2 guidance + DISAGREEMENT_ACTIVE
    started = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="chg-start",
        action="challenge_revision",
        proposal_id=prop_id,
        content="I want to keep the wall",
    )
    chg_turn = started["turns"][-1]
    assert chg_turn["guidance"]["disagreement"]["phase"] == "reason_pending"
    assert chg_turn["guidance"]["challengeCardSchemaVersion"] == 2
    assert chg_turn["guidance"]["challengeState"]["status"] == "awaiting_reason"
    assert chg_turn["guidance"]["challengeCard"]["challengeId"]

    with env["db"].read_conn() as conn:
        chg_row = conn.execute(
            "SELECT challenge_id, source_proposal_turn_id, challenge_turn_id, status "
            "FROM revision_challenges WHERE session_id = ?",
            (sid,),
        ).fetchone()
    assert chg_row is not None and chg_row["status"] == "awaiting_reason"
    chg_id = chg_row["challenge_id"]
    assert any(
        ev.event_type == "node" and ev.payload.get("nodeType") == "player_challenge"
        for ev in env["rooms_repo"].list_room_events(room["matchId"])
    )

    with pytest.raises(ConflictError) as dis_blocked:
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="chg-blk",
            content="Make proposal",
            request_proposal=True,
        )
    assert dis_blocked.value.code == "DISAGREEMENT_ACTIVE"

    # 3. Reason review LLM failure -> challenge_reason_review_pending; then valid -> choice_pending
    def _audit_events(session_id: str) -> list[tuple[str, dict[str, Any]]]:
        with env["db"].read_conn() as conn:
            rows = conn.execute(
                "SELECT event_type, payload_json FROM audit_events WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    with pytest.raises(UpstreamLLMError):
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="chg-r-fail",
            content="Keeping the wall keeps the puzzle rich.",
        )
    assert any(t == "challenge_reason_review_pending" for t, _ in _audit_events(sid))

    chosen_ready = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="chg-r-ok",
        content="Keeping the wall keeps the puzzle rich.",
    )
    assert chosen_ready["turns"][-1]["guidance"]["disagreement"]["phase"] == "choice_pending"
    assert chosen_ready["turns"][-1]["guidance"]["challengeState"]["status"] == "choice_pending"

    # 4. revise_proposal -> execute-only purple card + activeDisagreement cleared + outcome
    revised = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="chg-rev",
        action="continue_challenge",
        challenge_id=chg_id,
        challenge_choice="revise_proposal",
        content="Try removing water instead",
    )
    rev_turn = revised["turns"][-1]
    offer = rev_turn["guidance"]["proposalOffer"]
    assert offer["proposalPresentation"]["schemaVersion"] == 2
    assert offer["availableActions"] == ["execute_revision"]
    new_prop_id = next(p["proposalId"] for p in revised["proposals"] if p["status"] == "pending")
    accepted = await env["proposal"].decide_proposal(
        sid,
        new_prop_id,
        access,
        decision="accept",
        base_version_id=v1,
        reason="Works well",
        idempotency_key="chg-acc",
    )
    new_vid = accepted["currentVersionId"]
    outcome = next(
        ev_payload
        for ev_type, ev_payload in _audit_events(sid)
        if ev_type == "revision_execution_outcome" and ev_payload.get("versionId") == new_vid
    )
    assert outcome["versionId"] == new_vid
    assert outcome["status"] == "verified"
    assert outcome["beforeFingerprint"] and outcome["afterFingerprint"]
    assert outcome["summary"] and outcome["verifiedDiff"] and outcome["changeSummary"]
    assert outcome["diff"] and outcome["validation"]

    # 5. Start another challenge on v2 and exit cleanly via exit_challenge=True
    with env["db"].write_tx() as conn:
        conn.execute(
            "INSERT INTO change_proposals("
            "proposal_id, session_id, base_version_id, assistant_turn_id, status, "
            "proposal_json, created_at"
            ") VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (
                "prop-chg-2",
                sid,
                new_vid,
                rev_turn["turnId"],
                dump_json({"proposedRows": BASE_ROWS, "summary": "Revert"}),
                env["clock"].utc_now(),
            ),
        )
    await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=new_vid,
        idempotency_key="chg-start-2",
        action="challenge_revision",
        proposal_id="prop-chg-2",
        content="Wait",
    )
    exited = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=new_vid,
        idempotency_key="chg-exit",
        exit_challenge=True,
    )
    assert exited["turns"][-1]["guidance"]["disagreement"]["status"] == "resolved"


async def test_human_edit_engagement_recovery_intent_idempotency_and_partial_translation(
    tmp_path: Path,
) -> None:
    """Verify human_edit_engagement recovery, LLMError audits, & partial translation."""
    engage_routing = json.dumps(
        {
            "acts": ["evaluation"],
            "elements": ["internal_walls"],
            "mapRelated": True,
            "directionSufficient": True,
            "routeTarget": "ordinary",
            "evidenceSpan": "Let us discuss",
            "changes": [],
            "humanEditEngagement": {"decision": "engage", "evidenceSpan": "Let us discuss"},
        }
    )
    responses: list[Any] = [
        # 1. Active human_edit + routing LLM failure -> body still delivered + recovery enqueued
        UpstreamLLMError("MODEL_UNAVAILABLE", "routing timeout", retryable=True),
        UpstreamLLMError("MODEL_UNAVAILABLE", "routing timeout", retryable=True),
        _chat_json("Got your thoughts on the manual edit."),
        # 2. Recovery retry (human_edit_engagement) succeeds
        engage_routing,
        # 3. Direct chat with humanEditEngagement.decision == engage clears disagreement
        engage_routing,
        _chat_json("Great, disagreement resolved."),
        # 4. Ordinary chat LLM failure records LLMError code + retryable (max_attempts=3)
        _routing_json("ordinary", evidence_span="Rate limit"),
        UpstreamLLMError("MODEL_RATE_LIMIT", "rate limited", retryable=True),
        UpstreamLLMError("MODEL_RATE_LIMIT", "rate limited", retryable=True),
        UpstreamLLMError("MODEL_RATE_LIMIT", "rate limited", retryable=True),
        # 5. Partial translation: 1st turn succeeds, 2nd fails -> 1st saved, 2nd kept original
        "",  # populated dynamically
        json.dumps({"accepted": True, "reason": "Verified"}),
        UpstreamLLMError("MODEL_TIMEOUT", "translation timeout", retryable=True),
        UpstreamLLMError("MODEL_TIMEOUT", "translation timeout", retryable=True),
    ]
    env = _build_env(tmp_path, responses=responses)
    room = await env["room_sync"].create_room(study_session_id="sr-h1", menu_started_flow=True)
    await env["room_sync"].join_room(
        room_code=room["roomCode"], study_session_id="sr-h2", menu_started_flow=True
    )
    created = await env["lifecycle"].create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="hum-sess",
        match_id=room["matchId"],
        player_number=1,
    )
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    # Seed active human_edit disagreement on v1
    with env["db"].write_tx() as conn:
        v1_rec = env["sessions_repo"].get_version(sid, v1)
        ctx = set_active_disagreement(
            ensure_design_context(v1_rec.design_context),
            {
                "status": "active",
                "subject": "human_edit",
                "disagreementId": "dis-hum-1",
                "userPosition": "Removed a wall",
                "aiPosition": "Wall protected push line",
                "coreDisagreement": "Wall removal changes route",
                "nextQuestion": "Why remove that wall?",
            },
            v1,
            "seed-turn",
        )
        conn.execute(
            "UPDATE level_versions SET design_context_json = ? "
            "WHERE session_id = ? AND version_id = ?",
            (dump_json(ctx.to_wire_dict()), sid, v1),
        )

    def _audit_events(session_id: str) -> list[tuple[str, dict[str, Any]]]:
        with env["db"].read_conn() as conn:
            rows = conn.execute(
                "SELECT event_type, payload_json FROM audit_events WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    # 1. Routing fails during active human_edit -> body still delivered, recovery task created
    delivered = await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="hum-m1", content="Let us discuss my edit"
    )
    assert "Got your thoughts on the manual edit." in delivered["turns"][-1]["content"]
    assert any(
        ev_type == "reply_delivery_outcome"
        and payload.get("fullQualityPassed") is False
        and payload.get("component") == "human_edit_engagement"
        for ev_type, payload in _audit_events(sid)
    )
    with env["db"].read_conn() as conn:
        rec_id = conn.execute(
            "SELECT recovery_id FROM component_recovery_tasks WHERE session_id = ?", (sid,)
        ).fetchone()[0]
    task = env["op_repo"].get_task(sid, rec_id)
    assert task is not None
    assert task.state["component"] == "human_edit_engagement"
    assert task.state["status"] == "retry_pending"
    assert task.state["maxAttempts"] == 2

    # Concurrent active lease check -> RECOVERY_IN_PROGRESS; wrong baseVersionId -> VERSION_CONFLICT
    with pytest.raises(ConflictError) as bad_cas:
        await env["recovery"].retry_recovery(
            sid, rec_id, access, base_version_id="wrong-v", idempotency_key="rec-cas"
        )
    assert bad_cas.value.code == "VERSION_CONFLICT"

    future_iso = "2099-01-01T00:00:00Z"
    env["op_repo"].upsert_task(
        rec_id,
        sid,
        "human_edit_engagement",
        "dis-hum-1",
        {**task.state, "status": "claimed", "leaseUntil": future_iso},
        updated_at=env["clock"].utc_now(),
    )
    with pytest.raises(ConflictError) as locked_rec:
        await env["recovery"].retry_recovery(
            sid, rec_id, access, base_version_id=v1, idempotency_key="rec-locked"
        )
    assert locked_rec.value.code == "RECOVERY_IN_PROGRESS"

    # Reset lease to retry_pending and complete recovery -> clears activeDisagreement + attemptsUsed
    env["op_repo"].upsert_task(
        rec_id,
        sid,
        "human_edit_engagement",
        "dis-hum-1",
        task.state,
        updated_at=env["clock"].utc_now(),
    )
    await env["recovery"].retry_recovery(
        sid, rec_id, access, base_version_id=v1, idempotency_key="rec-go"
    )
    v1_after = env["sessions_repo"].get_version(sid, v1)
    assert v1_after is not None
    assert (v1_after.design_context or {}).get("activeDisagreement") is None
    assert any(
        ev_type == "component_recovery" and payload.get("attemptsUsed") == 1
        for ev_type, payload in _audit_events(sid)
    )

    # 3. Direct humanEditEngagement.decision == engage clears re-seeded disagreement
    with env["db"].write_tx() as conn:
        v1_rec2 = env["sessions_repo"].get_version(sid, v1)
        ctx2 = set_active_disagreement(
            ensure_design_context(v1_rec2.design_context),
            {
                "status": "active",
                "subject": "human_edit",
                "disagreementId": "dis-hum-2",
                "userPosition": "Edit 2",
                "aiPosition": "AI view 2",
                "coreDisagreement": "Diff 2",
                "nextQuestion": "Q2",
            },
            v1,
            "seed-turn-2",
        )
        conn.execute(
            "UPDATE level_versions SET design_context_json = ? "
            "WHERE session_id = ? AND version_id = ?",
            (dump_json(ctx2.to_wire_dict()), sid, v1),
        )
    engaged_res = await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="hum-m2", content="Let us discuss edit 2"
    )
    v1_eng = env["sessions_repo"].get_version(sid, v1)
    assert v1_eng is not None
    assert (v1_eng.design_context or {}).get("activeDisagreement") is None
    assert any(ev_type == "disagreement_acknowledged" for ev_type, _ in _audit_events(sid))

    # 4. UpstreamLLMError on chat records classified failure + retryable=True
    with pytest.raises(UpstreamLLMError):
        await env["orchestrator"].send_message(
            sid, access, base_version_id=v1, idempotency_key="m-llm-fail", content="Rate limit?"
        )
    audits_fail = _audit_events(sid)
    assert any(
        ev_type == "message_generation_failed"
        and payload.get("code") == "MODEL_RATE_LIMIT"
        and payload.get("retryable") is True
        for ev_type, payload in audits_fail
    )
    assert any(
        ev_type == "reply_generation_failed" and payload.get("retryable") is True
        for ev_type, payload in audits_fail
    )

    # 5. Partial translation preserves succeeded turns & leaves failed turns untranslated
    en_turns = [t for t in engaged_res["turns"] if t["language"] != "zh-CN"]
    assert len(en_turns) >= 2
    t0, t1 = en_turns[0], en_turns[1]
    t0_tr_item = {
        "turnId": t0["turnId"],
        "body": f"中文:{t0['content'][:30]}",
        "uiCueTexts": [f"提示:{c['text']}" for c in (t0["guidance"].get("uiCues") or [])],
    }
    if t0["guidance"].get("followUpQuestion"):
        t0_tr_item["followUpQuestion"] = "中文追问？"
    env["transport"].responses[0] = json.dumps({"translations": [t0_tr_item]})

    partial = await env["translation"].translate_turns(
        sid, access, target_language="zh-CN", turn_ids=[t0["turnId"], t1["turnId"]]
    )
    by_id = {t["turnId"]: t for t in partial["turns"]}
    assert "zh-CN" in by_id[t0["turnId"]]["translations"]
    assert "zh-CN" not in by_id[t1["turnId"]]["translations"]

    # Expired session rejected by TranslationService via require_active_session
    env["clock"].advance(950.0)
    with pytest.raises(ConflictError):
        await env["translation"].translate_turns(
            sid, access, target_language="zh-CN", turn_ids=[t1["turnId"]]
        )


async def test_p0_solver_delta_and_recovery_completed_outside_tx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify format_solver_delta_notice and RecoveryService completed replay outside write_tx."""
    from server.core.solver import SolveResult
    from server.i18n.messages import format_solver_delta_notice

    solved_sample = SolveResult(
        solvable=True,
        rows=tuple(ALT_ROWS),
        searched_states=10,
        solution_steps=8,
        solution_pushes=2,
        solution="RRRR",
    )
    en_notice = format_solver_delta_notice(
        {"solutionSteps": 10, "solutionPushes": 3}, solved_sample, "en"
    )
    zh_notice = format_solver_delta_notice(
        {"solutionSteps": 10, "solutionPushes": 3},
        {"solutionSteps": 8, "solutionPushes": 2},
        "zh-CN",
    )
    assert "10 -> 8" in en_notice and "3 -> 2" in en_notice
    assert "10 → 8" in zh_notice and "3 → 2" in zh_notice
    assert "8 minimum moves" in format_solver_delta_notice(None, solved_sample, "en")

    env = _build_env(tmp_path)
    created = await env["lifecycle"].create_demo_session(language="en", idempotency_key="d-p0")
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    env["op_repo"].upsert_task(
        "rec-done",
        sid,
        "stage_opening",
        v1,
        {
            "component": "stage_opening",
            "baseVersionId": v1,
            "status": "completed",
            "attemptsUsed": 1,
        },
        updated_at=env["clock"].utc_now(),
    )
    tx_states: list[bool] = []
    orig_get_payload = env["read_model"].get_session_payload

    async def _checked_payload(session_id: str) -> dict[str, Any]:
        tx_states.append(env["db"].in_write_tx)
        return await orig_get_payload(session_id)

    monkeypatch.setattr(env["read_model"], "get_session_payload", _checked_payload)
    payload = await env["recovery"].retry_recovery(
        sid, "rec-done", access, base_version_id=v1, idempotency_key="rec-done-try"
    )
    assert payload["sessionId"] == sid
    assert tx_states == [False]


async def test_purple_card_guards_challenge_lifecycle_and_ordinary_strip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ordinary proposalOffer stripping, ChallengeHandler binding/solve, & execute guards."""
    import server.application.dialogue.handlers.challenge as challenge_mod

    chat_with_rogue_offer = json.dumps(
        {
            "assistantMessage": "Just chatting about the center.",
            "move": "offer_perspective",
            "followUpQuestion": None,
            "intentDecision": None,
            "intentConfidence": None,
            "revisionOffer": {
                "summary": "Rogue offer",
                "rationale": "Should be ignored",
                "objective": "Remove wall",
                "changes": [
                    {"row": 4, "column": 6, "before": "#", "after": ".", "operation": "remove_wall"}
                ],
                "preserve": ["boxes", "targets", "player"],
                "expectedEffect": "Faster",
            },
            "disagreement": None,
            "uiCues": [],
            "coordinateLinks": [],
            "assessment": None,
            "designContextPatch": None,
        }
    )
    alt_req = json.dumps(
        {
            "requirements": [
                {
                    "component": "wall",
                    "property": "count",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "",
                    "focused": False,
                    "sourceTurnId": "t",
                    "evidenceSpan": "Remove center wall",
                    "statement": "Remove wall at (4,6)",
                    "scope": "all",
                }
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
    )
    alt_plan = json.dumps(
        {
            "strategies": [
                {
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["remove_wall"],
                    "preserve": ["boxes", "targets", "player"],
                    "editBudget": 1,
                }
            ]
        }
    )
    alt_ops = json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove wall",
                    "operations": [
                        {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                    ],
                }
            ]
        }
    )
    review_reason = json.dumps(
        {
            "relation": "different",
            "merit": "reasonable",
            "comparison": "Comparing keeping vs revising the wall removal.",
        }
    )

    responses: list[Any] = [
        _routing_json("ordinary", evidence_span="Just chatting"),
        chat_with_rogue_offer,
        lambda kw: alt_req.replace('"t"', json.dumps(_extract_turn_id(kw, index=-1))),
        alt_plan,
        alt_ops,
        review_reason,
    ]
    env = _build_env(tmp_path, responses=responses)
    created = await env["lifecycle"].create_demo_session(language="en", idempotency_key="d-p13")
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    # 1. Ordinary chat strips unbound proposalOffer
    after_chat = await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="ord-1", content="Just chatting"
    )
    ordinary_turn = after_chat["turns"][-1]
    assert "proposalOffer" not in ordinary_turn["guidance"]

    # Non-existent or unbound turn -> TURN_NOT_FOUND / INVALID_CARD_SOURCE
    with pytest.raises(NotFoundError) as missing_turn:
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="exec-missing",
            action="execute_revision",
            assistant_turn_id="no-such-turn",
        )
    assert missing_turn.value.code == "TURN_NOT_FOUND"

    with pytest.raises(ConflictError) as bad_source:
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="exec-bad-src",
            action="execute_revision",
            assistant_turn_id=ordinary_turn["turnId"],
        )
    assert bad_source.value.code == "INVALID_CARD_SOURCE"

    # 2. Create a bound proposal (empty tradeoff_notice -> format_solver_delta_notice)
    with_offer = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="alt-1",
        action="alternative_revision",
        content="Remove center wall",
    )
    offer_turn = next(
        t for t in with_offer["turns"] if isinstance(t["guidance"].get("proposalOffer"), dict)
    )
    prop_id = next(p["proposalId"] for p in with_offer["proposals"] if p["status"] == "pending")

    # 3. Start challenge -> binding becomes challenged; accept & early keep_proposal blocked
    started = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="chg-1",
        action="challenge_revision",
        proposal_id=prop_id,
        content="Wait on wall",
    )
    with env["db"].read_conn() as conn:
        b1 = json.loads(
            conn.execute(
                "SELECT proposal_binding_json FROM conversation_turns WHERE turn_id = ?",
                (offer_turn["turnId"],),
            ).fetchone()[0]
        )
    assert b1["status"] == "challenged"

    with pytest.raises(ConflictError) as dis_acc:
        await env["proposal"].decide_proposal(
            sid, prop_id, access, decision="accept", base_version_id=v1, idempotency_key="acc-blk"
        )
    assert dis_acc.value.code == "DISAGREEMENT_ACTIVE"

    chg_id = started["turns"][-1]["guidance"]["challengeState"]["challengeId"]
    with pytest.raises(ConflictError) as early_keep:
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="keep-early",
            action="continue_challenge",
            challenge_id=chg_id,
            challenge_choice="keep_proposal",
        )
    assert early_keep.value.code == "CHALLENGE_NOT_READY"

    # 4. Review reason -> awaiting_choice -> keep_proposal replays outside tx & restores active
    await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="chg-reason", content="Wall is useful"
    )
    solve_tx_states: list[bool] = []
    orig_solve_chg = challenge_mod.solve_stage_async

    async def _tracked_solve(rows: Any, *, clock: Any = None) -> Any:
        solve_tx_states.append(env["db"].in_write_tx)
        return await orig_solve_chg(rows, clock=clock)

    monkeypatch.setattr(challenge_mod, "solve_stage_async", _tracked_solve)
    await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="keep-ok",
        action="continue_challenge",
        challenge_id=chg_id,
        challenge_choice="keep_proposal",
    )
    assert solve_tx_states == [False]
    with env["db"].read_conn() as conn:
        b2 = json.loads(
            conn.execute(
                "SELECT proposal_binding_json FROM conversation_turns WHERE turn_id = ?",
                (offer_turn["turnId"],),
            ).fetchone()[0]
        )
    assert b2["status"] == "active"

    # 5. Execute revision on latest offer turn -> older offer turn becomes PROPOSAL_STALE
    executed = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="exec-ok",
        action="execute_revision",
        assistant_turn_id=offer_turn["turnId"],
    )
    new_offer_turn = executed["turns"][-1]
    assert "Verified solvable candidate" in new_offer_turn["content"]
    with pytest.raises(ConflictError) as stale_exec:
        await env["orchestrator"].send_message(
            sid,
            access,
            base_version_id=v1,
            idempotency_key="exec-old",
            action="execute_revision",
            assistant_turn_id=offer_turn["turnId"],
        )
    assert stale_exec.value.code == "PROPOSAL_STALE"

    # 6. Challenge new proposal & cancel_challenge -> proposal superseded & binding stale
    new_prop_id = next(p["proposalId"] for p in executed["proposals"] if p["status"] == "pending")
    await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="chg-2",
        action="challenge_revision",
        proposal_id=new_prop_id,
        content="Actually cancel",
    )
    await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="chg-2-cancel", exit_challenge=True
    )
    with env["db"].read_conn() as conn:
        p_status = conn.execute(
            "SELECT status FROM change_proposals WHERE proposal_id = ?", (new_prop_id,)
        ).fetchone()[0]
        b3 = json.loads(
            conn.execute(
                "SELECT proposal_binding_json FROM conversation_turns WHERE turn_id = ?",
                (new_offer_turn["turnId"],),
            ).fetchone()[0]
        )
    assert p_status == "superseded"
    assert b3["status"] == "stale"


async def test_binary_no_pre_tx_strategy_merge_and_source_language(tmp_path: Path) -> None:
    """Verify BinaryDiscoveryHandler defers tx, merges strategies, & emits sourceLanguage."""
    def _ask_r2(kwargs: dict[str, Any]) -> str:
        _extract_turn_id(kwargs)
        return json.dumps(
            {
                "action": "ask",
                "question": "Which water change next?",
                "note": "Pick water direction.",
                "branches": [
                    {
                        "label": "Remove water tile",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water"],
                            "preserve": ["boxes", "targets", "player"],
                            "editBudget": 7,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                        },
                    },
                    {
                        "label": "Reshape water nearby",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["add_water", "remove_water"],
                            "preserve": ["boxes", "targets", "player"],
                            "editBudget": 3,
                        },
                        "promise": {
                            "kind": "reshape_water",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 2},
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": None,
            }
        )

    plan_empty_strategies = json.dumps(
        {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": None,
        }
    )
    merged_ops = json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove wall and water",
                    "operations": [
                        {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."},
                        {"row": 5, "column": 6, "op": "remove_water", "from": "@", "to": "."},
                    ],
                }
            ]
        }
    )
    responses: list[Any] = [
        lambda kw: _make_single_bin_ask(_extract_turn_id(kw)),
        UpstreamLLMError("MODEL_TIMEOUT", "run_step failed", retryable=True),
        UpstreamLLMError("MODEL_TIMEOUT", "run_step failed", retryable=True),
        _ask_r2,
        plan_empty_strategies,
        merged_ops,
    ]
    env = _build_env(tmp_path, responses=responses)
    created = await env["lifecycle"].create_demo_session(language="en", idempotency_key="d-bin")
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    asked = await env["orchestrator"].send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="b-start",
        content="Simplify",
        request_proposal=True,
    )
    pq1 = asked["proposalFlowState"]["pendingQuestion"]
    assert pq1["sourceLanguage"] == "en"

    # run_step fails -> no premature choice tx, topic stays clarifying, reply failure recorded
    with pytest.raises(UpstreamLLMError):
        await env["proposal"].choose_binary_choice(
            sid,
            access,
            topic_id=pq1["topicId"],
            question_id=pq1["questionId"],
            choice_id=pq1["choices"][0]["choiceId"],
            idempotency_key="b-fail",
        )
    topic_after_fail = env["op_repo"].get_topic(sid, pq1["topicId"])
    assert topic_after_fail is not None and topic_after_fail.state["status"] == "clarifying"
    assert topic_after_fail.state["choices"] == []
    with env["db"].read_conn() as conn:
        ev_types = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM audit_events WHERE session_id = ?", (sid,)
            ).fetchall()
        ]
    assert "reply_generation_failed" in ev_types
    assert "binary_choice_selected" not in ev_types

    # Retry succeeds -> round 2 -> merges strategies (editBudget <= 12) & commits atomically
    r2 = await env["proposal"].choose_binary_choice(
        sid,
        access,
        topic_id=pq1["topicId"],
        question_id=pq1["questionId"],
        choice_id=pq1["choices"][0]["choiceId"],
        idempotency_key="b-c1",
    )
    pq2 = r2["proposalFlowState"]["pendingQuestion"]
    assert pq2["sourceLanguage"] == "en"
    planned = await env["proposal"].choose_binary_choice(
        sid,
        access,
        topic_id=pq2["topicId"],
        question_id=pq2["questionId"],
        choice_id=pq2["choices"][0]["choiceId"],
        idempotency_key="b-c2",
    )
    assert len(planned["proposals"]) == 1
    with env["db"].read_conn() as conn:
        prop_json = json.loads(
            conn.execute(
                "SELECT proposal_json FROM change_proposals WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        )
        choice_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE session_id = ? AND event_type = 'binary_choice_selected'",
            (sid,),
        ).fetchone()[0]
    merged_strat = prop_json["revisionPlan"]["strategies"][0]
    assert set(merged_strat["operators"]) == {"remove_wall", "remove_water"}
    assert merged_strat["editBudget"] <= 12
    assert choice_audits == 2


async def test_recovery_validation_redaction_single_tx_cancellation_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify P1-7/8/9/10/12/15 and P2-4 behaviors cleanly."""
    import asyncio

    from server.application.dialogue._shared import generate_safe_failure_analysis
    from server.core.llm_client import LLMValidationError

    bad_engage = json.dumps(
        {
            "acts": ["evaluation"],
            "elements": ["internal_walls"],
            "mapRelated": True,
            "directionSufficient": True,
            "routeTarget": "ordinary",
            "evidenceSpan": "Let us discuss",
            "changes": [],
            "humanEditEngagement": {"decision": "engage", "evidenceSpan": "NOT IN USER TEXT"},
        }
    )
    responses: list[Any] = [
        # 1. Routing fails on active human_edit -> single atomic write_tx
        UpstreamLLMError("MODEL_UNAVAILABLE", "route timeout", retryable=True),
        UpstreamLLMError("MODEL_UNAVAILABLE", "route timeout", retryable=True),
        _chat_json("Reply delivered despite routing failure."),
        # 2. Recovery fails validation (evidenceSpan mismatch -> LLMValidationError, 1024 tokens)
        bad_engage,
        # 3. Recovery fails with secret in exception -> redacted in lastError
        UpstreamLLMError("UPSTREAM_SERVER_ERROR", "leaked super-secret-key-xyz", retryable=False),
        # 4. Routing before CancelledError on ordinary handler
        _routing_json("ordinary", evidence_span="Cancel"),
        # 5. Translation non-retryable LLMError aborts immediately
        UpstreamLLMError("UPSTREAM_AUTHENTICATION_FAILED", "bad key", retryable=False),
    ]
    env = _build_env(tmp_path, responses=responses)
    und_pipe = env["orchestrator"]._understanding_pipeline
    chat_pipe = env["orchestrator"]._ordinary_handler._chat_pipeline
    rec_svc = RecoveryService(
        env["db"],
        env["sessions_repo"],
        env["op_repo"],
        env["read_model"],
        chat_pipeline=chat_pipe,
        understanding_pipeline=und_pipe,
        room_sync=env["room_sync"],
        clock=env["clock"],
        settings=env["orchestrator"]._settings,
        extra_secrets=("super-secret-key-xyz",),
    )

    created = await env["lifecycle"].create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="d-rec",
    )
    sid = created["sessionId"]
    access = (
        await env["lifecycle"].authorize_browser_access(
            sid, _extract_hash_param(created["launchUrl"], "bootstrap")
        )
    )["accessToken"]
    p1 = await env["lifecycle"].update_language(sid, access, language="en")
    v1 = p1["currentVersionId"]

    with env["db"].write_tx() as conn:
        v1_rec = env["sessions_repo"].get_version(sid, v1)
        ctx = set_active_disagreement(
            ensure_design_context(v1_rec.design_context),
            {
                "status": "active",
                "subject": "human_edit",
                "disagreementId": "dis-single-tx",
                "userPosition": "Edit",
                "aiPosition": "AI",
                "coreDisagreement": "Diff",
                "nextQuestion": "Why?",
            },
            v1,
            "seed-turn",
        )
        conn.execute(
            "UPDATE level_versions SET design_context_json = ? "
            "WHERE session_id = ? AND version_id = ?",
            (dump_json(ctx.to_wire_dict()), sid, v1),
        )

    # 1. Count write_tx entries during ordinary fallback with recovery task + complete_key
    tx_entries = 0
    orig_write_tx = env["db"].write_tx

    @contextmanager
    def _counting_tx():
        nonlocal tx_entries
        tx_entries += 1
        with orig_write_tx() as conn:
            yield conn

    monkeypatch.setattr(env["db"], "write_tx", _counting_tx)
    delivered = await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="msg-atomic", content="Let us discuss"
    )
    # 1 for claim_key + 1 single atomic write_tx for turn + recovery + complete_key
    assert tx_entries == 2
    monkeypatch.setattr(env["db"], "write_tx", orig_write_tx)

    rec_id = delivered["interactionState"]["recoveries"][0]["recoveryId"]
    with pytest.raises(LLMValidationError):
        await rec_svc.retry_recovery(
            sid, rec_id, access, base_version_id=v1, idempotency_key="rec-val-fail"
        )
    assert env["transport"].calls[-1]["max_tokens"] == 1024

    # Reset attemptsUsed to 0 and test secret redaction in lastError
    t_rec = env["op_repo"].get_task(sid, rec_id)
    assert t_rec is not None
    env["op_repo"].upsert_task(
        rec_id,
        sid,
        "human_edit_engagement",
        "dis-single-tx",
        {**t_rec.state, "status": "retry_pending", "attemptsUsed": 0},
        updated_at=env["clock"].utc_now(),
    )
    with pytest.raises(UpstreamLLMError):
        await rec_svc.retry_recovery(
            sid, rec_id, access, base_version_id=v1, idempotency_key="rec-secret"
        )
    task_after_secret = env["op_repo"].get_task(sid, rec_id)
    assert task_after_secret is not None
    assert "super-secret-key-xyz" not in (task_after_secret.state.get("lastError") or "")
    assert "[redacted]" in (task_after_secret.state.get("lastError") or "")

    # 2. CancelledError rollback in RecoveryService and MessageOrchestrator
    env["op_repo"].upsert_task(
        rec_id,
        sid,
        "human_edit_engagement",
        "dis-single-tx",
        {**t_rec.state, "status": "retry_pending", "attemptsUsed": 0, "leaseUntil": None},
        updated_at=env["clock"].utc_now(),
    )

    async def _cancel_coro(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(rec_svc, "_recover_human_edit_engagement", _cancel_coro)
    with pytest.raises(asyncio.CancelledError):
        await rec_svc.retry_recovery(
            sid, rec_id, access, base_version_id=v1, idempotency_key="rec-cancel"
        )
    task_cancelled = env["op_repo"].get_task(sid, rec_id)
    assert task_cancelled is not None
    assert task_cancelled.state["status"] == "retry_pending"
    assert task_cancelled.state["attemptsUsed"] == 0
    assert task_cancelled.state["leaseUntil"] is None

    monkeypatch.setattr(env["orchestrator"]._ordinary_handler, "handle", _cancel_coro)
    with pytest.raises(asyncio.CancelledError):
        await env["orchestrator"].send_message(
            sid, access, base_version_id=v1, idempotency_key="msg-cancel", content="Cancel"
        )
    cancelled_key = env["op_repo"].get_key(sid, "message", "msg-cancel")
    assert cancelled_key is not None and cancelled_key.status == "failed"

    # 3. Non-retryable LLMError aborts translation immediately
    with pytest.raises(UpstreamLLMError) as tr_abort:
        await env["translation"].translate_turns(sid, access, target_language="zh-CN")
    assert tr_abort.value.code == "UPSTREAM_AUTHENTICATION_FAILED"

    # 4. generate_safe_failure_analysis does not swallow RuntimeError
    class _BrokenPipeline:
        async def generate_failure_analysis(self, **kwargs: Any) -> str:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await generate_safe_failure_analysis(
            _BrokenPipeline(),
            rows=BASE_ROWS,
            designer_turns=[],
            failure_code="X",
            failure_summary="Y",
            language="en",
        )

    # 5. Completed idempotency replay succeeds after deadline expiry; fresh request fails
    env["clock"].advance(950.0)
    replayed_after_exp = await env["orchestrator"].send_message(
        sid, access, base_version_id=v1, idempotency_key="msg-atomic", content="Let us discuss"
    )
    assert replayed_after_exp["deadlineExpired"] is True
    with pytest.raises(ConflictError) as fresh_exp:
        await env["orchestrator"].send_message(
            sid, access, base_version_id=v1, idempotency_key="msg-after-exp", content="Fresh"
        )
    assert fresh_exp.value.code == "SESSION_DEADLINE_EXPIRED"
