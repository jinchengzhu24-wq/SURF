"""Evidence-bound proposal semantics; no natural-language keyword inference.

The model separates a design goal, editable components and spatial references.
This module validates provenance and supplies the same record to every phase.
"""
import copy
import hashlib
import json
import re
from conversation_evidence import CONTEXT_VERSION, user_sources, evidence_signature
from proposal_diagnostics import ProposalValidationError, structural_issue
from component_patches import PatchRequest, Target


POLICY_VERSION = 1
MEANING_REVIEW_VERSION = "meaning-preservation-2"
EDITABLE = {"water", "internal_walls"}
PROTECTED = {"outer_shell", "player", "boxes", "targets"}
COMPONENTS = EDITABLE | PROTECTED | {"gameplay", "unknown"}
DIMENSIONS = {"visual_direction", "experience_goal", "edit_scope", "mechanism", "binding", "preserve", "none"}


def evidence_item_schema(extra=None):
    properties = {"statement": {"type": "string"}, "sourceTurnId": {"type": "string"},
        "evidenceSpan": {"type": "string"}, **(extra or {})}
    return {"type": "object", "additionalProperties": False, "properties": properties,
        "required": list(properties)}


def topic_schema():
    properties = {
        "aspect": {"type": "string", "enum": ["visual", "gameplay", "mixed", "unspecified"]},
        "goals": {"type": "array", "items": evidence_item_schema()},
        "editScope": {"type": "array", "items": evidence_item_schema({
            "component": {"type": "string", "enum": sorted(COMPONENTS)}})},
        "focus": {"type": "array", "items": evidence_item_schema({
            "entities": {"type": "array", "items": {"type": "string"}}})},
        "preserve": {"type": "array", "items": evidence_item_schema()},
        "nextQuestionDimension": {"type": "string", "enum": sorted(DIMENSIONS)},
        "sufficient": {"type": "boolean"},
    }
    return {"type": "object", "additionalProperties": False, "properties": properties,
        "required": list(properties)}


def turn_schema():
    change_properties = {
        "component": {"type": "string", "enum": sorted(COMPONENTS)},
        "operation": {"type": "string", "enum": ["change", "preserve", "mention"]},
        "property": {"type": "string", "enum": ["count", "position", "shape", "layout", "route", "push_order", "switching", "rhythm", "appearance", "unknown"]},
        "evidenceSpan": {"type": "string"},
    }
    properties = {
        "acts": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string",
            "enum": ["evaluation", "intent", "explanation_request", "idea_request", "revision_request", "unclear"]}},
        "elements": {"type": "array", "maxItems": 6, "items": {"type": "string",
            "enum": sorted(COMPONENTS - {"gameplay"})}},
        "evidenceSpan": {"type": "string"}, "directionSufficient": {"type": "boolean"},
        "mapRelated": {"type": "boolean"},
        "changes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": change_properties, "required": list(change_properties)}},
        "proposalUnderstanding": {"anyOf": [topic_schema(), {"type": "null"}]},
        "humanEditEngagement": {"anyOf": [engagement_schema(), {"type": "null"}]},
    }
    return {"type": "object", "additionalProperties": False, "properties": properties,
        "required": list(properties)}


def engagement_schema():
    return {"type": "object", "additionalProperties": False,
            "properties": {"decision": {"type": "string", "enum": ["engage", "unchanged", "unclear"]},
                           "evidenceSpan": {"type": "string"}},
            "required": ["decision", "evidenceSpan"]}


def validate_engagement(value, latest):
    if not isinstance(value, dict) or set(value) != {"decision", "evidenceSpan"}:
        raise ValueError("Invalid human-edit engagement component.")
    decision, span = value["decision"], value["evidenceSpan"]
    if decision not in {"engage", "unchanged", "unclear"} or not isinstance(span, str):
        raise ValueError("Invalid human-edit engagement decision.")
    if (decision == "engage" and not span.strip()) or (span and span not in latest):
        raise ValueError("Engagement needs exact current-user evidence.")
    return {"decision": decision, "evidenceSpan": span}


def designer_sources(context, latest=None):
    sources = user_sources((context or {}).get("dialogueEvidence"))
    sources.update({str(x["id"]): str(x["content"]) for x in (context or {}).get("userTurns") or []})
    if latest is not None:
        # The HTTP caller binds this temporary ID to the inserted Turn atomically.
        sources["latest"] = latest
    return sources


def validate_topic(payload, sources, snapshot, source_roles=None):
    if not isinstance(payload, dict) or set(payload) != set(topic_schema()["properties"]):
        raise ProposalValidationError([structural_issue("proposalUnderstanding", "TOPIC_SCHEMA_INVALID")],
            message="Proposal understanding needs separate goals, edit scope, focus and preservation.")
    result = copy.deepcopy(payload)
    issues = []
    def fail(path, code, kind="unknown"):
        issues.append(structural_issue("proposalUnderstanding." + path, code, kind))
    if not isinstance(payload["aspect"], str) or payload["aspect"] not in {"visual", "gameplay", "mixed", "unspecified"}:
        fail("aspect", "ASPECT_INVALID")
    if not isinstance(payload["nextQuestionDimension"], str) or payload["nextQuestionDimension"] not in DIMENSIONS:
        fail("nextQuestionDimension", "QUESTION_DIMENSION_INVALID")
    if type(payload["sufficient"]) is not bool:
        fail("sufficient", "SUFFICIENCY_TYPE_INVALID")
    elif payload["sufficient"] != (payload["nextQuestionDimension"] == "none"):
        fail("sufficient", "SUFFICIENCY_INCONSISTENT")
        fail("nextQuestionDimension", "SUFFICIENCY_INCONSISTENT")
    labels = {str(x.get("id") or "").upper() for x in (snapshot or {}).get("entities") or []}
    for key in ("goals", "editScope", "focus", "preserve"):
        items = payload[key]
        fields = {"statement", "sourceTurnId", "evidenceSpan"} | ({"component"} if key == "editScope" else {"entities"} if key == "focus" else set())
        if not isinstance(items, list) or len(items) > 48:
            fail(key, "EVIDENCE_COLLECTION_INVALID")
            continue
        for index, item in enumerate(items):
            path = f"{key}[{index}]"
            if not isinstance(item, dict) or set(item) != fields:
                fail(path, "EVIDENCE_ITEM_SCHEMA_INVALID")
                continue
            span = item["evidenceSpan"]
            identity = item["sourceTurnId"]
            source = sources.get(identity) if isinstance(identity, str) else None
            kind = ("invalid_type" if not isinstance(identity, str) else "latest_user" if identity == "latest" else
                    "historical_user" if identity in sources else "assistant" if (source_roles or {}).get(identity) == "assistant" else
                    "external_reference" if identity.startswith("user:") else "server" if identity == "snapshot" else "unknown")
            if not isinstance(identity, str):
                fail(path + ".sourceTurnId", "SOURCE_ID_TYPE_INVALID", kind)
            elif source is None:
                fail(path + ".sourceTurnId", "SOURCE_NOT_FOUND", kind)
            elif not isinstance(source, str):
                fail(path + ".sourceTurnId", "SOURCE_CONTENT_TYPE_INVALID", kind)
            if not isinstance(span, str):
                fail(path + ".evidenceSpan", "EVIDENCE_SPAN_TYPE_INVALID", kind)
            elif not span.strip():
                fail(path + ".evidenceSpan", "EVIDENCE_SPAN_EMPTY", kind)
            elif isinstance(source, str) and span not in source:
                fail(path + ".evidenceSpan", "EVIDENCE_SPAN_NOT_EXACT", kind)
            if not isinstance(item["statement"], str) or not item["statement"].strip() or len(item["statement"]) > 600:
                fail(path + ".statement", "STATEMENT_INVALID")
            if key == "editScope" and (not isinstance(item["component"], str) or item["component"] not in COMPONENTS):
                fail(path + ".component", "EDIT_COMPONENT_INVALID")
            if key == "focus" and (not isinstance(item["entities"], list) or
                    any(not isinstance(x, str) or (snapshot is not None and x.upper() not in labels) for x in item["entities"])):
                fail(path + ".entities", "CURRENT_ENTITY_INVALID")
    if payload["sufficient"] and not payload["goals"]:
        fail("goals", "DESIGNER_GOAL_MISSING")
    if payload["aspect"] == "visual" and payload["nextQuestionDimension"] == "mechanism":
        fail("nextQuestionDimension", "VISUAL_MECHANISM_REQUIRED")
    if issues:
        raise ProposalValidationError(issues)
    return result


def understanding_patch_request(frozen, error, sources, snapshot):
    """Target owned structural paths only. Never rebind evidence or infer meaning."""
    paths = []
    for issue in getattr(error, "issues", []):
        path = issue["fieldPath"]
        if not path.startswith("proposalUnderstanding"):
            continue
        parts = []
        for word, number in re.findall(r"([A-Za-z][A-Za-z0-9_]*)|\[([0-9]+)\]", path):
            parts.append(word if word else int(number))
        paths.append(tuple(parts))
    if not paths:
        paths = [("proposalUnderstanding",)]
    paths = list(dict.fromkeys(paths))
    paths = [path for path in paths if not any(path != other and path[:len(other)] == other for other in paths)]
    types = {"aspect": (str,), "nextQuestionDimension": (str,), "sufficient": (bool,),
             "goals": (list,), "editScope": (list,), "focus": (list,), "preserve": (list,),
             "entities": (list,), "sourceTurnId": (str,), "evidenceSpan": (str,), "statement": (str,), "component": (str,)}
    targets = [Target("proposalUnderstanding" if path == ("proposalUnderstanding",) else
                      ".".join(str(part) for part in path), ("understanding", *path),
                      (dict,) if isinstance(path[-1], int) or path == ("proposalUnderstanding",) else types[path[-1]]) for path in paths]
    return PatchRequest("understanding", {"understanding": frozen}, targets,
        {"snapshot": snapshot, **{f"user:{key}": content for key, content in sources.items()}})


def critical_review_reason(understanding, context):
    changes = (understanding or {}).get("changes")
    if changes is None and set(understanding.get("elements") or []) & PROTECTED:
        return "legacy_protected_annotation"
    if any(x.get("component") in PROTECTED and x.get("operation", "change") == "change"
            and x.get("property") in {"count", "position", "shape", "layout"} for x in changes or []):
        return "protected_change"
    previous = (context or {}).get("understanding") or {}
    proposed = understanding.get("proposalUnderstanding") or {}
    old_scope = {x["component"] for x in previous.get("editScope") or []}
    new_scope = {x["component"] for x in proposed.get("editScope") or []}
    if new_scope & PROTECTED:
        return "protected_edit_scope"
    if old_scope and new_scope != old_scope:
        return "edit_scope_changed"
    old_scope_meanings = {(x["component"], x["sourceTurnId"], x["evidenceSpan"], x["statement"])
                         for x in previous.get("editScope") or []}
    new_scope_meanings = {(x["component"], x["sourceTurnId"], x["evidenceSpan"], x["statement"])
                         for x in proposed.get("editScope") or []}
    if old_scope_meanings and old_scope_meanings != new_scope_meanings:
        return "edit_scope_meaning_changed"
    for key in ("goals", "preserve"):
        old_evidence = {(x["sourceTurnId"], x["evidenceSpan"], x["statement"]) for x in previous.get(key) or []}
        new_evidence = {(x["sourceTurnId"], x["evidenceSpan"], x["statement"]) for x in proposed.get(key) or []}
        if not old_evidence.issubset(new_evidence):
            return "goal_or_preservation_changed"
        if key == "preserve" and new_evidence - old_evidence:
            return "new_preservation_requirement"
    # Legacy topics have no authoritative interpretation; reconstruct and review
    # before using their old annotations as a new permission decision.
    if (context or {}).get("topicId") and not previous:
        return "legacy_topic_reconstruction"
    return None


def bind_latest(understanding, turn_id, version_id):
    result = copy.deepcopy(understanding)
    topic = result.get("proposalUnderstanding")
    if topic:
        for key in ("goals", "editScope", "focus", "preserve"):
            for item in topic[key]:
                if item["sourceTurnId"] == "latest":
                    item["sourceTurnId"] = turn_id
        result["proposalUnderstanding"] = topic
    result["semanticPolicyVersion"] = POLICY_VERSION
    result["dialogueContextVersion"] = CONTEXT_VERSION
    result["versionId"] = version_id
    return result


def reusable_understanding(result, version_id):
    if (not isinstance(result, dict) or result.get("semanticPolicyVersion") != POLICY_VERSION
            or result.get("dialogueContextVersion") != CONTEXT_VERSION
            or result.get("versionId") != version_id
            or not isinstance(result.get("evidenceSignature"), str) or len(result["evidenceSignature"]) != 64):
        return False
    reason = critical_review_reason(result, None)
    review = result.get("criticalReview") or {}
    return reason is None or (review.get("verified") is True and review.get("policyVersion") == MEANING_REVIEW_VERSION)


def validate_plan_scope(plan, topic):
    scope = {x["component"] for x in (topic or {}).get("editScope") or []} & EDITABLE
    if not scope:
        return
    components = {"add_water": "water", "remove_water": "water",
        "add_wall": "internal_walls", "remove_wall": "internal_walls"}
    for strategy in plan.strategies:
        if any(components.get(operator) not in scope for operator in strategy.operators):
            raise ValueError("RevisionPlan changes components outside the designer's current requested edit scope.")


def topic_signature(snapshot, context, latest):
    data = {"snapshot": snapshot, "sources": designer_sources(context, latest),
        "questions": (context or {}).get("questions") or [], "answers": (context or {}).get("answers") or [],
        "understanding": (context or {}).get("understanding"), "latest": latest,
        "policyVersion": POLICY_VERSION}
    data["contextVersion"] = CONTEXT_VERSION
    data["meaningReviewVersion"] = MEANING_REVIEW_VERSION
    data["dialogueSignature"] = evidence_signature((context or {}).get("dialogueEvidence"))
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def protected_request_message(understanding, language):
    names = {"targets": ("目标", "targets"), "boxes": ("箱子", "boxes"),
        "player": ("玩家起点", "player start"), "outer_shell": ("外壳", "outer shell")}
    properties = {"position": ("位置", "position"), "count": ("数量", "count"),
        "shape": ("形状", "shape"), "layout": ("布局", "layout")}
    changes = [x for x in understanding.get("changes") or [] if x.get("component") in PROTECTED
        and x.get("operation") == "change" and x.get("property") in properties]
    if not changes or not (understanding.get("criticalReview") or {}).get("verified"):
        raise ValueError("A permission refusal needs independently reviewed change evidence.")
    chinese = language == "zh-CN"
    index = 0 if chinese else 1
    details = list(dict.fromkeys(names[x["component"]][index] + ("的" if chinese else " ") + properties[x["property"]][index] for x in changes))
    evidence = "；".join(dict.fromkeys(x["evidenceSpan"] for x in changes))
    if chinese:
        return f"你提出的“{evidence}”要求改变{'、'.join(details)}，超出了当前修改权限。当前只能调整水域和内部墙体；已保存的地图和此前讨论仍保留。"
    return f'Your request “{evidence}” changes {", ".join(details)}, beyond the current editing permissions. Water and internal walls remain editable; the saved map and prior discussion are retained.'
