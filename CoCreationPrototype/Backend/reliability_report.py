"""Read-only audit report; never reads conversation prose or calls a model."""
import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


EVENT_TYPES = (
    "message_generation_failed", "reply_generation_failed",
    "challenge_reason_review_pending", "reply_delivery_outcome",
)
CATEGORY_LABELS = {
    "upstream_timeout": "请求时间预算或模型/网络超时",
    "upstream_service": "模型上游连接、限流或服务错误",
    "validation_rejected": "输出或证据验证未通过（需人工判断原因）",
    "no_feasible_candidate": "没有找到合格候选",
    "permission_or_contract": "权限或执行合同限制",
    "state_or_delivery": "版本、幂等或交付状态问题",
    "configuration": "服务配置问题",
    "component_quality": "辅助组件或表达质量问题",
    "unknown": "记录不足，原因未确定",
}


def failure_category(code):
    code = str(code or "").upper()
    if "SESSION_" in code or "COCREATION_EXPIRED" in code:
        return "state_or_delivery"
    if "TIMEOUT" in code or "DEADLINE" in code:
        return "upstream_timeout"
    if code.startswith("UPSTREAM_"):
        return "upstream_service"
    if code in {"CONFIGURATION_ERROR", "AUTHENTICATION_ERROR"}:
        return "configuration"
    if any(part in code for part in ("SEARCH_EXHAUSTED", "NO_CANDIDATE", "UNSOLVABLE", "CANDIDATE_DUPLICATED", "OBJECTIVE_NOT_MET", "SEMANTIC_CONSTRAINT_NOT_MET", "SOFT_OBJECTIVE_EVIDENCE_MISSING")):
        return "no_feasible_candidate"
    if any(part in code for part in ("INVALID_CARD", "VERSION_CONFLICT", "IDEMPOTENCY", "DELIVERY", "RESPONSE_CONTRACT")):
        return "state_or_delivery"
    if any(part in code for part in ("PERMISSION", "PROTECTED", "EXECUTION_CONTRACT", "SEMANTIC_CONTRACT", "REVISION_CONTRACT")):
        return "permission_or_contract"
    if code.startswith("OPTIONAL_") or code in {"PRESENTATION_QUALITY", "QUALITY_REVIEW_ISSUES"}:
        return "component_quality"
    if any(part in code for part in ("MODEL_", "GROUNDING", "REQUIREMENT_", "EVIDENCE", "VALIDATION")):
        return "validation_rejected"
    return "unknown"


def _task(event):
    task = event["payload"].get("task")
    # Challenge review is part of the same message, not an extra user request.
    if event["eventType"] in {"message_generation_failed", "challenge_reason_review_pending"} or task == "challenge_review":
        return "message"
    return task or "message"


def _latencies(samples):
    values = sorted(float(item["latencyMs"]) for item in samples
        if isinstance(item.get("latencyMs"), (int, float))
        and not isinstance(item["latencyMs"], bool)
        and math.isfinite(item["latencyMs"]) and item["latencyMs"] >= 0)
    def percentile(fraction):
        return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 2) if values else None
    return {"samples": len(values), "missing": len(samples) - len(values),
        "p50Ms": percentile(0.5), "p95Ms": percentile(0.95),
        "maxMs": max(values) if values else None,
        "scope": "recorded generation latency; not browser end-to-end latency"}


def summarize(events, example_limit=3):
    requests = {}
    skipped = Counter()
    for event in events:
        if event.get("eventType") not in EVENT_TYPES:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not payload.get("messageKey"):
            skipped["missingMessageKeyOrPayload"] += 1
            continue
        task = _task(event)
        key = (event.get("sessionId"), task, payload["messageKey"])
        success = event["eventType"] == "reply_delivery_outcome"
        delivered = payload.get("reliableBodyDelivered") if isinstance(payload.get("reliableBodyDelivered"), bool) else None
        sample = {**payload, "task": task, "createdAt": event.get("createdAt"),
            "reliableBodyDelivered": delivered if success else False}
        bucket = requests.setdefault(key, {"first": sample, "last": sample, "records": [], "issues": set()})
        bucket["last"] = sample
        bucket["records"].append(sample)
        code = payload.get("failureCode") or payload.get("code")
        if not success or code:
            bucket["issues"].add((failure_category(code), str(code or "UNKNOWN")))
        for component in payload.get("componentChecks") or []:
            if isinstance(component, dict) and component.get("status") in {"omitted", "rejected", "failed"}:
                component_code = str(component.get("code") or "UNSPECIFIED_COMPONENT_ISSUE")
                bucket["issues"].add((failure_category(component_code), component_code))

    def counts(samples):
        eligible = [item for item in samples if not item.get("deterministicReceipt") and not item.get("snapshotFallback")]
        result = {}
        for field in ("modelGenerationSucceeded", "reliableBodyDelivered", "fullQualityPassed"):
            observed = [item for item in eligible if isinstance(item.get(field), bool)]
            passed = sum(item[field] for item in observed)
            result[field] = {"passed": passed, "total": len(eligible), "observed": len(observed),
                "missing": len(eligible) - len(observed), "rate": passed / len(observed) if observed else None}
        return result

    first = [item["first"] for item in requests.values()]
    last = [item["last"] for item in requests.values()]
    # A retry success may omit proposalRequested; keep the original request intent.
    proposals = [item for item in requests.values() if any(record.get("proposalRequested") for record in item["records"])]
    groups = {}
    for key, bucket in requests.items():
        for category in sorted({issue[0] for issue in bucket["issues"]}):
            group = groups.setdefault(category, {"category": category, "label": CATEGORY_LABELS[category],
                "affectedRequests": 0, "bodyDeliveredAfterIssue": 0, "withoutBodyAtEnd": 0, "bodyUnknownAtEnd": 0,
                "codes": Counter(), "examples": []})
            group["affectedRequests"] += 1
            delivered = bucket["last"]["reliableBodyDelivered"]
            group["bodyDeliveredAfterIssue"] += int(delivered is True)
            group["withoutBodyAtEnd"] += int(delivered is False)
            group["bodyUnknownAtEnd"] += int(delivered is None)
            group["codes"].update({issue[1] for issue in bucket["issues"] if issue[0] == category})
            if len(group["examples"]) < max(0, example_limit):
                group["examples"].append({
                    "requestRef": hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:12],
                    "task": key[1], "bodyDeliveredAtEnd": delivered,
                    "firstObservedAt": bucket["first"].get("createdAt"),
                    "codes": sorted({issue[1] for issue in bucket["issues"] if issue[0] == category}),
                })
    ranked = sorted(groups.values(), key=lambda item: (-item["withoutBodyAtEnd"], -item["affectedRequests"], item["category"]))
    for group in ranked:
        group["codes"] = dict(sorted(group["codes"].items()))
    tasks = sorted({item["task"] for item in last})
    verified = sum(bool(item["last"].get("verifiedProposal")) for item in proposals)
    return {
        "logicalRequests": len(requests),
        "retriedRequests": sum(len(item["records"]) > 1 for item in requests.values()),
        "recordedOutcomes": sum(len(item["records"]) for item in requests.values()),
        "firstAttempt": counts(first), "afterRetries": counts(last),
        "verifiedProposalCount": sum(bool(item.get("verifiedProposal")) for item in last),
        "verifiedProposalRate": {"passed": verified, "total": len(proposals), "rate": verified / len(proposals) if proposals else None},
        "snapshotFallbackCount": sum(bool(item.get("snapshotFallback")) for item in last),
        "failureGroups": ranked, "topIssues": ranked[:3], "latency": _latencies(last),
        "byTask": {task: {"logicalRequests": sum(item["task"] == task for item in last),
            "afterRetries": counts([item for item in last if item["task"] == task]),
            "latency": _latencies([item for item in last if item["task"] == task])} for task in tasks},
        "skippedRecords": dict(skipped),
        "coverage": {
            "retryUnit": "multiple audit outcome records for one session/task/message key; not internal model attempts",
            "rates": "missing booleans are unknown, not failures; rate denominator is observed",
            "issues": "codes classify observed symptoms, not proven misunderstanding or false rejection; groups can overlap",
            "delivery": "body delivery does not prove a proposal succeeded or the browser received it",
            "unobserved": ["browser delivery failures", "failures before an audit outcome", "human-rated semantic quality", "token cost"],
        },
    }


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_events(path, since, until=None):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        sql = "SELECT session_id, event_type, payload_json, created_at FROM audit_events WHERE julianday(created_at) >= julianday(?)"
        parameters = [since]
        if until:
            sql += " AND julianday(created_at) < julianday(?)"
            parameters.append(until)
        sql += " AND event_type IN (" + ",".join("?" for _ in EVENT_TYPES) + ") ORDER BY julianday(created_at), id"
        parameters.extend(EVENT_TYPES)
        result = []
        for session_id, event_type, payload, created_at in database.execute(sql, parameters):
            try:
                parsed = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            result.append({"sessionId": session_id, "eventType": event_type, "payload": parsed, "createdAt": created_at})
        return result


def render_text(report):
    lines = [f"逻辑请求：{report['logicalRequests']}；多条结果记录的请求：{report['retriedRequests']}",
        f"验证提案：{report['verifiedProposalRate']['passed']}/{report['verifiedProposalRate']['total']}",
        f"已记录生成耗时 P50/P95（毫秒）：{report['latency']['p50Ms']}/{report['latency']['p95Ms']}；缺失 {report['latency']['missing']}",
        "优先检查的三类问题（按最终未交付正文数排序）："]
    for item in report["topIssues"]:
        lines.append(f"- {item['label']}：涉及 {item['affectedRequests']}；最终无正文 {item['withoutBodyAtEnd']}；最终有正文 {item['bodyDeliveredAfterIssue']}；交付未知 {item['bodyUnknownAtEnd']}；代码 {','.join(item['codes'])}")
    if not report["topIssues"]:
        lines.append("- 当前记录没有问题证据；不代表系统没有失败。")
    lines.append("错误码只说明观测到的问题；误解/误伤需人工复核。浏览器交付、审计前失败和成本尚未覆盖。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--since", required=True, type=utc_timestamp, help="UTC by default; accepts explicit timezone offsets")
    parser.add_argument("--until", type=utc_timestamp, help="Exclusive end of reporting interval")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    if args.until and datetime.fromisoformat(args.until.replace("Z", "+00:00")) <= datetime.fromisoformat(args.since.replace("Z", "+00:00")):
        parser.error("--until must be after --since")
    report = summarize(read_events(args.database, args.since, args.until))
    report["window"] = {"since": args.since, "until": args.until}
    print(render_text(report) if args.format == "text" else json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
