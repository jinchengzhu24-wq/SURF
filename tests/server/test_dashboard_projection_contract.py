"""TDD contract regression tests for Batch 2A: authoritative Dashboard projection,
research NDJSON export, cascade deletion, IntentService wiring, opening cards,
raw-turn suppression, historical fallback backfill, and >1200s co-creation duration.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from server.application import DashboardService, OnlineRoomService
from server.container import build_container
from server.core.clock import FakeClock
from server.core.solver import solve_min_steps
from server.domain.rooms.events import CoCreationFlowEvent
from server.main import create_app
from server.persistence import RoomsRepository, SQLiteDatabase
from server.persistence.database import dump_json
from server.settings import SokobanSettings
from tests.conftest import (
    BASE_ROWS,
    FakeLLMTransport,
    extract_hash_param,
    extract_query_param,
    make_binary_ask_json,
    make_binary_plan_json,
    make_chat_json,
    make_map_ops_json,
    make_opening_json,
    make_routing_json,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "web"
    / "apps"
    / "dashboard"
    / "src"
    / "__tests__"
    / "fixtures"
    / "matchmaking_projection.contract.json"
)

_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "secret_hash",
        "secretHash",
        "access_token_hash",
        "accessTokenHash",
        "integration_token_hash",
        "integrationTokenHash",
        "bootstrap_token_hash",
        "bootstrapTokenHash",
        "ticket_hash",
        "ticketHash",
        "attempt_token_hash",
        "attemptTokenHash",
        "tokenHash",
        "playerToken",
        "accessToken",
        "integrationToken",
        "bootstrapToken",
        "attemptToken",
    }
)

MANUAL_ROWS_V2: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@..#..#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

MANUAL_ROWS_V3: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@..##.#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

MANUAL_ROWS_V4: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@.....#",
    "#........#.#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


def _assert_no_secret_keys(obj: Any, *, path: str = "$") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in _FORBIDDEN_SECRET_KEYS, f"Forbidden secret key {k!r} at {path}.{k}"
            _assert_no_secret_keys(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _assert_no_secret_keys(item, path=f"{path}[{idx}]")


def _extract_last_turn_id(kwargs: dict[str, Any]) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("designerTurns") or payload.get("initialDesignerTurns") or []
    return str(turns[-1]["id"]) if turns else "turn-hint"


def _make_manual_conflict_classifier(relation: str = "conflict") -> Any:
    def _builder(kwargs: dict[str, Any]) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        dirs = payload.get("directions") or []
        return json.dumps(
            {
                "comparisons": [
                    {
                        "directionId": str(d["id"]),
                        "relation": relation,
                        "explanation": (
                            "The added wall narrows the open center."
                            if relation == "conflict"
                            else "Moving the flank wall restores the open center."
                        ),
                    }
                    for d in dirs
                ]
            }
        )

    return _builder


def _make_manual_review_conflict_json() -> str:
    return json.dumps(
        {
            "openingMessage": "You added a wall on the right side of the center lane.",
            "reviewMessage": (
                "This narrows the right side after you asked to keep the center open."
            ),
            "conflict": {
                "userPosition": "You placed an extra wall on the right flank.",
                "aiPosition": "Earlier you preferred keeping the central corridor open.",
                "coreDisagreement": "Does this extra wall help pacing or block the open center?",
                "nextQuestion": "Do you want to keep this narrower choke point?",
            },
            "assessment": {
                "solutionSummary": "Direct push remains solvable with right-flank wall.",
                "difficultyOpinion": "Slightly tighter around row 5.",
                "features": ["Right-side choke wall"],
                "suggestions": ["Test whether the detour feels natural."],
            },
        }
    )


def _make_manual_review_no_conflict_json() -> str:
    return json.dumps(
        {
            "openingMessage": "You moved the flank wall lower toward the bottom-right.",
            "reviewMessage": "This restores openness in the center lane.",
            "assessment": {
                "solutionSummary": "Center is open again.",
                "difficultyOpinion": "Balanced pacing.",
                "features": ["Lower-right anchor wall"],
                "suggestions": ["Playtest the restored lane."],
            },
        }
    )


def _make_routing_with_human_edit_json(evidence_span: str) -> str:
    return json.dumps(
        {
            "acts": ["evaluation"],
            "elements": ["internal_walls"],
            "mapRelated": True,
            "directionSufficient": True,
            "routeTarget": "ordinary",
            "evidenceSpan": evidence_span,
            "changes": [],
            "humanEditEngagement": {
                "decision": "engage",
                "evidenceSpan": evidence_span,
            },
        }
    )


def _make_binary_ask_round2_json(kwargs: dict[str, Any]) -> str:
    _ = _extract_last_turn_id(kwargs)
    return json.dumps(
        {
            "action": "ask",
            "question": "Should we also clear the lower flank wall or the central water tile?",
            "note": "Narrowing down the second obstacle change.",
            "branches": [
                {
                    "label": "Also clear lower flank wall",
                    "strategy": {
                        "effect": "adjust_internal_walls",
                        "focus": {"row": 6, "column": 10, "radius": 1},
                        "operators": ["remove_wall"],
                        "preserve": ["water"],
                        "editBudget": 1,
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "internal_walls",
                        "focus": {"row": 6, "column": 10, "radius": 1},
                    },
                },
                {
                    "label": "Also clear central water tile",
                    "strategy": {
                        "effect": "reshape_water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": [],
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
        }
    )


def _make_challenge_review_json() -> str:
    return json.dumps(
        {
            "relation": "different",
            "merit": "reasonable",
            "comparison": (
                "Removing the water tile opens vertical movement while removing the wall "
                "opens the upper center lane."
            ),
        }
    )


def _make_water_plan_json() -> str:
    return json.dumps(
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


def _make_water_ops_json() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove center water tile",
                    "operations": [
                        {"row": 5, "column": 6, "op": "remove_water", "from": "@", "to": "."}
                    ],
                }
            ]
        }
    )


def _make_req_record_json(statement: str, evidence_span: str) -> Any:
    def _builder(kwargs: dict[str, Any]) -> str:
        tid = _extract_last_turn_id(kwargs)
        return json.dumps(
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
                        "sourceTurnId": tid,
                        "evidenceSpan": evidence_span,
                        "statement": statement,
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            }
        )

    return _builder


async def _build_full_contract_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Run a realistic, deterministic 2-player formal flow through real services."""
    uuid_counter = 0

    def _deterministic_uuid4() -> uuid.UUID:
        nonlocal uuid_counter
        uuid_counter += 1
        return uuid.UUID(f"{uuid_counter:08x}-2026-4000-8000-{uuid_counter:012x}")

    monkeypatch.setattr(uuid, "uuid4", _deterministic_uuid4)
    import server.application.rooms.service as rooms_service_mod

    room_codes = iter(["CONT2A", "HIST99", "DEMO01"])
    monkeypatch.setattr(rooms_service_mod, "generate_room_code", lambda: next(room_codes))

    clock = FakeClock(utc_time=datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC))
    cocreation_transport = FakeLLMTransport(
        [
            # --- Player 1 ---
            # 1. Stage 1 opening
            make_opening_json(
                "Stage 1 is solvable in 6 moves with a clear central corridor."
            ),
            # 2. Ordinary discussion turn 1 (no intent)
            make_routing_json("ordinary", evidence_span="center corridor"),
            make_chat_json(
                "Keeping the central corridor open lets players read the first push quickly."
            ),
            # 3. Ordinary discussion turn 2 -> tentative intent hypothesis
            make_routing_json("ordinary", evidence_span="center"),
            make_chat_json(
                "Got it — you want to keep the center airy for clean look-ahead.",
                hyp="Keep the center open for readable pushing.",
                evidence_span="center",
            ),
            # 4. Stage 2 manual edit -> conflict classifier + manual_edit_review + LLM challenge #1
            _make_manual_conflict_classifier("conflict"),
            _make_manual_review_conflict_json(),
            # 5. Player 1 engages LLM challenge #1 -> acknowledged
            _make_routing_with_human_edit_json("narrower"),
            make_chat_json(
                "Thanks for clarifying — keeping that right-flank choke makes sense now."
            ),
            # 6. Stage 3 manual edit -> conflict classifier + manual_edit_review + LLM challenge #2
            _make_manual_conflict_classifier("conflict"),
            _make_manual_review_conflict_json(),
            # 7. Stage 4 manual edit (re-edit closing LLM challenge #2) -> no-conflict review
            _make_manual_conflict_classifier("aligned"),
            _make_manual_review_no_conflict_json(),
            # 8. Policy 2 Binary Discovery Q1 -> choice -> Q2 -> choice -> Offer
            make_binary_ask_json,
            _make_binary_ask_round2_json,
            json.dumps(
                {
                    "action": "plan",
                    "question": "",
                    "note": "",
                    "branches": [],
                    "planStrategies": [
                        {
                            "effect": "adjust_internal_walls",
                            "focus": {"row": 5, "column": 8, "radius": 3},
                            "operators": ["remove_wall"],
                            "preserve": ["water"],
                            "editBudget": 2,
                        }
                    ],
                    "protectedEntities": [],
                }
            ),
            json.dumps(
                {
                    "candidates": [
                        {
                            "strategyIndex": 1,
                            "rationale": "Remove both center and lower flank walls",
                            "operations": [
                                {
                                    "row": 4,
                                    "column": 6,
                                    "op": "remove_wall",
                                    "from": "#",
                                    "to": ".",
                                },
                                {
                                    "row": 6,
                                    "column": 10,
                                    "op": "remove_wall",
                                    "from": "#",
                                    "to": ".",
                                },
                            ],
                        }
                    ]
                }
            ),
            # 9. Player Challenge on Offer -> reason review -> revise_proposal -> replacement offer
            _make_challenge_review_json(),
            _make_req_record_json("Remove the central water tile instead", "remove the water"),
            _make_water_plan_json(),
            _make_water_ops_json(),
            # --- Player 2 ---
            # 10. Player 2 Stage 1 opening (left unconsumed!)
            make_opening_json("Player 2 Stage 1 starts with an open baseline route."),
            # 11. Player 2 Policy 2 Binary Discovery Q1 -> choice -> Offer
            make_binary_ask_json,
            make_binary_plan_json(),
            make_map_ops_json(),
            # 12. Player 2 Challenge -> reason review -> keep_proposal (original_selected)
            _make_challenge_review_json(),
        ]
    )

    settings = SokobanSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "contract.sqlite3",
        study_logs_dir=tmp_path / "study_logs",
        token_secret="contract-test-secret-key-999999",
        dashboard_delete_password="delete-pass-123",
        cocreation_deadline_seconds=1800,
        reaper_enabled=False,
    )
    container = build_container(
        settings=settings,
        clock=clock,
        deepseek_transport=FakeLLMTransport([]),
        cocreation_transport=cocreation_transport,
    )
    container.db.initialize()
    assert container.chat_pipeline is not None
    orig_generate_opening = container.chat_pipeline.generate_stage_opening

    async def _opening_with_guidance_cards(*args: Any, **kwargs: Any) -> Any:
        res = await orig_generate_opening(*args, **kwargs)
        enriched = {
            **dict(res.guidance or {}),
            "followUpQuestion": "Would you like to test or tweak this layout?",
            "uiCues": [{"type": "clarification", "text": "Playtest to verify box pacing."}],
        }
        from dataclasses import replace

        return replace(res, guidance=enriched)

    monkeypatch.setattr(
        container.chat_pipeline, "generate_stage_opening", _opening_with_guidance_cards
    )
    app = create_app(settings=settings, container=container)

    # Seed an unrelated demo session to prove demo isolation everywhere
    demo = container.sessions_repo.create_session(
        session_id="demo-session-excluded-001",
        mode="algorithm_demo",
        language="en",
        access_token_hash="demo-access-hash",
        integration_token_hash="demo-int-hash",
        bootstrap_token_hash="demo-boot-hash",
        secret_hash="demo-secret-hash",
        is_demo=True,
        created_at=clock.utc_now(),
    )
    demo_sid = demo.session_id

    # Seed a study_records row to prove study_records preservation on delete
    await container.study_records_service.record_survey(
        {"participantId": "study-p1-full-uuid", "q1": 5}
    )

    # Create 2-player formal room
    p1_room = await container.online_room_service.create_room(
        study_session_id="study-p1-full-uuid", menu_started_flow=True
    )
    match_id = p1_room["matchId"]
    room_code = p1_room["roomCode"]
    p1_tok = p1_room["playerToken"]
    clock.advance(5.0)
    p2_room = await container.online_room_service.join_room(
        room_code=room_code, study_session_id="study-p2-full-uuid", menu_started_flow=True
    )
    p2_tok = p2_room["playerToken"]
    await container.online_room_service.set_ready(match_id, p1_tok, ready=True)
    await container.online_room_service.set_ready(match_id, p2_tok, ready=True)

    # Record DG drafts for both players
    draft_p1 = {
        "finalDifficulty": "Medium",
        "finalLayout": "Balanced",
        "q1Answer": "observe_then_decide",
        "q2Answer": "easy_to_adjust",
        "q3Answer": "focused_area",
        "q4Answer": "short_routes",
        "aiReflection": "Balanced medium pacing with focused center.",
        "aiDifficultyRationale": "Steady planning maps to Medium.",
        "aiLayoutRationale": "Focused spacing maps to Balanced.",
        "aiRecommendedDifficulty": "Medium",
        "aiRecommendedLayout": "Balanced",
        "aiRecommendationSource": "llm",
    }
    draft_p2 = {
        "finalDifficulty": "Easy",
        "finalLayout": "Compact",
        "q1Answer": "quick_start",
        "q2Answer": "easy_to_adjust",
        "q3Answer": "focused_area",
        "q4Answer": "short_routes",
        "aiReflection": "Quick start with compact routing.",
        "aiDifficultyRationale": "Quick start maps to Easy.",
        "aiLayoutRationale": "Focused area maps to Compact.",
        "aiRecommendedDifficulty": "Easy",
        "aiRecommendedLayout": "Compact",
        "aiRecommendationSource": "llm",
    }
    clock.advance(5.0)
    await container.online_room_service.record_draft(match_id, p1_tok, draft_p1)
    await container.online_room_service.record_draft(match_id, p2_tok, draft_p2)

    # --- Player 1 co-creation session ---
    clock.advance(5.0)
    p1_created = await container.session_lifecycle_service.create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="description_generation",
        language="en",
        idempotency_key="c2a-p1-create",
        match_id=match_id,
        player_number=1,
    )
    p1_sid = p1_created["sessionId"]
    p1_boot = extract_hash_param(p1_created["launchUrl"], "bootstrap")
    p1_access = (
        await container.session_lifecycle_service.authorize_browser_access(p1_sid, p1_boot)
    )["accessToken"]

    # Record draft regenerations (1 failed + 1 completed) before language lock
    clock.advance(3.0)
    container.sessions_repo.create_draft_regeneration(
        request_id="regen-p1-1",
        session_id=p1_sid,
        idempotency_key="regen-k1",
        status="failed",
        generation=2,
        failure_code="GENERATOR_TIMEOUT",
        requested_at=clock.utc_now(),
    )
    clock.advance(4.0)
    solved_base = solve_min_steps(BASE_ROWS)
    container.sessions_repo.create_draft_regeneration(
        request_id="regen-p1-2",
        session_id=p1_sid,
        idempotency_key="regen-k2",
        status="completed",
        generation=3,
        rows=list(BASE_ROWS),
        validation=solved_base.as_dict(),
        requested_at=clock.utc_now(),
    )

    # Lock language -> Stage 1 (first_stage) + deadline_at = +1800s
    clock.advance(5.0)
    p1_s1 = await container.session_lifecycle_service.update_language(
        p1_sid, p1_access, language="en"
    )
    v1 = p1_s1["currentVersionId"]

    # Stage 1 Opening (with cards!)
    clock.advance(5.0)
    await container.stage_service.assess_version(
        p1_sid, v1, p1_access, idempotency_key="p1-open-v1"
    )

    # Ordinary discussion turn 1 (no intent)
    clock.advance(10.0)
    await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v1,
        idempotency_key="p1-disc-1",
        content="How does the center corridor feel right now?",
    )

    # Ordinary discussion turn 2 -> tentative intent hypothesis -> revise via IntentService
    clock.advance(10.0)
    intent_turn_state = await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v1,
        idempotency_key="p1-intent-1",
        content="I want the center open so players can read the pushes.",
    )
    intent_turn = next(
        t
        for t in reversed(intent_turn_state["turns"])
        if isinstance(t["guidance"].get("intentState"), dict)
    )
    hyp_id = intent_turn["guidance"]["intentState"]["hypothesisId"]
    clock.advance(10.0)
    await container.intent_service.submit_intent_feedback(
        p1_sid,
        hyp_id,
        p1_access,
        action="revise",
        base_version_id=v1,
        idempotency_key="p1-intent-rev",
        candidate_text="Keep the center open with clear visual lanes.",
    )

    # Stage 2 manual edit -> manual_edit_review + LLM challenge #1 -> acknowledged
    clock.advance(15.0)
    s2 = await container.stage_service.save_version(
        p1_sid,
        p1_access,
        base_version_id=v1,
        rows=MANUAL_ROWS_V2,
        summary="Added right flank wall",
        idempotency_key="p1-save-v2",
    )
    v2 = s2["currentVersionId"]
    clock.advance(5.0)
    await container.stage_service.assess_version(
        p1_sid, v2, p1_access, idempotency_key="p1-assess-v2"
    )
    clock.advance(10.0)
    await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v2,
        idempotency_key="p1-ack-llm-chg",
        content="I want that right-side wall to create a narrower turn.",
    )

    # Stage 3 manual edit -> manual_edit_review + LLM challenge #2 -> closed via Stage 4 re-edit
    clock.advance(15.0)
    s3 = await container.stage_service.save_version(
        p1_sid,
        p1_access,
        base_version_id=v2,
        rows=MANUAL_ROWS_V3,
        summary="Extended right flank wall",
        idempotency_key="p1-save-v3",
    )
    v3 = s3["currentVersionId"]
    clock.advance(5.0)
    await container.stage_service.assess_version(
        p1_sid, v3, p1_access, idempotency_key="p1-assess-v3"
    )
    clock.advance(15.0)
    s4 = await container.stage_service.save_version(
        p1_sid,
        p1_access,
        base_version_id=v3,
        rows=MANUAL_ROWS_V4,
        summary="Moved flank wall lower",
        idempotency_key="p1-save-v4",
    )
    v4 = s4["currentVersionId"]
    clock.advance(5.0)
    await container.stage_service.assess_version(
        p1_sid, v4, p1_access, idempotency_key="p1-assess-v4"
    )

    # Policy 2 Binary Discovery Q1 -> choice -> Q2 -> choice -> Offer
    clock.advance(10.0)
    ask1 = await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v4,
        idempotency_key="p1-bin-start",
        content="Make center simpler",
        request_proposal=True,
    )
    pq1 = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(ask1["turns"])
        if t["guidance"].get("proposalQuestion")
    )
    clock.advance(10.0)
    ask2 = await container.proposal_service.choose_binary_choice(
        p1_sid,
        p1_access,
        topic_id=pq1["topicId"],
        question_id=pq1["questionId"],
        choice_id=pq1["choices"][0]["choiceId"],
        base_version_id=v4,
        idempotency_key="p1-bin-c1",
    )
    pq2 = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(ask2["turns"])
        if t["guidance"].get("proposalQuestion")
    )
    clock.advance(10.0)
    planned = await container.proposal_service.choose_binary_choice(
        p1_sid,
        p1_access,
        topic_id=pq2["topicId"],
        question_id=pq2["questionId"],
        choice_id=pq2["choices"][0]["choiceId"],
        base_version_id=v4,
        idempotency_key="p1-bin-c2",
    )
    offer_turn = next(
        t
        for t in reversed(planned["turns"])
        if isinstance(t["guidance"].get("proposalOffer"), dict)
    )

    # Player Challenge -> reason -> revise_proposal -> replacement offer -> execute -> accept
    clock.advance(10.0)
    started_chg = await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v4,
        idempotency_key="p1-chg-start",
        content="Challenge this plan",
        action="challenge_revision",
        assistant_turn_id=offer_turn["turnId"],
    )
    chg_turn = started_chg["turns"][-1]
    challenge_id = chg_turn["guidance"]["challengeCard"]["challengeId"]
    clock.advance(10.0)
    await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v4,
        idempotency_key="p1-chg-reason",
        content="I prefer to remove the water tile instead of the wall.",
        action="send",
    )
    clock.advance(10.0)
    revised_chg = await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v4,
        idempotency_key="p1-chg-revise",
        content="No",
        action="send",
        challenge_choice="revise_proposal",
    )
    repl_offer_turn = revised_chg["turns"][-1]
    assert isinstance(repl_offer_turn["guidance"].get("proposalOffer"), dict)

    clock.advance(10.0)
    executed = await container.message_orchestrator.send_message(
        p1_sid,
        p1_access,
        base_version_id=v4,
        idempotency_key="p1-exec-repl",
        content="Draft this suggestion",
        action="execute_revision",
        assistant_turn_id=repl_offer_turn["turnId"],
    )
    prop_id = executed["proposals"][0]["proposalId"]
    clock.advance(10.0)
    accepted = await container.proposal_service.decide_proposal(
        p1_sid,
        prop_id,
        p1_access,
        decision="accept",
        base_version_id=v4,
        idempotency_key="p1-acc-repl",
    )
    v5 = accepted["currentVersionId"]

    # Play attempt #1 on Stage 5 (abandoned)
    clock.advance(5.0)
    pa1 = await container.stage_service.create_play_attempt(
        p1_sid, v5, p1_access, idempotency_key="p1-pa-1"
    )
    pa1_ticket = extract_query_param(pa1["playUrl"], "cocreationPlay")
    pa1_boot = await container.stage_service.bootstrap_play_attempt(pa1["attemptId"], pa1_ticket)
    await container.stage_service.start_play_attempt(pa1["attemptId"], pa1_boot["attemptToken"])
    clock.advance(4.0)
    await container.stage_service.abandon_play_attempt(
        pa1["attemptId"],
        pa1_boot["attemptToken"],
        duration_seconds=4.0,
        move_count=3,
        push_count=0,
        restart_count=1,
    )

    # Restore Stage 4 replacing Stage 5 -> Stage 6 (restored)
    clock.advance(10.0)
    restored = await container.stage_service.restore_version(
        p1_sid, v4, p1_access, base_version_id=v5, idempotency_key="p1-restore-v4"
    )
    v6 = restored["currentVersionId"]

    # Play attempt #2 on Stage 6 (completed)
    clock.advance(5.0)
    pa2 = await container.stage_service.create_play_attempt(
        p1_sid, v6, p1_access, idempotency_key="p1-pa-2"
    )
    pa2_ticket = extract_query_param(pa2["playUrl"], "cocreationPlay")
    pa2_boot = await container.stage_service.bootstrap_play_attempt(pa2["attemptId"], pa2_ticket)
    await container.stage_service.start_play_attempt(pa2["attemptId"], pa2_boot["attemptToken"])
    clock.advance(8.0)
    await container.stage_service.complete_play_attempt(
        pa2["attemptId"],
        pa2_boot["attemptToken"],
        duration_seconds=8.0,
        move_count=7,
        push_count=2,
        restart_count=0,
    )

    # Advance clock so elapsed co-creation duration is 1450s (> 1200s!) and finalize P1
    clock.advance(1253.0)
    p1_finalized = await container.session_lifecycle_service.finalize_session(
        p1_sid, p1_access, base_version_id=v6, idempotency_key="p1-finalize"
    )
    assert p1_finalized["status"] == "awaiting_intention"
    p1_final_rows = next(
        v["rows"]
        for v in p1_finalized["versions"]
        if v["versionId"] == p1_finalized["finalVersionId"]
    )
    clock.advance(5.0)
    await container.session_lifecycle_service.submit_intention(
        p1_sid,
        p1_access,
        content="Restored the open center with a lower flank anchor for clean pacing.",
        idempotency_key="p1-intention",
    )

    # --- Player 2 co-creation session ---
    clock.advance(5.0)
    p2_created = await container.session_lifecycle_service.create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="c2a-p2-create",
        match_id=match_id,
        player_number=2,
    )
    p2_sid = p2_created["sessionId"]
    p2_boot = extract_hash_param(p2_created["launchUrl"], "bootstrap")
    p2_access = (
        await container.session_lifecycle_service.authorize_browser_access(p2_sid, p2_boot)
    )["accessToken"]
    clock.advance(5.0)
    p2_s1 = await container.session_lifecycle_service.update_language(
        p2_sid, p2_access, language="en"
    )
    p2_v1 = p2_s1["currentVersionId"]

    # Stage 1 Opening WITH cards (left unconsumed by discussion -> visible on Stage 1!)
    clock.advance(5.0)
    await container.stage_service.assess_version(
        p2_sid, p2_v1, p2_access, idempotency_key="p2-open-v1"
    )

    # Player 2 Binary Discovery -> Offer -> Player Challenge (keep_proposal -> original_selected)
    # -> Reject executed proposal
    clock.advance(10.0)
    p2_ask = await container.message_orchestrator.send_message(
        p2_sid,
        p2_access,
        base_version_id=p2_v1,
        idempotency_key="p2-bin-start",
        content="Make center simpler",
        request_proposal=True,
    )
    p2_pq = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(p2_ask["turns"])
        if t["guidance"].get("proposalQuestion")
    )
    clock.advance(10.0)
    p2_planned = await container.proposal_service.choose_binary_choice(
        p2_sid,
        p2_access,
        topic_id=p2_pq["topicId"],
        question_id=p2_pq["questionId"],
        choice_id=p2_pq["choices"][0]["choiceId"],
        base_version_id=p2_v1,
        idempotency_key="p2-bin-c1",
    )
    p2_offer_turn = next(
        t
        for t in reversed(p2_planned["turns"])
        if isinstance(t["guidance"].get("proposalOffer"), dict)
    )
    clock.advance(10.0)
    await container.message_orchestrator.send_message(
        p2_sid,
        p2_access,
        base_version_id=p2_v1,
        idempotency_key="p2-chg-start",
        content="Challenge this plan",
        action="challenge_revision",
        assistant_turn_id=p2_offer_turn["turnId"],
    )
    clock.advance(10.0)
    await container.message_orchestrator.send_message(
        p2_sid,
        p2_access,
        base_version_id=p2_v1,
        idempotency_key="p2-chg-reason",
        content="Maybe keep the water tile instead.",
        action="send",
    )
    clock.advance(10.0)
    p2_kept = await container.message_orchestrator.send_message(
        p2_sid,
        p2_access,
        base_version_id=p2_v1,
        idempotency_key="p2-chg-keep",
        content="Yes",
        action="send",
        challenge_choice="keep_proposal",
    )
    p2_prop_id = p2_kept["proposals"][0]["proposalId"]
    clock.advance(10.0)
    await container.proposal_service.decide_proposal(
        p2_sid,
        p2_prop_id,
        p2_access,
        decision="reject",
        base_version_id=p2_v1,
        idempotency_key="p2-rej-prop",
        reason="Keeping my Stage 1 layout as-is.",
    )

    clock.advance(360.0)
    await container.session_lifecycle_service.finalize_session(
        p2_sid, p2_access, base_version_id=p2_v1, idempotency_key="p2-finalize"
    )
    clock.advance(5.0)
    await container.session_lifecycle_service.submit_intention(
        p2_sid,
        p2_access,
        content="Clean short puzzle focusing on direct push order.",
        idempotency_key="p2-intention",
    )

    # Submit challenges & cross-player results (P1 completed P2's challenge; P2 timed out on P1's)
    clock.advance(5.0)
    await container.online_room_service.submit_challenge(match_id, p1_tok, rows=p1_final_rows)
    await container.online_room_service.submit_challenge(match_id, p2_tok, rows=BASE_ROWS)
    clock.advance(20.0)
    await container.online_room_service.submit_result(
        match_id,
        p1_tok,
        {
            "durationSeconds": 18.5,
            "moveCount": 8,
            "minimumMoves": 6,
            "restartCount": 2,
            "outcome": "completed",
        },
    )
    clock.advance(20.0)
    await container.online_room_service.submit_result(
        match_id,
        p2_tok,
        {
            "durationSeconds": 180.0,
            "moveCount": 24,
            "minimumMoves": 6,
            "restartCount": 3,
            "outcome": "timed_out",
        },
    )

    return {
        "app": app,
        "container": container,
        "clock": clock,
        "match_id": match_id,
        "p1_sid": p1_sid,
        "p2_sid": p2_sid,
        "demo_sid": demo_sid,
        "challenge_id": challenge_id,
        "hyp_id": hyp_id,
        "topic_id_p1": pq1["topicId"],
        "topic_id_p2": p2_pq["topicId"],
    }


async def test_authoritative_dashboard_projection_and_contract_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify full authoritative Dashboard projection AND keep frontend contract fixture synced."""
    env = await _build_full_contract_environment(tmp_path, monkeypatch)
    container = env["container"]
    match_id = env["match_id"]

    # #6: IntentService must be wired to online_room_service & operational_repo
    assert container.intent_service._room_sync is container.online_room_service
    assert container.intent_service._operational_repo is container.operational_repo

    # #16 & Polish #1: ZERO raw eventType == "turn" or fragmentary "node" rows in online_room_events
    raw_room_events = container.rooms_repo.list_room_events(match_id)
    assert not any(ev.event_type == "turn" for ev in raw_room_events)
    assert not any(ev.event_type == "node" for ev in raw_room_events)
    raw_event_types = {ev.event_type for ev in raw_room_events}
    assert {
        "draft",
        "first_stage",
        "stage",
        "opening",
        "final",
        "message",
        "challenge_submitted",
        "designer_intention_synchronized",
        "result_submitted",
    }.issubset(raw_event_types)

    # #13: Opening sync event in online_room_events carries real cards (not hardcoded [])
    opening_room_events = [ev.payload for ev in raw_room_events if ev.event_type == "opening"]
    assert any(len(ev.get("cards") or []) >= 1 for ev in opening_room_events)

    # Run real DashboardService.get_matchmaking_records()
    payload = await container.dashboard_service.get_matchmaking_records()
    _assert_no_secret_keys(payload)
    assert payload["summary"]["matchCount"] == 1
    assert payload["summary"]["completedCount"] == 1

    match = payload["matches"][0]
    assert match["matchId"] == match_id
    assert match["status"] == "completed"
    p1, p2 = match["players"][0], match["players"][1]
    p1_flow = p1["coCreationFlow"]
    p2_flow = p2["coCreationFlow"]

    # Zero raw "turn" events in public projection
    assert not any(ev.get("eventType") == "turn" for ev in p1_flow)
    assert not any(ev.get("eventType") == "turn" for ev in p2_flow)

    # Player 1: Draft + Draft Regenerations + Stage solver + Play Attempts
    assert any(ev["eventType"] == "draft" for ev in p1_flow)
    assert len(p1["draftRegenerations"]) == 2
    assert [r["status"] for r in p1["draftRegenerations"]] == ["failed", "completed"]
    assert len(p1["playAttempts"]) == 2
    assert {a["status"] for a in p1["playAttempts"]} == {"abandoned", "completed"}

    stages_p1 = [ev for ev in p1_flow if ev["eventType"] in {"first_stage", "stage"}]
    assert len(stages_p1) == 6
    assert all(isinstance(st.get("solver"), dict) for st in stages_p1)
    assert all(st["solver"]["solvable"] is True for st in stages_p1)
    assert isinstance(stages_p1[0]["solver"]["minSteps"], int)
    restored_stage = next(st for st in stages_p1 if st.get("source") == "restored")
    assert restored_stage["restoredFromStageNumber"] == 4
    assert restored_stage["replacedStageNumber"] == 5
    assert len(restored_stage["playAttempts"]) == 1
    assert restored_stage["playAttempts"][0]["status"] == "completed"

    # Opening cards & consumption: P1 Stage 1 consumed by discussion; P2 Stage 1 unconsumed
    p1_opening = next(ev for ev in p1_flow if ev["eventType"] == "opening")
    assert len(p1_opening["cards"]) >= 1
    assert p1_opening["consumed"] is True
    p2_opening = next(ev for ev in p2_flow if ev["eventType"] == "opening")
    assert len(p2_opening["cards"]) >= 1
    assert p2_opening["consumed"] is False

    # Ordinary discussion node preserved (not dropped!)
    p1_discussions = [
        ev for ev in p1_flow if ev["eventType"] == "node" and ev["nodeType"] == "discussion"
    ]
    assert len(p1_discussions) >= 1
    assert p1_discussions[0].get("openingAssistantTurnId") == p1_opening["assistantTurnId"]
    assert any(
        "center corridor" in str(e.get("text") or "").lower()
        for e in p1_discussions[0]["nodeEntries"]
    )

    # Intent node: full history preserved + status == "revised" + studySessionId + no typo
    p1_intents = [
        ev for ev in p1_flow if ev["eventType"] == "node" and ev["nodeType"] == "intent"
    ]
    assert len(p1_intents) == 1
    assert p1_intents[0]["nodeStatus"] == "revised"
    assert p1_intents[0]["studySessionId"] == "study-p1-full-uuid"
    assert all(
        "Reviseed" not in str(e.get("text") or "") for e in p1_intents[0]["nodeEntries"]
    )
    intent_kinds = [e["kind"] for e in p1_intents[0]["nodeEntries"]]
    assert {"player_message", "llm_message", "card", "player_action"}.issubset(set(intent_kinds))

    # Manual Edit Review + separate LLM Challenge nodes (acknowledged AND manual_reedit!)
    p1_manual_reviews = [
        ev
        for ev in p1_flow
        if ev["eventType"] == "node" and ev["nodeType"] == "manual_edit_review"
    ]
    assert len(p1_manual_reviews) == 3
    p1_llm_challenges = [
        ev for ev in p1_flow if ev["eventType"] == "node" and ev["nodeType"] == "llm_challenge"
    ]
    assert len(p1_llm_challenges) == 2
    assert [c["nodeStatus"] for c in p1_llm_challenges] == ["acknowledged", "manual_reedit"]
    assert all(
        str(c.get("nodeParentId") or "").startswith("manual-review:") for c in p1_llm_challenges
    )

    # Policy 2 Proposal (full Q1-Q2 history!) + Player Challenge (replacement_selected)
    # + unified child Proposal (accepted, ZERO split orphan nodes!)
    p1_proposals = [
        ev for ev in p1_flow if ev["eventType"] == "node" and ev["nodeType"] == "proposal"
    ]
    assert len(p1_proposals) == 2
    orig_prop = next(p for p in p1_proposals if p["nodeId"] == f"proposal:{env['topic_id_p1']}")
    # Must contain BOTH Q1 & Q2 discovery rounds + choices (never overwritten!)
    orig_texts = " ".join(str(e.get("text") or "") for e in orig_prop["nodeEntries"])
    assert "Make center simpler" in orig_texts
    assert "Which simplification works better?" in orig_texts
    assert "Should we also clear the lower flank wall or the central water tile?" in orig_texts

    p1_player_chgs = [
        ev for ev in p1_flow if ev["eventType"] == "node" and ev["nodeType"] == "player_challenge"
    ]
    assert len(p1_player_chgs) == 1
    p1_chg = p1_player_chgs[0]
    assert p1_chg["nodeParentId"] == orig_prop["nodeId"]
    assert p1_chg["nodeStatus"] == "replacement_selected"
    chg_texts = " ".join(str(e.get("text") or "") for e in p1_chg["nodeEntries"])
    assert "remove the water tile" in chg_texts.lower()
    assert "opens vertical movement" in chg_texts.lower()

    repl_prop = next(p for p in p1_proposals if p.get("nodeParentId") == p1_chg["nodeId"])
    assert repl_prop["nodeStatus"] == "accepted"
    assert any(e["kind"] == "map_diff" for e in repl_prop["nodeEntries"])
    assert any("Accepted" in str(e.get("text") or "") for e in repl_prop["nodeEntries"])

    # Player 2: keep_proposal -> original_selected + rejected proposal
    p2_chgs = [
        ev for ev in p2_flow if ev["eventType"] == "node" and ev["nodeType"] == "player_challenge"
    ]
    assert len(p2_chgs) == 1 and p2_chgs[0]["nodeStatus"] == "original_selected"
    p2_proposals = [
        ev for ev in p2_flow if ev["eventType"] == "node" and ev["nodeType"] == "proposal"
    ]
    assert any(p["nodeStatus"] == "rejected" for p in p2_proposals)

    # #22: Final duration > 1200s works cleanly without truncation or validation error!
    p1_final = next(ev for ev in p1_flow if ev["eventType"] == "final")
    assert p1_final["coCreationDurationSeconds"] > 1200
    assert p1_final["opponentRestartCount"] == 3
    p2_final = next(ev for ev in p2_flow if ev["eventType"] == "final")
    assert p2_final["opponentRestartCount"] == 2
    assert p1["challenge"]["runResult"]["outcome"] == "timed_out"
    assert p2["challenge"]["runResult"]["outcome"] == "completed"

    # Contract fixture check (must stay strictly in sync with live backend output!)
    assert FIXTURE_PATH.exists(), f"Missing contract fixture at {FIXTURE_PATH}"
    checked_in = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload == checked_in


async def test_research_export_ndjson_excludes_demo_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify GET /export/matchmaking-research.jsonl (#7) and /export/study-logs.jsonl."""
    env = await _build_full_contract_environment(tmp_path, monkeypatch)
    import httpx

    transport = httpx.ASGITransport(app=env["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/export/matchmaking-research.jsonl")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        assert 'filename="matchmaking-research.jsonl"' in resp.headers["content-disposition"]

        lines = [line for line in resp.text.splitlines() if line.strip()]
        assert len(lines) == 1
        row = json.loads(lines[0])
        _assert_no_secret_keys(row)
        assert row["schemaVersion"] == 1
        assert row["exportedAt"]
        assert row["match"]["matchId"] == env["match_id"]
        assert isinstance(row["roomEvents"], list) and len(row["roomEvents"]) > 0
        # Polish #1: roomEvents holds skeleton events only (ZERO node/turn rows) while
        # match.players[].coCreationFlow projects ALL authoritative nodes.
        exported_room_types = {ev["eventType"] for ev in row["roomEvents"]}
        assert "node" not in exported_room_types
        assert "turn" not in exported_room_types
        assert {"draft", "first_stage", "stage", "opening", "final", "message"}.issubset(
            exported_room_types
        )
        p1_export_node_types = {
            ev["nodeType"]
            for ev in row["match"]["players"][0]["coCreationFlow"]
            if ev.get("eventType") == "node"
        }
        assert {
            "discussion",
            "intent",
            "manual_edit_review",
            "llm_challenge",
            "proposal",
            "player_challenge",
        }.issubset(p1_export_node_types)
        assert set(row["coCreationSessions"].keys()) == {"1", "2"}

        p1_bundle = row["coCreationSessions"]["1"]
        assert p1_bundle["session"]["sessionId"] == env["p1_sid"]
        assert p1_bundle["session"]["isDemo"] is False
        assert len(p1_bundle["levelVersions"]) == 6
        assert len(p1_bundle["conversationTurns"]) > 0
        assert len(p1_bundle["changeProposals"]) >= 1
        assert len(p1_bundle["revisionChallenges"]) == 1
        assert len(p1_bundle["playAttempts"]) == 2
        assert len(p1_bundle["draftRegenerations"]) == 2
        assert len(p1_bundle["binaryDiscoveryTopics"]) == 1
        assert len(p1_bundle["auditEvents"]) > 0
        assert "componentRecoveryTasks" in p1_bundle

        # Demo session must NEVER appear in the export text
        assert env["demo_sid"] not in resp.text

        # Legacy /export/study-logs.jsonl remains intact
        study_resp = await client.get("/export/study-logs.jsonl")
        assert study_resp.status_code == 200
        study_lines = [json.loads(ln) for ln in study_resp.text.splitlines() if ln.strip()]
        assert len(study_lines) == 1 and study_lines[0]["participantId"] == "study-p1-full-uuid"


async def test_delete_and_clear_cascade_design_sessions_but_preserve_demo_and_study(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify #21: delete_online_match & clear_matchmaking_records cascade formal sessions only."""
    env = await _build_full_contract_environment(tmp_path, monkeypatch)
    container = env["container"]
    db = container.db
    match_id = env["match_id"]
    p1_sid = env["p1_sid"]
    p2_sid = env["p2_sid"]
    demo_sid = env["demo_sid"]

    # Also add a standalone formal session (match_id=None) that clear_matches must preserve
    standalone = await container.session_lifecycle_service.create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="standalone-keep",
        match_id=None,
        player_number=None,
    )
    standalone_sid = standalone["sessionId"]

    # Add a second room + formal session to test clear_matchmaking_records afterwards
    r2 = await container.online_room_service.create_room(
        study_session_id="study-m2", menu_started_flow=True
    )
    m2_id = r2["matchId"]
    m2_sess = await container.session_lifecycle_service.create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="m2-sess-create",
        match_id=m2_id,
        player_number=1,
    )
    m2_sid = m2_sess["sessionId"]

    # Delete match 1
    deleted = await container.dashboard_service.delete_online_match(
        match_id, supplied_password="delete-pass-123"
    )
    assert deleted["matchId"] == match_id
    assert deleted["deletedSessionCount"] == 2

    child_tables = (
        "design_sessions",
        "level_versions",
        "conversation_turns",
        "change_proposals",
        "revision_challenges",
        "play_attempts",
        "draft_regenerations",
        "binary_discovery_topics",
        "component_recovery_tasks",
        "audit_events",
        "idempotency_keys",
    )
    with db.read_conn() as conn:
        for tbl in child_tables:
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE session_id IN (?, ?)",
                (p1_sid, p2_sid),
            ).fetchone()[0]
            assert cnt == 0, f"Orphaned rows remain in {tbl}"
        # Demo, standalone, match 2, and study_records are ALL preserved
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE session_id = ?", (demo_sid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE session_id = ?", (standalone_sid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE session_id = ?", (m2_sid,)
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM study_records").fetchone()[0] == 1

    # Clear all remaining matchmaking records
    cleared = await container.dashboard_service.clear_matchmaking_records(
        supplied_password="delete-pass-123"
    )
    assert cleared["cleared"] is True
    assert cleared["deletedSessionCount"] == 1
    with db.read_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE session_id = ?", (m2_sid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE session_id = ?", (demo_sid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM design_sessions WHERE session_id = ?", (standalone_sid,)
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM study_records").fetchone()[0] == 1


async def test_historical_fallback_backfills_fields_and_strips_raw_turn(tmp_path: Path) -> None:
    """Verify #24 + #16: legacy rooms without design_sessions backfill playerNumber/eventType
    and strip legacy eventType='turn' rows."""
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "hist.sqlite3")
    db.initialize()
    rooms_repo = RoomsRepository(db)
    room_svc = OnlineRoomService(rooms_repo, clock=clock)
    dash = DashboardService(rooms_repo, db=db, delete_password="pw", clock=clock)

    r1 = await room_svc.create_room(study_session_id="hist-p1", menu_started_flow=True)
    mid = r1["matchId"]
    with db.write_tx() as conn:
        # Insert legacy first_stage missing playerNumber and eventType inside payload_json
        conn.execute(
            "INSERT INTO online_room_events("
            "match_id, player_slot, seq, event_type, payload_json, created_at"
            ") VALUES (?, 1, 90, 'first_stage', ?, ?)",
            (
                mid,
                dump_json(
                    {
                        "eventId": "legacy-fs-1",
                        "sessionId": "legacy-s1",
                        "versionId": "legacy-v1",
                        "stageNumber": 1,
                        "rows": BASE_ROWS,
                    }
                ),
                "2026-01-01T00:01:00Z",
            ),
        )
        # Insert legacy raw turn row (must be stripped from public projection!)
        conn.execute(
            "INSERT INTO online_room_events("
            "match_id, player_slot, seq, event_type, payload_json, created_at"
            ") VALUES (?, 1, 91, 'turn', ?, ?)",
            (
                mid,
                dump_json(
                    {
                        "eventId": "legacy-turn-1",
                        "eventType": "turn",
                        "sessionId": "legacy-s1",
                        "versionId": "legacy-v1",
                        "userText": "raw secret prompt",
                        "assistantText": "raw reply",
                    }
                ),
                "2026-01-01T00:01:05Z",
            ),
        )

    proj = await dash.get_matchmaking_records()
    assert proj["summary"]["matchCount"] == 1
    flow = proj["matches"][0]["players"][0]["coCreationFlow"]
    assert len(flow) == 1
    assert flow[0]["eventType"] == "first_stage"
    assert flow[0]["playerNumber"] == 1
    assert flow[0]["matchId"] == mid
    assert not any(ev.get("eventType") == "turn" for ev in flow)


def test_final_flow_event_allows_duration_above_1200_seconds() -> None:
    """Verify #22: FinalFlowEvent supports configurable COCREATION_DEADLINE_SECONDS > 1200."""
    adapter: TypeAdapter[CoCreationFlowEvent] = TypeAdapter(CoCreationFlowEvent)
    parsed = adapter.validate_python(
        {
            "eventId": "ev-final-long",
            "eventType": "final",
            "playerNumber": 1,
            "sessionId": "sess-long",
            "versionId": "v1",
            "stageNumber": 1,
            "rows": BASE_ROWS,
            "coCreationDurationSeconds": 1850.5,
        }
    )
    assert parsed.co_creation_duration_seconds == 1850.5

    with pytest.raises(PydanticValidationError):
        adapter.validate_python(
            {
                "eventId": "ev-final-neg",
                "eventType": "final",
                "playerNumber": 1,
                "sessionId": "sess-long",
                "versionId": "v1",
                "stageNumber": 1,
                "rows": BASE_ROWS,
                "coCreationDurationSeconds": -1.0,
            }
        )
    with pytest.raises(PydanticValidationError):
        adapter.validate_python(
            {
                "eventId": "ev-final-huge",
                "eventType": "final",
                "playerNumber": 1,
                "sessionId": "sess-long",
                "versionId": "v1",
                "stageNumber": 1,
                "rows": BASE_ROWS,
                "coCreationDurationSeconds": 90000.0,
            }
        )


async def test_authoritative_projection_status_variants(tmp_path: Path) -> None:
    """Verify clarifying_1..3, cancelled, review_pending, awaiting_reason, exited, confirmed."""
    clock = FakeClock(utc_time=datetime(2026, 11, 27, 9, 0, 0, tzinfo=UTC))
    db = SQLiteDatabase(tmp_path / "variants.sqlite3")
    db.initialize()
    settings = SokobanSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "variants.sqlite3",
        study_logs_dir=tmp_path / "study_logs",
        token_secret="variant-secret-key-1234567890",
        dashboard_delete_password="pw",
        reaper_enabled=False,
    )
    container = build_container(
        settings=settings,
        clock=clock,
        db=db,
        deepseek_transport=FakeLLMTransport([]),
        cocreation_transport=FakeLLMTransport([]),
    )

    r1 = await container.online_room_service.create_room(
        study_session_id="var-p1", menu_started_flow=True
    )
    mid = r1["matchId"]
    sess = container.sessions_repo.create_session(
        session_id="var-sess-1",
        match_id=mid,
        player_slot=1,
        mode="partial_completion",
        language="en",
        access_token_hash="h1",
        integration_token_hash="h2",
        bootstrap_token_hash="h3",
        created_at="2026-11-27T09:00:00Z",
    )
    solved = solve_min_steps(BASE_ROWS)
    container.sessions_repo.insert_version(
        version_id="var-v1",
        session_id=sess.session_id,
        version_number=1,
        source="initial",
        summary="Initial stage",
        rows=list(BASE_ROWS),
        solver=solved.as_dict(),
        design_context={
            "intentHypotheses": [
                {"id": "hyp-conf", "statement": "Confirmed goal", "status": "confirmed"},
                {"id": "hyp-wait", "statement": "Pending goal", "status": "tentative"},
            ]
        },
        created_at="2026-11-27T09:00:05Z",
    )

    # 1) Clarifying round 2 topic
    container.operational_repo.upsert_topic(
        sess.session_id,
        "topic-c2",
        {
            "topicId": "topic-c2",
            "versionId": "var-v1",
            "status": "clarifying",
            "questionCount": 2,
            "initialRequest": "Tweak walls",
            "choices": [{"questionId": "q1", "choiceId": "c1", "label": "Left side"}],
            "pendingQuestion": {"questionId": "q2", "question": "Upper or lower?"},
        },
        "2026-11-27T09:00:10Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-c2-u",
        session_id=sess.session_id,
        role="user",
        content="Tweak walls",
        version_id="var-v1",
        request_id="req-c2-1",
        created_at="2026-11-27T09:00:08Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-c2-a1",
        session_id=sess.session_id,
        role="assistant",
        content="Left or right?",
        version_id="var-v1",
        request_id="req-c2-1",
        guidance={
            "proposalDiscovery": {"topicId": "topic-c2", "status": "clarifying"},
            "proposalQuestion": {
                "topicId": "topic-c2",
                "questionId": "q1",
                "question": "Left or right?",
                "choices": [{"choiceId": "c1", "label": "Left side"}],
            },
        },
        created_at="2026-11-27T09:00:09Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-c2-a2",
        session_id=sess.session_id,
        role="assistant",
        content="Upper or lower?",
        version_id="var-v1",
        request_id="req-c2-2",
        guidance={
            "proposalDiscovery": {"topicId": "topic-c2", "status": "clarifying"},
            "proposalQuestion": {
                "topicId": "topic-c2",
                "questionId": "q2",
                "question": "Upper or lower?",
                "choices": [{"choiceId": "c2", "label": "Upper"}],
            },
        },
        created_at="2026-11-27T09:00:10Z",
    )

    # 2) Cancelled topic
    container.operational_repo.upsert_topic(
        sess.session_id,
        "topic-cancel",
        {
            "topicId": "topic-cancel",
            "versionId": "var-v1",
            "status": "cancelled",
            "questionCount": 1,
            "initialRequest": "Cancel me",
            "choices": [],
        },
        "2026-11-27T09:00:20Z",
    )
    container.dialogue_repo.append_event(
        sess.session_id,
        "binary_discovery_cancelled",
        {"topicId": "topic-cancel"},
        "2026-11-27T09:00:20Z",
    )

    # 3) Player challenges: review_pending & exited
    container.dialogue_repo.insert_turn(
        turn_id="t-offer-src",
        session_id=sess.session_id,
        role="assistant",
        content="Standalone offer",
        version_id="var-v1",
        guidance={"move": "offer_revision", "proposalOffer": {"summary": "Remove wall"}},
        created_at="2026-11-27T09:00:25Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-chg-rp-a",
        session_id=sess.session_id,
        role="assistant",
        content="Why challenge?",
        version_id="var-v1",
        guidance={
            "challengeCard": {"challengeId": "chg-rp", "status": "awaiting_reason"},
            "challengeState": {"challengeId": "chg-rp", "status": "awaiting_reason"},
        },
        created_at="2026-11-27T09:00:26Z",
    )
    container.dialogue_repo.upsert_challenge(
        challenge_id="chg-rp",
        session_id=sess.session_id,
        base_version_id="var-v1",
        source_proposal_turn_id="t-offer-src",
        challenge_turn_id="t-chg-rp-a",
        status="awaiting_reason",
        created_at="2026-11-27T09:00:26Z",
        updated_at="2026-11-27T09:00:26Z",
    )
    container.dialogue_repo.append_event(
        sess.session_id,
        "challenge_reason_review_pending",
        {"challengeId": "chg-rp", "code": "LLM_TIMEOUT"},
        "2026-11-27T09:00:27Z",
    )

    container.dialogue_repo.insert_turn(
        turn_id="t-chg-ex-a1",
        session_id=sess.session_id,
        role="assistant",
        content="Why challenge?",
        version_id="var-v1",
        guidance={"challengeCard": {"challengeId": "chg-ex", "status": "awaiting_reason"}},
        created_at="2026-11-27T09:00:30Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-chg-ex-a2",
        session_id=sess.session_id,
        role="assistant",
        content="Exited challenge.",
        version_id="var-v1",
        guidance={
            "challengeCard": {
                "challengeId": "chg-ex",
                "status": "resolved",
                "selectedChoice": "cancel_challenge",
            },
            "disagreement": {
                "challengeId": "chg-ex",
                "status": "resolved",
                "resolution": "retain_current",
            },
        },
        created_at="2026-11-27T09:00:31Z",
    )
    container.dialogue_repo.upsert_challenge(
        challenge_id="chg-ex",
        session_id=sess.session_id,
        base_version_id="var-v1",
        source_proposal_turn_id="t-offer-src",
        challenge_turn_id="t-chg-ex-a1",
        status="resolved",
        created_at="2026-11-27T09:00:30Z",
        updated_at="2026-11-27T09:00:31Z",
    )

    # 4) Intents: confirmed + awaiting_player_choice
    for idx, (hid, stmt, ts) in enumerate(
        [
            ("hyp-conf", "Confirmed goal", "2026-11-27T09:00:40Z"),
            ("hyp-wait", "Pending goal", "2026-11-27T09:00:45Z"),
        ]
    ):
        container.dialogue_repo.insert_turn(
            turn_id=f"t-int-u-{idx}",
            session_id=sess.session_id,
            role="user",
            content=f"User message for {hid}",
            version_id="var-v1",
            request_id=f"req-int-{idx}",
            created_at=ts,
        )
        container.dialogue_repo.insert_turn(
            turn_id=f"t-int-a-{idx}",
            session_id=sess.session_id,
            role="assistant",
            content=f"Assistant reply for {hid}",
            version_id="var-v1",
            request_id=f"req-int-{idx}",
            guidance={"_intentHypothesisId": hid, "intentHypothesis": stmt},
            created_at=ts,
        )
    container.dialogue_repo.append_event(
        sess.session_id,
        "intent_hypothesis_feedback_applied",
        {"hypothesisId": "hyp-conf", "versionId": "var-v1", "action": "confirm"},
        "2026-11-27T09:00:41Z",
    )

    proj = await container.dashboard_service.get_matchmaking_records()
    assert proj["summary"]["matchCount"] == 1
    nodes = {
        ev["nodeId"]: ev
        for ev in proj["matches"][0]["players"][0]["coCreationFlow"]
        if ev["eventType"] == "node"
    }
    assert nodes["proposal:topic-c2"]["nodeStatus"] == "clarifying_2"
    assert nodes["proposal:topic-cancel"]["nodeStatus"] == "cancelled"
    assert nodes["player-challenge:chg-rp"]["nodeStatus"] == "review_pending"
    assert nodes["player-challenge:chg-ex"]["nodeStatus"] == "exited"
    assert nodes["intent:hyp-conf"]["nodeStatus"] == "confirmed"
    assert nodes["intent:hyp-wait"]["nodeStatus"] == "awaiting_player_choice"


def test_load_formal_session_bundles_chunks_sql_in_queries_above_999_rows(
    tmp_path: Path,
) -> None:
    """Verify Polish #2: SQL IN (...) queries chunk bind params by <= 400 and keep ordering."""
    import re
    from contextlib import contextmanager

    from server.application.analytics._cocreation_projection import load_formal_session_bundles

    db = SQLiteDatabase(tmp_path / "chunked.sqlite3")
    db.initialize()

    total_matches = 1005
    match_ids: set[str] = set()
    rows_json = dump_json(BASE_ROWS)
    solver_json = dump_json({"solvable": True, "solutionSteps": 6, "solutionPushes": 2})

    # Seed 1005 formal sessions with interleaved created_at timestamps across chunk boundaries,
    # plus an older duplicate session on match-0500 slot 1 to verify latest-wins ordering.
    with db.write_tx() as conn:
        for i in range(total_matches):
            mid = f"match-{i:04d}"
            sid = f"sess-{i:04d}"
            match_ids.add(mid)
            # Interleave timestamps inversely so chunk order != created_at order
            ts = f"2026-11-27T{(i % 24):02d}:{(i % 60):02d}:{(i % 59):02d}Z"
            conn.execute(
                "INSERT INTO design_sessions("
                "session_id, match_id, player_slot, mode, language, status, "
                "secret_hash, access_token_hash, integration_token_hash, bootstrap_token_hash, "
                "is_demo, created_at, updated_at"
                ") VALUES (?, ?, 1, 'partial_completion', 'en', 'active', "
                "'sh', 'ah', 'ih', 'bh', 0, ?, ?)",
                (sid, mid, ts, ts),
            )
            if i in (0, 399, 400, 799, 800, 1004):
                for vnum in (1, 2):
                    conn.execute(
                        "INSERT INTO level_versions("
                        "version_id, session_id, version_number, source, summary, "
                        "rows_json, diff_json, solver_json, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)",
                        (
                            f"{sid}-v{vnum}",
                            sid,
                            vnum,
                            "initial" if vnum == 1 else "human_edit",
                            f"Stage {vnum}",
                            rows_json,
                            solver_json,
                            f"2026-11-27T10:00:0{vnum}Z",
                        ),
                    )
                for seq in (1, 2):
                    conn.execute(
                        "INSERT INTO conversation_turns("
                        "turn_id, session_id, sequence_number, request_id, role, content, "
                        "language, guidance_json, model, version_id, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'en', '{}', 'test', ?, ?)",
                        (
                            f"{sid}-t{seq}",
                            sid,
                            seq,
                            f"req-{seq}",
                            "user" if seq == 1 else "assistant",
                            f"Turn {seq}",
                            f"{sid}-v1",
                            f"2026-11-27T10:01:0{seq}Z",
                        ),
                    )
                conn.execute(
                    "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
                    "VALUES (?, 'test_ev', '{}', '2026-11-27T10:02:00Z')",
                    (sid,),
                )
        # Insert an earlier duplicate for match-0500 slot 1 (must lose to newer sess-0500)
        conn.execute(
            "INSERT INTO design_sessions("
            "session_id, match_id, player_slot, mode, language, status, "
            "secret_hash, access_token_hash, integration_token_hash, bootstrap_token_hash, "
            "is_demo, created_at, updated_at"
            ") VALUES ('sess-0500-old', 'match-0500', 1, 'partial_completion', 'en', 'active', "
            "'sh', 'ah', 'ih', 'bh', 0, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
        )

    in_clause_sizes: list[int] = []
    in_pattern = re.compile(r"\bIN\s*\(([^)]+)\)", re.IGNORECASE)
    orig_read_conn = db.read_conn

    @contextmanager
    def _traced_read_conn():
        with orig_read_conn() as conn:
            def _trace(sql: str) -> None:
                for match in in_pattern.finditer(sql):
                    inner = match.group(1)
                    # Ignore small constant tuples like player_slot IN (1, 2)
                    items = [part.strip() for part in inner.split(",") if part.strip()]
                    if len(items) > 2:
                        in_clause_sizes.append(len(items))

            conn.set_trace_callback(_trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    db.read_conn = _traced_read_conn  # type: ignore[method-assign]

    bundles_filtered = load_formal_session_bundles(db, match_ids=match_ids)
    assert len(bundles_filtered) == total_matches
    assert bundles_filtered[("match-0500", 1)].session.session_id == "sess-0500"
    for idx in (0, 399, 400, 799, 800, 1004):
        b = bundles_filtered[(f"match-{idx:04d}", 1)]
        assert [v.version_number for v in b.versions] == [1, 2]
        assert [t.sequence_number for t in b.turns] == [1, 2]
        assert len(b.audit_events) == 1

    # Also verify match_ids=None path chunks child table IN (...) queries by <= 400
    bundles_all = load_formal_session_bundles(db, match_ids=None)
    assert len(bundles_all) == total_matches
    assert bundles_all[("match-0500", 1)].session.session_id == "sess-0500"

    assert in_clause_sizes, "Expected traced IN (...) queries"
    assert max(in_clause_sizes) <= 400, f"Exceeded 400 bind params: max={max(in_clause_sizes)}"
    assert len(in_clause_sizes) >= 30


async def test_player_challenge_revise_exhausted_projects_revision_needed(
    tmp_path: Path,
) -> None:
    """Verify Polish #4: revise_proposal without child proposal projects revision_needed."""
    clock = FakeClock(utc_time=datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC))
    db = SQLiteDatabase(tmp_path / "chg_exhausted.sqlite3")
    db.initialize()
    settings = SokobanSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "chg_exhausted.sqlite3",
        study_logs_dir=tmp_path / "study_logs",
        token_secret="chg-exhausted-secret-key-123456",
        dashboard_delete_password="pw",
        reaper_enabled=False,
    )
    container = build_container(
        settings=settings,
        clock=clock,
        db=db,
        deepseek_transport=FakeLLMTransport([]),
        cocreation_transport=FakeLLMTransport([]),
    )

    r1 = await container.online_room_service.create_room(
        study_session_id="chg-ex-p1", menu_started_flow=True
    )
    mid = r1["matchId"]
    sess = container.sessions_repo.create_session(
        session_id="chg-ex-sess-1",
        match_id=mid,
        player_slot=1,
        mode="partial_completion",
        language="en",
        access_token_hash="h1",
        integration_token_hash="h2",
        bootstrap_token_hash="h3",
        created_at="2026-11-27T10:00:00Z",
    )
    solved = solve_min_steps(BASE_ROWS)
    container.sessions_repo.insert_version(
        version_id="chg-ex-v1",
        session_id=sess.session_id,
        version_number=1,
        source="initial",
        summary="Initial stage",
        rows=list(BASE_ROWS),
        solver=solved.as_dict(),
        created_at="2026-11-27T10:00:05Z",
    )

    # Case A: revise_proposal where ProposalSearchExhausted produced NO replacement proposal
    container.dialogue_repo.insert_turn(
        turn_id="t-offer-a",
        session_id=sess.session_id,
        role="assistant",
        content="Offer A",
        version_id="chg-ex-v1",
        guidance={"move": "offer_revision", "proposalOffer": {"summary": "Offer A summary"}},
        created_at="2026-11-27T10:00:10Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-chg-a-start",
        session_id=sess.session_id,
        role="assistant",
        content="Why challenge A?",
        version_id="chg-ex-v1",
        guidance={
            "challengeCard": {"challengeId": "chg-exhausted", "status": "awaiting_reason"},
            "challengeState": {"challengeId": "chg-exhausted", "status": "awaiting_reason"},
        },
        created_at="2026-11-27T10:00:11Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-chg-a-res",
        session_id=sess.session_id,
        role="assistant",
        content="Could not find a solvable replacement matching that reason.",
        version_id="chg-ex-v1",
        guidance={
            "move": "offer_perspective",
            "disagreement": {
                "challengeId": "chg-exhausted",
                "status": "resolved",
                "subject": "ai_revision_challenge",
                "resolution": "user",
                "displayCard": False,
            },
            "challengeState": {"challengeId": "chg-exhausted", "status": "resolved"},
            "challengeCard": {
                "challengeId": "chg-exhausted",
                "status": "resolved",
                "selectedChoice": "revise_proposal",
            },
        },
        created_at="2026-11-27T10:00:15Z",
    )
    container.dialogue_repo.upsert_challenge(
        challenge_id="chg-exhausted",
        session_id=sess.session_id,
        base_version_id="chg-ex-v1",
        source_proposal_turn_id="t-offer-a",
        challenge_turn_id="t-chg-a-start",
        status="resolved",
        created_at="2026-11-27T10:00:11Z",
        updated_at="2026-11-27T10:00:15Z",
    )

    # Case B: revise_proposal WITH a replacement offer -> still projects "replacement_selected"
    container.dialogue_repo.insert_turn(
        turn_id="t-offer-b",
        session_id=sess.session_id,
        role="assistant",
        content="Offer B",
        version_id="chg-ex-v1",
        guidance={"move": "offer_revision", "proposalOffer": {"summary": "Offer B summary"}},
        created_at="2026-11-27T10:00:20Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-chg-b-start",
        session_id=sess.session_id,
        role="assistant",
        content="Why challenge B?",
        version_id="chg-ex-v1",
        guidance={
            "challengeCard": {"challengeId": "chg-succeeded", "status": "awaiting_reason"},
            "challengeState": {"challengeId": "chg-succeeded", "status": "awaiting_reason"},
        },
        created_at="2026-11-27T10:00:21Z",
    )
    container.dialogue_repo.insert_turn(
        turn_id="t-chg-b-res",
        session_id=sess.session_id,
        role="assistant",
        content="Here is a replacement offer.",
        version_id="chg-ex-v1",
        guidance={
            "move": "offer_revision",
            "proposalOffer": {"summary": "Replacement B summary"},
            "disagreement": {
                "challengeId": "chg-succeeded",
                "status": "resolved",
                "subject": "ai_revision_challenge",
                "resolution": "user",
                "displayCard": False,
            },
            "challengeState": {"challengeId": "chg-succeeded", "status": "resolved"},
            "challengeCard": {
                "challengeId": "chg-succeeded",
                "status": "resolved",
                "selectedChoice": "revise_proposal",
            },
        },
        created_at="2026-11-27T10:00:25Z",
    )
    container.dialogue_repo.upsert_challenge(
        challenge_id="chg-succeeded",
        session_id=sess.session_id,
        base_version_id="chg-ex-v1",
        source_proposal_turn_id="t-offer-b",
        challenge_turn_id="t-chg-b-start",
        status="resolved",
        created_at="2026-11-27T10:00:21Z",
        updated_at="2026-11-27T10:00:25Z",
    )

    proj = await container.dashboard_service.get_matchmaking_records()
    nodes = {
        ev["nodeId"]: ev
        for ev in proj["matches"][0]["players"][0]["coCreationFlow"]
        if ev["eventType"] == "node"
    }
    assert nodes["player-challenge:chg-exhausted"]["nodeStatus"] == "revision_needed"
    assert "proposal:challenge:chg-exhausted" not in nodes
    assert nodes["proposal:offer:t-offer-a"]["nodeStatus"] == "proposal_ready"

    assert nodes["player-challenge:chg-succeeded"]["nodeStatus"] == "replacement_selected"
    assert "proposal:challenge:chg-succeeded" in nodes
    assert nodes["proposal:offer:t-offer-b"]["nodeStatus"] == "replaced"
