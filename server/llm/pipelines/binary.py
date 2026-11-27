"""Native-async Policy-2 binary discovery pipeline (NO Policy-1 fallback)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock, remaining_seconds
from server.core.grid import Grid, parse_grid
from server.core.llm_client import LLMTransport, LLMValidationError, call_structured
from server.core.solver import SolveResult, solve_min_steps
from server.domain.proposals import (
    BinaryError,
    ProposalSearchExhausted,
    binary_signature,
    check_promises,
    connected_outer_shell,
    normalize_requirement_record,
    parse_binary_step,
    parse_revision_plan,
    preflight_requirements,
    render_protected_refusal,
    search_revision_plan,
    validate_requirement_record,
)
from server.domain.proposals.binary_discovery import _corridor_span, _focus_cells
from server.i18n.messages import normalize_language
from server.llm._schemas_proposals import select_binary_discovery_schema
from server.llm._schemas_tasks import StrictSchema
from server.llm.grounding import (
    build_stage_facts,
    sanitize_visible_text,
    validate_visible_language,
    verify_grounding,
)
from server.llm.prompts.binary import build_binary_discovery_messages

from .proposal import format_preflight_issues, merge_revision_strategies


def _check_branch_promise_precondition(
    rows: Sequence[str],
    outer_shell: set[tuple[int, int]],
    *,
    branch_index: int,
    promise: Mapping[str, Any],
    strategy: Mapping[str, Any] | None = None,
) -> None:
    """Fast base-map precondition check before spending beam-search budget on doomed branches."""
    kind = str(promise.get("kind") or "")
    component = str(promise.get("component") or "")
    if kind == "widen_corridor":
        corridor = promise.get("corridor")
        if isinstance(corridor, Mapping):
            row = int(corridor["row"])
            col = int(corridor["column"])
            axis = str(corridor["axis"])
            orth_axis = "vertical" if axis == "horizontal" else "horizontal"
            before = _corridor_span(rows, row, col, axis)
            if before == 0:
                tile = rows[row - 1][col - 1]
                raise LLMValidationError(
                    "BINARY_PREFLIGHT_NOT_FOUND",
                    (
                        f"Branch {branch_index} widen_corridor cell ({row},{col}) is not "
                        f"walkable (tile={tile!r}); choose a walkable floor cell inside a "
                        "narrow corridor, or switch promise.kind to 'reduce_obstacles' or "
                        "'shorter_solution'."
                    ),
                )
            along = _corridor_span(rows, row, col, orth_axis)
            if along < 3 or before >= along:
                raise LLMValidationError(
                    "BINARY_PREFLIGHT_NOT_FOUND",
                    (
                        f"Branch {branch_index} widen_corridor at ({row},{col}) axis='{axis}' "
                        f"fails corridor geometry on the current Stage (width={before}, "
                        f"orthogonal length={along}; requires width < length and length >= 3). "
                        "Note: corridor.axis is the narrow cross-section axis to widen "
                        "(perpendicular to travel direction: use axis='vertical' for a horizontal "
                        "corridor and axis='horizontal' for a vertical corridor). Pick the "
                        "narrower axis/cell, or switch promise.kind to 'reduce_obstacles' or "
                        "'shorter_solution'."
                    ),
                )
    elif kind in {"reduce_obstacles", "reshape_water"}:
        prom_focus = promise.get("focus") if isinstance(promise.get("focus"), Mapping) else None
        strat_focus = (
            strategy.get("focus")
            if isinstance(strategy, Mapping) and isinstance(strategy.get("focus"), Mapping)
            else None
        )
        height = len(rows)
        inter_cells = [
            (y, x, tile)
            for y, x, tile in _focus_cells(rows, prom_focus)
            if 0 < y < height - 1
            and 0 < x < len(rows[y]) - 1
            and (
                strat_focus is None
                or max(
                    abs(y + 1 - int(strat_focus["row"])),
                    abs(x + 1 - int(strat_focus["column"])),
                )
                <= int(strat_focus["radius"])
            )
        ]
        if kind == "reshape_water":
            water_count = sum(1 for _, _, tile in inter_cells if tile == "@")
            dest_count = sum(
                1
                for y, x, tile in inter_cells
                if tile == "." or (tile == "#" and (x, y) not in outer_shell)
            )
            if water_count == 0 or dest_count == 0:
                raise LLMValidationError(
                    "BINARY_PREFLIGHT_NOT_FOUND",
                    (
                        f"Branch {branch_index} reshape_water/water requires both '@' "
                        f"(got {water_count}) and destination '.' or editable '#' "
                        f"(got {dest_count}) inside strategy.focus={strat_focus} & "
                        f"promise.focus={prom_focus}; widen/align focus, set focus=null, "
                        "or switch to 'reduce_obstacles'."
                    ),
                )
            return

        if component == "water":
            editable_count = sum(1 for _, _, tile in inter_cells if tile == "@")
        elif component == "internal_walls":
            editable_count = sum(
                1 for y, x, tile in inter_cells if tile == "#" and (x, y) not in outer_shell
            )
        else:
            editable_count = sum(
                1
                for y, x, tile in inter_cells
                if tile == "@" or (tile == "#" and (x, y) not in outer_shell)
            )
        if editable_count == 0:
            raise LLMValidationError(
                "BINARY_PREFLIGHT_NOT_FOUND",
                (
                    f"Branch {branch_index} reduce_obstacles/{component} contains 0 matching "
                    f"editable tiles inside strategy.focus={strat_focus} & "
                    f"promise.focus={prom_focus} on the current Stage; align/widen focus, "
                    "set focus=null, or pick another component."
                ),
            )


def _normalize_binary_payload_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Fill context-omitted fields and defaults so downstream domain structures stay consistent."""
    normalized: dict[str, Any] = dict(raw)
    normalized.setdefault("question", "")
    normalized.setdefault("note", "")
    normalized.setdefault("branches", [])
    normalized.setdefault("planStrategies", [])
    normalized.setdefault("protectedEntities", [])
    normalized.setdefault("initialRequirements", None)

    norm_branches: list[dict[str, Any]] = []
    for branch in normalized["branches"]:
        b = dict(branch)
        if isinstance(b.get("strategy"), Mapping):
            strat = dict(b["strategy"])
            strat.setdefault("metricGoals", [])
            b["strategy"] = strat
        if isinstance(b.get("promise"), Mapping):
            prom = dict(b["promise"])
            prom.setdefault("focus", None)
            prom.setdefault("corridor", None)
            b["promise"] = prom
        norm_branches.append(b)
    normalized["branches"] = norm_branches

    norm_plan_strategies: list[Any] = []
    for strat in normalized["planStrategies"]:
        if isinstance(strat, Mapping):
            s = dict(strat)
            s.setdefault("metricGoals", [])
            norm_plan_strategies.append(s)
        else:
            norm_plan_strategies.append(strat)
    normalized["planStrategies"] = norm_plan_strategies
    return normalized


@dataclass(frozen=True, slots=True)
class BranchPreflightCandidate:
    """Verified solvable candidate backing one Policy-2 A/B choice."""

    branch_index: int
    label: str
    rows: tuple[str, ...]
    solve_result: SolveResult
    promise_outcomes: tuple[dict[str, Any], ...]
    strategy: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BinaryDiscoveryResult:
    """Validated Policy-2 binary discovery step and verified branch preflight caches."""

    step: dict[str, Any]
    initial_requirements: dict[str, Any] | None
    branch_candidates: tuple[BranchPreflightCandidate, ...]
    refusal_message: str | None = None
    attempts_used: int = 0
    request_id: str = ""
    latency_ms: int = 0
    usage: dict[str, int | None] = field(default_factory=dict)


class BinaryDiscoveryPipeline:
    """Native-async Policy-2 binary choice generation and deterministic preflight."""

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

    async def run_step(
        self,
        *,
        grid_or_rows: Grid | Sequence[str],
        initial_request: str,
        initial_designer_turns: Sequence[Mapping[str, Any]],
        question_count: int = 0,
        selected_choices: Sequence[Mapping[str, Any]] = (),
        verified_initial_requirements: Mapping[str, Any] | None = None,
        design_context: Mapping[str, Any] | None = None,
        entity_bindings: Mapping[str, Any] | None = None,
        language: str = "en",
        deadline: float | None = None,
        preflight_budget_seconds: float = 20.0,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> BinaryDiscoveryResult:
        """Execute Policy-2 single-slot binary discovery with bounded deterministic preflight."""
        locale = normalize_language(language)
        grid = parse_grid(grid_or_rows)
        outer_shell = connected_outer_shell(grid.rows)
        snapshot = build_stage_facts(grid, entity_bindings).to_prompt_dict()
        messages = build_binary_discovery_messages(
            stage_snapshot=snapshot,
            initial_request=initial_request,
            initial_designer_turns=initial_designer_turns,
            question_count=question_count,
            selected_choices=selected_choices,
            verified_initial_requirements=verified_initial_requirements,
            design_context=design_context,
            language=locale,
        )
        prior_promises = [choice["promise"] for choice in selected_choices if "promise" in choice]
        prior_sigs = {binary_signature(p) for p in prior_promises}
        prior_strategies = [
            choice["strategy"]
            for choice in selected_choices
            if isinstance(choice.get("strategy"), Mapping)
        ]
        baseline_solve: SolveResult | None = None
        state_holder: dict[str, Any] = {}
        preflight_remaining = min(20.0, max(1.0, float(preflight_budget_seconds)))

        async def _get_baseline() -> SolveResult:
            nonlocal baseline_solve
            if baseline_solve is None:
                budget = (
                    min(2.5, remaining_seconds(deadline, clock=self.clock))
                    if deadline is not None
                    else 2.5
                )
                baseline_solve = await asyncio.to_thread(
                    solve_min_steps, grid, deadline_seconds=budget, clock=self.clock
                )
            return baseline_solve

        response_model = select_binary_discovery_schema(
            needs_initial_requirements=verified_initial_requirements is None,
            can_ask=question_count < 3,
        )

        async def _validate(parsed: StrictSchema) -> StrictSchema:
            nonlocal preflight_remaining
            raw = _normalize_binary_payload_dict(parsed.model_dump(by_alias=True))
            try:
                step = parse_binary_step(raw, question_count)
            except BinaryError as exc:
                raise LLMValidationError(
                    exc.code,
                    f"Binary step invalid ({exc.field}): {exc.detail or exc.code}",
                ) from exc

            if step["action"] == "protected_refusal":
                state_holder["step"] = step
                state_holder["initial_requirements"] = None
                state_holder["candidates"] = ()
                return parsed

            if step["action"] == "blocked":
                note_text = step["note"]
                issues = verify_grounding(note_text, grid, entity_bindings)
                if issues:
                    raise LLMValidationError(
                        "BINARY_TEXT_INVALID",
                        f"note failed grounding check: {issues[0].message}",
                    )
                lang_issues = validate_visible_language(note_text, locale)
                if lang_issues:
                    raise LLMValidationError(
                        "INTERNAL_TOKEN_LEAK",
                        f"note leaked internal token: {lang_issues[0]}",
                    )
                step["note"] = sanitize_visible_text(note_text, locale)
                state_holder["step"] = step
                state_holder["initial_requirements"] = (
                    dict(verified_initial_requirements)
                    if verified_initial_requirements is not None
                    else None
                )
                state_holder["candidates"] = ()
                return parsed

            for idx, branch in enumerate(step["branches"], start=1):
                if binary_signature(branch["promise"]) in prior_sigs:
                    raise LLMValidationError(
                        "BINARY_ALREADY_SELECTED_DIRECTION",
                        (
                            f"Branch {idx} ({branch['label']!r}) repeats an already selected "
                            f"promise {branch['promise']}; propose a new materially distinct "
                            "direction."
                        ),
                    )

            if verified_initial_requirements is None:
                if step["initialRequirements"] is None:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_REQUIRED",
                        "initialRequirements must be non-null until verified.",
                    )
                try:
                    normalized_req, _ = normalize_requirement_record(
                        step["initialRequirements"],
                        list(initial_designer_turns)[:1],
                        snapshot,
                        design_context=design_context,
                    )
                    initial_req = validate_requirement_record(
                        normalized_req, list(initial_designer_turns)[:1], snapshot
                    )
                except ValueError as exc:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_INVALID", str(exc)
                    ) from exc
                preflight_issues = preflight_requirements(grid.rows, initial_req)
                if preflight_issues:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_INVALID",
                        (
                            "initialRequirements conflict with map permissions: "
                            f"{format_preflight_issues(preflight_issues)}"
                        ),
                    )
            else:
                if step["initialRequirements"] is not None:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_LOCKED",
                        "initialRequirements must be omitted/null once already verified.",
                    )
                initial_req = dict(verified_initial_requirements)

            labeled_texts: list[tuple[str, str]] = [
                ("question", step["question"]),
                ("note", step["note"]),
                *(
                    (f"branches[{idx}].label", b["label"])
                    for idx, b in enumerate(step["branches"])
                ),
            ]
            for field_name, label_text in labeled_texts:
                if not label_text:
                    continue
                issues = verify_grounding(label_text, grid, entity_bindings)
                if issues:
                    raise LLMValidationError(
                        "BINARY_TEXT_INVALID",
                        f"{field_name} failed grounding check: {issues[0].message}",
                    )
                lang_issues = validate_visible_language(label_text, locale)
                if lang_issues:
                    raise LLMValidationError(
                        "INTERNAL_TOKEN_LEAK",
                        f"{field_name} leaked internal token: {lang_issues[0]}",
                    )

            candidates: list[BranchPreflightCandidate] = []
            if step["action"] == "ask":
                for idx, branch in enumerate(step["branches"], start=1):
                    eff_strat = (
                        merge_revision_strategies([*prior_strategies, branch["strategy"]])
                        if prior_strategies
                        else branch["strategy"]
                    )
                    _check_branch_promise_precondition(
                        grid.rows,
                        outer_shell,
                        branch_index=idx,
                        promise=branch["promise"],
                        strategy=eff_strat,
                    )
                base_solve = await _get_baseline()
                if preflight_remaining <= 0.0:
                    raise LLMValidationError(
                        "BINARY_PREFLIGHT_NOT_FOUND",
                        "Deterministic preflight budget exhausted across retries.",
                    )
                pf_start = self.clock.monotonic()
                preflight_deadline = pf_start + preflight_remaining
                if deadline is not None:
                    preflight_deadline = min(preflight_deadline, deadline)
                try:
                    per_branch = max(0.5, (preflight_deadline - self.clock.monotonic()) / 2.0)

                    for idx, branch in enumerate(step["branches"], start=1):
                        strategy_for_search = (
                            merge_revision_strategies([*prior_strategies, branch["strategy"]])
                            if prior_strategies
                            else branch["strategy"]
                        )
                        plan = parse_revision_plan({"strategies": [strategy_for_search]})
                        cumulative_promises = [*prior_promises, branch["promise"]]
                        branch_deadline = min(
                            preflight_deadline, self.clock.monotonic() + per_branch
                        )
                        promise_outcomes_holder: dict[str, list[dict[str, Any]]] = {}

                        def _branch_validator(
                            rows: list[str],
                            *,
                            _promises: list[dict[str, Any]] = cumulative_promises,
                            _holder: dict[str, list[dict[str, Any]]] = promise_outcomes_holder,
                            _b_deadline: float = branch_deadline,
                        ) -> SolveResult:
                            rem = remaining_seconds(_b_deadline, clock=self.clock)
                            solved = solve_min_steps(
                                rows, deadline_seconds=min(2.0, rem), clock=self.clock
                            )
                            outcomes = check_promises(
                                grid.rows, rows, _promises, base_solve, solved
                            )
                            _holder["outcomes"] = outcomes
                            return solved

                        try:
                            res = await asyncio.to_thread(
                                search_revision_plan,
                                grid.rows,
                                plan,
                                _branch_validator,
                                baseline_metrics=base_solve.as_dict(),
                                deadline=branch_deadline,
                                maximum_valid_candidates=1,
                                monotonic_fn=self.clock.monotonic,
                            )
                        except (
                            ProposalSearchExhausted,
                            BinaryError,
                            TimeoutError,
                            ValueError,
                        ) as exc:
                            detail = str(exc)
                            advice = ""
                            if isinstance(exc, ProposalSearchExhausted):
                                reasons = (exc.diagnostics or {}).get("failureReasons")
                                if reasons:
                                    detail = f"{detail} failureReasons={reasons}"
                                    if (
                                        isinstance(reasons, Mapping)
                                        and "BINARY_PROMISE_NOT_REALIZED" in reasons
                                    ) or (
                                        isinstance(reasons, str)
                                        and "BINARY_PROMISE_NOT_REALIZED" in reasons
                                    ):
                                        p_kind = str(branch["promise"].get("kind") or "")
                                        if p_kind == "reduce_obstacles":
                                            alt_hint = (
                                                "switching to 'shorter_solution' or 'reshape_water'"
                                            )
                                        elif p_kind == "shorter_solution":
                                            alt_hint = (
                                                "switching to 'reduce_obstacles' or 'reshape_water'"
                                            )
                                        else:
                                            alt_hint = (
                                                "switching to 'reduce_obstacles' or "
                                                "'shorter_solution'"
                                            )
                                        advice = (
                                            " Try broadening/aligning strategy.focus and "
                                            f"promise.focus (or focus=null), increasing "
                                            f"editBudget, {alt_hint}, or returning "
                                            "action='blocked' if the request is "
                                            "contradictory/impossible."
                                        )
                            raise LLMValidationError(
                                "BINARY_PREFLIGHT_NOT_FOUND",
                                (
                                    f"Branch {idx} (promise={branch['promise']['kind']}/"
                                    f"{branch['promise']['component']}) failed deterministic "
                                    f"preflight: {detail}{advice}"
                                ),
                            ) from exc

                        candidates.append(
                            BranchPreflightCandidate(
                                branch_index=idx,
                                label=sanitize_visible_text(branch["label"], locale),
                                rows=res.rows,
                                solve_result=res.validation,
                                promise_outcomes=tuple(
                                    promise_outcomes_holder.get("outcomes", ())
                                ),
                                strategy=branch["strategy"],
                            )
                        )
                finally:
                    preflight_remaining = max(
                        0.0,
                        preflight_remaining - max(0.0, self.clock.monotonic() - pf_start),
                    )

            step["question"] = sanitize_visible_text(step["question"], locale)
            step["note"] = sanitize_visible_text(step["note"], locale)
            for branch in step["branches"]:
                branch["label"] = sanitize_visible_text(branch["label"], locale)

            state_holder["step"] = step
            state_holder["initial_requirements"] = initial_req
            state_holder["candidates"] = tuple(candidates)
            return parsed

        now_mono = self.clock.monotonic()
        if deadline is not None:
            rem_total = deadline - now_mono
            if rem_total > 25.0:
                model_deadline: float | None = deadline - 20.0
                attempt_timeout = min(70.0, max(5.0, model_deadline - now_mono))
            else:
                model_deadline = deadline
                attempt_timeout = min(70.0, max(1.0, rem_total))
        else:
            model_deadline = None
            attempt_timeout = 70.0

        result = await call_structured(
            self.transport,
            task="binary_discovery",
            model=self.model,
            messages=messages,
            response_model=response_model,
            validator=_validate,
            deadline=model_deadline,
            max_attempts=max_attempts,
            temperature=0.6,
            max_tokens=4096,
            attempt_timeout_seconds=attempt_timeout,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        step = state_holder["step"]
        refusal = (
            render_protected_refusal(step["protectedEntities"], initial_request, locale)
            if step["action"] == "protected_refusal"
            else None
        )
        return BinaryDiscoveryResult(
            step=step,
            initial_requirements=state_holder.get("initial_requirements"),
            branch_candidates=state_holder.get("candidates", ()),
            refusal_message=refusal,
            attempts_used=result.attempts_used,
            request_id=result.request_id,
            latency_ms=result.latency_ms,
            usage=result.usage,
        )
