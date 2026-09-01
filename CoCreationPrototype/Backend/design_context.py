"""Shared, server-owned semantic memory for one co-created level.

The memory is deliberately small and provenance-heavy.  It is a Stage snapshot,
not a second transcript: raw chat remains in conversation_turns while this
module stores only design meaning that can safely be handed to another agent.
"""

from copy import deepcopy
import hashlib
import re


SCHEMA_VERSION = 1
AUTHORITIES = {"explicit", "confirmed", "inferred"}
GOAL_STATUSES = {"active", "superseded", "rejected"}
DECISION_STATUSES = {"active", "superseded"}
QUESTION_STATUSES = {"open", "resolved"}
MAX_ITEMS = 32
MAX_TEXT = 1200


def empty_design_context():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userGoals": [],
        "designConstraints": [],
        "confirmedDecisions": [],
        "rejectedDecisions": [],
        "openQuestions": [],
        "activeDisagreement": None,
        "updatedFromStageId": None,
        "updatedFromTurnId": None,
    }


def _text(value, maximum=MAX_TEXT):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:maximum]


def _stable_id(kind, *values):
    seed = "|".join(_text(value, 500) for value in values)
    return f"dc_{kind}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _authority(value, default="inferred"):
    return value if value in AUTHORITIES else default


def _confidence(value, default=0.5):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _source(value):
    if value is None:
        return None
    return _text(value, 128) or None


def _normalize_goal(item, field_name, index):
    if not isinstance(item, dict):
        return None
    value = _text(item.get(field_name))
    if not value:
        return None
    status = item.get("status", "active")
    if status not in GOAL_STATUSES:
        status = "active"
    return {
        "id": _text(item.get("id"), 96) or _stable_id(field_name, value, index),
        field_name: value,
        "authority": _authority(item.get("authority")),
        "status": status,
        "sourceStageId": _source(item.get("sourceStageId")),
        "sourceTurnId": _source(item.get("sourceTurnId")),
        "confidence": _confidence(item.get("confidence")),
        "supersedesId": _source(item.get("supersedesId")),
    }


def _normalize_decision(item, rejected=False, index=0):
    if not isinstance(item, dict):
        return None
    decision = _text(item.get("decision"))
    if not decision:
        return None
    result = {
        "id": _text(item.get("id"), 96) or _stable_id(
            "rejected" if rejected else "confirmed",
            decision,
            item.get("sourceTurnId"),
            index,
        ),
        "decision": decision,
        "reason": _text(item.get("reason"), MAX_TEXT),
        "sourceStageId": _source(item.get("sourceStageId")),
        "sourceTurnId": _source(item.get("sourceTurnId")),
        "proposalId": _source(item.get("proposalId")),
    }
    if not rejected:
        status = item.get("status", "active")
        result["status"] = status if status in DECISION_STATUSES else "active"
    return result


def _normalize_question(item, index=0):
    if not isinstance(item, dict):
        return None
    question = _text(item.get("question"))
    if not question:
        return None
    status = item.get("status", "open")
    return {
        "id": _text(item.get("id"), 96) or _stable_id("question", question, index),
        "question": question,
        "status": status if status in QUESTION_STATUSES else "open",
        "sourceStageId": _source(item.get("sourceStageId")),
        "sourceTurnId": _source(item.get("sourceTurnId")),
    }


def _normalize_disagreement(value):
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if status not in {"active", "resolved"}:
        return None
    result = {"status": status}
    for field in (
        "subject",
        "userPosition",
        "aiPosition",
        "coreDisagreement",
        "nextQuestion",
        "resolution",
    ):
        result[field] = _text(value.get(field), MAX_TEXT) or None
    if status == "active":
        result["resolution"] = None
    return result


def normalize_design_context(value):
    """Return a safe snapshot; malformed/legacy values become an empty snapshot."""
    if not isinstance(value, dict):
        return empty_design_context()
    result = empty_design_context()
    result["userGoals"] = [
        item
        for index, raw in enumerate(value.get("userGoals") or [])
        if (item := _normalize_goal(raw, "goal", index)) is not None
    ][:MAX_ITEMS]
    result["designConstraints"] = [
        item
        for index, raw in enumerate(value.get("designConstraints") or [])
        if (item := _normalize_goal(raw, "constraint", index)) is not None
    ][:MAX_ITEMS]
    result["confirmedDecisions"] = [
        item
        for index, raw in enumerate(value.get("confirmedDecisions") or [])
        if (item := _normalize_decision(raw, index=index)) is not None
    ][:MAX_ITEMS]
    result["rejectedDecisions"] = [
        item
        for index, raw in enumerate(value.get("rejectedDecisions") or [])
        if (item := _normalize_decision(raw, rejected=True, index=index)) is not None
    ][:MAX_ITEMS]
    result["openQuestions"] = [
        item
        for index, raw in enumerate(value.get("openQuestions") or [])
        if (item := _normalize_question(raw, index=index)) is not None
    ][:MAX_ITEMS]
    result["activeDisagreement"] = _normalize_disagreement(
        value.get("activeDisagreement")
    )
    result["updatedFromStageId"] = _source(value.get("updatedFromStageId"))
    result["updatedFromTurnId"] = _source(value.get("updatedFromTurnId"))
    return result


def clone_design_context(value):
    return deepcopy(normalize_design_context(value))


def validate_design_context_patch(value):
    """Validate the optional model patch without trusting its authority."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("designContextPatch must be an object")
    allowed = {"goals", "constraints", "decisions", "rejections", "openQuestions", "corrections"}
    if set(value) - allowed:
        raise ValueError("designContextPatch contains unknown fields")
    result = {field: [] for field in allowed}
    for field in allowed:
        entries = value.get(field) or []
        if not isinstance(entries, list) or len(entries) > 8:
            raise ValueError(f"designContextPatch.{field} must contain at most 8 items")
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError(f"designContextPatch.{field} contains a non-object")
            if field in {"goals", "constraints"}:
                key = "goal" if field == "goals" else "constraint"
                clean = _text(raw.get(key))
                if not clean:
                    raise ValueError(f"designContextPatch.{field} contains empty text")
                result[field].append({
                    key: clean,
                    # The server always downgrades model-provided authority.
                    "authority": "inferred",
                    "confidence": _confidence(raw.get("confidence")),
                })
            elif field in {"decisions", "rejections"}:
                decision = _text(raw.get("decision"))
                if not decision:
                    raise ValueError(f"designContextPatch.{field} contains empty decision")
                result[field].append({
                    "decision": decision,
                    "reason": _text(raw.get("reason")),
                })
            elif field == "openQuestions":
                question = _text(raw.get("question"))
                if not question:
                    raise ValueError("designContextPatch.openQuestions contains empty question")
                result[field].append({"question": question})
            else:
                target = _text(raw.get("targetId"), 96)
                replacement = _text(raw.get("replacement"))
                if not target or not replacement:
                    raise ValueError("designContextPatch.corrections requires targetId and replacement")
                result[field].append({
                    "targetId": target,
                    "replacementType": _text(raw.get("replacementType"), 32),
                    "replacement": replacement,
                    "reason": _text(raw.get("reason")),
                })
    return result


def _authority_rank(value):
    return {"inferred": 0, "explicit": 1, "confirmed": 2}.get(value, 0)


def _merge_goal(items, field_name, value, authority, stage_id, turn_id, confidence=0.5):
    clean = _text(value)
    if not clean:
        return None
    normalized = clean.casefold()
    existing = next(
        (
            item for item in items
            if item.get("status") == "active"
            and _text(item.get(field_name)).casefold() == normalized
        ),
        None,
    )
    if existing is not None:
        if _authority_rank(authority) <= _authority_rank(existing.get("authority")):
            return existing["id"]
        existing["status"] = "superseded"
        supersedes_id = existing["id"]
    else:
        supersedes_id = None
        # A stronger correction supersedes earlier entries in the same semantic
        # family without deleting provenance.
        for item in items:
            if item.get("status") == "active" and _authority_rank(item.get("authority")) < _authority_rank(authority):
                item["status"] = "superseded"
                supersedes_id = item["id"]
    item = {
        "id": _stable_id(field_name, clean, stage_id, turn_id),
        field_name: clean,
        "authority": authority,
        "status": "active",
        "sourceStageId": _source(stage_id),
        "sourceTurnId": _source(turn_id),
        "confidence": _confidence(confidence, 1.0 if authority != "inferred" else 0.5),
        "supersedesId": supersedes_id,
    }
    items.append(item)
    return item["id"]


def _append_unique(items, item, identity_fields):
    key = tuple(_text(item.get(field)).casefold() for field in identity_fields)
    for existing in items:
        if tuple(_text(existing.get(field)).casefold() for field in identity_fields) == key:
            return existing["id"]
    items.append(item)
    return item["id"]


def merge_chat_update(context, patch=None, user_text=None, stage_id=None, turn_id=None):
    """Merge one chat response and the latest user text into a Stage snapshot.

    Model patches are reference-only.  Explicit entries come from the user's
    own words, so a model cannot promote an inferred statement by writing an
    authority field in JSON.
    """
    result = normalize_design_context(context)
    patch = validate_design_context_patch(patch) if patch is not None else None
    user_value = _text(user_text).casefold()
    explicit_goals, explicit_constraints = extract_explicit_user_memory(user_text)
    for value in explicit_goals:
        _merge_goal(result["userGoals"], "goal", value, "explicit", stage_id, turn_id, 1.0)
    for value in explicit_constraints:
        _merge_goal(result["designConstraints"], "constraint", value, "explicit", stage_id, turn_id, 1.0)

    if patch:
        for entry in patch["goals"]:
            _merge_goal(
                result["userGoals"], "goal", entry["goal"], "inferred",
                stage_id, turn_id, entry.get("confidence", 0.5),
            )
        for entry in patch["constraints"]:
            _merge_goal(
                result["designConstraints"], "constraint", entry["constraint"], "inferred",
                stage_id, turn_id, entry.get("confidence", 0.5),
            )
        for entry in patch["openQuestions"]:
            question = {
                "id": _stable_id("question", entry["question"], stage_id, turn_id),
                "question": entry["question"],
                "status": "open",
                "sourceStageId": _source(stage_id),
                "sourceTurnId": _source(turn_id),
            }
            _append_unique(result["openQuestions"], question, ("question",))

        # Corrections are suggestions from the model, not authority.  They are
        # allowed to close an old inference only when the same user turn is
        # visibly corrective; the replacement itself remains inferred unless
        # the user's text is also extracted as an explicit statement above.
        correction_markers = (
            "not ", "don't", "do not", "instead", "actually", "不要", "不想", "不是", "改成", "而是",
        )
        if user_value and any(marker in user_value for marker in correction_markers):
            targets = {entry.get("id") for entry in patch["corrections"]}
            for item in result["userGoals"] + result["designConstraints"]:
                if item.get("id") in targets and item.get("status") == "active":
                    item["status"] = "superseded"

    # A plainly corrective user sentence invalidates the nearest matching
    # active inferred goal, but does not erase a confirmed decision.
    if user_value and any(marker in user_value for marker in (
        "not ", "don't", "do not", "instead", "actually", "不要", "不想", "不是", "改成", "而是",
    )):
        for item in result["userGoals"] + result["designConstraints"]:
            if item.get("authority") == "inferred" and item.get("status") == "active":
                item["status"] = "superseded"

    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result)


def extract_explicit_user_memory(user_text):
    """Conservative extraction: retain user-owned design language, not all chat."""
    text = _text(user_text)
    if not text or len(text) < 4:
        return [], []
    lowered = text.casefold()
    goal_markers = (
        "i want", "i prefer", "i'd like", "would like", "make ", "change ", "adjust ",
        "keep ", "increase ", "reduce ", "move ", "希望", "我想", "我希望", "我更在意", "保持", "增加", "减少", "调整", "改",
    )
    constraint_markers = (
        "avoid", "must not", "do not", "don't", "preserve", "fair", "solvable", "readable",
        "避免", "不要", "不能", "必须", "公平", "可解", "可读", "不破坏", "保留",
    )
    # Questions are requests for clarification unless they also contain a
    # declarative preference.  Keep the extraction bounded to one user turn.
    if "?" in text or "？" in text:
        if not any(marker in lowered or marker in text for marker in goal_markers + constraint_markers):
            return [], []
    constraints = [text] if any(marker in lowered or marker in text for marker in constraint_markers) else []
    goals = [text] if any(marker in lowered or marker in text for marker in goal_markers) else []
    if constraints and goals and any(marker in lowered or marker in text for marker in ("avoid", "must", "do not", "不要", "不能", "必须")):
        goals = []
    return goals, constraints


def add_confirmed_decision(context, decision, reason, stage_id, turn_id, proposal_id=None):
    result = normalize_design_context(context)
    item = _normalize_decision({
        "decision": decision,
        "reason": reason,
        "sourceStageId": stage_id,
        "sourceTurnId": turn_id,
        "proposalId": proposal_id,
        "status": "active",
    }, index=len(result["confirmedDecisions"]))
    if item is not None:
        for existing in result["confirmedDecisions"]:
            if existing.get("status") == "active" and existing.get("decision", "").casefold() == decision.casefold():
                existing["status"] = "superseded"
        result["confirmedDecisions"].append(item)
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return result


def add_rejected_decision(context, decision, reason, stage_id, turn_id, proposal_id=None):
    result = normalize_design_context(context)
    item = _normalize_decision({
        "decision": decision,
        "reason": reason,
        "sourceStageId": stage_id,
        "sourceTurnId": turn_id,
        "proposalId": proposal_id,
    }, rejected=True, index=len(result["rejectedDecisions"]))
    if item is not None:
        _append_unique(result["rejectedDecisions"], item, ("decision", "reason"))
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return result


def set_active_disagreement(context, disagreement, stage_id=None, turn_id=None):
    result = normalize_design_context(context)
    value = _normalize_disagreement(disagreement)
    result["activeDisagreement"] = value if value and value.get("status") == "active" else None
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return result


def revision_projection(context):
    value = normalize_design_context(context)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "activeGoals": [
            item for item in value["userGoals"]
            if item["status"] == "active" and item["authority"] in {"explicit", "confirmed"}
        ],
        "activeConstraints": [
            item for item in value["designConstraints"]
            if item["status"] == "active" and item["authority"] in {"explicit", "confirmed"}
        ],
        "confirmedDecisions": [
            item for item in value["confirmedDecisions"] if item["status"] == "active"
        ],
        "rejectedDecisions": value["rejectedDecisions"][-12:],
        "openQuestions": [item for item in value["openQuestions"] if item["status"] == "open"],
        "activeDisagreement": value["activeDisagreement"],
    }


def evaluator_projection(context):
    return normalize_design_context(context)


# Keep the user-language extractor below in Unicode-escape form.  This file is
# also shipped to hosts whose editor defaults are not UTF-8; escaped literals
# prevent a source-code encoding conversion from disabling Chinese memory
# updates at runtime.
def extract_explicit_user_memory(user_text):
    """Conservatively retain design language that came from the user's turn."""
    text = _text(user_text)
    if not text or len(text) < 4:
        return [], []
    lowered = text.casefold()
    goal_markers = (
        "i want", "i prefer", "i'd like", "would like", "make ", "change ", "adjust ",
        "keep ", "increase ", "reduce ", "move ",
        "\u5e0c\u671b", "\u6211\u60f3", "\u6211\u5e0c\u671b", "\u6211\u66f4\u5728\u610f",
        "\u4fdd\u6301", "\u589e\u52a0", "\u51cf\u5c11", "\u8c03\u6574", "\u6539",
    )
    constraint_markers = (
        "avoid", "must not", "do not", "don't", "preserve", "fair", "solvable", "readable",
        "\u907f\u514d", "\u4e0d\u8981", "\u4e0d\u80fd", "\u5fc5\u987b", "\u516c\u5e73",
        "\u53ef\u89e3", "\u53ef\u8bfb", "\u4e0d\u7834\u574f", "\u4fdd\u7559",
    )
    question_markers = ("?", "\uFF1F")
    if any(marker in text for marker in question_markers):
        if not any(marker in lowered or marker in text for marker in goal_markers + constraint_markers):
            return [], []
    has_constraint = any(marker in lowered or marker in text for marker in constraint_markers)
    has_goal = any(marker in lowered or marker in text for marker in goal_markers)
    constraints = [text] if has_constraint else []
    goals = [text] if has_goal else []
    return goals, constraints


def merge_chat_update(context, patch=None, user_text=None, stage_id=None, turn_id=None):
    """Merge server-owned memory without allowing model authority promotion."""
    result = normalize_design_context(context)
    patch = validate_design_context_patch(patch) if patch is not None else None
    user_value = _text(user_text).casefold()
    explicit_goals, explicit_constraints = extract_explicit_user_memory(user_text)
    for value in explicit_goals:
        _merge_goal(result["userGoals"], "goal", value, "explicit", stage_id, turn_id, 1.0)
    for value in explicit_constraints:
        _merge_goal(result["designConstraints"], "constraint", value, "explicit", stage_id, turn_id, 1.0)

    if patch:
        for entry in patch["goals"]:
            _merge_goal(
                result["userGoals"], "goal", entry["goal"], "inferred",
                stage_id, turn_id, entry.get("confidence", 0.5),
            )
        for entry in patch["constraints"]:
            _merge_goal(
                result["designConstraints"], "constraint", entry["constraint"], "inferred",
                stage_id, turn_id, entry.get("confidence", 0.5),
            )
        for entry in patch["openQuestions"]:
            _append_unique(
                result["openQuestions"],
                {
                    "id": _stable_id("question", entry["question"], stage_id, turn_id),
                    "question": entry["question"],
                    "status": "open",
                    "sourceStageId": _source(stage_id),
                    "sourceTurnId": _source(turn_id),
                },
                ("question",),
            )
        correction_markers = (
            "not ", "don't", "do not", "instead", "actually",
            "\u4e0d\u8981", "\u4e0d\u60f3", "\u4e0d\u662f", "\u6539\u6210", "\u800c\u662f",
        )
        if user_value and any(marker in user_value for marker in correction_markers):
            targets = {entry.get("targetId") for entry in patch["corrections"]}
            for item in result["userGoals"] + result["designConstraints"]:
                if item.get("id") in targets and item.get("status") == "active":
                    item["status"] = "superseded"

    # A direct corrective sentence can invalidate an unconfirmed AI reading,
    # but it cannot rewrite an explicit or confirmed user decision.
    correction_markers = (
        "not ", "don't", "do not", "instead", "actually",
        "\u4e0d\u8981", "\u4e0d\u60f3", "\u4e0d\u662f", "\u6539\u6210", "\u800c\u662f",
    )
    if user_value and any(marker in user_value for marker in correction_markers):
        for item in result["userGoals"] + result["designConstraints"]:
            if item.get("authority") == "inferred" and item.get("status") == "active":
                item["status"] = "superseded"

    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result)
