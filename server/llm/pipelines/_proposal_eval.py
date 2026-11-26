"""Candidate evaluation & lazy min-push helpers shared by ProposalPipeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import Clock, remaining_seconds
from server.core.grid import Grid, TileChange, diff_grids, map_fingerprint, parse_grid
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


def compute_route_features(
    grid: Grid,
    solve_result: SolveResult,
    *,
    need_min_pushes: bool,
    deadline_seconds: float | None,
    clock: Clock,
) -> dict[str, Any]:
    features: dict[str, Any] = {
        "solutionSteps": solve_result.solution_steps,
        "solutionPushes": solve_result.solution_pushes,
        "searchedStates": solve_result.searched_states,
        "solution": solve_result.solution,
    }
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
