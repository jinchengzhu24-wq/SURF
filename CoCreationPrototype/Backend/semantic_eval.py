"""Isolated turn-understanding evaluations; no HTTP sessions or database writes.

validate/score/compare are offline. Only run --live calls the existing Kimi
entry point. Expected outcomes are never included in its model input.
"""
import argparse
import copy
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from level_validation import build_entity_bindings, build_stage_snapshot, validate_and_solve


DEFAULT_SUITE = Path(__file__).with_name("evaluations") / "turn_understanding_v1.json"
ACTS = {"evaluation", "intent", "explanation_request", "idea_request", "revision_request", "unclear"}
ELEMENTS = {"water", "internal_walls", "outer_shell", "player", "boxes", "targets", "unknown"}
INPUT_KEYS = {"stage", "conversation", "forcedProposal", "proposalContext"}
EXPECTATION_KEYS = {"actsAll", "actsAny", "actsForbidden", "elementsAll", "mapRelated", "directionSufficient", "changesAll", "forbidProtectedChange",
    "topicAspect", "editScopeAll", "editScopeForbidden", "focusEntitiesAll", "preserveEmpty"}


def suite_hash(suite):
    return hashlib.sha256(json.dumps(suite, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_suite(path=DEFAULT_SUITE):
    suite = json.loads(path.read_text(encoding="utf-8"))
    if suite.get("schemaVersion") != 1 or suite.get("target") != "turn_understanding":
        raise ValueError("Unsupported evaluation suite.")
    stages = suite.get("stages") or {}
    for stage_id, stage in stages.items():
        if not isinstance(stage, dict):
            raise ValueError(f"Stage {stage_id} must be structurally valid and solvable.")
        validate_and_solve(stage["rows"])
    identities = set()
    for case in suite.get("cases") or []:
        identity = case.get("id")
        if not identity or identity in identities:
            raise ValueError("Evaluation case IDs must be unique.")
        identities.add(identity)
        inputs = case.get("input") or {}
        if set(inputs) - INPUT_KEYS or inputs.get("stage") not in stages:
            raise ValueError(f"Invalid model input in {identity}.")
        conversation = inputs.get("conversation")
        if not isinstance(conversation, list) or not conversation or conversation[-1].get("role") != "user":
            raise ValueError(f"Case {identity} needs a latest user turn.")
        if any(set(turn) != {"role", "content"} or turn["role"] not in {"user", "assistant"}
               or not isinstance(turn["content"], str) or not turn["content"].strip() for turn in conversation):
            raise ValueError(f"Invalid conversation in {identity}.")
        if "forcedProposal" in inputs and not isinstance(inputs["forcedProposal"], bool):
            raise ValueError(f"Invalid proposal flag in {identity}.")
        expected = case.get("expected") or {}
        if not expected or set(expected) - EXPECTATION_KEYS:
            raise ValueError(f"Invalid expectations in {identity}.")
        for key in ("actsAll", "actsAny", "actsForbidden"):
            if not set(expected.get(key) or []).issubset(ACTS):
                raise ValueError(f"Invalid speech acts in {identity}.")
        if not set(expected.get("elementsAll") or []).issubset(ELEMENTS):
            raise ValueError(f"Invalid elements in {identity}.")
        for key in ("mapRelated", "directionSufficient", "forbidProtectedChange"):
            if key in expected and not isinstance(expected[key], bool):
                raise ValueError(f"Invalid boolean expectation in {identity}.")
        if "topicAspect" in expected and expected["topicAspect"] not in {"visual", "gameplay", "mixed", "unspecified"}:
            raise ValueError(f"Invalid topic aspect in {identity}.")
        for key in ("editScopeAll", "editScopeForbidden"):
            if not set(expected.get(key) or []).issubset(ELEMENTS | {"gameplay"}):
                raise ValueError(f"Invalid edit scope in {identity}.")
        if "preserveEmpty" in expected and type(expected["preserveEmpty"]) is not bool:
            raise ValueError(f"Invalid preservation expectation in {identity}.")
    if not identities:
        raise ValueError("An evaluation suite cannot be empty.")
    return suite


def build_model_input(suite, case):
    inputs = case["input"]
    stage = suite["stages"][inputs["stage"]]
    bindings = build_entity_bindings(stage["rows"])
    snapshot = build_stage_snapshot(stage["rows"], version_id=inputs["stage"],
        stage_number=stage["stageNumber"], entity_bindings=bindings)
    # Explicit allowlist: no rubric, expected result, case label or research data.
    return {"conversation": copy.deepcopy(inputs["conversation"]), "snapshot": snapshot,
        "forced_proposal": inputs.get("forcedProposal", False),
        "proposal_context": copy.deepcopy(inputs.get("proposalContext") or {})}


def check_result(case, result):
    from llm_client import _validate_turn_understanding
    failures = []
    try:
        # Do not let forced-button normalization silently repair a captured result.
        value = _validate_turn_understanding(copy.deepcopy(result), case["input"]["conversation"][-1]["content"], False)
    except (TypeError, KeyError, ValueError):
        return ["invalid_result_contract_or_user_evidence"]
    expected = case["expected"]
    acts = set(value["acts"])
    if not set(expected.get("actsAll") or []).issubset(acts):
        failures.append("missing_required_act")
    if expected.get("actsAny") and not acts.intersection(expected["actsAny"]):
        failures.append("missing_acceptable_act")
    if acts.intersection(expected.get("actsForbidden") or []):
        failures.append("unexpected_act")
    if not set(expected.get("elementsAll") or []).issubset(value["elements"]):
        failures.append("missing_required_element")
    for key in ("mapRelated", "directionSufficient"):
        if key in expected and value[key] != expected[key]:
            failures.append("wrong_" + key)
    changes = value.get("changes") or []
    for required in expected.get("changesAll") or []:
        if not any(all(change.get(key, "change" if key == "operation" else None) == val
                       for key, val in required.items()) for change in changes):
            failures.append("missing_required_change")
    if expected.get("forbidProtectedChange") and any(
        change.get("component") in {"outer_shell", "player", "boxes", "targets"}
        and change.get("operation", "change") == "change" for change in changes
    ):
        failures.append("invented_protected_change")
    topic = value.get("proposalUnderstanding") or {}
    if "topicAspect" in expected and topic.get("aspect") != expected["topicAspect"]:
        failures.append("wrong_topic_aspect")
    scope = {x.get("component") for x in topic.get("editScope") or [] if isinstance(x, dict)}
    if not set(expected.get("editScopeAll") or []).issubset(scope):
        failures.append("missing_requested_edit_scope")
    if scope.intersection(expected.get("editScopeForbidden") or []):
        failures.append("invented_edit_scope")
    focus = {entity for x in topic.get("focus") or [] if isinstance(x, dict) for entity in x.get("entities") or []}
    if not set(expected.get("focusEntitiesAll") or []).issubset(focus):
        failures.append("missing_spatial_focus")
    if "preserveEmpty" in expected and (not topic or bool(topic.get("preserve")) == expected["preserveEmpty"]):
        failures.append("wrong_preservation_evidence")
    return failures


def score(suite, captures):
    if captures.get("suiteHash") != suite_hash(suite):
        raise ValueError("Captured outputs belong to a different suite; do not compare changed answers or inputs.")
    repeats = captures.get("repeats", 1)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 20:
        raise ValueError("Invalid repeat count.")
    cases = {case["id"]: case for case in suite["cases"]}
    runs = {}
    for run in captures.get("runs") or []:
        key = (run.get("caseId"), run.get("repeat"))
        if key[0] not in cases or not isinstance(key[1], int) or isinstance(key[1], bool) or not 1 <= key[1] <= repeats or key in runs:
            raise ValueError("Unknown, duplicate or out-of-range captured run.")
        runs[key] = run
    outcomes = []
    reasons = Counter()
    for identity, case in cases.items():
        for repeat in range(1, repeats + 1):
            run = runs.get((identity, repeat))
            if run is None:
                failures = ["missing_capture"]
            elif run.get("errorCode"):
                failures = ["request_error:" + str(run["errorCode"])]
            else:
                failures = check_result(case, run.get("result"))
            reasons.update(failures)
            outcomes.append({"caseId": identity, "repeat": repeat, "passed": not failures, "failures": failures})
    per_case = {}
    for identity in cases:
        passed = sum(item["passed"] for item in outcomes if item["caseId"] == identity)
        per_case[identity] = {"passed": passed, "total": repeats, "stablePass": passed == repeats,
            "inconsistent": 0 < passed < repeats}
    total = len(outcomes)
    passed = sum(item["passed"] for item in outcomes)
    return {"suiteHash": suite_hash(suite), "target": "turn_understanding", "repeats": repeats,
        "model": captures.get("model"), "sourceHash": captures.get("sourceHash"),
        "cases": len(cases), "captured": len(runs), "missing": total - len(runs),
        "passed": passed, "total": total, "rate": passed / total,
        "stablePassCases": sum(item["stablePass"] for item in per_case.values()),
        "inconsistentCases": sum(item["inconsistent"] for item in per_case.values()),
        "failureReasons": dict(reasons), "byCase": per_case, "outcomes": outcomes,
        "limitations": "Speech-act/referent/change understanding only; not full prose, proposal execution, challenge routing, or production success rate."}


def compare(suite, baseline, candidate):
    before, after = score(suite, baseline), score(suite, candidate)
    if before["repeats"] != after["repeats"]:
        raise ValueError("Compare equal repeat counts.")
    return {"baseline": before, "candidate": after,
        "regressedCases": [key for key in before["byCase"] if after["byCase"][key]["passed"] < before["byCase"][key]["passed"]],
        "improvedCases": [key for key in before["byCase"] if after["byCase"][key]["passed"] > before["byCase"][key]["passed"]]}


def run_live(suite, output, repeats=1):
    from llm_client import classify_turn_understanding, _llm_credentials, LLMServiceError, KIMI_MODEL
    if not _llm_credentials()[0]:
        raise ValueError("8010 Kimi credentials must already be configured in the environment.")
    captures = {"suiteHash": suite_hash(suite), "repeats": repeats, "model": KIMI_MODEL,
        "sourceHash": hashlib.sha256(Path(__file__).with_name("llm_client.py").read_bytes()).hexdigest(),
        "runs": []}
    # Refuse overwrite, then persist every run so a later interruption retains results.
    with output.open("x", encoding="utf-8") as stream:
        json.dump(captures, stream, ensure_ascii=False, indent=2)
    for case in suite["cases"]:
        for repeat in range(1, repeats + 1):
            inputs = build_model_input(suite, case)
            started = time.monotonic()
            run = {"caseId": case["id"], "repeat": repeat,
                "mapFingerprint": inputs["snapshot"]["mapFingerprint"]}
            try:
                run["result"] = classify_turn_understanding(inputs["conversation"], inputs["snapshot"],
                    f"semantic-eval-{case['id']}-{repeat}", forced_proposal=inputs["forced_proposal"],
                    proposal_context=inputs["proposal_context"], _deadline=started + 35.0)
            except LLMServiceError as error:
                run["errorCode"] = error.code
            run["latencyMs"] = round((time.monotonic() - started) * 1000)
            captures["runs"].append(run)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output)
    return captures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    scoring = sub.add_parser("score")
    scoring.add_argument("results", type=Path)
    comparison = sub.add_parser("compare")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    running = sub.add_parser("run")
    running.add_argument("--live", action="store_true", help="Explicitly enable billed Kimi calls; no HTTP session creation")
    running.add_argument("--output", required=True, type=Path)
    running.add_argument("--repeats", type=int, choices=range(1, 21), default=1)
    args = parser.parse_args()
    if args.command == "run" and not args.live:
        parser.error("run requires --live; validate/score/compare never call a model")
    suite = load_suite(args.suite)
    if args.command == "validate":
        result = {"suiteHash": suite_hash(suite), "cases": len(suite["cases"]), "stages": len(suite["stages"]), "modelCalls": 0}
    elif args.command == "score":
        result = score(suite, json.loads(args.results.read_text(encoding="utf-8")))
    elif args.command == "compare":
        result = compare(suite, json.loads(args.baseline.read_text(encoding="utf-8")), json.loads(args.candidate.read_text(encoding="utf-8")))
    else:
        result = score(suite, run_live(suite, args.output, args.repeats))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
