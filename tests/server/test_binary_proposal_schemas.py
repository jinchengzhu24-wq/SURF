"""Unit tests for refined binary discovery, proposal, plan, requirement, and map-op schemas."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from pydantic import ValidationError
from server.core.clock import FakeClock
from server.core.grid import parse_grid
from server.core.llm_client import LLMValidationError, TransportResponse
from server.domain.proposals import (
    BinaryError,
    check_promises,
    parse_binary_step,
    parse_revision_plan,
    validate_requirement_record,
)
from server.llm._schemas_proposals import (
    AutomaticBindingPayload,
    BinaryBranchPayload,
    BinaryCorridorPayload,
    BinaryDiscoveryFollowupFinalPayload,
    BinaryDiscoveryFollowupPayload,
    BinaryDiscoveryInitialFinalPayload,
    BinaryDiscoveryInitialPayload,
    BinaryDiscoveryPayload,
    BinaryStrategyPayload,
    ExactTransitionPayload,
    MapOperationCandidatePayload,
    MapOperationItemPayload,
    ProposalFailureAnalysisPayload,
    RequiredTransitionPayload,
    RequirementItemPayload,
    RequirementRecordPayload,
    RevisionStrategyPayload,
    select_binary_discovery_schema,
)
from server.llm.pipelines.binary import BinaryDiscoveryPipeline
from server.llm.pipelines.proposal import ProposalPipeline

SAMPLE_ROWS: tuple[str, ...] = (
    "############",
    "#..........#",
    "#.p.s..t...#",
    "#....#.....#",
    "#....@.....#",
    "#..........#",
    "#..s...t...#",
    "#..........#",
    "#..........#",
    "############",
)


class FakeAsyncTransport:
    """Deterministic fake LLMTransport recording messages and response_format."""

    def __init__(self, responses: Sequence[dict[str, Any] | str | Exception]) -> None:
        self._queue: list[dict[str, Any] | str | Exception] = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> TransportResponse:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "response_format": response_format,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "extra_body": extra_body,
            }
        )
        assert self._queue, "FakeAsyncTransport received more calls than queued responses."
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        content = nxt if isinstance(nxt, str) else json.dumps(nxt, ensure_ascii=False)
        return TransportResponse(
            content=content,
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )


def _valid_initial_req(
    *,
    source_turn_id: str = "t1",
    evidence_span: str = "open up the center",
    component: str = "gameplay",
    prop: str = "experience",
    relation: str = "seek",
    strength: str = "goal",
    entities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "requirements": [
            {
                "component": component,
                "property": prop,
                "relation": relation,
                "strength": strength,
                "entities": list(entities or []),
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": source_turn_id,
                "evidenceSpan": evidence_span,
                "statement": "Open up the center",
                "scope": "all",
            }
        ],
        "automaticBindings": [],
        "exactTransitions": [],
    }


def _wall_branch(label: str = "Clear center wall") -> dict[str, Any]:
    return {
        "label": label,
        "strategy": {
            "effect": "open_route",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "operators": ["remove_wall"],
            "preserve": ["water"],
            "editBudget": 2,
        },
        "promise": {
            "kind": "reduce_obstacles",
            "component": "internal_walls",
            "focus": {"row": 4, "column": 6, "radius": 1},
        },
    }


def _water_branch(label: str = "Clear center water") -> dict[str, Any]:
    return {
        "label": label,
        "strategy": {
            "effect": "open_route",
            "focus": {"row": 5, "column": 6, "radius": 1},
            "operators": ["remove_water"],
            "preserve": ["walls"],
            "editBudget": 2,
        },
        "promise": {
            "kind": "reduce_obstacles",
            "component": "water",
            "focus": {"row": 5, "column": 6, "radius": 1},
        },
    }


# ---------------------------------------------------------------------------
# 1. Context-scoped BinaryDiscovery* schemas & JSON schema generation
# ---------------------------------------------------------------------------


def test_binary_discovery_context_scoped_json_schemas() -> None:
    assert BinaryDiscoveryPayload is BinaryDiscoveryInitialPayload
    assert (
        select_binary_discovery_schema(needs_initial_requirements=True, can_ask=True)
        is BinaryDiscoveryInitialPayload
    )
    assert (
        select_binary_discovery_schema(needs_initial_requirements=False, can_ask=True)
        is BinaryDiscoveryFollowupPayload
    )
    assert (
        select_binary_discovery_schema(needs_initial_requirements=True, can_ask=False)
        is BinaryDiscoveryInitialFinalPayload
    )
    assert (
        select_binary_discovery_schema(needs_initial_requirements=False, can_ask=False)
        is BinaryDiscoveryFollowupFinalPayload
    )

    initial_schema = BinaryDiscoveryInitialPayload.model_json_schema()
    followup_schema = BinaryDiscoveryFollowupPayload.model_json_schema()
    initial_final_schema = BinaryDiscoveryInitialFinalPayload.model_json_schema()
    followup_final_schema = BinaryDiscoveryFollowupFinalPayload.model_json_schema()

    for schema in (initial_schema, followup_schema, initial_final_schema, followup_final_schema):
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False
        serialized = json.dumps(schema)
        assert '"discriminator"' not in serialized
        assert '"oneOf"' not in serialized
        strategy_def = schema["$defs"]["BinaryStrategyPayload"]
        strat_props = set(strategy_def["properties"])
        assert strat_props == {"effect", "focus", "operators", "preserve", "editBudget"}
        assert "metricGoals" not in strat_props
        assert "requiredTransitions" not in strat_props
        assert "anchorEntities" not in strat_props
        assert "playObjective" not in strat_props
        assert "displayReason" not in strat_props
        assert strategy_def["properties"]["preserve"]["items"]["enum"] == ["water", "walls"]

    # BinaryBranchPayload.promise uses OpenAI-strict-compatible anyOf (no discriminator/oneOf)
    branch_promise_schema = initial_schema["$defs"]["BinaryBranchPayload"]["properties"]["promise"]
    assert "anyOf" in branch_promise_schema
    assert "oneOf" not in branch_promise_schema
    assert "discriminator" not in branch_promise_schema

    # Initial vs Followup: initialRequirements omitted on followup
    assert "initialRequirements" in initial_schema["properties"]
    assert "initialRequirements" not in followup_schema["properties"]
    assert "RequirementRecordPayload" not in followup_schema["$defs"]
    assert "initialRequirements" in initial_final_schema["properties"]
    assert "initialRequirements" not in followup_final_schema["properties"]

    # can_ask=True vs can_ask=False: ask/question/branches omitted on final schemas
    assert "ask" in initial_schema["properties"]["action"]["enum"]
    assert "question" in initial_schema["properties"]
    assert "branches" in initial_schema["properties"]
    for final_schema in (initial_final_schema, followup_final_schema):
        assert "ask" not in final_schema["properties"]["action"]["enum"]
        assert "question" not in final_schema["properties"]
        assert "branches" not in final_schema["properties"]


def test_binary_discovery_model_validators() -> None:
    # ask missing initialRequirements on initial turn
    with pytest.raises(ValidationError, match="initialRequirements"):
        BinaryDiscoveryInitialPayload.model_validate(
            {
                "action": "ask",
                "question": "Which obstacle first?",
                "note": "",
                "branches": [_wall_branch(), _water_branch()],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": None,
            }
        )

    # ask with duplicate normalized promises
    dup_branch_1 = _wall_branch("Remove wall A")
    dup_branch_2 = _wall_branch("Remove wall B")
    with pytest.raises(ValidationError, match="distinct branch promises"):
        BinaryDiscoveryFollowupPayload.model_validate(
            {
                "action": "ask",
                "question": "Which wall?",
                "note": "",
                "branches": [dup_branch_1, dup_branch_2],
                "planStrategies": [],
                "protectedEntities": [],
            }
        )

    # ask with unused planStrategies
    with pytest.raises(ValidationError, match="planStrategies"):
        BinaryDiscoveryFollowupPayload.model_validate(
            {
                "action": "ask",
                "question": "Which obstacle?",
                "note": "",
                "branches": [_wall_branch(), _water_branch()],
                "planStrategies": [_wall_branch()["strategy"]],
                "protectedEntities": [],
            }
        )

    # plan on initial turn allows planStrategies=[] and clears stray non-empty note
    initial_plan = BinaryDiscoveryInitialFinalPayload.model_validate(
        {
            "action": "plan",
            "note": "Ready to proceed to proposal generation.",
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": _valid_initial_req(),
        }
    )
    assert initial_plan.action == "plan"
    assert initial_plan.plan_strategies == []
    assert initial_plan.note == ""

    # plan on initial turn still requires non-null initialRequirements
    with pytest.raises(ValidationError, match="initialRequirements"):
        BinaryDiscoveryInitialFinalPayload.model_validate(
            {
                "action": "plan",
                "note": "",
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": None,
            }
        )

    # plan on followup allows 0 planStrategies
    followup_plan = BinaryDiscoveryFollowupFinalPayload.model_validate(
        {
            "action": "plan",
            "note": "Proceeding now.",
            "planStrategies": [],
            "protectedEntities": [],
        }
    )
    assert followup_plan.action == "plan"
    assert followup_plan.note == ""

    # protected_refusal clears stray non-empty note & initialRequirements, requires unique
    refusal_with_note = BinaryDiscoveryInitialPayload.model_validate(
        {
            "action": "protected_refusal",
            "question": "",
            "note": "Boxes are fixed by platform rules.",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": ["boxes"],
            "initialRequirements": _valid_initial_req(),
        }
    )
    assert refusal_with_note.action == "protected_refusal"
    assert refusal_with_note.note == ""
    assert refusal_with_note.initial_requirements is None

    with pytest.raises(ValidationError, match="duplicates"):
        BinaryDiscoveryInitialPayload.model_validate(
            {
                "action": "protected_refusal",
                "question": "",
                "note": "",
                "branches": [],
                "planStrategies": [],
                "protectedEntities": ["boxes", "boxes"],
                "initialRequirements": None,
            }
        )

    # blocked requires non-blank note and clears stray initialRequirements on initial turn
    blocked_ok = BinaryDiscoveryInitialPayload.model_validate(
        {
            "action": "blocked",
            "question": "",
            "note": "No safe editable corridor exists.",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": _valid_initial_req(),
        }
    )
    assert blocked_ok.action == "blocked"
    assert blocked_ok.initial_requirements is None
    with pytest.raises(ValidationError, match="non-blank 'note'"):
        BinaryDiscoveryFollowupFinalPayload.model_validate(
            {
                "action": "blocked",
                "note": "   ",
                "planStrategies": [],
                "protectedEntities": [],
            }
        )


def test_binary_strategy_and_tagged_promise_payloads() -> None:
    # BinaryStrategyPayload rejects preserve conflicting with operators
    with pytest.raises(ValidationError, match="preserves every component"):
        BinaryStrategyPayload.model_validate(
            {
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": ["walls"],
                "editBudget": 2,
            }
        )

    # BinaryStrategyPayload rejects operators incompatible with effect
    with pytest.raises(ValidationError, match="cannot realize effect"):
        BinaryStrategyPayload.model_validate(
            {
                "effect": "reshape_water",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": [],
                "editBudget": 2,
            }
        )

    # BinaryStrategyPayload forbids metricGoals, but silently strips platform-fixed preserve keys
    with pytest.raises(ValidationError):
        BinaryStrategyPayload.model_validate(
            {
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": [],
                "editBudget": 2,
                "metricGoals": [],
            }
        )
    stripped_strat = BinaryStrategyPayload.model_validate(
        {
            "effect": "open_route",
            "focus": None,
            "operators": ["remove_wall"],
            "preserve": [
                "outer_shell",
                "player",
                "boxes",
                "targets",
                "water",
                "unrelated_areas",
            ],
            "editBudget": 2,
        }
    )
    assert stripped_strat.preserve == ["water"]

    # Unknown preserve values outside platform-fixed & water/walls are still rejected
    with pytest.raises(ValidationError):
        BinaryStrategyPayload.model_validate(
            {
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": ["lava"],
                "editBudget": 2,
            }
        )

    # Tagged BinaryPromisePayload: widen_corridor requires interior corridor
    branch = BinaryBranchPayload.model_validate(
        {
            "label": "Widen middle corridor",
            "strategy": _wall_branch()["strategy"],
            "promise": {
                "kind": "widen_corridor",
                "component": "internal_walls",
                "corridor": {"row": 4, "column": 6, "axis": "horizontal"},
            },
        }
    )
    assert branch.promise.kind == "widen_corridor"
    assert isinstance(branch.promise.corridor, BinaryCorridorPayload)

    # Perimeter corridor rejected (row=1)
    with pytest.raises(ValidationError):
        BinaryCorridorPayload.model_validate({"row": 1, "column": 6, "axis": "horizontal"})

    # widen_corridor missing corridor rejected
    with pytest.raises(ValidationError):
        BinaryBranchPayload.model_validate(
            {
                "label": "Widen middle corridor",
                "strategy": _wall_branch()["strategy"],
                "promise": {
                    "kind": "widen_corridor",
                    "component": "internal_walls",
                },
            }
        )

    # reshape_water forbids component != water and forbids corridor
    with pytest.raises(ValidationError):
        BinaryBranchPayload.model_validate(
            {
                "label": "Reshape walls",
                "strategy": _wall_branch()["strategy"],
                "promise": {
                    "kind": "reshape_water",
                    "component": "internal_walls",
                },
            }
        )

    # shorter_solution forbids focus and requires component == obstacles
    with pytest.raises(ValidationError):
        BinaryBranchPayload.model_validate(
            {
                "label": "Shorter path",
                "strategy": _wall_branch()["strategy"],
                "promise": {
                    "kind": "shorter_solution",
                    "component": "obstacles",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                },
            }
        )


# ---------------------------------------------------------------------------
# 2. Domain BinaryError preservation & unwrapping in parse_binary_step
# ---------------------------------------------------------------------------


def test_domain_binary_error_detail_and_unwrapping() -> None:
    err = BinaryError("BINARY_UNUSED_FIELDS", "envelope", detail="question must be empty")
    assert str(err) == "question must be empty"
    assert err.code == "BINARY_UNUSED_FIELDS"
    assert err.field == "envelope"

    base_step: dict[str, Any] = {
        "action": "ask",
        "question": "Which obstacle first?",
        "note": "",
        "branches": [_wall_branch(), _water_branch()],
        "planStrategies": [],
        "protectedEntities": [],
        "initialRequirements": None,
    }

    # metricGoals triggers BINARY_METRIC_GOALS_FORBIDDEN (not BINARY_PROTECTED_STRATEGY!)
    bad_goals = json.loads(json.dumps(base_step))
    bad_goals["branches"][0]["strategy"]["metricGoals"] = [
        {"metric": "solutionSteps", "direction": "decrease", "minimumDelta": 1}
    ]
    with pytest.raises(BinaryError) as exc_goals:
        parse_binary_step(bad_goals, question_count=0)
    assert exc_goals.value.code == "BINARY_METRIC_GOALS_FORBIDDEN"
    assert exc_goals.value.field == "branch-1.strategy.metricGoals"
    assert "branch.promise" in str(exc_goals.value)

    # Non-editable operator triggers BINARY_PROTECTED_STRATEGY
    bad_op = json.loads(json.dumps(base_step))
    bad_op["branches"][0]["strategy"]["operators"] = ["move_box"]
    with pytest.raises(BinaryError) as exc_op:
        parse_binary_step(bad_op, question_count=0)
    assert exc_op.value.code == "BINARY_PROTECTED_STRATEGY"
    assert "move_box" in str(exc_op.value)

    # Pydantic ValidationError wrapping BinaryError unwraps BINARY_CORRIDOR_INVALID
    bad_corridor = json.loads(json.dumps(base_step))
    bad_corridor["branches"][0]["promise"] = {
        "kind": "widen_corridor",
        "component": "internal_walls",
        "focus": None,
        "corridor": None,
    }
    with pytest.raises(BinaryError) as exc_corr:
        parse_binary_step(bad_corridor, question_count=0)
    assert exc_corr.value.code == "BINARY_CORRIDOR_INVALID"
    assert "widen_corridor" in str(exc_corr.value)

    # Unwraps BINARY_CORRIDOR_UNEXPECTED
    unexpected_corridor = json.loads(json.dumps(base_step))
    unexpected_corridor["branches"][0]["promise"] = {
        "kind": "reduce_obstacles",
        "component": "internal_walls",
        "focus": None,
        "corridor": {"row": 4, "column": 6, "axis": "horizontal"},
    }
    with pytest.raises(BinaryError) as exc_unexp:
        parse_binary_step(unexpected_corridor, question_count=0)
    assert exc_unexp.value.code == "BINARY_CORRIDOR_UNEXPECTED"

    # check_promises failure detail includes kind and before/after
    with pytest.raises(BinaryError) as exc_prom:
        check_promises(
            SAMPLE_ROWS,
            SAMPLE_ROWS,
            [
                {
                    "kind": "reduce_obstacles",
                    "component": "internal_walls",
                    "focus": None,
                    "corridor": None,
                }
            ],
        )
    assert exc_prom.value.code == "BINARY_PROMISE_NOT_REALIZED"
    assert "reduce_obstacles" in str(exc_prom.value)
    assert "before=" in str(exc_prom.value)


# ---------------------------------------------------------------------------
# 3. RevisionStrategy, Requirement, ExactTransition, MapOperation, Failure schemas
# ---------------------------------------------------------------------------


def test_revision_strategy_and_plan_validators() -> None:
    # RequiredTransitionPayload rejects perimeter coords and from == to
    with pytest.raises(ValidationError):
        RequiredTransitionPayload.model_validate({"row": 1, "column": 5, "from": "#", "to": "."})
    with pytest.raises(ValidationError, match="must differ"):
        RequiredTransitionPayload.model_validate({"row": 4, "column": 6, "from": "#", "to": "#"})

    # RevisionStrategyPayload normalizes blank playObjective to None, rejects newlines
    strat = RevisionStrategyPayload.model_validate(
        {
            "effect": "open_route",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "operators": ["remove_wall"],
            "preserve": ["outer_shell", "water"],
            "editBudget": 2,
            "metricGoals": [],
            "requiredTransitions": [{"row": 4, "column": 6, "from": "#", "to": "."}],
            "anchorEntities": ["B1"],
            "playObjective": "   ",
        }
    )
    assert strat.play_objective is None

    with pytest.raises(ValidationError, match="newline"):
        RevisionStrategyPayload.model_validate(
            {
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": [],
                "editBudget": 1,
                "playObjective": "Line 1\nLine 2",
            }
        )

    # Required transition outside focus rejected
    with pytest.raises(ValidationError, match="outside focus"):
        RevisionStrategyPayload.model_validate(
            {
                "effect": "open_route",
                "focus": {"row": 2, "column": 2, "radius": 1},
                "operators": ["remove_wall"],
                "preserve": [],
                "editBudget": 2,
                "requiredTransitions": [{"row": 6, "column": 8, "from": "#", "to": "."}],
            }
        )

    # Required transition count > editBudget rejected
    with pytest.raises(ValidationError, match="exceeding editBudget"):
        RevisionStrategyPayload.model_validate(
            {
                "effect": "open_route",
                "focus": None,
                "operators": ["remove_wall"],
                "preserve": [],
                "editBudget": 1,
                "requiredTransitions": [
                    {"row": 4, "column": 6, "from": "#", "to": "."},
                    {"row": 5, "column": 6, "from": "#", "to": "."},
                ],
            }
        )

    # Domain plan_parser gives actionable detail
    with pytest.raises(ValueError, match="include at least one of"):
        parse_revision_plan(
            {
                "strategies": [
                    {
                        "effect": "reshape_water",
                        "focus": None,
                        "operators": ["remove_wall"],
                        "preserve": [],
                        "editBudget": 1,
                        "metricGoals": [],
                    }
                ]
            }
        )


def test_requirement_and_exact_transition_validators() -> None:
    # gameplay + map property rejected
    with pytest.raises(ValidationError, match="component='gameplay'"):
        RequirementItemPayload.model_validate(
            {
                "component": "gameplay",
                "property": "count",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "sourceTurnId": "t1",
                "evidenceSpan": "fewer things",
                "statement": "Fewer things",
            }
        )

    # invariant + directional goal rejected
    with pytest.raises(ValidationError, match="strength='invariant'"):
        RequirementItemPayload.model_validate(
            {
                "component": "wall",
                "property": "count",
                "relation": "decrease",
                "strength": "invariant",
                "entities": [],
                "value": None,
                "unit": "none",
                "sourceTurnId": "t1",
                "evidenceSpan": "remove walls",
                "statement": "Remove walls",
            }
        )

    # value/unit mismatch and delta=0 rejected
    with pytest.raises(ValidationError, match="unit cannot be 'none'"):
        RequirementItemPayload.model_validate(
            {
                "component": "wall",
                "property": "count",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": 2,
                "unit": "none",
                "sourceTurnId": "t1",
                "evidenceSpan": "remove 2 walls",
                "statement": "Remove 2 walls",
            }
        )
    with pytest.raises(ValidationError, match="value >= 1"):
        RequirementItemPayload.model_validate(
            {
                "component": "wall",
                "property": "count",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": 0,
                "unit": "delta",
                "sourceTurnId": "t1",
                "evidenceSpan": "remove 0 walls",
                "statement": "Remove 0 walls",
            }
        )

    # scope='original_cells' outside (water|wall, count) downgrades cleanly to 'all'
    downgraded_scope = RequirementItemPayload.model_validate(
        {
            "component": "gameplay",
            "property": "solutionSteps",
            "relation": "decrease",
            "strength": "goal",
            "entities": [],
            "value": None,
            "unit": "none",
            "sourceTurnId": "t1",
            "evidenceSpan": "shorter",
            "statement": "Shorter solution",
            "scope": "original_cells",
        }
    )
    assert downgraded_scope.scope == "all"

    # RequirementItemPayload.entities uppercases, dedups, and strips generic labels while keeping
    # JSON Schema strictly narrowed to EntityId
    ent_schema = RequirementItemPayload.model_json_schema()
    assert ent_schema["properties"]["entities"]["items"]["enum"] == ["P", "B1", "B2", "T1", "T2"]
    norm_item = RequirementItemPayload.model_validate(
        {
            "component": "box",
            "property": "distance",
            "relation": "nearer",
            "strength": "goal",
            "entities": ["WATER", "box", "TARGETS", "b1", "B1", "gameplay"],
            "value": None,
            "unit": "none",
            "sourceTurnId": "t1",
            "evidenceSpan": "move box closer",
            "statement": "Move B1 closer",
        }
    )
    assert norm_item.entities == ["B1"]

    with pytest.raises(ValidationError):
        RequirementItemPayload.model_validate(
            {
                "component": "box",
                "property": "distance",
                "relation": "nearer",
                "strength": "goal",
                "entities": ["B9"],
                "value": None,
                "unit": "none",
                "sourceTurnId": "t1",
                "evidenceSpan": "move B9 closer",
                "statement": "Move B9 closer",
            }
        )

    # Unknown entity rejected by Literal on standalone AutomaticBindingPayload
    with pytest.raises(ValidationError):
        AutomaticBindingPayload.model_validate({"entity": "BOX", "reason": "anchor"})

    # ExactTransitionPayload rejects perimeter & from == to
    with pytest.raises(ValidationError):
        ExactTransitionPayload.model_validate(
            {
                "row": 10,
                "column": 5,
                "from": "#",
                "to": ".",
                "sourceTurnId": "t1",
                "evidenceSpan": "row 10 col 5",
            }
        )
    with pytest.raises(ValidationError, match="must differ"):
        ExactTransitionPayload.model_validate(
            {
                "row": 4,
                "column": 6,
                "from": "#",
                "to": "#",
                "sourceTurnId": "t1",
                "evidenceSpan": "row 4 col 6",
            }
        )

    # RequirementRecordPayload strips generic automaticBindings, uppercases valid ones when an
    # unbound requirement exists, and clears them when all requirements specify entities
    kept_bindings = RequirementRecordPayload.model_validate(
        {
            "requirements": [
                {
                    "component": "box",
                    "property": "distance",
                    "relation": "nearer",
                    "strength": "goal",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "sourceTurnId": "t1",
                    "evidenceSpan": "move box closer",
                    "statement": "Move box closer",
                }
            ],
            "automaticBindings": [
                {"entity": "WATER", "reason": "generic label"},
                {"entity": "box", "reason": "generic label"},
                {"entity": "b1", "reason": "closest box"},
            ],
            "exactTransitions": [],
        }
    )
    assert [(b.entity, b.reason) for b in kept_bindings.automatic_bindings] == [
        ("B1", "closest box")
    ]

    cleared_bindings = RequirementRecordPayload.model_validate(
        {
            "requirements": [
                {
                    "component": "box",
                    "property": "distance",
                    "relation": "nearer",
                    "strength": "goal",
                    "entities": ["B1"],
                    "value": None,
                    "unit": "none",
                    "sourceTurnId": "t1",
                    "evidenceSpan": "B1 nearer",
                    "statement": "B1 nearer",
                }
            ],
            "automaticBindings": [{"entity": "b1", "reason": "redundant"}],
            "exactTransitions": [],
        }
    )
    assert cleared_bindings.automatic_bindings == []

    # Domain validate_requirement_record distinguishes from == to from non-editable tiles
    with pytest.raises(ValueError, match="identical 'from' and 'to'"):
        validate_requirement_record(
            {
                "requirements": [],
                "automaticBindings": [],
                "exactTransitions": [
                    {
                        "row": 4,
                        "column": 6,
                        "from": "#",
                        "to": "#",
                        "sourceTurnId": "t1",
                        "evidenceSpan": "change row 4 column 6",
                    }
                ],
            },
            user_turns=[{"id": "t1", "content": "change row 4 column 6"}],
            snapshot={"entities": [{"id": "P"}]},
        )


def test_map_operation_and_failure_analysis_validators() -> None:
    # Mismatched (op, from, to) rejected
    with pytest.raises(ValidationError, match="requires from='#' and to='.'"):
        MapOperationItemPayload.model_validate(
            {"row": 4, "column": 6, "op": "remove_wall", "from": ".", "to": "#"}
        )

    # Perimeter coords rejected
    with pytest.raises(ValidationError):
        MapOperationItemPayload.model_validate(
            {"row": 1, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
        )

    # Duplicate coordinate in candidate rejected
    with pytest.raises(ValidationError, match="repeats coordinate"):
        MapOperationCandidatePayload.model_validate(
            {
                "strategyIndex": 1,
                "operations": [
                    {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."},
                    {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."},
                ],
            }
        )

    # Blank ProposalFailureAnalysisPayload rejected, whitespace trimmed
    with pytest.raises(ValidationError, match="non-blank"):
        ProposalFailureAnalysisPayload.model_validate({"body": "   \t  "})
    ok_fail = ProposalFailureAnalysisPayload.model_validate({"body": "  Try clearing water.  "})
    assert ok_fail.body == "Try clearing water."


# ---------------------------------------------------------------------------
# 4. BinaryDiscoveryPipeline context schemas, blocked fix & repair messages
# ---------------------------------------------------------------------------


async def test_binary_pipeline_blocked_turn0_and_followup_schema_selection() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)

    # Turn 0 blocked WITHOUT initialRequirements MUST succeed!
    t_blocked = FakeAsyncTransport(
        [
            {
                "action": "blocked",
                "question": "",
                "note": (
                    "No safe editable change can satisfy the request without breaking solvability."
                ),
                "branches": [],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": None,
            }
        ]
    )
    pipe_blocked = BinaryDiscoveryPipeline(t_blocked, model="kimi-k2.6", clock=clock)
    blocked_res = await pipe_blocked.run_step(
        grid_or_rows=grid,
        initial_request="make it impossible to fail",
        initial_designer_turns=[{"id": "t1", "content": "make it impossible to fail"}],
        language="en",
    )
    assert blocked_res.step["action"] == "blocked"
    assert blocked_res.initial_requirements is None

    # Turn >= 1 with verified_initial_requirements and question_count == 3 selects FollowupFinal
    t_final = FakeAsyncTransport(
        [
            {
                "action": "plan",
                "note": "",
                "planStrategies": [],
                "protectedEntities": [],
            }
        ]
    )
    pipe_final = BinaryDiscoveryPipeline(t_final, model="kimi-k2.6", clock=clock)
    final_res = await pipe_final.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        question_count=3,
        verified_initial_requirements=_valid_initial_req(),
        language="en",
    )
    assert final_res.step["action"] == "plan"
    sent_schema = t_final.calls[0]["response_format"]["json_schema"]["schema"]
    assert "initialRequirements" not in sent_schema["properties"]
    assert "question" not in sent_schema["properties"]
    assert "branches" not in sent_schema["properties"]


async def test_binary_pipeline_repair_messages_for_preflight_and_conflicts() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)

    # Attempt 1: branch 1 tries widen_corridor at an un-widenable cell -> BINARY_PREFLIGHT_NOT_FOUND
    # with failureReasons and promise kind in repair message; Attempt 2 succeeds.
    t_pf = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment do you prefer?",
                "note": "",
                "branches": [
                    {
                        "label": "Reshape water into corner",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 2, "column": 2, "radius": 1},
                            "operators": ["add_water", "remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reshape_water",
                            "component": "water",
                            "focus": {"row": 2, "column": 2, "radius": 1},
                        },
                    },
                    _wall_branch(),
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
            {
                "action": "ask",
                "question": "Which obstacle should we clear first?",
                "note": "",
                "branches": [_wall_branch(), _water_branch()],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
        ]
    )
    pipe_pf = BinaryDiscoveryPipeline(t_pf, model="kimi-k2.6", clock=clock)
    res_pf = await pipe_pf.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert res_pf.attempts_used == 2
    repair_msg = t_pf.calls[1]["messages"][-1]["content"]
    assert "Branch 1" in repair_msg
    assert "reshape_water" in repair_msg
    assert "failureReasons=" in repair_msg

    # Turn-0 initialRequirements preflight conflict (box count increase) rejected cleanly
    t_req_bad = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which obstacle should we clear first?",
                "note": "",
                "branches": [_wall_branch(), _water_branch()],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(
                    evidence_span="add another box and open up the center",
                    component="box",
                    prop="count",
                    relation="increase",
                    strength="goal",
                ),
            }
        ]
    )
    pipe_req_bad = BinaryDiscoveryPipeline(t_req_bad, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError) as exc_req:
        await pipe_req_bad.run_step(
            grid_or_rows=grid,
            initial_request="add another box and open up the center",
            initial_designer_turns=[
                {"id": "t1", "content": "add another box and open up the center"}
            ],
            language="en",
            max_attempts=1,
        )
    assert exc_req.value.code == "BINARY_INITIAL_REQUIREMENTS_INVALID"
    assert "protected entity" in str(exc_req.value)
    assert "{'requirementId'" not in str(exc_req.value)


async def test_binary_pipeline_already_selected_and_field_named_leak_messages() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    prior_choice = {
        "questionId": "q1",
        "choiceId": "c1",
        "label": "Clear center wall",
        "promise": {
            "kind": "reduce_obstacles",
            "component": "internal_walls",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "corridor": None,
        },
        "strategy": {
            **_wall_branch()["strategy"],
            "metricGoals": [],
        },
    }
    t = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which next?",
                "note": "",
                "branches": [_wall_branch("Remove center wall again"), _water_branch()],
                "planStrategies": [],
                "protectedEntities": [],
            }
        ]
    )
    pipe = BinaryDiscoveryPipeline(t, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError) as exc_dup:
        await pipe.run_step(
            grid_or_rows=grid,
            initial_request="open up the center",
            initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
            question_count=1,
            selected_choices=[prior_choice],
            verified_initial_requirements=_valid_initial_req(),
            language="en",
            max_attempts=1,
        )
    assert exc_dup.value.code == "BINARY_ALREADY_SELECTED_DIRECTION"
    assert "Branch 1" in str(exc_dup.value)

    # Field-named token leak on branches[0].label
    t_leak = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which obstacle should we clear first?",
                "note": "",
                "branches": [_wall_branch("Clear stageSnapshot wall"), _water_branch()],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            }
        ]
    )
    pipe_leak = BinaryDiscoveryPipeline(t_leak, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError) as exc_leak:
        await pipe_leak.run_step(
            grid_or_rows=grid,
            initial_request="open up the center",
            initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
            language="en",
            max_attempts=1,
        )
    assert exc_leak.value.code == "INTERNAL_TOKEN_LEAK"
    assert "branches[0].label" in str(exc_leak.value)


# ---------------------------------------------------------------------------
# 5. ProposalPipeline map_operations retry validator & preflight formatting
# ---------------------------------------------------------------------------


async def test_proposal_pipeline_map_operations_retry_validator_recovers_on_attempt_2() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            # Revision plan
            {
                "strategies": [
                    {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_wall"],
                        "preserve": ["water"],
                        "editBudget": 1,
                        "metricGoals": [],
                    }
                ]
            },
            # map_operations attempt 1: wrong tile at (2,2) (floor, not wall) -> triggers repair!
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Wrong coordinate",
                        "operations": [
                            {"row": 2, "column": 2, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
            # map_operations attempt 2: valid center wall at (4,6) -> succeeds!
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Remove actual wall at (4,6)",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
        ]
    )
    pipeline = ProposalPipeline(transport, model="kimi-k2.6", clock=clock)
    res = await pipeline.generate_proposal(
        grid_or_rows=grid,
        authorized_brief="Remove center wall",
        language="en",
    )
    assert res.source == "llm_operations"
    assert len(res.changes) == 1
    assert res.changes[0].row == 4 and res.changes[0].column == 6
    # Verify attempt 2 received actionable repair feedback from _validate_map_operations
    assert len(transport.calls) == 3
    repair_prompt = transport.calls[2]["messages"][-1]["content"]
    assert "SOURCE_TILE_MISMATCH" in repair_prompt
    assert "candidate 1" in repair_prompt


async def test_proposal_pipeline_requirement_preflight_conflict_clean_message() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            {
                "requirements": [
                    {
                        "component": "box",
                        "property": "count",
                        "relation": "increase",
                        "strength": "goal",
                        "entities": [],
                        "value": None,
                        "unit": "none",
                        "priorityEvidenceSpan": "",
                        "focused": False,
                        "sourceTurnId": "t1",
                        "evidenceSpan": "add more boxes",
                        "statement": "Add more boxes",
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            }
        ]
    )
    pipeline = ProposalPipeline(transport, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError) as exc_info:
        await pipeline.compile_requirements(
            designer_turns=[{"id": "t1", "content": "add more boxes"}],
            grid_or_rows=grid,
            max_attempts=1,
        )
    assert exc_info.value.code == "REQUIREMENT_PREFLIGHT_CONFLICT"
    assert "protected entity" in str(exc_info.value)
    assert "{'requirementId'" not in str(exc_info.value)
