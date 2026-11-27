"""Unit tests for refined binary discovery, proposal, plan, requirement, and map-op schemas."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError
from server.application.dialogue.handlers._binary_commit import commit_binary_step_impl
from server.application.sessions import (
    CoCreationAppSettings,
    SessionLifecycleService,
    SessionReadModelService,
)
from server.core.clock import FakeClock
from server.core.grid import parse_grid
from server.core.llm_client import LLMValidationError, TransportResponse
from server.domain.proposals import (
    BinaryError,
    check_promises,
    normalize_requirement_record,
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
from server.llm.grounding import build_stage_facts
from server.llm.pipelines import binary as binary_pipeline_mod
from server.llm.pipelines.binary import BinaryDiscoveryPipeline, BinaryDiscoveryResult
from server.llm.pipelines.proposal import ProposalPipeline
from server.llm.prompts.binary import build_binary_discovery_messages
from server.persistence import (
    DialogueRepository,
    OperationalRepository,
    SessionsRepository,
    SQLiteDatabase,
)

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

    # Attempt 1: branch 1 adds an interior wall far from boxes/targets while promising
    # shorter_solution (passes schema + precondition) -> ProposalSearchExhausted with
    # failureReasons + repair advice; Attempt 2 succeeds.
    t_pf = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment do you prefer?",
                "note": "",
                "branches": [
                    {
                        "label": "Shorten route via corner wall",
                        "strategy": {
                            "effect": "narrow_route",
                            "focus": {"row": 9, "column": 10, "radius": 1},
                            "operators": ["add_wall"],
                            "preserve": ["water"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "shorter_solution",
                            "component": "obstacles",
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
    assert "Try broadening/aligning strategy.focus and promise.focus (or focus=null)" in repair_msg
    assert "action='blocked'" in repair_msg

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


# ---------------------------------------------------------------------------
# 6. Requirement record re-validation idempotency & binary plan commit
# ---------------------------------------------------------------------------


def test_requirement_record_normalize_and_validate_idempotent() -> None:
    grid = parse_grid(SAMPLE_ROWS)
    snapshot = build_stage_facts(grid, None).to_prompt_dict()
    turns = [{"id": "t1", "content": "remove wall at row 4 column 6 to help the box"}]
    raw_payload: dict[str, Any] = {
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
                "focused": True,
                "sourceTurnId": "t1",
                "evidenceSpan": "remove wall at row 4 column 6",
                "statement": "Remove wall at row 4 column 6",
                "scope": "all",
            }
        ],
        "automaticBindings": [
            {"entity": "b1", "reason": "Nearest box to the removed wall"},
        ],
        "exactTransitions": [
            {
                "row": 4,
                "column": 6,
                "from": "#",
                "to": ".",
                "sourceTurnId": "t1",
                "evidenceSpan": "remove wall at row 4 column 6",
            }
        ],
    }

    norm1, _ = normalize_requirement_record(raw_payload, turns, snapshot)
    val1 = validate_requirement_record(norm1, turns, snapshot)
    assert val1["policyVersion"] == 1
    assert val1["requirements"][0]["requirementId"].startswith("req-")
    assert val1["automaticBindings"] == [
        {
            "entity": "B1",
            "reason": "Nearest box to the removed wall",
            "authority": "proposal_assumption",
        }
    ]
    assert len(val1["exactTransitions"]) == 1

    # Re-normalizing and re-validating an ALREADY-validated record must be 100% idempotent
    norm2, _ = normalize_requirement_record(val1, turns, snapshot)
    assert "policyVersion" not in norm2
    assert "requirementId" not in norm2["requirements"][0]
    assert "authority" not in norm2["automaticBindings"][0]
    val2 = validate_requirement_record(norm2, turns, snapshot)
    assert val2 == val1

    # Payload without policyVersion but still carrying validator-injected authority/requirementId
    rebuilt = {
        "requirements": list(val1["requirements"]),
        "automaticBindings": list(val1["automaticBindings"]),
        "exactTransitions": list(val1["exactTransitions"]),
    }
    norm3, _ = normalize_requirement_record(rebuilt, turns, snapshot)
    val3 = validate_requirement_record(norm3, turns, snapshot)
    assert val3 == val1


async def test_commit_binary_step_plan_with_automatic_bindings_succeeds(tmp_path: Path) -> None:
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "sokoban.sqlite3")
    settings = CoCreationAppSettings(
        token_secret="test-strong-secret",
        cocreation_deadline_seconds=900.0,
        demo_ttl_seconds=86400.0,
    )
    sessions_repo = SessionsRepository(db)
    dialogue_repo = DialogueRepository(db)
    op_repo = OperationalRepository(db)
    read_model_svc = SessionReadModelService(
        sessions_repo, dialogue_repo, op_repo, clock=clock, settings=settings
    )
    lifecycle = SessionLifecycleService(
        db,
        sessions_repo,
        op_repo,
        read_model_svc,
        room_sync=None,
        clock=clock,
        settings=settings,
    )
    created = await lifecycle.create_formal_session(
        rows=list(SAMPLE_ROWS),
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key="init-plan-bindings",
    )
    sid = created["sessionId"]
    bootstrap = parse_qs(urlparse(created["launchUrl"]).fragment)["bootstrap"][0]
    access = (await lifecycle.authorize_browser_access(sid, bootstrap))["accessToken"]
    await lifecycle.update_language(sid, access, language="en")
    read_model = sessions_repo.load_session_read_model(sid)
    assert read_model is not None
    current_ver = next(
        v for v in read_model.versions if v.version_id == read_model.session.current_version_id
    )

    initial_text = "remove wall at row 4 column 6 to help the box"
    initial_turns = [{"id": "user_turn_1", "content": initial_text}]
    snapshot = build_stage_facts(
        parse_grid(current_ver.rows), current_ver.entity_bindings
    ).to_prompt_dict()
    norm_initial, _ = normalize_requirement_record(
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
                    "focused": True,
                    "sourceTurnId": "user_turn_1",
                    "evidenceSpan": "remove wall at row 4 column 6",
                    "statement": "Remove wall at row 4 column 6",
                    "scope": "all",
                }
            ],
            "automaticBindings": [
                {"entity": "B1", "reason": "Nearest box to the central wall"},
            ],
            "exactTransitions": [
                {
                    "row": 4,
                    "column": 6,
                    "from": "#",
                    "to": ".",
                    "sourceTurnId": "user_turn_1",
                    "evidenceSpan": "remove wall at row 4 column 6",
                }
            ],
        },
        initial_turns,
        snapshot,
    )
    verified_initial_req = validate_requirement_record(norm_initial, initial_turns, snapshot)
    assert verified_initial_req["automaticBindings"][0]["authority"] == "proposal_assumption"

    prop_transport = FakeAsyncTransport(
        [
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Remove wall at row 4 column 6",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            }
        ]
    )
    prop_pipeline = ProposalPipeline(prop_transport, model="kimi-k2.6", clock=clock)
    step_res = BinaryDiscoveryResult(
        step={
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [
                {
                    "effect": "open_route",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["remove_wall"],
                    "preserve": ["water"],
                    "editBudget": 1,
                    "metricGoals": [],
                }
            ],
            "protectedEntities": [],
            "initialRequirements": None,
        },
        initial_requirements=verified_initial_req,
        branch_candidates=(),
        attempts_used=1,
        latency_ms=42,
    )

    committed = await commit_binary_step_impl(
        db=db,
        proposal_pipeline=prop_pipeline,
        binary_model="kimi-k2.6",
        room_sync=None,
        clock=clock,
        read_model=read_model,
        current_ver=current_ver,
        topic_id="top_plan_bindings",
        initial_request=initial_text,
        initial_turns=initial_turns,
        prior_choices=[],
        verified_initial_req=verified_initial_req,
        step_res=step_res,
        user_text=initial_text,
        user_visible_action=True,
        idempotency_key="idem-plan-bindings",
        request_id="req-plan-bindings",
        deadline=None,
    )
    assert committed["action"] == "plan"
    updated = sessions_repo.load_session_read_model(sid)
    assert updated is not None
    assert updated.proposals == []
    offer_turn = next(
        t for t in reversed(updated.turns) if t.turn_id == committed["assistantTurnId"]
    )
    assert isinstance(offer_turn.proposal_binding, dict)
    saved_req = offer_turn.proposal_binding["requirementRecord"]
    assert saved_req["automaticBindings"] == [
        {
            "entity": "B1",
            "reason": "Nearest box to the central wall",
            "authority": "proposal_assumption",
        }
    ]
    assert len(saved_req["exactTransitions"]) == 1
    assert isinstance(offer_turn.proposal_binding.get("revisionPlan"), dict)


# ---------------------------------------------------------------------------
# 7. Fast base-map preflight checks for widen_corridor & empty focus
# ---------------------------------------------------------------------------


async def test_binary_pipeline_fast_preflight_widen_corridor_and_empty_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    real_search = binary_pipeline_mod.search_revision_plan
    search_calls: list[int] = []

    def _spy_search(*args: Any, **kwargs: Any) -> Any:
        search_calls.append(1)
        return real_search(*args, **kwargs)

    monkeypatch.setattr(binary_pipeline_mod, "search_revision_plan", _spy_search)

    valid_attempt_2 = {
        "action": "ask",
        "question": "Which obstacle should we clear first?",
        "note": "",
        "branches": [_wall_branch(), _water_branch()],
        "planStrategies": [],
        "protectedEntities": [],
        "initialRequirements": _valid_initial_req(),
    }

    # Case 1: widen_corridor targeting non-walkable wall cell (4, 6) fails fast (0 search calls)
    search_calls.clear()
    t_nonwalk = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment first?",
                "note": "",
                "branches": [
                    {
                        "label": "Widen wall cell",
                        "strategy": _wall_branch()["strategy"],
                        "promise": {
                            "kind": "widen_corridor",
                            "component": "internal_walls",
                            "corridor": {"row": 4, "column": 6, "axis": "horizontal"},
                        },
                    },
                    _water_branch(),
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
            valid_attempt_2,
        ]
    )
    pipe_nonwalk = BinaryDiscoveryPipeline(t_nonwalk, model="kimi-k2.6", clock=clock)
    res_nonwalk = await pipe_nonwalk.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert res_nonwalk.attempts_used == 2
    assert len(search_calls) == 2  # Only attempt 2's 2 branches ran beam search
    msg_nonwalk = t_nonwalk.calls[1]["messages"][-1]["content"]
    assert "Branch 1 widen_corridor cell (4,6) is not walkable (tile='#')" in msg_nonwalk
    assert "'reduce_obstacles' or 'shorter_solution'" in msg_nonwalk

    # Case 2: widen_corridor on open room / wrong axis (2, 2) axis='horizontal' (width=10 >= 8)
    search_calls.clear()
    t_geom = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment first?",
                "note": "",
                "branches": [
                    {
                        "label": "Widen open row",
                        "strategy": _wall_branch()["strategy"],
                        "promise": {
                            "kind": "widen_corridor",
                            "component": "internal_walls",
                            "corridor": {"row": 2, "column": 2, "axis": "horizontal"},
                        },
                    },
                    _water_branch(),
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
            valid_attempt_2,
        ]
    )
    pipe_geom = BinaryDiscoveryPipeline(t_geom, model="kimi-k2.6", clock=clock)
    res_geom = await pipe_geom.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert res_geom.attempts_used == 2
    assert len(search_calls) == 2
    msg_geom = t_geom.calls[1]["messages"][-1]["content"]
    assert "Branch 1 widen_corridor at (2,2) axis='horizontal' fails corridor geometry" in msg_geom
    assert "width=10, orthogonal length=8" in msg_geom
    assert "perpendicular to travel direction" in msg_geom
    assert "axis='vertical'" in msg_geom

    # Case 3: reduce_obstacles/internal_walls focus containing ONLY outer-shell '#' (0 editable)
    search_calls.clear()
    t_shell = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment first?",
                "note": "",
                "branches": [
                    {
                        "label": "Clear corner walls",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 2, "column": 2, "radius": 1},
                            "operators": ["remove_wall"],
                            "preserve": ["water"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": {"row": 2, "column": 2, "radius": 1},
                        },
                    },
                    _water_branch(),
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
            valid_attempt_2,
        ]
    )
    pipe_shell = BinaryDiscoveryPipeline(t_shell, model="kimi-k2.6", clock=clock)
    res_shell = await pipe_shell.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert res_shell.attempts_used == 2
    assert len(search_calls) == 2
    msg_shell = t_shell.calls[1]["messages"][-1]["content"]
    assert "Branch 1 reduce_obstacles/internal_walls" in msg_shell
    assert "contains 0 matching editable tiles" in msg_shell

    # Case 4: reshape_water with empty focus (0 '@' tiles) fails fast with 0 search calls
    search_calls.clear()
    t_empty_water = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment first?",
                "note": "",
                "branches": [
                    {
                        "label": "Reshape corner water",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 2, "column": 2, "radius": 1},
                            "operators": ["add_water", "remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 2,
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
            valid_attempt_2,
        ]
    )
    pipe_empty_water = BinaryDiscoveryPipeline(t_empty_water, model="kimi-k2.6", clock=clock)
    res_empty_water = await pipe_empty_water.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert res_empty_water.attempts_used == 2
    assert len(search_calls) == 2
    msg_empty_water = t_empty_water.calls[1]["messages"][-1]["content"]
    assert "Branch 1 reshape_water/water" in msg_empty_water
    assert "requires both '@'" in msg_empty_water


def test_binary_discovery_prompt_corridor_and_blocked_guidance() -> None:
    from server.llm.prompts.proposal import build_requirements_messages

    snapshot = build_stage_facts(parse_grid(SAMPLE_ROWS), None).to_prompt_dict()
    msgs = build_binary_discovery_messages(
        stage_snapshot=snapshot,
        initial_request="widen the corridor",
        initial_designer_turns=[{"id": "t1", "content": "widen the corridor"}],
        question_count=0,
        language="en",
    )
    system = msgs[0]["content"]
    assert "axis='vertical'" in system
    assert "axis='horizontal'" in system
    assert "perpendicular to travel direction" in system
    assert "prefer reduce_obstacles or shorter_solution when unsure" in system
    assert "contradictory/impossible" in system
    assert "Allowed (property -> relations):" in system
    assert "Use property='change_scope' ONLY" in system
    assert "NEVER remap them onto component='wall' or property='change_scope'" in system
    assert "NEVER copy text from designContext into evidenceSpan" in system

    req_msgs = build_requirements_messages(
        designer_turns=[{"id": "t1", "content": "widen the corridor"}],
        stage_snapshot=snapshot,
        language="en",
    )
    req_system = req_msgs[0]["content"]
    assert "Allowed (property -> relations):" in req_system
    assert "Use property='change_scope' ONLY" in req_system
    assert "NEVER remap them onto component='wall' or property='change_scope'" in req_system
    assert "NEVER copy text from designContext into evidenceSpan" in req_system

    from server.llm.prompts.proposal import build_revision_plan_messages

    plan_msgs = build_revision_plan_messages(
        stage_snapshot=snapshot,
        authorized_brief="widen the corridor",
        language="en",
    )
    assert "Unless requirements.exactTransitions is non-empty, set requiredTransitions=[]" in (
        plan_msgs[0]["content"]
    )


def test_requirement_qualitative_seek_avoid_and_protection_normalization() -> None:
    req_schema = RequirementItemPayload.model_json_schema()
    assert req_schema["type"] == "object"
    assert "anyOf" not in req_schema
    assert "oneOf" not in req_schema

    # 1. Qualitative seek/avoid with value=None on change_scope / distribution / count coerces
    #    cleanly to component='gameplay', property='experience', unit='none', scope='all'.
    for bad_comp, bad_prop, bad_rel, bad_strength, span in (
        ("wall", "change_scope", "seek", "goal", "让通道更顺畅"),
        ("wall", "distribution", "avoid", "invariant", "避免死胡同"),
        ("water", "count", "seek", "goal", "想要舒服一点的体验"),
    ):
        coerced = RequirementItemPayload.model_validate(
            {
                "component": bad_comp,
                "property": bad_prop,
                "relation": bad_rel,
                "strength": bad_strength,
                "entities": [],
                "value": None,
                "unit": "none",
                "sourceTurnId": "t1",
                "evidenceSpan": span,
                "statement": span,
                "scope": "original_cells",
            }
        )
        assert coerced.component == "gameplay"
        assert coerced.property == "experience"
        assert coerced.relation == bad_rel
        assert coerced.unit == "none"
        assert coerced.scope == "all"
        assert coerced.strength in {"goal", "preference"}

    # appearance stays intact; appearance with numeric value still rejected
    app_ok = RequirementItemPayload.model_validate(
        {
            "component": "water",
            "property": "appearance",
            "relation": "seek",
            "strength": "preference",
            "entities": [],
            "value": None,
            "unit": "none",
            "sourceTurnId": "t1",
            "evidenceSpan": "nice pond",
            "statement": "Nice pond",
        }
    )
    assert app_ok.component == "water"
    assert app_ok.property == "appearance"
    with pytest.raises(ValidationError, match="property='appearance'"):
        RequirementItemPayload.model_validate(
            {
                "component": "water",
                "property": "appearance",
                "relation": "seek",
                "strength": "preference",
                "entities": [],
                "value": 2,
                "unit": "absolute",
                "sourceTurnId": "t1",
                "evidenceSpan": "2 nice ponds",
                "statement": "2 nice ponds",
            }
        )

    # 2. normalize_requirement_record drops unbounded contradictory change_scope while keeping
    #    explicit numeric change_scope caps.
    snapshot = build_stage_facts(parse_grid(SAMPLE_ROWS), None).to_prompt_dict()
    turns = [
        {
            "id": "t1",
            "content": (
                "Keep all boxes and targets fixed, Keep outer walls fixed, 保持外墙和玩家不变, "
                "Keep boxes and water fixed, 保持箱子和墙体不变, "
                "Keep targets fixed and reduce steps, "
                "保持玩家不动并拓宽通道, limit edits to 2 cells"
            ),
        }
    ]
    raw_record = {
        "requirements": [
            # Unbounded change_scope -> dropped
            {
                "component": "wall",
                "property": "change_scope",
                "relation": "preserve",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "limit edits to 2 cells",
                "statement": "preserve change scope",
                "scope": "all",
            },
            {
                "component": "wall",
                "property": "change_scope",
                "relation": "equal",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "limit edits to 2 cells",
                "statement": "equal change scope",
                "scope": "all",
            },
            {
                "component": "wall",
                "property": "change_scope",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "limit edits to 2 cells",
                "statement": "decrease change scope",
                "scope": "all",
            },
            # Explicit numeric change_scope -> kept
            {
                "component": "wall",
                "property": "change_scope",
                "relation": "decrease",
                "strength": "invariant",
                "entities": [],
                "value": 2,
                "unit": "absolute",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "limit edits to 2 cells",
                "statement": "limit edits to 2 cells",
                "scope": "all",
            },
            # Pure protected locks mis-mapped onto wall -> dropped
            {
                "component": "wall",
                "property": "positions",
                "relation": "preserve",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "Keep all boxes and targets fixed",
                "statement": "Keep all boxes and targets fixed",
                "scope": "all",
            },
            {
                "component": "wall",
                "property": "positions",
                "relation": "preserve",
                "strength": "invariant",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "保持外墙和玩家不变",
                "statement": "保持外墙和玩家不变",
                "scope": "all",
            },
            # Mixed sentences mentioning water/walls/steps/corridors -> MUST BE PRESERVED
            {
                "component": "water",
                "property": "positions",
                "relation": "preserve",
                "strength": "invariant",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "Keep boxes and water fixed",
                "statement": "Keep boxes and water fixed",
                "scope": "all",
            },
            {
                "component": "wall",
                "property": "count",
                "relation": "preserve",
                "strength": "invariant",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "保持箱子和墙体不变",
                "statement": "保持箱子和墙体不变",
                "scope": "all",
            },
            {
                "component": "gameplay",
                "property": "solutionSteps",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "Keep targets fixed and reduce steps",
                "statement": "Keep targets fixed and reduce steps",
                "scope": "all",
            },
            {
                "component": "gameplay",
                "property": "experience",
                "relation": "seek",
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t1",
                "evidenceSpan": "保持玩家不动并拓宽通道",
                "statement": "保持玩家不动并拓宽通道",
                "scope": "all",
            },
        ],
        "automaticBindings": [],
        "exactTransitions": [],
    }
    normalized, notes = normalize_requirement_record(raw_record, turns, snapshot)
    assert "dropped_unbounded_change_scope" in notes
    assert "dropped_platform_protection" in notes
    kept_spans = [item["evidenceSpan"] for item in normalized["requirements"]]
    assert kept_spans == [
        "limit edits to 2 cells",
        "Keep boxes and water fixed",
        "保持箱子和墙体不变",
        "Keep targets fixed and reduce steps",
        "保持玩家不动并拓宽通道",
    ]
    validated = validate_requirement_record(normalized, turns, snapshot)
    assert len(validated["requirements"]) == 5


@pytest.mark.asyncio
async def test_compile_revision_plan_optional_required_transitions_fallback() -> None:
    clock = FakeClock()
    # SAMPLE_ROWS has 'p' at row 3, column 3 (0-based x=2, y=2).
    bad_transition_plan = {
        "strategies": [
            {
                "effect": "open_route",
                "focus": {"row": 4, "column": 6, "radius": 1},
                "operators": ["remove_wall"],
                "preserve": ["water"],
                "editBudget": 2,
                "metricGoals": [],
                "requiredTransitions": [],
                "anchorEntities": [],
                "playObjective": None,
                "displayReason": "Open central wall",
            },
            {
                "effect": "narrow_route",
                "focus": {"row": 3, "column": 3, "radius": 1},
                "operators": ["add_wall"],
                "preserve": ["water"],
                "editBudget": 2,
                "metricGoals": [],
                "requiredTransitions": [
                    {"row": 3, "column": 3, "from": ".", "to": "#"},
                ],
                "anchorEntities": [],
                "playObjective": None,
                "displayReason": "Collides with player p if transition kept",
            },
        ]
    }

    # 1. When exactTransitions is empty, colliding optional requiredTransitions on strategy 2
    #    is stripped cleanly without failing the whole plan.
    t_empty = FakeAsyncTransport([bad_transition_plan])
    pipe = ProposalPipeline(t_empty, model="kimi-k2.6", clock=clock)
    plan, attempts = await pipe.compile_revision_plan(
        grid_or_rows=SAMPLE_ROWS,
        authorized_brief="adjust internal obstacles",
        requirements={"requirements": [], "automaticBindings": [], "exactTransitions": []},
        language="en",
    )
    assert attempts == 1
    assert len(plan.strategies) == 2
    assert plan.strategies[1].required_transitions == ()

    # 2. When exactTransitions is non-empty, colliding requiredTransitions MUST NOT be stripped.
    t_exact = FakeAsyncTransport([bad_transition_plan, bad_transition_plan])
    pipe_exact = ProposalPipeline(t_exact, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError):
        await pipe_exact.compile_revision_plan(
            grid_or_rows=SAMPLE_ROWS,
            authorized_brief="adjust internal obstacles",
            requirements={
                "requirements": [],
                "automaticBindings": [],
                "exactTransitions": [
                    {
                        "row": 4,
                        "column": 6,
                        "from": "#",
                        "to": ".",
                        "sourceTurnId": "t1",
                        "evidenceSpan": "row 4 col 6",
                    }
                ],
            },
            language="en",
            max_attempts=2,
        )
    assert len(t_exact.calls) == 2


def test_normalize_requirement_record_design_context_echo_boundary() -> None:
    snapshot = build_stage_facts(parse_grid(SAMPLE_ROWS), None).to_prompt_dict()
    turns = [
        {
            "id": "t2",
            "content": "Remove some internal walls around the center.",
        }
    ]
    design_context = {
        "userGoals": [
            {
                "goal": "Keep fewer internal walls overall.",
                "status": "active",
                "authority": "explicit",
            }
        ],
        "designConstraints": [],
        "intentHypotheses": [],
    }

    # 1. Cross-stage echo absent from user_turns and present in design_context -> dropped safely.
    echo_record = {
        "requirements": [
            {
                "component": "wall",
                "property": "count",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": 1,
                "unit": "delta",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t2",
                "evidenceSpan": "keep fewer internal walls",
                "statement": "Keep fewer internal walls",
                "scope": "all",
            }
        ],
        "automaticBindings": [],
        "exactTransitions": [],
    }
    norm_echo, echo_notes = normalize_requirement_record(
        echo_record, turns, snapshot, design_context=design_context
    )
    assert "dropped_design_context_echo" in echo_notes
    assert norm_echo["requirements"] == []

    # 2. Fabricated evidenceSpan NOT in design_context -> NOT dropped, rejected strictly!
    forged_span_record = {
        "requirements": [
            {
                "component": "wall",
                "property": "count",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": 5,
                "unit": "delta",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t2",
                "evidenceSpan": "remove 5 walls",
                "statement": "Remove 5 walls",
                "scope": "all",
            }
        ],
        "automaticBindings": [],
        "exactTransitions": [],
    }
    norm_forged, forged_notes = normalize_requirement_record(
        forged_span_record, turns, snapshot, design_context=design_context
    )
    assert "dropped_design_context_echo" not in forged_notes
    with pytest.raises(ValueError, match="not an exact verbatim substring"):
        validate_requirement_record(norm_forged, turns, snapshot)

    # 3. Real evidenceSpan IS in user_turns with fabricated number -> NOT dropped,
    #    rejected strictly!
    forged_num_record = {
        "requirements": [
            {
                "component": "wall",
                "property": "count",
                "relation": "decrease",
                "strength": "goal",
                "entities": [],
                "value": 3,
                "unit": "delta",
                "priorityEvidenceSpan": "",
                "focused": False,
                "sourceTurnId": "t2",
                "evidenceSpan": "Remove some internal walls",
                "statement": "Remove 3 walls",
                "scope": "all",
            }
        ],
        "automaticBindings": [],
        "exactTransitions": [],
    }
    norm_num, num_notes = normalize_requirement_record(
        forged_num_record, turns, snapshot, design_context=design_context
    )
    assert "dropped_design_context_echo" not in num_notes
    with pytest.raises(ValueError, match="numeric value=3"):
        validate_requirement_record(norm_num, turns, snapshot)


def test_binary_branch_payload_strategy_promise_alignment() -> None:
    with pytest.raises(ValidationError, match="requires 'remove_wall'"):
        BinaryBranchPayload.model_validate(
            {
                "label": "Add internal wall",
                "strategy": {
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["add_wall"],
                    "preserve": ["water"],
                    "editBudget": 2,
                },
                "promise": {
                    "kind": "reduce_obstacles",
                    "component": "internal_walls",
                    "focus": None,
                },
            }
        )

    with pytest.raises(ValidationError, match="requires 'remove_water'"):
        BinaryBranchPayload.model_validate(
            {
                "label": "Wrong operator for water reduction",
                "strategy": {
                    "effect": "open_route",
                    "focus": None,
                    "operators": ["remove_wall"],
                    "preserve": ["water"],
                    "editBudget": 2,
                },
                "promise": {
                    "kind": "reduce_obstacles",
                    "component": "water",
                    "focus": None,
                },
            }
        )

    with pytest.raises(ValidationError, match="requires 'remove_wall' or 'remove_water'"):
        BinaryBranchPayload.model_validate(
            {
                "label": "Narrow instead of widen",
                "strategy": {
                    "effect": "narrow_route",
                    "focus": None,
                    "operators": ["add_wall", "add_water"],
                    "preserve": [],
                    "editBudget": 2,
                },
                "promise": {
                    "kind": "widen_corridor",
                    "component": "obstacles",
                    "corridor": {"row": 5, "column": 4, "axis": "vertical"},
                },
            }
        )

    with pytest.raises(ValidationError, match="both 'add_water' and 'remove_water'"):
        BinaryBranchPayload.model_validate(
            {
                "label": "Reshape water without add_water",
                "strategy": {
                    "effect": "reshape_water",
                    "focus": None,
                    "operators": ["remove_water"],
                    "preserve": ["walls"],
                    "editBudget": 2,
                },
                "promise": {"kind": "reshape_water", "component": "water", "focus": None},
            }
        )

    with pytest.raises(ValidationError, match="editBudget >= 2"):
        BinaryBranchPayload.model_validate(
            {
                "label": "Reshape water with budget 1",
                "strategy": {
                    "effect": "reshape_water",
                    "focus": None,
                    "operators": ["add_water", "remove_water"],
                    "preserve": ["walls"],
                    "editBudget": 1,
                },
                "promise": {"kind": "reshape_water", "component": "water", "focus": None},
            }
        )

    ok_branch = BinaryBranchPayload.model_validate(
        {
            "label": "Shift central water pond",
            "strategy": {
                "effect": "reshape_water",
                "focus": None,
                "operators": ["add_water", "remove_water"],
                "preserve": ["walls"],
                "editBudget": 2,
            },
            "promise": {"kind": "reshape_water", "component": "water", "focus": None},
        }
    )
    assert ok_branch.promise.kind == "reshape_water"


@pytest.mark.asyncio
async def test_binary_pipeline_intersection_preflight_and_non_circular_retry_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    real_search = binary_pipeline_mod.search_revision_plan
    search_calls: list[int] = []

    def _spy_search(*args: Any, **kwargs: Any) -> Any:
        search_calls.append(1)
        return real_search(*args, **kwargs)

    monkeypatch.setattr(binary_pipeline_mod, "search_revision_plan", _spy_search)

    valid_attempt_2 = {
        "action": "ask",
        "question": "Which obstacle should we clear first?",
        "note": "",
        "branches": [_wall_branch(), _water_branch()],
        "planStrategies": [],
        "protectedEntities": [],
        "initialRequirements": _valid_initial_req(),
    }

    # 1. strategy.focus in empty corner (2,2) + promise.focus=None -> 0 internal '#' in
    #    intersection -> fast fail with 0 search calls!
    t_disjoint = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which center adjustment first?",
                "note": "",
                "branches": [
                    {
                        "label": "Clear corner wall",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 2, "column": 2, "radius": 1},
                            "operators": ["remove_wall"],
                            "preserve": ["water"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": None,
                        },
                    },
                    _water_branch(),
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
            valid_attempt_2,
        ]
    )
    pipe_disjoint = BinaryDiscoveryPipeline(t_disjoint, model="kimi-k2.6", clock=clock)
    res_disjoint = await pipe_disjoint.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert res_disjoint.attempts_used == 2
    assert len(search_calls) == 2  # Only attempt 2's 2 branches searched
    msg_disjoint = t_disjoint.calls[1]["messages"][-1]["content"]
    assert "contains 0 matching editable tiles inside strategy.focus=" in msg_disjoint

    # 2. reshape_water where intersection of strat_focus (row 3, col 2, r=1) and prom_focus
    #    (row 1, col 3, r=1) = row 2 cols 2..3 has '@' and 0 destination '.'/editable '#' ->
    #    fast fail with 0 search calls!
    water_locked_rows = list(SAMPLE_ROWS)
    water_locked_rows[1] = "#@@........#"
    t_nodest = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Adjust water?",
                "note": "",
                "branches": [
                    {
                        "label": "Reshape top-left water",
                        "strategy": {
                            "effect": "reshape_water",
                            "focus": {"row": 3, "column": 2, "radius": 1},
                            "operators": ["add_water", "remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reshape_water",
                            "component": "water",
                            "focus": {"row": 1, "column": 3, "radius": 1},
                        },
                    },
                    {
                        "label": "Clear center",
                        "strategy": {
                            "effect": "open_route",
                            "focus": None,
                            "operators": ["remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": None,
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
            {
                "action": "plan",
                "question": "",
                "note": "",
                "branches": [],
                "planStrategies": [
                    {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_water"],
                        "preserve": ["walls"],
                        "editBudget": 1,
                    }
                ],
                "protectedEntities": [],
                "initialRequirements": _valid_initial_req(),
            },
        ]
    )
    search_calls.clear()
    pipe_nodest = BinaryDiscoveryPipeline(t_nodest, model="kimi-k2.6", clock=clock)
    await pipe_nodest.run_step(
        grid_or_rows=water_locked_rows,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
        language="en",
    )
    assert len(search_calls) == 0
    msg_nodest = t_nodest.calls[1]["messages"][-1]["content"]
    assert "requires both '@' (got 2) and destination '.' or editable '#' (got 0)" in msg_nodest

    # 3. Retry advice for reduce_obstacles NEVER tells the model "switching to 'reduce_obstacles'"
    def _fail_promise(*args: Any, **kwargs: Any) -> Any:
        raise binary_pipeline_mod.ProposalSearchExhausted(
            {"failureReasons": {"BINARY_PROMISE_NOT_REALIZED": 10}}
        )

    monkeypatch.setattr(binary_pipeline_mod, "search_revision_plan", _fail_promise)
    t_retry = FakeAsyncTransport([valid_attempt_2, valid_attempt_2])
    pipe_retry = BinaryDiscoveryPipeline(t_retry, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError):
        await pipe_retry.run_step(
            grid_or_rows=grid,
            initial_request="open up the center",
            initial_designer_turns=[{"id": "t1", "content": "open up the center"}],
            language="en",
        )
    retry_msg = t_retry.calls[1]["messages"][-1]["content"]
    assert "switching to 'reduce_obstacles'" not in retry_msg
    assert "switching to 'shorter_solution' or 'reshape_water'" in retry_msg


def test_backup_item_filtering_and_effect_operator_prompts() -> None:
    from server.llm.prompts.binary import build_binary_discovery_messages
    from server.llm.prompts.proposal import build_revision_plan_messages
    from server.llm.schemas import (
        BinaryDiscoveryFollowupFinalPayload,
        BinaryDiscoveryFollowupPayload,
        BinaryDiscoveryInitialFinalPayload,
        BinaryDiscoveryInitialPayload,
        MapOperationsPayload,
        RevisionPlanPayload,
    )

    valid_rev_strat = {
        "effect": "open_route",
        "focus": None,
        "operators": ["remove_wall"],
        "preserve": ["water"],
        "editBudget": 2,
        "metricGoals": [],
        "requiredTransitions": [],
        "anchorEntities": [],
        "playObjective": None,
        "displayReason": None,
    }
    invalid_rev_strat = {
        "effect": "narrow_route",
        "focus": None,
        "operators": ["remove_wall"],
        "preserve": ["water"],
        "editBudget": 2,
        "metricGoals": [],
        "requiredTransitions": [],
        "anchorEntities": [],
        "playObjective": None,
        "displayReason": None,
    }

    # RevisionPlanPayload keeps valid primary strategy when backup is invalid (len 2..3)
    rev_filtered = RevisionPlanPayload.model_validate(
        {"strategies": [valid_rev_strat, invalid_rev_strat]}
    )
    assert len(rev_filtered.strategies) == 1
    assert rev_filtered.strategies[0].effect == "open_route"

    # RevisionPlanPayload passes through untouched when all strategies fail (0 valid)
    with pytest.raises(ValidationError) as exc_rev_all_bad:
        RevisionPlanPayload.model_validate({"strategies": [invalid_rev_strat, invalid_rev_strat]})
    assert "cannot realize effect" in str(exc_rev_all_bad.value)

    # RevisionPlanPayload preserves max_length=3 without truncating >3 items
    with pytest.raises(ValidationError) as exc_rev_too_many:
        RevisionPlanPayload.model_validate({"strategies": [valid_rev_strat] * 4})
    assert "at most 3 items" in str(exc_rev_too_many.value)

    # MapOperationsPayload keeps valid primary candidate when backup op/from/to mismatches
    valid_cand = {
        "strategyIndex": 1,
        "rationale": "valid remove wall",
        "operations": [{"row": 3, "column": 4, "op": "remove_wall", "from": "#", "to": "."}],
    }
    invalid_cand = {
        "strategyIndex": 1,
        "rationale": "invalid op pair",
        "operations": [{"row": 3, "column": 5, "op": "remove_wall", "from": ".", "to": "#"}],
    }
    ops_filtered = MapOperationsPayload.model_validate({"candidates": [valid_cand, invalid_cand]})
    assert len(ops_filtered.candidates) == 1

    with pytest.raises(ValidationError):
        MapOperationsPayload.model_validate({"candidates": [invalid_cand, invalid_cand]})

    with pytest.raises(ValidationError) as exc_ops_too_many:
        MapOperationsPayload.model_validate({"candidates": [valid_cand] * 4})
    assert "at most 3 items" in str(exc_ops_too_many.value)

    # All 4 BinaryDiscovery*Payload schemas reuse the backup-item filter for planStrategies
    valid_bin_strat = {
        "effect": "open_route",
        "focus": None,
        "operators": ["remove_wall"],
        "preserve": ["water"],
        "editBudget": 2,
    }
    invalid_bin_strat = {
        "effect": "narrow_route",
        "focus": None,
        "operators": ["remove_wall"],
        "preserve": ["water"],
        "editBudget": 2,
    }
    init_req = _valid_initial_req()

    variants: list[tuple[type[Any], bool, bool]] = [
        (BinaryDiscoveryInitialPayload, True, True),
        (BinaryDiscoveryFollowupPayload, False, True),
        (BinaryDiscoveryInitialFinalPayload, True, False),
        (BinaryDiscoveryFollowupFinalPayload, False, False),
    ]
    for schema_cls, needs_init, can_ask in variants:
        base_plan: dict[str, Any] = {
            "action": "plan",
            "note": "",
            "planStrategies": [valid_bin_strat, invalid_bin_strat],
            "protectedEntities": [],
        }
        if can_ask:
            base_plan["question"] = ""
            base_plan["branches"] = []
        if needs_init:
            base_plan["initialRequirements"] = init_req

        parsed = schema_cls.model_validate(base_plan)
        assert len(parsed.plan_strategies) == 1
        assert parsed.plan_strategies[0].effect == "open_route"

        # 0 valid strategies -> exact ValidationError preserved
        with pytest.raises(ValidationError) as exc_bin_all_bad:
            schema_cls.model_validate(
                {**base_plan, "planStrategies": [invalid_bin_strat, invalid_bin_strat]}
            )
        assert "cannot realize effect" in str(exc_bin_all_bad.value)

        # >3 strategies -> max_length=3 preserved (never silently sliced to 3)
        with pytest.raises(ValidationError) as exc_bin_too_many:
            schema_cls.model_validate({**base_plan, "planStrategies": [valid_bin_strat] * 4})
        assert "at most 3 items" in str(exc_bin_too_many.value)

        # Non-'plan' action ('blocked') STILL rejects non-empty planStrategies
        blocked_payload: dict[str, Any] = {
            "action": "blocked",
            "note": "No safe editable path",
            "planStrategies": [valid_bin_strat, invalid_bin_strat],
            "protectedEntities": [],
        }
        if can_ask:
            blocked_payload["question"] = ""
            blocked_payload["branches"] = []
        if needs_init:
            blocked_payload["initialRequirements"] = None
        with pytest.raises(ValidationError) as exc_blocked:
            schema_cls.model_validate(blocked_payload)
        assert "planStrategies (must be [])" in str(exc_blocked.value)

        if can_ask:
            ask_payload: dict[str, Any] = {
                "action": "ask",
                "question": "Which obstacle should we clear?",
                "note": "",
                "branches": [_wall_branch(), _water_branch()],
                "planStrategies": [valid_bin_strat, invalid_bin_strat],
                "protectedEntities": [],
            }
            if needs_init:
                ask_payload["initialRequirements"] = init_req
            with pytest.raises(ValidationError) as exc_ask:
                schema_cls.model_validate(ask_payload)
            assert "planStrategies (must be [])" in str(exc_ask.value)

    # Both prompts expose explicit (effect -> required usable operators) mapping
    grid = parse_grid(SAMPLE_ROWS)
    snap = {"rows": grid.rows, "editableInternalWallCells": [], "entities": []}
    rev_msgs = build_revision_plan_messages(
        stage_snapshot=snap,
        authorized_brief="Open a path through the center.",
        language="en",
    )
    bin_msgs = build_binary_discovery_messages(
        stage_snapshot=snap,
        initial_request="Open a path through the center.",
        initial_designer_turns=[{"id": "t1", "content": "Open a path through the center."}],
        question_count=0,
        language="en",
    )
    for msgs in (rev_msgs, bin_msgs):
        sys_prompt = msgs[0]["content"]
        assert "open_route -> remove_wall|remove_water" in sys_prompt
        assert "narrow_route -> add_wall|add_water" in sys_prompt
        assert "adjust_internal_walls -> add_wall|remove_wall" in sys_prompt
        assert "reshape_water -> add_water|remove_water" in sys_prompt
