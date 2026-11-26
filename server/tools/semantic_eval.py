"""Offline/live turn-understanding evaluation CLI adapted to unified server/."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from server.core.clock import SYSTEM_CLOCK, Clock
from server.core.llm_client import AsyncOpenAITransport, LLMError
from server.core.solver import solve_min_steps
from server.llm.pipelines.understanding import UnderstandingPipeline
from server.llm.schemas import TurnRoutingPayload
from server.settings import PROJECT_ROOT, load_settings

DEFAULT_SUITE = PROJECT_ROOT / "server" / "evaluations" / "turn_understanding_v1.json"
ACTS = {
    "evaluation",
    "intent",
    "explanation_request",
    "idea_request",
    "revision_request",
    "challenge",
    "unclear",
}
ELEMENTS = {
    "water",
    "internal_walls",
    "outer_shell",
    "player",
    "boxes",
    "targets",
    "gameplay",
    "unknown",
}
INPUT_KEYS = {"stage", "conversation", "forcedProposal", "proposalContext"}
EXPECTATION_KEYS = {
    "actsAll",
    "actsAny",
    "actsForbidden",
    "elementsAll",
    "mapRelated",
    "directionSufficient",
    "changesAll",
    "forbidProtectedChange",
    "topicAspect",
    "editScopeAll",
    "editScopeForbidden",
    "focusEntitiesAll",
    "preserveEmpty",
}


def suite_hash(suite: dict[str, Any]) -> str:
    encoded = json.dumps(suite, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    suite = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(suite, dict)
        or suite.get("schemaVersion") != 1
        or suite.get("target") != "turn_understanding"
    ):
        raise ValueError("Unsupported evaluation suite.")
    stages = suite.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise ValueError("Evaluation suite requires a stages mapping.")
    for stage_id, stage in stages.items():
        if not isinstance(stage, dict) or not isinstance(stage.get("rows"), list):
            raise ValueError(f"Stage {stage_id} must contain rows.")
        solve_min_steps(stage["rows"])
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Evaluation suite cases must be a non-empty list.")
    seen: set[str] = set()
    for case in cases:
        cid = case.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError("Evaluation case IDs must be unique.")
        seen.add(cid)
        inputs = case.get("input") or {}
        if inputs.get("stage") not in stages or set(inputs) - INPUT_KEYS:
            raise ValueError(f"Invalid input in {cid}.")
    return suite


def check_result(case: dict[str, Any], result: Any) -> list[str]:
    failures: list[str] = []
    try:
        payload = TurnRoutingPayload.model_validate(result)
    except Exception:
        return ["invalid_result_contract"]

    expected = case.get("expected") or {}
    acts = set(payload.acts)
    if not set(expected.get("actsAll") or []).issubset(acts):
        failures.append("missing_required_act")
    if expected.get("actsAny") and not acts.intersection(expected["actsAny"]):
        failures.append("missing_acceptable_act")
    if acts.intersection(expected.get("actsForbidden") or []):
        failures.append("unexpected_act")

    components = {c.component for c in payload.changes}
    if not set(expected.get("elementsAll") or []).issubset(components):
        failures.append("missing_required_element")
    if expected.get("forbidProtectedChange") and any(
        c.component in {"outer_shell", "player", "boxes", "targets"} and c.operation == "change"
        for c in payload.changes
    ):
        failures.append("invented_protected_change")
    return failures


def score(suite: dict[str, Any], captures: dict[str, Any]) -> dict[str, Any]:
    if captures.get("suiteHash") != suite_hash(suite):
        raise ValueError("Captured outputs belong to a different suite.")
    repeats = int(captures.get("repeats", 1))
    cases = {case["id"]: case for case in suite["cases"]}
    runs = {
        (run["caseId"], run["repeat"]): run
        for run in (captures.get("runs") or [])
        if isinstance(run, dict)
    }
    outcomes: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for cid, case in cases.items():
        for repeat in range(1, repeats + 1):
            run = runs.get((cid, repeat))
            if run is None:
                failures = ["missing_capture"]
            elif run.get("errorCode"):
                failures = [f"request_error:{run['errorCode']}"]
            else:
                failures = check_result(case, run.get("result"))
            reasons.update(failures)
            outcomes.append(
                {"caseId": cid, "repeat": repeat, "passed": not failures, "failures": failures}
            )
    total = len(outcomes)
    passed = sum(1 for item in outcomes if item["passed"])
    return {
        "suiteHash": suite_hash(suite),
        "target": "turn_understanding",
        "repeats": repeats,
        "model": captures.get("model"),
        "cases": len(cases),
        "passed": passed,
        "total": total,
        "rate": (passed / total) if total else 0.0,
        "failureReasons": dict(reasons),
        "outcomes": outcomes,
    }


async def run_live_async(
    suite: dict[str, Any],
    output: Path,
    *,
    repeats: int = 1,
    pipeline: UnderstandingPipeline | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> dict[str, Any]:
    owns_transport = False
    transport: AsyncOpenAITransport | None = None
    active_pipeline = pipeline
    if active_pipeline is None:
        settings = load_settings()
        if not settings.has_cocreation_llm_config:
            raise ValueError("Co-creation Kimi/Moonshot credentials are required for --live.")
        transport = AsyncOpenAITransport(
            api_key=settings.cocreation_llm_api_key.get_secret_value(),
            base_url=settings.cocreation_llm_base_url,
            default_timeout_seconds=settings.cocreation_llm_timeout_seconds,
        )
        owns_transport = True
        active_pipeline = UnderstandingPipeline(
            transport, model=settings.cocreation_llm_model, clock=clock
        )

    captures: dict[str, Any] = {
        "suiteHash": suite_hash(suite),
        "repeats": repeats,
        "model": active_pipeline.model,
        "runs": [],
    }
    try:
        for case in suite["cases"]:
            conv = case["input"]["conversation"]
            latest = conv[-1]["content"]
            prev = [t["content"] for t in conv[:-1] if t["role"] == "user"]
            for repeat in range(1, repeats + 1):
                started = clock.monotonic()
                run: dict[str, Any] = {"caseId": case["id"], "repeat": repeat}
                try:
                    res = await active_pipeline.route_turn(
                        latest,
                        previous_user_messages=prev,
                        deadline=started + 35.0,
                        request_id=f"semantic-eval-{case['id']}-{repeat}",
                    )
                    run["result"] = res.payload.model_dump(by_alias=True)
                except LLMError as exc:
                    run["errorCode"] = exc.code
                run["latencyMs"] = round((clock.monotonic() - started) * 1000)
                captures["runs"].append(run)
                serialized = json.dumps(captures, ensure_ascii=False, indent=2)
                await asyncio.to_thread(output.write_text, serialized, encoding="utf-8")
    finally:
        if owns_transport and transport is not None:
            await transport.close()
    return captures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    scoring = sub.add_parser("score")
    scoring.add_argument("results", type=Path)
    running = sub.add_parser("run")
    running.add_argument("--live", action="store_true")
    running.add_argument("--output", required=True, type=Path)
    running.add_argument("--repeats", type=int, choices=range(1, 21), default=1)
    args = parser.parse_args()
    if args.command == "run" and not args.live:
        parser.error("run requires --live; validate and score never call a model")
    suite = load_suite(args.suite)
    if args.command == "validate":
        result: Any = {
            "suiteHash": suite_hash(suite),
            "cases": len(suite["cases"]),
            "stages": len(suite["stages"]),
            "modelCalls": 0,
        }
    elif args.command == "score":
        result = score(suite, json.loads(args.results.read_text(encoding="utf-8")))
    else:
        captures = asyncio.run(run_live_async(suite, args.output, repeats=args.repeats))
        result = score(suite, captures)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
