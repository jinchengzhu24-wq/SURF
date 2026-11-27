"""Prompt builders for lightweight Policy-2 turn routing, manual edit review, and challenges."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_TURN_ROUTING_SYSTEM_BASE = (
    "You are the Policy-2 lightweight turn router for a Sokoban co-creation workbench. "
    "Classify the latest user message semantically (never by raw keywords) and return ONLY JSON "
    "matching the required schema.\n"
    "Acts may overlap: evaluation (reaction), intent (desired feel without asking for edits), "
    "explanation_request (asks what P/B/T or an element means), idea_request (asks for concepts), "
    "revision_request (explicitly asks for a map plan or edit), challenge (questions a proposal), "
    "unclear. Never promote evaluation, intent, or explanation_request to revision_request.\n"
    "Elements: choose referenced topics from ('water', 'internal_walls', 'outer_shell', 'player', "
    "'boxes', 'targets', 'gameplay', 'unknown'). Set directionSufficient=true when the user's "
    "direction is already specific enough without further narrowing; false when ambiguous.\n"
    "routeTarget: use ONLY 'binary_discovery' for concrete editable proposal/revision requests, "
    "or 'ordinary' for all discussion, evaluation, intent, explanation, or off-topic chat.\n"
    "Permissions: ONLY water and internal_walls are editable; outer_shell, player, boxes, and "
    "targets are fixed. In changes, mark operation='change' ONLY for explicit requests to "
    "add/remove/move/reshape an object; use 'preserve' for locks and 'mention' for landmarks. "
    "Copy every evidenceSpan verbatim from latestUserMessage (evidenceSpan must be non-empty "
    "whenever mapRelated=true, and every changes[].evidenceSpan must be non-empty)."
)

_TURN_ROUTING_HUMAN_EDIT_ADDENDUM = (
    "\nAn active human-edit disagreement is present: you MUST populate humanEditEngagement "
    "with decision in ('engage', 'unchanged', 'unclear'), and when decision='engage' copy a "
    "non-empty verbatim evidenceSpan from latestUserMessage."
)

_HUMAN_EDIT_ENGAGEMENT_RECOVERY_SYSTEM = (
    "Determine whether the designer's latest message engages with the active human-edit "
    "disagreement. Return ONLY JSON matching HumanEditEngagementRecoveryPayload "
    "({'decision': 'engage' | 'unchanged' | 'unclear', 'evidenceSpan': str}).\n"
    "Use 'engage' when the message addresses, responds to, explains, defends, or reconsiders "
    "the manual-edit disagreement, and copy a non-empty verbatim evidenceSpan from "
    "latestUserMessage. Use 'unchanged' when the message moves on without addressing the "
    "disagreement, or 'unclear' when ambiguous."
)

_MANUAL_EDIT_CONFLICT_SYSTEM = (
    "You are the manual-edit conflict adjudicator. Compare the verified manual map diff, solver "
    "delta, and play evidence against each supplied confirmed design direction. Return ONLY JSON "
    "matching the required schema.\n"
    "All coordinates are 1-based (row, column). Tile legend: # wall, . floor, @ water, p player, "
    "s box, t target. Trust verifiedChangeSummary as authoritative. Return one entry for EVERY "
    "supplied directionId exactly once (no missing, extra, or duplicate IDs) with relation in "
    "('conflict', 'aligned', 'tradeoff', 'unrelated', 'unclear'). Use 'conflict' ONLY when the "
    "edit directly reverses a confirmed direction; solver improvements do not erase a reversal."
)

_MANUAL_EDIT_REVIEW_BASE = (
    "You are the Sokoban co-creation evaluator reviewing a newly saved, validated manual edit. "
    "Return ONLY JSON matching the required schema in the requested language.\n"
    "All coordinates are 1-based (row, column) matching stageSnapshot and reviewContext.diff "
    "(verifiedDiff); when mentioning changed cells, cite the exact 1-based row and column from "
    "reviewContext.diff.\n"
    "openingMessage: concise declarative card-free observation of the saved Stage "
    "(no '?' or '？').\n"
    "assessment: populate non-empty solutionSummary, non-empty difficultyOpinion, features, and "
    "suggestions.\n"
    "reviewMessage: declarative comparison of the verified diff against confirmed directions and "
    "lineage (no '?' or '？', no raw evidence IDs, no invented motives when no confirmed direction "
    "exists)."
)

_MANUAL_EDIT_REVIEW_CONFLICT_ADDENDUM = (
    "\nfrozenConflictVerdict is 'conflict': you MUST populate conflict with non-empty "
    "userPosition, aiPosition, coreDisagreement, and nextQuestion containing exactly one "
    "'?' or '？'."
)

_CHALLENGE_REASON_SYSTEM = (
    "Classify the designer's challenge reason against the active proposal and tentative "
    "hypotheses. Return ONLY JSON matching ChallengeReasonReviewPayload with relation "
    "('primary', 'secondary', 'different', 'unclear'), merit ('reasonable', 'not_yet_reasonable', "
    "'unclear'), and a concise non-empty comparison in the requested language (no internal keys)."
)


def _is_active_human_edit(active_disagreement: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(active_disagreement, Mapping)
        and active_disagreement.get("subject") == "human_edit"
        and active_disagreement.get("status", "active") == "active"
    )


def build_turn_routing_messages(
    *,
    latest_user_message: str,
    previous_user_messages: Sequence[str] = (),
    active_disagreement: Mapping[str, Any] | None = None,
    has_active_proposal: bool = False,
) -> list[dict[str, str]]:
    """Build compact Policy-2 turn-routing messages without leaking full StageSnapshot."""
    active_human_edit = _is_active_human_edit(active_disagreement)
    system_content = (
        f"{_TURN_ROUTING_SYSTEM_BASE}{_TURN_ROUTING_HUMAN_EDIT_ADDENDUM}"
        if active_human_edit
        else _TURN_ROUTING_SYSTEM_BASE
    )
    payload: dict[str, Any] = {
        "latestUserMessage": str(latest_user_message),
        "previousUserMessages": [str(m)[:400] for m in list(previous_user_messages)[-2:]],
        "hasActiveProposal": bool(has_active_proposal),
        "editableComponents": ["water", "internal_walls"],
        "protectedComponents": ["outer_shell", "player", "boxes", "targets"],
        "entityLegend": {"P": "player", "B": "boxes", "T": "targets"},
    }
    if active_human_edit and active_disagreement is not None:
        payload["activeHumanEditDisagreement"] = dict(active_disagreement)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_human_edit_engagement_recovery_messages(
    *,
    latest_user_message: str,
    previous_user_messages: Sequence[str] = (),
    active_disagreement: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build focused prompt for recovering human_edit engagement judgment only."""
    payload = {
        "latestUserMessage": str(latest_user_message),
        "previousUserMessages": [str(m)[:400] for m in list(previous_user_messages)[-2:]],
        "activeHumanEditDisagreement": dict(active_disagreement),
    }
    return [
        {"role": "system", "content": _HUMAN_EDIT_ENGAGEMENT_RECOVERY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _is_zero_based_tile_diff(obj: Mapping[str, Any]) -> bool:
    x = obj.get("x")
    y = obj.get("y")
    return (
        "before" in obj
        and "after" in obj
        and isinstance(x, int)
        and not isinstance(x, bool)
        and isinstance(y, int)
        and not isinstance(y, bool)
    )


def _convert_diff_coords_to_one_based(obj: Any) -> Any:
    """Recursively convert raw 0-based tile-diff dicts to 1-based row/column without mutation."""
    if isinstance(obj, Mapping):
        if _is_zero_based_tile_diff(obj):
            converted: dict[str, Any] = {
                "row": obj["y"] + 1,
                "column": obj["x"] + 1,
                "before": _convert_diff_coords_to_one_based(obj["before"]),
                "after": _convert_diff_coords_to_one_based(obj["after"]),
            }
            for key, value in obj.items():
                if key in {"x", "y", "row", "column", "before", "after"}:
                    continue
                converted[key] = _convert_diff_coords_to_one_based(value)
            return converted
        return {key: _convert_diff_coords_to_one_based(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_convert_diff_coords_to_one_based(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_convert_diff_coords_to_one_based(item) for item in obj)
    return obj


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
        "effectEvidence": [_convert_diff_coords_to_one_based(e) for e in effect_evidence],
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
    requires_conflict = conflict_verdict == "conflict"
    system_content = (
        f"{_MANUAL_EDIT_REVIEW_BASE}{_MANUAL_EDIT_REVIEW_CONFLICT_ADDENDUM}"
        if requires_conflict
        else _MANUAL_EDIT_REVIEW_BASE
    )
    payload = {
        "responseLanguage": locale,
        "frozenConflictVerdict": conflict_verdict,
        "stageSnapshot": dict(stage_snapshot),
        "reviewContext": _convert_diff_coords_to_one_based(review_context),
    }
    return [
        {"role": "system", "content": system_content},
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
