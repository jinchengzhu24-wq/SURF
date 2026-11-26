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
    normalize_requirement_record,
    parse_binary_step,
    parse_revision_plan,
    render_protected_refusal,
    search_revision_plan,
    validate_requirement_record,
)
from server.i18n.messages import normalize_language
from server.llm.grounding import (
    build_stage_facts,
    sanitize_visible_text,
    validate_visible_language,
    verify_grounding,
)
from server.llm.prompts.binary import build_binary_discovery_messages
from server.llm.schemas import BinaryDiscoveryPayload

from .proposal import merge_revision_strategies


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

        async def _validate(parsed: BinaryDiscoveryPayload) -> BinaryDiscoveryPayload:
            nonlocal preflight_remaining
            raw = parsed.model_dump(by_alias=True)
            try:
                step = parse_binary_step(raw, question_count)
            except BinaryError as exc:
                raise LLMValidationError(exc.code, f"Binary step invalid: {exc.code}") from exc

            if step["action"] == "protected_refusal":
                state_holder["step"] = step
                state_holder["initial_requirements"] = None
                state_holder["candidates"] = ()
                return parsed

            if any(binary_signature(b["promise"]) in prior_sigs for b in step["branches"]):
                raise LLMValidationError(
                    "BINARY_ALREADY_SELECTED_DIRECTION",
                    "Branch repeats an already selected promise.",
                )

            if verified_initial_requirements is None:
                if step["initialRequirements"] is None:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_REQUIRED",
                        "initialRequirements must be non-null until verified.",
                    )
                try:
                    normalized_req, _ = normalize_requirement_record(
                        step["initialRequirements"], list(initial_designer_turns)[:1], snapshot
                    )
                    initial_req = validate_requirement_record(
                        normalized_req, list(initial_designer_turns)[:1], snapshot
                    )
                except ValueError as exc:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_INVALID", str(exc)
                    ) from exc
            else:
                if step["initialRequirements"] is not None:
                    raise LLMValidationError(
                        "BINARY_INITIAL_REQUIREMENTS_LOCKED",
                        "initialRequirements must be null once already verified.",
                    )
                initial_req = dict(verified_initial_requirements)

            for label_text in (
                step["question"],
                step["note"],
                *(b["label"] for b in step["branches"]),
            ):
                issues = verify_grounding(label_text, grid, entity_bindings)
                if issues:
                    raise LLMValidationError("BINARY_TEXT_INVALID", issues[0].message)
                lang_issues = validate_visible_language(label_text, locale)
                if lang_issues:
                    raise LLMValidationError("INTERNAL_TOKEN_LEAK", lang_issues[0])

            candidates: list[BranchPreflightCandidate] = []
            if step["action"] == "ask":
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
                            raise LLMValidationError(
                                "BINARY_PREFLIGHT_NOT_FOUND",
                                f"Branch {idx} failed deterministic preflight: {exc}",
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
            response_model=BinaryDiscoveryPayload,
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
