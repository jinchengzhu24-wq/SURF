"""Server-owned semantic memory for one co-created level.

Raw chat remains in ``conversation_turns``. This module stores only bounded,
provenance-aware meaning that can safely be carried between immutable Stages.
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
MAX_PATCH_ITEMS = 8
MAX_TEXT = 1200


_QUESTION_MARKERS = re.compile(
    r"(?:\?|\uFF1F|\b(?:what|which|how|whether|can|should|would|could|do|does|is|are|"
    r"did|will)\b|\u4ec0\u4e48|\u54ea|\u5982\u4f55|\u662f\u5426|\u8981\u4e0d\u8981|\u5e94\u8be5|"
    r"\u66f4\u503e\u5411|\u5e0c\u671b|\u54ea\u4e2a)",
    flags=re.IGNORECASE,
)
_COORDINATE_MARKERS = re.compile(
    r"[\(\uFF08]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\)\uFF09]"
)
_ROUTE_TRACE_MARKERS = re.compile(
    r"(?:\b(?:route\s+from|path|next|then|move\s+to|go\s+to|reachability|reachable|"
    r"bfs|solver|step)\b|\u5148(?:\u5230|\u8d70|\u63a8|\u79fb\u52a8)|\u518d(?:\u5230|\u8d70|\u63a8|\u79fb\u52a8)|"
    r"\u7ecf\u8fc7|\u8def\u5f84\u600e\u4e48\u8d70|\u54ea\u4e00\u6b65|\u4e0b\u4e00\u6b65|\u56de\u5230|"
    r"\u7ed5\u884c|\u53ef\u8fbe|\u901a\u4e0d\u901a|\u79fb\u52a8\u5230|\u63a8\u5230|\u8d70\u5230|"
    r"\u2192|->)",
    flags=re.IGNORECASE,
)
_SPATIAL_ONLY_MARKERS = re.compile(
    r"(?:\b(?:where|which\s+cell|what\s+coordinate|adjacent|near|beside|left|right|above|"
    r"below)\b|\u54ea\u91cc|\u5728\u54ea|\u54ea\u4e2a\u683c|\u5750\u6807|\u76f8\u90bb|\u9760\u8fd1|"
    r"\u5de6\u4fa7|\u53f3\u4fa7|\u4e0a\u65b9|\u4e0b\u65b9)",
    flags=re.IGNORECASE,
)
_DESIGN_MARKERS = re.compile(
    r"(?:\b(?:design|experience|intent|trade[- ]?off|choice|emphasis|rhythm|difficulty|"
    r"readability|guidance|layout|purpose|highlight|notice|stand\s+out|understand|read|"
    r"clarity|expectation|feel|match|direction|matters|first\s+push|push\s+order|detour|"
    r"route|corridor|water|box|target|player)\b|"
    r"\u8bbe\u8ba1|\u4f53\u9a8c|\u610f\u56fe|\u53d6\u820d|\u503e\u5411|\u91cd\u70b9|\u5f3a\u8c03|\u7a81\u51fa|"
    r"\u8282\u594f|\u96be\u5ea6|\u53ef\u8bfb\u6027|\u5f15\u5bfc|\u5e03\u5c40|\u4f5c\u7528|\u5206\u5de5|"
    r"\u5148\u540e|\u987a\u5e8f|\u8def\u7ebf|\u8def\u5f84|\u901a\u9053|\u7bb1\u5b50|\u76ee\u6807|"
    r"\u6c34\u57df|\u74f6\u9888|\u7ed5\u8def|\u8bfb\u61c2|\u6ce8\u610f|\u5e0c\u671b|\u503c\u5f97|"
    r"\u611f\u89c9|\u9884\u671f|\u6539\u52a8|\u4fdd\u7559|\u5e94\u8be5)",
    flags=re.IGNORECASE,
)
_STRONG_DESIGN_MARKERS = re.compile(
    r"(?:\b(?:design|experience|intent|trade[- ]?off|choice|emphasis|rhythm|difficulty|"
    r"readability|guidance|layout|purpose|highlight|stand\s+out|first\s+push|push\s+order|"
    r"feel|read|understand|notice|expectation|stay\s+open|keep|preserve|winding|longer|shorter)\b|"
    r"\u8bbe\u8ba1|\u4f53\u9a8c|\u610f\u56fe|\u53d6\u820d|\u91cd\u70b9|\u5f3a\u8c03|\u7a81\u51fa|"
    r"\u8282\u594f|\u96be\u5ea6|\u53ef\u8bfb\u6027|\u5f15\u5bfc|\u5e03\u5c40|\u4f5c\u7528|\u5206\u5de5|"
    r"\u5148\u540e|\u987a\u5e8f|\u7ed5\u8def|\u74f6\u9888)",
    flags=re.IGNORECASE,
)


def is_design_level_question(question, evidence_text=None):
    """Return whether text is a designer-facing open question, not route mechanics."""
    clean = _text(question)
    if not clean or not _QUESTION_MARKERS.search(clean):
        return False

    lowered = clean.casefold()
    generic = (
        "what do you think",
        "does this work",
        "is this okay",
        "would you like to continue",
        "你怎么看",
        "可以吗",
        "是否满意",
        "还满意吗",
    )
    if any(value in lowered or value in clean for value in generic):
        return False

    if re.search(r"\b(?:bfs|solver|reachability|reachable)\b", lowered):
        return False

    coordinate_count = len(_COORDINATE_MARKERS.findall(clean))
    has_route_trace = bool(_ROUTE_TRACE_MARKERS.search(clean))
    has_design = bool(_DESIGN_MARKERS.search(clean))
    has_strong_design = bool(_STRONG_DESIGN_MARKERS.search(clean))
    has_spatial_only = bool(_SPATIAL_ONLY_MARKERS.search(clean))

    if coordinate_count >= 2 and not has_strong_design:
        return False
    if has_route_trace and coordinate_count >= 1 and not has_strong_design:
        return False
    if has_spatial_only and not has_design:
        return False
    if not has_design:
        return False

    # Evidence may contain route facts, but it cannot turn a mechanical question
    # into a design question. It is retained only as provenance by the caller.
    _ = evidence_text
    return True


def design_level_open_questions(context):
    """Return only unresolved design questions for prompts and public progress views."""
    value = normalize_design_context(context)
    return [
        item
        for item in value.get("openQuestions", [])
        if item.get("status") == "open"
        and is_design_level_question(item.get("question"), item.get("evidenceText"))
    ]


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


_CURRENT_MAP_FACT_FRAGMENT = re.compile(
    r"(?:\b(?:P|B\d+|T\d+)\b\s*(?:在|位于|坐落于|occupies|is\s+at|"
    r"is\s+located\s+at|sits\s+on)\s*(?:第\s*)?\d{1,2}\s*(?:行\s*[,，]?\s*第\s*|[,，])\s*\d{1,2}\s*(?:列)?"
    r"|\b(?:P|B\d+|T\d+)\b\s*(?:在|位于|坐落于|occupies|is\s+at|"
    r"is\s+located\s+at|sits\s+on)\s*[（(]\s*\d{1,2}\s*[,，]\s*\d{1,2}\s*[）)]"
    r"|[（(]\s*\d{1,2}\s*[,，]\s*\d{1,2}\s*[）)]\s*(?:是|为|属于|is|contains)\s*"
    r"(?:水域|水|墙|墙体|地面|空地|通道|water|wall|floor|ground|corridor))",
    flags=re.IGNORECASE,
)


def sanitize_user_design_text(value):
    """Remove present-tense map facts before semantic memory is persisted.

    Coordinates describing a future edit remain intact.  This is deliberately
    syntactic and conservative; the current StageSnapshot performs the actual
    fact check in the application layer.
    """
    text = _text(value)
    if not text:
        return text
    current_fact = re.compile(
        r"(?:\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_])\s*(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+at|sits\s+on)\s*"
        r"(?:\u7b2c\s*)?\d{1,2}\s*(?:\u884c\s*[,\uFF0C]?\s*\u7b2c\s*|[,\uFF0C])\s*\d{1,2}\s*(?:\u5217)?"
        r"|\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_])\s*(?:\u5728|\u4f4d\u4e8e|\u5750\u843d\u4e8e|occupies|is\s+at|is\s+located\s+at|sits\s+on)\s*[\uFF08(]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\uFF09)]"
        r"|[\uFF08(]\s*\d{1,2}\s*[,\uFF0C]\s*\d{1,2}\s*[\uFF09)]\s*(?:\u662f|\u4e3a|\u5c5e\u4e8e|is|contains)\s*"
        r"(?:\u6c34\u57df|\u6c34|\u5899|\u5899\u4f53|\u5730\u9762|\u7a7a\u5730|\u901a\u9053|water|wall|floor|ground|corridor)"
        r"|(?:\u7b2c\s*)?\d{1,2}\s*\u884c\s*(?:\u7b2c\s*)?\d{1,2}\s*\u5217\s*(?:\u662f|\u4e3a)\s*(?:P|B\d+|T\d+)(?![A-Za-z0-9_])"
        r"|\b(?:P|B\d+|T\d+)(?![A-Za-z0-9_])\s*(?:occupies|is\s+at|is\s+located\s+(?:at|in)|sits\s+on)\s+row\s*\d{1,2}\s*,\s*column\s*\d{1,2}"
        r"|\brow\s*\d{1,2}\s*[,，]\s*column\s*\d{1,2}\s*(?:is|contains)\s*(?:P|B\d+|T\d+)(?![A-Za-z0-9_]))",
        flags=re.IGNORECASE,
    )
    def remove_current_fact(match):
        # Keep future or hypothetical design coordinates such as
        # "I want B1 at (4,4)" and "if B1 is at (4,4)". A current fact
        # followed by a preference is still removed because the marker is
        # after, rather than before, the matched fact.
        prefix = text[max(0, match.start() - 32):match.start()]
        if re.search(
            r"(?:\u5982\u679c|\u82e5|\u5047\u8bbe|\u5c06|\u4f1a|\u5e0c\u671b|\u60f3\u8981|\u60f3\u628a|\u60f3\u8ba9|"
            r"\b(?:if|when|would|will|want|wish)\b)",
            prefix,
            flags=re.IGNORECASE,
        ):
            return match.group(0)
        return " "

    cleaned = current_fact.sub(remove_current_fact, text)
    reverse_fact = re.compile(
        r"\brow\s*\d{1,2}\s*[,\uFF0C]\s*column\s*\d{1,2}\s*"
        r"(?:is|contains)\s*(?:P|B\d+|T\d+)(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )
    cleaned = reverse_fact.sub(
        lambda match: match.group(0)
        if re.search(r"\b(?:if|when|would|will|want|wish)\b", cleaned[:match.start()], flags=re.IGNORECASE)
        else " ",
        cleaned,
    )
    return re.sub(r"\s+([\uFF0C\u3002\uFF1B;,.!?\uFF01\uFF1F])", r"\1", cleaned).strip()


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
    value = sanitize_user_design_text(item.get(field_name))
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
        "reason": _text(item.get("reason")),
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
    question = sanitize_user_design_text(item.get("question"))
    if not question:
        return None
    status = item.get("status", "open")
    return {
        "id": _text(item.get("id"), 96) or _stable_id("question", question, index),
        "question": question,
        "status": status if status in QUESTION_STATUSES else "open",
        "sourceStageId": _source(item.get("sourceStageId")),
        "sourceTurnId": _source(item.get("sourceTurnId")),
        "updatedFromTurnId": _source(
            item.get("updatedFromTurnId") or item.get("sourceTurnId")
        ),
        "resolvedByTurnId": _source(item.get("resolvedByTurnId")),
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
    """Return a safe snapshot; malformed and legacy values become valid memory."""
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
    """Validate the optional model patch without trusting model authority."""
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
        if not isinstance(entries, list) or len(entries) > MAX_PATCH_ITEMS:
            raise ValueError(f"designContextPatch.{field} must contain at most {MAX_PATCH_ITEMS} items")

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
                    "authority": "inferred",
                    "confidence": _confidence(raw.get("confidence")),
                    "evidenceText": _text(raw.get("evidenceText"), MAX_TEXT) or None,
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
                status = raw.get("status", "open")
                if status not in QUESTION_STATUSES:
                    raise ValueError("designContextPatch.openQuestions has an invalid status")
                if not is_design_level_question(question, raw.get("evidenceText")):
                    continue
                result[field].append({
                    "question": question,
                    "status": status,
                    "targetId": _text(raw.get("targetId"), 96) or None,
                    "evidenceText": _text(raw.get("evidenceText"), MAX_TEXT) or None,
                })
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
    clean = sanitize_user_design_text(value)
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
        for item in items:
            if (
                item.get("status") == "active"
                and _authority_rank(item.get("authority")) < _authority_rank(authority)
            ):
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


def _verified_user_evidence(evidence_text, user_text):
    evidence = str(evidence_text or "").strip()
    return bool(evidence and evidence in str(user_text or ""))


def _question_key(value):
    """Compare equivalent questions without duplicating punctuation or spacing variants."""
    return re.sub(r"\s+", " ", _text(value)).strip("?\uFF1F").casefold()


def _merge_open_question(result, entry, stage_id, turn_id, user_text):
    question = sanitize_user_design_text(entry.get("question"))
    if not question or not is_design_level_question(
        question,
        entry.get("evidenceText"),
    ):
        return False

    target_id = _source(entry.get("targetId"))
    existing = next(
        (
            item for item in result["openQuestions"]
            if (target_id and item.get("id") == target_id)
            or _question_key(item.get("question")) == _question_key(question)
        ),
        None,
    )
    status = entry.get("status", "open")

    if status == "resolved":
        if not _verified_user_evidence(entry.get("evidenceText"), user_text):
            return False
        if existing is None:
            return False
        existing["status"] = "resolved"
        existing["updatedFromTurnId"] = _source(turn_id)
        existing["resolvedByTurnId"] = _source(turn_id)
        return True

    if existing is not None:
        if existing.get("status") == "resolved":
            existing["status"] = "open"
        existing["question"] = question
        existing["updatedFromTurnId"] = _source(turn_id)
        return True

    result["openQuestions"].append({
        "id": _stable_id("question", question, stage_id, turn_id),
        "question": question,
        "status": "open",
        "sourceStageId": _source(stage_id),
        "sourceTurnId": _source(turn_id),
        "updatedFromTurnId": _source(turn_id),
        "resolvedByTurnId": None,
    })
    return True


def add_open_question(context, question, stage_id=None, turn_id=None):
    """Add an assistant-raised open question using the normal provenance rules."""
    result = normalize_design_context(context)
    clean = _text(question)
    if not clean or not is_design_level_question(clean):
        return result

    _merge_open_question(
        result,
        {"question": clean, "status": "open"},
        stage_id,
        turn_id,
        None,
    )
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result)


def extract_explicit_user_memory(user_text):
    """Conservatively retain design language that came from the user's turn."""
    text = sanitize_user_design_text(user_text)
    if not text or len(text) < 4:
        return [], []

    lowered = text.casefold()
    goal_markers = (
        "i want", "i prefer", "i'd like", "would like", "make ", "change ", "adjust ",
        "keep ", "increase ", "reduce ", "move ", "i lean toward", "i tend to",
        "\u5e0c\u671b", "\u6211\u60f3", "\u6211\u60f3\u8981", "\u6211\u503e\u5411\u4e8e",
        "\u6211\u503e\u5411", "\u6211\u5e0c\u671b", "\u6211\u66f4\u5728\u610f", "\u8bf7\u4fdd\u6301",
        "\u4fdd\u6301", "\u589e\u52a0", "\u51cf\u5c11", "\u8c03\u6574", "\u6539",
    )
    constraint_markers = (
        "avoid", "must not", "do not", "don't", "preserve", "fair", "solvable", "readable",
        "\u907f\u514d", "\u4e0d\u8981", "\u4e0d\u80fd", "\u5fc5\u987b", "\u516c\u5e73",
        "\u53ef\u89e3", "\u53ef\u8bfb", "\u4e0d\u7834\u574f", "\u4fdd\u7559",
    )
    if any(marker in text for marker in ("?", "\uFF1F")):
        if not any(marker in lowered or marker in text for marker in goal_markers + constraint_markers):
            return [], []

    has_constraint = any(marker in lowered or marker in text for marker in constraint_markers)
    has_goal = any(marker in lowered or marker in text for marker in goal_markers)
    return ([text] if has_goal else []), ([text] if has_constraint else [])


def merge_chat_update(context, patch=None, user_text=None, stage_id=None, turn_id=None):
    """Merge user evidence and model suggestions into a Stage snapshot."""
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
            authority = "explicit" if _verified_user_evidence(entry.get("evidenceText"), user_text) else "inferred"
            _merge_goal(
                result["userGoals"], "goal", entry["goal"], authority,
                stage_id, turn_id, 1.0 if authority == "explicit" else entry.get("confidence", 0.5),
            )
        for entry in patch["constraints"]:
            authority = "explicit" if _verified_user_evidence(entry.get("evidenceText"), user_text) else "inferred"
            _merge_goal(
                result["designConstraints"], "constraint", entry["constraint"], authority,
                stage_id, turn_id, 1.0 if authority == "explicit" else entry.get("confidence", 0.5),
            )
        for entry in patch["openQuestions"]:
            _merge_open_question(result, entry, stage_id, turn_id, user_text)

        correction_markers = (
            "not ", "don't", "do not", "instead", "actually",
            "\u4e0d\u8981", "\u4e0d\u60f3", "\u4e0d\u662f", "\u6539\u6210", "\u800c\u662f",
        )
        if user_value and any(marker in user_value for marker in correction_markers):
            targets = {entry.get("targetId") for entry in patch["corrections"]}
            for item in result["userGoals"] + result["designConstraints"]:
                if item.get("id") in targets and item.get("status") == "active":
                    item["status"] = "superseded"

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
            if (
                existing.get("status") == "active"
                and existing.get("decision", "").casefold() == decision.casefold()
            ):
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
        "openQuestions": design_level_open_questions(value),
        "activeDisagreement": value["activeDisagreement"],
    }


def evaluator_projection(context):
    return normalize_design_context(context)
