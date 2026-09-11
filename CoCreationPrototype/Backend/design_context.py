"""Server-owned semantic memory for one co-created level.

Raw chat remains in ``conversation_turns``. This module stores only bounded,
provenance-aware meaning that can safely be carried between immutable Stages.
"""

from copy import deepcopy
import hashlib
import re


SCHEMA_VERSION = 4
AUTHORITIES = {"explicit", "confirmed", "inferred"}
GOAL_STATUSES = {"active", "superseded", "rejected"}
DECISION_STATUSES = {"active", "superseded"}
QUESTION_STATUSES = {"open", "answered", "ignored", "resolved"}
HYPOTHESIS_STATUSES = {"tentative", "confirmed", "rejected", "superseded", "legacy_unverified"}
INTENT_TOPICS = {
    "difficulty", "route_readability", "push_dependency", "space_distribution",
    "route_rhythm", "water_function", "entity_placement", "preservation", "other",
}
MAX_ACTIVE_INFERRED = 16
MAX_INACTIVE_ITEMS = 16
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
        "intentHypotheses": [],
        "processedEvidenceIds": [],
        "activeDisagreement": None,
        "updatedFromStageId": None,
        "updatedFromTurnId": None,
    }


def _bounded_semantic_items(items):
    """Keep authoritative active memory and prefer recent tentative/history items."""
    authoritative = [
        item for item in items
        if item.get("status") == "active"
        and item.get("authority") in {"explicit", "confirmed"}
    ]
    inferred = [
        item for item in items
        if item.get("status") == "active" and item.get("authority") == "inferred"
    ][-MAX_ACTIVE_INFERRED:]
    inactive = [item for item in items if item.get("status") != "active"][-MAX_INACTIVE_ITEMS:]
    return authoritative + inferred + inactive


def _bounded_status_items(items, active_status):
    active = [item for item in items if item.get("status") == active_status]
    inactive = [item for item in items if item.get("status") != active_status][-MAX_INACTIVE_ITEMS:]
    return active + inactive


def _bounded_hypotheses(items):
    confirmed = [item for item in items if item.get("status") == "confirmed"]
    tentative = [item for item in items if item.get("status") == "tentative"][-MAX_ACTIVE_INFERRED:]
    inactive = [
        item for item in items
        if item.get("status") not in {"confirmed", "tentative"}
    ][-MAX_INACTIVE_ITEMS:]
    return confirmed + tentative + inactive


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
    return re.sub(
        r"\s+([\uFF0C\u3002\uFF1B;,.!?\uFF01\uFF1F])", r"\1", cleaned
    ).strip(" \t\r\n,;\uFF0C\uFF1B")


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
    if status == "resolved":
        status = "answered"
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
        "answeredAtStageId": _source(
            item.get("answeredAtStageId") or item.get("resolvedAtStageId")
        ),
        "ignoredAtStageId": _source(item.get("ignoredAtStageId")),
        "ignoredAt": _text(item.get("ignoredAt"), 64) or None,
        "sourceKind": _text(item.get("sourceKind"), 32) or "legacy",
        "sourceKey": _text(item.get("sourceKey"), 192) or None,
    }


def _normalize_hypothesis(item, index=0):
    if not isinstance(item, dict):
        return None
    statement = sanitize_user_design_text(item.get("statement"))
    if not statement:
        return None
    topic = _text(item.get("topicKey"), 64)
    if topic not in INTENT_TOPICS:
        topic = "other"
    status = item.get("status", "tentative")
    if status not in HYPOTHESIS_STATUSES:
        status = "tentative"
    supporting = [
        _source(value) for value in item.get("supportingEvidenceIds", [])
        if _source(value)
    ][:32]
    contradicting = [
        _source(value) for value in item.get("contradictingEvidenceIds", [])
        if _source(value)
    ][:32]
    return {
        "id": _text(item.get("id"), 96) or _stable_id("hypothesis", topic, statement, index),
        "topicKey": topic,
        "statement": statement,
        "status": status,
        "confidence": min(_confidence(item.get("confidence"), 0.35), 0.75)
        if status in {"tentative", "legacy_unverified"}
        else _confidence(item.get("confidence"), 1.0 if status == "confirmed" else 0.0),
        "supportingEvidenceIds": list(dict.fromkeys(supporting)),
        "contradictingEvidenceIds": list(dict.fromkeys(contradicting)),
        "sourceStageId": _source(item.get("sourceStageId")),
        "sourceTurnId": _source(item.get("sourceTurnId")),
        "lastUpdatedStageId": _source(
            item.get("lastUpdatedStageId") or item.get("sourceStageId")
        ),
        "origin": _text(item.get("origin"), 32) or "model",
        "displayed": bool(item.get("displayed", False)),
        "originalStatement": sanitize_user_design_text(
            item.get("originalStatement")
        ) or None,
        "confirmedAtStageId": _source(item.get("confirmedAtStageId")),
        "feedbackAction": (
            item.get("feedbackAction")
            if item.get("feedbackAction") in {"confirm", "revise"}
            else None
        ),
        "displayStatement": sanitize_user_design_text(
            item.get("displayStatement")
        ) or None,
        "displayLanguage": _text(item.get("displayLanguage"), 16) or None,
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
    phase = value.get("phase")
    if phase in {"reason_review", "choice_pending"}:
        result["phase"] = phase
    result["displayCard"] = bool(value.get("displayCard", True))
    for field in (
        "primaryHypothesis",
        "secondaryHypothesis",
        "proposalSummary",
        "acceptedReason",
    ):
        text = _text(value.get(field), MAX_TEXT)
        if text:
            result[field] = text
    if status == "active":
        result["resolution"] = None
    return result


def normalize_design_context(value):
    """Return a safe snapshot; malformed and legacy values become valid memory."""
    if not isinstance(value, dict):
        return empty_design_context()

    result = empty_design_context()
    result["userGoals"] = _bounded_semantic_items([
        item
        for index, raw in enumerate(value.get("userGoals") or [])
        if (item := _normalize_goal(raw, "goal", index)) is not None
    ])
    result["designConstraints"] = _bounded_semantic_items([
        item
        for index, raw in enumerate(value.get("designConstraints") or [])
        if (item := _normalize_goal(raw, "constraint", index)) is not None
    ])
    result["confirmedDecisions"] = _bounded_status_items([
        item
        for index, raw in enumerate(value.get("confirmedDecisions") or [])
        if (item := _normalize_decision(raw, index=index)) is not None
    ], "active")
    result["rejectedDecisions"] = [
        item
        for index, raw in enumerate(value.get("rejectedDecisions") or [])
        if (item := _normalize_decision(raw, rejected=True, index=index)) is not None
    ][-MAX_INACTIVE_ITEMS:]
    # Question history is user-visible progress. Do not discard answered
    # questions merely because the prompt projection is bounded elsewhere.
    result["openQuestions"] = [
        item
        for index, raw in enumerate(value.get("openQuestions") or [])
        if (item := _normalize_question(raw, index=index)) is not None
    ]
    result["intentHypotheses"] = _bounded_hypotheses([
        item
        for index, raw in enumerate(value.get("intentHypotheses") or [])
        if (item := _normalize_hypothesis(raw, index=index)) is not None
    ])
    if not result["intentHypotheses"] and value.get("schemaVersion") == 1:
        legacy_inferred = [
            ("goal", item)
            for item in result["userGoals"]
            if item.get("authority") == "inferred" and item.get("status") == "active"
        ] + [
            ("constraint", item)
            for item in result["designConstraints"]
            if item.get("authority") == "inferred" and item.get("status") == "active"
        ]
        result["intentHypotheses"] = [
            _normalize_hypothesis({
                "id": _stable_id("legacy_hypothesis", item.get("id")),
                "topicKey": infer_intent_topic(item.get(field_name)),
                "statement": item.get(field_name),
                "status": "legacy_unverified",
                "confidence": min(item.get("confidence", 0.5), 0.5),
                "sourceStageId": item.get("sourceStageId"),
                "sourceTurnId": item.get("sourceTurnId"),
                "origin": "legacy",
            }, index)
            for index, (field_name, item) in enumerate(legacy_inferred)
        ][-MAX_ACTIVE_INFERRED:]
    result["processedEvidenceIds"] = list(dict.fromkeys(
        _source(value) for value in value.get("processedEvidenceIds", []) if _source(value)
    ))[-128:]
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


def infer_intent_topic(value):
    text = _text(value).casefold()
    topic_markers = (
        ("water_function", ("water", "\u6c34\u57df", "\u6c34")),
        ("push_dependency", ("push order", "dependency", "first push", "\u63a8\u52a8\u987a\u5e8f", "\u5148\u540e", "\u7b2c\u4e00\u63a8")),
        ("route_readability", ("readable", "readability", "clear route", "\u53ef\u8bfb", "\u8bfb\u61c2", "\u6e05\u6670")),
        ("route_rhythm", ("rhythm", "pacing", "detour", "winding", "\u8282\u594f", "\u7ed5\u8def", "\u8fc2\u56de")),
        ("space_distribution", (
            "space", "open area", "corridor", "layout", "composition", "arrangement",
            "crowded", "cramped", "dense", "compact", "\u7a7a\u95f4", "\u901a\u9053", "\u5f00\u653e",
            "\u5e03\u5c40", "\u6392\u7248", "\u6392\u5e03", "\u6784\u56fe", "\u62e5\u6324", "\u5bc6\u96c6", "\u7d27\u51d1",
        )),
        ("difficulty", ("difficulty", "harder", "easier", "\u96be\u5ea6", "\u66f4\u96be", "\u66f4\u5bb9\u6613")),
        ("preservation", ("preserve", "keep", "unchanged", "\u4fdd\u7559", "\u4fdd\u6301", "\u4e0d\u53d8")),
        ("entity_placement", ("position", "placement", "move the box", "move the target", "\u4f4d\u7f6e", "\u79fb\u52a8\u7bb1\u5b50", "\u79fb\u52a8\u76ee\u6807")),
    )
    for topic, markers in topic_markers:
        if any(marker in text for marker in markers):
            return topic
    return "other"


def merge_intent_hypothesis(
    context,
    statement,
    evidence_ids=None,
    stage_id=None,
    turn_id=None,
    confidence=0.35,
    displayed=False,
):
    """Upsert one model hypothesis; it always remains tentative until user confirmation."""
    result = normalize_design_context(context)
    clean = sanitize_user_design_text(statement)
    if not clean:
        return result, None
    topic = infer_intent_topic(clean)
    evidence_ids = list(dict.fromkeys(
        _source(value) for value in (evidence_ids or []) if _source(value)
    ))[:32]
    exact = next((
        item for item in result["intentHypotheses"]
        if item.get("status") == "tentative"
        and item.get("statement", "").casefold() == clean.casefold()
    ), None)
    if exact is not None:
        exact["supportingEvidenceIds"] = list(dict.fromkeys(
            exact.get("supportingEvidenceIds", []) + evidence_ids
        ))[:32]
        exact["confidence"] = min(0.75, max(
            exact.get("confidence", 0.35), confidence,
        ))
        exact["lastUpdatedStageId"] = _source(stage_id)
        exact["displayed"] = bool(exact.get("displayed") or displayed)
        hypothesis_id = exact["id"]
    else:
        for item in result["intentHypotheses"]:
            if item.get("status") == "tentative" and item.get("topicKey") == topic:
                item["status"] = "superseded"
        hypothesis_id = _stable_id("hypothesis", topic, clean, stage_id, turn_id)
        result["intentHypotheses"].append({
            "id": hypothesis_id,
            "topicKey": topic,
            "statement": clean,
            "status": "tentative",
            "confidence": min(0.75, _confidence(confidence, 0.35)),
            "supportingEvidenceIds": evidence_ids,
            "contradictingEvidenceIds": [],
            "sourceStageId": _source(stage_id),
            "sourceTurnId": _source(turn_id),
            "lastUpdatedStageId": _source(stage_id),
            "origin": "model",
            "displayed": bool(displayed),
        })
    result["processedEvidenceIds"] = list(dict.fromkeys(
        result.get("processedEvidenceIds", []) + evidence_ids
    ))[-128:]
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result), hypothesis_id


def apply_hypothesis_feedback(
    context,
    user_text,
    stage_id=None,
    turn_id=None,
    target_hypothesis_id=None,
):
    """Resolve only the most recent displayed tentative reading from explicit feedback."""
    result = normalize_design_context(context)
    text = _text(user_text).casefold()
    active = [
        item for item in result["intentHypotheses"]
        if item.get("status") == "tentative"
        and item.get("sourceTurnId")
        and item.get("displayed")
        and (
            target_hypothesis_id is None
            or item.get("id") == target_hypothesis_id
        )
    ]
    if not text or not active:
        return result, None
    target = active[-1]
    compact = re.sub(r"[\s,.!?\u3002\uFF0C\uFF01\uFF1F]+", " ", text).strip()
    confirmations = {
        "yes that is what i mean", "yes that's what i mean", "that is what i mean",
        "exactly", "correct", "\u5bf9 \u5c31\u662f\u8fd9\u4e2a\u610f\u601d", "\u5bf9\u7684 \u5c31\u662f\u8fd9\u4e2a\u610f\u601d",
        "\u6ca1\u9519", "\u6b63\u662f\u8fd9\u6837",
    }
    rejection_markers = (
        "no that is not", "no that's not", "not what i mean", "you misunderstood",
        "\u4e0d\u662f\u8fd9\u4e2a\u610f\u601d", "\u4f60\u7406\u89e3\u9519", "\u6211\u4e0d\u662f\u60f3",
    )
    if compact in confirmations:
        target["status"] = "confirmed"
        target["confidence"] = 1.0
        _merge_goal(
            result["userGoals"], "goal", target["statement"], "confirmed",
            stage_id, turn_id, 1.0,
        )
        outcome = "confirmed"
    elif any(marker in compact for marker in rejection_markers):
        target["status"] = "rejected"
        target["confidence"] = 0.0
        outcome = "rejected"
    else:
        return result, None
    target["lastUpdatedStageId"] = _source(stage_id)
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result), {"id": target["id"], "status": outcome}


def resolve_intent_hypothesis(
    context,
    hypothesis_id,
    action,
    *,
    candidate_text=None,
    supersedes_ids=None,
    evidence_id=None,
    stage_id=None,
    turn_id=None,
    display_statement=None,
    display_language=None,
):
    """Apply explicit card feedback to one displayed tentative hypothesis."""
    result = normalize_design_context(context)
    target = next((
        item for item in result["intentHypotheses"]
        if item.get("id") == _source(hypothesis_id)
    ), None)
    if target is None:
        raise ValueError("INTENT_HYPOTHESIS_NOT_FOUND")
    if target.get("status") != "tentative" or not target.get("displayed"):
        raise ValueError("INTENT_HYPOTHESIS_STALE")
    if action not in {"confirm", "reject", "revise"}:
        raise ValueError("INVALID_INTENT_FEEDBACK")

    if action == "reject":
        target["status"] = "rejected"
        target["confidence"] = 0.0
        if evidence_id:
            target["contradictingEvidenceIds"] = list(dict.fromkeys(
                target.get("contradictingEvidenceIds", []) + [_source(evidence_id)]
            ))[:32]
    else:
        original = target.get("statement")
        replacement = (
            sanitize_user_design_text(candidate_text)
            if action == "revise"
            else original
        )
        if not replacement or len(replacement) < 4:
            raise ValueError("INVALID_INTENT_FEEDBACK")
        supersedes = set(
            _source(value) for value in (supersedes_ids or []) if _source(value)
        )
        supersedes.discard(target["id"])
        known_confirmed = {
            item["id"] for item in result["intentHypotheses"]
            if item.get("status") == "confirmed"
        }
        if not supersedes.issubset(known_confirmed):
            raise ValueError("INVALID_INTENT_SUPERSEDES")
        superseded_statements = set()
        for item in result["intentHypotheses"]:
            if item.get("id") in supersedes:
                superseded_statements.add(_text(item.get("statement")).casefold())
                item["status"] = "superseded"
                item["lastUpdatedStageId"] = _source(stage_id)
        for goal in result["userGoals"]:
            if (
                goal.get("authority") == "confirmed"
                and goal.get("status") == "active"
                and _text(goal.get("goal")).casefold() in superseded_statements
            ):
                goal["status"] = "superseded"
        target["originalStatement"] = original if replacement != original else None
        target["statement"] = replacement
        target["topicKey"] = infer_intent_topic(replacement)
        target["status"] = "confirmed"
        target["confidence"] = 1.0
        target["origin"] = "user_revision" if action == "revise" else target.get("origin")
        target["confirmedAtStageId"] = _source(stage_id)
        target["feedbackAction"] = action
        target["displayStatement"] = (
            sanitize_user_design_text(display_statement) or replacement
        )
        target["displayLanguage"] = _text(display_language, 16) or None
        if evidence_id:
            target["supportingEvidenceIds"] = list(dict.fromkeys(
                target.get("supportingEvidenceIds", []) + [_source(evidence_id)]
            ))[:32]
        _merge_goal(
            result["userGoals"],
            "goal",
            replacement,
            "confirmed",
            stage_id,
            turn_id,
            1.0,
        )

    target["lastUpdatedStageId"] = _source(stage_id)
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result), target["id"]


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


def question_dedup_key(value):
    """Return a conservative identity for visibly equivalent questions.

    Answer-format suffixes are presentation details, not separate design
    questions.  Keep the comparison deliberately narrower than semantic
    similarity so two genuinely different trade-off questions are preserved.
    """
    clean = _text(value).casefold()
    clean = re.sub(r"[\u201c\u201d\u2018\u2019\"'\u300c\u300d\u300e\u300f]", "", clean)
    clean = re.sub(
        r"^(?:\u8bf7)?(?:\u53ea|\u4ec5)?\u786e\u8ba4\s*[:\uFF1A]\s*",
        "",
        clean,
    )
    clean = re.sub(
        r"(?:"
        r"(?:[?\uFF1F,\uFF0C;\uFF1B:\uFF1A.\u3002!\uFF01]\s*)?"
        r"(?:(?:please\s+)?(?:answer|reply)\s+(?:with\s+)?)?"
        r"yes\s*(?:or|/)\s*no\s*(?:only)?"
        r"|(?:[?\uFF1F,\uFF0C;\uFF1B:\uFF1A.\u3002!\uFF01]\s*)?"
        r"(?:\u8bf7)?(?:\u76f4\u63a5)?(?:\u56de\u7b54|\u56de\u590d)?\s*"
        r"\u662f\s*(?:\u6216|\u8fd8\u662f|/)\s*\u5426\s*"
        r"(?:\u5373\u53ef|\u5c31\u53ef\u4ee5)?"
        r")[.\u3002!\uFF01?\uFF1F]*$",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip(" ?\uFF1F,\uFF0C;\uFF1B:\uFF1A.\u3002!\uFF01").casefold()


def deduplicate_open_questions(context, preferred_questions=None):
    """Collapse legacy duplicate questions while preserving the oldest ID."""
    result = normalize_design_context(context)
    preferred = {
        question_dedup_key(value): _text(value)
        for value in (preferred_questions or [])
        if question_dedup_key(value)
    }
    merged = []
    by_key = {}
    status_rank = {"open": 0, "ignored": 1, "answered": 2, "resolved": 2}
    for item in result["openQuestions"]:
        key = question_dedup_key(item.get("question"))
        source_key = _text(item.get("sourceKey"), 192)
        identity = ("source", source_key) if source_key else ("text", key)
        existing = by_key.get(identity)
        if existing is None and key:
            existing = next(
                (candidate for candidate in merged
                 if question_dedup_key(candidate.get("question")) == key),
                None,
            )
        if existing is None:
            merged.append(item)
            by_key[identity] = item
            continue
        if status_rank.get(item.get("status"), 0) > status_rank.get(existing.get("status"), 0):
            for field in (
                "status", "resolvedByTurnId", "answeredAtStageId",
                "ignoredAtStageId", "ignoredAt", "updatedFromTurnId",
            ):
                existing[field] = item.get(field)
        if not existing.get("sourceKey") and source_key:
            existing["sourceKey"] = source_key
        if key in preferred:
            existing["question"] = preferred[key]
    result["openQuestions"] = merged
    return normalize_design_context(result)


def _merge_open_question(result, entry, stage_id, turn_id, user_text):
    question = sanitize_user_design_text(entry.get("question"))
    is_visible_output = entry.get("sourceKind") == "visible_output"
    if not question or (
        not is_visible_output
        and not is_design_level_question(question, entry.get("evidenceText"))
    ):
        return False

    target_id = _source(entry.get("targetId"))
    existing = next(
        (
            item for item in result["openQuestions"]
            if (target_id and item.get("id") == target_id)
            or (
                entry.get("sourceKey")
                and item.get("sourceKey") == entry.get("sourceKey")
            )
            or question_dedup_key(item.get("question")) == question_dedup_key(question)
        ),
        None,
    )
    status = entry.get("status", "open")

    if status in {"resolved", "answered"}:
        if not _verified_user_evidence(entry.get("evidenceText"), user_text):
            return False
        if existing is None:
            return False
        existing["status"] = "answered"
        existing["updatedFromTurnId"] = _source(turn_id)
        if entry.get("sourceKey"):
            existing["sourceKey"] = _text(entry.get("sourceKey"), 192)
        if entry.get("preferQuestion"):
            existing["question"] = question
        existing["resolvedByTurnId"] = _source(turn_id)
        existing["answeredAtStageId"] = _source(stage_id)
        return True

    if existing is not None:
        if existing.get("status") == "resolved":
            existing["status"] = "open"
        existing["updatedFromTurnId"] = _source(turn_id)
        if entry.get("sourceKind") == "visible_output":
            existing["sourceKind"] = "visible_output"
        if entry.get("preferQuestion"):
            existing["question"] = question
        return True

    result["openQuestions"].append({
        "id": _stable_id("question", question, stage_id, turn_id),
        "question": question,
        "status": "open",
        "sourceStageId": _source(stage_id),
        "sourceTurnId": _source(turn_id),
        "updatedFromTurnId": _source(turn_id),
        "resolvedByTurnId": None,
        "answeredAtStageId": None,
        "ignoredAtStageId": None,
        "ignoredAt": None,
        "sourceKind": _text(entry.get("sourceKind"), 32) or "model_patch",
        "sourceKey": _text(entry.get("sourceKey"), 192) or None,
    })
    return True


def add_open_question(
    context,
    question,
    stage_id=None,
    turn_id=None,
    *,
    source_kind="visible_output",
    source_key=None,
    prefer_question=False,
):
    """Add an assistant-raised open question using the normal provenance rules."""
    result = normalize_design_context(context)
    clean = _text(question)
    if not clean or (
        source_kind != "visible_output" and not is_design_level_question(clean)
    ):
        return result

    _merge_open_question(
        result,
        {
            "question": clean,
            "status": "open",
            "sourceKind": source_kind,
            "sourceKey": source_key,
            "preferQuestion": prefer_question,
        },
        stage_id,
        turn_id,
        None,
    )
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result)


def apply_question_answer_review(context, answered_question_ids, stage_id=None, turn_id=None):
    """Mark only model-reviewed question IDs as answered in this Stage snapshot."""
    result = normalize_design_context(context)
    answered = {
        _source(value) for value in (answered_question_ids or []) if _source(value)
    }
    if not answered:
        return result
    for item in result["openQuestions"]:
        if item.get("id") not in answered or item.get("status") != "open":
            continue
        item["status"] = "answered"
        item["updatedFromTurnId"] = _source(turn_id)
        item["resolvedByTurnId"] = _source(turn_id)
        item["answeredAtStageId"] = _source(stage_id)
    result["updatedFromStageId"] = _source(stage_id)
    result["updatedFromTurnId"] = _source(turn_id)
    return normalize_design_context(result)


def apply_question_feedback(context, question_id, action, stage_id=None, changed_at=None):
    """Apply a deterministic ignore/restore action to one visible question."""
    result = normalize_design_context(context)
    target = next(
        (item for item in result["openQuestions"] if item.get("id") == _source(question_id)),
        None,
    )
    if target is None:
        return result, False
    if action == "ignore" and target.get("status") == "open":
        target["status"] = "ignored"
        target["ignoredAtStageId"] = _source(stage_id)
        target["ignoredAt"] = _text(changed_at, 64) or None
    elif action == "restore" and target.get("status") == "ignored":
        target["status"] = "open"
        target["ignoredAtStageId"] = None
        target["ignoredAt"] = None
    else:
        return result, False
    result["updatedFromStageId"] = _source(stage_id)
    return normalize_design_context(result), True


def extract_explicit_user_memory(user_text):
    """Conservatively retain design language that came from the user's turn."""
    text = sanitize_user_design_text(user_text)
    if not text or len(text) < 4:
        return [], []

    goal_pattern = re.compile(
        r"(?:\b(?:i\s+(?:want|prefer|would\s+like|lean\s+toward|care\s+more\s+about)|"
        r"please\s+(?:make|change|adjust|keep|increase|reduce|move))\b|"
        r"\u6211(?:\u60f3\u8981?|\u5e0c\u671b|\u503e\u5411(?:\u4e8e)?|\u66f4\u5728\u610f)|"
        r"\u8bf7(?:\u4fdd\u6301|\u589e\u52a0|\u51cf\u5c11|\u8c03\u6574|\u6539|\u79fb\u52a8))",
        flags=re.IGNORECASE,
    )
    constraint_pattern = re.compile(
        r"(?:\b(?:i\s+(?:do\s+not|don't)\s+want|please\s+(?:avoid|preserve)|"
        r"must\s+not|do\s+not|don't)\b|"
        r"\u6211\u4e0d\u60f3|\u8bf7(?:\u907f\u514d|\u4fdd\u7559|\u4e0d\u8981)|"
        r"\b(?:keep|preserve|fair|solvable|readable)\b|\u4e0d\u8981|\u4e0d\u80fd|\u5fc5\u987b|\u4e0d\u7834\u574f|"
        r"\u4fdd\u6301|\u4fdd\u7559|\u516c\u5e73|\u53ef\u89e3)",
        flags=re.IGNORECASE,
    )
    question_start = re.compile(
        r"^(?:how|what|which|where|why|can|could|would|should|do|does|is|are|"
        r"\u5982\u4f55|\u600e\u4e48|\u4ec0\u4e48|\u54ea|\u662f\u5426|\u80fd\u5426|\u53ef\u4ee5\u5417|\u6211\u8be5)",
        flags=re.IGNORECASE,
    )
    evaluative_view_pattern = re.compile(
        r"(?:\b(?:i\s+(?:think|feel|find)|in\s+my\s+view|to\s+me)\b|"
        r"\u6211(?:\u89c9\u5f97|\u8ba4\u4e3a|\u611f\u89c9|\u53d1\u73b0)|\u5728\u6211\u770b\u6765|\u5bf9\u6211\u6765\u8bf4)",
        flags=re.IGNORECASE,
    )
    design_subject_pattern = re.compile(
        r"(?:\b(?:box|target|player|route|path|corridor|layout|wall|water|push|"
        r"start|distance|space|difficulty|rhythm|choice)\b|"
        r"\u7bb1\u5b50|\u76ee\u6807|\u73a9\u5bb6|\u8def\u7ebf|\u8def\u5f84|\u901a\u9053|\u5e03\u5c40|"
        r"\u5899|\u6c34\u57df|\u63a8\u52a8|\u8d77\u70b9|\u8ddd\u79bb|\u7a7a\u95f4|\u96be\u5ea6|\u8282\u594f|\u9009\u62e9)",
        flags=re.IGNORECASE,
    )
    evaluation_pattern = re.compile(
        r"(?:\b(?:too\s+(?:close|far|long|short|easy|hard|tight|open)|"
        r"crowded|cramped|unclear|clearer|harder|easier|awkward)\b|"
        r"\u592a(?:\u8fd1|\u8fdc|\u957f|\u77ed|\u6324|\u7d27|\u7a7a|\u96be|\u5bb9\u6613)|"
        r"\u6328\u5f97\u592a\u8fd1|\u62e5\u6324|\u5c40\u4fc3|\u4e0d\u6e05\u695a|\u66f4\u6e05\u695a|\u66f4\u96be|\u66f4\u5bb9\u6613|\u522b\u626d)",
        flags=re.IGNORECASE,
    )
    uncertainty_pattern = re.compile(
        r"(?:\b(?:maybe|perhaps|might|not\s+sure)\b|\u4e0d\u786e\u5b9a|\u4e5f\u8bb8|\u53ef\u80fd)",
        flags=re.IGNORECASE,
    )
    clauses = [
        part.strip()
        for part in re.split(r"(?<=[.!?\u3002\uFF01\uFF1F;\uFF1B])\s*|\n+", text)
        if part.strip()
    ]
    goals = []
    constraints = []
    for clause in clauses:
        lowered = clause.casefold()
        is_question = clause.endswith(("?", "\uFF1F")) or bool(question_start.search(lowered))
        if is_question:
            continue
        explicit_evaluation = bool(
            evaluative_view_pattern.search(clause)
            and design_subject_pattern.search(clause)
            and evaluation_pattern.search(clause)
            and not uncertainty_pattern.search(clause)
        )
        if goal_pattern.search(clause) or explicit_evaluation:
            goals.append(clause)
        if constraint_pattern.search(clause):
            constraints.append(clause)
    return goals, constraints


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
            authority = "inferred"
            _merge_goal(
                result["userGoals"], "goal", entry["goal"], authority,
                stage_id, turn_id, 1.0 if authority == "explicit" else entry.get("confidence", 0.5),
            )
        for entry in patch["constraints"]:
            authority = "inferred"
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
        inferred = [
            item for item in result["userGoals"] + result["designConstraints"]
            if item.get("authority") == "inferred" and item.get("status") == "active"
        ]
        if len(inferred) == 1:
            inferred[0]["status"] = "superseded"

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
