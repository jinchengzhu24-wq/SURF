"""Primitive generation, state construction, and beam selection for revision search."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any

from .models import (
    BEAM_WIDTH,
    EFFECT_OPERATORS,
    MAX_EDIT_BUDGET,
    OPERATOR_COMPONENT,
    Primitive,
    RevisionStrategy,
    SearchState,
)
from .plan_parser import (
    _REQUIRED_TRANSITION_OPERATOR,
    _focus_distance,
    _inside_focus,
    connected_outer_shell,
)


def _required_transition_primitive(
    base_rows: Sequence[str],
    transition: tuple[int, int, str, str],
    allowed_operators: set[str],
    effective_preserved: set[str],
    shell: set[tuple[int, int]],
) -> Primitive | None:
    row, column, before, after = transition
    x, y = column - 1, row - 1
    if not (0 <= y < len(base_rows) and 0 <= x < len(base_rows[y])):
        return None
    if base_rows[y][x] != before or (before == "#" and (x, y) in shell):
        return None
    mapped = _REQUIRED_TRANSITION_OPERATOR.get((before, after))
    if mapped is None:
        return None
    op_name, required_ops = mapped
    if not set(required_ops).issubset(allowed_operators):
        return None
    if any(OPERATOR_COMPONENT.get(op) in effective_preserved for op in required_ops):
        return None
    return Primitive(op_name, ((x, y, before, after),), True, 0)


def _state_from_primitives(
    base_rows: tuple[str, ...],
    primitives: tuple[Primitive, ...],
    edit_budget: int,
) -> SearchState | None:
    changes: dict[tuple[int, int], str] = {}
    for primitive in primitives:
        for x, y, before, after in primitive.changes:
            if (x, y) in changes or base_rows[y][x] != before or before == after:
                return None
            changes[(x, y)] = after
    if not changes or len(changes) > edit_budget or len(changes) > MAX_EDIT_BUDGET:
        return None
    mutable = [list(row) for row in base_rows]
    for (x, y), after in changes.items():
        mutable[y][x] = after
    return SearchState(primitives, tuple("".join(row) for row in mutable))


def _generate_primitives(
    rows: tuple[str, ...],
    strategy: RevisionStrategy,
    baseline_metrics: Mapping[str, Any],
    preserved_components: Sequence[str] | None,
) -> list[Primitive]:
    shell = connected_outer_shell(rows)
    trace_cells = _solution_trace_cells(rows, baseline_metrics.get("solution"))
    internal_walls = [
        (x, y)
        for y, row in enumerate(rows)
        for x, tile in enumerate(row)
        if tile == "#" and (x, y) not in shell
    ]
    water = [(x, y) for y, row in enumerate(rows) for x, v in enumerate(row) if v == "@"]
    entity_anchors = [
        (x, y) for y, row in enumerate(rows) for x, v in enumerate(row) if v in {"p", "s", "t"}
    ]
    primitives: list[Primitive] = []
    allowed = set(strategy.operators)
    preserved = set(preserved_components or ()) | set(strategy.preserve)

    def _inside_local(anchors: Sequence[tuple[int, int]], x: int, y: int) -> bool:
        if strategy.focus is not None or not anchors:
            return True
        return any(max(abs(ax - x), abs(ay - y)) <= 3 for ax, ay in anchors)

    def _cells(
        tiles: set[str],
        anchors: Sequence[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int]]:
        found = [
            (x, y)
            for y, row in enumerate(rows)
            for x, tile in enumerate(row)
            if tile in tiles
            and _inside_focus(strategy.focus, x, y)
            and _inside_local(anchors or (), x, y)
        ]
        found.sort(key=lambda pos: (_focus_distance(strategy.focus, *pos), pos[1], pos[0]))
        return found

    if {"remove_water", "add_wall"}.issubset(allowed) and not {"water", "walls"} & preserved:
        for x, y in _cells({"@"}):
            primitives.append(
                Primitive(
                    "water_to_wall",
                    ((x, y, "@", "#"),),
                    True,
                    _focus_distance(strategy.focus, x, y),
                )
            )
    if {"remove_wall", "add_water"}.issubset(allowed) and not {"water", "walls"} & preserved:
        for x, y in _cells({"#"}):
            if (x, y) not in shell:
                primitives.append(
                    Primitive(
                        "wall_to_water",
                        ((x, y, "#", "@"),),
                        True,
                        _focus_distance(strategy.focus, x, y),
                    )
                )

    for operator in strategy.operators:
        if OPERATOR_COMPONENT.get(operator) in preserved:
            continue
        if operator in {"add_wall", "add_water"}:
            after = "#" if operator == "add_wall" else "@"
            anchors = (
                [*internal_walls, *entity_anchors, *water]
                if operator == "add_wall"
                else [*water, *entity_anchors]
            )
            for x, y in _cells({"."}, anchors):
                primitives.append(
                    Primitive(
                        operator,
                        ((x, y, ".", after),),
                        (x, y) not in trace_cells,
                        _focus_distance(strategy.focus, x, y),
                    )
                )
        elif operator == "remove_wall":
            for x, y in _cells({"#"}):
                if (x, y) not in shell:
                    primitives.append(
                        Primitive(
                            operator,
                            ((x, y, "#", "."),),
                            True,
                            _focus_distance(strategy.focus, x, y),
                        )
                    )
        elif operator == "remove_water":
            for x, y in _cells({"@"}):
                primitives.append(
                    Primitive(
                        operator,
                        ((x, y, "@", "."),),
                        True,
                        _focus_distance(strategy.focus, x, y),
                    )
                )

    preferred = EFFECT_OPERATORS[strategy.effect]
    primitives.sort(
        key=lambda item: (
            item.operator not in preferred,
            not item.preserves_solution,
            item.distance,
            len(item.changes),
            item.operator,
            item.changes,
        )
    )
    if strategy.required_transitions or strategy.edit_budget < 3:
        return primitives

    grouped: list[Primitive] = []
    for component in ("water", "wall"):
        comp_key = "walls" if component == "wall" else "water"
        if comp_key in preserved:
            continue
        removals = [p for p in primitives if p.operator == f"remove_{component}"][:4]
        additions = [p for p in primitives if p.operator == f"add_{component}"][:6]
        for removal in removals:
            for pair in combinations(additions, 2):
                group = (removal, *pair)
                grouped.append(
                    Primitive(
                        f"compensate_{component}",
                        tuple(ch for p in group for ch in p.changes),
                        all(p.preserves_solution for p in group),
                        sum(p.distance for p in group),
                    )
                )
                if sum(p.operator == f"compensate_{component}" for p in grouped) >= 4:
                    break
            if sum(p.operator == f"compensate_{component}" for p in grouped) >= 4:
                break
    return grouped + primitives


def _solution_trace_cells(rows: Sequence[str], solution: Any) -> set[tuple[int, int]]:
    if not isinstance(solution, str) or not solution:
        return set()
    players = [(x, y) for y, r in enumerate(rows) for x, v in enumerate(r) if v == "p"]
    if len(players) != 1:
        return set()
    player = players[0]
    boxes = {(x, y) for y, r in enumerate(rows) for x, v in enumerate(r) if v == "s"}
    traced = {player, *boxes}
    directions = {"U": (0, -1), "D": (0, 1), "L": (-1, 0), "R": (1, 0)}
    for move in solution:
        if move not in directions:
            return set()
        dx, dy = directions[move]
        dest = (player[0] + dx, player[1] + dy)
        traced.add(dest)
        if dest in boxes:
            box_dest = (dest[0] + dx, dest[1] + dy)
            boxes.remove(dest)
            boxes.add(box_dest)
            traced.add(box_dest)
        player = dest
    return traced


def _select_beam(states: Sequence[SearchState]) -> list[SearchState]:
    unique: dict[tuple[str, ...], SearchState] = {}
    for state in states:
        unique.setdefault(state.rows, state)
    ordered = sorted(
        unique.values(),
        key=lambda state: (
            not all(item.preserves_solution for item in state.primitives),
            sum(item.distance for item in state.primitives),
            sum(len(item.changes) for item in state.primitives),
            tuple((item.operator, item.changes) for item in state.primitives),
            state.rows,
        ),
    )
    compound = [
        state
        for state in ordered
        if any(item.operator.startswith("compensate_") for item in state.primitives)
    ][:4]
    return compound + [s for s in ordered if s not in compound][: BEAM_WIDTH - len(compound)]
