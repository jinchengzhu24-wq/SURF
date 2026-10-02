"""Evidence-bound requirements shared by planning, search and execution.

Natural language is interpreted by Kimi. This module validates that interpretation
and evaluates typed facts; it never infers a requirement from keyword proximity.
"""
import hashlib
import json


POLICY_VERSION = 1
COMPONENTS = {"water", "wall", "player", "box", "target", "gameplay"}
PROPERTIES = {
    "count", "positions", "distribution", "distance", "change_scope",
    "solutionSteps", "minimumPushes", "solutionPushes", "boxAlternations",
    "longestPushRun", "timing", "dependency", "experience", "appearance",
}
RELATIONS = {"increase", "decrease", "equal", "preserve", "relocate", "nearer", "farther", "early", "later", "seek", "avoid"}
TILES = {"water": {"@"}, "wall": {"#"}, "player": {"p", "+"}, "box": {"s", "*"}, "target": {"t", "+", "*"}}
METRICS = {"solutionSteps", "minimumPushes", "solutionPushes", "boxAlternations", "longestPushRun"}


def requirement_response_schema():
    properties = {
        "component": {"type": "string", "enum": sorted(COMPONENTS)},
        "property": {"type": "string", "enum": sorted(PROPERTIES)},
        "relation": {"type": "string", "enum": sorted(RELATIONS)},
        "strength": {"type": "string", "enum": ["invariant", "goal", "preference"]},
        "entities": {"type": "array", "items": {"type": "string"}},
        "value": {"type": ["integer", "null"]},
        "unit": {"type": "string", "enum": ["absolute", "delta", "none"]},
        "priorityEvidenceSpan": {"type": "string"},
        "focused": {"type": "boolean"},
        "sourceTurnId": {"type": "string"},
        "evidenceSpan": {"type": "string"},
        "statement": {"type": "string"},
    }
    return {"type": "object", "additionalProperties": False, "properties": {
        "requirements": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}},
        "automaticBindings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"entity": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["entity", "reason"]}},
        "exactTransitions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"row": {"type": "integer"}, "column": {"type": "integer"},
                "from": {"type": "string", "enum": [".", "#", "@"]},
                "to": {"type": "string", "enum": [".", "#", "@"]},
                "sourceTurnId": {"type": "string"}, "evidenceSpan": {"type": "string"}},
            "required": ["row", "column", "from", "to", "sourceTurnId", "evidenceSpan"]}},
    }, "required": ["requirements", "automaticBindings", "exactTransitions"]}


def validate_requirement_record(payload, user_turns, snapshot):
    if not isinstance(payload, dict) or set(payload) != {"requirements", "automaticBindings", "exactTransitions"}:
        raise ValueError("Requirements need an evidence-bound envelope.")
    sources = {str(item["id"]): str(item["content"]) for item in user_turns}
    labels = {str(item.get("id") or item.get("label") or "").upper() for item in (snapshot or {}).get("entities") or []}
    raw_requirements = payload["requirements"]
    if not isinstance(raw_requirements, list) or len(raw_requirements) > 48:
        raise ValueError("Invalid requirements collection.")
    requirements, seen = [], set()
    fields = set(requirement_response_schema()["properties"]["requirements"]["items"]["required"])
    for raw in raw_requirements:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("Invalid typed requirement.")
        item = dict(raw)
        source = sources.get(item["sourceTurnId"])
        span = item["evidenceSpan"]
        if source is None or not isinstance(span, str) or not span.strip() or span not in source:
            raise ValueError("Requirement lacks exact designer evidence.")
        if item["component"] not in COMPONENTS or item["property"] not in PROPERTIES or item["relation"] not in RELATIONS:
            raise ValueError("Unsupported requirement attribute.")
        if item["strength"] not in {"invariant", "goal", "preference"}:
            raise ValueError("Invalid requirement strength.")
        if not isinstance(item["entities"], list) or any(not isinstance(x, str) or x.upper() not in labels for x in item["entities"]):
            raise ValueError("Requirement references an unknown current entity.")
        item["entities"] = list(dict.fromkeys(x.upper() for x in item["entities"]))
        value = item["value"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("Requirement value must be an explicit nonnegative integer.")
        if item["unit"] not in {"absolute", "delta", "none"} or not isinstance(item["focused"], bool):
            raise ValueError("Invalid value unit or focus.")
        # Numeric authority must occur in the evidence, not in an entity label.
        if value is not None:
            import re
            without_labels = re.sub(r"(?<![A-Za-z0-9])(?:B\d+|T\d+|P)(?![A-Za-z0-9])", "", span, flags=re.I)
            chinese_numbers = {0: "零", 1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
            if not re.search(rf"(?<!\d){value}(?!\d)", without_labels) and chinese_numbers.get(value, "\0") not in without_labels and not (value == 2 and "二" in without_labels):
                raise ValueError("Numeric requirement is not supported by designer evidence.")
        priority_span = item["priorityEvidenceSpan"]
        if not isinstance(priority_span, str) or (priority_span and priority_span not in source):
            raise ValueError("Priority lacks exact designer evidence.")
        if not isinstance(item["statement"], str) or not item["statement"].strip():
            raise ValueError("Requirement needs a readable statement.")
        item["statement"] = item["statement"].strip()[:400]
        prop, relation = item["property"], item["relation"]
        # Directional wishes are negotiable even when clearly stated. Only
        # explicit locks/prohibitions can be immutable requirements.
        if item["strength"] == "invariant" and not (
            (prop in {"count", "positions", "distribution"} and relation in {"preserve", "equal"})
            or (prop == "change_scope" and value is not None)
        ):
            raise ValueError("A directional goal cannot become an immutable constraint.")
        if prop in {"count", "positions", "distribution", "distance"} and item["component"] == "gameplay":
            raise ValueError("Map attribute needs an actual map component.")
        if prop in METRICS and relation not in {"increase", "decrease", "equal", "preserve"}:
            raise ValueError("Metric direction is invalid.")
        if prop in {"count", "distribution", "change_scope"} and relation not in {"increase", "decrease", "equal", "preserve"}:
            raise ValueError("Numeric map attribute needs a numeric relation.")
        if prop == "distance" and relation not in {"nearer", "farther", "equal", "preserve"}:
            raise ValueError("Distance needs a distance relation.")
        identity = json.dumps({key: item[key] for key in ("component", "property", "relation", "entities", "value", "unit", "strength")}, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        item["requirementId"] = "req-" + hashlib.sha256((item["sourceTurnId"] + identity).encode()).hexdigest()[:20]
        requirements.append(item)
    bindings = payload["automaticBindings"]
    if not isinstance(bindings, list) or any(not isinstance(x, dict) or set(x) != {"entity", "reason"} or x["entity"].upper() not in labels or not isinstance(x["reason"], str) for x in bindings):
        raise ValueError("Automatic binding must reference the current snapshot.")
    # Bindings select only unbound requirements. They never rewrite entities
    # carried by a requirement, even if another goal has a different focus.
    if bindings and not any(not r["entities"] for r in requirements):
        raise ValueError("Automatic binding needs an unspecified goal.")
    transitions = payload["exactTransitions"]
    if not isinstance(transitions, list) or len(transitions) > 12:
        raise ValueError("Invalid exact transitions.")
    for transition in transitions:
        if not isinstance(transition, dict) or set(transition) != {"row", "column", "from", "to", "sourceTurnId", "evidenceSpan"}:
            raise ValueError("Invalid exact transition evidence.")
        span = transition["evidenceSpan"]
        if not isinstance(span, str) or not span or span not in sources.get(transition["sourceTurnId"], ""):
            raise ValueError("Exact transition lacks designer evidence.")
        import re
        r, c = transition["row"], transition["column"]
        if type(r) is not int or type(c) is not int or not re.search(rf"(?<!\d){r}(?!\d)", span) or not re.search(rf"(?<!\d){c}(?!\d)", span):
            raise ValueError("Exact coordinates lack designer evidence.")
        if transition["from"] not in {".", "#", "@"} or transition["to"] not in {".", "#", "@"} or transition["from"] == transition["to"]:
            raise ValueError("Exact transition outside editable tile types.")
    return {"policyVersion": POLICY_VERSION, "requirements": requirements,
        "exactTransitions": transitions,
        "automaticBindings": [{"entity": x["entity"].upper(), "reason": x["reason"][:300], "authority": "proposal_assumption"} for x in bindings]}


def positions(rows, component):
    return {(r + 1, c + 1) for r, row in enumerate(rows) for c, tile in enumerate(row) if tile in TILES.get(component, set())}


def evaluate_requirements(base_rows, candidate_rows, record, before_features=None, after_features=None):
    before_features, after_features = before_features or {}, after_features or {}
    results = []
    for item in (record or {}).get("requirements") or []:
        prop, relation = item["property"], item["relation"]
        entities = item["entities"] or [x["entity"] for x in (record or {}).get("automaticBindings") or []]
        before, after = positions(base_rows, item["component"]), positions(candidate_rows, item["component"])
        value_before = value_after = None
        if prop == "count":
            value_before, value_after = len(before), len(after)
        elif prop in METRICS:
            value_before, value_after = before_features.get(prop), after_features.get(prop)
        elif prop == "change_scope":
            value_before = 0
            value_after = sum(a != b for left, right in zip(base_rows, candidate_rows) for a, b in zip(left, right))
        elif prop == "positions":
            value_before, value_after = before, after
        elif prop == "distribution":
            def extent(cells):
                return sum(max(v) - min(v) for v in zip(*cells)) if cells else 0
            value_before, value_after = extent(before), extent(after)
        elif prop == "distance" and entities:
            # Entity positions remain fixed for new AI revisions; distance to
            # those anchors is therefore measurable without model coordinates.
            anchors = before_features.get("entityPositions") or {}
            point = anchors.get(entities[0])
            if point and before and after:
                value_before = min(abs(r - point[0]) + abs(c - point[1]) for r, c in before)
                value_after = min(abs(r - point[0]) + abs(c - point[1]) for r, c in after)
        elif prop == "timing" and entities:
            runs_before = before_features.get("firstRuns") or {}
            runs_after = after_features.get("firstRuns") or {}
            value_before, value_after = runs_before.get(entities[0]), runs_after.get(entities[0])
        status, passed, reason = "deferred", True, "not_verified"
        if value_before is not None and value_after is not None:
            if relation in {"preserve", "equal"}:
                expected = item["value"] if item["value"] is not None and item["unit"] == "absolute" else value_before
                passed = value_after == expected
                status, reason = ("fulfilled", "verified") if passed else ("deferred", "locked_requirement_changed")
                if item["strength"] == "goal" and relation == "equal" and isinstance(expected, (int, float)):
                    distance_before, distance_after = abs(value_before - expected), abs(value_after - expected)
                    passed = distance_after <= distance_before
                    status = "fulfilled" if distance_after == 0 else "partial" if distance_after < distance_before else "deferred"
                    reason = "verified" if status == "fulfilled" else "opposite_direction" if not passed else "unchanged" if distance_after == distance_before else "below_requested_amount"
            elif prop == "positions" and relation == "relocate":
                status = "fulfilled" if value_after != value_before else "deferred"
                reason = "verified" if status == "fulfilled" else "unchanged"
            elif isinstance(value_before, (int, float)) and isinstance(value_after, (int, float)):
                sign = -1 if relation in {"decrease", "nearer", "early"} else 1
                delta = sign * (value_after - value_before)
                passed = delta >= 0
                target = item["value"]
                if item["unit"] == "absolute" and target is not None:
                    satisfied = value_after >= target if sign == 1 else value_after <= target
                else:
                    satisfied = delta >= (target if target is not None else 1)
                status = "fulfilled" if passed and satisfied else "partial" if delta > 0 else "deferred"
                reason = "opposite_direction" if not passed else "verified" if satisfied else "unchanged" if delta == 0 else "below_requested_amount"
        elif prop in METRICS | {"distance", "timing"} or item["strength"] == "invariant":
            passed, reason = False, "required_fact_unavailable"
        if prop == "timing" and entities and "interruptions" in after_features:
            # A shorter completed transport is not an interruption. The route
            # must return to this box after pushing another box.
            interrupted_before = (before_features.get("interruptions") or {}).get(entities[0], False)
            interrupted_after = (after_features.get("interruptions") or {}).get(entities[0], False)
            if not interrupted_after:
                status, passed = "deferred", not interrupted_before
                reason = "interruption_removed" if interrupted_before else "interruption_not_verified"
        if item["strength"] == "invariant" and status != "fulfilled":
            passed = False
        elif item["strength"] == "preference":
            # Ambiguous/preference-only answers do not impose a directional gate.
            passed = True
        results.append({"requirementId": item["requirementId"], "statement": item["statement"],
            "strength": item["strength"], "property": prop, "status": status,
            "passed": bool(passed), "reason": reason,
            "before": sorted(value_before) if isinstance(value_before, set) else value_before,
            "after": sorted(value_after) if isinstance(value_after, set) else value_after,
            "prioritized": bool(item["priorityEvidenceSpan"]), "focused": item["focused"]})
    for ordinal, item in enumerate((record or {}).get("exactTransitions") or []):
        r, c = item["row"] - 1, item["column"] - 1
        valid = 0 <= r < len(base_rows) and 0 <= c < len(base_rows[r])
        passed = valid and base_rows[r][c] == item["from"] and candidate_rows[r][c] == item["to"]
        results.append({"requirementId": f"exact:{ordinal}", "statement": f"({r + 1}, {c + 1}): {item['from']} → {item['to']}",
            "strength": "invariant", "property": "exactTransition", "status": "fulfilled" if passed else "deferred",
            "passed": bool(passed), "reason": "verified" if passed else "exact_transition_changed",
            "before": item["from"], "after": item["to"], "prioritized": False, "focused": False})
    return results


def preflight_requirements(rows, record):
    """Reject actual fixed-rule conflicts before spending the search budget."""
    issues, locks, transitions = [], {}, {}
    for item in (record or {}).get("requirements") or []:
        key = (item["component"], item["property"], tuple(item["entities"]))
        if item["strength"] == "invariant":
            if key in locks and (locks[key]["value"], locks[key]["relation"]) != (item["value"], item["relation"]):
                issues.append({"requirementId": item["requirementId"], "reason": "conflicting_fixed_requirements"})
            locks[key] = item
        if item["component"] in {"player", "box", "target"} and item["property"] in {"count", "positions"}:
            before = len(positions(rows, item["component"]))
            change = item["relation"] not in {"preserve", "equal"} or (item["property"] == "count" and item["value"] is not None and item["value"] != before)
            if change:
                issues.append({"requirementId": item["requirementId"], "reason": "protected_entity_change"})
    for item in (record or {}).get("exactTransitions") or []:
        point = item["row"], item["column"]
        r, c = point[0] - 1, point[1] - 1
        if not (0 < r < len(rows) - 1 and 0 < c < len(rows[r]) - 1) or rows[r][c] != item["from"]:
            issues.append({"reason": "exact_transition_conflicts_with_snapshot", "point": point})
        if point in transitions and transitions[point] != item["to"]:
            issues.append({"reason": "conflicting_exact_transitions", "point": point})
        transitions[point] = item["to"]
        for component, tiles in TILES.items():
            if item["from"] in tiles or item["to"] in tiles:
                lock = locks.get((component, "positions", ()))
                if lock:
                    issues.append({"requirementId": lock["requirementId"], "reason": "exact_transition_conflicts_with_preservation"})
    return issues


def goal_rank(results):
    goals = [x for x in results if x["strength"] != "invariant"]
    return (sum(x["status"] == "fulfilled" and x["prioritized"] for x in goals),
        sum(x["status"] == "partial" and x["prioritized"] for x in goals),
        sum(x["status"] == "fulfilled" and x["focused"] for x in goals),
        sum(x["status"] == "partial" and x["focused"] for x in goals),
        sum(x["status"] == "fulfilled" for x in goals),
        sum(x["status"] == "partial" for x in goals))


def tradeoff_message(record, outcomes, language):
    if not (record or {}).get("requirements") and not (record or {}).get("automaticBindings") and not (record or {}).get("exactTransitions"):
        return ""
    def statements(status):
        return "；".join(x["statement"] for x in outcomes if x["strength"] != "invariant" and x["status"] == status)
    fulfilled, partial, deferred = (statements(s) for s in ("fulfilled", "partial", "deferred"))
    locked = "；".join(x["statement"] for x in outcomes if x["strength"] == "invariant" and x["passed"])
    binding_labels = ", ".join(x["entity"] for x in (record or {}).get("automaticBindings") or [])
    if language == "zh-CN":
        parts = [f"这版已核验的结果是：{fulfilled}。" if fulfilled else ""]
        if partial:
            parts.append(f"以下目标只实现了一部分：{partial}。")
        if deferred:
            parts.append(f"以下目标暂未核实实现：{deferred}。我先保留这份通过验证的候选；这些目标仍保留，接受这版不表示放弃它们。")
        if any(x["property"] == "boxAlternations" and x["status"] != "deferred" for x in outcomes):
            parts.append("切换次数来自已核对的一条求解路线，不能据此断言所有解法都必须这样切换。")
        if locked:
            parts.append(f"以下固定要求已经复验：{locked}。")
        if binding_labels:
            parts.append(f"你没有指定具体对象的部分，这版先围绕 {binding_labels} 尝试；这是本方案的选择。")
    else:
        parts = [f"Verified in this candidate: {fulfilled}." if fulfilled else ""]
        if partial:
            parts.append(f"Partly achieved: {partial}.")
        if deferred:
            parts.append(f"Not verified as achieved yet: {deferred}. I retained this validated candidate; accepting it does not withdraw those goals.")
        if any(x["property"] == "boxAlternations" and x["status"] != "deferred" for x in outcomes):
            parts.append("Alternation counts describe one verified solution, not a proof of mandatory switching in every solution.")
        if locked:
            parts.append(f"Rechecked fixed requirements: {locked}.")
        if binding_labels:
            parts.append(f"For the unspecified focus, this proposal uses {binding_labels} as a correctable proposal assumption.")
    return " ".join(x for x in parts if x)


def protected_change_requested(understanding, active_topic=False):
    changes = (understanding or {}).get("changes")
    protected = {"outer_shell", "player", "boxes", "targets"}
    if isinstance(changes, list):
        return any(x.get("component") in protected and x.get("property") in {"count", "position", "shape", "layout"}
            and x.get("operation", "change") == "change" for x in changes)
    # Legacy annotations mention subjects, not operations. During an existing
    # topic they must not reinterpret a short answer as a protected edit.
    return not active_topic and bool(set((understanding or {}).get("elements") or []) & protected)
