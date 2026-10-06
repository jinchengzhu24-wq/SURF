import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reliability_report import failure_category, read_events, render_text, summarize, utc_timestamp


def event(key="m", kind="reply_delivery_outcome", **payload):
    return {"sessionId": "private-session", "eventType": kind,
        "payload": {"messageKey": key, **payload}}


class ReliabilityReportTests(unittest.TestCase):
    def test_timeout_then_success_counts_one_issue_and_retains_proposal_request(self):
        report = summarize([
            event(kind="message_generation_failed", code="UPSTREAM_TIMEOUT", proposalRequested=True),
            event(task="message", reliableBodyDelivered=True, modelGenerationSucceeded=True,
                fullQualityPassed=True, verifiedProposal=True, latencyMs=1200),
        ])
        self.assertEqual(report["logicalRequests"], 1)
        self.assertEqual(report["retriedRequests"], 1)
        self.assertEqual(report["verifiedProposalRate"]["total"], 1)
        self.assertEqual(report["verifiedProposalRate"]["passed"], 1)
        self.assertEqual(report["topIssues"][0]["bodyDeliveredAfterIssue"], 1)
        self.assertEqual(report["topIssues"][0]["withoutBodyAtEnd"], 0)

    def test_same_message_key_for_opening_and_chat_does_not_merge_tasks(self):
        report = summarize([event(task="stage_opening", reliableBodyDelivered=True),
            event(kind="message_generation_failed", code="MODEL_RESPONSE_INVALID")])
        self.assertEqual(report["logicalRequests"], 2)
        self.assertEqual(report["byTask"]["message"]["logicalRequests"], 1)

    def test_challenge_review_and_message_result_share_logical_request(self):
        report = summarize([event(kind="challenge_reason_review_pending", task="challenge_review", failureCode="MAP_GROUNDING_INVALID"),
            event(task="message", reliableBodyDelivered=True)])
        self.assertEqual(report["logicalRequests"], 1)
        self.assertEqual(report["topIssues"][0]["category"], "validation_rejected")

    def test_repeated_problem_counts_requests_not_error_records(self):
        report = summarize([event(kind="message_generation_failed", code="UPSTREAM_TIMEOUT") for _ in range(3)])
        self.assertEqual(report["topIssues"][0]["affectedRequests"], 1)
        self.assertEqual(report["topIssues"][0]["codes"]["UPSTREAM_TIMEOUT"], 1)

    def test_no_candidate_analysis_is_body_success_but_not_proposal_success(self):
        report = summarize([event(reliableBodyDelivered=True, failureCode="DETERMINISTIC_SEARCH_EXHAUSTED",
            proposalRequested=True, verifiedProposal=False)])
        self.assertEqual(report["topIssues"][0]["category"], "no_feasible_candidate")
        self.assertEqual(report["topIssues"][0]["bodyDeliveredAfterIssue"], 1)
        self.assertEqual(report["verifiedProposalRate"]["passed"], 0)

    def test_optional_component_issue_preserves_body_success(self):
        report = summarize([event(reliableBodyDelivered=True, fullQualityPassed=False,
            componentChecks=[{"component": "intent_card", "status": "omitted", "code": "OPTIONAL_PRESENTATION_FAILED"}])])
        self.assertEqual(report["topIssues"][0]["category"], "component_quality")
        self.assertEqual(report["afterRetries"]["reliableBodyDelivered"]["passed"], 1)

    def test_missing_model_flags_are_unknown_and_fallbacks_are_separate(self):
        report = summarize([event(reliableBodyDelivered=True), event("fallback", snapshotFallback=True, reliableBodyDelivered=True)])
        metric = report["afterRetries"]["modelGenerationSucceeded"]
        self.assertEqual(metric["total"], 1)
        self.assertEqual(metric["observed"], 0)
        self.assertEqual(metric["missing"], 1)
        self.assertIsNone(metric["rate"])
        self.assertEqual(report["snapshotFallbackCount"], 1)

    def test_summary_never_exposes_chat_prose_or_raw_identity(self):
        report = summarize([event(kind="message_generation_failed", code="MODEL_RESPONSE_INVALID",
            safeReason="private designer prose", content="private designer prose")])
        encoded = json.dumps(report)
        self.assertNotIn("private designer prose", encoded)
        self.assertNotIn("private-session", encoded)
        self.assertEqual(len(report["topIssues"][0]["examples"][0]["requestRef"]), 12)

    def test_missing_delivery_flag_is_unknown_not_failed_recovery(self):
        report = summarize([event(kind="message_generation_failed", code="UPSTREAM_TIMEOUT"), event()])
        self.assertEqual(report["topIssues"][0]["withoutBodyAtEnd"], 0)
        self.assertEqual(report["topIssues"][0]["bodyUnknownAtEnd"], 1)
        self.assertEqual(report["afterRetries"]["reliableBodyDelivered"]["missing"], 1)

    def test_latency_excludes_missing_invalid_and_boolean_values(self):
        report = summarize([event(str(i), latencyMs=value) for i, value in enumerate((10, 20, 100, None, -1, True, float("nan")))])
        self.assertEqual(report["latency"]["samples"], 3)
        self.assertEqual(report["latency"]["missing"], 4)
        self.assertEqual(report["latency"]["p50Ms"], 20)
        self.assertEqual(report["latency"]["p95Ms"], 100)

    def test_read_only_window_handles_offsets_exclusive_end_and_bad_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.sqlite3"
            with closing(sqlite3.connect(path)) as database, database:
                database.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, session_id TEXT, event_type TEXT, payload_json TEXT, created_at TEXT)")
                for stamp, payload in (("2026-10-06T00:00:00Z", "{}"),
                        ("2026-10-06T08:30:00+08:00", '{"messageKey":"m"}'),
                        ("2026-10-06T01:00:00Z", "{}"), ("2026-10-06T00:45:00Z", "broken")):
                    database.execute("INSERT INTO audit_events VALUES (NULL,?,?,?,?)", ("s", "message_generation_failed", payload, stamp))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            events = read_events(path, utc_timestamp("2026-10-06T08:15:00+08:00"), utc_timestamp("2026-10-06T01:00:00Z"))
            self.assertEqual(len(events), 2)
            self.assertEqual(summarize(events)["skippedRecords"]["missingMessageKeyOrPayload"], 1)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(sqlite3.OperationalError):
                read_events(Path(directory) / "missing.sqlite3", "2026-10-06T00:00:00Z")
            self.assertFalse((Path(directory) / "missing.sqlite3").exists())

    def test_empty_text_report_does_not_claim_no_failures(self):
        report = summarize([])
        self.assertIn("不代表系统没有失败", render_text(report))
        self.assertIsNone(report["verifiedProposalRate"]["rate"])


if __name__ == "__main__":
    unittest.main()
