"""Read-only availability report. Does not inspect chat prose or call Kimi."""
import argparse
import json
import sqlite3
from pathlib import Path


def summarize(events):
    requests = {}
    for event in events:
        payload = event["payload"]
        key = (event["sessionId"], payload.get("messageKey"))
        if not key[1]:
            continue
        success = event["eventType"] == "reply_delivery_outcome"
        sample = {**payload, "reliableBodyDelivered": success and payload.get("reliableBodyDelivered", False)}
        bucket = requests.setdefault(key, {"first": sample, "last": sample, "attempts": 0})
        bucket["last"] = sample
        bucket["attempts"] += 1
    def counts(samples):
        eligible = [sample for sample in samples if not sample.get("deterministicReceipt") and not sample.get("snapshotFallback")]
        total = len(eligible)
        def rate(field):
            passed = sum(bool(sample.get(field)) for sample in eligible)
            return {"passed": passed, "total": total, "rate": passed / total if total else None}
        return {field: rate(field) for field in ("modelGenerationSucceeded", "reliableBodyDelivered", "fullQualityPassed")}
    last = [item["last"] for item in requests.values()]
    proposals = [item for item in last if item.get("proposalRequested")]
    verified_proposals = sum(bool(item.get("verifiedProposal")) for item in proposals)
    return {"logicalRequests": len(requests), "retriedRequests": sum(item["attempts"] > 1 for item in requests.values()),
        "firstAttempt": counts([item["first"] for item in requests.values()]), "afterRetries": counts(last),
        "verifiedProposalCount": sum(bool(item.get("verifiedProposal")) for item in last),
        "verifiedProposalRate": {"passed": verified_proposals, "total": len(proposals),
            "rate": verified_proposals / len(proposals) if proposals else None},
        "snapshotFallbackCount": sum(bool(item.get("snapshotFallback")) for item in last)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--since", required=True, help="UTC timestamp, e.g. 2026-10-03T08:00:00")
    args = parser.parse_args()
    database = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        events = [{"sessionId": session_id, "eventType": event_type, "payload": json.loads(payload)}
            for session_id, event_type, payload in database.execute(
                """SELECT session_id, event_type, payload_json FROM audit_events
                   WHERE created_at >= ? AND event_type IN
                       ('message_generation_failed', 'reply_generation_failed',
                        'challenge_reason_review_pending', 'reply_delivery_outcome')
                   ORDER BY created_at, id""", (args.since,))]
        print(json.dumps(summarize(events), ensure_ascii=False, indent=2))
    finally:
        database.close()


if __name__ == "__main__":
    main()
