"""State transitions for goals, constraints, hypotheses, decisions, and disagreements."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from server.domain.errors import ConflictError, NotFoundError, ValidationError

from .claims import INTENT_TOPICS, HypothesisStatus, IntentHypothesis, SemanticClaim
from .models import (
    SCHEMA_VERSION,
    Authority,
    Constraint,
    Decision,
    DecisionStatus,
    DesignContextState,
    Disagreement,
    DisagreementStatus,
    Goal,
    GoalStatus,
    ensure_design_context,
)
from .normalization import (
    _clamp_confidence,
    _clean_text,
    _opt_source,
    _stable_id,
    sanitize_user_design_text,
)
from .questions import _merge_open_question, validate_design_context_patch


def _authority_rank(value: str) -> int:
    return {
        Authority.INFERRED.value: 0,
        Authority.EXPLICIT.value: 1,
        Authority.CONFIRMED.value: 2,
    }.get(value, 0)


def _merge_goal_or_constraint(
    items: list[Any],
    model_cls: type[Goal] | type[Constraint],
    field_name: str,
    value: str,
    authority: str,
    stage_id: str | None,
    turn_id: str | None,
    confidence: float = 0.5,
) -> str | None:
    clean = sanitize_user_design_text(value)
    if not clean:
        return None
    normalized = clean.casefold()
    existing = next(
        (
            item
            for item in items
            if item.status == GoalStatus.ACTIVE.value
            and _clean_text(getattr(item, field_name)).casefold() == normalized
        ),
        None,
    )
    if existing is not None:
        if _authority_rank(authority) <= _authority_rank(existing.authority):
            return existing.id
        existing.status = GoalStatus.SUPERSEDED.value
        supersedes_id = existing.id
    else:
        supersedes_id = None

    entry = model_cls.model_validate(
        {
            "id": _stable_id(field_name, clean, stage_id, turn_id),
            field_name: clean,
            "authority": authority,
            "status": GoalStatus.ACTIVE.value,
            "sourceStageId": stage_id,
            "sourceTurnId": turn_id,
            "confidence": confidence,
            "supersedesId": supersedes_id,
        }
    )
    items.append(entry)
    return entry.id


def merge_intent_hypothesis(
    context: DesignContextState | Mapping[str, Any] | None,
    statement: str,
    *,
    topic_key: str = "other",
    evidence_ids: Iterable[str] | None = None,
    stage_id: str | None = None,
    turn_id: str | None = None,
    confidence: float = 0.35,
    displayed: bool = False,
    semantic_claims: Sequence[Mapping[str, Any]] | None = None,
    source_user_turn_id: str | None = None,
    origin: str = "kimi_reviewed",
) -> tuple[DesignContextState, str | None]:
    """Upsert one tentative intent hypothesis without using keyword heuristics."""
    state = ensure_design_context(context)
    clean = sanitize_user_design_text(statement)
    if not clean:
        return state, None

    resolved_topic = topic_key if topic_key in INTENT_TOPICS else "other"
    deduped_evidence = list(
        dict.fromkeys(v for v in (_opt_source(x) for x in (evidence_ids or ())) if v)
    )[:32]

    claims_payload = [
        {
            **claim,
            "sourceUserTurnId": _opt_source(source_user_turn_id)
            or _opt_source(claim.get("sourceUserTurnId")),
        }
        for claim in (semantic_claims or ())
        if isinstance(claim, Mapping)
    ]

    exact = next(
        (
            item
            for item in state.intent_hypotheses
            if item.status == HypothesisStatus.TENTATIVE.value
            and item.statement.casefold() == clean.casefold()
        ),
        None,
    )
    if exact is not None:
        exact.supporting_evidence_ids = list(
            dict.fromkeys([*exact.supporting_evidence_ids, *deduped_evidence])
        )[:32]
        exact.confidence = min(0.75, max(exact.confidence, confidence))
        exact.last_updated_stage_id = _opt_source(stage_id)
        exact.displayed = bool(exact.displayed or displayed)
        parsed_claims = [
            c
            for raw in claims_payload
            if (c := SemanticClaim.from_raw(raw)) is not None
        ][:4]
        if parsed_claims:
            exact.semantic_claims = parsed_claims
        hypothesis_id = exact.id
    else:
        for item in state.intent_hypotheses:
            if item.status == HypothesisStatus.TENTATIVE.value and item.topic_key == resolved_topic:
                item.status = HypothesisStatus.SUPERSEDED.value
        hypothesis_id = _stable_id("hypothesis", resolved_topic, clean, stage_id, turn_id)
        state.intent_hypotheses.append(
            IntentHypothesis.model_validate(
                {
                    "id": hypothesis_id,
                    "topicKey": resolved_topic,
                    "statement": clean,
                    "status": HypothesisStatus.TENTATIVE.value,
                    "confidence": min(0.75, _clamp_confidence(confidence, 0.35)),
                    "supportingEvidenceIds": deduped_evidence,
                    "contradictingEvidenceIds": [],
                    "sourceStageId": stage_id,
                    "sourceTurnId": turn_id,
                    "lastUpdatedStageId": stage_id,
                    "origin": origin,
                    "displayed": displayed,
                    "semanticClaims": claims_payload,
                }
            )
        )

    state.processed_evidence_ids = list(
        dict.fromkeys([*state.processed_evidence_ids, *deduped_evidence])
    )[-128:]
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return DesignContextState.model_validate(state), hypothesis_id


def resolve_intent_hypothesis(
    context: DesignContextState | Mapping[str, Any] | None,
    hypothesis_id: str,
    action: Literal["confirm", "reject", "revise"],
    *,
    candidate_text: str | None = None,
    supersedes_ids: Iterable[str] | None = None,
    evidence_id: str | None = None,
    stage_id: str | None = None,
    turn_id: str | None = None,
    display_statement: str | None = None,
    display_language: str | None = None,
) -> tuple[DesignContextState, str]:
    """Apply explicit designer feedback to one displayed tentative hypothesis."""
    state = ensure_design_context(context)
    target = next(
        (item for item in state.intent_hypotheses if item.id == _opt_source(hypothesis_id)),
        None,
    )
    if target is None:
        raise NotFoundError("INTENT_HYPOTHESIS_NOT_FOUND")
    if target.status != HypothesisStatus.TENTATIVE.value or not target.displayed:
        raise ConflictError("INTENT_HYPOTHESIS_STALE")
    if action not in {"confirm", "reject", "revise"}:
        raise ValidationError("INVALID_INTENT_FEEDBACK")

    if action == "reject":
        target.status = HypothesisStatus.REJECTED.value
        target.confidence = 0.0
        if evidence_id:
            ev = _opt_source(evidence_id)
            if ev and ev not in target.contradicting_evidence_ids:
                target.contradicting_evidence_ids = [*target.contradicting_evidence_ids, ev][:32]
    else:
        original = target.statement
        replacement = (
            sanitize_user_design_text(candidate_text) if action == "revise" else original
        )
        if not replacement or len(replacement) < 4:
            raise ValidationError("INVALID_INTENT_FEEDBACK")
        supersedes = {v for v in (_opt_source(x) for x in (supersedes_ids or ())) if v}
        supersedes.discard(target.id)
        known_confirmed = {
            item.id
            for item in state.intent_hypotheses
            if item.status == HypothesisStatus.CONFIRMED.value
        }
        if not supersedes.issubset(known_confirmed):
            raise ValidationError("INVALID_INTENT_SUPERSEDES")

        superseded_statements: set[str] = set()
        for item in state.intent_hypotheses:
            if item.id in supersedes:
                superseded_statements.add(_clean_text(item.statement).casefold())
                item.status = HypothesisStatus.SUPERSEDED.value
                item.last_updated_stage_id = _opt_source(stage_id)
        for goal in state.user_goals:
            if (
                goal.authority == Authority.CONFIRMED.value
                and goal.status == GoalStatus.ACTIVE.value
                and _clean_text(goal.goal).casefold() in superseded_statements
            ):
                goal.status = GoalStatus.SUPERSEDED.value

        target.original_statement = original if replacement != original else None
        target.statement = replacement
        target.status = HypothesisStatus.CONFIRMED.value
        target.confidence = 1.0
        target.origin = "user_revision" if action == "revise" else target.origin
        target.confirmed_at_stage_id = _opt_source(stage_id)
        target.feedback_action = action
        target.display_statement = sanitize_user_design_text(display_statement) or replacement
        target.display_language = _clean_text(display_language, 16) or None
        if evidence_id:
            ev = _opt_source(evidence_id)
            if ev and ev not in target.supporting_evidence_ids:
                target.supporting_evidence_ids = [*target.supporting_evidence_ids, ev][:32]
        _merge_goal_or_constraint(
            state.user_goals,
            Goal,
            "goal",
            replacement,
            Authority.CONFIRMED.value,
            stage_id,
            turn_id,
            1.0,
        )

    target.last_updated_stage_id = _opt_source(stage_id)
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return DesignContextState.model_validate(state), target.id


def apply_design_context_patch(
    context: DesignContextState | Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
    *,
    explicit_goals: Sequence[str] = (),
    explicit_constraints: Sequence[str] = (),
    user_text: str | None = None,
    stage_id: str | None = None,
    turn_id: str | None = None,
) -> DesignContextState:
    """Apply validated explicit goals/constraints and model patch into DesignContextState."""
    state = ensure_design_context(context)
    validated_patch = validate_design_context_patch(patch) if patch is not None else None

    for goal_text in explicit_goals:
        _merge_goal_or_constraint(
            state.user_goals,
            Goal,
            "goal",
            goal_text,
            Authority.EXPLICIT.value,
            stage_id,
            turn_id,
            1.0,
        )
    for constraint_text in explicit_constraints:
        _merge_goal_or_constraint(
            state.design_constraints,
            Constraint,
            "constraint",
            constraint_text,
            Authority.EXPLICIT.value,
            stage_id,
            turn_id,
            1.0,
        )

    if validated_patch:
        for entry in validated_patch["goals"]:
            _merge_goal_or_constraint(
                state.user_goals,
                Goal,
                "goal",
                entry["goal"],
                Authority.INFERRED.value,
                stage_id,
                turn_id,
                entry.get("confidence", 0.5),
            )
        for entry in validated_patch["constraints"]:
            _merge_goal_or_constraint(
                state.design_constraints,
                Constraint,
                "constraint",
                entry["constraint"],
                Authority.INFERRED.value,
                stage_id,
                turn_id,
                entry.get("confidence", 0.5),
            )
        for entry in validated_patch["openQuestions"]:
            _merge_open_question(state, entry, stage_id, turn_id, user_text)
        targets = {entry.get("targetId") for entry in validated_patch["corrections"]}
        if targets:
            for item in [*state.user_goals, *state.design_constraints]:
                if item.id in targets and item.status == GoalStatus.ACTIVE.value:
                    item.status = GoalStatus.SUPERSEDED.value

    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return DesignContextState.model_validate(state)


def add_confirmed_decision(
    context: DesignContextState | Mapping[str, Any] | None,
    decision: str,
    reason: str,
    stage_id: str | None,
    turn_id: str | None,
    proposal_id: str | None = None,
) -> DesignContextState:
    state = ensure_design_context(context)
    clean_decision = _clean_text(decision)
    if clean_decision:
        for existing in state.confirmed_decisions:
            if (
                existing.status == DecisionStatus.ACTIVE.value
                and existing.decision.casefold() == clean_decision.casefold()
            ):
                existing.status = DecisionStatus.SUPERSEDED.value
        state.confirmed_decisions.append(
            Decision.model_validate(
                {
                    "decision": clean_decision,
                    "reason": reason,
                    "sourceStageId": stage_id,
                    "sourceTurnId": turn_id,
                    "proposalId": proposal_id,
                    "status": DecisionStatus.ACTIVE.value,
                }
            )
        )
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return DesignContextState.model_validate(state)


def add_rejected_decision(
    context: DesignContextState | Mapping[str, Any] | None,
    decision: str,
    reason: str,
    stage_id: str | None,
    turn_id: str | None,
    proposal_id: str | None = None,
) -> DesignContextState:
    state = ensure_design_context(context)
    clean_decision = _clean_text(decision)
    clean_reason = _clean_text(reason)
    if clean_decision:
        duplicate = any(
            existing.decision.casefold() == clean_decision.casefold()
            and existing.reason.casefold() == clean_reason.casefold()
            for existing in state.rejected_decisions
        )
        if not duplicate:
            item_id = _stable_id(
                "rejected",
                clean_decision,
                turn_id,
                len(state.rejected_decisions),
            )
            state.rejected_decisions.append(
                Decision.model_validate(
                    {
                        "id": item_id,
                        "decision": clean_decision,
                        "reason": clean_reason,
                        "sourceStageId": stage_id,
                        "sourceTurnId": turn_id,
                        "proposalId": proposal_id,
                        "status": None,
                    }
                )
            )
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return DesignContextState.model_validate(state)


def set_active_disagreement(
    context: DesignContextState | Mapping[str, Any] | None,
    disagreement: Disagreement | Mapping[str, Any] | None,
    stage_id: str | None = None,
    turn_id: str | None = None,
) -> DesignContextState:
    state = ensure_design_context(context)
    if disagreement is None:
        state.active_disagreement = None
    else:
        parsed = (
            disagreement
            if isinstance(disagreement, Disagreement)
            else Disagreement.model_validate(disagreement)
        )
        state.active_disagreement = (
            parsed if parsed.status == DisagreementStatus.ACTIVE.value else None
        )
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return state


def revision_projection(context: DesignContextState | Mapping[str, Any] | None) -> dict[str, Any]:
    state = ensure_design_context(context)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "activeGoals": [
            item.model_dump(by_alias=True)
            for item in state.user_goals
            if item.status == GoalStatus.ACTIVE.value
            and item.authority in {Authority.EXPLICIT.value, Authority.CONFIRMED.value}
        ],
        "activeConstraints": [
            item.model_dump(by_alias=True)
            for item in state.design_constraints
            if item.status == GoalStatus.ACTIVE.value
            and item.authority in {Authority.EXPLICIT.value, Authority.CONFIRMED.value}
        ],
        "confirmedDecisions": [
            item.model_dump(by_alias=True)
            for item in state.confirmed_decisions
            if item.status == DecisionStatus.ACTIVE.value
        ],
        "rejectedDecisions": [
            item.model_dump(by_alias=True) for item in state.rejected_decisions[-12:]
        ],
        "openQuestions": [q.model_dump(by_alias=True) for q in state.design_level_open_questions()],
        "activeDisagreement": (
            state.active_disagreement.model_dump(by_alias=True)
            if state.active_disagreement is not None
            else None
        ),
    }
