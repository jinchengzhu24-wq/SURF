"""Component outcomes and bounded recovery shared by the 8010 LLM tasks."""
import json
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass


REPLY_PIPELINE_VERSION = "ordinary-harness-2026-10-13-v2"
CURRENT_REPLY_TRACE = ContextVar("cocreation_reply_trace", default=None)


def failure_reason(error):
    """Stable categories only: never put exception/model text in delivery traces."""
    if isinstance(error, TimeoutError):
        return "UPSTREAM_TIMEOUT"
    if isinstance(error, json.JSONDecodeError):
        return "INVALID_JSON"
    code = getattr(error, "code", "")
    if isinstance(code, str) and code and code.replace("_", "").isalnum() and code.upper() == code:
        return code[:80]
    message = str(error).lower()
    for marker, reason in (("truncat", "TRUNCATED_OUTPUT"), ("output limit", "TRUNCATED_OUTPUT"),
                           ("evidence", "EXACT_EVIDENCE_INVALID"), ("ground", "MAP_GROUNDING_INVALID"),
                           ("coordinate", "MAP_GROUNDING_INVALID"), ("language", "VISIBLE_LANGUAGE_INVALID"),
                           ("implementation identifier", "VISIBLE_LANGUAGE_INVALID"),
                           ("tentative", "TENTATIVE_PRESENTATION_INVALID")):
        if marker in message:
            return reason
    return "VALIDATION_FAILED"


class ReplyTrace:
    """Bounded request-local diagnostics, independent of model and state decisions."""
    def __init__(self, request_id, message_key, version_id, logger):
        self.identity = {"requestId": request_id, "messageKey": message_key,
                         "baseVersionId": version_id, "pipelineVersion": REPLY_PIPELINE_VERSION}
        self.steps = []
        self.started = {}
        self.logger = logger

    def record(self, stage, status, code="", **fields):
        allowed = {"attempt", "modelCalls", "repairAttempts", "optionalSpentSeconds", "optionalRemainingSeconds",
                   "classification", "relation", "source", "cardPresent", "questionCount", "answeredCount",
                   "rule", "assistantTurnId", "branch", "cacheHits", "mapChecks", "questionIds"}
        data = {key: value for key, value in fields.items() if key in allowed}
        stamp = time.monotonic()
        if status == "started":
            self.started[stage] = stamp
        elif stage in self.started:
            data["elapsedMs"] = int(max(0, stamp - self.started.pop(stage)) * 1000)
        step = {"stage": stage, "status": status, "code": code, **data}
        self.steps.append(step)
        self.steps = self.steps[-96:]
        self.logger("reply_pipeline_stage", **self.identity, **step)

    def diagnostic(self, assistant_turn_id=None):
        return {**self.identity, "assistantTurnId": assistant_turn_id, "steps": list(self.steps)}


def trace_reply(stage, status, code="", **fields):
    trace = CURRENT_REPLY_TRACE.get()
    if trace is not None:
        trace.record(stage, status, code, **fields)


@dataclass(frozen=True)
class ComponentCheck:
    component: str
    status: str
    code: str = ""
    dependencies: tuple = ()
    repairable: bool = False

    def diagnostic(self):
        return asdict(self)


class OptionalRepairBudget:
    """Optional presentation work must never consume the recovery reserve."""
    def __init__(self, deadline):
        self.deadline = deadline
        self.spent = 0.0

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining < 20.0:
            return 0.0
        return max(0.0, min(10.0 - self.spent, remaining - 20.0))

    def record(self, started):
        self.spent += max(0.0, time.monotonic() - started)


def parse_json_object(content):
    """Unwrap one fenced object; never guess missing or truncated semantics."""
    source = str(content or "").strip()
    if source.startswith("```") and source.endswith("```"):
        lines = source.splitlines()
        if lines[0].strip().lower() in {"```", "```json"}:
            source = "\n".join(lines[1:-1]).strip()
    value = json.loads(source)
    if not isinstance(value, dict):
        raise ValueError("The response must contain exactly one JSON object.")
    return value


def requirement_cache_key(stage_context, turns):
    """Bind reuse to exact evidence, immutable map, decisions and policy."""
    import hashlib
    data = {
        "snapshot": stage_context.get("stageSnapshot"),
        "turns": turns,
        "answers": (stage_context.get("proposalDiscovery") or {}).get("answers") or [],
        "proposalUnderstanding": (stage_context.get("proposalDiscovery") or {}).get("understanding") or {},
        "decisions": (stage_context.get("revisionDesignContext") or {}).get("confirmedDecisions") or [],
        "projection": stage_context.get("revisionDesignContext") or {},
        "responseLanguage": stage_context.get("responseLanguage", "en"),
        "resolution": stage_context.get("requirementDecisionEvidence"),
        "policyVersion": stage_context.get("requirementPolicyVersion"),
        "promptVersion": "requirements-component-review-6-minimal-evidence-patch",
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
