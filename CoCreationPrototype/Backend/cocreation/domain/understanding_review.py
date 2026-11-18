"""Independent review deltas against one immutable, validated understanding.

No persistence or natural-language inference. No deletion/reorder operator;
literal cross-item copies are rejected. Semantic equivalence is model-reviewed.
"""
import copy
import re

from component_patches import PatchRequest, PatchError, Target, signature
from proposal_diagnostics import ProposalValidationError, structural_issue
from proposal_understanding import evidence_item_schema, turn_schema


REVIEW_VERSION = "understanding-delta-3-quote-leaf"
REVIEW_FIELDS = {"decision": (str,), "issues": (list,), "corrections": (list,)}


def review_schema():
    turn = turn_schema()["properties"]
    item_types = [evidence_item_schema(), evidence_item_schema({"component": {"type": "string"}}),
        evidence_item_schema({"entities": {"type": "array", "items": {"type": "string"}}}),
        turn["changes"]["items"]]
    value_schema = {"anyOf": [{"type": "string"}, {"type": "boolean"}, *item_types,
        turn["optionDecision"], {"type": "array", "items": {"anyOf": [{"type": "string"}, *item_types]}}]}
    fields = {"targetId": {"type": "string"}, "reason": {"type": "string"},
              "value": value_schema,
              "evidenceRefs": {"type": "array", "items": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
        "properties": {"decision": {"type": "string", "enum": ["APPROVE", "CORRECT", "REJECT"]},
            "issues": {"type": "array", "items": {"type": "string"}},
            "corrections": {"type": "array", "maxItems": 32, "items": {
                "type": "object", "additionalProperties": False,
                "properties": fields, "required": list(fields)}}},
        "required": list(REVIEW_FIELDS)}


def review_targets(candidate):
    """IDs bind the whole base plus path/value; append never shifts old indices."""
    targets = []
    def add(path, value, mode="replace"):
        targets.append({"targetId": signature([REVIEW_VERSION, candidate, path, value, mode])[:24],
                        "path": list(path), "mode": mode, "currentValue": copy.deepcopy(value)})
    for key in ("acts", "elements", "evidenceSpan", "directionSufficient", "mapRelated", "optionDecision"):
        add((key,), candidate.get(key))
    for index, value in enumerate(candidate.get("changes") or []):
        add(("changes", index), value)
    add(("changes",), [], "append")
    topic = candidate.get("proposalUnderstanding")
    if topic:
        for key in ("aspect", "sufficient", "nextQuestionDimension"):
            add(("proposalUnderstanding", key), topic[key])
        for key in ("goals", "editScope", "focus", "preserve"):
            for index, value in enumerate(topic[key]):
                add(("proposalUnderstanding", key, index), value)
            add(("proposalUnderstanding", key), [], "append")
    return targets


def fail(path, code):
    raise ProposalValidationError([structural_issue(path, code)])


def merge_review(verdict, candidate, targets, sources):
    if isinstance(verdict, dict) and verdict.get("decision") == "REJECT":
        fail("criticalReview", "SEMANTIC_REVIEW_REJECTED")
    if (not isinstance(verdict, dict) or set(verdict) != set(REVIEW_FIELDS)
            or not isinstance(verdict.get("decision"), str)
            or verdict["decision"] not in {"APPROVE", "CORRECT", "REJECT"}
            or not isinstance(verdict.get("issues"), list)
            or any(not isinstance(x, str) for x in verdict["issues"])
            or not isinstance(verdict.get("corrections"), list)):
        fail("criticalReview", "REVIEW_COMPONENT_INVALID")
    decision, corrections = verdict["decision"], verdict["corrections"]
    if verdict["issues"] or (decision == "APPROVE" and corrections) or (decision == "CORRECT" and not corrections):
        fail("criticalReview", "REVIEW_DECISION_INCONSISTENT")
    if len(corrections) > 32:
        fail("corrections", "REVIEW_CORRECTIONS_INVALID")
    # Recompute rather than trusting a stale or mutated target inventory.
    if targets != review_targets(candidate):
        fail("criticalReview", "REVIEW_BINDING_CHANGED")
    allowed = {x["targetId"]: x for x in targets}
    result, seen = copy.deepcopy(candidate), set()
    for index, correction in enumerate(corrections):
        path = f"corrections[{index}]"
        if not isinstance(correction, dict) or set(correction) != {"targetId", "reason", "value", "evidenceRefs"}:
            fail(path, "REVIEW_CORRECTION_INVALID")
        identity = correction["targetId"]
        if not isinstance(identity, str) or identity not in allowed or identity in seen:
            fail(path + ".targetId", "REVIEW_TARGET_NOT_ALLOWED")
        if not isinstance(correction["reason"], str) or not correction["reason"].strip():
            fail(path + ".reason", "REVIEW_REASON_MISSING")
        refs = correction["evidenceRefs"]
        if (not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or ref not in {"user:" + key for key in sources} for ref in refs)):
            fail(path + ".evidenceRefs", "REVIEW_EVIDENCE_INVALID")
        value = correction["value"]
        target = allowed[identity]
        current = target["currentValue"]
        if (target["mode"] == "append" and (not isinstance(value, list) or not value)
                or target["mode"] == "replace" and current is not None and type(value) is not type(current)):
            fail(path + ".value", "REVIEW_VALUE_INVALID")
        if value == current:
            fail(path + ".value", "REVIEW_NO_CHANGE")
        items = value if target["mode"] == "append" else [value]
        if (
            len(target["path"]) >= 2
            and target["path"][0] == "proposalUnderstanding"
            and target["path"][1] in {"goals", "preserve"}
        ):
            if target["mode"] == "append" or len(target["path"]) == 3:
                for item in items:
                    if not isinstance(item, dict) or "component" in item or "entities" in item:
                        fail(path + ".value", "REVIEW_VALUE_INVALID")
            elif len(target["path"]) >= 4 and target["path"][3] in {"component", "entities"}:
                fail(path + ".value", "REVIEW_VALUE_INVALID")
        for item in items:
            if isinstance(item, dict) and "sourceTurnId" in item and "user:" + str(item["sourceTurnId"]) not in refs:
                fail(path + ".evidenceRefs", "REVIEW_EVIDENCE_INVALID")
        parent = result
        for part in target["path"][:-1]:
            parent = parent[part]
        leaf = target["path"][-1]
        if (target["mode"] == "replace" and len(target["path"]) == 3
                and target["path"][0] == "proposalUnderstanding"
                and target["path"][1] in {"goals", "editScope", "focus", "preserve"}):
            original = candidate["proposalUnderstanding"][target["path"][1]]
            if any(value == other for position, other in enumerate(original) if position != leaf):
                fail(path + ".value", "REVIEW_CROSS_ITEM_COPY")
        if target["mode"] == "append":
            parent[leaf].extend(copy.deepcopy(value))
        else:
            parent[leaf] = copy.deepcopy(value)
        seen.add(identity)
    for key in ("goals", "editScope", "focus", "preserve"):
        original = (candidate.get("proposalUnderstanding") or {}).get(key, [])
        final = (result.get("proposalUnderstanding") or {}).get(key, [])
        if any(final.count(item) > max(1, original.count(item)) for item in final):
            fail("proposalUnderstanding." + key, "REVIEW_DUPLICATE_ITEM")
    return result


class QuoteRepairRequest(PatchRequest):
    """Repair quote leaves only; compare the entire remainder after merging."""
    def merge(self, payload, current_evidence=None, current_value=None):
        result = super().merge(payload, current_evidence, current_value)
        masked = copy.deepcopy(result)
        for target in self.targets.values():
            old, new = self.value, masked
            for part in target.path[:-1]:
                old, new = old[part], new[part]
            new[target.path[-1]] = copy.deepcopy(old[target.path[-1]])
        if signature(masked) != signature(self.value):
            raise PatchError("REVIEW_LOCKED_FIELDS_CHANGED")
        return result


def review_patch_request(verdict, error, targets, sources, review_source):
    """Existing second slot, exact quote leaves; never reopen reviewed meaning."""
    if not isinstance(verdict, dict) or not verdict or set(verdict) - set(REVIEW_FIELDS):
        return None
    evidence = {"review_source": review_source, **{"user:" + key: value for key, value in sources.items()}}
    # Preserve the existing missing-APPROVE-metadata repair: no semantic delta
    # exists, and both fields must be empty. A partial CORRECT cannot be locked.
    if verdict.get("decision") == "APPROVE":
        if any(value != [] for key, value in verdict.items() if key != "decision"):
            return None
        missing = [Target(key, (key,), (list,)) for key in ("issues", "corrections") if key not in verdict]
        return PatchRequest("understanding_review", verdict, missing, evidence) if missing else None
    candidate = review_source.get("candidate")
    if not isinstance(candidate, dict):
        return None
    try:
        merge_review(verdict, candidate, targets, sources)
    except ProposalValidationError:
        return None
    issues = getattr(error, "issues", [])
    if not issues or getattr(error, "issues_truncated", False):
        return None
    allowed = {x["targetId"]: x for x in targets}
    repair = {}
    for issue in issues:
        match = re.fullmatch(r"proposalUnderstanding\.(goals|editScope|focus|preserve)\[([0-9]+)\]\.evidenceSpan", issue["fieldPath"])
        if not match or issue["reasonCode"] not in {"EVIDENCE_SPAN_EMPTY", "EVIDENCE_SPAN_NOT_EXACT"}:
            return None
        collection, position = match[1], int(match[2])
        original = candidate["proposalUnderstanding"][collection]
        matches = []
        for index, correction in enumerate(verdict["corrections"]):
            target = allowed[correction["targetId"]]
            path = ("corrections", index, "value")
            if target["mode"] == "replace" and target["path"] == ["proposalUnderstanding", collection, position]:
                item = correction["value"]
            elif target["mode"] == "append" and target["path"] == ["proposalUnderstanding", collection]:
                offset = position - len(original)
                if not isinstance(correction["value"], list) or not 0 <= offset < len(correction["value"]):
                    continue
                item = correction["value"][offset]
                path += (offset,)
            else:
                continue
            fields = {"statement", "sourceTurnId", "evidenceSpan"}
            fields |= {"component"} if collection == "editScope" else {"entities"} if collection == "focus" else set()
            if (not isinstance(item, dict) or set(item) != fields
                    or not isinstance(item["statement"], str) or not item["statement"].strip()
                    or not isinstance(item["sourceTurnId"], str) or item["sourceTurnId"] not in sources
                    or not isinstance(sources[item["sourceTurnId"]], str) or not sources[item["sourceTurnId"]].strip()
                    or not isinstance(item["evidenceSpan"], str)):
                return None
            path += ("evidenceSpan",)
            matches.append(Target(".".join(str(p) if not isinstance(p, int) else f"[{p}]" for p in path).replace(".[", "["), path, (str,)))
        if len(matches) != 1:
            return None
        repair[matches[0].field] = matches[0]
    return QuoteRepairRequest("understanding_review", verdict, list(repair.values()), evidence)
