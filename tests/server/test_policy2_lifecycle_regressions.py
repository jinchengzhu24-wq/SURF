"""TDD contract regression tests for Policy-2 purple card lifecycle (#1) & stage artifacts (#12)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from server.core.grid import map_fingerprint
from server.persistence.database import dump_json
from tests.conftest import (
    BASE_ROWS,
    extract_hash_param,
    make_binary_ask_json,
    make_binary_plan_json,
    make_map_ops_json,
)


def _extract_turn_id(kwargs: dict[str, Any], *, index: int = -1) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("designerTurns") or payload.get("initialDesignerTurns") or []
    return str(turns[index]["id"]) if turns else "turn-hint"


def _make_req_json(
    component: str,
    evidence_span: str,
    statement: str,
) -> Any:
    def _builder(kwargs: dict[str, Any]) -> str:
        tid = _extract_turn_id(kwargs, index=-1)
        return json.dumps(
            {
                "requirements": [
                    {
                        "component": component,
                        "property": "count",
                        "relation": "decrease",
                        "strength": "goal",
                        "entities": [],
                        "value": None,
                        "unit": "none",
                        "priorityEvidenceSpan": "",
                        "focused": False,
                        "sourceTurnId": tid,
                        "evidenceSpan": evidence_span,
                        "statement": statement,
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            }
        )

    return _builder


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
            "comparison": (
                "Keeping the central wall preserves structure while water removal opens space."
            ),
        }
    )


async def _bootstrap_formal_stage1(client: Any) -> tuple[str, str]:
    created = (
        await client.post(
            "/api/sessions",
            json={
                "rows": BASE_ROWS,
                "initialDraftMethod": "partial_completion",
                "language": "en",
                "idempotencyKey": f"p2-sess-{uuid.uuid4().hex[:8]}",
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


async def _run_binary_to_offer(
    bundle: Any,
    client: Any,
    sid: str,
    vid: str,
    key_prefix: str,
) -> dict[str, Any]:
    bundle.cocreation_transport.queue(
        make_binary_ask_json,
        make_binary_plan_json(),
        make_map_ops_json(),
    )
    asked = (
        await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Make center simpler",
                "baseVersionId": vid,
                "idempotencyKey": f"{key_prefix}-ask",
                "requestProposal": True,
                "action": "send",
            },
        )
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
            "baseVersionId": vid,
            "idempotencyKey": f"{key_prefix}-choose",
        },
    )
    assert planned.status_code == 200, planned.text
    return planned.json()


@pytest.mark.asyncio
async def test_binary_offer_no_premature_proposal_execute_and_accept_lifecycle(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        sid, v1 = await _bootstrap_formal_stage1(client)
        planned = await _run_binary_to_offer(bundle, client, sid, v1, "bin1")

        # 1. Offer turn has purple proposalOffer card + active proposalState, ZERO change_proposals
        offer_turn = planned["turns"][-1]
        assert offer_turn["guidance"]["move"] == "offer_revision"
        assert isinstance(offer_turn["guidance"].get("proposalOffer"), dict)
        assert offer_turn["proposalState"]["status"] == "active"
        assert offer_turn["proposalState"]["actionable"] is True
        assert planned["proposals"] == []
        with bundle.container.db.read_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM change_proposals WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        assert count == 0

        # 2. Playtesting is NOT blocked by an unexecuted purple offer
        pa = await client.post(
            f"/api/sessions/{sid}/versions/{v1}/play-attempts",
            json={"idempotencyKey": "pa-offer-allowed"},
        )
        assert pa.status_code == 200, pa.text

        # 3. execute_revision creates pending proposal, omits proposalOffer, marks offer stale
        executed_resp = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Draft this suggestion",
                "baseVersionId": v1,
                "idempotencyKey": "exec-1",
                "action": "execute_revision",
                "sourceTurnId": offer_turn["turnId"],
            },
        )
        assert executed_resp.status_code == 200, executed_resp.text
        executed = executed_resp.json()
        exec_turn = executed["turns"][-1]
        assert not exec_turn["guidance"].get("proposalOffer")
        assert any(
            cue.get("type") == "manual_edit" for cue in (exec_turn["guidance"].get("uiCues") or [])
        )
        pending_props = [p for p in executed["proposals"] if p["status"] == "pending"]
        assert len(pending_props) == 1
        prop = pending_props[0]
        assert prop["assistantTurnId"] == exec_turn["turnId"]

        refreshed_offer = next(t for t in executed["turns"] if t["turnId"] == offer_turn["turnId"])
        assert refreshed_offer["proposalState"]["status"] == "stale"
        assert refreshed_offer["proposalState"]["actionable"] is False
        with bundle.container.db.read_conn() as conn:
            raw_binding = conn.execute(
                "SELECT proposal_binding_json FROM conversation_turns WHERE turn_id = ?",
                (offer_turn["turnId"],),
            ).fetchone()[0]
            prop_json = json.loads(
                conn.execute(
                    "SELECT proposal_json FROM change_proposals WHERE proposal_id = ?",
                    (prop["proposalId"],),
                ).fetchone()[0]
            )
        parsed_binding = json.loads(raw_binding)
        assert parsed_binding["status"] == "stale"
        assert isinstance(parsed_binding.get("requirementRecord"), dict)
        assert isinstance(parsed_binding.get("revisionPlan"), dict)
        assert prop_json["requirementRecord"] == parsed_binding["requirementRecord"]
        assert prop_json["revisionPlan"] == parsed_binding["revisionPlan"]

        # 4. Re-executing the consumed offer turn is blocked (no infinite execute loop)
        re_exec = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Draft again",
                "baseVersionId": v1,
                "idempotencyKey": "exec-repeat",
                "action": "execute_revision",
                "sourceTurnId": offer_turn["turnId"],
            },
        )
        assert re_exec.status_code == 409
        assert re_exec.json()["error"]["code"] == "PROPOSAL_STALE"

        # 5. Seed a sibling active binary topic + sibling pending proposal to verify #12 on accept
        now_iso = bundle.clock.utc_now()
        with bundle.container.db.write_tx() as conn:
            conn.execute(
                "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    sid,
                    "topic-sibling-active",
                    dump_json(
                        {
                            "topicId": "topic-sibling-active",
                            "versionId": v1,
                            "status": "clarifying",
                            "retryAction": {"kind": "choice"},
                        }
                    ),
                    now_iso,
                ),
            )
            conn.execute(
                "INSERT INTO change_proposals("
                "proposal_id, session_id, base_version_id, assistant_turn_id, status, "
                "proposal_json, created_at"
                ") VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    "prop-sibling-pending",
                    sid,
                    v1,
                    offer_turn["turnId"],
                    dump_json({"proposedRows": BASE_ROWS, "summary": "Sibling"}),
                    now_iso,
                ),
            )

        accepted_resp = await client.post(
            f"/api/sessions/{sid}/proposals/{prop['proposalId']}/decision",
            json={
                "decision": "accept",
                "baseVersionId": v1,
                "idempotencyKey": "acc-1",
                "reason": "Looks cleaner",
            },
        )
        assert accepted_resp.status_code == 200, accepted_resp.text
        v2 = accepted_resp.json()["currentVersionId"]
        assert v2 != v1

        with bundle.container.db.read_conn() as conn:
            acc_row = conn.execute(
                "SELECT status, candidate_version_id FROM change_proposals WHERE proposal_id = ?",
                (prop["proposalId"],),
            ).fetchone()
            sib_prop = conn.execute(
                "SELECT status FROM change_proposals WHERE proposal_id = ?",
                ("prop-sibling-pending",),
            ).fetchone()
            sib_topic = conn.execute(
                "SELECT state_json FROM binary_discovery_topics "
                "WHERE session_id = ? AND topic_id = ?",
                (sid, "topic-sibling-active"),
            ).fetchone()
        assert acc_row["status"] == "accepted"
        assert acc_row["candidate_version_id"] == v2
        assert sib_prop["status"] == "superseded"
        assert json.loads(sib_topic["state_json"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_challenge_directly_from_offer_turn_keep_proposal_and_revise_proposal_flows(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        # Flow A: Challenge offer turn directly (no change_proposals) -> reason -> keep ("ai")
        sid_a, v1_a = await _bootstrap_formal_stage1(client)
        planned_a = await _run_binary_to_offer(bundle, client, sid_a, v1_a, "chgA")
        offer_a = planned_a["turns"][-1]
        assert planned_a["proposals"] == []

        started_a = await client.post(
            f"/api/sessions/{sid_a}/messages",
            json={
                "content": "Challenge this plan",
                "baseVersionId": v1_a,
                "idempotencyKey": "chgA-start",
                "action": "challenge_revision",
                "sourceTurnId": offer_a["turnId"],
            },
        )
        assert started_a.status_code == 200, started_a.text
        chg_a_id = started_a.json()["turns"][-1]["guidance"]["challengeState"]["challengeId"]
        assert started_a.json()["proposals"] == []

        # Early revise_proposal before reason review must raise 409 CHALLENGE_NOT_READY
        early_revise = await client.post(
            f"/api/sessions/{sid_a}/messages",
            json={
                "content": "No",
                "baseVersionId": v1_a,
                "idempotencyKey": "chgA-early-revise",
                "action": "continue_challenge",
                "challengeId": chg_a_id,
                "challengeChoice": "user",
            },
        )
        assert early_revise.status_code == 409
        assert early_revise.json()["error"]["code"] == "CHALLENGE_NOT_READY"

        bundle.cocreation_transport.queue(_make_review_reason_json())
        reviewed_a = await client.post(
            f"/api/sessions/{sid_a}/messages",
            json={
                "content": "I think the wall helps guide the push.",
                "baseVersionId": v1_a,
                "idempotencyKey": "chgA-reason",
                "action": "continue_challenge",
                "challengeId": chg_a_id,
            },
        )
        assert reviewed_a.status_code == 200, reviewed_a.text
        assert (
            reviewed_a.json()["turns"][-1]["guidance"]["disagreement"]["phase"]
            == "choice_pending"
        )
        assert reviewed_a.json()["proposals"] == []

        # Choosing "ai" (Yes) executes original offer into a pending proposal & marks source stale
        kept_a_resp = await client.post(
            f"/api/sessions/{sid_a}/messages",
            json={
                "content": "Yes",
                "baseVersionId": v1_a,
                "idempotencyKey": "chgA-keep",
                "action": "continue_challenge",
                "challengeId": chg_a_id,
                "challengeChoice": "ai",
            },
        )
        assert kept_a_resp.status_code == 200, kept_a_resp.text
        kept_a = kept_a_resp.json()
        keep_turn = kept_a["turns"][-1]
        assert not keep_turn["guidance"].get("proposalOffer")
        assert keep_turn["guidance"]["disagreement"]["status"] == "resolved"
        assert any(
            cue.get("type") == "manual_edit" for cue in (keep_turn["guidance"].get("uiCues") or [])
        )
        pending_a = [p for p in kept_a["proposals"] if p["status"] == "pending"]
        assert len(pending_a) == 1
        assert pending_a[0]["assistantTurnId"] == keep_turn["turnId"]
        orig_offer_a = next(t for t in kept_a["turns"] if t["turnId"] == offer_a["turnId"])
        assert orig_offer_a["proposalState"]["status"] == "stale"
        with bundle.container.db.read_conn() as conn:
            keep_prop_json = json.loads(
                conn.execute(
                    "SELECT proposal_json FROM change_proposals WHERE proposal_id = ?",
                    (pending_a[0]["proposalId"],),
                ).fetchone()[0]
            )
            orig_binding_a = json.loads(
                conn.execute(
                    "SELECT proposal_binding_json FROM conversation_turns WHERE turn_id = ?",
                    (offer_a["turnId"],),
                ).fetchone()[0]
            )
        assert keep_prop_json["requirementRecord"] == orig_binding_a["requirementRecord"]
        assert keep_prop_json["revisionPlan"] == orig_binding_a["revisionPlan"]

        # Flow B: Challenge offer turn directly (no change_proposals) -> reason -> revise ("user")
        sid_b, v1_b = await _bootstrap_formal_stage1(client)
        planned_b = await _run_binary_to_offer(bundle, client, sid_b, v1_b, "chgB")
        offer_b = planned_b["turns"][-1]

        started_b = await client.post(
            f"/api/sessions/{sid_b}/messages",
            json={
                "content": "Challenge this plan",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-start",
                "action": "challenge_revision",
                "sourceTurnId": offer_b["turnId"],
            },
        )
        assert started_b.status_code == 200, started_b.text
        chg_b_id = started_b.json()["turns"][-1]["guidance"]["challengeState"]["challengeId"]

        bundle.cocreation_transport.queue(
            _make_review_reason_json(),
            _make_req_json("water", "Try removing water instead", "Remove the water tile"),
            _make_water_plan_json(),
            _make_water_ops_json(),
        )
        reviewed_b = await client.post(
            f"/api/sessions/{sid_b}/messages",
            json={
                "content": "Try removing water instead",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-reason",
                "action": "continue_challenge",
                "challengeId": chg_b_id,
            },
        )
        assert reviewed_b.status_code == 200, reviewed_b.text

        revised_b_resp = await client.post(
            f"/api/sessions/{sid_b}/messages",
            json={
                "content": "No",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-revise",
                "action": "continue_challenge",
                "challengeId": chg_b_id,
                "challengeChoice": "user",
            },
        )
        assert revised_b_resp.status_code == 200, revised_b_resp.text
        revised_b = revised_b_resp.json()
        rev_turn = revised_b["turns"][-1]
        assert rev_turn["guidance"]["move"] == "offer_revision"
        assert rev_turn["guidance"]["proposalOffer"]["availableActions"] == ["execute_revision"]
        # NO premature change_proposals row on replacement purple offer!
        assert revised_b["proposals"] == []
        orig_offer_b = next(t for t in revised_b["turns"] if t["turnId"] == offer_b["turnId"])
        assert orig_offer_b["proposalState"]["status"] == "stale"

        # Execute-only replacement card cannot be challenged again
        re_chg = await client.post(
            f"/api/sessions/{sid_b}/messages",
            json={
                "content": "Challenge replacement",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-rechallenge",
                "action": "challenge_revision",
                "sourceTurnId": rev_turn["turnId"],
            },
        )
        assert re_chg.status_code == 409
        assert re_chg.json()["error"]["code"] == "INVALID_CARD_SOURCE"

        # Executing replacement purple card creates pending proposal & marks replacement stale
        exec_b = await client.post(
            f"/api/sessions/{sid_b}/messages",
            json={
                "content": "Draft this suggestion",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-exec",
                "action": "execute_revision",
                "sourceTurnId": rev_turn["turnId"],
            },
        )
        assert exec_b.status_code == 200, exec_b.text
        exec_b_json = exec_b.json()
        pending_b = [p for p in exec_b_json["proposals"] if p["status"] == "pending"]
        assert len(pending_b) == 1
        assert not exec_b_json["turns"][-1]["guidance"].get("proposalOffer")
        refreshed_rev = next(t for t in exec_b_json["turns"] if t["turnId"] == rev_turn["turnId"])
        assert refreshed_rev["proposalState"]["status"] == "stale"

        # Rejecting the executed proposal leaves source card stale (cannot re-execute)
        rej_b = await client.post(
            f"/api/sessions/{sid_b}/proposals/{pending_b[0]['proposalId']}/decision",
            json={
                "decision": "reject",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-rej",
                "reason": "Will adjust manually",
            },
        )
        assert rej_b.status_code == 200, rej_b.text
        re_exec_after_rej = await client.post(
            f"/api/sessions/{sid_b}/messages",
            json={
                "content": "Draft again after reject",
                "baseVersionId": v1_b,
                "idempotencyKey": "chgB-reexec-after-rej",
                "action": "execute_revision",
                "sourceTurnId": rev_turn["turnId"],
            },
        )
        assert re_exec_after_rej.status_code == 409
        assert re_exec_after_rej.json()["error"]["code"] == "PROPOSAL_STALE"


@pytest.mark.asyncio
async def test_alternative_revision_excludes_offer_bindings_marks_stale_and_defers_proposal(
    app_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        sid, v1 = await _bootstrap_formal_stage1(client)
        planned = await _run_binary_to_offer(bundle, client, sid, v1, "alt1")
        offer_1 = planned["turns"][-1]
        assert planned["proposals"] == []

        # Compute expected candidate fingerprint from offer #1 (wall at row 4, col 6 removed)
        offer_1_rows = list(BASE_ROWS)
        offer_1_rows[3] = "#..........#"
        expected_excluded_fp = map_fingerprint(offer_1_rows)

        captured_excluded: list[list[str]] = []
        orig_gen = bundle.container.proposal_pipeline.generate_proposal

        async def _spy_generate_proposal(*args: Any, **kwargs: Any) -> Any:
            captured_excluded.append(list(kwargs.get("excluded_fingerprints") or ()))
            return await orig_gen(*args, **kwargs)

        monkeypatch.setattr(
            bundle.container.proposal_pipeline, "generate_proposal", _spy_generate_proposal
        )
        bundle.cocreation_transport.queue(
            _make_req_json("water", "Suggest another option", "Remove water instead"),
            _make_water_plan_json(),
            _make_water_ops_json(),
        )
        alt_resp = await client.post(
            f"/api/sessions/{sid}/messages",
            json={
                "content": "Suggest another option",
                "baseVersionId": v1,
                "idempotencyKey": "alt-req-1",
                "action": "alternative_revision",
                "sourceTurnId": offer_1["turnId"],
            },
        )
        assert alt_resp.status_code == 200, alt_resp.text
        alt_json = alt_resp.json()

        # 1. excluded_fingerprints included offer #1's candidate map despite zero change_proposals
        assert captured_excluded and expected_excluded_fp in captured_excluded[0]
        # 2. Still ZERO change_proposals created on alternative_revision offer turn
        assert alt_json["proposals"] == []
        # 3. Offer #1 marked stale; Offer #2 is active purple card
        offer_1_after = next(t for t in alt_json["turns"] if t["turnId"] == offer_1["turnId"])
        assert offer_1_after["proposalState"]["status"] == "stale"
        assert offer_1_after["proposalState"]["actionable"] is False
        offer_2 = alt_json["turns"][-1]
        assert offer_2["turnId"] != offer_1["turnId"]
        assert offer_2["guidance"]["move"] == "offer_revision"
        assert offer_2["proposalState"]["status"] == "active"
        assert offer_2["proposalState"]["actionable"] is True


@pytest.mark.asyncio
async def test_finalize_allowed_with_unexecuted_offer_and_supersedes_active_stage_artifacts(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        sid, v1 = await _bootstrap_formal_stage1(client)
        planned = await _run_binary_to_offer(bundle, client, sid, v1, "fin1")
        offer_turn = planned["turns"][-1]
        assert planned["proposals"] == []

        # Seed active binary topic and open revision challenge on v1
        now_iso = bundle.clock.utc_now()
        with bundle.container.db.write_tx() as conn:
            conn.execute(
                "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    sid,
                    "topic-fin-active",
                    dump_json(
                        {
                            "topicId": "topic-fin-active",
                            "versionId": v1,
                            "status": "clarifying",
                            "retryAction": {"kind": "choice"},
                        }
                    ),
                    now_iso,
                ),
            )
            conn.execute(
                "INSERT INTO revision_challenges("
                "challenge_id, session_id, base_version_id, source_proposal_turn_id, "
                "challenge_turn_id, status, current_reason_turn_id, reviews_json, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'awaiting_reason', NULL, '[]', ?, ?)",
                (
                    "chg-fin-open",
                    sid,
                    v1,
                    offer_turn["turnId"],
                    f"turn-chg-{uuid.uuid4().hex[:8]}",
                    now_iso,
                    now_iso,
                ),
            )

        fin = await client.post(
            f"/api/sessions/{sid}/finalize",
            json={"baseVersionId": v1, "idempotencyKey": "fin-with-offer"},
        )
        assert fin.status_code == 200, fin.text
        assert fin.json()["status"] == "awaiting_intention"

        with bundle.container.db.read_conn() as conn:
            topic_row = conn.execute(
                "SELECT state_json FROM binary_discovery_topics "
                "WHERE session_id = ? AND topic_id = ?",
                (sid, "topic-fin-active"),
            ).fetchone()
            chg_row = conn.execute(
                "SELECT status FROM revision_challenges WHERE challenge_id = ?",
                ("chg-fin-open",),
            ).fetchone()
        assert json.loads(topic_row["state_json"])["status"] == "cancelled"
        assert chg_row["status"] == "resolved"
