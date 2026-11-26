"""Shared constants, exceptions, requirement models, and search dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EDITABLE_OPERATORS: frozenset[str] = frozenset(
    {"add_wall", "remove_wall", "add_water", "remove_water"}
)
ALLOWED_EFFECTS: frozenset[str] = frozenset(
    {"open_route", "narrow_route", "adjust_internal_walls", "reshape_water"}
)
PRESERVE_COMPONENTS: frozenset[str] = frozenset(
    {"outer_shell", "player", "boxes", "targets", "water", "walls", "unrelated_areas"}
)
SEARCH_METRICS: frozenset[str] = frozenset(
    {"solutionSteps", "solutionPushes", "minimumPushes", "searchedStates"}
)
METRIC_DIRECTIONS: frozenset[str] = frozenset({"increase", "decrease", "preserve"})

EFFECT_OPERATORS: dict[str, set[str]] = {
    "open_route": {"remove_wall", "remove_water"},
    "narrow_route": {"add_wall", "add_water"},
    "adjust_internal_walls": {"add_wall", "remove_wall"},
    "reshape_water": {"add_water", "remove_water"},
}
OPERATOR_COMPONENT: dict[str, str] = {
    "add_wall": "walls",
    "remove_wall": "walls",
    "add_water": "water",
    "remove_water": "water",
}

BEAM_WIDTH = 24
BEAM_DEPTH = 3
PRIMITIVE_LIMIT = 32
MAX_CONSTRUCTED_CANDIDATES = 128
MAX_VALID_CANDIDATES = 12
MAX_EDIT_BUDGET = 12

POLICY_VERSION = 1
INTERPRETATION_VERSION = 2
REQ_COMPONENTS: frozenset[str] = frozenset(
    {"water", "wall", "player", "box", "target", "gameplay"}
)
REQ_PROPERTIES: frozenset[str] = frozenset(
    {
        "count",
        "positions",
        "distribution",
        "distance",
        "change_scope",
        "solutionSteps",
        "minimumPushes",
        "solutionPushes",
        "boxAlternations",
        "longestPushRun",
        "timing",
        "dependency",
        "experience",
        "appearance",
    }
)
REQ_RELATIONS: frozenset[str] = frozenset(
    {
        "increase",
        "decrease",
        "equal",
        "preserve",
        "relocate",
        "nearer",
        "farther",
        "early",
        "later",
        "seek",
        "avoid",
    }
)
REQ_METRICS: frozenset[str] = frozenset(
    {"solutionSteps", "minimumPushes", "solutionPushes", "boxAlternations", "longestPushRun"}
)
REQ_TILES: dict[str, set[str]] = {
    "water": {"@"},
    "wall": {"#"},
    "player": {"p", "+"},
    "box": {"s", "*"},
    "target": {"t", "+", "*"},
}
PROPERTY_RELATIONS: dict[str, set[str]] = {
    **{
        prop: {"increase", "decrease", "equal", "preserve"}
        for prop in REQ_METRICS | {"count", "distribution", "change_scope"}
    },
    "distance": {"nearer", "farther", "equal", "preserve"},
    "appearance": {"seek", "avoid"},
}
_WALKABLE_TILES = frozenset(".pst")
_CHINESE_DIGITS = {
    0: "零",
    1: "一",
    2: "两",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
}


class RevisionPlanError(ValueError):
    pass


class ProposalSearchExhausted(ValueError):
    def __init__(self, diagnostics: dict[str, Any]) -> None:
        super().__init__("Deterministic search found no solvable map satisfying the revision plan.")
        self.diagnostics = diagnostics


class BinaryError(ValueError):
    def __init__(
        self,
        code: str,
        field: str = "branches",
        retryable: bool = True,
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.field = field
        self.retryable = retryable
        self.detail = detail
        super().__init__(code)


class RequirementItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requirement_id: str = Field(default="", alias="requirementId")
    component: str
    property: str
    relation: str
    strength: Literal["invariant", "goal", "preference"]
    entities: list[str] = Field(default_factory=list)
    value: int | None = None
    unit: Literal["absolute", "delta", "none"]
    priority_evidence_span: str = Field(default="", alias="priorityEvidenceSpan")
    focused: bool = False
    source_turn_id: str = Field(alias="sourceTurnId")
    source_turn_ids: list[str] | None = Field(default=None, alias="sourceTurnIds")
    evidence_span: str = Field(alias="evidenceSpan")
    statement: str
    scope: Literal["all", "original_cells"] = "all"


class AutomaticBinding(BaseModel):
    entity: str
    reason: str
    authority: str = "proposal_assumption"


class ExactTransition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    row: int
    column: int
    before: Literal[".", "#", "@"] = Field(alias="from")
    after: Literal[".", "#", "@"] = Field(alias="to")
    source_turn_id: str = Field(alias="sourceTurnId")
    evidence_span: str = Field(alias="evidenceSpan")


class RequirementRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    policy_version: int = Field(default=POLICY_VERSION, alias="policyVersion")
    requirements: list[RequirementItem] = Field(default_factory=list)
    automatic_bindings: list[AutomaticBinding] = Field(
        default_factory=list,
        alias="automaticBindings",
    )
    exact_transitions: list[ExactTransition] = Field(
        default_factory=list,
        alias="exactTransitions",
    )


@dataclass(frozen=True)
class Focus:
    row: int
    column: int
    radius: int


@dataclass(frozen=True)
class MetricGoal:
    metric: str
    direction: str
    minimum_delta: int = 1


@dataclass(frozen=True)
class RevisionStrategy:
    effect: str
    focus: Focus | None
    operators: tuple[str, ...]
    preserve: frozenset[str]
    edit_budget: int
    metric_goals: tuple[MetricGoal, ...]
    required_transitions: tuple[tuple[int, int, str, str], ...] = ()
    anchor_entities: tuple[str, ...] = ()
    play_objective: str | None = None
    display_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "focus": (
                {"row": self.focus.row, "column": self.focus.column, "radius": self.focus.radius}
                if self.focus is not None
                else None
            ),
            "operators": list(self.operators),
            "preserve": sorted(self.preserve),
            "editBudget": self.edit_budget,
            "metricGoals": [
                {
                    "metric": g.metric,
                    "direction": g.direction,
                    **({"minimumDelta": g.minimum_delta} if g.minimum_delta != 1 else {}),
                }
                for g in self.metric_goals
            ],
            "requiredTransitions": [
                {"row": r, "column": c, "from": before, "to": after}
                for r, c, before, after in self.required_transitions
            ],
            "anchorEntities": list(self.anchor_entities),
            "playObjective": self.play_objective,
            **({"displayReason": self.display_reason} if self.display_reason else {}),
        }


@dataclass(frozen=True)
class RevisionPlan:
    strategies: tuple[RevisionStrategy, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"strategies": [s.as_dict() for s in self.strategies]}


@dataclass(frozen=True)
class Primitive:
    operator: str
    changes: tuple[tuple[int, int, str, str], ...]
    preserves_solution: bool
    distance: int

    @property
    def positions(self) -> frozenset[tuple[int, int]]:
        return frozenset((x, y) for x, y, _, _ in self.changes)


@dataclass(frozen=True)
class SearchState:
    primitives: tuple[Primitive, ...]
    rows: tuple[str, ...]

    @property
    def operators(self) -> frozenset[str]:
        ops: set[str] = set()
        for primitive in self.primitives:
            if primitive.operator == "water_to_wall":
                ops.update({"remove_water", "add_wall"})
            elif primitive.operator == "wall_to_water":
                ops.update({"remove_wall", "add_water"})
            elif primitive.operator.startswith("compensate_"):
                component = primitive.operator.removeprefix("compensate_")
                ops.update({f"remove_{component}", f"add_{component}"})
            else:
                ops.add(primitive.operator)
        return frozenset(ops)


@dataclass(frozen=True)
class VerifiedCandidate:
    rows: tuple[str, ...]
    validation: Any
    strategy_index: int
    operators: tuple[str, ...]
    metric_matches: int
    effect_match: int
    changed_cells: int

    @property
    def selection_key(self) -> tuple[Any, ...]:
        return (-self.metric_matches, -self.effect_match, self.changed_cells, self.rows)


@dataclass(frozen=True)
class ProposalSearchResult:
    rows: tuple[str, ...]
    validation: Any
    strategy_index: int
    operators: tuple[str, ...]
    score: dict[str, Any]
    diagnostics: dict[str, Any]
