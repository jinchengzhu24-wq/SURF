"""Turn guidance, translation, and audit indexing helpers for SessionReadModelService."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from server.persistence.models import (
    AuditEventRecord,
    ConversationTurnRecord,
    DesignSessionRecord,
)

from .projections import (
    build_intent_state,
    clone_json_dict,
    sanitize_turn_guidance,
)


def index_audit_events(events: list[AuditEventRecord]) -> dict[str, Any]:
    """Index audit events by kind/entity for zero-N+1 read projection."""
    proposal_request_keys: set[str] = set()
    execution_outcomes: dict[str, dict[str, Any]] = {}
    intent_evidence: dict[str, dict[str, Any]] = {}
    intent_feedback: dict[tuple[str, str], dict[str, Any]] = {}
    rewrites: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in events:
        p = ev.payload
        if ev.event_type == "proposal_request_requested" and p.get("messageKey"):
            proposal_request_keys.add(str(p["messageKey"]))
        elif ev.event_type == "revision_execution_outcome" and p.get("versionId"):
            execution_outcomes[str(p["versionId"])] = dict(p)
        elif ev.event_type == "intent_evidence_recorded" and p.get("evidenceId"):
            intent_evidence[str(p["evidenceId"])] = {
                **p,
                "auditId": ev.id,
                "createdAt": ev.created_at,
            }
        elif ev.event_type == "intent_progress_rewrite":
            atype, aid = p.get("artifactType"), p.get("artifactId")
            if atype and aid:
                rewrites[(str(atype), str(aid))] = dict(p)
        elif ev.event_type in {
            "intent_hypothesis_feedback_applied",
            "intent_hypothesis_feedback_pending",
        }:
            hid, vid = p.get("hypothesisId"), p.get("versionId")
            if hid and vid:
                intent_feedback[(str(vid), str(hid))] = {
                    **p,
                    "eventType": ev.event_type,
                    "createdAt": ev.created_at,
                }
    return {
        "proposal_request_keys": proposal_request_keys,
        "execution_outcomes": execution_outcomes,
        "intent_evidence": intent_evidence,
        "intent_feedback": intent_feedback,
        "rewrites": rewrites,
    }


def index_turn_metadata(
    turns: list[ConversationTurnRecord],
) -> tuple[
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
    dict[str, str],
    dict[str, str],
]:
    """Index translated bodies/intents and latest intent/revision turns per Stage."""
    translated_content: dict[tuple[str, str], str] = {}
    translated_intents: dict[tuple[str, str], str] = {}
    latest_intent: dict[str, str] = {}
    latest_revision: dict[str, str] = {}
    for t in turns:
        g = t.guidance or {}
        if t.role == "assistant" and g.get("_intentHypothesisId"):
            latest_intent[t.version_id] = t.turn_id
        for lang, tr in (t.translations or {}).items():
            if isinstance(tr, Mapping) and tr.get("body"):
                translated_content[(t.turn_id, str(lang))] = str(tr["body"])
            tr_g = tr.get("guidance") if isinstance(tr, Mapping) else None
            tr_stmt = (
                str(tr_g.get("intentHypothesis") or "").strip()
                if isinstance(tr_g, Mapping)
                else ""
            )
            if g.get("_intentHypothesisId") and tr_stmt:
                translated_intents[(str(g["_intentHypothesisId"]), str(lang))] = tr_stmt
    for t in reversed(turns):
        if t.role != "assistant":
            continue
        offer = (t.guidance or {}).get("proposalOffer")
        if isinstance(offer, Mapping) and str(offer.get("summary") or "").strip():
            latest_revision.setdefault(t.version_id, t.turn_id)
    return translated_content, translated_intents, latest_intent, latest_revision


def build_public_guidance_map(
    turns: list[ConversationTurnRecord],
    *,
    session: DesignSessionRecord,
    deadline_expired: bool,
    versions_by_id: Mapping[str, Any],
    contexts_by_version: Mapping[str, dict[str, Any]],
    latest_intent_turn: Mapping[str, str],
    public_content_by_turn: Mapping[str, str],
    translated_intent_statements: Mapping[tuple[str, str], str],
    raw_content_by_turn: Mapping[str, str],
    translated_content_by_turn: Mapping[tuple[str, str], str],
) -> dict[str, dict[str, Any]]:
    """Build sanitized public `guidance` dict for every turn."""
    result: dict[str, dict[str, Any]] = {}
    shown_human_edit: set[str] = set()
    for t in turns:
        ver = versions_by_id.get(t.version_id)
        g = sanitize_turn_guidance(
            t.guidance,
            public_content_by_turn[t.turn_id],
            ver.rows if ver else [],
            ver.entity_bindings if ver else None,
        )
        dis = g.get("disagreement")
        if (
            t.role == "assistant"
            and isinstance(dis, Mapping)
            and dis.get("status") == "active"
            and dis.get("subject") == "human_edit"
        ):
            if t.version_id in shown_human_edit:
                g["disagreement"] = {**dis, "displayCard": False}
            elif dis.get("displayCard") is not False:
                shown_human_edit.add(t.version_id)
        istate = build_intent_state(
            t,
            session_status=session.status,
            current_version_id=session.current_version_id,
            deadline_expired=deadline_expired,
            version_context=contexts_by_version.get(t.version_id, {}),
            is_latest_intent_turn=(latest_intent_turn.get(t.version_id) == t.turn_id),
            translated_statements=translated_intent_statements,
            raw_content_by_turn=raw_content_by_turn,
            translated_content_by_turn=translated_content_by_turn,
        )
        if istate is not None:
            g["intentState"] = istate
        result[t.turn_id] = g
    return result


def project_translations(
    turns: list[ConversationTurnRecord],
    versions_by_id: Mapping[str, Any],
    public_guidance_by_turn: Mapping[str, dict[str, Any]],
    translated_intent_statements: Mapping[tuple[str, str], str],
) -> dict[str, dict[str, Any]]:
    """Sanitize and enrich stored per-language turn translations."""
    result: dict[str, dict[str, Any]] = {}
    for turn in turns:
        ver = versions_by_id.get(turn.version_id)
        rows = ver.rows if ver else []
        bindings = ver.entity_bindings if ver else None
        per_lang: dict[str, Any] = {}
        for lang, raw_tr in (turn.translations or {}).items():
            if not isinstance(raw_tr, Mapping):
                continue
            body = str(raw_tr.get("body") or "")
            tr_g = sanitize_turn_guidance(
                raw_tr.get("guidance") if isinstance(raw_tr.get("guidance"), Mapping) else {},
                body,
                rows,
                bindings,
            )
            src_g = public_guidance_by_turn.get(turn.turn_id, {})
            src_dis = src_g.get("disagreement")
            tr_dis = tr_g.get("disagreement")
            if (
                isinstance(src_dis, Mapping)
                and src_dis.get("subject") == "human_edit"
                and src_dis.get("displayCard") is False
                and isinstance(tr_dis, Mapping)
                and tr_dis.get("subject") == "human_edit"
            ):
                tr_g["disagreement"] = {**tr_dis, "displayCard": False}
            src_intent = src_g.get("intentState")
            if isinstance(src_intent, Mapping):
                copied = clone_json_dict(src_intent)
                cc = copied.get("conflictChoice")
                if isinstance(cc, dict):
                    for opt in cc.get("options", []):
                        hid = str(opt.get("hypothesisId") or "")
                        if (hid, str(lang)) in translated_intent_statements:
                            opt["statement"] = translated_intent_statements[(hid, str(lang))]
                tr_g["intentState"] = copied
            per_lang[str(lang)] = {
                "body": body,
                "guidance": tr_g,
                "proposalSummary": raw_tr.get("proposalSummary"),
                "createdAt": raw_tr.get("createdAt"),
            }
        if per_lang:
            result[turn.turn_id] = per_lang
    return result


def annotate_proposal_questions(
    turns: list[ConversationTurnRecord],
    public_guidance_by_turn: Mapping[str, dict[str, Any]],
    translations_by_turn: Mapping[str, dict[str, Any]],
    topic_state: Mapping[str, Any] | None = None,
    *,
    topics_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    current_version_id: str | None = None,
) -> None:
    """Attach selectedChoiceId/status onto Policy-2 binary discovery proposalQuestion cards."""
    topic_map: dict[str, Mapping[str, Any]] = {}
    if topics_by_id:
        for tid, state in topics_by_id.items():
            if isinstance(state, Mapping):
                topic_map[str(tid)] = state
    latest_topic_id: str | None = None
    if isinstance(topic_state, Mapping) and topic_state.get("topicId"):
        latest_topic_id = str(topic_state["topicId"])
        topic_map.setdefault(latest_topic_id, topic_state)

    for turn in turns:
        turn_guidance = public_guidance_by_turn.get(turn.turn_id)
        if not isinstance(turn_guidance, dict):
            continue
        question = turn_guidance.get("proposalQuestion")
        if not isinstance(question, dict):
            continue
        qid = question.get("questionId")
        q_topic_id = str(question.get("topicId") or "").strip()
        matched_topic = topic_map.get(q_topic_id) if q_topic_id else None
        if matched_topic is None and not q_topic_id and isinstance(topic_state, Mapping):
            matched_topic = topic_state

        topic_choices = list((matched_topic or {}).get("choices") or ())
        selected = next(
            (c.get("choiceId") for c in topic_choices if c.get("questionId") == qid),
            None,
        )
        if not selected:
            persisted_selected = question.get("selectedChoiceId")
            if isinstance(persisted_selected, str) and persisted_selected.strip():
                selected = persisted_selected.strip()

        pending_qid = ((matched_topic or {}).get("pendingQuestion") or {}).get("questionId")
        matched_status = (matched_topic or {}).get("status")
        matched_version_id = (matched_topic or {}).get("versionId")
        is_latest = latest_topic_id is None or q_topic_id == latest_topic_id
        version_matches = (
            current_version_id is None
            or matched_version_id is None
            or matched_version_id == current_version_id
        )
        active = bool(
            matched_topic is not None
            and q_topic_id
            and is_latest
            and version_matches
            and matched_status == "clarifying"
            and pending_qid == qid
        )
        status = "selected" if selected else "pending" if active else "expired"
        turn_guidance["proposalQuestion"] = {
            **question,
            "selectedChoiceId": selected,
            "status": status,
            "sourceLanguage": turn.language,
        }
        for locale, tr in translations_by_turn.get(turn.turn_id, {}).items():
            tr_guidance = tr.get("guidance")
            tr_q = (
                tr_guidance.get("proposalQuestion")
                if isinstance(tr_guidance, dict)
                else None
            )
            if isinstance(tr_q, dict):
                tr_guidance["proposalQuestion"] = {
                    **tr_q,
                    "selectedChoiceId": selected,
                    "status": status,
                    "sourceLanguage": locale,
                }
