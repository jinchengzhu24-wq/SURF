"""TDD regression tests for #20 atomic idempotency completion and #17 audit prose boundary."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any

import pytest
from server.application.analytics._cocreation_projection import (
    build_authoritative_player_projection,
    load_formal_session_bundles,
)
from server.domain.memory import add_open_question, ensure_design_context
from server.persistence.database import dump_json
from tests.conftest import (
    ALT_ROWS,
    BASE_ROWS,
    extract_hash_param,
    make_binary_ask_json,
    make_binary_plan_json,
    make_chat_json,
    make_map_ops_json,
    make_opening_json,
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
            "comparison": "Keeping the wall preserves structure while water removal opens space.",
        }
    )


async def _setup_session(
    bundle: Any, *, match_id: str | None = None, player_slot: int | None = None
) -> tuple[str, str, str]:
    created = await bundle.container.session_lifecycle_service.create_formal_session(
        rows=BASE_ROWS,
        initial_draft_method="partial_completion",
        language="en",
        idempotency_key=f"b3-sess-{uuid.uuid4().hex[:8]}",
        match_id=match_id,
        player_number=player_slot,
    )
    sid = created["sessionId"]
    bootstrap = extract_hash_param(created["launchUrl"], "bootstrap")
    access = (
        await bundle.container.session_lifecycle_service.authorize_browser_access(
            sid, bootstrap
        )
    )["accessToken"]
    locked = await bundle.container.session_lifecycle_service.update_language(
        sid, access, language="en"
    )
    return sid, access, locked["currentVersionId"]


def _count_write_tx_and_block_post_complete(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Any,
    *,
    session_id: str,
    scope: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Track write_tx entries and assert idempotency key is completed inside the domain tx."""
    state: dict[str, Any] = {"tx_count": 0, "status_after_tx": []}
    orig_write_tx = bundle.container.db.write_tx

    @contextmanager
    def _tracking_tx():
        state["tx_count"] += 1
        with orig_write_tx() as conn:
            yield conn
        # Inspect committed status immediately after this write_tx commits
        rec = bundle.container.operational_repo.get_key(session_id, scope, idempotency_key)
        state["status_after_tx"].append(rec.status if rec is not None else None)

    monkeypatch.setattr(bundle.container.db, "write_tx", _tracking_tx)
    return state


@pytest.mark.asyncio
async def test_atomic_idempotency_stage_save_restore_assess_and_finalize(
    app_bundle_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = app_bundle_factory()
    sid, access, v1 = await _setup_session(bundle)
    stage_svc = bundle.container.stage_service
    lifecycle = bundle.container.session_lifecycle_service
    op_repo = bundle.container.operational_repo

    # 1. assess_version (stage_ops.py): 1 claim_key + 1 atomic domain+complete write_tx
    bundle.cocreation_transport.queue(make_opening_json("Welcome to Stage 1."))
    st_assess = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="assess_version", idempotency_key="ik-assess-1"
    )
    await stage_svc.assess_version(sid, v1, access, idempotency_key="ik-assess-1")
    monkeypatch.undo()
    assert st_assess["tx_count"] == 2
    assert st_assess["status_after_tx"] == ["in_progress", "completed"]

    # 2. save_version (stage/service.py): post-tx read failure must NOT leave key in_progress/failed
    orig_get_payload = bundle.container.session_read_model_service.get_session_payload

    async def _boom_payload(_sid: str) -> dict[str, Any]:
        raise RuntimeError("simulated post-tx read failure")

    st_save = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="save_version", idempotency_key="ik-save-1"
    )
    monkeypatch.setattr(
        bundle.container.session_read_model_service, "get_session_payload", _boom_payload
    )
    with pytest.raises(RuntimeError, match="simulated post-tx read failure"):
        await stage_svc.save_version(
            sid,
            access,
            base_version_id=v1,
            rows=ALT_ROWS,
            idempotency_key="ik-save-1",
            summary="Removed center wall",
        )
    monkeypatch.undo()
    assert st_save["tx_count"] == 2
    assert st_save["status_after_tx"] == ["in_progress", "completed"]
    saved_key = op_repo.get_key(sid, "save_version", "ik-save-1")
    assert saved_key is not None and saved_key.status == "completed"
    # Replay immediately succeeds using completed key
    replayed = await stage_svc.save_version(
        sid,
        access,
        base_version_id=v1,
        rows=ALT_ROWS,
        idempotency_key="ik-save-1",
        summary="Removed center wall",
    )
    v2 = replayed["currentVersionId"]
    assert v2 != v1
    assert len(replayed["versions"]) == 2

    # 3. restore_version (stage/stage_ops.py): 1 claim_key + 1 atomic restore+complete write_tx
    st_restore = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="restore_version", idempotency_key="ik-restore-1"
    )
    restored = await stage_svc.restore_version(
        sid, v1, access, base_version_id=v2, idempotency_key="ik-restore-1"
    )
    monkeypatch.undo()
    assert st_restore["tx_count"] == 2
    assert st_restore["status_after_tx"] == ["in_progress", "completed"]
    v3 = restored["currentVersionId"]

    # 4. finalize_session (lifecycle/finalize.py): 1 claim_key + 1 atomic finalize+complete tx
    st_fin = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="finalize", idempotency_key="ik-fin-1"
    )
    monkeypatch.setattr(
        bundle.container.session_read_model_service, "get_session_payload", _boom_payload
    )
    with pytest.raises(RuntimeError, match="simulated post-tx read failure"):
        await lifecycle.finalize_session(
            sid, access, base_version_id=v3, idempotency_key="ik-fin-1"
        )
    monkeypatch.undo()
    assert st_fin["tx_count"] == 2
    assert st_fin["status_after_tx"] == ["in_progress", "completed"]
    fin_key = op_repo.get_key(sid, "finalize", "ik-fin-1")
    assert fin_key is not None and fin_key.status == "completed"
    # Replay succeeds and preserves session state
    final_payload = await lifecycle.finalize_session(
        sid, access, base_version_id=v3, idempotency_key="ik-fin-1"
    )
    assert final_payload["status"] == "awaiting_intention"
    assert orig_get_payload is not None


@pytest.mark.asyncio
async def test_atomic_idempotency_dialogue_orchestrator_proposals_and_intents(
    app_bundle_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = app_bundle_factory()
    sid, access, v1 = await _setup_session(bundle)
    orch = bundle.container.message_orchestrator
    prop_svc = bundle.container.proposal_service
    intent_svc = bundle.container.intent_service

    # 1. Ordinary chat with tentative intent hypothesis
    bundle.cocreation_transport.queue(
        make_routing_json("ordinary", evidence_span="center"),
        make_chat_json(
            "Let us widen the center.",
            hyp="Widen the center for smoother pushing.",
            evidence_span="center",
        ),
    )
    st_ord = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-ord-1"
    )
    chat_payload = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-ord-1",
        content="Let us open the center",
    )
    monkeypatch.undo()
    assert st_ord["tx_count"] == 2
    assert st_ord["status_after_tx"] == ["in_progress", "completed"]

    # Seed a visible_output open question to test IntentService.submit_question_feedback
    with bundle.container.db.write_tx() as conn:
        v1_rec = bundle.container.sessions_repo.get_version(sid, v1)
        assert v1_rec is not None
        q_ctx = add_open_question(
            ensure_design_context(v1_rec.design_context),
            "Keep the bottom open?",
            stage_id=v1,
            turn_id=chat_payload["turns"][-1]["turnId"],
            source_kind="visible_output",
        )
        conn.execute(
            "UPDATE level_versions SET design_context_json = ? "
            "WHERE session_id = ? AND version_id = ?",
            (dump_json(q_ctx.to_wire_dict()), sid, v1),
        )
    refreshed = await bundle.container.session_read_model_service.get_session_payload(sid)

    # 2. IntentService: submit_question_feedback & submit_intent_feedback (2 tx each)
    qid = refreshed["progressContexts"][0]["questionRecords"][0]["questionId"]
    st_qf = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="question_feedback", idempotency_key="ik-qf-1"
    )
    await intent_svc.submit_question_feedback(
        sid, qid, access, action="ignore", base_version_id=v1, idempotency_key="ik-qf-1"
    )
    monkeypatch.undo()
    assert st_qf["tx_count"] == 2
    assert st_qf["status_after_tx"] == ["in_progress", "completed"]

    asst_turn = chat_payload["turns"][-1]
    hid = asst_turn["guidance"]["intentState"]["hypothesisId"]
    st_if = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="intent_feedback", idempotency_key="ik-if-1"
    )
    await intent_svc.submit_intent_feedback(
        sid,
        hid,
        access,
        action="confirm",
        base_version_id=v1,
        idempotency_key="ik-if-1",
        source_turn_id=asst_turn["turnId"],
    )
    monkeypatch.undo()
    assert st_if["tx_count"] == 2
    assert st_if["status_after_tx"] == ["in_progress", "completed"]

    # 3. Binary discovery start_topic (orchestrator -> binary_discovery/_binary_commit)
    bundle.cocreation_transport.queue(make_binary_ask_json)
    st_bstart = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-bstart-1"
    )
    asked = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-bstart-1",
        content="Make center simpler",
        request_proposal=True,
    )
    monkeypatch.undo()
    assert st_bstart["tx_count"] == 2
    assert st_bstart["status_after_tx"] == ["in_progress", "completed"]

    pq = next(
        t["guidance"]["proposalQuestion"]
        for t in reversed(asked["turns"])
        if t["guidance"].get("proposalQuestion")
    )

    # 4. Binary choice (proposal_service.choose_binary_choice -> _binary_commit)
    bundle.cocreation_transport.queue(make_binary_plan_json(), make_map_ops_json())
    st_bchoice = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="binary_choice", idempotency_key="ik-bchoice-1"
    )
    planned = await prop_svc.choose_binary_choice(
        sid,
        access,
        topic_id=pq["topicId"],
        question_id=pq["questionId"],
        choice_id=pq["choices"][0]["choiceId"],
        base_version_id=v1,
        idempotency_key="ik-bchoice-1",
    )
    monkeypatch.undo()
    assert st_bchoice["tx_count"] == 2
    assert st_bchoice["status_after_tx"] == ["in_progress", "completed"]
    offer_turn = planned["turns"][-1]

    # 5. Execute revision on binary offer -> reject proposal (revision_card + decide_proposal)
    st_exec = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-exec-1"
    )
    executed = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-exec-1",
        action="execute_revision",
        assistant_turn_id=offer_turn["turnId"],
        content="Execute suggestion",
    )
    monkeypatch.undo()
    assert st_exec["tx_count"] == 2
    assert st_exec["status_after_tx"] == ["in_progress", "completed"]

    pid1 = next(p["proposalId"] for p in executed["proposals"] if p["status"] == "pending")
    st_rej = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="decide_proposal", idempotency_key="ik-rej-1"
    )
    await prop_svc.decide_proposal(
        sid, pid1, access, decision="reject", base_version_id=v1, idempotency_key="ik-rej-1"
    )
    monkeypatch.undo()
    assert st_rej["tx_count"] == 2
    assert st_rej["status_after_tx"] == ["in_progress", "completed"]

    # 6. Alternative revision (revision_card.alternative_revision)
    bundle.cocreation_transport.queue(
        _make_water_req_json,
        _make_water_plan_json(),
        _make_water_ops_json(),
    )
    st_alt = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-alt-1"
    )
    alt_res = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-alt-1",
        action="alternative_revision",
        content="Please remove water instead",
    )
    monkeypatch.undo()
    assert st_alt["tx_count"] == 2
    assert st_alt["status_after_tx"] == ["in_progress", "completed"]
    alt_turn = alt_res["turns"][-1]

    # 7. Challenge start + reason review + keep_proposal (challenge.py) + accept (decide_proposal)
    st_cstart = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-cstart-1"
    )
    chg_started = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-cstart-1",
        action="challenge_revision",
        assistant_turn_id=alt_turn["turnId"],
        content="Challenge this proposal",
    )
    monkeypatch.undo()
    assert st_cstart["tx_count"] == 2
    assert st_cstart["status_after_tx"] == ["in_progress", "completed"]
    chg_id = chg_started["turns"][-1]["guidance"]["challengeState"]["challengeId"]

    bundle.cocreation_transport.queue(_make_review_reason_json())
    st_crev = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-crev-1"
    )
    long_challenge_reason = "CHALLENGE_REASON_PROSE_" + ("C" * 700)
    await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-crev-1",
        action="continue_challenge",
        challenge_id=chg_id,
        content=long_challenge_reason,
    )
    monkeypatch.undo()
    assert st_crev["tx_count"] == 2
    assert st_crev["status_after_tx"] == ["in_progress", "completed"]
    # Challenge disagreement audit must not duplicate long acceptedReason prose
    with bundle.container.db.read_conn() as conn:
        dis_rows = [
            json.loads(r["payload_json"])
            for r in conn.execute(
                "SELECT payload_json FROM audit_events "
                "WHERE session_id = ? AND event_type = 'disagreement_updated'",
                (sid,),
            ).fetchall()
        ]
    assert dis_rows
    assert "CHALLENGE_REASON_PROSE_" not in json.dumps(dis_rows[-1])
    assert "acceptedReason" not in (dis_rows[-1].get("disagreement") or {})

    st_ckeep = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="message", idempotency_key="ik-ckeep-1"
    )
    kept = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="ik-ckeep-1",
        action="continue_challenge",
        challenge_id=chg_id,
        challenge_choice="keep_proposal",
        content="Yes",
    )
    monkeypatch.undo()
    assert st_ckeep["tx_count"] == 2
    assert st_ckeep["status_after_tx"] == ["in_progress", "completed"]

    pid2 = next(p["proposalId"] for p in kept["proposals"] if p["status"] == "pending")
    st_acc = _count_write_tx_and_block_post_complete(
        monkeypatch, bundle, session_id=sid, scope="decide_proposal", idempotency_key="ik-acc-1"
    )
    await prop_svc.decide_proposal(
        sid, pid2, access, decision="accept", base_version_id=v1, idempotency_key="ik-acc-1"
    )
    monkeypatch.undo()
    assert st_acc["tx_count"] == 2
    assert st_acc["status_after_tx"] == ["in_progress", "completed"]


@pytest.mark.asyncio
async def test_audit_prose_boundary_preserves_progress_drawer_and_projection_with_zero_leaks(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    match_id = f"match-b3-{uuid.uuid4().hex[:8]}"
    sid, access, v1 = await _setup_session(bundle, match_id=match_id, player_slot=1)
    orch = bundle.container.message_orchestrator
    intent_svc = bundle.container.intent_service
    stage_svc = bundle.container.stage_service

    # 1. Ordinary turn sets explicit goal + tentative intent -> confirm intent
    bundle.cocreation_transport.queue(
        make_routing_json("ordinary", evidence_span="center"),
        make_chat_json(
            "Opening the center helps pacing.",
            hyp="Keep the center open for smoother play.",
            evidence_span="center",
        ),
    )
    chat_res = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="audit-m1",
        content="Keep the center open and brisk",
    )
    turn1 = chat_res["turns"][-1]
    hid = turn1["guidance"]["intentState"]["hypothesisId"]
    await intent_svc.submit_intent_feedback(
        sid,
        hid,
        access,
        action="confirm",
        base_version_id=v1,
        idempotency_key="audit-if1",
        source_turn_id=turn1["turnId"],
    )

    # 2. Manual edit + assess_version with long LLM comparison explanation & long review prose
    long_explanation = "EXPLANATION_PROSE_" + ("X" * 700)
    long_review_prose = "LONG_REVIEW_PROSE_" + ("R" * 900)
    saved = await stage_svc.save_version(
        sid,
        access,
        base_version_id=v1,
        rows=ALT_ROWS,
        idempotency_key="audit-save-v2",
        summary="Manual edit summary " + ("S" * 700),
    )
    v2 = saved["currentVersionId"]

    # Link manual_edit evidence onto confirmed hypothesis so ProgressDrawer projects detailedText
    with bundle.container.db.write_tx() as conn:
        ev_row = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE session_id = ? AND event_type = 'intent_evidence_recorded' "
            "ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        manual_ev_id = json.loads(ev_row["payload_json"])["evidenceId"]
        ver_row = conn.execute(
            "SELECT design_context_json FROM level_versions WHERE version_id = ?", (v2,)
        ).fetchone()
        ctx = json.loads(ver_row["design_context_json"])
        ctx["intentHypotheses"][0]["supportingEvidenceIds"].append(manual_ev_id)
        conn.execute(
            "UPDATE level_versions SET design_context_json = ? WHERE version_id = ?",
            (dump_json(ctx), v2),
        )

    def _conflict_classify(kwargs: dict[str, Any]) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        dirs = payload.get("directions") or payload.get("confirmedDirections") or []
        return json.dumps(
            {
                "comparisons": [
                    {
                        "directionId": d["id"],
                        "relation": "conflict",
                        "explanation": long_explanation,
                    }
                    for d in dirs
                ]
            }
        )

    bundle.cocreation_transport.queue(
        _conflict_classify,
        json.dumps(
            {
                "openingMessage": "Stage 2 manual edit reviewed.",
                "reviewMessage": long_review_prose,
                "conflict": {
                    "userPosition": "Removed the central wall.",
                    "aiPosition": "Prior direction kept a tighter corridor.",
                    "coreDisagreement": "Tradeoff between openness and detour.",
                    "nextQuestion": "Do you want to keep this wider layout?",
                },
                "assessment": {
                    "solutionSummary": "Direct push.",
                    "difficultyOpinion": "Easier layout.",
                    "features": ["Open center"],
                    "suggestions": ["Consider testing flow."],
                },
            }
        ),
    )
    assessed = await stage_svc.assess_version(sid, v2, access, idempotency_key="audit-assess-v2")

    # A. Progress drawer projection STILL works 100% (including manual_edit detailedText <= 500)
    v2_progress = next(pc for pc in assessed["progressContexts"] if pc["versionId"] == v2)
    assert len(v2_progress["designInclinations"]) == 1
    trail = v2_progress["designInclinations"][0]["evidenceTrail"]
    manual_trail = next(item for item in trail if item["kind"] == "manual_edit")
    assert manual_trail["text"]
    assert manual_trail["detailedText"] is not None
    assert manual_trail["detailedText"].startswith("LONG_REVIEW_PROSE_")
    assert len(manual_trail["detailedText"]) <= 500

    # B. Dashboard authoritative projection STILL emits BOTH manual_edit_review AND llm_challenge
    bundles = load_formal_session_bundles(bundle.container.db, match_ids={match_id})
    flow, _, _ = build_authoritative_player_projection(
        bundles[(match_id, 1)],
        match_id=match_id,
        room_code="ROOM1",
        player_number=1,
        study_session_id="study-1",
    )
    node_types = [ev.get("nodeType") for ev in flow if ev.get("eventType") == "node"]
    assert "manual_edit_review" in node_types
    assert "llm_challenge" in node_types

    # C. Inspect audit_events directly for tightened prose boundary & zero leakage
    with bundle.container.db.read_conn() as conn:
        audits = [
            (r["event_type"], json.loads(r["payload_json"]))
            for r in conn.execute(
                "SELECT event_type, payload_json FROM audit_events WHERE session_id = ? "
                "ORDER BY id",
                (sid,),
            ).fetchall()
        ]

    reviewed_ev = next(p for t, p in audits if t == "human_edit_reviewed")
    # Unread LLM comparison explanation prose MUST NOT be dumped into human_edit_reviewed
    assert "EXPLANATION_PROSE_" not in json.dumps(reviewed_ev)
    assert reviewed_ev.get("hasDisagreement") is True
    assert "disagreement" not in reviewed_ev  # lives in disagreement_started, not duplicated twice
    for comp in reviewed_ev.get("comparisons") or []:
        assert "explanation" not in comp
        assert {"confirmedDirectionId", "relation", "evidenceIds"}.issubset(comp.keys())

    rewrite_ev = next(
        p
        for t, p in audits
        if t == "intent_progress_rewrite" and p.get("artifactType") == "manual_edit"
    )
    assert len(rewrite_ev["summaryText"]) <= 500
    assert len(rewrite_ev["detailedText"]) <= 500
    assert rewrite_ev["reviewCharCount"] == len(long_review_prose)
    assert rewrite_ev["hasDisagreement"] is True

    # Telemetry-only audit events MUST contain ZERO prose, prompts, full 12x10 maps, or secrets
    telemetry_types = {
        "reply_delivery_outcome",
        "message_generation_failed",
        "reply_generation_failed",
        "challenge_reason_review_pending",
        "component_recovery",
    }
    secret_val = bundle.settings.token_secret.get_secret_value()
    for ev_type, payload in audits:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert secret_val not in serialized
        assert "############" not in serialized
        assert "You are a Sokoban" not in serialized
        if ev_type in telemetry_types:
            assert "LONG_REVIEW_PROSE_" not in serialized
            assert "EXPLANATION_PROSE_" not in serialized
            assert "Opening the center helps pacing." not in serialized


def _assert_all_strings_bounded(obj: Any, *, max_len: int = 500, path: str = "$") -> None:
    if isinstance(obj, str):
        assert len(obj) <= max_len, f"String at {path} has length {len(obj)} > {max_len}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _assert_all_strings_bounded(v, max_len=max_len, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _assert_all_strings_bounded(item, max_len=max_len, path=f"{path}[{idx}]")


@pytest.mark.asyncio
async def test_human_edit_disagreement_audits_are_structured_summaries_and_projection_reads_card(
    app_bundle_factory,
) -> None:
    """Verify Polish #3: human_edit disagreement_started/acknowledged/resolved audits are
    structured summaries <= 500 chars with zero duplicated card prose, while
    _cocreation_projection still reads the full LET'S DISCUSS card from
    review_turn.guidance['disagreement'] and detects acknowledged & manual_reedit."""
    bundle = app_bundle_factory()
    match_id = f"match-pol3-{uuid.uuid4().hex[:8]}"
    sid, access, v1 = await _setup_session(bundle, match_id=match_id, player_slot=1)
    orch = bundle.container.message_orchestrator
    stage_svc = bundle.container.stage_service
    recovery_svc = bundle.container.recovery_service

    # Establish a confirmed direction so conflict classifier triggers
    intent_svc = bundle.container.intent_service
    bundle.cocreation_transport.queue(
        make_routing_json("ordinary", evidence_span="center"),
        make_chat_json(
            "Noted, keeping the center open.",
            hyp="Keep the center open for readable pushing.",
            evidence_span="center",
        ),
    )
    dir_res = await orch.send_message(
        sid,
        access,
        base_version_id=v1,
        idempotency_key="pol3-dir",
        content="Keep the center open",
    )
    dir_turn = dir_res["turns"][-1]
    dir_hid = dir_turn["guidance"]["intentState"]["hypothesisId"]
    await intent_svc.submit_intent_feedback(
        sid,
        dir_hid,
        access,
        action="confirm",
        base_version_id=v1,
        idempotency_key="pol3-dir-confirm",
        source_turn_id=dir_turn["turnId"],
    )

    long_user_pos = "HUMAN_DIS_USER_POS_" + ("U" * 280)
    long_ai_pos = "HUMAN_DIS_AI_POS_" + ("A" * 280)
    long_core = "HUMAN_DIS_CORE_" + ("C" * 620)
    long_next_q = "HUMAN_DIS_NEXT_Q_" + ("Q" * 180) + "?"

    def _conflict_json(kwargs: dict[str, Any]) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        dirs = payload.get("directions") or payload.get("confirmedDirections") or []
        return json.dumps(
            {
                "comparisons": [
                    {
                        "directionId": str(d["id"]),
                        "relation": "conflict",
                        "explanation": "Conflicts with open center.",
                    }
                    for d in dirs
                ]
            }
        )

    def _review_conflict_json() -> str:
        return json.dumps(
            {
                "openingMessage": "Manual edit observed.",
                "reviewMessage": "Short manual edit review body.",
                "conflict": {
                    "userPosition": long_user_pos,
                    "aiPosition": long_ai_pos,
                    "coreDisagreement": long_core,
                    "nextQuestion": long_next_q,
                },
                "assessment": {
                    "solutionSummary": "Direct push.",
                    "difficultyOpinion": "Tighter.",
                    "features": ["Flank wall"],
                    "suggestions": ["Playtest."],
                },
            }
        )

    rows_v3 = list(BASE_ROWS)
    rows_v3[4] = "#....@..#..#"
    rows_v4 = list(BASE_ROWS)
    rows_v4[5] = "#........#.#"
    rows_v5 = list(BASE_ROWS)
    rows_v5[6] = "#........#.#"

    # 1. Stage 2 manual edit -> disagreement_started -> direct ordinary engage -> acknowledged
    s2 = await stage_svc.save_version(
        sid, access, base_version_id=v1, rows=ALT_ROWS, idempotency_key="pol3-s2", summary="Edit 2"
    )
    v2 = s2["currentVersionId"]
    bundle.cocreation_transport.queue(_conflict_json, _review_conflict_json())
    await stage_svc.assess_version(sid, v2, access, idempotency_key="pol3-a2")
    bundle.cocreation_transport.queue(
        json.dumps(
            {
                "acts": ["evaluation"],
                "elements": ["internal_walls"],
                "mapRelated": True,
                "directionSufficient": True,
                "routeTarget": "ordinary",
                "evidenceSpan": "keep it tighter",
                "changes": [],
                "humanEditEngagement": {
                    "decision": "engage",
                    "evidenceSpan": "keep it tighter",
                },
            }
        ),
        make_chat_json("Understood, keeping it tighter."),
    )
    await orch.send_message(
        sid,
        access,
        base_version_id=v2,
        idempotency_key="pol3-ack-direct",
        content="Yes, I want to keep it tighter here.",
    )

    # 2. Stage 3 manual edit -> disagreement_started -> recovery engage -> acknowledged
    s3 = await stage_svc.save_version(
        sid, access, base_version_id=v2, rows=rows_v3, idempotency_key="pol3-s3", summary="Edit 3"
    )
    v3 = s3["currentVersionId"]
    bundle.cocreation_transport.queue(_conflict_json, _review_conflict_json())
    await stage_svc.assess_version(sid, v3, access, idempotency_key="pol3-a3")
    # Routing LLM failure during active human_edit disagreement creates a recovery task
    from server.core.llm_client import UpstreamLLMError

    bundle.cocreation_transport.queue(
        UpstreamLLMError("MODEL_TIMEOUT", "timeout", retryable=False),
        make_chat_json("Got your note about the flank wall."),
    )
    rec_res = await orch.send_message(
        sid,
        access,
        base_version_id=v3,
        idempotency_key="pol3-ack-rec-msg",
        content="That flank wall is deliberate.",
    )
    rec_id = rec_res["interactionState"]["recoveries"][0]["recoveryId"]
    bundle.cocreation_transport.queue(
        json.dumps({"decision": "engage", "evidenceSpan": "deliberate"})
    )
    await recovery_svc.retry_recovery(
        sid, rec_id, access, base_version_id=v3, idempotency_key="pol3-rec-retry"
    )

    # 3. Stage 4 manual edit -> disagreement_started -> Stage 5 re-edit -> disagreement_resolved
    s4 = await stage_svc.save_version(
        sid, access, base_version_id=v3, rows=rows_v4, idempotency_key="pol3-s4", summary="Edit 4"
    )
    v4 = s4["currentVersionId"]
    bundle.cocreation_transport.queue(_conflict_json, _review_conflict_json())
    await stage_svc.assess_version(sid, v4, access, idempotency_key="pol3-a4")
    await stage_svc.save_version(
        sid, access, base_version_id=v4, rows=rows_v5, idempotency_key="pol3-s5", summary="Edit 5"
    )

    # Verify (a): ALL human_edit disagreement audit payloads are structured summaries <= 500 chars
    with bundle.container.db.read_conn() as conn:
        dis_audits = [
            (r["event_type"], json.loads(r["payload_json"]))
            for r in conn.execute(
                "SELECT event_type, payload_json FROM audit_events "
                "WHERE session_id = ? "
                "AND event_type IN ('disagreement_started', 'disagreement_acknowledged', "
                "'disagreement_resolved') ORDER BY id",
                (sid,),
            ).fetchall()
        ]
    assert [t for t, _ in dis_audits] == [
        "disagreement_started",
        "disagreement_acknowledged",
        "disagreement_started",
        "disagreement_acknowledged",
        "disagreement_started",
        "disagreement_resolved",
    ]
    prose_markers = (
        "HUMAN_DIS_USER_POS_",
        "HUMAN_DIS_AI_POS_",
        "HUMAN_DIS_CORE_",
        "HUMAN_DIS_NEXT_Q_",
    )
    forbidden_dis_keys = {
        "userPosition",
        "aiPosition",
        "coreDisagreement",
        "nextQuestion",
        "displayCard",
    }
    for ev_type, payload in dis_audits:
        assert payload.get("subject") == "human_edit", (ev_type, payload)
        dis_summary = payload.get("disagreement")
        assert isinstance(dis_summary, dict)
        assert dis_summary.get("disagreementId")
        assert dis_summary.get("subject") == "human_edit"
        assert dis_summary.get("status") in {"active", "acknowledged", "resolved"}
        assert forbidden_dis_keys.isdisjoint(dis_summary.keys()), (ev_type, dis_summary.keys())
        serialized = json.dumps(payload, ensure_ascii=False)
        for marker in prose_markers:
            assert marker not in serialized, f"Prose marker {marker} leaked into {ev_type}"
        _assert_all_strings_bounded(payload, max_len=500, path=ev_type)

    # Verify (b): _cocreation_projection STILL reads full LET'S DISCUSS card from
    # review_turn.guidance["disagreement"] and projects acknowledged / manual_reedit cleanly!
    bundles = load_formal_session_bundles(bundle.container.db, match_ids={match_id})
    flow, _, _ = build_authoritative_player_projection(
        bundles[(match_id, 1)],
        match_id=match_id,
        room_code="POL3",
        player_number=1,
        study_session_id="study-pol3",
    )
    llm_challenges = [
        ev for ev in flow if ev.get("eventType") == "node" and ev.get("nodeType") == "llm_challenge"
    ]
    assert [n["nodeStatus"] for n in llm_challenges] == [
        "acknowledged",
        "acknowledged",
        "manual_reedit",
    ]
    for node in llm_challenges:
        card = next(
            e for e in node["nodeEntries"] if e["kind"] == "card" and e["label"] == "LET'S DISCUSS"
        )
        for marker in prose_markers:
            assert marker in card["text"], f"Missing {marker} in projected LET'S DISCUSS card"
    reedit_texts = " ".join(e["text"] for e in llm_challenges[2]["nodeEntries"])
    assert "Ended through a new manual edit (Stage 5)" in reedit_texts
