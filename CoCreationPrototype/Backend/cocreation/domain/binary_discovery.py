"""Server-owned binary choices and hypothetical, deadline-bound feasibility proofs."""
import copy
import hashlib
import json
import re
import time

from cocreation.domain.design_requirements import requirement_response_schema
from cocreation.domain.level_validation import WALKABLE_TILES, validate_and_solve
from cocreation.domain.proposal_search import RevisionPlan, _map_fingerprint, parse_revision_plan, search_revision_plan, PRESERVE_COMPONENTS

POLICY_VERSION = 2
PROMISE_VERSION = 1
EDITABLE_OPERATORS = {"add_wall", "remove_wall", "add_water", "remove_water"}
KINDS = {"reduce_obstacles", "widen_corridor", "shorter_solution", "reshape_water"}
ACTIVE = {"clarifying", "planning", "retry_pending", "revision_needed"}


class BinaryError(ValueError):
    def __init__(self, code, field="branches", retryable=True, detail=None):
        self.code, self.field, self.retryable, self.detail = code, field, retryable, detail
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
    props = {
        "action": {"type": "string", "enum": ["ask", "plan", "protected_refusal", "blocked"]},
        "question": {"type": "string"},
        "note": {"type": "string"},
        "branches": {"type": "array", "maxItems": 2, "items": branch},
        "planStrategies": {"type": "array", "maxItems": 3, "items": strategy},
        "protectedEntities": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "enum": ["player", "boxes", "targets", "outer_shell"]},
        },
        "initialRequirements": {"anyOf": [requirement_response_schema(), {"type": "null"}]},
    }
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


PROTECTED_ENTITY_KEYS = ("player", "boxes", "targets", "outer_shell")


def parse_step(value, question_count):
    core_keys = {"action", "question", "note", "branches", "initialRequirements"}
    allowed_keys = core_keys | {"planStrategies", "protectedEntities"}
    if not isinstance(value, dict) or not core_keys.issubset(value) or set(value) - allowed_keys:
        raise BinaryError("BINARY_ENVELOPE_INVALID", "envelope")
    value.setdefault("planStrategies", [])
    value.setdefault("protectedEntities", [])
    if (
        value["action"] not in {"ask", "plan", "protected_refusal", "blocked"}
        or not isinstance(value["branches"], list)
        or not isinstance(value["planStrategies"], list)
        or not isinstance(value["protectedEntities"], list)
    ):
        raise BinaryError("BINARY_ACTION_INVALID", "action")
    for field in ("question", "note"):
        if not isinstance(value[field], str) or len(value[field]) > 1200:
            raise BinaryError("BINARY_TEXT_INVALID", field)
        value[field] = " ".join(value[field].split())
    if value["action"] == "protected_refusal":
        if value["question"] or value["branches"] or value["planStrategies"]:
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        entities = value["protectedEntities"]
        if (
            not entities
            or len(entities) > 4
            or any(not isinstance(item, str) or item not in PROTECTED_ENTITY_KEYS for item in entities)
        ):
            raise BinaryError("BINARY_PROTECTED_ENTITIES_REQUIRED", "protectedEntities")
        value["protectedEntities"] = list(dict.fromkeys(entities))
        return value
    if value["action"] == "plan":
        if value["question"] or value["branches"] or value["protectedEntities"]:
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        if len(value["planStrategies"]) > 3:
            raise BinaryError("BINARY_PLAN_STRATEGY_INVALID", "planStrategies")
        for item in value["planStrategies"]:
            try:
                plan = parse_revision_plan({"strategies": [item]})
                if set(plan.strategies[0].operators) - EDITABLE_OPERATORS or plan.strategies[0].metric_goals:
                    raise BinaryError("BINARY_PLAN_STRATEGY_INVALID", "planStrategies")
            except (ValueError, TypeError, KeyError) as error:
                raise BinaryError("BINARY_PLAN_STRATEGY_INVALID", "planStrategies") from error
        return value
    if value["action"] == "blocked":
        if value["question"] or value["branches"] or value["planStrategies"] or value["protectedEntities"]:
            raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
        if not value["note"]:
            raise BinaryError("BINARY_TEXT_INVALID", "note")
        return value
    if value["planStrategies"] or value["protectedEntities"]:
        raise BinaryError("BINARY_UNUSED_FIELDS", "envelope")
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


def _validate_corridor_baseline(rows, promise, branch_field):
    if promise.get("kind") != "widen_corridor":
        return
    c = promise["corridor"]
    row, col, axis = c["row"], c["column"], c["axis"]
    tile = rows[row - 1][col - 1]
    if tile not in WALKABLE_TILES:
        raise BinaryError("BINARY_CORRIDOR_INVALID", branch_field) from ValueError(
            f"corridor ({row},{col}) is {tile!r}, not a walkable floor cell; pick a walkable cell inside the corridor or use reduce_obstacles."
        )
    before = _span(rows, row, col, axis)
    other_axis = "horizontal" if axis == "vertical" else "vertical"
    along = _span(rows, row, col, other_axis)
    if not (before > 0 and along >= 3 and before < along):
        if before >= 3 and along < before:
            message = (
                f"corridor ({row},{col}) axis={axis!r} measures length {before} instead of narrow width {along}; "
                f"use axis={other_axis!r} across the width, or use reduce_obstacles."
            )
        else:
            message = (
                f"corridor ({row},{col}) is not a narrow corridor (span({axis})={before}, perpendicular={along}); "
                "pick a narrow walkable corridor cell or use reduce_obstacles."
            )
        raise BinaryError("BINARY_CORRIDOR_INVALID", branch_field) from ValueError(message)


def _validate_cumulative_strategy(rows, branch, prior_promises, branch_field):
    strategy = branch["strategy"]
    operators = set(strategy["operators"])
    preserve = set(strategy["preserve"])
    strategy_focus = strategy["focus"]
    for promise in [*prior_promises, branch["promise"]]:
        kind = promise["kind"]
        component = promise["component"]
        promise_focus = promise["focus"]
        if kind == "reduce_obstacles":
            if component == "water" and ("remove_water" not in operators or "water" in preserve):
                raise BinaryError(
                    "BINARY_CUMULATIVE_STRATEGY_INVALID",
                    branch_field,
                    detail=f"{branch_field}: water reduction requires operator 'remove_water' and must not preserve 'water'.",
                )
            if component == "internal_walls" and ("remove_wall" not in operators or "walls" in preserve):
                raise BinaryError(
                    "BINARY_CUMULATIVE_STRATEGY_INVALID",
                    branch_field,
                    detail=f"{branch_field}: internal_walls reduction requires operator 'remove_wall' and must not preserve 'walls'.",
                )
            if component == "obstacles" and not (
                ("remove_water" in operators and "water" not in preserve)
                or ("remove_wall" in operators and "walls" not in preserve)
            ):
                raise BinaryError(
                    "BINARY_CUMULATIVE_STRATEGY_INVALID",
                    branch_field,
                    detail=f"{branch_field}: obstacle reduction requires an unpreserved remove_water or remove_wall operator.",
                )
        elif kind == "reshape_water" and (not {"add_water", "remove_water"}.issubset(operators) or "water" in preserve):
            raise BinaryError(
                "BINARY_CUMULATIVE_STRATEGY_INVALID",
                branch_field,
                detail=f"{branch_field}: reshape_water requires both add_water and remove_water without preserving 'water'.",
            )
        if strategy_focus is not None and kind == "reduce_obstacles":
            tiles = {"@"} if component == "water" else {"#"} if component == "internal_walls" else {"#", "@"}
            strategy_cells = {(y, x) for y, x, tile in _cells(rows, strategy_focus) if tile in tiles}
            promise_cells = {(y, x) for y, x, tile in _cells(rows, promise_focus) if tile in tiles}
            if not (strategy_cells & promise_cells):
                raise BinaryError(
                    "BINARY_CUMULATIVE_STRATEGY_INVALID",
                    branch_field,
                    detail=(
                        f"{branch_field}: strategy.focus does not reach {component} tiles required by "
                        "prior/current promise; use strategy.focus=null when combining multiple areas."
                    ),
                )


def preflight(step, topic, rows, snapshot, validator, deadline, strategy_validator=None, budget_seconds=20.0, clock=None):
    """At most twenty seconds total, fair per-branch shares, original base/edit budget."""
    if step["action"] != "ask":
        return []
    monotonic = clock.monotonic if clock is not None else time.monotonic
    preflight_deadline = min(deadline - 20.0, monotonic() + max(0, budget_seconds))
    if preflight_deadline <= monotonic():
        raise BinaryError("BINARY_PREFLIGHT_BUDGET")
    solve_kwargs = {"deadline": preflight_deadline}
    if clock is not None:
        solve_kwargs["clock"] = clock
    baseline = validate_and_solve(rows, **solve_kwargs)
    prior = [item["promise"] for item in topic.get("choices", [])]
    static_errors = []
    for index, branch in enumerate(step["branches"]):
        branch_field = "branch-" + str(index + 1)
        try:
            _validate_corridor_baseline(rows, branch["promise"], branch_field)
            _validate_cumulative_strategy(rows, branch, prior, branch_field)
        except BinaryError as error:
            static_errors.append(error)
    if len(static_errors) == 1:
        raise static_errors[0]
    if len(static_errors) > 1:
        code = (
            "BINARY_CUMULATIVE_STRATEGY_INVALID"
            if any(err.code == "BINARY_CUMULATIVE_STRATEGY_INVALID" for err in static_errors)
            else static_errors[0].code
        )
        details = "; ".join(
            part
            for err in static_errors
            for part in (err.detail, str(err.__cause__) if err.__cause__ else "")
            if part
        )[:450]
        raise BinaryError(code, "branches", detail=details or None)
    caches = []
    for index, branch in enumerate(step["branches"]):
        branch_field = "branch-" + str(index + 1)
        now = monotonic()
        branch_deadline = now + max(0.0, preflight_deadline - now) / (2 - index)
        promises = [*prior, branch["promise"]]
        evidence = {}
        def validate(candidate):
            if monotonic() >= branch_deadline:
                raise TimeoutError()
            result = validator(candidate, branch_deadline)
            if strategy_validator is not None:
                strategy_validator(candidate, branch["strategy"])
            evidence[signature(candidate)] = check_promises(rows, candidate, promises, baseline, result)
            return result
        plan = parse_revision_plan({"strategies": [branch["strategy"]]})
        used_fingerprints = {_map_fingerprint(item["rows"]) for item in caches}
        used_signatures = {signature(item["rows"]) for item in caches}
        cached = topic.get("selectedCandidate")
        result = None
        if cached and cached.get("topicBinding") == topic["binding"] and signature(cached["rows"]) not in used_signatures:
            try:
                old_validation = validate(cached["rows"])
                result = (cached["rows"], old_validation)
            except (ValueError, TimeoutError):
                pass
        if result is None:
            search_kwargs = {"baseline_metrics": baseline.as_dict(), "deadline": branch_deadline,
                "entity_bindings": snapshot.get("entityBindings"), "maximum_valid_candidates": 1}
            if clock is not None:
                search_kwargs["clock"] = clock
            if used_fingerprints:
                search_kwargs["excluded_map_fingerprints"] = used_fingerprints
            try:
                found = search_revision_plan(rows, plan, validate, **search_kwargs)
            except ValueError as error:
                code = "BINARY_PREFLIGHT_BUDGET" if monotonic() >= branch_deadline else "BINARY_PREFLIGHT_NOT_FOUND"
                diagnostics = getattr(error, "diagnostics", None) or {}
                reasons = diagnostics.get("failureReasons") or {}
                safe_reasons = [
                    f"{key}={reasons[key]}"
                    for key in sorted(reasons)
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key)) and isinstance(reasons[key], int)
                ][:5]
                if safe_reasons:
                    detail = "search failure reasons: " + ", ".join(safe_reasons)
                elif diagnostics.get("constructedCandidates") == 0:
                    detail = (
                        "zero constructible editable candidates inside focus "
                        "(walls connected to outer border belong to fixed outer_shell; "
                        "use editableInternalWallCells or focus=null)"
                    )
                else:
                    detail = None
                raise BinaryError(code, branch_field, detail=detail) from error
            result = (list(found.rows), found.validation)
        candidate, validation = result
        caches.append({"rows": list(candidate), "strategy": branch["strategy"], "promise": branch["promise"],
            "preflightMs": int((monotonic() - now) * 1000),
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


def current_topic(database, session_id, version_id, clock=None):
    row = database.execute("SELECT payload_json FROM audit_events WHERE session_id=? AND event_type='binary_discovery_state' "
        "AND json_extract(payload_json,'$.versionId')=? ORDER BY id DESC LIMIT 1", (session_id, version_id)).fetchone()
    topic = json.loads(row["payload_json"]) if row else None
    now_ts = clock.utc_datetime().timestamp() if clock is not None else time.time()
    if topic and topic["status"] == "planning" and topic.get("leaseUntil", 0) < now_ts:
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
        items = [r for r in record["requirements"] if r["sourceTurnId"] == choice["sourceTurnId"] or choice["sourceTurnId"] in (r.get("sourceTurnIds") or ())]
        if not items or any(r["sourceTurnId"] == choice["sourceTurnId"] and r.get("value") is not None for r in items):
            raise ValueError("BINARY_CHOICE_COVERAGE: preserve the visible selected action without hidden numeric locks.")
        kind = choice["promise"]["kind"]
        if kind == "reduce_obstacles" and not any(r["property"] == "count" and r["relation"] == "decrease"
                and r["component"] in {"wall", "water"} for r in items):
            raise ValueError("BINARY_CHOICE_COVERAGE: selected obstacle reduction was omitted or changed.")
        if kind == "shorter_solution" and not any(r["property"] == "solutionSteps" and r["relation"] == "decrease" for r in items):
            raise ValueError("BINARY_CHOICE_COVERAGE: selected shorter solution was omitted or changed.")
        if any(t.get("sourceTurnId") == choice["sourceTurnId"] for t in record.get("exactTransitions", [])):
            raise ValueError("BINARY_CHOICE_COVERAGE: an interface choice did not specify exact tile transitions.")


_PROTECTED_TEXT_ZH = re.compile(
    r"(?:移动|挪动|移走|删掉|删除|移除|新增|添加|增加|拆掉|打破|修改)(?:.{0,4})(?:玩家|起点|出生点|箱子|目标点?|外墙|外壳)"
    r"(?![的之]?(?:绕路|难度|步数|推|拉|节奏|交互|配合|切换|顺序|选择|路径|路线|思考|压力|成本|体验|操作|方向))"
    r"(?!\s*(?:到目标|往目标|去目标))"
)
_PROTECTED_TEXT_ZH_BA = re.compile(
    r"把(?:.{0,4})(?:玩家|起点|出生点|箱子|目标点?|外墙|外壳)"
    r"(?![的之]?(?:推进|推拉|顺序|路线|路径|节奏|方式|策略))"
    r"(?:.{0,8})(?:移动|挪动|移走|删掉|删除|移除|拆掉|打破|改|换)"
)
_PROTECTED_TEXT_EN = re.compile(
    r"\b(?:move|relocate|shift|remove|delete|add)\s+(?:the\s+)?(?:player|start|box(?:es)?|target(?:s)?|outer\s+(?:wall|shell))\b",
    re.IGNORECASE,
)
_NEGATION_PREFIX = re.compile(
    r"(?:不要|别|不|勿|保留|保持|不动|不改|keep|preserve|without|never|cannot|can't|won't|do\s+not|don't)\s*$",
    re.IGNORECASE,
)


def _has_protected_edit_text(text):
    source = str(text or "")
    for pattern in (_PROTECTED_TEXT_ZH, _PROTECTED_TEXT_ZH_BA, _PROTECTED_TEXT_EN):
        for match in pattern.finditer(source):
            prefix = source[max(0, match.start() - 16) : match.start()]
            if _NEGATION_PREFIX.search(prefix):
                continue
            return True
    return False


def validate_binary_step_deterministic(step, topic, language):
    """Enforce hard permission & component/operator contracts deterministically instead of a sync LLM review."""
    if step.get("action") != "ask":
        return
    texts = [("question", step.get("question", "")), ("note", step.get("note", ""))]
    for index, branch in enumerate(step.get("branches") or [], start=1):
        texts.append((f"branch-{index}", branch.get("label", "")))
    for field, text in texts:
        if _has_protected_edit_text(text):
            raise BinaryError("BINARY_PROTECTED_TEXT", field)
    for index, branch in enumerate(step.get("branches") or [], start=1):
        component = branch["promise"]["component"]
        operators = set(branch["strategy"]["operators"])
        if component == "water" and not (operators & {"add_water", "remove_water"}):
            raise BinaryError("BINARY_BRANCH_COMPONENT_MISMATCH", f"branch-{index}")
        if component == "internal_walls" and not (operators & {"add_wall", "remove_wall"}):
            raise BinaryError("BINARY_BRANCH_COMPONENT_MISMATCH", f"branch-{index}")
        if component == "obstacles" and not (operators & EDITABLE_OPERATORS):
            raise BinaryError("BINARY_BRANCH_COMPONENT_MISMATCH", f"branch-{index}")


def compile_choice_requirements_deterministic(choices, user_turns, language="zh-CN"):
    """Deterministically compile signed button choices into validated typed requirements."""
    sources = {str(item["id"]): str(item["content"]) for item in user_turns or []}
    compiled = []
    for choice in choices or []:
        turn_id = choice["sourceTurnId"]
        span = sources.get(turn_id) or str(choice.get("label") or "")
        promise = choice["promise"]
        kind = promise["kind"]
        comp = promise["component"]
        if kind in {"reduce_obstacles", "widen_corridor"}:
            req_component = "water" if comp == "water" else "wall"
            req_property, req_relation = "count", "decrease"
        elif kind == "shorter_solution":
            req_component, req_property, req_relation = "gameplay", "solutionSteps", "decrease"
        else:
            req_component, req_property, req_relation = "water", "positions", "relocate"
        compiled.append(
            {
                "component": req_component,
                "property": req_property,
                "relation": req_relation,
                "strength": "goal",
                "entities": [],
                "value": None,
                "unit": "none",
                "priorityEvidenceSpan": "",
                "focused": True,
                "sourceTurnId": turn_id,
                "evidenceSpan": span,
                "statement": str(choice.get("label") or span).strip()[:400],
                "scope": "all",
            }
        )
    return compiled


def protected_refusal_message(protected_entities, initial_request, language):
    """Build a participant-facing permission explanation for protected entities without leaking internal rules."""
    names = {
        "targets": ("目标点", "targets"),
        "boxes": ("箱子", "boxes"),
        "player": ("玩家起点", "player start"),
        "outer_shell": ("外墙轮廓", "outer shell"),
    }
    chinese = language == "zh-CN"
    idx = 0 if chinese else 1
    labels = list(dict.fromkeys(names[key][idx] for key in protected_entities or [] if key in names))
    entity_text = ("、".join(labels) if chinese else ", ".join(labels)) or ("受保护对象" if chinese else "protected elements")
    snippet = " ".join(str(initial_request or "").split())[:120]
    if chinese:
        prefix = f"你提到的“{snippet}”涉及修改{entity_text}，" if snippet else f"这项请求涉及修改{entity_text}，"
        return f"{prefix}超出了当前 AI 修改权限。目前只能调整水域和内部墙体；玩家、箱子、目标点和连接外壳都会保持不变，已保存的 Stage 与此前讨论也会保留。"
    prefix = f'Your request “{snippet}” asks to change {entity_text}, which is ' if snippet else f"This request asks to change {entity_text}, which is "
    return f"{prefix}beyond the current editing permissions. Only water and internal walls may be edited; player, boxes, targets, and the connected outer shell remain fixed, and the saved Stage and prior discussion are preserved."
