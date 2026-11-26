"""AI-editable beam search and candidate evaluation for RevisionPlan."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from server.domain.blueprints import GRID_HEIGHT, GRID_WIDTH

from ._search_primitives import (
    _generate_primitives,
    _required_transition_primitive,
    _select_beam,
    _state_from_primitives,
)
from .models import (
    BEAM_DEPTH,
    EFFECT_OPERATORS,
    MAX_CONSTRUCTED_CANDIDATES,
    MAX_VALID_CANDIDATES,
    PRIMITIVE_LIMIT,
    MetricGoal,
    Primitive,
    ProposalSearchExhausted,
    ProposalSearchResult,
    RevisionPlan,
    RevisionStrategy,
    SearchState,
    VerifiedCandidate,
)
from .plan_parser import (
    _map_fingerprint,
    connected_outer_shell,
    validate_revision_plan_against_map,
)


def _evaluate_state(
    state: SearchState,
    strategy: RevisionStrategy,
    strategy_index: int,
    base_rows: tuple[str, ...],
    proposal_validator: Callable[[list[str]], Any],
    baseline_metrics: Mapping[str, Any],
    diagnostics: dict[str, Any],
    verified: list[VerifiedCandidate],
    *,
    required_transitions: tuple[tuple[int, int, str, str], ...] = (),
    min_pushes_fn: Callable[[Any], int] | None = None,
) -> None:
    if required_transitions:
        observed = {
            (r + 1, c + 1, b, a)
            for r, (br, row) in enumerate(zip(base_rows, state.rows, strict=True))
            for c, (b, a) in enumerate(zip(br, row, strict=True))
            if b != a
        }
        if observed != set(required_transitions):
            diagnostics["staticRejectedCandidates"] += 1
            _count_failure(diagnostics, "required_transition_not_realized")
            return

    if not state.operators.intersection(EFFECT_OPERATORS[strategy.effect]):
        diagnostics["staticRejectedCandidates"] += 1
        _count_failure(diagnostics, "effect_not_realized")
        return

    if (
        len(state.rows) != GRID_HEIGHT
        or any(len(r) != GRID_WIDTH for r in state.rows)
        or sum(r.count("p") for r in state.rows) != 1
        or not (1 <= sum(r.count("s") for r in state.rows) <= 2)
        or sum(r.count("s") for r in state.rows) != sum(r.count("t") for r in state.rows)
    ):
        diagnostics["staticRejectedCandidates"] += 1
        _count_failure(diagnostics, "static_structure")
        return

    try:
        validation = proposal_validator(list(state.rows))
        diagnostics["solvedCandidates"] += 1
    except TimeoutError:
        diagnostics["solverRejectedCandidates"] += 1
        _count_failure(diagnostics, "TimeoutError")
        diagnostics["deadlineReached"] = True
        return
    except Exception as exc:
        diagnostics["solverRejectedCandidates"] += 1
        code = getattr(exc, "code", None)
        _count_failure(diagnostics, str(code)[:80] if code else type(exc).__name__[:80])
        return

    metric_matches = _metric_matches(
        strategy.metric_goals, baseline_metrics, validation, min_pushes_fn=min_pushes_fn
    )
    if metric_matches != len(strategy.metric_goals):
        _count_failure(diagnostics, "metric_goal_not_met")
        return

    changed_cells = sum(
        b != a
        for br, ar in zip(base_rows, state.rows, strict=True)
        for b, a in zip(br, ar, strict=True)
    )
    verified.append(
        VerifiedCandidate(
            rows=state.rows,
            validation=validation,
            strategy_index=strategy_index,
            operators=tuple(sorted(state.operators)),
            metric_matches=metric_matches,
            effect_match=int(bool(state.operators & EFFECT_OPERATORS[strategy.effect])),
            changed_cells=changed_cells,
        )
    )


def _metric_matches(
    goals: Sequence[MetricGoal],
    baseline: Mapping[str, Any],
    validation: Any,
    *,
    min_pushes_fn: Callable[[Any], int] | None = None,
) -> int:
    matches = 0
    values = {
        "solutionSteps": getattr(validation, "solution_steps", None),
        "solutionPushes": getattr(validation, "solution_pushes", None),
        "minimumPushes": None,
        "searchedStates": getattr(validation, "searched_states", None),
    }
    for goal in goals:
        before = baseline.get(goal.metric)
        if goal.metric == "minimumPushes" and min_pushes_fn is not None:
            try:
                values["minimumPushes"] = min_pushes_fn(validation.rows)
            except TimeoutError:
                raise
            except Exception:
                values["minimumPushes"] = None
        after = values.get(goal.metric)
        if before is None or after is None:
            continue
        if (
            (goal.direction == "increase" and after - before >= goal.minimum_delta)
            or (goal.direction == "decrease" and before - after >= goal.minimum_delta)
            or (goal.direction == "preserve" and after == before)
        ):
            matches += 1
    return matches


def _count_failure(diagnostics: dict[str, Any], reason: str) -> None:
    reasons = diagnostics["failureReasons"]
    reasons[reason] = reasons.get(reason, 0) + 1


def search_revision_plan(
    base_rows: Sequence[str],
    plan: RevisionPlan,
    proposal_validator: Callable[[list[str]], Any],
    *,
    baseline_metrics: Mapping[str, Any] | None = None,
    deadline: float | None = None,
    preserved_components: Sequence[str] | None = None,
    required_transitions: Sequence[tuple[int, int, str, str]] | None = None,
    excluded_map_fingerprints: Sequence[str] | set[str] | None = None,
    maximum_valid_candidates: int = MAX_VALID_CANDIDATES,
    monotonic_fn: Callable[[], float] | None = None,
    min_pushes_fn: Callable[[Any], int] | None = None,
) -> ProposalSearchResult:
    """Beam search over AI-editable wall/water operators with exact-transition short-circuiting."""
    now_fn = monotonic_fn or time.monotonic
    started_at = now_fn()
    rows = tuple(base_rows)
    validate_revision_plan_against_map(rows, plan)
    baseline = baseline_metrics or {}
    eff_deadline = deadline if deadline is not None else float("inf")
    diagnostics: dict[str, Any] = {
        "strategies": len(plan.strategies),
        "constructedCandidates": 0,
        "staticRejectedCandidates": 0,
        "solverRejectedCandidates": 0,
        "solvedCandidates": 0,
        "validCandidates": 0,
        "deadlineReached": False,
        "failureReasons": {},
    }
    verified: list[VerifiedCandidate] = []
    excluded = set(excluded_map_fingerprints or ())

    for strategy_index, strategy in enumerate(plan.strategies, start=1):
        if now_fn() >= eff_deadline:
            diagnostics["deadlineReached"] = True
            break

        effective_required = (
            tuple(required_transitions)
            if required_transitions is not None
            else strategy.required_transitions
        )
        shell = connected_outer_shell(rows)
        allowed_operators = set(strategy.operators)
        effective_preserved = set(preserved_components or ()) | set(strategy.preserve)

        # Short-circuit exact transitions: only the exact transition set
        # can satisfy observed == required.
        if effective_required:
            req_primitives = [
                p
                for p in (
                    _required_transition_primitive(
                        rows, t, allowed_operators, effective_preserved, shell
                    )
                    for t in effective_required
                )
                if p is not None
            ]
            if len(req_primitives) != len(effective_required):
                diagnostics["staticRejectedCandidates"] += 1
                _count_failure(diagnostics, "required_transition_not_realized")
                continue
            exact_state = _state_from_primitives(rows, tuple(req_primitives), strategy.edit_budget)
            if exact_state is None:
                diagnostics["staticRejectedCandidates"] += 1
                _count_failure(diagnostics, "required_transition_not_realized")
                continue
            diagnostics["constructedCandidates"] += 1
            if _map_fingerprint(exact_state.rows) in excluded:
                diagnostics["staticRejectedCandidates"] += 1
                _count_failure(diagnostics, "CANDIDATE_DUPLICATED")
                continue
            _evaluate_state(
                exact_state,
                strategy,
                strategy_index,
                rows,
                proposal_validator,
                baseline,
                diagnostics,
                verified,
                required_transitions=effective_required,
                min_pushes_fn=min_pushes_fn,
            )
            if len(verified) >= maximum_valid_candidates or diagnostics["deadlineReached"]:
                break
            continue

        seen_maps = {rows}
        primitives = _generate_primitives(rows, strategy, baseline, preserved_components)
        candidate_primitives: list[Primitive] = []
        seen_keys: set[tuple[str, tuple[tuple[int, int, str, str], ...]]] = set()
        for item in primitives:
            key = (item.operator, item.changes)
            if key not in seen_keys:
                seen_keys.add(key)
                candidate_primitives.append(item)
                if len(candidate_primitives) >= PRIMITIVE_LIMIT:
                    break

        max_depth = min(strategy.edit_budget, BEAM_DEPTH)
        beam = _select_beam(
            [
                s
                for p in candidate_primitives
                if (s := _state_from_primitives(rows, (p,), strategy.edit_budget)) is not None
            ]
        )

        for depth in range(1, max_depth + 1):
            if not beam:
                break
            next_states: list[SearchState] = []
            for state in beam:
                if diagnostics["constructedCandidates"] >= MAX_CONSTRUCTED_CANDIDATES:
                    break
                if now_fn() >= eff_deadline:
                    diagnostics["deadlineReached"] = True
                    break
                if state.rows not in seen_maps:
                    seen_maps.add(state.rows)
                    diagnostics["constructedCandidates"] += 1
                    if _map_fingerprint(state.rows) in excluded:
                        diagnostics["staticRejectedCandidates"] += 1
                        _count_failure(diagnostics, "CANDIDATE_DUPLICATED")
                        continue
                    _evaluate_state(
                        state,
                        strategy,
                        strategy_index,
                        rows,
                        proposal_validator,
                        baseline,
                        diagnostics,
                        verified,
                        min_pushes_fn=min_pushes_fn,
                    )
                    if diagnostics["deadlineReached"] or len(verified) >= maximum_valid_candidates:
                        break
                if depth < max_depth:
                    used_positions = frozenset().union(*(p.positions for p in state.primitives))
                    last_key = (state.primitives[-1].operator, state.primitives[-1].changes)
                    for primitive in candidate_primitives:
                        if (primitive.operator, primitive.changes) <= last_key or (
                            primitive.positions & used_positions
                        ):
                            continue
                        expanded = _state_from_primitives(
                            rows, (*state.primitives, primitive), strategy.edit_budget
                        )
                        if expanded is not None:
                            next_states.append(expanded)
            if (
                diagnostics["constructedCandidates"] >= MAX_CONSTRUCTED_CANDIDATES
                or diagnostics["deadlineReached"]
                or len(verified) >= maximum_valid_candidates
            ):
                break
            beam = _select_beam(next_states)
        if (
            diagnostics["constructedCandidates"] >= MAX_CONSTRUCTED_CANDIDATES
            or diagnostics["deadlineReached"]
            or len(verified) >= maximum_valid_candidates
        ):
            break

    diagnostics["validCandidates"] = len(verified)
    diagnostics["elapsedMs"] = int((now_fn() - started_at) * 1000)
    if not verified:
        raise ProposalSearchExhausted(diagnostics)

    candidate_rank = getattr(proposal_validator, "candidate_rank", None)
    selected = min(
        verified,
        key=lambda cand: (
            (
                *(-x for x in candidate_rank(cand.rows)),
                cand.changed_cells,
                cand.strategy_index,
                cand.rows,
            )
            if candidate_rank is not None
            else cand.selection_key
        ),
    )
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
