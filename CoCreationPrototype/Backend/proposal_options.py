"""Private proposal directions and evidence-bound choice; never map authorization."""
import copy
import hashlib
import json

from proposal_diagnostics import ProposalValidationError, structural_issue


VERSION = 1
FIELDS = {"summary", "components", "focus", "mechanism", "effect", "tradeoff", "preserve"}
EDITABLE = {"water", "internal_walls"}
ENVELOPE_FIELDS = {"body", "question", "options", "comparedOptions", "discriminant"}


def reuses_options(discovery, snapshot):
    return bool(current_set(discovery, snapshot) and
                (discovery.get("optionDecision") or {}).get("selection") != "revise")


def clarification_response_format(discovery, snapshot):
    """The wire contract and parser share the same server-owned option mode."""
    option = {"type": "object", "additionalProperties": False,
        "properties": {key: ({"type": "array", "minItems": 1, "maxItems": 2,
            "items": {"type": "string", "enum": sorted(EDITABLE)}} if key == "components" else
            {"type": "string", "minLength": 1, "maxLength": 240}) for key in sorted(FIELDS)},
        "required": sorted(FIELDS)}
    properties = {"body": {"type": "string"}, "question": {"type": "string"},
        "options": ({"type": "null"} if reuses_options(discovery, snapshot) else
                    {"type": "array", "maxItems": 3, "items": option}),
        "comparedOptions": {"type": "array", "items": {"type": "string"}},
        "discriminant": {"type": "string", "maxLength": 240}}
    return {"type": "json_schema", "json_schema": {"name": "cocreation_proposal_options",
        "strict": True, "schema": {"type": "object", "additionalProperties": False,
            "properties": properties, "required": sorted(properties)}}}


def review_response_format():
    properties = {"bodyIssues": {"type": "array", "items": {"type": "string"}},
        "questionIssue": {"type": ["string", "null"]},
        "optionIssues": {"type": "array", "items": {"type": "string"}}}
    return {"type": "json_schema", "json_schema": {"name": "cocreation_proposal_options_review",
        "strict": True, "schema": {"type": "object", "additionalProperties": False,
            "properties": properties, "required": sorted(properties)}}}


def validate_envelope_shape(payload, discovery, snapshot):
    """Report technical fields together, before any semantic component is locked."""
    if not isinstance(payload, dict) or set(payload) - ENVELOPE_FIELDS:
        invalid("envelope", "OPTION_ENVELOPE_INVALID")
    properties = clarification_response_format(discovery, snapshot)["json_schema"]["schema"]["properties"]
    issues = []
    for field, schema in properties.items():
        value = payload.get(field)
        kind = schema["type"]
        valid = field in payload and (
            (kind == "string" and isinstance(value, str)) or
            (kind == "array" and isinstance(value, list)) or
            (kind == "null" and value is None))
        if valid and field == "comparedOptions":
            valid = all(isinstance(item, str) for item in value)
        if valid and field == "discriminant":
            valid = len(value) <= 240
        if not valid:
            issues.append(structural_issue(field, "OPTION_FIELD_INVALID", "invalid_type"))
    if issues:
        raise ProposalValidationError(issues)


def context_signature(discovery):
    data = {key: (discovery or {}).get(key) for key in ("optionSet", "optionQuestion", "lastQuestionText")}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def invalid(field, code):
    raise ProposalValidationError([structural_issue(field, code)])


def decision_schema():
    return {"type": "object", "additionalProperties": False, "properties": {
        "preferredOptionIds": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "rejectedOptionIds": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "selection": {"type": "string", "enum": ["explicit", "inferred", "unclear", "revise"]},
        "evidenceSpan": {"type": "string"}},
        "required": ["preferredOptionIds", "rejectedOptionIds", "selection", "evidenceSpan"]}


def current_set(discovery, snapshot=None):
    value = (discovery or {}).get("optionSet")
    if not isinstance(value, dict) or value.get("schemaVersion") != VERSION:
        return None
    if value.get("topicId") != (discovery or {}).get("topicId"):
        return None
    if value.get("stageId") != (discovery or {}).get("versionId"):
        return None
    if snapshot is not None and value.get("mapFingerprint") != snapshot.get("mapFingerprint"):
        return None
    return value


def validate_decision(value, latest, discovery, snapshot=None):
    option_set = current_set(discovery, snapshot)
    if not option_set:
        return None
    if not isinstance(value, dict) or set(value) != set(decision_schema()["properties"]):
        invalid("optionDecision", "OPTION_DECISION_INVALID")
    ids = {item["id"] for item in all_options(option_set)}
    for field in ("preferredOptionIds", "rejectedOptionIds"):
        items = value[field]
        if (not isinstance(items, list) or len(items) > 3
                or any(not isinstance(item, str) or item not in ids for item in items)
                or len(set(items)) != len(items)):
            invalid("optionDecision", "OPTION_REFERENCE_INVALID")
    preferred, rejected = value["preferredOptionIds"], value["rejectedOptionIds"]
    if set(preferred) & set(rejected):
        invalid("optionDecision", "OPTION_DECISION_CONFLICT")
    selection, span = value["selection"], value["evidenceSpan"]
    if (not isinstance(selection, str) or selection not in {"explicit", "inferred", "unclear", "revise"}
            or not isinstance(span, str) or not span.strip() or span not in latest
            or (selection in {"explicit", "inferred"} and not preferred)
            or (selection in {"unclear", "revise"} and preferred)):
        invalid("optionDecision", "OPTION_EVIDENCE_INVALID")
    return copy.deepcopy(value)


def build_set(raw, discovery, snapshot):
    if not isinstance(raw, list) or len(raw) > 3:
        invalid("options", "OPTION_SET_INVALID")
    result = []
    scope = {item.get("component") for item in (discovery.get("understanding") or {}).get("editScope") or []} & EDITABLE
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != FIELDS:
            invalid("options", "OPTION_SCHEMA_INVALID")
        components = item["components"]
        if (not isinstance(components, list) or not components or len(components) > 2
                or any(not isinstance(part, str) or part not in EDITABLE for part in components)
                or len(set(components)) != len(components) or (scope and not set(components) <= scope)):
            invalid("options", "OPTION_PERMISSION_INVALID")
        if any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 240
               for key in FIELDS - {"components"}):
            invalid("options", "OPTION_DETAIL_INVALID")
        canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
        identity = hashlib.sha256((str(discovery["topicId"]) + str(snapshot.get("mapFingerprint")) + canonical).encode()).hexdigest()[:16]
        result.append({**copy.deepcopy(item), "id": "option-" + identity, "label": chr(65 + index)})
    if len({item["id"] for item in result}) != len(result):
        invalid("options", "OPTION_DUPLICATE")
    return {"schemaVersion": VERSION, "topicId": discovery["topicId"],
        "stageId": discovery.get("versionId"), "mapFingerprint": snapshot.get("mapFingerprint"),
        "options": result}


def parse_envelope(payload, discovery, snapshot):
    existing = current_set(discovery, snapshot)
    decision = discovery.get("optionDecision") or {}
    if existing and decision.get("selection") != "revise":
        if payload.get("options") is not None:
            invalid("options", "OPTION_SET_LOCKED")
        option_set = copy.deepcopy(existing)
        rejected = set(decision.get("rejectedOptionIds") or [])
        originals = all_options(option_set)
        option_set["options"] = [item for item in originals if item["id"] not in rejected]
        option_set["excludedOptions"] = [item for item in originals if item["id"] in rejected]
    else:
        option_set = build_set(payload.get("options"), discovery, snapshot)
    options = option_set["options"]
    compared = payload.get("comparedOptions")
    discriminator = payload.get("discriminant")
    if (not isinstance(compared, list) or any(not isinstance(item, str) for item in compared)
            or len(set(compared)) != len(compared) or not isinstance(discriminator, str) or len(discriminator) > 240):
        invalid("question", "OPTION_QUESTION_INVALID")
    aliases = {str(index + 1): item["id"] for index, item in enumerate(options)}
    ids = {item["id"] for item in options}
    compared = [aliases.get(item, item) for item in compared]
    question = payload.get("question")
    if len(options) >= 2:
        if len(compared) < 2 or not set(compared) <= ids or not discriminator.strip() or not isinstance(question, str) or not question.strip():
            invalid("question", "OPTION_QUESTION_UNBOUND")
    elif compared or question or discriminator:
        invalid("question", "OPTION_QUESTION_UNNECESSARY")
    return option_set, {"comparedOptionIds": compared, "discriminant": discriminator}


def render_options(option_set):
    # These are Kimi-authored, semantically reviewed strings; no fallback prose.
    return "\n\n".join(f"{item['label']}. " + " ".join(item[key].strip() for key in
        ("summary", "focus", "mechanism", "effect", "tradeoff", "preserve"))
        for item in option_set["options"])


def all_options(option_set):
    return sorted([*(option_set.get("options") or []), *(option_set.get("excludedOptions") or [])],
                  key=lambda item: item["label"])


def planning_projection(discovery):
    option_set = current_set(discovery)
    if not option_set:
        return None
    decision = discovery.get("optionDecision") or {}
    if decision.get("selection") == "revise":
        return None
    rejected = set(decision.get("rejectedOptionIds") or [])
    candidates = [item for item in all_options(option_set) if item["id"] not in rejected]
    preferred = decision.get("preferredOptionIds") or []
    if decision.get("selection") == "explicit":
        candidates = [item for item in candidates if item["id"] in preferred]
    candidates.sort(key=lambda item: preferred.index(item["id"]) if item["id"] in preferred else 3)
    return {"candidates": candidates[:1], "decision": decision,
        "rule": "Directions are AI proposal assumptions, not additional user requirements. Select one compatible route; "
                "do not switch away from an explicit choice or reverse/retract any designer goal. Explain automatic choices."}
