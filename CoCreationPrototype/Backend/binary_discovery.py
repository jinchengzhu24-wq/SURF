"""Server-owned binary choices and hypothetical, deadline-bound feasibility proofs."""
import copy
import hashlib
import json
import time

from level_validation import WALKABLE_TILES, validate_and_solve
from proposal_search import RevisionPlan, parse_revision_plan, search_revision_plan, PRESERVE_COMPONENTS

POLICY_VERSION = 2
PROMISE_VERSION = 1
EDITABLE_OPERATORS = {"add_wall", "remove_wall", "add_water", "remove_water"}
KINDS = {"reduce_obstacles", "widen_corridor", "shorter_solution", "reshape_water"}
ACTIVE = {"clarifying", "planning", "retry_pending", "revision_needed"}


class BinaryError(ValueError):
    def __init__(self, code, field="branches", retryable=True):
        self.code, self.field, self.retryable = code, field, retryable
        super().__init__(code)


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def binding(snapshot, initial, design_context):
    return signature({"versionId": snapshot.get("versionId"), "map": snapshot.get("mapFingerprint"),
        "entities": snapshot.get("entityBindingFingerprint"), "initial": initial, "context": design_context,
        "policy": POLICY_VERSION, "promisePolicy": PROMISE_VERSION})


def path_signature(choices):
    return signature([{key: item[key] for key in ("questionId", "choiceId", "promise")} for item in choices])


def response_format():
    from design_requirements import requirement_response_schema
    focus = {"type": ["object", "null"], "additionalProperties": False,
        "properties": {"row": {"type": "integer"}, "column": {"type": "integer"}, "radius": {"type": "integer"}},
        "required": ["row", "column", "radius"]}
    strategy = {"type": "object", "additionalProperties": False, "properties": {
        "effect": {"type": "string", "enum": ["open_route", "narrow_route", "adjust_internal_walls", "reshape_water"]},
        "focus": focus, "operators": {"type": "array", "items": {"type": "string", "enum": sorted(EDITABLE_OPERATORS)}},
        "preserve": {"type": "array", "items": {"type": "string", "enum": sorted(PRESERVE_COMPONENTS)}}, "editBudget": {"type": "integer", "minimum": 1, "maximum": 12},
        "metricGoals": {"type": "array", "maxItems": 0, "items": {"type": "object"}}},
        "required": ["effect", "focus", "operators", "preserve", "editBudget", "metricGoals"]}
    promise = {"type": "object", "additionalProperties": False, "properties": {
        "kind": {"type": "string", "enum": sorted(KINDS)},
        "component": {"type": "string", "enum": ["water", "internal_walls", "obstacles"]},
        "focus": focus,
        "corridor": {"type": ["object", "null"], "additionalProperties": False,
            "properties": {"row": {"type": "integer"}, "column": {"type": "integer"},
                "axis": {"type": "string", "enum": ["horizontal", "vertical"]}}, "required": ["row", "column", "axis"]}},
        "required": ["kind", "component", "focus", "corridor"]}
    branch = {"type": "object", "additionalProperties": False, "properties": {
        "label": {"type": "string"}, "strategy": strategy, "promise": promise},
        "required": ["label", "strategy", "promise"]}
    props = {"action": {"type": "string", "enum": ["ask", "plan", "blocked"]}, "question": {"type": "string"},
        "note": {"type": "string"}, "branches": {"type": "array", "maxItems": 2, "items": branch},
        "initialRequirements": {"anyOf": [requirement_response_schema(), {"type": "null"}]}}
    return {"type": "json_schema", "json_schema": {"name": "cocreation_binary_discovery", "strict": True,
        "schema": {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}}}


def review_format():
    props = {"accepted": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}}}
    return {"type": "json_schema", "json_schema": {"name": "cocreation_binary_review", "strict": True,
        "schema": {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}}}


def _focus(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {"row", "column", "radius"}
            or any(type(v) is not int for v in value.values()) or not 1 <= value["row"] <= 10
            or not 1 <= value["column"] <= 12 or not 1 <= value["radius"] <= 3):
        raise BinaryError("BINARY_FOCUS_INVALID")


def validate_promise(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "component", "focus", "corridor"}
            or value.get("kind") not in KINDS or value.get("component") not in {"water", "internal_walls", "obstacles"}):
        raise BinaryError("BINARY_PROMISE_INVALID")
    _focus(value["focus"])
    corridor = value["corridor"]
    if value["kind"] == "widen_corridor":
        if (not isinstance(corridor, dict) or set(corridor) != {"row", "column", "axis"}
                or type(corridor["row"]) is not int or type(corridor["column"]) is not int
                or not 1 <= corridor["row"] <= 10 or not 1 <= corridor["column"] <= 12
                or corridor["axis"] not in {"horizontal", "vertical"}):
            raise BinaryError("BINARY_CORRIDOR_INVALID")
    elif corridor is not None:
        raise BinaryError("BINARY_CORRIDOR_UNEXPECTED")
    return copy.deepcopy(value)


def parse_step(value, question_count):
    if not isinstance(value, dict) or set(value) != {"action", "question", "note", "branches", "initialRequirements"}:
        raise BinaryError("BINARY_ENVELOPE_INVALID", "envelope")
    if value["action"] not in {"ask", "plan", "blocked"} or not isinstance(value["branches"], list):
        raise BinaryError("BINARY_ACTION_INVALID", "action")
    for field in ("question", "note"):
        if not isinstance(value[field], str) or len(value[field]) > 1200:
            raise BinaryError("BINARY_TEXT_INVALID", field)
        value[field] = " ".join(value[field].split())
    if value["action"] != "ask":
        if value["question"] or value["branches"]:
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        if value["action"] == "blocked" and not value["note"]:
            raise BinaryError("BINARY_TEXT_INVALID", "note")
        return value
    if question_count >= 3:
        raise BinaryError("BINARY_QUESTION_LIMIT", "action")
    if not value["question"] or len(value["branches"]) != 2:
        raise BinaryError("BINARY_TWO_BRANCHES_REQUIRED")
    for index, branch in enumerate(value["branches"]):
        try:
            if not isinstance(branch, dict) or set(branch) != {"label", "strategy", "promise"}:
                raise BinaryError("BINARY_BRANCH_INVALID")
            if not isinstance(branch["label"], str) or not branch["label"].strip() or len(branch["label"]) > 240:
                raise BinaryError("BINARY_LABEL_INVALID")
            branch["label"] = " ".join(branch["label"].split())
            plan = parse_revision_plan({"strategies": [branch["strategy"]]})
            if set(plan.strategies[0].operators) - EDITABLE_OPERATORS or plan.strategies[0].metric_goals:
                raise BinaryError("BINARY_PROTECTED_STRATEGY")
            branch["promise"] = validate_promise(branch["promise"])
        except (ValueError, TypeError, KeyError) as error:
            raise BinaryError(getattr(error, "code", "BINARY_STRATEGY_INVALID"), "branch-" + str(index + 1)) from error
    if (value["branches"][0]["label"] == value["branches"][1]["label"] or
            signature(value["branches"][0]["promise"]) == signature(value["branches"][1]["promise"])):
        raise BinaryError("BINARY_DUPLICATE_BRANCHES")
    return value


def _cells(rows, focus):
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if focus is None or max(abs(y + 1 - focus["row"]), abs(x + 1 - focus["column"])) <= focus["radius"]:
                yield y, x, tile


def _span(rows, row, col, axis):
    y, x = row - 1, col - 1
    if rows[y][x] not in WALKABLE_TILES:
        return 0
    dx, dy = (1, 0) if axis == "horizontal" else (0, 1)
    size = 1
    for sign in (-1, 1):
        a, b = x + sign * dx, y + sign * dy
        while 0 <= a < 12 and 0 <= b < 10 and rows[b][a] in WALKABLE_TILES:
            size += 1
            a, b = a + sign * dx, b + sign * dy
    return size


def check_promises(base, candidate, promises, baseline=None, validation=None):
    outcomes = []
    for promise in promises:
        validate_promise(promise)
        kind, focus = promise["kind"], promise["focus"]
        if kind == "reduce_obstacles":
            tiles = {"@"} if promise["component"] == "water" else {"#"} if promise["component"] == "internal_walls" else {"#", "@"}
            before = sum(tile in tiles for _, _, tile in _cells(base, focus))
            after = sum(tile in tiles for _, _, tile in _cells(candidate, focus))
            passed = after < before
        elif kind == "reshape_water":
            before = {(y, x) for y, x, tile in _cells(base, focus) if tile == "@"}
            after = {(y, x) for y, x, tile in _cells(candidate, focus) if tile == "@"}
            passed = before != after and len(before) == len(after)
            passed = passed and sum(row.count("@") for row in base) == sum(row.count("@") for row in candidate)
            before, after = len(before), len(after)
        elif kind == "widen_corridor":
            c = promise["corridor"]
            before = _span(base, c["row"], c["column"], c["axis"])
            after = _span(candidate, c["row"], c["column"], c["axis"])
            along = _span(base, c["row"], c["column"], "vertical" if c["axis"] == "horizontal" else "horizontal")
            passed = before > 0 and along >= 3 and before < along and after > before
        else:
            if baseline is None or validation is None:
                raise BinaryError("BINARY_METRIC_UNVERIFIED")
            before = baseline.get("solutionSteps") if isinstance(baseline, dict) else baseline.solution_steps
            after = validation.get("solutionSteps") if isinstance(validation, dict) else validation.solution_steps
            if type(before) is not int or type(after) is not int:
                raise BinaryError("BINARY_METRIC_UNVERIFIED")
            passed = after < before
        outcomes.append({"kind": kind, "passed": bool(passed), "before": before, "after": after})
    if any(not item["passed"] for item in outcomes):
        raise BinaryError("BINARY_PROMISE_NOT_REALIZED", retryable=False)
    return outcomes


def preflight(step, topic, rows, snapshot, validator, deadline, strategy_validator=None, budget_seconds=20.0):
    """At most twenty seconds total, fair per-branch shares, original base/edit budget."""
    if step["action"] != "ask":
        return []
    preflight_deadline = min(deadline - 20.0, time.monotonic() + max(0, budget_seconds))
    if preflight_deadline <= time.monotonic():
        raise BinaryError("BINARY_PREFLIGHT_BUDGET")
    baseline = validate_and_solve(rows, deadline=preflight_deadline)
    prior = [item["promise"] for item in topic.get("choices", [])]
    caches = []
    for index, branch in enumerate(step["branches"]):
        now = time.monotonic()
        branch_deadline = now + max(0.0, preflight_deadline - now) / (2 - index)
        promises = [*prior, branch["promise"]]
        evidence = {}
        def validate(candidate):
            if time.monotonic() >= branch_deadline:
                raise TimeoutError()
            result = validator(candidate, branch_deadline)
            if strategy_validator is not None:
                strategy_validator(candidate, branch["strategy"])
            evidence[signature(candidate)] = check_promises(rows, candidate, promises, baseline, result)
            return result
        plan = parse_revision_plan({"strategies": [branch["strategy"]]})
        cached = topic.get("selectedCandidate")
        result = None
        if cached and cached.get("topicBinding") == topic["binding"]:
            try:
                old_validation = validate(cached["rows"])
                result = (cached["rows"], old_validation)
            except (ValueError, TimeoutError):
                pass
        if result is None:
            try:
                found = search_revision_plan(rows, plan, validate, baseline_metrics=baseline.as_dict(),
                    deadline=branch_deadline, entity_bindings=snapshot.get("entityBindings"), maximum_valid_candidates=1)
            except ValueError as error:
                code = "BINARY_PREFLIGHT_BUDGET" if time.monotonic() >= branch_deadline else "BINARY_PREFLIGHT_NOT_FOUND"
                raise BinaryError(code, "branch-" + str(index + 1)) from error
            result = (list(found.rows), found.validation)
        candidate, validation = result
        caches.append({"rows": list(candidate), "strategy": branch["strategy"], "promise": branch["promise"],
            "preflightMs": int((time.monotonic() - now) * 1000),
            "validation": validation.as_dict(), "promiseResults": evidence[signature(candidate)],
            "topicBinding": topic["binding"], "prefix": path_signature(topic.get("choices", [])),
            "policyVersion": POLICY_VERSION, "promiseVersion": PROMISE_VERSION})
    if signature(caches[0]["rows"]) == signature(caches[1]["rows"]):
        raise BinaryError("BINARY_DUPLICATE_CANDIDATES")
    return caches


def public_question(topic):
    question = topic.get("pendingQuestion")
    if not question:
        return None
    selected = next((item["choiceId"] for item in topic.get("choices", []) if item["questionId"] == question["questionId"]), None)
    return {"questionId": question["questionId"], "topicId": topic["topicId"], "baseVersionId": topic["versionId"],
        "question": question["question"], "note": question["note"], "number": question["number"],
        "choices": [{"choiceId": item["choiceId"], "label": item["label"], "letter": item["letter"]} for item in question["choices"]],
        "selectedChoiceId": selected, "status": "selected" if selected else "pending" if topic["status"] == "clarifying" else "expired"}


def current_topic(database, session_id, version_id):
    row = database.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='binary_discovery_state' "
        "AND json_extract(payload_json,'$.versionId')=? ORDER BY id DESC LIMIT 1", (session_id, version_id)).fetchone()
    topic = json.loads(row["payload_json"]) if row else None
    if topic and topic["status"] == "planning" and topic.get("leaseUntil", 0) < time.time():
        topic["status"] = "retry_pending"
        topic["failureCode"] = "BINARY_REQUEST_INTERRUPTED"
    return topic


def public_flow(topic):
    return {"active": topic["status"] in ACTIVE, "status": topic["status"], "topicId": topic["topicId"],
        "interactionMode": "binary_choice", "clarificationQuestionCount": topic["questionCount"],
        "recordedUserTurnIds": [turn["id"] for turn in topic.get("userTurns", [])],
        "pendingQuestion": public_question(topic), "retryAction": topic.get("retryAction"),
        "retryable": topic["status"] == "retry_pending", "failureCategory": topic.get("failureCode"),
        "canSupplement": False, "recoverySuggestions": []}


def require_choice_coverage(record, choices):
    """Interface evidence must survive compilation; hidden coordinates never become invariants."""
    for choice in choices:
        items = [r for r in record["requirements"] if r["sourceTurnId"] == choice["sourceTurnId"]]
        if not items or any(r.get("value") is not None for r in items):
            raise ValueError("BINARY_CHOICE_COVERAGE: preserve the visible selected action without hidden numeric locks.")
        kind = choice["promise"]["kind"]
        if kind == "reduce_obstacles" and not any(r["property"] == "count" and r["relation"] == "decrease"
                and r["component"] in {"wall", "water"} for r in items):
            raise ValueError("BINARY_CHOICE_COVERAGE: selected obstacle reduction was omitted or changed.")
        if kind == "shorter_solution" and not any(r["property"] == "solutionSteps" and r["relation"] == "decrease" for r in items):
            raise ValueError("BINARY_CHOICE_COVERAGE: selected shorter solution was omitted or changed.")
        if any(t.get("sourceTurnId") == choice["sourceTurnId"] for t in record.get("exactTransitions", [])):
            raise ValueError("BINARY_CHOICE_COVERAGE: an interface choice did not specify exact tile transitions.")
