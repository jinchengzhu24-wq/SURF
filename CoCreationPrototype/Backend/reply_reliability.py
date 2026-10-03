"""Component outcomes and bounded recovery shared by the 8010 LLM tasks."""
import json
import time
from dataclasses import asdict, dataclass


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
        "decisions": (stage_context.get("revisionDesignContext") or {}).get("confirmedDecisions") or [],
        "projection": stage_context.get("revisionDesignContext") or {},
        "responseLanguage": stage_context.get("responseLanguage", "en"),
        "resolution": stage_context.get("requirementDecisionEvidence"),
        "policyVersion": stage_context.get("requirementPolicyVersion"),
        "promptVersion": "requirements-component-review-2",
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
