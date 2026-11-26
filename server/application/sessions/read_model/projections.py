"""Pure turn guidance, intent state, and proposal state projection helpers."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from server.core.grid import map_fingerprint
from server.domain.sessions import to_public_guidance
from server.i18n.messages import get_message
from server.llm.grounding import extract_coordinate_links, sanitize_visible_text
from server.persistence.models import ConversationTurnRecord, LevelVersionRecord


def clone_json_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def sanitize_turn_guidance(
    raw_guidance: Mapping[str, Any] | None,
    body: str,
    rows: list[str],
    entity_bindings: Mapping[str, Any] | None,
    *,
    include_dashboard_node: bool = True,
) -> dict[str, Any]:
    """Sanitize turn guidance via canonical PublicTurnGuidance and recheck coordinate links."""
    guidance = to_public_guidance(raw_guidance, include_dashboard_node=include_dashboard_node)
    if rows:
        try:
            links = extract_coordinate_links(
                body, rows, guidance.get("coordinateLinks"), entity_bindings
            )
        except ValueError:
            links = []
    else:
        links = []
    if links:
        guidance["coordinateLinks"] = links
    else:
        guidance.pop("coordinateLinks", None)
    return guidance


def public_turn_content(turn: ConversationTurnRecord, *, stage_number: int | None) -> str:
    _ = stage_number
    if turn.role != "assistant":
        return turn.content
    guidance = turn.guidance or {}
    disagreement = guidance.get("disagreement") or {}
    if (
        isinstance(disagreement, Mapping)
        and disagreement.get("subject") == "ai_revision_challenge"
        and disagreement.get("phase") == "choice_pending"
        and disagreement.get("status") == "active"
        and guidance.get("challengeCardSchemaVersion") != 2
    ):
        return get_message("challenge.scope_choice_legacy", turn.language)
    return sanitize_visible_text(turn.content, turn.language)


def build_intent_state(
    turn: ConversationTurnRecord,
    *,
    session_status: str,
    current_version_id: str | None,
    deadline_expired: bool,
    version_context: Mapping[str, Any],
    is_latest_intent_turn: bool,
    translated_statements: Mapping[tuple[str, str], str],
    raw_content_by_turn: Mapping[str, str],
    translated_content_by_turn: Mapping[tuple[str, str], str],
) -> dict[str, Any] | None:
    guidance = turn.guidance or {}
    hypothesis_id = guidance.get("_intentHypothesisId")
    if turn.role != "assistant" or not hypothesis_id:
        return None
    hypothesis = next(
        (
            item
            for item in version_context.get("intentHypotheses", [])
            if item.get("id") == hypothesis_id
        ),
        None,
    )
    if hypothesis is None:
        return None
    status = str(hypothesis.get("status") or "tentative")
    review = guidance.get("_intentReviewState") or {}
    actionable = bool(
        is_latest_intent_turn
        and status == "tentative"
        and turn.version_id == current_version_id
        and session_status == "active"
        and not deadline_expired
    )
    review_mode = str(review.get("interactionMode") or "full")
    mode = (
        "conflict_choice"
        if review_mode == "conflict_choice"
        else review_mode if actionable else "resolved"
    )
    result: dict[str, Any] = {
        "hypothesisId": hypothesis_id,
        "status": status,
        "interactionMode": mode,
        "actionable": actionable,
        "resolvedStatement": (
            hypothesis.get("statement") if status in {"confirmed", "superseded"} else None
        ),
        "reviewExplanation": review.get("reviewExplanation") if actionable else None,
    }
    if review_mode == "conflict_choice":
        existing_id = str(review.get("existingHypothesisId") or "").strip()
        existing = next(
            (
                item
                for item in version_context.get("intentHypotheses", [])
                if item.get("id") == existing_id
            ),
            None,
        )
        if existing is not None:
            selected_id: str | None = None
            if status == "confirmed" and existing.get("status") == "superseded":
                selected_id = hypothesis_id
            elif status == "rejected" and existing.get("status") == "confirmed":
                selected_id = existing_id

            def _evidence_text(hyp: Mapping[str, Any]) -> str | None:
                for claim in hyp.get("semanticClaims") or []:
                    tid = claim.get("sourceUserTurnId")
                    if tid and (tid, turn.language) in translated_content_by_turn:
                        return translated_content_by_turn[(tid, turn.language)]
                    if tid and tid in raw_content_by_turn:
                        return raw_content_by_turn[tid]
                return None

            def _statement(hyp: Mapping[str, Any]) -> str:
                hid = str(hyp.get("id") or "")
                return (
                    translated_statements.get((hid, turn.language))
                    or (
                        hyp.get("displayStatement")
                        if hyp.get("displayLanguage") == turn.language
                        and hyp.get("displayStatement")
                        else None
                    )
                    or str(hyp.get("statement") or "")
                )

            candidate_stmt = str(review.get("candidateText") or "").strip()
            result["conflictChoice"] = {
                "explanation": str(review.get("reviewExplanation") or "").strip(),
                "selectedHypothesisId": selected_id,
                "options": [
                    {
                        "role": "new",
                        "hypothesisId": hypothesis_id,
                        "statement": candidate_stmt or _statement(hypothesis),
                        "status": status,
                        "evidenceText": _evidence_text(hypothesis),
                    },
                    {
                        "role": "existing",
                        "hypothesisId": existing_id,
                        "statement": _statement(existing),
                        "status": existing.get("status", "confirmed"),
                        "evidenceText": _evidence_text(existing),
                    },
                ],
            }
    return result


def build_proposal_state(
    turn: ConversationTurnRecord,
    *,
    session_status: str,
    current_version_id: str | None,
    deadline_expired: bool,
    versions_by_id: Mapping[str, LevelVersionRecord],
    contexts_by_version: Mapping[str, dict[str, Any]],
    latest_revision_turn_by_version: Mapping[str, str],
) -> dict[str, Any] | None:
    guidance = turn.guidance or {}
    offer = guidance.get("proposalOffer")
    if (
        turn.role != "assistant"
        or not isinstance(offer, Mapping)
        or not str(offer.get("summary") or "").strip()
    ):
        return None
    if session_status != "active" or deadline_expired:
        return {"status": "locked", "actionable": False}
    binding = turn.proposal_binding
    if not isinstance(binding, Mapping) or not isinstance(binding.get("executionBrief"), Mapping):
        return {"status": "unbound", "actionable": False, "reason": "missing_binding"}
    base_version_id = str(binding.get("baseVersionId") or "")
    base_version = versions_by_id.get(base_version_id)
    if base_version is None or base_version_id != current_version_id:
        return {"status": "stale", "actionable": False, "reason": "version_changed"}
    active_dis = (contexts_by_version.get(base_version_id) or {}).get("activeDisagreement") or {}
    if active_dis.get("status") == "active":
        return {
            "status": "disagreement_active",
            "actionable": False,
            "reason": "disagreement_active",
        }
    rows = base_version.rows
    expected_fp = binding.get("mapFingerprint")
    if expected_fp and map_fingerprint(rows) != expected_fp:
        return {"status": "stale", "actionable": False, "reason": "map_changed"}
    expected_bfp = binding.get("entityBindingFingerprint")
    actual_bfp = (base_version.entity_bindings or {}).get("bindingFingerprint")
    if not expected_bfp or not actual_bfp or expected_bfp != actual_bfp:
        return {"status": "stale", "actionable": False, "reason": "entity_binding_changed"}
    brief = binding["executionBrief"]
    transitions = brief.get("requiredTransitions")
    if not isinstance(transitions, list) or not transitions:
        return {"status": "unbound", "actionable": False, "reason": "missing_exact_transitions"}
    for t in transitions:
        r, c = t.get("row"), t.get("column")
        if (
            not isinstance(r, int)
            or not isinstance(c, int)
            or not (1 <= r <= len(rows))
            or not (1 <= c <= len(rows[r - 1]))
        ):
            return {"status": "stale", "actionable": False, "reason": "invalid_binding"}
        actual = rows[r - 1][c - 1]
        if actual == t.get("to"):
            return {
                "status": "already_satisfied",
                "actionable": False,
                "reason": "already_satisfied",
            }
        if actual != t.get("from"):
            return {"status": "stale", "actionable": False, "reason": "precondition_failed"}
    b_status = binding.get("status")
    if b_status == "already_satisfied":
        return {"status": "already_satisfied", "actionable": False, "reason": "already_satisfied"}
    if b_status == "stale":
        return {"status": "stale", "actionable": False, "reason": "proposal_consumed_or_stale"}
    if b_status == "challenged":
        return {"status": "challenged", "actionable": False, "reason": "proposal_challenged"}
    is_latest = latest_revision_turn_by_version.get(turn.version_id) == turn.turn_id
    return {
        "status": "active",
        "actionable": (b_status or "active") in {"active", "ready"}
        and is_latest
        and turn.version_id == current_version_id,
        "reason": None,
    }
