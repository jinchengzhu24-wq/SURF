"""Bounded structural diagnostics; never infer designer meaning from prose."""
import asyncio
import functools
import inspect
import json
import re
from contextvars import ContextVar


VERSION = "proposal-diagnostics-2026-10-13-v1"
CURRENT_PROPOSAL_DIAGNOSTIC = ContextVar("proposal_diagnostic", default=None)
PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*|\[[0-9]+\])*$")


class ProposalValidationError(ValueError):
    def __init__(self, issues, *, truncated=False, message="Proposal meaning lacks exact designer evidence."):
        self.issues = issues[:16]
        self.issue_count = len(issues)
        self.issues_truncated = truncated or len(issues) > 16
        super().__init__(message)


def structural_issue(path, code, source_kind="unknown"):
    return {"fieldPath": path, "reasonCode": code, "sourceKind": source_kind}


def safe_issues(error):
    result = []
    for issue in getattr(error, "issues", [])[:16]:
        if not isinstance(issue, dict):
            continue
        path, code = issue.get("fieldPath"), issue.get("reasonCode")
        if isinstance(path, str) and PATH.fullmatch(path) and isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,80}", code):
            kind = issue.get("sourceKind")
            result.append(structural_issue(path, code, kind if kind in {
                "latest_user", "historical_user", "assistant", "external_reference", "server", "unknown", "invalid_type"
            } else "unknown"))
    return result


def reason_code(error):
    issues = safe_issues(error)
    if issues:
        return issues[0]["reasonCode"]
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "BUDGET_OR_ATTEMPT_TIMEOUT"
    if isinstance(error, json.JSONDecodeError):
        return "INVALID_JSON"
    if type(error).__name__ == "ProposalSearchExhausted":
        return "SEARCH_NO_VALID_CANDIDATE"
    if type(error).__name__ == "SemanticConstraintError":
        return "SEMANTIC_CONSTRAINT_FAILED"
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,80}", code):
        return code
    # Unclassified validation stays unknown; exception text is not evidence.
    return "UNCLASSIFIED_VALIDATION"


class ProposalDiagnostic:
    def __init__(self, identity=None, logger=None):
        self.identity = identity or {}
        self.logger = logger
        self.steps = []
        self.first_failure = None
        self.final_failure = None
        self.pending_repairs = set()
        self.outcome = "in_progress"

    def record(self, stage, component, status, code="", **fields):
        step = {"stage": stage, "component": component, "status": status, "reasonCode": code}
        for name in ("attempt", "repairAttempts", "maximumAttempts", "issueCount", "issuesTruncated", "sourceTurnId", "proposalId", "bindingSignature"):
            if name not in fields:
                continue
            value = fields.get(name)
            if name in {"attempt", "repairAttempts", "maximumAttempts", "issueCount"}:
                if value is None or type(value) is int:
                    step[name] = value
            elif name == "issuesTruncated":
                step[name] = value is True
            elif isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
                step[name] = value
        if "issues" in fields:
            holder = ProposalValidationError(fields["issues"])
            step["issues"] = safe_issues(holder)
        self.steps.append(step)
        self.steps = self.steps[-96:]
        if status in {"failed", "rejected", "revalidation_failed"}:
            self.first_failure = self.first_failure or step
            self.final_failure = step
        if self.logger:
            self.logger("proposal_pipeline_stage", **self.identity, **step)

    def error(self, stage, component, error, **fields):
        existing = (getattr(error, "details", {}) or {}).get("proposalDiagnostic") or {}
        if existing.get("finalFailure"):
            # Wrapping an error must preserve its actual failing phase.
            self.final_failure = existing["finalFailure"]
            return
        fields.setdefault("attempt", getattr(error, "attempts_used", None))
        fields.setdefault("maximumAttempts", (getattr(error, "details", {}) or {}).get("maximumAttempts"))
        self.record(stage, component, "failed", reason_code(error), issues=safe_issues(error),
                    issuesTruncated=getattr(error, "issues_truncated", False),
                    issueCount=getattr(error, "issue_count", None), **fields)

    def repair(self, component, status):
        self.record("component_repair", component, status)
        if status in {"proposed", "merged"}:
            self.pending_repairs.add(component)
        elif status == "revalidated":
            self.pending_repairs.discard(component)

    def snapshot(self):
        return {"diagnosticVersion": VERSION, **self.identity, "firstFailure": self.first_failure,
                "finalFailure": self.final_failure, "requestOutcome": self.outcome, "steps": list(self.steps),
                "pendingRevalidation": sorted(self.pending_repairs)}


def trace_proposal(stage, component, status, code="", **fields):
    trace = CURRENT_PROPOSAL_DIAGNOSTIC.get()
    if trace is not None:
        trace.record(stage, component, status, code, **fields)


def trace_repair(component, status):
    trace = CURRENT_PROPOSAL_DIAGNOSTIC.get()
    if trace is not None:
        trace.repair(component, status)


def proposal_phase(stage, component, *, revalidates=None):
    """Instrumentation only: does not add calls, retries, permissions or state."""
    def decorate(function):
        signature = inspect.signature(function)

        def begin(args, kwargs):
            trace = CURRENT_PROPOSAL_DIAGNOSTIC.get()
            token = None
            if trace is None:
                bound = signature.bind_partial(*args, **kwargs).arguments
                request = bound.get("request")
                request_id = bound.get("request_id") or getattr(getattr(request, "state", None), "request_id", None)
                trace = ProposalDiagnostic({"requestId": request_id} if request_id else {})
                token = CURRENT_PROPOSAL_DIAGNOSTIC.set(trace)
            trace.record(stage, component, "started")
            return trace, token

        def finish(trace, token, error=None):
            try:
                if error is not None:
                    trace.error(stage, component, error)
                    if token is not None:
                        trace.outcome = "failed"
                    error.details = {**(getattr(error, "details", {}) or {}), "proposalDiagnostic": trace.snapshot()}
                else:
                    trace.record(stage, component, "passed")
                    if revalidates and revalidates in trace.pending_repairs:
                        trace.repair(revalidates, "revalidated")
                    if token is not None:
                        trace.outcome = "passed"
                        trace.final_failure = None
            finally:
                if token is not None:
                    CURRENT_PROPOSAL_DIAGNOSTIC.reset(token)

        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapped(*args, **kwargs):
                trace, token = begin(args, kwargs)
                try:
                    result = await function(*args, **kwargs)
                except Exception as error:
                    finish(trace, token, error)
                    raise
                finish(trace, token)
                return result
        else:
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                trace, token = begin(args, kwargs)
                try:
                    result = function(*args, **kwargs)
                except Exception as error:
                    finish(trace, token, error)
                    raise
                finish(trace, token)
                return result
        return wrapped
    return decorate
