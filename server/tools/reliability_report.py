"""Read-only reliability audit CLI for the unified SQLite WAL database."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.persistence.database import DEFAULT_DATABASE_PATH

EVENT_TYPES = (
    "message_generation_failed",
    "reply_generation_failed",
    "challenge_reason_review_pending",
    "reply_delivery_outcome",
    "component_recovery",
    "reply_pipeline_diagnostic",
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
    "component_review_validation": "组件复核技术故障（不代表模型理解错误）",
    "component_repair": "局部补丁格式、边界或依据验证失败",
    "unknown": "记录不足，原因未确定",
}


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_key_part(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _safe_nonneg_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def failure_category(code: Any, details: Any = None) -> str:
    if _safe_dict(details).get("failureKind") == "review_validation":
        return "component_review_validation"
    upper = str(code or "").upper()
    if upper.startswith("PATCH_"):
        return "component_repair"
    if (
        "SESSION_" in upper
        or "COCREATION_EXPIRED" in upper
        or upper in {"STATE_UNVERIFIED", "CONCURRENT_CHANGE"}
    ):
        return "state_or_delivery"
    if "TIMEOUT" in upper or "DEADLINE" in upper:
        return "upstream_timeout"
    if upper.startswith("UPSTREAM_"):
        return "upstream_service"
    if upper in {"CONFIGURATION_ERROR", "AUTHENTICATION_ERROR", "LLM_NOT_CONFIGURED"}:
        return "configuration"
    if any(
        part in upper
        for part in (
            "SEARCH_EXHAUSTED",
            "NO_CANDIDATE",
            "UNSOLVABLE",
            "CANDIDATE_DUPLICATED",
            "OBJECTIVE_NOT_MET",
            "SEMANTIC_CONSTRAINT_NOT_MET",
            "SOFT_OBJECTIVE_EVIDENCE_MISSING",
        )
    ):
        return "no_feasible_candidate"
    if any(
        part in upper
        for part in (
            "INVALID_CARD",
            "VERSION_CONFLICT",
            "IDEMPOTENCY",
            "DELIVERY",
            "RESPONSE_CONTRACT",
        )
    ):
        return "state_or_delivery"
    if any(
        part in upper
        for part in (
            "PERMISSION",
            "PROTECTED",
            "EXECUTION_CONTRACT",
            "SEMANTIC_CONTRACT",
            "REVISION_CONTRACT",
        )
    ):
        return "permission_or_contract"
    if upper.startswith("OPTIONAL_") or upper in {
        "PRESENTATION_QUALITY",
        "QUALITY_REVIEW_ISSUES",
    }:
        return "component_quality"
    if any(
        part in upper
        for part in ("MODEL_", "GROUNDING", "REQUIREMENT_", "EVIDENCE", "VALIDATION")
    ):
        return "validation_rejected"
    return "unknown"


def _task(event: dict[str, Any]) -> str:
    payload = _safe_dict(event.get("payload"))
    task = payload.get("task")
    if (
        event.get("eventType") in {"message_generation_failed", "challenge_reason_review_pending"}
        or task in {"challenge_review", "proposal_clarification"}
    ):
        return "message"
    return str(task) if task is not None else "message"


def _latencies(samples: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(
        float(item["latencyMs"])
        for item in samples
        if isinstance(item.get("latencyMs"), (int, float))
        and not isinstance(item["latencyMs"], bool)
        and math.isfinite(item["latencyMs"])
        and item["latencyMs"] >= 0
    )

    def percentile(fraction: float) -> float | None:
        if not values:
            return None
        return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 2)

    return {
        "samples": len(values),
        "missing": len(samples) - len(values),
        "p50Ms": percentile(0.5),
        "p95Ms": percentile(0.95),
        "maxMs": max(values) if values else None,
        "scope": "recorded generation latency; not browser end-to-end latency",
    }


def summarize(events: list[dict[str, Any]], example_limit: int = 3) -> dict[str, Any]:
    requests: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    recoveries: dict[tuple[Any, Any], dict[str, Any]] = {}
    pipeline_attempts: dict[tuple[Any, Any], dict[str, Any]] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        session_ref = _safe_key_part(event.get("sessionId"))
        ev_type = event.get("eventType")
        if ev_type == "reply_pipeline_diagnostic":
            value = _safe_dict(event.get("payload"))
            val_req_id = _safe_key_part(value.get("requestId"))
            if val_req_id:
                pipeline_attempts[(session_ref, val_req_id)] = value
            continue
        if ev_type == "component_recovery":
            payload = _safe_dict(event.get("payload"))
            rec_id = _safe_key_part(payload.get("recoveryId"))
            if rec_id:
                recoveries[(session_ref, rec_id)] = payload
            continue
        if ev_type not in EVENT_TYPES:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not payload.get("messageKey"):
            skipped["missingMessageKeyOrPayload"] += 1
            continue
        task = _task(event)
        key = (session_ref, _safe_key_part(task), _safe_key_part(payload["messageKey"]))
        success = ev_type == "reply_delivery_outcome"
        delivered = (
            payload.get("reliableBodyDelivered")
            if isinstance(payload.get("reliableBodyDelivered"), bool)
            else None
        )
        sample = {
            **payload,
            "task": task,
            "createdAt": event.get("createdAt"),
            "reliableBodyDelivered": delivered if success else False,
        }
        bucket = requests.setdefault(
            key, {"first": sample, "last": sample, "records": [], "issues": set()}
        )
        bucket["last"] = sample
        bucket["records"].append(sample)
        code = payload.get("failureCode") or payload.get("code")
        if not success or code:
            bucket["issues"].add(
                (failure_category(code, payload.get("details")), str(code or "UNKNOWN"))
            )

    def counts(samples: list[dict[str, Any]]) -> dict[str, Any]:
        eligible = [
            item
            for item in samples
            if not item.get("deterministicReceipt") and not item.get("snapshotFallback")
        ]
        result: dict[str, Any] = {}
        for field in (
            "modelGenerationSucceeded",
            "reliableBodyDelivered",
            "fullQualityPassed",
        ):
            observed = [item for item in eligible if isinstance(item.get(field), bool)]
            passed = sum(bool(item[field]) for item in observed)
            result[field] = {
                "passed": passed,
                "total": len(eligible),
                "observed": len(observed),
                "missing": len(eligible) - len(observed),
                "rate": passed / len(observed) if observed else None,
            }
        return result

    first = [item["first"] for item in requests.values()]
    last = [item["last"] for item in requests.values()]
    proposals = [
        item
        for item in requests.values()
        if any(record.get("proposalRequested") for record in item["records"])
    ]
    groups: dict[str, dict[str, Any]] = {}
    for key, bucket in requests.items():
        for category in sorted({issue[0] for issue in bucket["issues"]}):
            group = groups.setdefault(
                category,
                {
                    "category": category,
                    "label": CATEGORY_LABELS.get(category, CATEGORY_LABELS["unknown"]),
                    "affectedRequests": 0,
                    "bodyDeliveredAfterIssue": 0,
                    "withoutBodyAtEnd": 0,
                    "bodyUnknownAtEnd": 0,
                    "codes": Counter(),
                    "examples": [],
                },
            )
            group["affectedRequests"] += 1
            delivered = bucket["last"]["reliableBodyDelivered"]
            group["bodyDeliveredAfterIssue"] += int(delivered is True)
            group["withoutBodyAtEnd"] += int(delivered is False)
            group["bodyUnknownAtEnd"] += int(delivered is None)
            group["codes"].update(
                {issue[1] for issue in bucket["issues"] if issue[0] == category}
            )
            if len(group["examples"]) < max(0, example_limit):
                group["examples"].append(
                    {
                        "requestRef": hashlib.sha256(
                            json.dumps(key, ensure_ascii=False).encode()
                        ).hexdigest()[:12],
                        "task": key[1],
                        "bodyDeliveredAtEnd": delivered,
                        "firstObservedAt": bucket["first"].get("createdAt"),
                        "codes": sorted(
                            {issue[1] for issue in bucket["issues"] if issue[0] == category}
                        ),
                    }
                )

    ranked = sorted(
        groups.values(),
        key=lambda item: (-item["withoutBodyAtEnd"], -item["affectedRequests"], item["category"]),
    )
    for group in ranked:
        group["codes"] = dict(sorted(group["codes"].items()))
    tasks = sorted({item["task"] for item in last})
    verified = sum(bool(item["last"].get("verifiedProposal")) for item in proposals)
    return {
        "logicalRequests": len(requests),
        "pipelineDiagnostics": {"httpAttempts": len(pipeline_attempts)},
        "stateRecoveries": {
            "tasks": len(recoveries),
            "attemptsUsed": sum(
                _safe_nonneg_int(item.get("attemptsUsed")) for item in recoveries.values()
            ),
            "byStatus": dict(
                Counter(str(item.get("status") or "unknown") for item in recoveries.values())
            ),
        },
        "retriedRequests": sum(len(item["records"]) > 1 for item in requests.values()),
        "recordedOutcomes": sum(len(item["records"]) for item in requests.values()),
        "firstAttempt": counts(first),
        "afterRetries": counts(last),
        "verifiedProposalCount": sum(bool(item.get("verifiedProposal")) for item in last),
        "verifiedProposalRate": {
            "passed": verified,
            "total": len(proposals),
            "rate": verified / len(proposals) if proposals else None,
        },
        "failureGroups": ranked,
        "topIssues": ranked[:3],
        "latency": _latencies(last),
        "byTask": {
            task: {
                "logicalRequests": sum(item["task"] == task for item in last),
                "afterRetries": counts([item for item in last if item["task"] == task]),
                "latency": _latencies([item for item in last if item["task"] == task]),
            }
            for task in tasks
        },
        "skippedRecords": dict(skipped),
    }


def utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def read_events(path: Path, since: str, until: str | None = None) -> list[dict[str, Any]]:
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        sql = (
            "SELECT session_id, event_type, payload_json, created_at FROM audit_events "
            "WHERE julianday(created_at) >= julianday(?)"
        )
        parameters: list[Any] = [since]
        if until:
            sql += " AND julianday(created_at) < julianday(?)"
            parameters.append(until)
        sql += (
            " AND event_type IN ("
            + ",".join("?" for _ in EVENT_TYPES)
            + ") ORDER BY julianday(created_at), id"
        )
        parameters.extend(EVENT_TYPES)
        result: list[dict[str, Any]] = []
        for session_id, event_type, payload, created_at in database.execute(sql, parameters):
            try:
                parsed = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            result.append(
                {
                    "sessionId": session_id,
                    "eventType": event_type,
                    "payload": parsed,
                    "createdAt": created_at,
                }
            )

        # Also include operational recovery tasks from component_recovery_tasks when present
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "component_recovery_tasks" in tables:
            rec_sql = (
                "SELECT session_id, recovery_id, state_json, updated_at "
                "FROM component_recovery_tasks WHERE julianday(updated_at) >= julianday(?)"
            )
            rec_params: list[Any] = [since]
            if until:
                rec_sql += " AND julianday(updated_at) < julianday(?)"
                rec_params.append(until)
            for session_id, recovery_id, state_json, updated_at in database.execute(
                rec_sql, rec_params
            ):
                try:
                    parsed_state = json.loads(state_json)
                except (TypeError, json.JSONDecodeError):
                    parsed_state = {}
                if isinstance(parsed_state, dict):
                    result.append(
                        {
                            "sessionId": session_id,
                            "eventType": "component_recovery",
                            "payload": {"recoveryId": recovery_id, **parsed_state},
                            "createdAt": updated_at,
                        }
                    )
        return result


def render_text(report: dict[str, Any]) -> str:
    recovery = report.get("stateRecoveries") or {}
    lines = [
        f"逻辑请求：{report['logicalRequests']}；多条结果记录的请求：{report['retriedRequests']}",
        (
            f"必要状态补齐：{recovery.get('tasks', 0)} 项；"
            f"尝试 {recovery.get('attemptsUsed', 0)} 次；"
            f"状态 {recovery.get('byStatus', {})}（不计入正文成功率）"
        ),
        f"验证提案：{report['verifiedProposalRate']['passed']}/{report['verifiedProposalRate']['total']}",
        (
            f"已记录生成耗时 P50/P95（毫秒）：{report['latency']['p50Ms']}/"
            f"{report['latency']['p95Ms']}；缺失 {report['latency']['missing']}"
        ),
        "优先检查的三类问题（按最终未交付正文数排序）：",
    ]
    for item in report["topIssues"]:
        lines.append(
            f"- {item['label']}：涉及 {item['affectedRequests']}；"
            f"最终无正文 {item['withoutBodyAtEnd']}；最终有正文 {item['bodyDeliveredAfterIssue']}；"
            f"交付未知 {item['bodyUnknownAtEnd']}；代码 {','.join(item['codes'])}"
        )
    if not report["topIssues"]:
        lines.append("- 当前记录没有问题证据；不代表系统没有失败。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--since", required=True, type=utc_timestamp)
    parser.add_argument("--until", type=utc_timestamp)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    if args.until:
        until_dt = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
        since_dt = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if until_dt <= since_dt:
            parser.error("--until must be after --since")
    report = summarize(read_events(args.database, args.since, args.until))
    report["window"] = {"since": args.since, "until": args.until}
    output = (
        render_text(report)
        if args.format == "text"
        else json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(output)


if __name__ == "__main__":
    main()
