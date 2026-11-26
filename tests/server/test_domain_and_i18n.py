"""Zero-paid-LLM unit tests for server.domain and server.i18n."""

from __future__ import annotations

import json
import string

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from server.domain.blueprints import (
    DEFAULT_LEVEL_PLAN,
    DGContext,
    LevelDesignPlan,
    apply_selected_ha_plan,
    build_dg_fallback_summary,
    normalize_generation_preferences,
    resolve_zero_feature_constraints,
    validate_dg_guide_payload,
)
from server.domain.errors import (
    AuthError,
    ConflictError,
    DeadlineExceededError,
    DomainError,
    IdempotencyConflictError,
    NotFoundError,
    RateLimitError,
    UpstreamLLMError,
    ValidationError,
)
from server.domain.memory import (
    MAX_ACTIVE_INFERRED,
    DesignContextState,
    merge_intent_hypothesis,
    resolve_intent_hypothesis,
    sanitize_user_design_text,
)
from server.domain.proposals import (
    EDITABLE_OPERATORS,
    BinaryError,
    ProposalSearchExhausted,
    RevisionPlanError,
    build_tradeoff_notice,
    check_promises,
    connected_outer_shell,
    evaluate_requirements,
    parse_binary_step,
    parse_revision_plan,
    preflight_requirements,
    render_protected_refusal,
    search_revision_plan,
    validate_requirement_record,
)
from server.domain.rooms import (
    CoCreationFlowEvent,
    PlayResultSummary,
    RoomStatus,
    authenticate_room_player,
    create_room,
    expire_room,
    join_room,
    leave_room,
    serialize_public_room,
    set_player_ready,
    submit_player_challenge,
    submit_player_result,
)
from server.domain.sessions import (
    summarize_stage_changes,
    to_public_guidance,
)
from server.i18n.messages import (
    MESSAGES,
    format_map_claim_correction,
    format_verified_diff_summary,
    get_message,
    get_tile_label,
)

BASE_MAP = [
    "############",
    "#..........#",
    "#..p..s.t..#",
    "#....#.....#",
    "#..@.......#",
    "#..........#",
    "#.....s.t..#",
    "#..........#",
    "#..........#",
    "############",
]


class _StubValidation:
    def __init__(
        self,
        rows: list[str] | tuple[str, ...],
        *,
        steps: int = 10,
        pushes: int = 3,
        states: int = 25,
    ) -> None:
        self.rows = tuple(rows)
        self.solution_steps = steps
        self.solution_pushes = pushes
        self.searched_states = states


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_domain_error_hierarchy_and_serialization() -> None:
    non_retryable = (
        NotFoundError,
        ConflictError,
        ValidationError,
        AuthError,
        IdempotencyConflictError,
    )
    for cls in non_retryable:
        err = cls("SAMPLE_CODE", "error.validation_failed", details={"field": "x"})
        assert isinstance(err, DomainError)
        assert err.retryable is False
        assert err.to_dict() == {
            "code": "SAMPLE_CODE",
            "messageKey": "error.validation_failed",
            "retryable": False,
            "details": {"field": "x"},
        }

    for cls in (UpstreamLLMError, DeadlineExceededError, RateLimitError):
        err = cls("RETRY_CODE")
        assert isinstance(err, DomainError)
        assert err.retryable is True
    assert issubclass(IdempotencyConflictError, ConflictError)


# ---------------------------------------------------------------------------
# Rooms & CoCreationFlowEvent
# ---------------------------------------------------------------------------


def test_online_room_matchmaking_state_transitions() -> None:
    room = create_room(
        match_id="match-1",
        room_code="abc234",
        player_token="tok-1",
        now=10.0,
        study_session_id="study-1",
        menu_started_flow=True,
    )
    assert room.status == RoomStatus.WAITING_FOR_OPPONENT
    assert room.room_code == "ABC234"

    with pytest.raises(ConflictError):
        set_player_ready(room, player_number=1, ready=True, now=11.0)

    room, _p2 = join_room(
        room,
        player_token="tok-2",
        now=12.0,
        study_session_id="study-2",
        menu_started_flow=True,
    )
    assert room.status == RoomStatus.BRIEFING

    with pytest.raises(ConflictError):
        join_room(room, player_token="tok-3", now=13.0)

    assert authenticate_room_player(room, "tok-2").player_number == 2
    with pytest.raises(AuthError):
        authenticate_room_player(room, "wrong")

    room, changed1 = set_player_ready(room, player_number=1, ready=True, now=14.0)
    assert changed1 is True
    assert room.status == RoomStatus.BRIEFING

    room, changed2 = set_player_ready(room, player_number=2, ready=True, now=15.0)
    assert changed2 is True
    assert room.status == RoomStatus.WAITING_FOR_CHALLENGES

    room, c1 = submit_player_challenge(
        room,
        player_number=1,
        rows=BASE_MAP,
        ai_assistant_mode="description_generation",
        now=16.0,
    )
    assert c1 is True
    assert room.status == RoomStatus.WAITING_FOR_CHALLENGES

    room, c2 = submit_player_challenge(
        room,
        player_number=2,
        rows=BASE_MAP,
        ai_assistant_mode="partial_completion",
        now=17.0,
    )
    assert c2 is True
    assert room.status == RoomStatus.CHALLENGES_READY

    res1 = PlayResultSummary(durationSeconds=12.345, moveCount=14, minimumMoves=10, restartCount=1)
    room, _ = submit_player_result(room, player_number=1, result=res1, now=18.0)
    assert room.status == RoomStatus.WAITING_FOR_RESULTS

    room, _ = submit_player_result(room, player_number=2, result=res1, now=19.0)
    assert room.status == RoomStatus.RESULTS_READY

    serialized = serialize_public_room(room, player_number=1, include_token=True)
    assert serialized["playerToken"] == "tok-1"
    assert serialized["opponentChallengeRows"] == BASE_MAP
    assert serialized["ownResult"]["durationSeconds"] == 12.35

    room_after_leave, left_changed = leave_room(room, player_number=1, now=20.0)
    assert left_changed is False
    assert room_after_leave.status == RoomStatus.RESULTS_READY

    cancelled_room, cancelled = leave_room(
        create_room(match_id="m2", room_code="XYZ789", player_token="t1", now=1.0),
        player_number=1,
        now=2.0,
    )
    assert cancelled is True
    assert cancelled_room.status == RoomStatus.CANCELLED
    assert expire_room(cancelled_room).status == RoomStatus.CANCELLED


def test_cocreation_flow_event_discriminated_union() -> None:
    adapter = TypeAdapter(CoCreationFlowEvent)

    events = [
        {
            "eventId": "ev-1",
            "eventType": "first_stage",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "versionId": "ver-1",
            "stageNumber": 1,
            "initialDraftMethod": "description_generation",
            "rows": BASE_MAP,
        },
        {
            "eventId": "ev-2",
            "eventType": "stage",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "versionId": "ver-2",
            "stageNumber": 2,
            "source": "restored",
            "rows": BASE_MAP,
            "restoredFromVersionId": "ver-1",
            "restoredFromStageNumber": 1,
            "replacedVersionId": "ver-1b",
            "replacedStageNumber": 2,
        },
        {
            "eventId": "ev-3",
            "eventType": "opening",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "versionId": "ver-1",
            "stageNumber": 1,
            "assistantTurnId": "turn-1",
            "assistantText": "Opening analysis",
            "language": "en",
        },
        {
            "eventId": "ev-4",
            "eventType": "turn",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "versionId": "ver-1",
            "userText": "Make it tighter",
            "assistantText": "Let's check",
            "language": "en",
        },
        {
            "eventId": "ev-5",
            "eventType": "final",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "versionId": "ver-2",
            "stageNumber": 2,
            "rows": BASE_MAP,
            "coCreationDurationSeconds": 120.5,
        },
        {
            "eventId": "ev-6",
            "eventType": "node",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "nodeId": "node-1",
            "nodeType": "discussion",
            "nodeStatus": "active",
            "nodeEntries": [
                {
                    "entryId": "e-1",
                    "kind": "player_message",
                    "text": "Hello",
                    "occurredAt": "2026-11-26T00:00:00Z",
                }
            ],
        },
        {
            "eventId": "ev-7",
            "eventType": "draft",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "finalDifficulty": "Medium",
            "finalLayout": "Balanced",
            "q1Answer": "observe_then_decide",
        },
        {
            "eventId": "ev-8",
            "eventType": "message",
            "playerNumber": 1,
            "sessionId": "sess-1",
            "message": "Final intention",
        },
    ]

    parsed = [adapter.validate_python(item) for item in events]
    assert [item.event_type for item in parsed] == [
        "first_stage",
        "stage",
        "opening",
        "turn",
        "final",
        "node",
        "draft",
        "message",
    ]

    with pytest.raises(PydanticValidationError):
        adapter.validate_python(
            {
                "eventId": "ev-bad-bool",
                "eventType": "message",
                "playerNumber": True,
                "sessionId": "sess-1",
                "message": "hi",
            }
        )

    with pytest.raises(PydanticValidationError):
        adapter.validate_python(
            {
                "eventId": "ev-bad-restored",
                "eventType": "stage",
                "playerNumber": 1,
                "sessionId": "sess-1",
                "versionId": "v2",
                "stageNumber": 2,
                "source": "restored",
                "rows": BASE_MAP,
            }
        )


# ---------------------------------------------------------------------------
# Blueprints, HA Deterministic Contract & DG
# ---------------------------------------------------------------------------


def test_level_design_plan_and_deterministic_ha_application() -> None:
    plan = LevelDesignPlan.model_validate(DEFAULT_LEVEL_PLAN)
    assert plan.to_wire_dict()["corridorPlacement"] == "none"

    prefs = normalize_generation_preferences(
        {
            "minWaterAreas": 0,
            "maxWaterAreas": 0,
            "corridorPlacement": "center",
            "corridorWidth": 1,
        }
    )
    assert prefs["corridorRole"] == "player_route"

    ha_option = {
        "title": "Remove internal walls",
        "description": "Clear internal obstacle blocks and expand steps.",
        "promptText": json.dumps(
            {
                "changes": {
                    "minWallObstacleBlocks": 0,
                    "maxWallObstacleBlocks": 0,
                    "minSolutionSteps": 25,
                },
                "preserveUnlisted": True,
            }
        ),
    }
    previous_plan = {
        **DEFAULT_LEVEL_PLAN,
        "corridorPlacement": "center",
        "corridorWidth": 1,
        "corridorPriority": "required",
    }
    applied = apply_selected_ha_plan(
        DEFAULT_LEVEL_PLAN,
        {
            "revisionMode": "ha",
            "previousLevelPlan": previous_plan,
            "selectedHAPlan": json.dumps(ha_option),
            "latestAdjustmentText": "no internal wall",
        },
        feature_constraints=resolve_zero_feature_constraints(
            {"latestAdjustmentText": "no internal wall"}
        ),
    )
    assert applied["minWallObstacleBlocks"] == 0
    assert applied["maxWallObstacleBlocks"] == 0
    assert applied["corridorPlacement"] == "none"
    assert applied["corridorWidth"] == 0
    assert applied["minSolutionSteps"] == 25
    assert applied["minWaterAreas"] == DEFAULT_LEVEL_PLAN["minWaterAreas"]
    assert applied["designNote"].startswith("Human-AI revision:")


def test_dg_context_validation_and_fallback() -> None:
    ctx = DGContext(
        firstMovePreference="quick_start",
        pushPlanningPreference="easy_to_adjust",
        spacePreference="focused_area",
        routeRhythmPreference="short_routes",
        language="en",
    )
    fallback = build_dg_fallback_summary(ctx, reason="timeout")
    assert fallback["recommendedDifficulty"] == "Easy"
    assert fallback["recommendedLayout"] == "Compact"
    assert fallback["source"] == "deterministic_fallback"

    summary_text = (
        "Got it! Let's try a quick start with simple pushes. "
        "How about one focused area?"
    )
    valid_llm = validate_dg_guide_payload(
        {
            "summary": summary_text,
            "recommendedDifficulty": "Easy",
            "difficultyRationale": "I connect quick_start and easy_to_adjust to Easy planning.",
            "recommendedLayout": "Compact",
            "layoutRationale": "I connect focused_area and short_routes to a Compact layout.",
        },
        ctx,
    )
    assert valid_llm["source"] == "llm"

    with pytest.raises(ValueError, match="neutral answer rules"):
        validate_dg_guide_payload(
            {
                "summary": summary_text,
                "recommendedDifficulty": "Hard",
                "difficultyRationale": "I connect quick_start and easy_to_adjust to Hard planning.",
                "recommendedLayout": "Compact",
                "layoutRationale": "I connect focused_area and short_routes to a Compact layout.",
            },
            ctx,
        )


# ---------------------------------------------------------------------------
# Memory, Sessions, PublicTurnGuidance & StageChangeSummary
# ---------------------------------------------------------------------------


def test_design_context_single_pass_validation_and_transitions() -> None:
    inferred_goals = [
        {"goal": f"Inferred goal number {i}", "authority": "inferred"}
        for i in range(MAX_ACTIVE_INFERRED + 4)
    ]
    state = DesignContextState.model_validate(
        {
            "userGoals": [
                {"goal": "Keep B1 near T1", "authority": "explicit"},
                *inferred_goals,
            ],
            "openQuestions": [{"question": "Should the route split?", "status": "resolved"}],
        }
    )
    assert state.open_questions[0].status == "answered"
    active_inferred = [
        g for g in state.user_goals if g.authority == "inferred" and g.status == "active"
    ]
    assert len(active_inferred) == MAX_ACTIVE_INFERRED
    assert any(g.authority == "explicit" for g in state.user_goals)

    assert "B1" not in sanitize_user_design_text("B1 is at (2, 3), please widen the corridor")
    assert "if B1 is at (2, 3)" in sanitize_user_design_text("if B1 is at (2, 3), keep space open")

    state2, hid = merge_intent_hypothesis(
        state,
        "Preserve a split route",
        topic_key="route_rhythm",
        stage_id="v1",
        turn_id="t1",
        displayed=True,
    )
    assert hid is not None
    state3, resolved_id = resolve_intent_hypothesis(
        state2,
        hid,
        "confirm",
        stage_id="v1",
        turn_id="t2",
    )
    assert resolved_id == hid
    confirmed = next(h for h in state3.intent_hypotheses if h.id == hid)
    assert confirmed.status == "confirmed"
    assert confirmed.confidence == 1.0


def test_public_turn_guidance_and_stage_change_summary() -> None:
    raw_guidance = {
        "move": "offer_revision",
        "_intentHypothesisId": "hyp-1",
        "_intentReviewState": {"interactionMode": "conflict_choice"},
        "proposalDiscovery": {"topicId": "topic-9"},
        "designContextPatch": {"goals": []},
        "relaxationOffer": {"status": "stale"},
        "proposalOffer": {
            "summary": "Widen gap",
            "executionBrief": {"secret": True},
            "revisionPlan": {"strategies": []},
            "_internal": 1,
        },
    }
    projected = to_public_guidance(raw_guidance, include_dashboard_node=True)
    assert "designContextPatch" not in projected
    assert "proposalDiscovery" not in projected
    assert "relaxationOffer" not in projected
    assert projected["proposalOffer"] == {"summary": "Widen gap"}
    assert projected["dashboardNode"] == {
        "intentHypothesisId": "hyp-1",
        "intentConflict": True,
        "proposalTopicId": "topic-9",
    }

    stored = to_public_guidance(raw_guidance, include_dashboard_node=False)
    assert "dashboardNode" not in stored

    after_map = list(BASE_MAP)
    after_map[3] = "#..........#"  # remove internal wall at (row=4, col=6)
    after_map[4] = "#..........#"  # remove water at (row=5, col=4)
    summary = summarize_stage_changes(BASE_MAP, after_map)
    assert summary.changed_cell_count == 2
    assert summary.components == ["water", "internalWalls"]
    assert summary.component_cell_counts == {"water": 1, "internalWalls": 1}


# ---------------------------------------------------------------------------
# Proposals, Requirements, Beam Search & Binary Discovery
# ---------------------------------------------------------------------------


def test_requirements_validation_evaluation_and_tradeoff() -> None:
    user_turns = [{"id": "u1", "content": "Reduce water by 1 and keep wall count at 1."}]
    snapshot = {"entities": [{"id": "P"}, {"id": "B1"}, {"id": "T1"}]}
    record = validate_requirement_record(
        {
            "requirements": [
                {
                    "component": "water",
                    "property": "count",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "value": 1,
                    "unit": "delta",
                    "priorityEvidenceSpan": "",
                    "focused": True,
                    "sourceTurnId": "u1",
                    "evidenceSpan": "Reduce water by 1",
                    "statement": "Reduce water by 1",
                }
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        },
        user_turns,
        snapshot,
    )
    after_map = list(BASE_MAP)
    after_map[4] = "#..........#"
    outcomes = evaluate_requirements(BASE_MAP, after_map, record)
    assert outcomes[0]["status"] == "fulfilled"
    assert outcomes[0]["passed"] is True
    assert preflight_requirements(BASE_MAP, record) == []

    notice_zh = build_tradeoff_notice(record, outcomes, "zh-CN")
    notice_en = build_tradeoff_notice(record, outcomes, "en")
    assert "这版已核验的结果是" in notice_zh
    assert "Verified in this candidate" in notice_en


def test_proposals_search_operators_outer_shell_and_exact_short_circuit() -> None:
    assert EDITABLE_OPERATORS == frozenset(
        {"add_wall", "remove_wall", "add_water", "remove_water"}
    )

    with pytest.raises(RevisionPlanError):
        parse_revision_plan(
            {
                "strategies": [
                    {
                        "effect": "relocate_box",
                        "focus": None,
                        "operators": ["move_box"],
                        "preserve": [],
                        "editBudget": 2,
                        "metricGoals": [],
                    }
                ]
            }
        )

    irregular_map = [
        "  ##########",
        "###........#",
        "#..p..s.t..#",
        "#....#.....#",
        "#..@.......#",
        "#..........#",
        "#.....s.t..#",
        "#..........#",
        "#..........#",
        "############",
    ]
    shell = connected_outer_shell(irregular_map)
    assert (0, 1) in shell
    assert (2, 0) in shell
    assert (5, 3) not in shell  # internal wall at row 4, col 6

    exact_plan = parse_revision_plan(
        {
            "strategies": [
                {
                    "effect": "open_route",
                    "focus": None,
                    "operators": ["remove_wall"],
                    "preserve": ["water"],
                    "editBudget": 3,
                    "metricGoals": [],
                    "requiredTransitions": [
                        {"row": 4, "column": 6, "from": "#", "to": "."}
                    ],
                }
            ]
        }
    )
    calls = 0

    def _validator(rows: list[str]) -> _StubValidation:
        nonlocal calls
        calls += 1
        return _StubValidation(rows)

    result = search_revision_plan(irregular_map, exact_plan, _validator)
    assert calls == 1
    assert result.diagnostics["constructedCandidates"] == 1
    assert result.rows[3][5] == "."

    # When exact transition is invalidated, search must not expand beyond the exact state.
    with pytest.raises(ProposalSearchExhausted) as exc_info:
        search_revision_plan(
            irregular_map,
            exact_plan,
            lambda _rows: (_ for _ in ()).throw(ValueError("unsolvable")),
        )
    assert exc_info.value.diagnostics["constructedCandidates"] == 1


def test_binary_step_promises_and_protected_refusal() -> None:
    step = parse_binary_step(
        {
            "action": "ask",
            "question": "Which obstacle should we clear?",
            "note": "",
            "branches": [
                {
                    "label": "Remove wall",
                    "strategy": {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_wall"],
                        "preserve": ["water"],
                        "editBudget": 2,
                        "metricGoals": [],
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "internal_walls",
                        "focus": None,
                        "corridor": None,
                    },
                },
                {
                    "label": "Remove water",
                    "strategy": {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_water"],
                        "preserve": ["walls"],
                        "editBudget": 2,
                        "metricGoals": [],
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "water",
                        "focus": None,
                        "corridor": None,
                    },
                },
            ],
            "initialRequirements": None,
        },
        question_count=0,
    )
    after_map = list(BASE_MAP)
    after_map[3] = "#..........#"
    outcomes = check_promises(BASE_MAP, after_map, [step["branches"][0]["promise"]])
    assert outcomes[0]["passed"] is True

    with pytest.raises(BinaryError):
        check_promises(BASE_MAP, BASE_MAP, [step["branches"][0]["promise"]])

    refusal_zh = render_protected_refusal(["boxes", "targets"], "move box", "zh-CN")
    refusal_en = render_protected_refusal(["boxes", "targets"], "move box", "en")
    assert "箱子" in refusal_zh and "目标点" in refusal_zh
    assert "boxes" in refusal_en and "targets" in refusal_en


# ---------------------------------------------------------------------------
# Unified Bilingual i18n Catalog
# ---------------------------------------------------------------------------


def test_i18n_catalog_en_zh_parity_and_formatters() -> None:
    formatter = string.Formatter()
    for key, translations in MESSAGES.items():
        assert set(translations) == {"en", "zh-CN"}, f"Locale mismatch for {key}"
        en_text = translations["en"].strip()
        zh_text = translations["zh-CN"].strip()
        assert en_text, f"Empty en text for {key}"
        assert zh_text, f"Empty zh-CN text for {key}"
        en_fields = {fname for _, fname, _, _ in formatter.parse(en_text) if fname is not None}
        zh_fields = {fname for _, fname, _, _ in formatter.parse(zh_text) if fname is not None}
        assert en_fields == zh_fields, f"Placeholder mismatch for {key}: {en_fields} vs {zh_fields}"

    assert get_message("error.version_conflict", "en") != get_message(
        "error.version_conflict", "zh-CN"
    )
    assert get_tile_label("@", "zh-CN", with_symbol=True) == "水面（@）"
    assert "row 2, column 3" in format_verified_diff_summary(
        [{"x": 2, "y": 1, "before": ".", "after": "#"}],
        "en",
    )
    assert "B1" in format_map_claim_correction(
        [{"kind": "entity", "entity": "B1", "expected": {"row": 3, "column": 7}}],
        "zh-CN",
    )
