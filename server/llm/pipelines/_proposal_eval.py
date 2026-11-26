"""Candidate evaluation & lazy min-push helpers shared by ProposalPipeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import Clock, remaining_seconds
from server.core.grid import (
    HEIGHT,
    TILE_BOX,
    TILE_PLAYER,
    TILE_TARGET,
    WIDTH,
    Grid,
    TileChange,
    diff_grids,
    map_fingerprint,
    parse_grid,
)
from server.core.solver import SolveResult, solve_min_pushes, solve_min_steps
from server.domain.proposals import (
    RevisionPlan,
    build_tradeoff_notice,
    check_promises,
    evaluate_requirements,
    goal_rank,
)

_PUSH_KEYWORD_MARKERS = ("push", "pushes", "minimumpushes", "推", "推动")


@dataclass(frozen=True, slots=True)
class ProposalResult:
    """Verified solvable map proposal produced by LLM operations or fallback beam search."""

    candidate_grid: Grid
    solve_result: SolveResult
    changes: tuple[TileChange, ...]
    requirement_record: dict[str, Any] | None
    revision_plan: dict[str, Any]
    tradeoff_notice: str
    source: str
    attempts_used: int
    request_id: str
    latency_ms: int
    diagnostics: dict[str, Any] = field(default_factory=dict)


def select_best_llm_candidate(
    verified: Sequence[tuple[Grid, SolveResult, int]],
    *,
    base_grid: Grid,
    outcomes_by_fp: Mapping[str, list[dict[str, Any]]],
    resolved_record: dict[str, Any] | None,
    plan: RevisionPlan,
    locale: str,
    attempts_used: int,
    request_id: str,
    latency_ms: int,
    need_pushes: bool,
) -> ProposalResult:
    """Pick the highest-ranked verified LLM candidate and build ProposalResult."""
    best_grid, best_solve, best_idx = min(
        verified,
        key=lambda item: (
            tuple(-x for x in goal_rank(outcomes_by_fp.get(item[0].fingerprint(), []))),
            len(diff_grids(base_grid, item[0])),
            item[2],
        ),
    )
    fp = map_fingerprint(best_grid.rows)
    return ProposalResult(
        candidate_grid=best_grid,
        solve_result=best_solve,
        changes=tuple(diff_grids(base_grid, best_grid)),
        requirement_record=resolved_record,
        revision_plan=plan.as_dict(),
        tradeoff_notice=build_tradeoff_notice(resolved_record, outcomes_by_fp.get(fp, []), locale),
        source="llm_operations",
        attempts_used=attempts_used,
        request_id=request_id,
        latency_ms=latency_ms,
        diagnostics={"selectedStrategyIndex": best_idx, "lazyMinPushes": need_pushes},
    )


def requirements_mention_pushes(
    requirements: Mapping[str, Any] | None,
    plan: RevisionPlan | None = None,
) -> bool:
    """Return True ONLY when requirements or metric goals explicitly mention pushes."""
    if plan is not None and any(
        goal.metric == "minimumPushes"
        for strategy in plan.strategies
        for goal in strategy.metric_goals
    ):
        return True
    if not isinstance(requirements, Mapping):
        return False
    for item in requirements.get("requirements") or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("property") in {"minimumPushes", "solutionPushes"}:
            return True
        statement = str(item.get("statement") or "").casefold()
        if any(marker in statement for marker in _PUSH_KEYWORD_MARKERS):
            return True
    return False


def step_budget(deadline: float | None, clock: Clock) -> float:
    return min(2.5, remaining_seconds(deadline, clock=clock)) if deadline is not None else 2.5


_WITNESS_DIRECTIONS: dict[str, tuple[int, int]] = {
    "U": (0, -1),
    "D": (0, 1),
    "L": (-1, 0),
    "R": (1, 0),
}


def _extract_witness_entities(
    grid: Grid,
    entity_bindings: Mapping[str, Any] | None,
) -> tuple[
    tuple[int, int] | None,
    dict[tuple[int, int], str],
    dict[tuple[int, int], str],
    dict[str, tuple[int, int]],
    bool,
]:
    """Resolve 0-based (x, y) coordinates and 1-based entityPositions from bindings or grid."""
    if entity_bindings is not None:
        if entity_bindings.get("identityStatus") != "exact":
            return None, {}, {}, {}, False
        entities = entity_bindings.get("entities") or []
        player_recs = [e for e in entities if isinstance(e, Mapping) and e.get("kind") == "player"]
        box_recs = [e for e in entities if isinstance(e, Mapping) and e.get("kind") == "box"]
        target_recs = [e for e in entities if isinstance(e, Mapping) and e.get("kind") == "target"]
        entity_positions = {
            str(x.get("id") or x.get("label") or "").upper(): (int(x["row"]), int(x["column"]))
            for x in entities
            if isinstance(x, Mapping)
            and x.get("row") is not None
            and x.get("column") is not None
            and (x.get("id") or x.get("label"))
        }
        if len(player_recs) != 1 or not box_recs or len(box_recs) != len(target_recs):
            return None, {}, {}, entity_positions, False
        try:
            player = (int(player_recs[0]["column"]) - 1, int(player_recs[0]["row"]) - 1)
            boxes = {
                (int(item["column"]) - 1, int(item["row"]) - 1): str(item["label"])
                for item in box_recs
            }
            targets = {
                (int(item["column"]) - 1, int(item["row"]) - 1): str(item["label"])
                for item in target_recs
            }
        except (KeyError, TypeError, ValueError):
            return None, {}, {}, entity_positions, False
        return player, boxes, targets, entity_positions, True

    players = grid.find_tiles(TILE_PLAYER)
    box_pts = grid.find_tiles(TILE_BOX)
    target_pts = grid.find_tiles(TILE_TARGET)
    entity_positions: dict[str, tuple[int, int]] = {}
    if players:
        entity_positions["P"] = (players[0][1] + 1, players[0][0] + 1)
    boxes: dict[tuple[int, int], str] = {}
    for idx, (x, y) in enumerate(box_pts, start=1):
        label = f"B{idx}"
        boxes[(x, y)] = label
        entity_positions[label] = (y + 1, x + 1)
    targets: dict[tuple[int, int], str] = {}
    for idx, (x, y) in enumerate(target_pts, start=1):
        label = f"T{idx}"
        targets[(x, y)] = label
        entity_positions[label] = (y + 1, x + 1)
    if len(players) != 1 or not boxes or len(boxes) != len(targets):
        return None, {}, {}, entity_positions, False
    return players[0], boxes, targets, entity_positions, True


def _witness_route_evidence(
    grid: Grid,
    solve_result: SolveResult,
    entity_bindings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Derive boxAlternations, longestPushRun, firstRuns, interruptions, and entityPositions."""
    player, boxes, targets, entity_positions, exact_ok = _extract_witness_entities(
        grid, entity_bindings
    )
    solution = str(solve_result.solution or "").strip().upper()
    if not exact_ok or player is None or not solution:
        return {"mode": "totals_only", "entityPositions": entity_positions}

    rows = grid.rows
    push_sequence: list[str] = []
    push_counts: dict[str, int] = {label: 0 for label in boxes.values()}
    try:
        for move in solution:
            delta = _WITNESS_DIRECTIONS.get(move)
            if delta is None:
                raise ValueError("invalid move")
            dx, dy = delta
            dest = (player[0] + dx, player[1] + dy)
            if not (0 <= dest[0] < WIDTH and 0 <= dest[1] < HEIGHT):
                raise ValueError("move outside map")
            if rows[dest[1]][dest[0]] in {"#", " ", "@"}:
                raise ValueError("move into blocked tile")
            if dest in boxes:
                box_dest = (dest[0] + dx, dest[1] + dy)
                if not (0 <= box_dest[0] < WIDTH and 0 <= box_dest[1] < HEIGHT):
                    raise ValueError("push outside map")
                if rows[box_dest[1]][box_dest[0]] in {"#", " ", "@"} or box_dest in boxes:
                    raise ValueError("invalid push")
                label = boxes.pop(dest)
                boxes[box_dest] = label
                push_sequence.append(label)
                push_counts[label] += 1
            player = dest
        if set(boxes) != set(targets):
            raise ValueError("solution does not finish on targets")
        if len(solution) != solve_result.solution_steps:
            raise ValueError("step count mismatch")
        if len(push_sequence) != solve_result.solution_pushes:
            raise ValueError("push count mismatch")
    except (KeyError, TypeError, ValueError, IndexError):
        return {"mode": "totals_only", "entityPositions": entity_positions}

    push_order: list[dict[str, Any]] = []
    for label in push_sequence:
        if push_order and push_order[-1]["box"] == label:
            push_order[-1]["pushes"] += 1
        else:
            push_order.append({"box": label, "pushes": 1})

    box_alternations = sum(
        left != right for left, right in zip(push_sequence, push_sequence[1:], strict=False)
    )
    box_labels = {item["box"] for item in push_order}
    return {
        "mode": "verified_route_summary",
        "pushBlocks": len(push_order),
        "boxAlternations": box_alternations,
        "pushesByBox": push_counts,
        "longestPushRun": max([0, *(int(item["pushes"]) for item in push_order)]),
        "firstRuns": {
            label: next(int(item["pushes"]) for item in push_order if item["box"] == label)
            for label in box_labels
        },
        "interruptions": {
            label: sum(item["box"] == label for item in push_order) > 1 for label in box_labels
        },
        "entityPositions": entity_positions,
    }


def compute_route_features(
    grid: Grid,
    solve_result: SolveResult,
    *,
    entity_bindings: Mapping[str, Any] | None = None,
    need_min_pushes: bool,
    deadline_seconds: float | None,
    clock: Clock,
) -> dict[str, Any]:
    evidence = _witness_route_evidence(grid, solve_result, entity_bindings)
    features: dict[str, Any] = {
        "solutionSteps": solve_result.solution_steps,
        "solutionPushes": solve_result.solution_pushes,
        "searchedStates": solve_result.searched_states,
        "solution": solve_result.solution,
        "routeMode": evidence.get("mode", "totals_only"),
        "entityPositions": evidence.get("entityPositions") or {},
    }
    if evidence.get("mode") == "verified_route_summary":
        features.update(
            {
                "pushBlocks": evidence["pushBlocks"],
                "boxAlternations": evidence["boxAlternations"],
                "pushesByBox": evidence["pushesByBox"],
                "longestPushRun": evidence["longestPushRun"],
                "firstRuns": evidence["firstRuns"],
                "interruptions": evidence["interruptions"],
            }
        )
    if need_min_pushes:
        push_res = solve_min_pushes(grid, deadline_seconds=deadline_seconds, clock=clock)
        features["minimumPushes"] = push_res.minimum_pushes
    return features


def build_candidate_validator(
    *,
    base_grid: Grid,
    baseline_solve: SolveResult,
    baseline_features: Mapping[str, Any],
    resolved_record: Mapping[str, Any] | None,
    binary_promises: Sequence[Mapping[str, Any]],
    excluded_set: set[str],
    need_pushes: bool,
    deadline: float | None,
    clock: Clock,
    outcomes_by_fp: dict[str, list[dict[str, Any]]],
    entity_bindings: Mapping[str, Any] | None = None,
) -> Callable[[Sequence[str]], SolveResult]:
    """Build deterministic validator checking solvability, promises, and requirements."""

    def _validate_candidate(candidate_rows: Sequence[str]) -> SolveResult:
        cand_grid = parse_grid(candidate_rows)
        fp = cand_grid.fingerprint()
        if fp in excluded_set:
            raise ValueError("CANDIDATE_DUPLICATED")
        budget = step_budget(deadline, clock)
        solved = solve_min_steps(cand_grid, deadline_seconds=budget, clock=clock)
        if binary_promises:
            check_promises(base_grid.rows, cand_grid.rows, binary_promises, baseline_solve, solved)
        if resolved_record is not None:
            cand_features = compute_route_features(
                cand_grid,
                solved,
                entity_bindings=entity_bindings,
                need_min_pushes=need_pushes,
                deadline_seconds=budget,
                clock=clock,
            )
            outcomes = evaluate_requirements(
                base_grid.rows,
                cand_grid.rows,
                resolved_record,
                baseline_features,
                cand_features,
            )
            if any(not item["passed"] for item in outcomes):
                raise ValueError("SEMANTIC_CONSTRAINT_NOT_MET")
            outcomes_by_fp[fp] = outcomes
        return solved

    return _validate_candidate
