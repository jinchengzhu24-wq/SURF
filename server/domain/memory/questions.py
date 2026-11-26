"""Open-question management and patch validation for DesignContextState."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from .models import (
    MAX_PATCH_ITEMS,
    MAX_TEXT,
    Authority,
    DesignContextState,
    OpenQuestion,
    QuestionStatus,
    ensure_design_context,
)
from .normalization import (
    _clamp_confidence,
    _clean_text,
    _opt_source,
    _stable_id,
    is_design_level_question,
    question_dedup_key,
    sanitize_user_design_text,
)


def validate_design_context_patch(value: Any) -> dict[str, list[dict[str, Any]]] | None:
    """Validate an optional LLM semantic-memory patch without trusting model authority."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("designContextPatch must be an object")

    allowed = {"goals", "constraints", "decisions", "rejections", "openQuestions", "corrections"}
    if set(value) - allowed:
        raise ValueError("designContextPatch contains unknown fields")

    result: dict[str, list[dict[str, Any]]] = {field: [] for field in allowed}
    for field_name in allowed:
        entries = value.get(field_name) or []
        if not isinstance(entries, list) or len(entries) > MAX_PATCH_ITEMS:
            raise ValueError(
                f"designContextPatch.{field_name} must contain at most {MAX_PATCH_ITEMS} items"
            )
        for raw in entries:
            if not isinstance(raw, Mapping):
                raise ValueError(f"designContextPatch.{field_name} contains a non-object")
            if field_name in {"goals", "constraints"}:
                key = "goal" if field_name == "goals" else "constraint"
                clean = _clean_text(raw.get(key))
                if not clean:
                    raise ValueError(f"designContextPatch.{field_name} contains empty text")
                result[field_name].append(
                    {
                        key: clean,
                        "authority": Authority.INFERRED.value,
                        "confidence": _clamp_confidence(raw.get("confidence")),
                        "evidenceText": _clean_text(raw.get("evidenceText"), MAX_TEXT) or None,
                    }
                )
            elif field_name in {"decisions", "rejections"}:
                decision = _clean_text(raw.get("decision"))
                if not decision:
                    raise ValueError(f"designContextPatch.{field_name} contains empty decision")
                result[field_name].append(
                    {
                        "decision": decision,
                        "reason": _clean_text(raw.get("reason")),
                    }
                )
            elif field_name == "openQuestions":
                question = _clean_text(raw.get("question"))
                if not question:
                    raise ValueError("designContextPatch.openQuestions contains empty question")
                status = raw.get("status", QuestionStatus.OPEN.value)
                if status not in {s.value for s in QuestionStatus} | {"resolved"}:
                    raise ValueError("designContextPatch.openQuestions has an invalid status")
                if not is_design_level_question(question, raw.get("evidenceText")):
                    continue
                result[field_name].append(
                    {
                        "question": question,
                        "status": QuestionStatus.ANSWERED.value if status == "resolved" else status,
                        "targetId": _clean_text(raw.get("targetId"), 96) or None,
                        "evidenceText": _clean_text(raw.get("evidenceText"), MAX_TEXT) or None,
                    }
                )
            else:
                target = _clean_text(raw.get("targetId"), 96)
                replacement = _clean_text(raw.get("replacement"))
                if not target or not replacement:
                    raise ValueError(
                        "designContextPatch.corrections requires targetId and replacement"
                    )
                result[field_name].append(
                    {
                        "targetId": target,
                        "replacementType": _clean_text(raw.get("replacementType"), 32),
                        "replacement": replacement,
                        "reason": _clean_text(raw.get("reason")),
                    }
                )
    return result


def _merge_open_question(
    state: DesignContextState,
    entry: Mapping[str, Any],
    stage_id: str | None,
    turn_id: str | None,
    user_text: str | None,
) -> bool:
    question = sanitize_user_design_text(entry.get("question"))
    is_visible = entry.get("sourceKind") == "visible_output"
    is_design = is_design_level_question(question, entry.get("evidenceText"))
    if not question or (not is_visible and not is_design):
        return False

    target_id = _opt_source(entry.get("targetId"))
    q_key = question_dedup_key(question)
    existing = next(
        (
            item
            for item in state.open_questions
            if (target_id and item.id == target_id)
            or (entry.get("sourceKey") and item.source_key == entry.get("sourceKey"))
            or question_dedup_key(item.question) == q_key
        ),
        None,
    )
    status = entry.get("status", QuestionStatus.OPEN.value)

    if status in {"resolved", QuestionStatus.ANSWERED.value}:
        evidence = str(entry.get("evidenceText") or "").strip()
        if not evidence or evidence not in str(user_text or "") or existing is None:
            return False
        existing.status = QuestionStatus.ANSWERED.value
        existing.updated_from_turn_id = _opt_source(turn_id)
        if entry.get("sourceKey"):
            existing.source_key = _clean_text(entry.get("sourceKey"), 192)
        if entry.get("preferQuestion"):
            existing.question = question
        existing.resolved_by_turn_id = _opt_source(turn_id)
        existing.answered_at_stage_id = _opt_source(stage_id)
        return True

    if existing is not None:
        existing.updated_from_turn_id = _opt_source(turn_id)
        if is_visible:
            existing.source_kind = "visible_output"
        if entry.get("preferQuestion"):
            existing.question = question
        return True

    state.open_questions.append(
        OpenQuestion.model_validate(
            {
                "id": _stable_id("question", question, stage_id, turn_id),
                "question": question,
                "status": QuestionStatus.OPEN.value,
                "sourceStageId": stage_id,
                "sourceTurnId": turn_id,
                "updatedFromTurnId": turn_id,
                "sourceKind": _clean_text(entry.get("sourceKind"), 32) or "model_patch",
                "sourceKey": _clean_text(entry.get("sourceKey"), 192) or None,
            }
        )
    )
    return True


def add_open_question(
    context: DesignContextState | Mapping[str, Any] | None,
    question: str,
    stage_id: str | None = None,
    turn_id: str | None = None,
    *,
    source_kind: str = "visible_output",
    source_key: str | None = None,
    prefer_question: bool = False,
) -> DesignContextState:
    state = ensure_design_context(context)
    clean = _clean_text(question)
    if not clean or (source_kind != "visible_output" and not is_design_level_question(clean)):
        return state
    _merge_open_question(
        state,
        {
            "question": clean,
            "status": QuestionStatus.OPEN.value,
            "sourceKind": source_kind,
            "sourceKey": source_key,
            "preferQuestion": prefer_question,
        },
        stage_id,
        turn_id,
        None,
    )
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return state


def apply_question_answer_review(
    context: DesignContextState | Mapping[str, Any] | None,
    answered_question_ids: Iterable[str] | None,
    stage_id: str | None = None,
    turn_id: str | None = None,
) -> DesignContextState:
    state = ensure_design_context(context)
    answered = {v for v in (_opt_source(x) for x in (answered_question_ids or ())) if v}
    if not answered:
        return state
    for item in state.open_questions:
        if item.id in answered and item.status == QuestionStatus.OPEN.value:
            item.status = QuestionStatus.ANSWERED.value
            item.updated_from_turn_id = _opt_source(turn_id)
            item.resolved_by_turn_id = _opt_source(turn_id)
            item.answered_at_stage_id = _opt_source(stage_id)
    state.updated_from_stage_id = _opt_source(stage_id)
    state.updated_from_turn_id = _opt_source(turn_id)
    return state


def apply_question_feedback(
    context: DesignContextState | Mapping[str, Any] | None,
    question_id: str,
    action: Literal["ignore", "restore"],
    stage_id: str | None = None,
    changed_at: str | None = None,
) -> tuple[DesignContextState, bool]:
    state = ensure_design_context(context)
    target = next(
        (item for item in state.open_questions if item.id == _opt_source(question_id)),
        None,
    )
    if target is None:
        return state, False
    if action == "ignore" and target.status == QuestionStatus.OPEN.value:
        target.status = QuestionStatus.IGNORED.value
        target.ignored_at_stage_id = _opt_source(stage_id)
        target.ignored_at = _clean_text(changed_at, 64) or None
    elif action == "restore" and target.status == QuestionStatus.IGNORED.value:
        target.status = QuestionStatus.OPEN.value
        target.ignored_at_stage_id = None
        target.ignored_at = None
    else:
        return state, False
    state.updated_from_stage_id = _opt_source(stage_id)
    return state, True


def deduplicate_open_questions(
    context: DesignContextState | Mapping[str, Any] | None,
    preferred_questions: Iterable[str] | None = None,
) -> DesignContextState:
    state = ensure_design_context(context)
    preferred = {
        question_dedup_key(v): _clean_text(v)
        for v in (preferred_questions or ())
        if question_dedup_key(v)
    }
    merged: list[OpenQuestion] = []
    by_key: dict[tuple[str, str], OpenQuestion] = {}
    status_rank = {
        QuestionStatus.OPEN.value: 0,
        QuestionStatus.IGNORED.value: 1,
        QuestionStatus.ANSWERED.value: 2,
    }
    for item in state.open_questions:
        key = question_dedup_key(item.question)
        identity = ("source", item.source_key) if item.source_key else ("text", key)
        existing = by_key.get(identity)
        if existing is None and key:
            existing = next(
                (cand for cand in merged if question_dedup_key(cand.question) == key),
                None,
            )
        if existing is None:
            merged.append(item)
            by_key[identity] = item
            continue
        if status_rank.get(item.status, 0) > status_rank.get(existing.status, 0):
            existing.status = item.status
            existing.resolved_by_turn_id = item.resolved_by_turn_id
            existing.answered_at_stage_id = item.answered_at_stage_id
            existing.ignored_at_stage_id = item.ignored_at_stage_id
            existing.ignored_at = item.ignored_at
            existing.updated_from_turn_id = item.updated_from_turn_id
        if not existing.source_key and item.source_key:
            existing.source_key = item.source_key
        if key in preferred:
            existing.question = preferred[key]
    state.open_questions = merged
    return state
