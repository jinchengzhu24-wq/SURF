"""Prompt builders for lightweight Policy-2 turn routing, manual edit review, and challenges."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_TURN_ROUTING_SYSTEM = (
    "You are the Policy-2 lightweight turn router for a Sokoban co-creation workbench. "
    "Classify the latest user message semantically (never by raw keywords) and return ONLY JSON "
    "matching TurnRoutingPayload.\n"
    "Acts may overlap: evaluation (reaction), intent (desired feel without asking for edits), "
    "explanation_request (asks what P/B/T or an element means), idea_request (asks for concepts), "
    "revision_request (explicitly asks for a map plan or edit), challenge (questions a proposal), "
    "unclear. Never promote evaluation, intent, or explanation_request to revision_request.\n"
    "routeTarget: 'binary_discovery' for concrete editable proposal requests; 'challenge' when "
    "challenging an active proposal; 'revision' for explicit revision execution; 'ordinary' for "
    "all discussion, evaluation, intent, explanation, or off-topic chat.\n"
    "Permissions: ONLY water and internal_walls are editable; outer_shell, player, boxes, and "
    "targets are fixed. In changes, mark operation='change' ONLY for explicit requests to "
    "add/remove/move/reshape an object; use 'preserve' for locks and 'mention' for landmarks. "
    "Copy every evidenceSpan verbatim from latestUserMessage.\n"
    "If activeHumanEditDisagreement is present, populate humanEditEngagement (engage/unchanged/"
    "unclear with verbatim evidenceSpan for engage); otherwise set humanEditEngagement=null."
)

_MANUAL_EDIT_CONFLICT_SYSTEM = (
    "You are the manual-edit conflict adjudicator. Compare the verified manual map diff, solver "
    "delta, and play evidence against each supplied confirmed design direction. Return ONLY JSON "
    "matching ManualEditConflictClassificationPayload.\n"
    "Tile legend: # wall, . floor, @ water, p player, s box, t target. Trust verifiedChangeSummary "
    "as authoritative. Return one entry per supplied directionId with relation in "
    "('conflict', 'aligned', 'tradeoff', 'unrelated', 'unclear'). Use 'conflict' ONLY when the "
    "edit directly reverses a confirmed direction; solver improvements do not erase a reversal."
)

_MANUAL_EDIT_REVIEW_SYSTEM = (
    "You are the Sokoban co-creation evaluator reviewing a newly saved, validated manual edit. "
    "Return ONLY JSON matching ManualEditReviewPayload in the requested language.\n"
    "openingMessage: concise declarative card-free observation of the saved Stage (no questions).\n"
    "reviewMessage: declarative comparison of the verified diff against confirmed directions and "
    "lineage (no questions, no raw evidence IDs, no invented motives when no confirmed direction "
    "exists).\n"
    "conflict: MUST be non-null (userPosition, aiPosition, coreDisagreement, and exactly one "
    "nextQuestion) if and only if frozenConflictVerdict is 'conflict'; otherwise conflict=null."
)

_CHALLENGE_REASON_SYSTEM = (
    "Classify the designer's challenge reason against the active proposal and tentative "
    "hypotheses. Return ONLY JSON matching ChallengeReasonReviewPayload with relation "
    "('primary', 'secondary', 'different', 'unclear'), merit ('reasonable', 'not_yet_reasonable', "
    "'unclear'), and a concise comparison in the requested language."
)


def build_turn_routing_messages(
    *,
    latest_user_message: str,
    previous_user_messages: Sequence[str] = (),
    active_disagreement: Mapping[str, Any] | None = None,
    has_active_proposal: bool = False,
) -> list[dict[str, str]]:
    """Build compact Policy-2 turn-routing messages without leaking full StageSnapshot."""
    payload = {
        "latestUserMessage": str(latest_user_message),
        "previousUserMessages": [str(m)[:400] for m in list(previous_user_messages)[-2:]],
        "hasActiveProposal": bool(has_active_proposal),
        "activeHumanEditDisagreement": (
            dict(active_disagreement)
            if isinstance(active_disagreement, Mapping)
            and active_disagreement.get("subject") == "human_edit"
            else None
        ),
        "editableComponents": ["water", "internal_walls"],
        "protectedComponents": ["outer_shell", "player", "boxes", "targets"],
        "entityLegend": {"P": "player", "B": "boxes", "T": "targets"},
    }
    return [
        {"role": "system", "content": _TURN_ROUTING_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_manual_edit_conflict_messages(
    *,
    directions: Sequence[Mapping[str, Any]],
    effect_evidence: Sequence[Mapping[str, Any]],
    language: str = "en",
) -> list[dict[str, str]]:
    """Build manual-edit conflict classification messages."""
    locale = normalize_language(language)
    payload = {
        "responseLanguage": locale,
        "directions": [dict(d) for d in directions],
        "effectEvidence": [dict(e) for e in effect_evidence],
    }
    return [
        {"role": "system", "content": _MANUAL_EDIT_CONFLICT_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_manual_edit_review_messages(
    *,
    stage_snapshot: Mapping[str, Any],
    conflict_verdict: str,
    review_context: Mapping[str, Any],
    language: str = "en",
) -> list[dict[str, str]]:
    """Build manual-edit opening + review + optional conflict card messages."""
    locale = normalize_language(language)
    payload = {
        "responseLanguage": locale,
        "frozenConflictVerdict": conflict_verdict,
        "stageSnapshot": dict(stage_snapshot),
        "reviewContext": dict(review_context),
    }
    return [
        {"role": "system", "content": _MANUAL_EDIT_REVIEW_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_challenge_reason_messages(
    *,
    user_reason: str,
    proposal_summary: str,
    primary_hypothesis: str,
    secondary_hypothesis: str,
    language: str = "en",
) -> list[dict[str, str]]:
    """Build challenge-reason review messages passing all user text via JSON."""
    payload = {
        "responseLanguage": normalize_language(language),
        "userReason": str(user_reason),
        "proposalSummary": str(proposal_summary),
        "primaryHypothesis": str(primary_hypothesis),
        "secondaryHypothesis": str(secondary_hypothesis),
    }
    return [
        {"role": "system", "content": _CHALLENGE_REASON_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
