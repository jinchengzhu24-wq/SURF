"""RevisionPlan parsing, map shell detection, and map-level plan validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from server.domain.blueprints import GRID_HEIGHT, GRID_WIDTH

from .models import (
    ALLOWED_EFFECTS,
    EDITABLE_OPERATORS,
    EFFECT_OPERATORS,
    MAX_EDIT_BUDGET,
    METRIC_DIRECTIONS,
    OPERATOR_COMPONENT,
    PRESERVE_COMPONENTS,
    SEARCH_METRICS,
    Focus,
    MetricGoal,
    RevisionPlan,
    RevisionPlanError,
    RevisionStrategy,
)

_REQUIRED_TRANSITION_OPERATOR: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    (".", "#"): ("add_wall", ("add_wall",)),
    ("#", "."): ("remove_wall", ("remove_wall",)),
    (".", "@"): ("add_water", ("add_water",)),
    ("@", "."): ("remove_water", ("remove_water",)),
    ("@", "#"): ("water_to_wall", ("remove_water", "add_wall")),
    ("#", "@"): ("wall_to_water", ("remove_wall", "add_water")),
}


def parse_revision_plan(payload: Any) -> RevisionPlan:
    if not isinstance(payload, Mapping) or set(payload) != {"strategies"}:
        got = sorted(payload.keys()) if isinstance(payload, Mapping) else type(payload).__name__
        raise RevisionPlanError(
            f"RevisionPlan must be an object containing only 'strategies'; got {got}."
        )
    raw_strategies = payload["strategies"]
    if not isinstance(raw_strategies, list) or not 1 <= len(raw_strategies) <= 3:
        got_len = (
            len(raw_strategies)
            if isinstance(raw_strategies, list)
            else type(raw_strategies).__name__
        )
        raise RevisionPlanError(
            f"strategies must be a list of 1 to 3 items; got {got_len}."
        )
    return RevisionPlan(
        tuple(_parse_strategy(item, index) for index, item in enumerate(raw_strategies, start=1))
    )


def _parse_focus(payload: Any, strategy_index: int) -> Focus | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping) or set(payload) != {"row", "column", "radius"}:
        got = sorted(payload.keys()) if isinstance(payload, Mapping) else type(payload).__name__
        raise RevisionPlanError(
            f"strategy {strategy_index} focus must have keys ['column', 'radius', 'row']; "
            f"got {got}."
        )
    row, column, radius = payload["row"], payload["column"], payload["radius"]
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (row, column, radius)):
        raise RevisionPlanError(
            f"strategy {strategy_index} focus values "
            f"(row={row!r}, column={column!r}, radius={radius!r}) must be integers."
        )
    if not 1 <= row <= GRID_HEIGHT or not 1 <= column <= GRID_WIDTH or not 1 <= radius <= 3:
        raise RevisionPlanError(
            f"strategy {strategy_index} focus (row={row}, column={column}, radius={radius}) "
            f"is outside bounds row 1..{GRID_HEIGHT}, column 1..{GRID_WIDTH}, radius 1..3."
        )
    return Focus(row, column, radius)


def _parse_required_transitions(
    payload: Any,
    strategy_index: int,
) -> tuple[tuple[int, int, str, str], ...]:
    if not isinstance(payload, list) or len(payload) > 12:
        raise RevisionPlanError(
            f"strategy {strategy_index} requiredTransitions must be a list of at most 12 items."
        )
    parsed: list[tuple[int, int, str, str]] = []
    seen: set[tuple[int, int]] = set()
    allowed_tiles = {"#", ".", "@"}
    for t_index, transition in enumerate(payload, start=1):
        if (
            not isinstance(transition, Mapping)
            or not {"row", "column", "from", "to"}.issubset(transition)
            or set(transition) - {"row", "column", "from", "to", "anchorEntity"}
        ):
            got = (
                sorted(transition.keys())
                if isinstance(transition, Mapping)
                else type(transition).__name__
            )
            raise RevisionPlanError(
                f"strategy {strategy_index} requiredTransitions[{t_index}] must have keys "
                f"['column', 'from', 'row', 'to']; got {got}."
            )
        row, column = transition["row"], transition["column"]
        before, after = transition["from"], transition["to"]
        if (
            isinstance(row, bool)
            or isinstance(column, bool)
            or not isinstance(row, int)
            or not isinstance(column, int)
            or not 1 <= row <= GRID_HEIGHT
            or not 1 <= column <= GRID_WIDTH
        ):
            raise RevisionPlanError(
                f"strategy {strategy_index} requiredTransitions[{t_index}] has out-of-bounds "
                f"coordinates (row={row!r}, column={column!r}); expected row 1..{GRID_HEIGHT}, "
                f"column 1..{GRID_WIDTH}."
            )
        if before not in allowed_tiles or after not in allowed_tiles:
            raise RevisionPlanError(
                f"strategy {strategy_index} requiredTransitions[{t_index}] at ({row}, {column}) "
                f"uses non-editable tile(s) from={before!r}, to={after!r}; "
                "allowed tiles are ['.', '#', '@']."
            )
        if before == after:
            raise RevisionPlanError(
                f"strategy {strategy_index} requiredTransitions[{t_index}] at ({row}, {column}) "
                f"has identical from={before!r} and to={after!r}; 'from' and 'to' must differ."
            )
        key = (row, column)
        if key in seen:
            raise RevisionPlanError(
                f"strategy {strategy_index} requiredTransitions[{t_index}] "
                f"repeats coordinate {key}."
            )
        seen.add(key)
        parsed.append((row, column, before, after))
    return tuple(parsed)


def _parse_strategy(payload: Any, index: int) -> RevisionStrategy:
    required = {"effect", "focus", "operators", "preserve", "editBudget", "metricGoals"}
    optional = {"requiredTransitions", "anchorEntities", "playObjective", "displayReason"}
    if not isinstance(payload, Mapping):
        raise RevisionPlanError(f"strategy {index} must be a JSON object.")
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - (required | optional))
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise RevisionPlanError(f"strategy {index} fields invalid: {', '.join(details)}.")

    effect = payload["effect"]
    if effect not in ALLOWED_EFFECTS:
        raise RevisionPlanError(
            f"strategy {index} has unsupported effect {effect!r}; "
            f"allowed effects: {sorted(ALLOWED_EFFECTS)}."
        )

    focus = _parse_focus(payload["focus"], index)
    operators = payload["operators"]
    if not isinstance(operators, list) or not 1 <= len(operators) <= 4:
        raise RevisionPlanError(
            f"strategy {index} operators must be a list of 1..4 items; got {operators!r}."
        )
    if len(set(operators)) != len(operators):
        raise RevisionPlanError(
            f"strategy {index} operators must not contain duplicates; got {operators!r}."
        )
    bad_ops = [op for op in operators if op not in EDITABLE_OPERATORS]
    if bad_ops:
        raise RevisionPlanError(
            f"strategy {index} operators contain invalid value(s) {bad_ops!r}; "
            f"allowed operators: {sorted(EDITABLE_OPERATORS)}."
        )

    preserve_raw = payload["preserve"]
    if not isinstance(preserve_raw, list):
        raise RevisionPlanError(f"strategy {index} preserve must be a list.")
    if len(set(preserve_raw)) != len(preserve_raw):
        raise RevisionPlanError(
            f"strategy {index} preserve must not contain duplicates; got {preserve_raw!r}."
        )
    bad_preserve = [c for c in preserve_raw if c not in PRESERVE_COMPONENTS]
    if bad_preserve:
        raise RevisionPlanError(
            f"strategy {index} preserve contains invalid value(s) {bad_preserve!r}; "
            f"allowed values: {sorted(PRESERVE_COMPONENTS)}."
        )
    preserve = frozenset(
        {*preserve_raw, "outer_shell", "player", "boxes", "targets", "unrelated_areas"}
    )

    usable_operators = tuple(
        op for op in operators if OPERATOR_COMPONENT.get(op) not in preserve
    )
    if not usable_operators:
        raise RevisionPlanError(
            f"strategy {index} preserves every component it asks to edit "
            f"(operators={operators}, preserve={preserve_raw})."
        )
    if not set(usable_operators).intersection(EFFECT_OPERATORS[effect]):
        raise RevisionPlanError(
            f"strategy {index} usable operators {list(usable_operators)} cannot realize effect "
            f"{effect!r}; include at least one of {sorted(EFFECT_OPERATORS[effect])}."
        )

    edit_budget = payload["editBudget"]
    if (
        isinstance(edit_budget, bool)
        or not isinstance(edit_budget, int)
        or not 1 <= edit_budget <= MAX_EDIT_BUDGET
    ):
        raise RevisionPlanError(
            f"strategy {index} editBudget must be an integer between 1 and {MAX_EDIT_BUDGET}; "
            f"got {edit_budget!r}."
        )

    raw_goals = payload["metricGoals"]
    if not isinstance(raw_goals, list) or len(raw_goals) > 3:
        raise RevisionPlanError(
            f"strategy {index} metricGoals must be a list of at most 3 items."
        )
    parsed_goals: list[MetricGoal] = []
    seen_metrics: set[str] = set()
    for g_index, goal in enumerate(raw_goals, start=1):
        if (
            not isinstance(goal, Mapping)
            or not {"metric", "direction"}.issubset(goal)
            or set(goal) - {"metric", "direction", "minimumDelta"}
        ):
            raise RevisionPlanError(
                f"strategy {index} metricGoals[{g_index}] must contain 'metric', 'direction', "
                "and optional 'minimumDelta'."
            )
        if goal["metric"] not in SEARCH_METRICS:
            raise RevisionPlanError(
                f"strategy {index} metricGoals[{g_index}] has unsupported metric "
                f"{goal['metric']!r}; allowed metrics: {sorted(SEARCH_METRICS)}."
            )
        if goal["direction"] not in METRIC_DIRECTIONS:
            raise RevisionPlanError(
                f"strategy {index} metricGoals[{g_index}] has unsupported direction "
                f"{goal['direction']!r}; allowed directions: {sorted(METRIC_DIRECTIONS)}."
            )
        if goal["metric"] in seen_metrics:
            raise RevisionPlanError(
                f"strategy {index} metricGoals[{g_index}] repeats metric {goal['metric']!r}."
            )
        seen_metrics.add(goal["metric"])
        minimum_delta = goal.get("minimumDelta", 1)
        if (
            isinstance(minimum_delta, bool)
            or not isinstance(minimum_delta, int)
            or not 1 <= minimum_delta <= 1000
        ):
            raise RevisionPlanError(
                f"strategy {index} metricGoals[{g_index}] minimumDelta must be an integer "
                f"in 1..1000; got {minimum_delta!r}."
            )
        parsed_goals.append(MetricGoal(goal["metric"], goal["direction"], minimum_delta))

    required_transitions = _parse_required_transitions(
        payload.get("requiredTransitions", []), index
    )
    raw_anchors = payload.get("anchorEntities", [])
    allowed_anchors = {"P", "B1", "B2", "T1", "T2"}
    if not isinstance(raw_anchors, list) or len(raw_anchors) > 5:
        raise RevisionPlanError(
            f"strategy {index} anchorEntities must be a list of at most 5 items."
        )
    if len(set(raw_anchors)) != len(raw_anchors):
        raise RevisionPlanError(
            f"strategy {index} anchorEntities must not contain duplicates; got {raw_anchors!r}."
        )
    bad_anchors = [a for a in raw_anchors if not isinstance(a, str) or a not in allowed_anchors]
    if bad_anchors:
        raise RevisionPlanError(
            f"strategy {index} anchorEntities contains invalid value(s) {bad_anchors!r}; "
            f"allowed values: {sorted(allowed_anchors)}."
        )

    raw_obj = payload.get("playObjective")
    if raw_obj is not None:
        if not isinstance(raw_obj, str):
            raise RevisionPlanError(
                f"strategy {index} playObjective must be a string or null."
            )
        if "\n" in raw_obj or "\r" in raw_obj:
            raise RevisionPlanError(
                f"strategy {index} playObjective must be a single line without newlines."
            )
        if not raw_obj.strip():
            raise RevisionPlanError(
                f"strategy {index} playObjective must be non-blank or null."
            )
        if len(raw_obj) > 120:
            raise RevisionPlanError(
                f"strategy {index} playObjective exceeds 120 characters ({len(raw_obj)})."
            )
        play_objective: str | None = raw_obj.strip()
    else:
        play_objective = None

    raw_reason = payload.get("displayReason")
    display_reason = (
        re.sub(r"\s+", " ", raw_reason).strip()[:600] or None
        if isinstance(raw_reason, str)
        else None
    )

    return RevisionStrategy(
        effect=effect,
        focus=focus,
        operators=usable_operators,
        preserve=preserve,
        edit_budget=edit_budget,
        metric_goals=tuple(parsed_goals),
        required_transitions=required_transitions,
        anchor_entities=tuple(raw_anchors),
        play_objective=play_objective,
        display_reason=display_reason,
    )


def connected_outer_shell(rows: Sequence[str]) -> set[tuple[int, int]]:
    """Flood-fill exterior void and border walls, marking visited on push to prevent re-visiting."""
    height = len(rows)
    if height == 0:
        return set()

    void_pending: deque[tuple[int, int]] = deque()
    exterior_void: set[tuple[int, int]] = set()
    for y, row in enumerate(rows):
        row_width = len(row)
        for x, tile in enumerate(row):
            if tile == " " and (x in {0, row_width - 1} or y in {0, height - 1}):
                pos = (x, y)
                if pos not in exterior_void:
                    exterior_void.add(pos)
                    void_pending.append(pos)

    while void_pending:
        x, y = void_pending.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if (
                0 <= ny < height
                and 0 <= nx < len(rows[ny])
                and (nx, ny) not in exterior_void
                and rows[ny][nx] == " "
            ):
                exterior_void.add((nx, ny))
                void_pending.append((nx, ny))

    pending: deque[tuple[int, int]] = deque()
    visited: set[tuple[int, int]] = set()
    for y, row in enumerate(rows):
        row_width = len(row)
        for x, tile in enumerate(row):
            if tile != "#":
                continue
            on_edge = x in {0, row_width - 1} or y in {0, height - 1}
            touches_void = any(
                (nx, ny) in exterior_void
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
            )
            if (on_edge or touches_void) and (x, y) not in visited:
                visited.add((x, y))
                pending.append((x, y))

    while pending:
        x, y = pending.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if (
                0 <= ny < height
                and 0 <= nx < len(rows[ny])
                and (nx, ny) not in visited
                and rows[ny][nx] == "#"
            ):
                visited.add((nx, ny))
                pending.append((nx, ny))
    return visited


def _inside_focus(focus: Focus | None, x: int, y: int) -> bool:
    if focus is None:
        return True
    return max(abs((focus.column - 1) - x), abs((focus.row - 1) - y)) <= focus.radius


def _focus_distance(focus: Focus | None, x: int, y: int) -> int:
    if focus is None:
        return 0
    return abs((focus.column - 1) - x) + abs((focus.row - 1) - y)


def validate_revision_plan_against_map(base_rows: Sequence[str], plan: RevisionPlan) -> bool:
    rows = tuple(base_rows)
    if len(rows) != GRID_HEIGHT or any(len(row) != GRID_WIDTH for row in rows):
        raise RevisionPlanError("The base Stage must contain 10 rows of 12 columns.")
    shell = connected_outer_shell(rows)
    for index, strategy in enumerate(plan.strategies, start=1):
        minimum = max(1, len(strategy.required_transitions))
        if minimum > strategy.edit_budget:
            raise RevisionPlanError(
                f"strategy {index} has an impossible edit range: {minimum}..{strategy.edit_budget}."
            )
        for row, column, before, after in strategy.required_transitions:
            x, y = column - 1, row - 1
            if rows[y][x] != before:
                raise RevisionPlanError(
                    f"strategy {index} requires row {row}, column {column} to be {before!r}, "
                    f"but the saved Stage contains {rows[y][x]!r}."
                )
            if (x, y) in shell or before == " " or after == " ":
                raise RevisionPlanError(
                    f"strategy {index} requires an invalid shell or void transition."
                )
            mapped = _REQUIRED_TRANSITION_OPERATOR.get((before, after))
            if mapped is None or not set(mapped[1]).issubset(strategy.operators):
                raise RevisionPlanError(
                    f"strategy {index} cannot realize required transition {before!r}->{after!r}."
                )
            for op in mapped[1]:
                component = OPERATOR_COMPONENT.get(op)
                if component and component in strategy.preserve:
                    raise RevisionPlanError(
                        f"strategy {index} preserves {component} but edits it."
                    )
            if strategy.focus and not _inside_focus(strategy.focus, x, y):
                raise RevisionPlanError(
                    f"strategy {index} required transition is outside its focus."
                )
    return True


def _map_fingerprint(rows: Sequence[str]) -> str:
    canonical = json.dumps(list(rows or ()), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()
