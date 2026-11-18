"""Explicit, bounded synthetic Kimi probe. Never creates a session or opens SQLite."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from cocreation.clock import Clock, SYSTEM_CLOCK
from cocreation.domain.level_validation import build_stage_snapshot, validate_and_solve
from cocreation.domain.proposal_diagnostics import ProposalDiagnostic, CURRENT_PROPOSAL_DIAGNOSTIC
from cocreation.domain.understanding_review import REVIEW_VERSION
from cocreation.llm.config_and_runtime import (
    LLMConfig,
    LLMServiceError,
    _default_create_async_client,
    _default_request_completion,
)
from cocreation.llm.service import CoCreationLLMService


BACKEND = Path(__file__).resolve().parents[2]
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


def bounded_factory(factory, calls, limit=MAX_CALLS, initial_count=0, clock: Clock = SYSTEM_CLOCK):
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
            started = clock.monotonic()
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
                entry["latencyMs"] = round((clock.monotonic() - started) * 1000)
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


def run(
    output,
    selected=None,
    prior=None,
    *,
    llm_service: CoCreationLLMService | None = None,
    clock: Clock | None = None,
):
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
    effective_clock = clock if clock is not None else (llm_service.clock if llm_service is not None else SYSTEM_CLOCK)
    if llm_service is None:
        load_dotenv(BACKEND / ".env", override=False)
        base_config = LLMConfig.from_env()
    else:
        base_config = llm_service.config
    if not base_config.api_key:
        raise ValueError("8010 Kimi credentials are unavailable.")
    # Validate a synthetic map locally; the probe stops after understanding/review.
    validate_and_solve(ROWS, clock=effective_clock)
    snapshot = build_stage_snapshot(ROWS, version_id="synthetic-probe")
    llm_bytes = b"".join(p.read_bytes() for p in sorted((BACKEND / "cocreation" / "llm").glob("*.py")))
    captures = {"model": base_config.model, "reviewVersion": REVIEW_VERSION,
        "syntheticOnly": True, "sqliteAccess": "none", "maximumActualCalls": MAX_CALLS,
        "priorActualCalls": prior_count,
        "sourceHash": hashlib.sha256(llm_bytes).hexdigest(),
        "sourceHashes": {
            "cocreation/llm": hashlib.sha256(llm_bytes).hexdigest(),
            "cocreation/domain/understanding_review.py": hashlib.sha256((BACKEND / "cocreation" / "domain" / "understanding_review.py").read_bytes()).hexdigest(),
            "cocreation/tools/understanding_review_probe.py": hashlib.sha256((BACKEND / "cocreation" / "tools" / "understanding_review_probe.py").read_bytes()).hexdigest(),
        },
        "calls": [], "cases": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
    base_factory = (llm_service.client_factory if llm_service is not None and llm_service.client_factory is not None else _default_create_async_client)
    factory = bounded_factory(base_factory, captures["calls"], initial_count=prior_count, clock=effective_clock)
    base_requester = (llm_service.completion_requester if llm_service is not None and llm_service.completion_requester is not None else _default_request_completion)
    for case_id, latest in CASES:
        if case_id not in selected:
            continue
        if prior_count + len(captures["calls"]) >= MAX_CALLS:
            captures["stoppedAtCallLimit"] = True
            break
        started, before = effective_clock.monotonic(), len(captures["calls"])
        entry = {"caseId": case_id, "input": latest, "initialUnderstanding": "synthetic_fault" if case_id.endswith("injection") else "live"}
        if case_id == "synthetic-5-native-correction":
            entry["initialUnderstanding"] = "live_then_synthetic_semantic_fault"
        diagnostic = ProposalDiagnostic()
        token = CURRENT_PROPOSAL_DIAGNOSTIC.set(diagnostic)
        async def request(*args, **kwargs):
            if case_id.endswith("injection") and kwargs.get("task") == "turn_understanding":
                content = json.dumps(injected_candidate(latest), ensure_ascii=False)
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
            response = await base_requester(*args, **kwargs)
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
        case_service = CoCreationLLMService(
            base_config,
            client_factory=factory,
            clock=effective_clock,
            completion_requester=request,
        )
        try:
            entry["result"] = case_service.classify_turn_understanding([{"role": "user", "content": latest}], snapshot,
                case_id, forced_proposal=True, proposal_context={}, _deadline=started + 116)
            entry["status"] = "validated"
        except LLMServiceError as error:
            entry.update(status="safe_failure", code=error.code, details=error.details)
        finally:
            CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)
            entry["latencyMs"] = round((effective_clock.monotonic() - started) * 1000)
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
