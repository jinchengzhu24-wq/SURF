"""Native-async proposal pipeline: requirements -> plan -> map ops -> fallback beam search."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.grid import (
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
    ProposalSearchExhausted,
    RevisionPlan,
    build_tradeoff_notice,
    parse_revision_plan,
    preflight_requirements,
    search_revision_plan,
    validate_requirement_record,
    validate_revision_plan_against_map,
)
from server.i18n.messages import normalize_language
from server.llm.grounding import build_stage_facts, sanitize_visible_text
from server.llm.prompts.proposal import (
    build_failure_analysis_messages,
    build_map_operations_messages,
    build_requirements_messages,
    build_revision_plan_messages,
)
from server.llm.schemas import (
    MapOperationsPayload,
    ProposalFailureAnalysisPayload,
    RequirementRecordPayload,
    RevisionPlanPayload,
)

from ._proposal_eval import (
    ProposalResult,
    build_candidate_validator,
    compute_route_features,
    requirements_mention_pushes,
    select_best_llm_candidate,
    step_budget,
)


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
            record = validate_requirement_record(
                parsed.model_dump(by_alias=True), designer_turns, snapshot
            )
            issues = preflight_requirements(grid.rows, record)
            if issues:
                raise LLMValidationError(
                    "REQUIREMENT_PREFLIGHT_CONFLICT",
                    f"Requirements conflict with map permissions: {issues[0]}",
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
            max_tokens=2400,
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
            try:
                reused = parse_revision_plan({"strategies": list(precompiled_strategies)})
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
        )

        verified: list[tuple[Grid, SolveResult, int]] = []
        op_messages = build_map_operations_messages(
            stage_snapshot=build_stage_facts(base_grid, entity_bindings).to_prompt_dict(),
            revision_plan=plan.as_dict(),
            baseline_metrics=baseline_features,
            requirements=resolved_record,
            language=locale,
        )
        try:
            op_res = await call_structured(
                self.transport,
                task="map_operations",
                model=self.model,
                messages=op_messages,
                response_model=MapOperationsPayload,
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
            for cand in op_res.parsed.candidates:
                raw_ops = [item.model_dump(by_alias=True) for item in cand.operations]
                try:
                    cand_grid = apply_operations(base_grid, raw_ops)
                    solved = validate_candidate(cand_grid.rows)
                    verified.append((cand_grid, solved, cand.strategy_index))
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

        min_pushes_fn = (
            (lambda rows: solve_min_pushes(rows, clock=self.clock).minimum_pushes)
            if need_pushes
            else None
        )
        search_res = search_revision_plan(
            base_grid.rows,
            plan,
            validate_candidate,
            baseline_metrics=baseline_features,
            deadline=deadline,
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
