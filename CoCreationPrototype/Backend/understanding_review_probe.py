"""Explicit, bounded synthetic Kimi probe. Never creates a session or opens SQLite."""
import argparse
import copy
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import load_dotenv
import llm_client as llm
from level_validation import build_stage_snapshot, validate_and_solve
from proposal_diagnostics import ProposalDiagnostic, CURRENT_PROPOSAL_DIAGNOSTIC
from understanding_review import REVIEW_VERSION


BACKEND = Path(__file__).resolve().parent
MAX_CALLS = 15
ROWS = ["############", "#..........#", "#..p.......#", "#..s..t....#", "#..........#",
        "#..s..t....#", "#..........#", "#..@@......#", "#..........#", "############"]
CASES = [
    ("synthetic-1", "请整理水域，让边界更整齐；保留右侧的内部墙体。"),
    ("synthetic-2", "Only reshape water. Keep all interior walls, boxes, targets and the player start unchanged."),
    ("synthetic-3", "请调整水和内部墙的视觉布局。\n保留要求：\n① 玩家起点不变；\n② 箱子、目标的位置不变。"),
    ("synthetic-4-review-injection", "请整理水域，让水边更整齐；保留右侧的内部墙体。"),
    ("synthetic-5-native-correction", "请调整水域的视觉布局；玩家起点保持不变。"),
]


def bounded_factory(factory, calls, limit=MAX_CALLS, initial_count=0):
    def create_client(*args, **kwargs):
        client = factory(*args, **kwargs)
        original = client.chat.completions.create
        async def create(**request):
            if initial_count + len(calls) >= limit:
                raise RuntimeError("SYNTHETIC_PROBE_CALL_LIMIT")
            fmt = request.get("response_format", {})
            entry = {"call": initial_count + len(calls) + 1, "schema": fmt.get("json_schema", {}).get("name", fmt.get("type")),
                "maxCompletionTokens": request["max_completion_tokens"], "model": request["model"]}
            calls.append(entry)  # Count at the actual transport boundary, including schema fallback.
            started = time.monotonic()
            try:
                result = await original(**request)
                entry["status"] = "returned"
                entry["finishReason"] = result.choices[0].finish_reason
                entry["syntheticResponse"] = result.choices[0].message.content
                usage = getattr(result, "usage", None)
                entry["usage"] = usage.model_dump() if hasattr(usage, "model_dump") else {}
                return result
            except Exception as error:
                entry["status"] = "error"
                entry["errorType"] = type(error).__name__
                entry["httpStatus"] = getattr(error, "status_code", None)
                raise
            finally:
                entry.setdefault("status", "cancelled_or_timed_out")
                entry["latencyMs"] = round((time.monotonic() - started) * 1000)
        client.chat.completions.create = create
        return client
    return create_client


def injected_candidate(latest):
    def item(statement, span, **extra):
        return {"statement": statement, "sourceTurnId": "latest", "evidenceSpan": span, **extra}
    return {"acts": ["revision_request"], "elements": ["water"], "evidenceSpan": latest,
        "directionSufficient": True, "mapRelated": True,
        "changes": [{"component": "water", "operation": "change", "property": "appearance", "evidenceSpan": "请整理水域"}],
        "proposalUnderstanding": {"aspect": "visual", "goals": [item("让水边更整齐", "让水边更整齐")],
            "editScope": [item("整理水域", "请整理水域", component="water")], "focus": [],
            "preserve": [item("允许删除右侧的内部墙体", "保留右侧的内部墙体")],
            "sufficient": True, "nextQuestionDimension": "none"},
        "humanEditEngagement": None, "optionDecision": None}


def run(output, selected=None, prior=None):
    root = (BACKEND / "evaluation_runs").resolve()
    output = output.resolve()
    if not output.is_relative_to(root) or output.exists():
        raise ValueError("Use a NEW output file under Backend/evaluation_runs.")
    prior_count = 0
    if prior:
        previous = json.loads(prior.read_text(encoding="utf-8"))
        if not previous.get("syntheticOnly") or previous.get("maximumActualCalls") != MAX_CALLS:
            raise ValueError("Not a bounded synthetic probe result.")
        prior_count = previous.get("cumulativeActualCalls", previous["actualCalls"])
        if not 0 <= prior_count <= MAX_CALLS:
            raise ValueError("Invalid previous call count.")
    selected = selected or [case_id for case_id, _ in CASES[:4]]
    if "synthetic-5-native-correction" in selected and not prior:
        raise ValueError("The fifth follow-up must include the prior result for the same 15-call cap.")
    load_dotenv(BACKEND / ".env", override=False)
    if not llm._llm_credentials()[0]:
        raise ValueError("8010 Kimi credentials are unavailable.")
    # Validate a synthetic map locally; the probe stops after understanding/review.
    validate_and_solve(ROWS)
    snapshot = build_stage_snapshot(ROWS, version_id="synthetic-probe")
    captures = {"model": llm.KIMI_MODEL, "reviewVersion": REVIEW_VERSION,
        "syntheticOnly": True, "sqliteAccess": "blocked", "maximumActualCalls": MAX_CALLS,
        "priorActualCalls": prior_count,
        "sourceHash": hashlib.sha256((BACKEND / "llm_client.py").read_bytes()).hexdigest(),
        "sourceHashes": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest()
                         for name in ("llm_client.py", "understanding_review.py", "understanding_review_probe.py")},
        "calls": [], "cases": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
    factory = bounded_factory(llm._create_async_client, captures["calls"], initial_count=prior_count)
    original_completion = llm._request_completion
    for case_id, latest in CASES:
        if case_id not in selected:
            continue
        if prior_count + len(captures["calls"]) >= MAX_CALLS:
            captures["stoppedAtCallLimit"] = True
            break
        started, before = time.monotonic(), len(captures["calls"])
        entry = {"caseId": case_id, "input": latest, "initialUnderstanding": "synthetic_fault" if case_id.endswith("injection") else "live"}
        if case_id == "synthetic-5-native-correction":
            entry["initialUnderstanding"] = "live_then_synthetic_semantic_fault"
        diagnostic = ProposalDiagnostic()
        token = CURRENT_PROPOSAL_DIAGNOSTIC.set(diagnostic)
        async def request(*args, **kwargs):
            if case_id.endswith("injection") and kwargs.get("task") == "turn_understanding":
                content = json.dumps(injected_candidate(latest), ensure_ascii=False)
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
            response = await original_completion(*args, **kwargs)
            if case_id == "synthetic-5-native-correction" and kwargs.get("task") == "turn_understanding":
                # One controlled fault in a live-generated candidate, with an exact
                # quote about an entity that really exists. No labels/expected answer
                # are sent to the independent reviewer.
                value = json.loads(response.choices[0].message.content)
                topic = value.get("proposalUnderstanding")
                if isinstance(topic, dict) and isinstance(topic.get("preserve"), list):
                    faulty = {"statement": "允许移动玩家起点", "sourceTurnId": "latest", "evidenceSpan": "玩家起点保持不变"}
                    if topic["preserve"]:
                        topic["preserve"][0] = faulty
                    else:
                        topic["preserve"].append(faulty)
                    response = copy.deepcopy(response)
                    response.choices[0].message.content = json.dumps(value, ensure_ascii=False)
                    entry["injectedField"] = "proposalUnderstanding.preserve[0].statement"
            return response
        try:
            with patch.object(sqlite3, "connect", side_effect=AssertionError("PROBE_SQLITE_ACCESS_FORBIDDEN")), \
                    patch.object(llm, "_create_async_client", factory), patch.object(llm, "_request_completion", request):
                entry["result"] = llm.classify_turn_understanding([{"role": "user", "content": latest}], snapshot,
                    case_id, forced_proposal=True, proposal_context={}, _deadline=started + 116)
            entry["status"] = "validated"
        except llm.LLMServiceError as error:
            entry.update(status="safe_failure", code=error.code, details=error.details)
        finally:
            CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)
            entry["latencyMs"] = round((time.monotonic() - started) * 1000)
            entry["actualCalls"] = len(captures["calls"]) - before
            entry["diagnostic"] = diagnostic.snapshot()
            captures["cases"].append(entry)
            save()
            print(json.dumps({key: entry[key] for key in ("caseId", "status", "latencyMs", "actualCalls")}), flush=True)
    captures["actualCalls"] = len(captures["calls"])
    captures["cumulativeActualCalls"] = prior_count + len(captures["calls"])
    captures["tokens"] = {name: sum((call.get("usage") or {}).get(name, 0) or 0 for call in captures["calls"])
                           for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    save()
    return captures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=[key for key, _ in CASES])
    parser.add_argument("--prior", type=Path, help="Carry forward actual calls already used by this round.")
    args = parser.parse_args()
    if not args.live:
        parser.error("Billed sampling requires --live; ordinary regressions never run this probe.")
    result = run(args.output, args.case, args.prior)
    print(json.dumps({key: result[key] for key in ("actualCalls", "cumulativeActualCalls", "tokens", "reviewVersion")}), flush=True)


if __name__ == "__main__":
    main()
