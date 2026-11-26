"""Native-async proposal pipeline: requirements -> plan -> map ops -> fallback beam search."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.grid import (
    HEIGHT,
    WIDTH,
    Grid,
    GridValidationError,
    apply_operations,
    diff_grids,
    map_fingerprint,
    parse_grid,
)
from server.core.llm_client import LLMError, LLMTransport, LLMValidationError, call_structured
from server.core.solver import SolverError, SolveResult, solve_min_pushes, solve_min_steps
from server.domain.proposals import (
    EDITABLE_OPERATORS,
    EFFECT_OPERATORS,
    MAX_EDIT_BUDGET,
    OPERATOR_COMPONENT,
    PRESERVE_COMPONENTS,
    ProposalSearchExhausted,
    RevisionPlan,
    build_tradeoff_notice,
    normalize_requirement_record,
    parse_revision_plan,
    preflight_requirements,
    search_revision_plan,
    validate_requirement_record,
    validate_revision_plan_against_map,
)
from server.i18n.messages import normalize_language
from server.llm._schemas_proposals import (
    MapOperationsPayload,
    ProposalFailureAnalysisPayload,
    RequirementRecordPayload,
    RevisionPlanPayload,
)
from server.llm.grounding import build_stage_facts, sanitize_visible_text
from server.llm.prompts.proposal import (
    build_failure_analysis_messages,
    build_map_operations_messages,
    build_requirements_messages,
    build_revision_plan_messages,
)

from ._proposal_eval import (
    ProposalResult,
    build_candidate_validator,
    compute_route_features,
    requirements_mention_pushes,
    select_best_llm_candidate,
    step_budget,
)


def format_preflight_issues(issues: Sequence[Mapping[str, Any]]) -> str:
    """Format requirement preflight conflicts as concise, actionable repair guidance."""
    parts: list[str] = []
    for issue in list(issues)[:3]:
        reason = str(issue.get("reason") or "conflict")
        req_id = issue.get("requirementId")
        point = issue.get("point")
        if reason == "protected_entity_change":
            parts.append(
                f"{req_id or 'requirement'} requests modifying a protected entity "
                "(player, boxes, and targets cannot be added, removed, or moved)"
            )
        elif reason == "conflicting_fixed_requirements":
            parts.append(
                f"{req_id or 'requirement'} conflicts with another invariant requirement"
            )
        elif reason == "exact_transition_conflicts_with_snapshot":
            parts.append(
                f"exactTransition at {point} does not match the current editable tile "
                "or targets the immutable outer_shell"
            )
        elif reason == "conflicting_exact_transitions":
            parts.append(f"exactTransitions specify conflicting target tiles at {point}")
        elif reason == "exact_transition_conflicts_with_preservation":
            parts.append(
                f"exactTransition conflicts with preserved invariant {req_id or 'requirement'}"
            )
        else:
            detail = req_id or point or "conflict"
            parts.append(f"{reason} ({detail})")
    return "; ".join(parts)


def merge_revision_strategies(strategies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge multiple single-branch strategies into one joint RevisionStrategy dict."""
    if len(strategies) == 1:
        return dict(strategies[0])
    operators: list[str] = []
    for strat in strategies:
        for op in strat.get("operators") or ():
            if op in EDITABLE_OPERATORS and op not in operators:
                operators.append(str(op))

    edited_components = {
        OPERATOR_COMPONENT[op] for op in operators if op in OPERATOR_COMPONENT
    }
    preserve_sets = [
        {str(c) for c in (strat.get("preserve") or ()) if str(c) in PRESERVE_COMPONENTS}
        for strat in strategies
    ]
    if preserve_sets:
        merged_preserve = set.intersection(*preserve_sets) - edited_components
    else:
        merged_preserve = set()
    merged_preserve.update({"outer_shell", "player", "boxes", "targets", "unrelated_areas"})

    focuses = [strat.get("focus") for strat in strategies]
    if any(f is None for f in focuses):
        merged_focus: dict[str, int] | None = None
    else:
        parsed_focuses = [f for f in focuses if isinstance(f, Mapping)]
        if not parsed_focuses:
            merged_focus = None
        else:
            min_r = max(1, min(int(f["row"]) - int(f["radius"]) for f in parsed_focuses))
            max_r = min(HEIGHT, max(int(f["row"]) + int(f["radius"]) for f in parsed_focuses))
            min_c = max(1, min(int(f["column"]) - int(f["radius"]) for f in parsed_focuses))
            max_c = min(WIDTH, max(int(f["column"]) + int(f["radius"]) for f in parsed_focuses))
            center_r = (min_r + max_r) // 2
            center_c = (min_c + max_c) // 2
            req_radius = max(
                1,
                center_r - min_r,
                max_r - center_r,
                center_c - min_c,
                max_c - center_c,
            )
            merged_focus = (
                {"row": center_r, "column": center_c, "radius": req_radius}
                if req_radius <= 3
                else None
            )

    total_budget = sum(max(1, int(strat.get("editBudget") or 1)) for strat in strategies)
    merged_budget = min(MAX_EDIT_BUDGET, max(1, total_budget))

    last_effect = str(strategies[-1].get("effect") or "open_route")
    op_set = set(operators)
    if last_effect in EFFECT_OPERATORS and (op_set & EFFECT_OPERATORS[last_effect]):
        merged_effect = last_effect
    else:
        merged_effect = next(
            (eff for eff, eff_ops in EFFECT_OPERATORS.items() if op_set & eff_ops),
            "open_route",
        )

    merged_goals: list[dict[str, Any]] = []
    seen_metrics: set[str] = set()
    for strat in strategies:
        for goal in strat.get("metricGoals") or ():
            if not isinstance(goal, Mapping):
                continue
            metric = str(goal.get("metric") or "")
            if metric and metric not in seen_metrics and len(merged_goals) < 3:
                seen_metrics.add(metric)
                merged_goals.append(dict(goal))

    merged_anchors: list[str] = []
    for strat in strategies:
        for anchor in strat.get("anchorEntities") or ():
            a = str(anchor)
            if a and a not in merged_anchors and len(merged_anchors) < 5:
                merged_anchors.append(a)

    merged_transitions: list[dict[str, Any]] = []
    if all(strat.get("requiredTransitions") for strat in strategies):
        seen_coords: set[tuple[int, int]] = set()
        for strat in strategies:
            for tr in strat.get("requiredTransitions") or ():
                if not isinstance(tr, Mapping):
                    continue
                coord = (int(tr["row"]), int(tr["column"]))
                if coord not in seen_coords and len(merged_transitions) < MAX_EDIT_BUDGET:
                    seen_coords.add(coord)
                    merged_transitions.append(dict(tr))

    play_objective = next(
        (
            str(strat["playObjective"]).strip()
            for strat in reversed(strategies)
            if strat.get("playObjective")
        ),
        None,
    )
    display_reason = next(
        (
            str(strat["displayReason"]).strip()
            for strat in reversed(strategies)
            if strat.get("displayReason")
        ),
        None,
    )

    effective_budget = (
        max(merged_budget, len(merged_transitions)) if merged_transitions else merged_budget
    )
    return {
        "effect": merged_effect,
        "focus": merged_focus,
        "operators": operators[:4],
        "preserve": sorted(merged_preserve),
        "editBudget": min(MAX_EDIT_BUDGET, effective_budget),
        "metricGoals": merged_goals,
        "requiredTransitions": merged_transitions,
        "anchorEntities": merged_anchors,
        "playObjective": play_objective,
        "displayReason": display_reason,
    }


def _needs_joint_strategy(strategies: Sequence[Mapping[str, Any]]) -> bool:
    if len(strategies) < 2:
        return False
    all_ops: set[str] = set()
    for strat in strategies:
        all_ops.update(str(op) for op in (strat.get("operators") or ()))
    for strat in strategies:
        ops = {str(op) for op in (strat.get("operators") or ())}
        preserve = {str(c) for c in (strat.get("preserve") or ())}
        usable = {op for op in ops if OPERATOR_COMPONENT.get(op) not in preserve}
        if all_ops.issubset(usable):
            return False
    return True


class ProposalPipeline:
    """Native-async proposal generation with lazy min-push solving and beam-search fallback."""

    def __init__(
        self,
        transport: LLMTransport,
        *,
        model: str,
        clock: Clock | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.transport = transport
        self.model = model
        self.clock = clock if clock is not None else SYSTEM_CLOCK
        self.extra_body = dict(extra_body) if extra_body is not None else None

    async def compile_requirements(
        self,
        *,
        designer_turns: Sequence[Mapping[str, Any]],
        grid_or_rows: Grid | Sequence[str],
        language: str = "en",
        selected_choices: Sequence[Mapping[str, Any]] = (),
        design_context: Mapping[str, Any] | None = None,
        entity_bindings: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        """Compile and strictly validate an evidence-bound RequirementRecord."""
        grid = parse_grid(grid_or_rows)
        snapshot = build_stage_facts(grid, entity_bindings).to_prompt_dict()
        messages = build_requirements_messages(
            designer_turns=designer_turns,
            stage_snapshot=snapshot,
            language=language,
            selected_choices=selected_choices,
            design_context=design_context,
        )
        holder: dict[str, Any] = {}

        def _validate(parsed: RequirementRecordPayload) -> RequirementRecordPayload:
            normalized_payload, _ = normalize_requirement_record(
                parsed.model_dump(by_alias=True), designer_turns, snapshot
            )
            record = validate_requirement_record(normalized_payload, designer_turns, snapshot)
            issues = preflight_requirements(grid.rows, record)
            if issues:
                formatted = format_preflight_issues(issues)
                raise LLMValidationError(
                    "REQUIREMENT_PREFLIGHT_CONFLICT",
                    f"Requirements conflict with map permissions: {formatted}",
                )
            holder["record"] = record
            return parsed

        await call_structured(
            self.transport,
            task="revision_requirements",
            model=self.model,
            messages=messages,
            response_model=RequirementRecordPayload,
            validator=_validate,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.6,
            max_tokens=3600,
            attempt_timeout_seconds=25.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        return holder["record"]

    async def compile_revision_plan(
        self,
        *,
        grid_or_rows: Grid | Sequence[str],
        authorized_brief: str,
        requirements: Mapping[str, Any] | None = None,
        precompiled_strategies: Sequence[Mapping[str, Any]] | None = None,
        language: str = "en",
        design_context: Mapping[str, Any] | None = None,
        preserved_components: Sequence[str] = (),
        excluded_hint: str = "",
        entity_bindings: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> tuple[RevisionPlan, int]:
        """Reuse validated precompiled strategies or compile a RevisionPlan via LLM."""
        grid = parse_grid(grid_or_rows)
        if precompiled_strategies:
            raw_list = [dict(item) for item in precompiled_strategies]
            candidate_strategies = (
                [merge_revision_strategies(raw_list), *raw_list][:3]
                if _needs_joint_strategy(raw_list)
                else raw_list
            )
            try:
                reused = parse_revision_plan({"strategies": candidate_strategies})
                validate_revision_plan_against_map(grid.rows, reused)
                return reused, 0
            except ValueError:
                pass

        snapshot = build_stage_facts(grid, entity_bindings).to_prompt_dict()
        messages = build_revision_plan_messages(
            stage_snapshot=snapshot,
            authorized_brief=authorized_brief,
            requirements=requirements,
            language=language,
            design_context=design_context,
            preserved_components=preserved_components,
            excluded_hint=excluded_hint,
        )
        holder: dict[str, RevisionPlan] = {}

        def _validate(parsed: RevisionPlanPayload) -> RevisionPlanPayload:
            plan = parse_revision_plan(parsed.model_dump(by_alias=True))
            validate_revision_plan_against_map(grid.rows, plan)
            holder["plan"] = plan
            return parsed

        result = await call_structured(
            self.transport,
            task="revision_plan",
            model=self.model,
            messages=messages,
            response_model=RevisionPlanPayload,
            validator=_validate,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.6,
            max_tokens=2000,
            attempt_timeout_seconds=35.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        return holder["plan"], result.attempts_used

    async def generate_proposal(
        self,
        *,
        grid_or_rows: Grid | Sequence[str],
        authorized_brief: str,
        designer_turns: Sequence[Mapping[str, Any]] = (),
        requirement_record: Mapping[str, Any] | None = None,
        precompiled_strategies: Sequence[Mapping[str, Any]] | None = None,
        binary_promises: Sequence[Mapping[str, Any]] = (),
        excluded_fingerprints: Sequence[str] | set[str] = (),
        preserved_components: Sequence[str] = (),
        design_context: Mapping[str, Any] | None = None,
        entity_bindings: Mapping[str, Any] | None = None,
        language: str = "en",
        deadline: float | None = None,
        request_id: str = "",
    ) -> ProposalResult:
        """Run full proposal pipeline with lazy min-push solving and beam-search fallback."""
        started = self.clock.perf_counter()
        locale = normalize_language(language)
        base_grid = parse_grid(grid_or_rows)
        attempts_total = 0

        resolved_record = dict(requirement_record) if requirement_record is not None else None
        if resolved_record is None and designer_turns:
            resolved_record = await self.compile_requirements(
                designer_turns=designer_turns,
                grid_or_rows=base_grid,
                language=locale,
                design_context=design_context,
                entity_bindings=entity_bindings,
                deadline=deadline,
                request_id=request_id,
            )
            attempts_total += 1

        plan, plan_attempts = await self.compile_revision_plan(
            grid_or_rows=base_grid,
            authorized_brief=authorized_brief,
            requirements=resolved_record,
            precompiled_strategies=precompiled_strategies,
            language=locale,
            design_context=design_context,
            preserved_components=preserved_components,
            entity_bindings=entity_bindings,
            deadline=deadline,
            request_id=request_id,
        )
        attempts_total += plan_attempts

        need_pushes = requirements_mention_pushes(resolved_record, plan)
        budget = step_budget(deadline, self.clock)
        baseline_solve = solve_min_steps(base_grid, deadline_seconds=budget, clock=self.clock)
        baseline_features = compute_route_features(
            base_grid,
            baseline_solve,
            entity_bindings=entity_bindings,
            need_min_pushes=need_pushes,
            deadline_seconds=budget,
            clock=self.clock,
        )
        excluded_set = set(excluded_fingerprints)
        outcomes_by_fp: dict[str, list[dict[str, Any]]] = {}
        validate_candidate = build_candidate_validator(
            base_grid=base_grid,
            baseline_solve=baseline_solve,
            baseline_features=baseline_features,
            resolved_record=resolved_record,
            binary_promises=binary_promises,
            excluded_set=excluded_set,
            need_pushes=need_pushes,
            deadline=deadline,
            clock=self.clock,
            outcomes_by_fp=outcomes_by_fp,
            entity_bindings=entity_bindings,
        )

        verified: list[tuple[Grid, SolveResult, int]] = []
        op_messages = build_map_operations_messages(
            stage_snapshot=build_stage_facts(base_grid, entity_bindings).to_prompt_dict(),
            revision_plan=plan.as_dict(),
            baseline_metrics=baseline_features,
            requirements=resolved_record,
            language=locale,
        )
        preserved_base = set(preserved_components)
        applicable_candidates: list[tuple[Grid, int]] = []

        def _validate_map_operations(parsed: MapOperationsPayload) -> MapOperationsPayload:
            applicable_candidates.clear()
            failure_reasons: list[str] = []
            for c_idx, cand in enumerate(parsed.candidates, start=1):
                if not (1 <= cand.strategy_index <= len(plan.strategies)):
                    failure_reasons.append(
                        f"candidate {c_idx}: strategyIndex={cand.strategy_index} is outside "
                        f"1..{len(plan.strategies)}"
                    )
                    continue
                strategy = plan.strategies[cand.strategy_index - 1]
                max_ops = min(MAX_EDIT_BUDGET, strategy.edit_budget)
                raw_ops = [item.model_dump(by_alias=True) for item in cand.operations]
                if not raw_ops or len(raw_ops) > max_ops:
                    failure_reasons.append(
                        f"candidate {c_idx}: {len(raw_ops)} operation(s) exceed "
                        f"editBudget={max_ops}"
                    )
                    continue
                effective_preserved = preserved_base | set(strategy.preserve)
                blocked_ops = sorted(
                    {
                        item["op"]
                        for item in raw_ops
                        if OPERATOR_COMPONENT.get(item["op"]) in effective_preserved
                    }
                )
                if blocked_ops:
                    blocked_components = sorted(effective_preserved & {"walls", "water"})
                    failure_reasons.append(
                        f"candidate {c_idx}: operator(s) {blocked_ops} edit preserved "
                        f"component(s) {blocked_components}"
                    )
                    continue
                try:
                    cand_grid = apply_operations(base_grid, raw_ops)
                except GridValidationError as exc:
                    failure_reasons.append(f"candidate {c_idx}: {exc.code} ({exc})")
                    continue
                changed_count = len(diff_grids(base_grid, cand_grid))
                if changed_count > max_ops:
                    failure_reasons.append(
                        f"candidate {c_idx}: {changed_count} changed cells exceed "
                        f"editBudget={max_ops}"
                    )
                    continue
                applicable_candidates.append((cand_grid, cand.strategy_index))

            if not applicable_candidates:
                raise LLMValidationError(
                    "MAP_OPERATIONS_INVALID",
                    (
                        "No map_operations candidate applied cleanly to the saved Stage: "
                        + "; ".join(failure_reasons[:3])
                    ),
                )
            return parsed

        try:
            op_res = await call_structured(
                self.transport,
                task="map_operations",
                model=self.model,
                messages=op_messages,
                response_model=MapOperationsPayload,
                validator=_validate_map_operations,
                deadline=deadline,
                max_attempts=2,
                temperature=0.6,
                max_tokens=1600,
                attempt_timeout_seconds=30.0,
                extra_body=self.extra_body,
                clock=self.clock,
                request_id=request_id,
            )
            attempts_total += op_res.attempts_used
            for cand_grid, strategy_index in applicable_candidates:
                try:
                    solved = validate_candidate(cand_grid.rows)
                    verified.append((cand_grid, solved, strategy_index))
                except (GridValidationError, SolverError, ValueError):
                    continue
        except LLMError:
            pass

        if verified:
            return select_best_llm_candidate(
                verified,
                base_grid=base_grid,
                outcomes_by_fp=outcomes_by_fp,
                resolved_record=resolved_record,
                plan=plan,
                locale=locale,
                attempts_used=attempts_total,
                request_id=request_id,
                latency_ms=round((self.clock.perf_counter() - started) * 1000),
                need_pushes=need_pushes,
            )

        search_cap = self.clock.monotonic() + 20.0
        search_deadline = min(deadline, search_cap) if deadline is not None else search_cap
        min_pushes_fn = (
            (
                lambda rows: solve_min_pushes(
                    rows,
                    deadline_seconds=step_budget(search_deadline, self.clock),
                    clock=self.clock,
                ).minimum_pushes
            )
            if need_pushes
            else None
        )
        search_res = await asyncio.to_thread(
            search_revision_plan,
            base_grid.rows,
            plan,
            validate_candidate,
            baseline_metrics=baseline_features,
            deadline=search_deadline,
            preserved_components=preserved_components,
            excluded_map_fingerprints=excluded_set,
            monotonic_fn=self.clock.monotonic,
            min_pushes_fn=min_pushes_fn,
        )
        cand_grid = parse_grid(search_res.rows)
        fp = map_fingerprint(cand_grid.rows)
        return ProposalResult(
            candidate_grid=cand_grid,
            solve_result=search_res.validation,
            changes=tuple(diff_grids(base_grid, cand_grid)),
            requirement_record=resolved_record,
            revision_plan=plan.as_dict(),
            tradeoff_notice=build_tradeoff_notice(
                resolved_record, outcomes_by_fp.get(fp, []), locale
            ),
            source="deterministic_beam_search",
            attempts_used=attempts_total,
            request_id=request_id,
            latency_ms=round((self.clock.perf_counter() - started) * 1000),
            diagnostics={**search_res.diagnostics, "lazyMinPushes": need_pushes},
        )

    async def generate_failure_analysis(
        self,
        *,
        grid_or_rows: Grid | Sequence[str],
        designer_turns: Sequence[Mapping[str, Any]],
        failure_code: str,
        failure_summary: str,
        requirements: Mapping[str, Any] | None = None,
        entity_bindings: Mapping[str, Any] | None = None,
        language: str = "en",
        deadline: float | None = None,
        request_id: str = "",
    ) -> str:
        """Explain an exhausted proposal search cleanly without claiming map edits."""
        locale = normalize_language(language)
        snapshot = build_stage_facts(parse_grid(grid_or_rows), entity_bindings).to_prompt_dict()
        messages = build_failure_analysis_messages(
            stage_snapshot=snapshot,
            designer_turns=designer_turns,
            failure_code=failure_code,
            failure_summary=failure_summary,
            requirements=requirements,
            language=locale,
        )
        result = await call_structured(
            self.transport,
            task="proposal_failure_analysis",
            model=self.model,
            messages=messages,
            response_model=ProposalFailureAnalysisPayload,
            deadline=deadline,
            max_attempts=1,
            temperature=0.3,
            max_tokens=700,
            attempt_timeout_seconds=18.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        return sanitize_visible_text(result.parsed.body, locale)


__all__ = [
    "ProposalPipeline",
    "ProposalResult",
    "ProposalSearchExhausted",
    "requirements_mention_pushes",
]
