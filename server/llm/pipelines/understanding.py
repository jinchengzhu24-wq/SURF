"""Native-async pipeline for Policy-2 turn routing, manual edit review, and challenge review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.grid import Grid, parse_structural_grid
from server.core.llm_client import LLMTransport, LLMValidationError, call_structured
from server.i18n.messages import normalize_language
from server.llm.grounding import (
    build_stage_facts,
    sanitize_visible_text,
    validate_visible_language,
    verify_grounding,
)
from server.llm.prompts.understanding import (
    build_challenge_reason_messages,
    build_manual_edit_conflict_messages,
    build_manual_edit_review_messages,
    build_turn_routing_messages,
)
from server.llm.schemas import (
    ChallengeReasonReviewPayload,
    ManualEditConflictClassificationPayload,
    ManualEditReviewPayload,
    TurnRoutingPayload,
)

_PROTECTED_COMPONENTS = frozenset({"outer_shell", "player", "boxes", "targets"})


@dataclass(frozen=True, slots=True)
class TurnRoutingResult:
    """Validated lightweight routing output with deterministic protected-edit check."""

    payload: TurnRoutingPayload
    protected_change_requested: bool
    protected_entities: tuple[str, ...]
    attempts_used: int
    request_id: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ManualEditConflictResult:
    """Frozen conflict adjudication over confirmed directions and concrete edit evidence."""

    verdict: str
    decision_source: str
    confirmed_direction_ids: tuple[str, ...]
    effect_evidence_ids: tuple[str, ...]
    comparisons: tuple[dict[str, Any], ...]
    attempts_used: int = 0
    request_id: str = ""
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class ManualEditReviewResult:
    """Validated opening + comparison review + optional LET'S DISCUSS conflict card."""

    opening_message: str
    assessment: dict[str, Any]
    review_message: str
    conflict: dict[str, Any] | None
    attempts_used: int
    request_id: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChallengeReasonResult:
    """Structured classification of a player's challenge reason."""

    relation: str
    merit: str
    comparison: str
    attempts_used: int
    request_id: str
    latency_ms: int


class UnderstandingPipeline:
    """Native-async understanding, routing, manual-edit review, and challenge review pipeline."""

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

    async def route_turn(
        self,
        latest_user_message: str,
        *,
        previous_user_messages: Sequence[str] = (),
        active_disagreement: Mapping[str, Any] | None = None,
        has_active_proposal: bool = False,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> TurnRoutingResult:
        """Run Policy-2 lightweight routing (15s cap) with verbatim evidenceSpan checks."""
        latest = str(latest_user_message or "").strip()
        if not latest:
            raise ValueError("A non-empty user message is required for turn routing.")

        active_human_edit = (
            isinstance(active_disagreement, Mapping)
            and active_disagreement.get("subject") == "human_edit"
        )
        messages = build_turn_routing_messages(
            latest_user_message=latest,
            previous_user_messages=previous_user_messages,
            active_disagreement=active_disagreement,
            has_active_proposal=has_active_proposal,
        )

        def _validate(parsed: TurnRoutingPayload) -> TurnRoutingPayload:
            if parsed.evidence_span and parsed.evidence_span not in latest:
                raise LLMValidationError(
                    "EVIDENCE_SPAN_MISMATCH", "evidenceSpan must be verbatim from latest message."
                )
            for change in parsed.changes:
                if not change.evidence_span or change.evidence_span not in latest:
                    raise LLMValidationError(
                        "CHANGE_EVIDENCE_MISMATCH", "Each change needs a verbatim evidenceSpan."
                    )
            if active_human_edit and parsed.human_edit_engagement is not None:
                eng = parsed.human_edit_engagement
                if eng.decision == "engage" and (
                    not eng.evidence_span or eng.evidence_span not in latest
                ):
                    raise LLMValidationError(
                        "ENGAGEMENT_EVIDENCE_MISMATCH", "engage requires a verbatim evidenceSpan."
                    )
            return parsed

        result = await call_structured(
            self.transport,
            task="turn_routing",
            model=self.model,
            messages=messages,
            response_model=TurnRoutingPayload,
            validator=_validate,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.0,
            max_tokens=450,
            attempt_timeout_seconds=15.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        protected_hits = tuple(
            dict.fromkeys(
                c.component
                for c in result.parsed.changes
                if c.operation == "change" and c.component in _PROTECTED_COMPONENTS
            )
        )
        return TurnRoutingResult(
            result.parsed, bool(protected_hits), protected_hits,
            result.attempts_used, result.request_id, result.latency_ms, result.usage,
        )

    async def classify_manual_edit_conflict(
        self,
        *,
        directions: Sequence[Mapping[str, Any]],
        effect_evidence: Sequence[Mapping[str, Any]],
        language: str = "en",
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> ManualEditConflictResult:
        """Adjudicate whether a verified manual edit conflicts with confirmed directions."""
        effect_ids = tuple(str(e["id"]) for e in effect_evidence if e.get("id"))
        expected_ids = [str(d["id"]) for d in directions if d.get("id")]
        if not expected_ids or not effect_ids:
            return ManualEditConflictResult(
                verdict="unrelated",
                decision_source="deterministic_no_comparable_evidence",
                confirmed_direction_ids=(),
                effect_evidence_ids=(),
                comparisons=(),
                request_id=request_id,
            )

        expected_set = set(expected_ids)
        messages = build_manual_edit_conflict_messages(
            directions=directions, effect_evidence=effect_evidence, language=language
        )

        def _validate(
            parsed: ManualEditConflictClassificationPayload,
        ) -> ManualEditConflictClassificationPayload:
            returned = [item.direction_id for item in parsed.comparisons]
            if len(returned) != len(expected_ids) or set(returned) != expected_set:
                raise LLMValidationError(
                    "DIRECTION_SLOTS_MISMATCH", "Cover every supplied directionId exactly once."
                )
            return parsed

        result = await call_structured(
            self.transport,
            task="manual_edit_conflict_classification",
            model=self.model,
            messages=messages,
            response_model=ManualEditConflictClassificationPayload,
            validator=_validate,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.0,
            max_tokens=1400,
            attempt_timeout_seconds=25.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        comparisons = tuple(
            {
                "confirmedDirectionId": item.direction_id,
                "relation": item.relation,
                "evidenceIds": list(effect_ids),
                "explanation": item.explanation.strip()[:800],
            }
            for item in result.parsed.comparisons
        )
        conflicts = [c for c in comparisons if c["relation"] == "conflict"]
        verdict = (
            "conflict"
            if conflicts
            else "unclear"
            if any(c["relation"] == "unclear" for c in comparisons)
            else "no_conflict"
        )
        relevant = conflicts or list(comparisons)
        return ManualEditConflictResult(
            verdict, "llm_adjudication",
            tuple(c["confirmedDirectionId"] for c in relevant), effect_ids,
            comparisons, result.attempts_used, result.request_id, result.latency_ms,
        )

    async def generate_manual_edit_review(
        self,
        *,
        grid_or_rows: Grid | Sequence[str],
        conflict_decision: ManualEditConflictResult,
        review_context: Mapping[str, Any],
        language: str = "en",
        entity_bindings: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> ManualEditReviewResult:
        """Generate grounded manual-edit opening + comparison + optional LET'S DISCUSS card."""
        locale = normalize_language(language)
        grid = parse_structural_grid(grid_or_rows)
        requires_conflict = conflict_decision.verdict == "conflict"
        messages = build_manual_edit_review_messages(
            stage_snapshot=build_stage_facts(grid, entity_bindings).to_prompt_dict(),
            conflict_verdict=conflict_decision.verdict,
            review_context=review_context,
            language=locale,
        )

        def _validate(parsed: ManualEditReviewPayload) -> ManualEditReviewPayload:
            if requires_conflict and parsed.conflict is None:
                raise LLMValidationError("CONFLICT_CARD_REQUIRED", "conflict card is required.")
            if not requires_conflict and parsed.conflict is not None:
                raise LLMValidationError("CONFLICT_CARD_FORBIDDEN", "conflict must be null.")
            review_clean = sanitize_visible_text(parsed.review_message, locale)
            opening_clean = sanitize_visible_text(parsed.opening_message, locale)
            if review_clean.count("?") + review_clean.count("？") > 0:
                raise LLMValidationError("REVIEW_MUST_BE_DECLARATIVE", "No questions in review.")
            texts = [opening_clean, review_clean]
            if parsed.conflict is not None:
                nq = parsed.conflict.next_question
                if nq.count("?") + nq.count("？") != 1:
                    raise LLMValidationError("CONFLICT_QUESTION_COUNT", "Need 1 nextQuestion.")
                texts.extend(
                    (
                        parsed.conflict.user_position,
                        parsed.conflict.ai_position,
                        parsed.conflict.core_disagreement,
                        nq,
                    )
                )
            for text in texts:
                issues = verify_grounding(text, grid, entity_bindings)
                if issues:
                    raise LLMValidationError("GROUNDING_MISMATCH", issues[0].message)
                leaks = validate_visible_language(text, locale)
                if leaks:
                    raise LLMValidationError("INTERNAL_TOKEN_LEAK", leaks[0])
            return parsed

        result = await call_structured(
            self.transport,
            task="manual_edit_review",
            model=self.model,
            messages=messages,
            response_model=ManualEditReviewPayload,
            validator=_validate,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.3,
            max_tokens=2600,
            attempt_timeout_seconds=55.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        conflict_dict: dict[str, Any] | None = None
        if result.parsed.conflict is not None:
            c = result.parsed.conflict
            conflict_dict = {
                "status": "active",
                "subject": "human_edit",
                "userPosition": sanitize_visible_text(c.user_position, locale),
                "aiPosition": sanitize_visible_text(c.ai_position, locale),
                "coreDisagreement": sanitize_visible_text(c.core_disagreement, locale),
                "nextQuestion": sanitize_visible_text(c.next_question, locale),
                "resolution": None,
            }
        return ManualEditReviewResult(
            sanitize_visible_text(result.parsed.opening_message, locale),
            result.parsed.assessment.model_dump(by_alias=True),
            sanitize_visible_text(result.parsed.review_message, locale),
            conflict_dict,
            result.attempts_used,
            result.request_id,
            result.latency_ms,
            result.usage,
        )

    async def review_challenge_reason(
        self,
        *,
        user_reason: str,
        proposal_summary: str,
        primary_hypothesis: str,
        secondary_hypothesis: str,
        language: str = "en",
        deadline: float | None = None,
        request_id: str = "",
        max_attempts: int = 2,
    ) -> ChallengeReasonResult:
        """Classify a designer's challenge reason against an active proposal."""
        locale = normalize_language(language)
        messages = build_challenge_reason_messages(
            user_reason=user_reason,
            proposal_summary=proposal_summary,
            primary_hypothesis=primary_hypothesis,
            secondary_hypothesis=secondary_hypothesis,
            language=locale,
        )
        result = await call_structured(
            self.transport,
            task="challenge_reason_review",
            model=self.model,
            messages=messages,
            response_model=ChallengeReasonReviewPayload,
            deadline=deadline,
            max_attempts=max_attempts,
            temperature=0.0,
            max_tokens=500,
            attempt_timeout_seconds=18.0,
            extra_body=self.extra_body,
            clock=self.clock,
            request_id=request_id,
        )
        return ChallengeReasonResult(
            result.parsed.relation,
            result.parsed.merit,
            sanitize_visible_text(result.parsed.comparison, locale),
            result.attempts_used,
            result.request_id,
            result.latency_ms,
        )
