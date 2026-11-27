"""Refined Pydantic v2 contracts for Policy-2 binary discovery, proposals, and requirements."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from server.domain.proposals.models import (
    EFFECT_OPERATORS,
    OPERATOR_COMPONENT,
    PROPERTY_RELATIONS,
)

from ._schemas_tasks import (
    EditableOpName,
    EditableTileChar,
    EffectKind,
    PreserveKey,
    ProtectedEntityKey,
    StrictSchema,
)

EntityId = Literal["P", "B1", "B2", "T1", "T2"]
BinaryPreserveKey = Literal["water", "walls"]
_PLATFORM_FIXED_PRESERVE: frozenset[str] = frozenset(
    {"outer_shell", "player", "boxes", "targets", "unrelated_areas"}
)
_GENERIC_ENTITY_LABELS: frozenset[str] = frozenset(
    {
        "PLAYER",
        "BOX",
        "BOXES",
        "TARGET",
        "TARGETS",
        "WALL",
        "WALLS",
        "WATER",
        "OUTER_SHELL",
        "GAMEPLAY",
    }
)

_OP_TO_TRANSITION: dict[str, tuple[str, str]] = {
    "add_wall": (".", "#"),
    "remove_wall": ("#", "."),
    "add_water": (".", "@"),
    "remove_water": ("@", "."),
}
_REQUIRED_TRANSITION_OPS: dict[tuple[str, str], tuple[str, ...]] = {
    (".", "#"): ("add_wall",),
    ("#", "."): ("remove_wall",),
    (".", "@"): ("add_water",),
    ("@", "."): ("remove_water",),
    ("@", "#"): ("remove_water", "add_wall"),
    ("#", "@"): ("remove_wall", "add_water"),
}


class FocusAreaPayload(StrictSchema):
    row: int = Field(ge=1, le=10)
    column: int = Field(ge=1, le=12)
    radius: int = Field(ge=1, le=3)


class MetricGoalPayload(StrictSchema):
    metric: Literal["solutionSteps", "solutionPushes", "minimumPushes", "searchedStates"]
    direction: Literal["increase", "decrease", "preserve"]
    minimum_delta: int = Field(default=1, alias="minimumDelta", ge=1, le=1000)


class RequiredTransitionPayload(StrictSchema):
    row: int = Field(ge=2, le=9)
    column: int = Field(ge=2, le=11)
    before: EditableTileChar = Field(alias="from")
    after: EditableTileChar = Field(alias="to")

    @model_validator(mode="after")
    def _validate_transition(self) -> RequiredTransitionPayload:
        if self.before == self.after:
            raise ValueError(
                f"requiredTransition at ({self.row}, {self.column}) has identical 'from' and 'to' "
                f"tile {self.before!r}; 'from' and 'to' must differ."
            )
        return self


class RevisionStrategyPayload(StrictSchema):
    effect: EffectKind
    focus: FocusAreaPayload | None = None
    operators: list[EditableOpName] = Field(min_length=1, max_length=4)
    preserve: list[PreserveKey] = Field(default_factory=list, max_length=7)
    edit_budget: int = Field(alias="editBudget", ge=1, le=12)
    metric_goals: list[MetricGoalPayload] = Field(
        default_factory=list, alias="metricGoals", max_length=3
    )
    required_transitions: list[RequiredTransitionPayload] = Field(
        default_factory=list, alias="requiredTransitions", max_length=12
    )
    anchor_entities: list[EntityId] = Field(
        default_factory=list, alias="anchorEntities", max_length=5
    )
    play_objective: str | None = Field(default=None, alias="playObjective", max_length=120)
    display_reason: str | None = Field(default=None, alias="displayReason", max_length=600)

    @model_validator(mode="after")
    def _validate_strategy(self) -> RevisionStrategyPayload:
        if len(set(self.operators)) != len(self.operators):
            raise ValueError(f"operators must not contain duplicates; got {self.operators}.")
        if len(set(self.preserve)) != len(self.preserve):
            raise ValueError(f"preserve must not contain duplicates; got {self.preserve}.")
        if len(set(self.anchor_entities)) != len(self.anchor_entities):
            raise ValueError(
                f"anchorEntities must not contain duplicates; got {self.anchor_entities}."
            )
        seen_metrics: set[str] = set()
        for goal in self.metric_goals:
            if goal.metric in seen_metrics:
                raise ValueError(
                    f"metricGoals repeats metric {goal.metric!r}; "
                    "each metric may appear at most once."
                )
            seen_metrics.add(goal.metric)

        usable_operators = [
            op for op in self.operators if OPERATOR_COMPONENT.get(op) not in self.preserve
        ]
        if not usable_operators:
            raise ValueError(
                f"strategy preserves every component it asks to edit "
                f"(operators={self.operators}, preserve={self.preserve}); "
                "remove 'walls' or 'water' from preserve or change operators."
            )
        allowed_for_effect = EFFECT_OPERATORS[self.effect]
        if not (set(usable_operators) & allowed_for_effect):
            raise ValueError(
                f"usable operators {usable_operators} cannot realize effect {self.effect!r}; "
                f"include at least one of {sorted(allowed_for_effect)}."
            )

        if len(self.required_transitions) > self.edit_budget:
            raise ValueError(
                f"requiredTransitions has {len(self.required_transitions)} items, exceeding "
                f"editBudget={self.edit_budget}."
            )
        seen_coords: set[tuple[int, int]] = set()
        usable_set = set(usable_operators)
        for tr in self.required_transitions:
            coord = (tr.row, tr.column)
            if coord in seen_coords:
                raise ValueError(
                    f"requiredTransitions repeats coordinate {coord}; each cell may appear once."
                )
            seen_coords.add(coord)
            needed_ops = _REQUIRED_TRANSITION_OPS.get((tr.before, tr.after))
            if needed_ops is None or not set(needed_ops).issubset(usable_set):
                raise ValueError(
                    f"requiredTransition at {coord} ({tr.before!r}->{tr.after!r}) requires "
                    f"operators {list(needed_ops or ())}, which are not covered by usable "
                    f"operators {usable_operators}."
                )
            if self.focus is not None:
                if (
                    max(abs(self.focus.row - tr.row), abs(self.focus.column - tr.column))
                    > self.focus.radius
                ):
                    raise ValueError(
                        f"requiredTransition at {coord} is outside focus "
                        f"(row={self.focus.row}, column={self.focus.column}, "
                        f"radius={self.focus.radius})."
                    )

        if self.play_objective is not None:
            if "\n" in self.play_objective or "\r" in self.play_objective:
                raise ValueError("playObjective must be a single line without newline characters.")
            stripped_objective = self.play_objective.strip()
            self.play_objective = stripped_objective or None
        return self


def _filter_valid_backup_items(value: Any, item_cls: type[StrictSchema]) -> Any:
    """Drop invalid backup items only when 2..3 items are supplied and >=1 is valid."""
    if not isinstance(value, list) or not (2 <= len(value) <= 3):
        return value
    valid_items: list[Any] = []
    for item in value:
        try:
            item_cls.model_validate(item)
        except ValidationError:
            continue
        valid_items.append(item)
    return valid_items if valid_items else value


class RevisionPlanPayload(StrictSchema):
    strategies: list[RevisionStrategyPayload] = Field(min_length=1, max_length=3)

    @field_validator("strategies", mode="before")
    @classmethod
    def _filter_strategies(cls, value: Any) -> Any:
        return _filter_valid_backup_items(value, RevisionStrategyPayload)


class RequirementItemPayload(StrictSchema):
    component: Literal["water", "wall", "player", "box", "target", "gameplay"]
    property: Literal[
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
    ]
    relation: Literal[
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
    ]
    strength: Literal["invariant", "goal", "preference"]
    entities: list[EntityId] = Field(default_factory=list, max_length=5)
    value: int | None = Field(default=None, ge=0)
    unit: Literal["absolute", "delta", "none"]
    priority_evidence_span: str = Field(default="", alias="priorityEvidenceSpan", max_length=400)
    focused: bool = False
    source_turn_id: str = Field(alias="sourceTurnId", min_length=1, max_length=128)
    evidence_span: str = Field(alias="evidenceSpan", min_length=1, max_length=400)
    statement: str = Field(min_length=1, max_length=400)
    scope: Literal["all", "original_cells"] = "all"

    @field_validator("entities", mode="before")
    @classmethod
    def _normalize_entities(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        seen: set[str] = set()
        for item in value:
            if isinstance(item, str):
                upper = item.strip().upper()
                if not upper or upper in _GENERIC_ENTITY_LABELS or upper in seen:
                    continue
                seen.add(upper)
                normalized.append(upper)
            else:
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def _validate_requirement_item(self) -> RequirementItemPayload:
        if not self.source_turn_id.strip():
            raise ValueError("sourceTurnId must be a non-blank string.")
        if not self.evidence_span.strip():
            raise ValueError("evidenceSpan must be a non-blank string.")
        if not self.statement.strip():
            raise ValueError("statement must be a non-blank string.")
        if len(set(self.entities)) != len(self.entities):
            raise ValueError(f"entities must not contain duplicates; got {self.entities}.")
        if (
            self.value is None
            and self.relation in {"seek", "avoid"}
            and self.property not in {"appearance", "experience", "timing", "dependency"}
        ):
            self.component = "gameplay"
            self.property = "experience"
            self.unit = "none"
            self.scope = "all"
            if self.strength == "invariant":
                self.strength = "goal"
        if self.scope == "original_cells" and (
            self.property != "count" or self.component not in {"water", "wall"}
        ):
            self.scope = "all"
        if self.component == "gameplay" and self.property in {
            "count",
            "positions",
            "distribution",
            "distance",
        }:
            raise ValueError(
                f"component='gameplay' cannot use map property {self.property!r}; "
                "select a map component ('water', 'wall', 'player', 'box', 'target')."
            )
        if self.strength == "invariant" and not (
            (
                self.property in {"count", "positions", "distribution"}
                and self.relation in {"preserve", "equal"}
            )
            or (self.property == "change_scope" and self.value is not None)
        ):
            raise ValueError(
                f"strength='invariant' is invalid for property={self.property!r}, "
                f"relation={self.relation!r}; use strength='goal' for directional wishes."
            )
        if self.property in PROPERTY_RELATIONS and (
            self.relation not in PROPERTY_RELATIONS[self.property]
        ):
            raise ValueError(
                f"property={self.property!r} does not allow relation={self.relation!r}; "
                f"allowed relations: {sorted(PROPERTY_RELATIONS[self.property])}."
            )
        if self.property == "positions" and self.relation not in {"preserve", "equal", "relocate"}:
            raise ValueError(
                f"property='positions' requires relation in ['equal', 'preserve', 'relocate']; "
                f"got {self.relation!r}."
            )
        if self.property == "appearance" and (
            self.relation not in {"seek", "avoid"}
            or self.value is not None
            or self.unit != "none"
        ):
            raise ValueError(
                "property='appearance' requires relation in ['avoid', 'seek'] with "
                "value=null and unit='none'."
            )
        if self.value is not None and self.unit == "none":
            raise ValueError(
                f"unit cannot be 'none' when value={self.value} is set; use 'absolute' or 'delta'."
            )
        if self.value is None and self.unit != "none":
            raise ValueError(
                f"unit must be 'none' when value is null; got unit={self.unit!r}."
            )
        if (
            self.unit == "delta"
            and self.relation in {"increase", "decrease"}
            and self.value == 0
        ):
            raise ValueError(
                f"delta {self.relation!r} requirement must have value >= 1, got 0."
            )
        return self


class AutomaticBindingPayload(StrictSchema):
    entity: EntityId
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("entity", mode="before")
    @classmethod
    def _normalize_entity(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _validate_reason(self) -> AutomaticBindingPayload:
        if not self.reason.strip():
            raise ValueError("automaticBinding.reason must be a non-blank string.")
        return self


class ExactTransitionPayload(StrictSchema):
    row: int = Field(ge=2, le=9)
    column: int = Field(ge=2, le=11)
    before: EditableTileChar = Field(alias="from")
    after: EditableTileChar = Field(alias="to")
    source_turn_id: str = Field(alias="sourceTurnId", min_length=1, max_length=128)
    evidence_span: str = Field(alias="evidenceSpan", min_length=1, max_length=400)

    @model_validator(mode="after")
    def _validate_exact_transition(self) -> ExactTransitionPayload:
        if self.before == self.after:
            raise ValueError(
                f"exactTransition at ({self.row}, {self.column}) has identical 'from' and 'to' "
                f"tile {self.before!r}; 'from' and 'to' must differ."
            )
        if not self.source_turn_id.strip():
            raise ValueError("exactTransition.sourceTurnId must be a non-blank string.")
        if not self.evidence_span.strip():
            raise ValueError("exactTransition.evidenceSpan must be a non-blank string.")
        return self


class RequirementRecordPayload(StrictSchema):
    requirements: list[RequirementItemPayload] = Field(default_factory=list, max_length=48)
    automatic_bindings: list[AutomaticBindingPayload] = Field(
        default_factory=list, alias="automaticBindings", max_length=8
    )
    exact_transitions: list[ExactTransitionPayload] = Field(
        default_factory=list, alias="exactTransitions", max_length=12
    )

    @field_validator("automatic_bindings", mode="before")
    @classmethod
    def _normalize_automatic_bindings(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        cleaned: list[Any] = []
        for item in value:
            if isinstance(item, Mapping):
                raw_entity = item.get("entity")
                if isinstance(raw_entity, str):
                    upper = raw_entity.strip().upper()
                    if upper in _GENERIC_ENTITY_LABELS:
                        continue
                    item = {**dict(item), "entity": upper}
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _validate_record(self) -> RequirementRecordPayload:
        seen_coords: set[tuple[int, int]] = set()
        for tr in self.exact_transitions:
            coord = (tr.row, tr.column)
            if coord in seen_coords:
                raise ValueError(
                    f"exactTransitions repeats coordinate {coord}; each cell may appear once."
                )
            seen_coords.add(coord)
        if self.automatic_bindings and not any(not req.entities for req in self.requirements):
            self.automatic_bindings = []
        return self


class MapOperationItemPayload(StrictSchema):
    """Single AI-editable map operation (strictly interior wall/water edits ONLY)."""

    row: int = Field(ge=2, le=9)
    column: int = Field(ge=2, le=11)
    op: EditableOpName
    before: EditableTileChar = Field(alias="from")
    after: EditableTileChar = Field(alias="to")

    @model_validator(mode="after")
    def _validate_op_transition(self) -> MapOperationItemPayload:
        expected_before, expected_after = _OP_TO_TRANSITION[self.op]
        if (self.before, self.after) != (expected_before, expected_after):
            raise ValueError(
                f"operation {self.op!r} at ({self.row}, {self.column}) requires "
                f"from={expected_before!r} and to={expected_after!r}, "
                f"got from={self.before!r}, to={self.after!r}."
            )
        return self


class MapOperationCandidatePayload(StrictSchema):
    strategy_index: int = Field(default=1, alias="strategyIndex", ge=1, le=3)
    rationale: str = Field(default="", max_length=400)
    operations: list[MapOperationItemPayload] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _validate_unique_coords(self) -> MapOperationCandidatePayload:
        seen: set[tuple[int, int]] = set()
        for op in self.operations:
            coord = (op.row, op.column)
            if coord in seen:
                raise ValueError(
                    f"candidate repeats coordinate {coord}; each cell may appear at most once."
                )
            seen.add(coord)
        return self


class MapOperationsPayload(StrictSchema):
    candidates: list[MapOperationCandidatePayload] = Field(min_length=1, max_length=3)

    @field_validator("candidates", mode="before")
    @classmethod
    def _filter_candidates(cls, value: Any) -> Any:
        return _filter_valid_backup_items(value, MapOperationCandidatePayload)


class BinaryStrategyPayload(StrictSchema):
    """Lean Policy-2 binary discovery strategy (no metricGoals or unused fields)."""

    effect: EffectKind
    focus: FocusAreaPayload | None = None
    operators: list[EditableOpName] = Field(min_length=1, max_length=4)
    preserve: list[BinaryPreserveKey] = Field(default_factory=list, max_length=2)
    edit_budget: int = Field(alias="editBudget", ge=1, le=12)

    @field_validator("preserve", mode="before")
    @classmethod
    def _strip_platform_fixed_preserve(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [
                item
                for item in value
                if not (isinstance(item, str) and item in _PLATFORM_FIXED_PRESERVE)
            ]
        return value

    @model_validator(mode="after")
    def _validate_binary_strategy(self) -> BinaryStrategyPayload:
        if len(set(self.operators)) != len(self.operators):
            raise ValueError(f"operators must not contain duplicates; got {self.operators}.")
        if len(set(self.preserve)) != len(self.preserve):
            raise ValueError(f"preserve must not contain duplicates; got {self.preserve}.")
        usable = [op for op in self.operators if OPERATOR_COMPONENT.get(op) not in self.preserve]
        if not usable:
            raise ValueError(
                f"strategy preserves every component it asks to edit "
                f"(operators={self.operators}, preserve={self.preserve}); "
                "remove 'walls' or 'water' from preserve or change operators."
            )
        allowed_for_effect = EFFECT_OPERATORS[self.effect]
        if not (set(usable) & allowed_for_effect):
            raise ValueError(
                f"usable operators {usable} cannot realize effect {self.effect!r}; "
                f"include at least one of {sorted(allowed_for_effect)}."
            )
        return self


class BinaryCorridorPayload(StrictSchema):
    row: int = Field(ge=2, le=9)
    column: int = Field(ge=2, le=11)
    axis: Literal["horizontal", "vertical"]


class ReduceObstaclesPromisePayload(StrictSchema):
    kind: Literal["reduce_obstacles"]
    component: Literal["water", "internal_walls", "obstacles"]
    focus: FocusAreaPayload | None = None


class WidenCorridorPromisePayload(StrictSchema):
    kind: Literal["widen_corridor"]
    component: Literal["water", "internal_walls", "obstacles"]
    corridor: BinaryCorridorPayload


class ReshapeWaterPromisePayload(StrictSchema):
    kind: Literal["reshape_water"]
    component: Literal["water"]
    focus: FocusAreaPayload | None = None


class ShorterSolutionPromisePayload(StrictSchema):
    kind: Literal["shorter_solution"]
    component: Literal["obstacles"]


BinaryPromisePayload = (
    ReduceObstaclesPromisePayload
    | WidenCorridorPromisePayload
    | ReshapeWaterPromisePayload
    | ShorterSolutionPromisePayload
)


def _normalize_promise_dict(promise: BinaryPromisePayload) -> dict[str, Any]:
    data = promise.model_dump(by_alias=True)
    data.setdefault("focus", None)
    data.setdefault("corridor", None)
    return data


class BinaryBranchPayload(StrictSchema):
    label: str = Field(min_length=1, max_length=240)
    strategy: BinaryStrategyPayload
    promise: BinaryPromisePayload

    @model_validator(mode="after")
    def _validate_branch(self) -> BinaryBranchPayload:
        if not self.label.strip():
            raise ValueError("branch.label must be a non-blank string.")
        usable = {
            op
            for op in self.strategy.operators
            if OPERATOR_COMPONENT.get(op) not in self.strategy.preserve
        }
        kind = self.promise.kind
        if kind in {"reduce_obstacles", "widen_corridor"}:
            comp = self.promise.component
            if comp == "internal_walls" and "remove_wall" not in usable:
                raise ValueError(
                    f"promise {kind}/internal_walls requires 'remove_wall' in usable "
                    f"strategy.operators (got usable={sorted(usable)})."
                )
            if comp == "water" and "remove_water" not in usable:
                raise ValueError(
                    f"promise {kind}/water requires 'remove_water' in usable "
                    f"strategy.operators (got usable={sorted(usable)})."
                )
            if comp == "obstacles" and not (usable & {"remove_wall", "remove_water"}):
                raise ValueError(
                    f"promise {kind}/obstacles requires 'remove_wall' or 'remove_water' in "
                    f"usable strategy.operators (got usable={sorted(usable)})."
                )
        elif kind == "reshape_water":
            if not {"add_water", "remove_water"}.issubset(usable):
                raise ValueError(
                    "promise reshape_water requires both 'add_water' and 'remove_water' in "
                    f"usable strategy.operators (got usable={sorted(usable)})."
                )
            if self.strategy.edit_budget < 2:
                raise ValueError(
                    "promise reshape_water requires strategy.editBudget >= 2 "
                    f"(got editBudget={self.strategy.edit_budget})."
                )
        return self


def _validate_binary_discovery_step(
    model: Any,
    *,
    is_initial: bool,
    can_ask: bool,
) -> None:
    action: str = model.action
    question: str = getattr(model, "question", "") if can_ask else ""
    note: str = model.note
    branches: list[BinaryBranchPayload] = getattr(model, "branches", []) if can_ask else []
    plan_strategies: list[BinaryStrategyPayload] = model.plan_strategies
    protected_entities: list[str] = model.protected_entities
    initial_requirements: RequirementRecordPayload | None = (
        getattr(model, "initial_requirements", None) if is_initial else None
    )

    if action == "ask":
        unused: list[str] = []
        if plan_strategies:
            unused.append("planStrategies (must be [])")
        if protected_entities:
            unused.append("protectedEntities (must be [])")
        if unused:
            raise ValueError(f"action='ask' forbids non-empty {', '.join(unused)}.")
        if not question.strip():
            raise ValueError("action='ask' requires a non-blank 'question'.")
        if len(branches) != 2:
            raise ValueError(f"action='ask' requires exactly 2 branches, got {len(branches)}.")
        label_0 = " ".join(branches[0].label.split())
        label_1 = " ".join(branches[1].label.split())
        if not label_0 or not label_1:
            raise ValueError("action='ask' requires non-blank branch labels.")
        if label_0 == label_1:
            raise ValueError(
                "action='ask' requires 2 distinct branch labels, "
                f"but both branches have {label_0!r}."
            )
        prom_0 = _normalize_promise_dict(branches[0].promise)
        prom_1 = _normalize_promise_dict(branches[1].promise)
        if json.dumps(prom_0, sort_keys=True) == json.dumps(prom_1, sort_keys=True):
            raise ValueError(
                f"action='ask' requires 2 distinct branch promises, but both share {prom_0}."
            )
        if is_initial and initial_requirements is None:
            raise ValueError(
                "action='ask' on the initial turn requires non-null 'initialRequirements'."
            )
        return

    if action == "plan":
        unused = []
        if can_ask and question:
            unused.append("question (must be '')")
        if can_ask and branches:
            unused.append("branches (must be [])")
        if protected_entities:
            unused.append("protectedEntities (must be [])")
        if unused:
            raise ValueError(f"action='plan' forbids non-empty {', '.join(unused)}.")
        if is_initial and initial_requirements is None:
            raise ValueError(
                "action='plan' on the initial turn requires non-null 'initialRequirements'."
            )
        model.note = ""
        return

    if action == "protected_refusal":
        unused = []
        if can_ask and question:
            unused.append("question (must be '')")
        if can_ask and branches:
            unused.append("branches (must be [])")
        if plan_strategies:
            unused.append("planStrategies (must be [])")
        if unused:
            raise ValueError(
                f"action='protected_refusal' forbids non-empty {', '.join(unused)}."
            )
        if is_initial:
            model.initial_requirements = None
        if not protected_entities:
            raise ValueError(
                "action='protected_refusal' requires 1 to 4 unique items in 'protectedEntities'."
            )
        if len(set(protected_entities)) != len(protected_entities):
            raise ValueError(
                f"protectedEntities must not contain duplicates; got {protected_entities}."
            )
        model.note = ""
        return

    # action == "blocked"
    unused = []
    if can_ask and question:
        unused.append("question (must be '')")
    if can_ask and branches:
        unused.append("branches (must be [])")
    if plan_strategies:
        unused.append("planStrategies (must be [])")
    if protected_entities:
        unused.append("protectedEntities (must be [])")
    if unused:
        raise ValueError(f"action='blocked' forbids non-empty {', '.join(unused)}.")
    if is_initial:
        model.initial_requirements = None
    if not note.strip():
        raise ValueError(
            "action='blocked' requires a non-blank 'note' explaining why no editable path exists."
        )


class BinaryDiscoveryInitialPayload(StrictSchema):
    """Policy-2 binary discovery output on initial turn when asking is still allowed."""

    action: Literal["ask", "plan", "protected_refusal", "blocked"]
    question: str = Field(default="", max_length=600)
    note: str = Field(default="", max_length=600)
    branches: list[BinaryBranchPayload] = Field(default_factory=list, max_length=2)
    plan_strategies: list[BinaryStrategyPayload] = Field(
        default_factory=list, alias="planStrategies", max_length=3
    )
    protected_entities: list[ProtectedEntityKey] = Field(
        default_factory=list, alias="protectedEntities", max_length=4
    )
    initial_requirements: RequirementRecordPayload | None = Field(
        default=None, alias="initialRequirements"
    )

    @field_validator("plan_strategies", mode="before")
    @classmethod
    def _filter_plan_strategies(cls, value: Any) -> Any:
        return _filter_valid_backup_items(value, BinaryStrategyPayload)

    @model_validator(mode="after")
    def _validate_step(self) -> BinaryDiscoveryInitialPayload:
        _validate_binary_discovery_step(self, is_initial=True, can_ask=True)
        return self


class BinaryDiscoveryFollowupPayload(StrictSchema):
    """Policy-2 binary discovery output on follow-up turns (initialRequirements omitted)."""

    action: Literal["ask", "plan", "protected_refusal", "blocked"]
    question: str = Field(default="", max_length=600)
    note: str = Field(default="", max_length=600)
    branches: list[BinaryBranchPayload] = Field(default_factory=list, max_length=2)
    plan_strategies: list[BinaryStrategyPayload] = Field(
        default_factory=list, alias="planStrategies", max_length=3
    )
    protected_entities: list[ProtectedEntityKey] = Field(
        default_factory=list, alias="protectedEntities", max_length=4
    )

    @field_validator("plan_strategies", mode="before")
    @classmethod
    def _filter_plan_strategies(cls, value: Any) -> Any:
        return _filter_valid_backup_items(value, BinaryStrategyPayload)

    @model_validator(mode="after")
    def _validate_step(self) -> BinaryDiscoveryFollowupPayload:
        _validate_binary_discovery_step(self, is_initial=False, can_ask=True)
        return self


class BinaryDiscoveryInitialFinalPayload(StrictSchema):
    """Policy-2 binary discovery output on initial turn when question limit is reached."""

    action: Literal["plan", "protected_refusal", "blocked"]
    note: str = Field(default="", max_length=600)
    plan_strategies: list[BinaryStrategyPayload] = Field(
        default_factory=list, alias="planStrategies", max_length=3
    )
    protected_entities: list[ProtectedEntityKey] = Field(
        default_factory=list, alias="protectedEntities", max_length=4
    )
    initial_requirements: RequirementRecordPayload | None = Field(
        default=None, alias="initialRequirements"
    )

    @field_validator("plan_strategies", mode="before")
    @classmethod
    def _filter_plan_strategies(cls, value: Any) -> Any:
        return _filter_valid_backup_items(value, BinaryStrategyPayload)

    @model_validator(mode="after")
    def _validate_step(self) -> BinaryDiscoveryInitialFinalPayload:
        _validate_binary_discovery_step(self, is_initial=True, can_ask=False)
        return self


class BinaryDiscoveryFollowupFinalPayload(StrictSchema):
    """Policy-2 binary discovery output on final follow-up turn (ask/initialReqs omitted)."""

    action: Literal["plan", "protected_refusal", "blocked"]
    note: str = Field(default="", max_length=600)
    plan_strategies: list[BinaryStrategyPayload] = Field(
        default_factory=list, alias="planStrategies", max_length=3
    )
    protected_entities: list[ProtectedEntityKey] = Field(
        default_factory=list, alias="protectedEntities", max_length=4
    )

    @field_validator("plan_strategies", mode="before")
    @classmethod
    def _filter_plan_strategies(cls, value: Any) -> Any:
        return _filter_valid_backup_items(value, BinaryStrategyPayload)

    @model_validator(mode="after")
    def _validate_step(self) -> BinaryDiscoveryFollowupFinalPayload:
        _validate_binary_discovery_step(self, is_initial=False, can_ask=False)
        return self


BinaryDiscoveryPayload = BinaryDiscoveryInitialPayload


def select_binary_discovery_schema(
    *,
    needs_initial_requirements: bool,
    can_ask: bool,
) -> type[StrictSchema]:
    """Select the tightest context-scoped BinaryDiscovery schema for the current turn."""
    if needs_initial_requirements and can_ask:
        return BinaryDiscoveryInitialPayload
    if not needs_initial_requirements and can_ask:
        return BinaryDiscoveryFollowupPayload
    if needs_initial_requirements and not can_ask:
        return BinaryDiscoveryInitialFinalPayload
    return BinaryDiscoveryFollowupFinalPayload


class ProposalFailureAnalysisPayload(StrictSchema):
    body: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _validate_body(self) -> ProposalFailureAnalysisPayload:
        stripped = self.body.strip()
        if not stripped:
            raise ValueError("body must be a non-blank string.")
        self.body = stripped
        return self


__all__ = [
    "AutomaticBindingPayload",
    "BinaryBranchPayload",
    "BinaryCorridorPayload",
    "BinaryDiscoveryFollowupFinalPayload",
    "BinaryDiscoveryFollowupPayload",
    "BinaryDiscoveryInitialFinalPayload",
    "BinaryDiscoveryInitialPayload",
    "BinaryDiscoveryPayload",
    "BinaryPreserveKey",
    "BinaryPromisePayload",
    "BinaryStrategyPayload",
    "EntityId",
    "ExactTransitionPayload",
    "FocusAreaPayload",
    "MapOperationCandidatePayload",
    "MapOperationItemPayload",
    "MapOperationsPayload",
    "MetricGoalPayload",
    "ProposalFailureAnalysisPayload",
    "ReduceObstaclesPromisePayload",
    "RequiredTransitionPayload",
    "RequirementItemPayload",
    "RequirementRecordPayload",
    "ReshapeWaterPromisePayload",
    "RevisionPlanPayload",
    "RevisionStrategyPayload",
    "ShorterSolutionPromisePayload",
    "WidenCorridorPromisePayload",
    "select_binary_discovery_schema",
]
