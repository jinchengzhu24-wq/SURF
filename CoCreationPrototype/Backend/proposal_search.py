from collections import deque
from dataclasses import dataclass
import time


WIDTH = 12
HEIGHT = 10
BEAM_WIDTH = 16
BEAM_DEPTH = 3
PRIMITIVE_LIMIT = 32
MAX_CONSTRUCTED_CANDIDATES = 64
MAX_VALID_CANDIDATES = 8
# A revision is a meaningful but bounded local experiment.  The operation
# agent uses this same upper bound for its machine-checkable edit contract.
MAX_EDIT_BUDGET = 12

EFFECTS = {
    "open_route",
    "narrow_route",
    "adjust_internal_walls",
    "relocate_start",
    "relocate_box",
    "relocate_target",
    "reshape_water",
    "change_box_order",
}
OPERATORS = {
    "add_wall",
    "remove_wall",
    "move_player",
    "move_box",
    "move_target",
    "add_water",
    "remove_water",
}
PRESERVE_COMPONENTS = {
    "outer_shell",
    "player",
    "boxes",
    "targets",
    "water",
    "walls",
    "unrelated_areas",
}
METRICS = {"solutionSteps", "solutionPushes", "searchedStates"}
METRIC_DIRECTIONS = {"increase", "decrease", "preserve"}

EFFECT_OPERATORS = {
    "open_route": {"remove_wall", "remove_water"},
    "narrow_route": {"add_wall", "add_water"},
    "adjust_internal_walls": {"add_wall", "remove_wall"},
    "relocate_start": {"move_player"},
    "relocate_box": {"move_box"},
    "relocate_target": {"move_target"},
    "reshape_water": {"add_water", "remove_water"},
    "change_box_order": {"move_box", "move_target", "add_wall", "remove_wall"},
}
OPERATOR_COMPONENT = {
    "move_player": "player",
    "move_box": "boxes",
    "move_target": "targets",
    "add_water": "water",
    "remove_water": "water",
}


class RevisionPlanError(ValueError):
    pass


class ProposalSearchExhausted(ValueError):
    def __init__(self, diagnostics):
        super().__init__("Deterministic search found no solvable map satisfying the revision plan.")
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class Focus:
    row: int
    column: int
    radius: int


@dataclass(frozen=True)
class MetricGoal:
    metric: str
    direction: str


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

    def as_dict(self):
        return {
            "effect": self.effect,
            "focus": (
                {
                    "row": self.focus.row,
                    "column": self.focus.column,
                    "radius": self.focus.radius,
                }
                if self.focus is not None
                else None
            ),
            "operators": list(self.operators),
            "preserve": sorted(self.preserve),
            "editBudget": self.edit_budget,
            "metricGoals": [
                {"metric": goal.metric, "direction": goal.direction}
                for goal in self.metric_goals
            ],
            "requiredTransitions": [
                {
                    "row": row,
                    "column": column,
                    "from": before,
                    "to": after,
                }
                for row, column, before, after in self.required_transitions
            ],
            "anchorEntities": list(self.anchor_entities),
            "playObjective": self.play_objective,
        }


@dataclass(frozen=True)
class RevisionPlan:
    strategies: tuple[RevisionStrategy, ...]

    def as_dict(self):
        return {"strategies": [strategy.as_dict() for strategy in self.strategies]}


@dataclass(frozen=True)
class Primitive:
    operator: str
    changes: tuple[tuple[int, int, str, str], ...]
    preserves_solution: bool
    distance: int

    @property
    def positions(self):
        return frozenset((x, y) for x, y, _, _ in self.changes)


@dataclass(frozen=True)
class SearchState:
    primitives: tuple[Primitive, ...]
    rows: tuple[str, ...]

    @property
    def operators(self):
        operators = set()
        for primitive in self.primitives:
            if primitive.operator == "water_to_wall":
                # This is an internal atomic realization of the two public
                # semantic operators.  It exists so a reviewable request to
                # seal a water tile as a wall does not have to expose an
                # invalid intermediate floor map to the search.
                operators.update({"remove_water", "add_wall"})
            else:
                operators.add(primitive.operator)
        return frozenset(operators)


@dataclass(frozen=True)
class VerifiedCandidate:
    rows: tuple[str, ...]
    validation: object
    strategy_index: int
    operators: tuple[str, ...]
    metric_matches: int
    effect_match: int
    changed_cells: int

    @property
    def selection_key(self):
        return (
            -self.metric_matches,
            -self.effect_match,
            self.changed_cells,
            self.rows,
        )


@dataclass(frozen=True)
class ProposalSearchResult:
    rows: tuple[str, ...]
    validation: object
    strategy_index: int
    operators: tuple[str, ...]
    score: dict
    diagnostics: dict


def parse_revision_plan(payload):
    if not isinstance(payload, dict) or set(payload) != {"strategies"}:
        raise RevisionPlanError("RevisionPlan must contain only strategies.")
    raw_strategies = payload["strategies"]
    if not isinstance(raw_strategies, list) or not 1 <= len(raw_strategies) <= 3:
        raise RevisionPlanError("strategies must contain one to three items.")
    return RevisionPlan(tuple(
        _parse_strategy(item, index)
        for index, item in enumerate(raw_strategies, start=1)
    ))


def _parse_strategy(payload, index):
    required = {"effect", "focus", "operators", "preserve", "editBudget", "metricGoals"}
    optional = {"requiredTransitions", "anchorEntities", "playObjective"}
    if not isinstance(payload, dict) or not required.issubset(payload) or not set(payload).issubset(required | optional):
        raise RevisionPlanError(f"strategy {index} does not match the required fields.")
    effect = payload["effect"]
    if effect not in EFFECTS:
        raise RevisionPlanError(f"strategy {index} has an unsupported effect.")
    focus = _parse_focus(payload["focus"], index)
    operators = payload["operators"]
    if (
        not isinstance(operators, list)
        or not 1 <= len(operators) <= 3
        or len(set(operators)) != len(operators)
        or any(operator not in OPERATORS for operator in operators)
    ):
        raise RevisionPlanError(f"strategy {index} operators are invalid.")
    preserve = payload["preserve"]
    if (
        not isinstance(preserve, list)
        or len(set(preserve)) != len(preserve)
        or any(component not in PRESERVE_COMPONENTS for component in preserve)
    ):
        raise RevisionPlanError(f"strategy {index} preserve values are invalid.")
    preserve = frozenset({*preserve, "outer_shell", "unrelated_areas"})
    usable_operators = tuple(
        operator
        for operator in operators
        if OPERATOR_COMPONENT.get(operator) not in preserve
    )
    if not usable_operators:
        raise RevisionPlanError(f"strategy {index} preserves every component it asks to edit.")
    if not set(usable_operators).intersection(EFFECT_OPERATORS[effect]):
        raise RevisionPlanError(
            f"strategy {index} operators cannot realize its declared effect."
        )
    edit_budget = payload["editBudget"]
    if isinstance(edit_budget, bool) or not isinstance(edit_budget, int) or not 1 <= edit_budget <= MAX_EDIT_BUDGET:
        raise RevisionPlanError(
            f"strategy {index} editBudget must be between 1 and {MAX_EDIT_BUDGET}."
        )
    metric_goals = payload["metricGoals"]
    if not isinstance(metric_goals, list) or len(metric_goals) > 3:
        raise RevisionPlanError(f"strategy {index} metricGoals are invalid.")
    parsed_goals = []
    seen_metrics = set()
    for goal in metric_goals:
        if not isinstance(goal, dict) or set(goal) != {"metric", "direction"}:
            raise RevisionPlanError(f"strategy {index} has an invalid metric goal.")
        if goal["metric"] not in METRICS or goal["direction"] not in METRIC_DIRECTIONS:
            raise RevisionPlanError(f"strategy {index} has an unsupported metric goal.")
        if goal["metric"] in seen_metrics:
            raise RevisionPlanError(f"strategy {index} repeats a metric goal.")
        seen_metrics.add(goal["metric"])
        parsed_goals.append(MetricGoal(goal["metric"], goal["direction"]))
    required_transitions = _parse_required_transitions(
        payload.get("requiredTransitions", []),
        index,
    )
    anchor_entities = _parse_anchor_entities(
        payload.get("anchorEntities", []),
        index,
    )
    play_objective = _parse_play_objective(payload.get("playObjective"), index)
    return RevisionStrategy(
        effect=effect,
        focus=focus,
        operators=usable_operators,
        preserve=preserve,
        edit_budget=edit_budget,
        metric_goals=tuple(parsed_goals),
        required_transitions=required_transitions,
        anchor_entities=anchor_entities,
        play_objective=play_objective,
    )


def _parse_required_transitions(payload, strategy_index):
    if not isinstance(payload, list) or len(payload) > 12:
        raise RevisionPlanError(f"strategy {strategy_index} requiredTransitions are invalid.")
    parsed = []
    seen = set()
    allowed_tiles = set("#.@pst")
    for transition in payload:
        if not isinstance(transition, dict) or set(transition) != {"row", "column", "from", "to"}:
            raise RevisionPlanError(f"strategy {strategy_index} has an invalid required transition.")
        row, column = transition["row"], transition["column"]
        before, after = transition["from"], transition["to"]
        if (
            isinstance(row, bool) or isinstance(column, bool)
            or not isinstance(row, int) or not isinstance(column, int)
            or not 1 <= row <= HEIGHT or not 1 <= column <= WIDTH
            or before not in allowed_tiles or after not in allowed_tiles
            or before == after
        ):
            raise RevisionPlanError(f"strategy {strategy_index} has an invalid required transition.")
        key = (row, column)
        if key in seen:
            raise RevisionPlanError(f"strategy {strategy_index} repeats a required transition coordinate.")
        seen.add(key)
        parsed.append((row, column, before, after))
    return tuple(parsed)


def _parse_anchor_entities(payload, strategy_index):
    allowed = {"P", "B1", "B2", "T1", "T2"}
    if (
        not isinstance(payload, list)
        or len(payload) > 5
        or any(not isinstance(entity, str) for entity in payload)
        or len(set(payload)) != len(payload)
        or any(entity not in allowed for entity in payload)
    ):
        raise RevisionPlanError(f"strategy {strategy_index} anchorEntities are invalid.")
    return tuple(payload)


def _parse_play_objective(payload, strategy_index):
    if payload is None:
        return None
    if not isinstance(payload, str) or not payload.strip() or len(payload) > 120:
        raise RevisionPlanError(f"strategy {strategy_index} playObjective is invalid.")
    if "\n" in payload or "\r" in payload:
        raise RevisionPlanError(f"strategy {strategy_index} playObjective is invalid.")
    return payload.strip()


def _parse_focus(payload, strategy_index):
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"row", "column", "radius"}:
        raise RevisionPlanError(f"strategy {strategy_index} focus is invalid.")
    row, column, radius = payload["row"], payload["column"], payload["radius"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (row, column, radius)):
        raise RevisionPlanError(f"strategy {strategy_index} focus values must be integers.")
    if not 1 <= row <= HEIGHT or not 1 <= column <= WIDTH or not 1 <= radius <= 3:
        raise RevisionPlanError(f"strategy {strategy_index} focus is outside the Stage.")
    return Focus(row, column, radius)


def validate_revision_plan_against_map(base_rows, plan):
    """Reject a semantically valid plan that cannot match the saved Stage."""
    rows = tuple(base_rows)
    if len(rows) != HEIGHT or any(len(row) != WIDTH for row in rows):
        raise RevisionPlanError("The base Stage must be a 12x10 map.")
    shell = _connected_outer_shell(rows)
    for index, strategy in enumerate(plan.strategies, start=1):
        minimum = _minimum_changed_cells(strategy)
        if minimum > strategy.edit_budget:
            raise RevisionPlanError(
                f"strategy {index} has an impossible edit range: {minimum}..{strategy.edit_budget}."
            )
        if len(strategy.required_transitions) > strategy.edit_budget:
            raise RevisionPlanError(
                f"strategy {index} requires more transitions than its edit budget."
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
            operator = _transition_operator(before, after)
            if operator is None or operator not in strategy.operators:
                raise RevisionPlanError(
                    f"strategy {index} cannot realize required transition {before!r}->{after!r}."
                )
            if operator in {"add_wall", "remove_wall"} and "walls" in strategy.preserve:
                raise RevisionPlanError(f"strategy {index} preserves walls but edits a wall.")
            component = OPERATOR_COMPONENT.get(operator)
            if component and component in strategy.preserve:
                raise RevisionPlanError(
                    f"strategy {index} preserves {component} but edits it."
                )
            if strategy.focus and not _inside_focus(strategy.focus, x, y):
                raise RevisionPlanError(
                    f"strategy {index} required transition is outside its focus."
                )
    return True


def _minimum_changed_cells(strategy):
    entity_operators = {"move_player", "move_box", "move_target"}
    if strategy.required_transitions:
        required_operators = {
            _transition_operator(before, after)
            for _, _, before, after in strategy.required_transitions
        }
        if required_operators.intersection(entity_operators):
            return max(2, len(strategy.required_transitions))
        return len(strategy.required_transitions)
    if strategy.effect in {"relocate_start", "relocate_box", "relocate_target"}:
        return 2
    if set(strategy.operators).intersection(entity_operators):
        return 2
    return 1


def _transition_operator(before, after):
    return {
        (".", "#"): "add_wall",
        ("#", "."): "remove_wall",
        ("p", "."): "move_player",
        (".", "p"): "move_player",
        ("s", "."): "move_box",
        (".", "s"): "move_box",
        ("t", "."): "move_target",
        (".", "t"): "move_target",
        (".", "@"): "add_water",
        ("@", "."): "remove_water",
    }.get((before, after))


def search_revision_plan(
    base_rows,
    plan,
    proposal_validator,
    baseline_metrics=None,
    deadline=None,
    movement_requirement=None,
    preserved_components=None,
    required_transitions=None,
):
    search_started_at = time.monotonic()
    base_rows = tuple(base_rows)
    baseline_metrics = baseline_metrics or {}
    deadline = deadline if deadline is not None else float("inf")
    diagnostics = {
        "strategies": len(plan.strategies),
        "constructedCandidates": 0,
        "staticRejectedCandidates": 0,
        "solverRejectedCandidates": 0,
        "solvedCandidates": 0,
        "validCandidates": 0,
        "deadlineReached": False,
        "failureReasons": {},
    }
    if movement_requirement:
        diagnostics["movementRequirement"] = dict(movement_requirement)
    if preserved_components:
        diagnostics["preservedComponents"] = sorted(set(preserved_components))
    if required_transitions:
        diagnostics["requiredTransitions"] = [
            {
                "row": row,
                "column": column,
                "from": before,
                "to": after,
            }
            for row, column, before, after in required_transitions
        ]
    verified = []
    seen_maps = {base_rows}

    for strategy_index, strategy in enumerate(plan.strategies, start=1):
        if _deadline_reached(deadline):
            diagnostics["deadlineReached"] = True
            break
        primitives = _generate_primitives(
            base_rows,
            strategy,
            baseline_metrics,
            movement_requirement,
            preserved_components,
        )
        beam = []
        for primitive in primitives[:PRIMITIVE_LIMIT]:
            state = _state_from_primitives(base_rows, (primitive,), strategy.edit_budget)
            if state is not None:
                beam.append(state)
        beam = _select_beam(beam)

        for depth in range(1, BEAM_DEPTH + 1):
            if not beam:
                break
            next_states = []
            for state in beam:
                if diagnostics["constructedCandidates"] >= MAX_CONSTRUCTED_CANDIDATES:
                    break
                if _deadline_reached(deadline):
                    diagnostics["deadlineReached"] = True
                    break
                if state.rows not in seen_maps:
                    seen_maps.add(state.rows)
                    diagnostics["constructedCandidates"] += 1
                    _evaluate_state(
                        state,
                        strategy,
                        strategy_index,
                        base_rows,
                        proposal_validator,
                        baseline_metrics,
                        diagnostics,
                        verified,
                        required_transitions=(
                            tuple(required_transitions)
                            if required_transitions is not None
                            else strategy.required_transitions
                        ),
                    )
                    if len(verified) >= MAX_VALID_CANDIDATES:
                        break
                if depth < BEAM_DEPTH:
                    used_positions = frozenset().union(*(
                        primitive.positions for primitive in state.primitives
                    ))
                    last_key = _primitive_key(state.primitives[-1])
                    for primitive in primitives[:PRIMITIVE_LIMIT]:
                        if _primitive_key(primitive) <= last_key or primitive.positions & used_positions:
                            continue
                        expanded = _state_from_primitives(
                            base_rows,
                            (*state.primitives, primitive),
                            strategy.edit_budget,
                        )
                        if expanded is not None:
                            next_states.append(expanded)
            if (
                diagnostics["constructedCandidates"] >= MAX_CONSTRUCTED_CANDIDATES
                or diagnostics["deadlineReached"]
                or len(verified) >= MAX_VALID_CANDIDATES
            ):
                break
            beam = _select_beam(next_states)
        if (
            diagnostics["constructedCandidates"] >= MAX_CONSTRUCTED_CANDIDATES
            or diagnostics["deadlineReached"]
            or len(verified) >= MAX_VALID_CANDIDATES
        ):
            break

    diagnostics["validCandidates"] = len(verified)
    diagnostics["elapsedMs"] = int((time.monotonic() - search_started_at) * 1000)
    if not verified:
        raise ProposalSearchExhausted(diagnostics)
    selected = min(verified, key=lambda candidate: candidate.selection_key)
    score = {
        "hardRequirementsSatisfied": True,
        "metricMatches": selected.metric_matches,
        "focusComponentMatch": selected.effect_match,
        "changedCells": selected.changed_cells,
    }
    diagnostics["selectedStrategyIndex"] = selected.strategy_index
    diagnostics["selectedOperators"] = list(selected.operators)
    diagnostics["selectionScore"] = score
    return ProposalSearchResult(
        rows=selected.rows,
        validation=selected.validation,
        strategy_index=selected.strategy_index,
        operators=selected.operators,
        score=score,
        diagnostics=diagnostics,
    )


def _generate_primitives(
    rows,
    strategy,
    baseline_metrics,
    movement_requirement=None,
    preserved_components=None,
):
    shell = _connected_outer_shell(rows)
    trace_cells = _solution_trace_cells(rows, baseline_metrics.get("solution"))
    player = _find_one(rows, "p")
    reachable = _player_reachable(rows, player)
    positions = {
        tile: [(x, y) for y, row in enumerate(rows) for x, value in enumerate(row) if value == tile]
        for tile in ("p", "s", "t")
    }
    internal_walls = [
        (x, y)
        for y, row in enumerate(rows)
        for x, tile in enumerate(row)
        if tile == "#" and (x, y) not in shell
    ]
    water = _find_all(rows, "@")
    entity_anchors = [*positions["p"], *positions["s"], *positions["t"]]
    primitives = []
    allowed_operators = set(strategy.operators)
    if (
        {"remove_water", "add_wall"}.issubset(allowed_operators)
        and "water" not in set(preserved_components or ())
        and "walls" not in set(preserved_components or ())
    ):
        # A water-to-wall request is a real one-cell replacement, not two
        # unrelated edits.  Generate it atomically from the base Stage so the
        # beam can preserve locality and edit-budget accounting.
        for x, y in _candidate_cells(rows, strategy.focus, {"@"}):
            primitives.append(Primitive(
                "water_to_wall",
                ((x, y, "@", "#"),),
                True,
                _focus_distance(strategy.focus, x, y),
            ))
    for operator in strategy.operators:
        if movement_requirement and operator != movement_requirement["operator"]:
            continue
        if _operator_changes_preserved_component(operator, preserved_components):
            continue
        if operator in {"add_wall", "add_water"}:
            after = "#" if operator == "add_wall" else "@"
            anchors = (
                [*internal_walls, *entity_anchors, *water]
                if operator == "add_wall"
                else [*water, *entity_anchors]
            )
            for x, y in _candidate_cells(rows, strategy.focus, {"."}, anchors):
                primitives.append(Primitive(
                    operator,
                    ((x, y, ".", after),),
                    (x, y) not in trace_cells,
                    _focus_distance(strategy.focus, x, y),
                ))
        elif operator == "remove_wall":
            for x, y in _candidate_cells(rows, strategy.focus, {"#"}):
                if (x, y) not in shell:
                    primitives.append(Primitive(
                        operator,
                        ((x, y, "#", "."),),
                        True,
                        _focus_distance(strategy.focus, x, y),
                    ))
        elif operator == "remove_water":
            for x, y in _candidate_cells(rows, strategy.focus, {"@"}):
                primitives.append(Primitive(
                    operator,
                    ((x, y, "@", "."),),
                    True,
                    _focus_distance(strategy.focus, x, y),
                ))
        elif operator == "move_player":
            source = positions["p"][0]
            for x, y in _candidate_cells(rows, strategy.focus, {"."}, [source]):
                if (
                    (x, y) in reachable
                    and _movement_destination_matches(
                        source,
                        (x, y),
                        movement_requirement,
                    )
                ):
                    primitives.append(Primitive(
                        operator,
                        ((source[0], source[1], "p", "."), (x, y, ".", "p")),
                        True,
                        _focus_distance(strategy.focus, x, y),
                    ))
        elif operator in {"move_box", "move_target"}:
            tile = "s" if operator == "move_box" else "t"
            for source in positions[tile]:
                for x, y in _candidate_cells(rows, strategy.focus, {"."}, [source]):
                    if not _movement_destination_matches(
                        source,
                        (x, y),
                        movement_requirement,
                    ):
                        continue
                    primitives.append(Primitive(
                        operator,
                        ((source[0], source[1], tile, "."), (x, y, ".", tile)),
                        False,
                        _focus_distance(strategy.focus, x, y),
                    ))
    preferred = EFFECT_OPERATORS[strategy.effect]
    primitives.sort(key=lambda item: (
        item.operator not in preferred,
        not item.preserves_solution,
        item.distance,
        len(item.changes),
        _primitive_key(item),
    ))
    return primitives


def _operator_changes_preserved_component(operator, preserved_components):
    preserved = set(preserved_components or ())
    component = OPERATOR_COMPONENT.get(operator)
    if component in preserved:
        return True
    return operator in {"add_wall", "remove_wall"} and "walls" in preserved


def _movement_destination_matches(source, destination, requirement):
    if not requirement:
        return True
    source_x, source_y = source
    destination_x, destination_y = destination
    direction = requirement["direction"]
    return {
        "right": destination_x > source_x,
        "left": destination_x < source_x,
        "up": destination_y < source_y,
        "down": destination_y > source_y,
        "upper_right": destination_x > source_x and destination_y < source_y,
        "upper_left": destination_x < source_x and destination_y < source_y,
        "lower_right": destination_x > source_x and destination_y > source_y,
        "lower_left": destination_x < source_x and destination_y > source_y,
    }.get(direction, False)


def _candidate_cells(rows, focus, tiles, anchors=None):
    cells = []
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if (
                tile in tiles
                and _inside_focus(focus, x, y)
                and _inside_default_local_area(focus, anchors, x, y)
            ):
                cells.append((x, y))
    cells.sort(key=lambda position: (_focus_distance(focus, *position), position[1], position[0]))
    return cells


def _inside_default_local_area(focus, anchors, x, y):
    if focus is not None or not anchors:
        return True
    return any(max(abs(anchor_x - x), abs(anchor_y - y)) <= 3 for anchor_x, anchor_y in anchors)


def _inside_focus(focus, x, y):
    if focus is None:
        return True
    return max(abs((focus.column - 1) - x), abs((focus.row - 1) - y)) <= focus.radius


def _focus_distance(focus, x, y):
    if focus is None:
        return 0
    return abs((focus.column - 1) - x) + abs((focus.row - 1) - y)


def _state_from_primitives(base_rows, primitives, edit_budget):
    changes = {}
    for primitive in primitives:
        for x, y, before, after in primitive.changes:
            if (x, y) in changes:
                return None
            if base_rows[y][x] != before or before == after:
                return None
            changes[(x, y)] = after
    if not changes or len(changes) > edit_budget or len(changes) > MAX_EDIT_BUDGET:
        return None
    mutable = [list(row) for row in base_rows]
    for (x, y), after in changes.items():
        mutable[y][x] = after
    return SearchState(tuple(primitives), tuple("".join(row) for row in mutable))


def _select_beam(states):
    unique = {}
    for state in states:
        unique.setdefault(state.rows, state)
    ordered = sorted(unique.values(), key=lambda state: (
        not all(item.preserves_solution for item in state.primitives),
        sum(item.distance for item in state.primitives),
        sum(len(item.changes) for item in state.primitives),
        tuple(_primitive_key(item) for item in state.primitives),
        state.rows,
    ))
    return ordered[:BEAM_WIDTH]


def _evaluate_state(
    state,
    strategy,
    strategy_index,
    base_rows,
    proposal_validator,
    baseline_metrics,
    diagnostics,
    verified,
    required_transitions=(),
):
    if not _state_realizes_required_transitions(
        state.rows,
        base_rows,
        required_transitions,
    ):
        diagnostics["staticRejectedCandidates"] += 1
        _count_failure(diagnostics, "required_transition_not_realized")
        return
    if not _state_realizes_effect(state, strategy):
        diagnostics["staticRejectedCandidates"] += 1
        _count_failure(diagnostics, "effect_not_realized")
        return
    if not _cheap_structure_valid(state.rows):
        diagnostics["staticRejectedCandidates"] += 1
        _count_failure(diagnostics, "static_structure")
        return
    try:
        validation = proposal_validator(list(state.rows))
        diagnostics["solvedCandidates"] += 1
    except Exception as exception:
        diagnostics["solverRejectedCandidates"] += 1
        _count_failure(diagnostics, _safe_failure_code(exception))
        return
    operators = tuple(sorted(state.operators))
    metric_matches = _metric_matches(strategy.metric_goals, baseline_metrics, validation)
    if metric_matches != len(strategy.metric_goals):
        _count_failure(diagnostics, "metric_goal_not_met")
        return
    effect_match = int(bool(state.operators & EFFECT_OPERATORS[strategy.effect]))
    changed_cells = sum(
        before != after
        for before_row, after_row in zip(base_rows, state.rows)
        for before, after in zip(before_row, after_row)
    )
    verified.append(VerifiedCandidate(
        rows=state.rows,
        validation=validation,
        strategy_index=strategy_index,
        operators=operators,
        metric_matches=metric_matches,
        effect_match=effect_match,
        changed_cells=changed_cells,
    ))


def _state_realizes_required_transitions(rows, base_rows, transitions):
    required = set(transitions or ())
    if not required:
        return True
    observed = {
        (row_index + 1, column_index + 1, before, after)
        for row_index, (base_row, row) in enumerate(zip(base_rows, rows))
        for column_index, (before, after) in enumerate(zip(base_row, row))
        if before != after
    }
    if observed != required:
        return False
    for row, column, before, after in required:
        x, y = column - 1, row - 1
        if not (0 <= y < len(rows) and 0 <= x < len(rows[y])):
            return False
        if base_rows[y][x] != before or rows[y][x] != after:
            return False
    return True


def _state_realizes_effect(state, strategy):
    return bool(state.operators.intersection(EFFECT_OPERATORS[strategy.effect]))


def _cheap_structure_valid(rows):
    if len(rows) != HEIGHT or any(len(row) != WIDTH for row in rows):
        return False
    player_count = sum(row.count("p") for row in rows)
    box_count = sum(row.count("s") for row in rows)
    target_count = sum(row.count("t") for row in rows)
    return player_count == 1 and 1 <= box_count <= 2 and target_count == box_count


def _metric_matches(goals, baseline, validation):
    matches = 0
    values = {
        "solutionSteps": getattr(validation, "solution_steps", None),
        "solutionPushes": getattr(validation, "solution_pushes", None),
        "searchedStates": getattr(validation, "searched_states", None),
    }
    for goal in goals:
        before = baseline.get(goal.metric)
        after = values.get(goal.metric)
        if before is None or after is None:
            continue
        if (
            (goal.direction == "increase" and after > before)
            or (goal.direction == "decrease" and after < before)
            or (goal.direction == "preserve" and after == before)
        ):
            matches += 1
    return matches


def _solution_trace_cells(rows, solution):
    if not isinstance(solution, str) or not solution:
        return set()
    player = _find_one(rows, "p")
    boxes = set(_find_all(rows, "s"))
    traced = {player, *boxes}
    directions = {"U": (0, -1), "D": (0, 1), "L": (-1, 0), "R": (1, 0)}
    for move in solution:
        if move not in directions:
            return set()
        dx, dy = directions[move]
        destination = (player[0] + dx, player[1] + dy)
        traced.add(destination)
        if destination in boxes:
            box_destination = (destination[0] + dx, destination[1] + dy)
            boxes.remove(destination)
            boxes.add(box_destination)
            traced.add(box_destination)
        player = destination
    return traced


def _player_reachable(rows, start):
    blocked = {"#", "@", " ", "s"}
    pending = deque([start])
    visited = {start}
    while pending:
        x, y = pending.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                continue
            if (nx, ny) in visited or rows[ny][nx] in blocked:
                continue
            visited.add((nx, ny))
            pending.append((nx, ny))
    return visited


def _connected_outer_shell(rows):
    pending = []
    visited = set()
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if tile == "#" and (x in {0, WIDTH - 1} or y in {0, HEIGHT - 1}):
                pending.append((x, y))
    while pending:
        position = pending.pop()
        if position in visited:
            continue
        visited.add(position)
        x, y = position
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < WIDTH and 0 <= ny < HEIGHT and rows[ny][nx] == "#":
                pending.append((nx, ny))
    return visited


def _find_one(rows, tile):
    matches = _find_all(rows, tile)
    if len(matches) != 1:
        raise RevisionPlanError(f"The base Stage must contain exactly one {tile} tile.")
    return matches[0]


def _find_all(rows, tile):
    return [(x, y) for y, row in enumerate(rows) for x, value in enumerate(row) if value == tile]


def _primitive_key(primitive):
    return (primitive.operator, primitive.changes)


def _deadline_reached(deadline):
    return time.monotonic() >= deadline


def _count_failure(diagnostics, reason):
    reasons = diagnostics["failureReasons"]
    reasons[reason] = reasons.get(reason, 0) + 1


def _safe_failure_code(exception):
    code = getattr(exception, "code", None)
    if code:
        return str(code)[:80]
    text = " ".join(str(exception).split()).casefold()
    if "solution" in text or "solvable" in text:
        return "UNSOLVABLE_LEVEL"
    return type(exception).__name__[:80]
