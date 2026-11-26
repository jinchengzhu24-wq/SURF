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
from server.core.llm_client import TransportResponse
from server.domain.errors import ConflictError, IdempotencyConflictError, NotFoundError
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


def _extract_turn_id(kwargs: dict[str, Any]) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("initialDesignerTurns") or []
    return str(turns[0]["id"]) if turns else "turn-hint"


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
        # 4. Translation: gen fails once (malformed JSON), gen succeeds, verifier accepts
        "{not-json",
        "",  # replaced dynamically before translate_turns
        json.dumps({"accepted": True, "reason": "Verified"}),
        # 5. Recovery retry (stage_opening)
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

    # Prepare valid translation response for all turns needing zh-CN translation
    pending_turns = [t for t in accepted["turns"] if t["language"] != "zh-CN"]
    tr_items = []
    for t in pending_turns:
        g = t["guidance"]
        tr_item: dict[str, Any] = {
            "turnId": t["turnId"],
            "body": f"中文:{t['content'][:40]}",
            "uiCueTexts": [f"提示:{c}" for c in (g.get("uiCueTexts") or [])],
        }
        if g.get("intentHypothesis"):
            tr_item["intentHypothesis"] = "中文意图"
        if isinstance(g.get("proposalOffer"), dict) and g["proposalOffer"].get("summary"):
            tr_item["proposalSummary"] = "中文提案摘要"
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
        tr_items.append(tr_item)
    env["transport"].responses[1] = json.dumps({"translations": tr_items})

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
