"""Comprehensive TDD unit and integration tests for server/tools/reliability_report.py (#8)."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from server.core.llm_client import UpstreamLLMError
from server.persistence.database import SQLiteDatabase, dump_json
from server.tools import reliability_report as rr
from tests.conftest import (
    BASE_ROWS,
    extract_hash_param,
    make_binary_ask_json,
    make_binary_plan_json,
    make_chat_json,
    make_map_ops_json,
    make_routing_json,
)


def _extract_turn_id(kwargs: dict[str, Any], *, index: int = -1) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("designerTurns") or payload.get("initialDesignerTurns") or []
    return str(turns[index]["id"]) if turns else "turn-hint"


def _make_water_req_json(kwargs: dict[str, Any]) -> str:
    tid = _extract_turn_id(kwargs, index=-1)
    return json.dumps(
        {
            "requirements": [
                {
                    "component": "water",
                    "property": "count",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "",
                    "focused": False,
                    "sourceTurnId": tid,
                    "evidenceSpan": "remove water",
                    "statement": "Remove a water tile",
                    "scope": "all",
                }
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
    )


def _make_water_plan_json() -> str:
    return json.dumps(
        {
            "strategies": [
                {
                    "effect": "reshape_water",
                    "focus": {"row": 5, "column": 6, "radius": 1},
                    "operators": ["remove_water"],
                    "preserve": ["boxes", "targets", "player"],
                    "editBudget": 1,
                }
            ]
        }
    )


def _make_water_ops_json() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove water tile",
                    "operations": [
                        {"row": 5, "column": 6, "op": "remove_water", "from": "@", "to": "."}
                    ],
                }
            ]
        }
    )


def _make_review_reason_json() -> str:
    return json.dumps(
        {
            "relation": "different",
            "merit": "reasonable",
            "comparison": "Keeping the wall preserves structure while water removal opens a path.",
        }
    )


async def _bootstrap_stage1(client: Any) -> tuple[str, str]:
    created = (
        await client.post(
            "/api/sessions",
            json={
                "rows": BASE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": f"rr-sess-{uuid.uuid4().hex[:8]}",
            },
        )
    ).json()
    sid = created["sessionId"]
    auth = await client.post(
        f"/api/sessions/{sid}/browser-access",
        json={"bootstrapToken": extract_hash_param(created["launchUrl"], "bootstrap")},
    )
    assert auth.status_code == 200
    locked = (
        await client.patch(f"/api/sessions/{sid}/language", json={"language": "en"})
    ).json()
    return sid, locked["currentVersionId"]


def test_task_normalization_merges_failure_and_retry_across_all_dialogue_tasks() -> None:
    dialogue_tasks = [
        "message",
        "ordinary_chat",
        "binary_discovery",
        "binary_choice",
        "challenge",
        "challenge_review",
        "proposal_clarification",
        "revision_card_execute",
        "alternative_revision",
        "llm_operations",
        "deterministic_beam_search",
    ]
    events: list[dict[str, Any]] = []
    for idx, task_name in enumerate(dialogue_tasks):
        mk = f"mk-{idx}"
        events.extend(
            [
                {
                    "sessionId": "s1",
                    "eventType": "message_generation_failed",
                    "createdAt": f"2026-01-01T00:0{idx % 10}:00Z",
                    "payload": {
                        "messageKey": mk,
                        "requestId": f"req-fail-{idx}",
                        "code": "UPSTREAM_TIMEOUT",
                        "details": {"attemptsUsed": 1},
                    },
                },
                {
                    "sessionId": "s1",
                    "eventType": "reply_generation_failed",
                    "createdAt": f"2026-01-01T00:0{idx % 10}:00Z",
                    "payload": {
                        "messageKey": mk,
                        "requestId": f"req-fail-{idx}",
                        "task": "message",
                        "code": "UPSTREAM_TIMEOUT",
                        "reliableBodyDelivered": False,
                        "details": {"attemptsUsed": 1},
                    },
                },
                {
                    "sessionId": "s1",
                    "eventType": "reply_delivery_outcome",
                    "createdAt": f"2026-01-01T00:0{idx % 10}:10Z",
                    "payload": {
                        "messageKey": mk,
                        "requestId": f"req-ok-{idx}",
                        "task": task_name,
                        "modelGenerationSucceeded": True,
                        "reliableBodyDelivered": True,
                        "fullQualityPassed": True,
                        "attemptsUsed": 1,
                        "latencyMs": 120,
                    },
                },
            ]
        )

    # Non-dialogue tasks stay in their own task buckets and still merge failure+retry
    events.extend(
        [
            {
                "sessionId": "s1",
                "eventType": "reply_generation_failed",
                "createdAt": "2026-01-01T01:00:00Z",
                "payload": {
                    "messageKey": "open-1",
                    "requestId": "req-open-fail",
                    "task": "stage_opening",
                    "code": "UPSTREAM_RATE_LIMIT",
                    "attemptsUsed": 2,
                    "reliableBodyDelivered": False,
                },
            },
            {
                "sessionId": "s1",
                "eventType": "reply_delivery_outcome",
                "createdAt": "2026-01-01T01:00:05Z",
                "payload": {
                    "messageKey": "open-1",
                    "requestId": "req-open-ok",
                    "task": "stage_opening",
                    "modelGenerationSucceeded": True,
                    "reliableBodyDelivered": True,
                    "fullQualityPassed": True,
                    "attemptsUsed": 1,
                    "latencyMs": 80,
                },
            },
        ]
    )

    report = rr.summarize(events)
    expected_total = len(dialogue_tasks) + 1
    assert report["logicalRequests"] == expected_total
    assert report["retriedRequests"] == expected_total
    assert report["recordedOutcomes"] == expected_total * 2
    assert report["firstAttempt"]["reliableBodyDelivered"]["passed"] == 0
    assert report["afterRetries"]["reliableBodyDelivered"]["passed"] == expected_total
    assert report["afterRetries"]["reliableBodyDelivered"]["rate"] == 1.0
    assert set(report["byTask"].keys()) == {"message", "stage_opening"}
    assert report["byTask"]["message"]["logicalRequests"] == len(dialogue_tasks)
    assert report["byTask"]["stage_opening"]["logicalRequests"] == 1
    timeout_group = next(g for g in report["failureGroups"] if g["category"] == "upstream_timeout")
    assert timeout_group["affectedRequests"] == len(dialogue_tasks)
    assert timeout_group["withoutBodyAtEnd"] == 0
    assert timeout_group["bodyDeliveredAfterIssue"] == len(dialogue_tasks)


def test_single_failure_paired_events_not_counted_as_retry_but_real_retry_is() -> None:
    # 1. Single HTTP failure emitting both message_generation_failed + reply_generation_failed
    single_fail = [
        {
            "sessionId": "s1",
            "eventType": "message_generation_failed",
            "createdAt": "2026-01-01T00:00:00Z",
            "payload": {
                "messageKey": "mk-single",
                "requestId": "req-1",
                "code": "UPSTREAM_ERROR",
                "details": {"attemptsUsed": 2},
            },
        },
        {
            "sessionId": "s1",
            "eventType": "reply_generation_failed",
            "createdAt": "2026-01-01T00:00:00Z",
            "payload": {
                "messageKey": "mk-single",
                "requestId": "req-1",
                "task": "message",
                "code": "UPSTREAM_ERROR",
                "reliableBodyDelivered": False,
                "details": {"attemptsUsed": 2},
            },
        },
    ]
    r_single = rr.summarize(single_fail)
    assert r_single["logicalRequests"] == 1
    assert r_single["retriedRequests"] == 0
    assert r_single["recordedOutcomes"] == 1
    assert r_single["pipelineDiagnostics"]["recordedModelAttempts"] == 2
    assert r_single["pipelineDiagnostics"]["httpAttempts"] == 2

    # 2. Challenge reason review failure emits 3 paired failure events for 1 attempt
    triple_fail = [
        {
            "sessionId": "s1",
            "eventType": "challenge_reason_review_pending",
            "createdAt": "2026-01-01T00:01:00Z",
            "payload": {
                "challengeId": "chg-1",
                "messageKey": "mk-chg",
                "requestId": "req-chg-1",
                "code": "UPSTREAM_ERROR",
                "details": {"attemptsUsed": 1},
            },
        },
        {
            "sessionId": "s1",
            "eventType": "message_generation_failed",
            "createdAt": "2026-01-01T00:01:01Z",
            "payload": {
                "messageKey": "mk-chg",
                "requestId": "req-chg-1",
                "code": "UPSTREAM_ERROR",
                "details": {"attemptsUsed": 1},
            },
        },
        {
            "sessionId": "s1",
            "eventType": "reply_generation_failed",
            "createdAt": "2026-01-01T00:01:01Z",
            "payload": {
                "messageKey": "mk-chg",
                "requestId": "req-chg-1",
                "task": "message",
                "code": "UPSTREAM_ERROR",
                "reliableBodyDelivered": False,
                "details": {"attemptsUsed": 1},
            },
        },
    ]
    r_triple = rr.summarize(triple_fail)
    assert r_triple["logicalRequests"] == 1
    assert r_triple["retriedRequests"] == 0
    assert r_triple["recordedOutcomes"] == 1

    # 3. Real user retry (even with fallback requestId == messageKey and same timestamp) IS a retry
    double_attempt_fail = [
        {
            "sessionId": "s1",
            "eventType": "message_generation_failed",
            "createdAt": "2026-01-01T00:02:00Z",
            "payload": {
                "messageKey": "mk-retry",
                "requestId": "mk-retry",
                "code": "UPSTREAM_ERROR",
                "details": {"attemptsUsed": 1},
            },
        },
        {
            "sessionId": "s1",
            "eventType": "reply_generation_failed",
            "createdAt": "2026-01-01T00:02:00Z",
            "payload": {
                "messageKey": "mk-retry",
                "requestId": "mk-retry",
                "task": "message",
                "code": "UPSTREAM_ERROR",
                "reliableBodyDelivered": False,
                "details": {"attemptsUsed": 1},
            },
        },
        {
            "sessionId": "s1",
            "eventType": "message_generation_failed",
            "createdAt": "2026-01-01T00:02:00Z",
            "payload": {
                "messageKey": "mk-retry",
                "requestId": "mk-retry",
                "code": "UPSTREAM_ERROR",
                "details": {"attemptsUsed": 1},
            },
        },
        {
            "sessionId": "s1",
            "eventType": "reply_generation_failed",
            "createdAt": "2026-01-01T00:02:00Z",
            "payload": {
                "messageKey": "mk-retry",
                "requestId": "mk-retry",
                "task": "message",
                "code": "UPSTREAM_ERROR",
                "reliableBodyDelivered": False,
                "details": {"attemptsUsed": 1},
            },
        },
    ]
    r_retry = rr.summarize(double_attempt_fail)
    assert r_retry["logicalRequests"] == 1
    assert r_retry["retriedRequests"] == 1
    assert r_retry["recordedOutcomes"] == 2
    assert r_retry["pipelineDiagnostics"]["recordedModelAttempts"] == 2


@pytest.mark.asyncio
async def test_deterministic_refusal_not_counted_as_llm_failure(app_bundle_factory) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        sid, v1 = await _bootstrap_stage1(client)
        # 1. Protected refusal via routing -> deterministic receipt in ordinary.py
        bundle.cocreation_transport.queue(
            json.dumps(
                {
                    "acts": ["revision_request"],
                    "elements": ["player"],
                    "mapRelated": True,
                    "directionSufficient": True,
                    "routeTarget": "ordinary",
                    "evidenceSpan": "move the player",
                    "changes": [
                        {
                            "component": "player",
                            "property": "position",
                            "operation": "change",
                            "evidenceSpan": "move the player",
                        }
                    ],
                }
            ),
            # 2. Normal LLM chat turn
            make_routing_json("ordinary", evidence_span="center"),
            make_chat_json("Center route looks clear."),
        )
        r1 = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Please move the player to the top left",
                "baseVersionId": v1,
                "idempotencyKey": "det-refusal-1",
            },
        )
        assert r1.status_code == 200, r1.text
        r2 = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "How does the center feel?",
                "baseVersionId": v1,
                "idempotencyKey": "llm-chat-1",
            },
        )
        assert r2.status_code == 200, r2.text

    with bundle.container.db.read_conn() as conn:
        outcomes = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT payload_json FROM audit_events "
                "WHERE session_id = ? AND event_type = 'reply_delivery_outcome' ORDER BY id",
                (sid,),
            ).fetchall()
        ]
    assert len(outcomes) == 2
    refusal_outcome, chat_outcome = outcomes
    assert refusal_outcome["deterministicReceipt"] is True
    assert refusal_outcome["attemptsUsed"] == 0
    assert chat_outcome["deterministicReceipt"] is False
    assert chat_outcome["attemptsUsed"] >= 1

    events = rr.read_events(bundle.settings.db_path, "2020-01-01T00:00:00Z")
    report = rr.summarize(events)
    assert report["logicalRequests"] == 2
    # Deterministic refusal excluded -> eligible LLM count is 1 and pass rate is 100%
    assert report["afterRetries"]["modelGenerationSucceeded"]["total"] == 1
    assert report["afterRetries"]["modelGenerationSucceeded"]["passed"] == 1
    assert report["afterRetries"]["modelGenerationSucceeded"]["rate"] == 1.0


@pytest.mark.asyncio
async def test_verified_proposal_rate_and_attempts_across_binary_revision_and_challenge(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        sid, v1 = await _bootstrap_stage1(client)

        # A. Binary discovery: ask -> choose -> verified offer
        bundle.cocreation_transport.queue(
            make_binary_ask_json,
            make_binary_plan_json(),
            make_map_ops_json(),
        )
        asked = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Make center simpler",
                "baseVersionId": v1,
                "idempotencyKey": "prop-bin-ask",
                "requestProposal": True,
            },
        )
        assert asked.status_code == 200, asked.text
        pq = next(
            t["guidance"]["proposalQuestion"]
            for t in reversed(asked.json()["turns"])
            if t["guidance"].get("proposalQuestion")
        )
        planned = await client.post(
            f"/api/sessions/{sid}/proposal-discovery/choices",
            json={
                "topicId": pq["topicId"],
                "questionId": pq["questionId"],
                "choiceId": pq["choices"][0]["choiceId"],
                "baseVersionId": v1,
                "idempotencyKey": "prop-bin-choose",
            },
        )
        assert planned.status_code == 200, planned.text
        offer_turn = planned.json()["turns"][-1]

        # B. Execute revision (deterministic verified proposal) -> reject so we can test alt/chg
        executed = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Execute revision",
                "baseVersionId": v1,
                "idempotencyKey": "prop-exec-1",
                "action": "execute_revision",
                "sourceTurnId": offer_turn["turnId"],
            },
        )
        assert executed.status_code == 200, executed.text
        pending_pid = next(
            p["proposalId"] for p in executed.json()["proposals"] if p["status"] == "pending"
        )
        rej = await client.post(
            f"/api/sessions/{sid}/proposals/{pending_pid}/decision",
            json={
                "decision": "reject",
                "baseVersionId": v1,
                "idempotencyKey": "prop-rej-1",
            },
        )
        assert rej.status_code == 200, rej.text

        # C. Alternative revision (LLM proposal offer excluding prior wall edit)
        bundle.cocreation_transport.queue(
            _make_water_req_json,
            _make_water_plan_json(),
            _make_water_ops_json(),
        )
        alt = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Try to remove water instead",
                "baseVersionId": v1,
                "idempotencyKey": "prop-alt-1",
                "action": "alternative_revision",
            },
        )
        assert alt.status_code == 200, alt.text
        alt_turn = alt.json()["turns"][-1]

        # D. Challenge flow: start -> reason review -> keep_proposal (deterministic)
        started_chg = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Challenge this plan",
                "baseVersionId": v1,
                "idempotencyKey": "prop-chg-start",
                "action": "challenge_revision",
                "sourceTurnId": alt_turn["turnId"],
            },
        )
        assert started_chg.status_code == 200, started_chg.text
        chg_id = started_chg.json()["turns"][-1]["guidance"]["challengeState"]["challengeId"]

        bundle.cocreation_transport.queue(_make_review_reason_json())
        reviewed = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Water keeps the lower loop interesting.",
                "baseVersionId": v1,
                "idempotencyKey": "prop-chg-reason",
                "action": "continue_challenge",
                "challengeId": chg_id,
            },
        )
        assert reviewed.status_code == 200, reviewed.text

        kept = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Yes",
                "baseVersionId": v1,
                "idempotencyKey": "prop-chg-keep",
                "action": "continue_challenge",
                "challengeId": chg_id,
                "challengeChoice": "ai",
            },
        )
        assert kept.status_code == 200, kept.text

    with bundle.container.db.read_conn() as conn:
        outcomes = {
            json.loads(r[0])["messageKey"]: json.loads(r[0])
            for r in conn.execute(
                "SELECT payload_json FROM audit_events "
                "WHERE session_id = ? AND event_type = 'reply_delivery_outcome'",
                (sid,),
            ).fetchall()
        }

    # Every dialogue reply_delivery_outcome carries integer attemptsUsed
    for mk, payload in outcomes.items():
        assert isinstance(payload.get("attemptsUsed"), int), f"{mk} missing attemptsUsed"

    assert outcomes["prop-alt-1"]["proposalRequested"] is True
    assert outcomes["prop-alt-1"]["verifiedProposal"] is True
    assert outcomes["prop-alt-1"]["attemptsUsed"] >= 1

    assert outcomes["prop-exec-1"]["proposalRequested"] is True
    assert outcomes["prop-exec-1"]["verifiedProposal"] is True
    assert outcomes["prop-exec-1"]["deterministicReceipt"] is True
    assert outcomes["prop-exec-1"]["attemptsUsed"] == 0

    assert outcomes["prop-chg-reason"]["proposalRequested"] is False
    assert outcomes["prop-chg-reason"]["attemptsUsed"] >= 1

    assert outcomes["prop-chg-keep"]["proposalRequested"] is True
    assert outcomes["prop-chg-keep"]["verifiedProposal"] is True
    assert outcomes["prop-chg-keep"]["deterministicReceipt"] is True
    assert outcomes["prop-chg-keep"]["attemptsUsed"] == 0

    report = rr.summarize(rr.read_events(bundle.settings.db_path, "2020-01-01T00:00:00Z"))
    # Proposal requests: bin-ask (ask), bin-choose (verified), exec-1 (verified),
    # alt-1 (verified), chg-keep (verified) -> 4/5
    assert report["verifiedProposalRate"]["total"] == 5
    assert report["verifiedProposalRate"]["passed"] == 4
    assert report["verifiedProposalCount"] == 4
    assert report["pipelineDiagnostics"]["recordedModelAttempts"] > 0
    assert report["pipelineDiagnostics"]["httpAttempts"] > 0


def test_component_recovery_tasks_time_window_created_inside_updated_later(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "rec_window.sqlite3"
    db = SQLiteDatabase(db_path)
    db.initialize()
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO design_sessions("
            "session_id, mode, language, status, access_token_hash, integration_token_hash, "
            "bootstrap_token_hash, created_at, updated_at"
            ") VALUES ('s1', 'partial_completion', 'en', 'active', 'h1', 'h2', 'h3', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        rows = [
            # Created inside [Jan 1, Jan 2), updated AFTER Jan 2 -> MUST MATCH
            (
                "rec-created-inside",
                "s1",
                "human_edit_engagement",
                "d1",
                dump_json(
                    {
                        "component": "human_edit_engagement",
                        "status": "completed",
                        "attemptsUsed": 1,
                        "createdAt": "2026-01-01T12:00:00Z",
                    }
                ),
                "2026-01-03T08:00:00Z",
            ),
            # Created BEFORE Jan 1, updated INSIDE [Jan 1, Jan 2) -> MUST MATCH
            (
                "rec-updated-inside",
                "s1",
                "human_edit_engagement",
                "d2",
                dump_json(
                    {
                        "component": "human_edit_engagement",
                        "status": "failed",
                        "attemptsUsed": 2,
                        "createdAt": "2025-12-31T20:00:00Z",
                    }
                ),
                "2026-01-01T09:00:00Z",
            ),
            # Created & updated BEFORE Jan 1 -> EXCLUDED
            (
                "rec-before",
                "s1",
                "human_edit_engagement",
                "d3",
                dump_json(
                    {
                        "component": "human_edit_engagement",
                        "status": "completed",
                        "attemptsUsed": 1,
                        "createdAt": "2025-12-30T00:00:00Z",
                    }
                ),
                "2025-12-31T23:59:59Z",
            ),
            # Created & updated AFTER Jan 2 -> EXCLUDED
            (
                "rec-after",
                "s1",
                "human_edit_engagement",
                "d4",
                dump_json(
                    {
                        "component": "human_edit_engagement",
                        "status": "retry_pending",
                        "attemptsUsed": 0,
                        "createdAt": "2026-01-02T00:00:00Z",
                    }
                ),
                "2026-01-02T05:00:00Z",
            ),
        ]
        conn.executemany(
            "INSERT INTO component_recovery_tasks("
            "recovery_id, session_id, target_kind, target_id, state_json, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    events = rr.read_events(db_path, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    report = rr.summarize(events)
    assert report["stateRecoveries"]["tasks"] == 2
    assert report["stateRecoveries"]["attemptsUsed"] == 3
    assert report["stateRecoveries"]["byStatus"] == {"completed": 1, "failed": 1}


@pytest.mark.asyncio
async def test_ordinary_recovery_task_persists_created_at_for_window_reporting(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        sid, v1 = await _bootstrap_stage1(client)
        # Seed active human_edit disagreement on v1
        with bundle.container.db.write_tx() as conn:
            conn.execute(
                "UPDATE level_versions SET design_context_json = ? "
                "WHERE session_id = ? AND version_id = ?",
                (
                    dump_json(
                        {
                            "updatedFromStageId": v1,
                            "activeDisagreement": {
                                "status": "active",
                                "subject": "human_edit",
                                "disagreementId": "dis-win-1",
                                "userPosition": "Edit",
                                "aiPosition": "AI",
                                "coreDisagreement": "Diff",
                                "nextQuestion": "Why?",
                            },
                        }
                    ),
                    sid,
                    v1,
                ),
            )
        # Routing fails twice (retry budget exhausted), chat succeeds -> creates recovery task
        bundle.cocreation_transport.queue(
            UpstreamLLMError("UPSTREAM_ERROR", "transient 1", retryable=True),
            UpstreamLLMError("UPSTREAM_ERROR", "transient 2", retryable=True),
            make_chat_json("Got your manual edit note."),
        )
        res = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Let us talk about my manual edit",
                "baseVersionId": v1,
                "idempotencyKey": "rec-win-msg",
            },
        )
        assert res.status_code == 200, res.text
        created_ts = bundle.clock.utc_now()

        # Advance clock past window end and update task updated_at
        window_until = "2026-01-01T01:00:00Z"
        bundle.clock.advance(7200.0)
        later_ts = bundle.clock.utc_now()
        with bundle.container.db.write_tx() as conn:
            row = conn.execute(
                "SELECT recovery_id, state_json FROM component_recovery_tasks "
                "WHERE session_id = ?",
                (sid,),
            ).fetchone()
            assert row is not None
            state = json.loads(row["state_json"])
            assert state.get("createdAt") == created_ts
            state["status"] = "completed"
            state["attemptsUsed"] = 1
            conn.execute(
                "UPDATE component_recovery_tasks SET state_json = ?, updated_at = ? "
                "WHERE recovery_id = ?",
                (dump_json(state), later_ts, row["recovery_id"]),
            )

        events = rr.read_events(bundle.settings.db_path, "2020-01-01T00:00:00Z", window_until)
        report = rr.summarize(events)
        assert report["stateRecoveries"]["tasks"] == 1
        assert report["stateRecoveries"]["byStatus"] == {"completed": 1}


def test_cli_json_and_text_formatting_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "cli_report.sqlite3"
    db = SQLiteDatabase(db_path)
    db.initialize()
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "s1",
                "reply_delivery_outcome",
                dump_json(
                    {
                        "messageKey": "m1",
                        "requestId": "r1",
                        "task": "ordinary_chat",
                        "modelGenerationSucceeded": True,
                        "reliableBodyDelivered": True,
                        "fullQualityPassed": True,
                        "proposalRequested": True,
                        "verifiedProposal": True,
                        "attemptsUsed": 1,
                        "latencyMs": 250,
                    }
                ),
                "2026-05-01T10:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "s1",
                "reply_pipeline_diagnostic",
                dump_json({"requestId": "legacy-r1", "attempts": 1}),
                "2026-05-01T10:01:00Z",
            ),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reliability_report",
            str(db_path),
            "--since",
            "2026-05-01T00:00:00Z",
            "--until",
            "2026-05-02T00:00:00Z",
            "--format",
            "json",
        ],
    )
    rr.main()
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["logicalRequests"] == 1
    assert json_out["retriedRequests"] == 0
    assert json_out["verifiedProposalRate"] == {"passed": 1, "total": 1, "rate": 1.0}
    assert json_out["window"] == {
        "since": "2026-05-01T00:00:00Z",
        "until": "2026-05-02T00:00:00Z",
    }
    assert json_out["pipelineDiagnostics"]["legacyDiagnosticEvents"] == 1
    assert json_out["pipelineDiagnostics"]["recordedModelAttempts"] == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reliability_report",
            str(db_path),
            "--since",
            "2026-05-01T00:00:00Z",
            "--format",
            "text",
        ],
    )
    rr.main()
    text_out = capsys.readouterr().out
    assert "逻辑请求：1" in text_out
    assert "验证提案：1/1" in text_out
    assert "250.0/250.0" in text_out

    # Invalid window (--until <= --since) fails cleanly with SystemExit(2)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reliability_report",
            str(db_path),
            "--since",
            "2026-05-02T00:00:00Z",
            "--until",
            "2026-05-01T00:00:00Z",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        rr.main()
    assert exc_info.value.code == 2
