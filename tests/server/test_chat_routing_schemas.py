"""Focused unit tests for split chat/routing/review/translation/blueprint/DG schemas."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from server.application.dialogue.recovery_service import RecoveryService
from server.application.dialogue.translation_service import (
    _apply_translated_guidance,
    _build_translation_item,
)
from server.application.sessions import SessionReadModelService
from server.core.clock import FakeClock
from server.core.grid import parse_grid
from server.core.llm_client import (
    TransportResponse,
    _prepare_schema_for_mode,
)
from server.domain.blueprints.validation import validate_level_plan
from server.llm.grounding import verify_grounding
from server.llm.pipelines.chat import ChatPipeline
from server.llm.pipelines.dg_guide import DGGuidePipeline, normalize_dg_partner_rationale
from server.llm.pipelines.level_plan import LevelPlanPipeline
from server.llm.pipelines.translation import TranslationPipeline
from server.llm.pipelines.understanding import ManualEditConflictResult, UnderstandingPipeline
from server.llm.prompts.dg_guide import _DG_GUIDE_SYSTEM, build_dg_guide_messages
from server.llm.prompts.translation import build_translation_messages
from server.llm.prompts.understanding import (
    build_manual_edit_conflict_messages,
    build_manual_edit_review_messages,
)
from server.llm.schemas import (
    ChatReplyPayload,
    CoordinateLinkPayload,
    HumanEditEngagementPayload,
    HumanEditEngagementRecoveryPayload,
    IntentClaimPayload,
    IntentDecisionPayload,
    ManualEditReviewNoConflictPayload,
    ManualEditReviewPayload,
    ManualEditReviewWithConflictPayload,
    OrdinaryChatReplyPayload,
    OrdinaryChatWithDisagreementPayload,
    PatchQuestionEntry,
    PlainTranslationPayload,
    StageAssessmentPayload,
    StageOpeningPayload,
    TranslatedBinaryQuestionTextPayload,
    TranslatedDisagreementTextPayload,
    TranslatedTurnItemPayload,
    TranslationVerificationPayload,
    TurnRoutingPayload,
    TurnRoutingWithHumanEditPayload,
    build_constrained_dg_guide_model,
    build_constrained_level_plan_model,
    build_manual_edit_conflict_classification_model,
)
from server.persistence.database import SQLiteDatabase, dump_json
from server.persistence.models import ConversationTurnRecord, LevelVersionRecord
from server.persistence.repositories.dialogue_repo import DialogueRepository
from server.persistence.repositories.operational_repo import OperationalRepository
from server.persistence.repositories.sessions_repo import SessionsRepository

SAMPLE_ROWS: tuple[str, ...] = (
    "############",
    "#..........#",
    "#.p.s..t...#",
    "#....#.....#",
    "#....@.....#",
    "#..........#",
    "#..s...t...#",
    "#..........#",
    "#..........#",
    "############",
)


class FakeAsyncTransport:
    """Deterministic fake LLMTransport returning queued JSON payloads or errors."""

    def __init__(
        self,
        responses: Sequence[dict[str, Any] | str | Exception],
        *,
        response_format_mode: str = "json_schema",
    ) -> None:
        self._queue: list[dict[str, Any] | str | Exception] = list(responses)
        self.response_format_mode = response_format_mode
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> TransportResponse:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "response_format": response_format,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "extra_body": extra_body,
            }
        )
        assert self._queue, "FakeAsyncTransport received more calls than queued responses."
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        content = nxt if isinstance(nxt, str) else json.dumps(nxt, ensure_ascii=False)
        return TransportResponse(
            content=content,
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )


def _sample_claim(evidence: str = "keep top open") -> dict[str, Any]:
    return {
        "normalizedMeaning": "Keep upper lane open",
        "subjectType": "route",
        "subjectText": "upper lane",
        "attributeType": "clarity",
        "attributeText": "open",
        "direction": "increase",
        "degree": "neutral",
        "aspect": "gameplay",
        "scopeType": "stage",
        "scopeText": "stage",
        "evidenceSpan": evidence,
    }


def _base_level_plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "minSolutionSteps": 20,
        "maxSolutionSteps": 36,
        "minWaterAreas": 1,
        "maxWaterAreas": 2,
        "minWallObstacleBlocks": 2,
        "maxWallObstacleBlocks": 2,
        "minPushes": 10,
        "maxPushes": 18,
        "minReversePulls": 16,
        "maxReversePulls": 28,
        "style": "balanced workshop",
        "archetype": "goal_room",
        "targetLayout": "clustered",
        "obstacleStyle": "central_baffle",
        "waterStyle": "corner_pool",
        "designNote": "Balanced two-box puzzle.",
        "corridorPlacement": "none",
        "corridorWidth": 0,
        "corridorOrientation": "any",
        "corridorRole": "visual_only",
        "corridorPriority": "preferred",
    }
    plan.update(overrides)
    return plan


# ---------------------------------------------------------------------------
# 1. Chat & Stage Opening split schemas
# ---------------------------------------------------------------------------


def test_chat_and_stage_opening_split_schemas_and_validators() -> None:
    # Backward-compat ChatReplyPayload still exported
    assert "assistantMessage" in ChatReplyPayload.model_json_schema()["properties"]

    # StageAssessmentPayload drops satisfactionQuestion & requires non-blank fields
    assess_schema = StageAssessmentPayload.model_json_schema()
    assert "satisfactionQuestion" not in assess_schema["properties"]
    with pytest.raises(ValidationError):
        StageAssessmentPayload.model_validate({"solutionSummary": "   ", "difficultyOpinion": "Ok"})
    with pytest.raises(ValidationError):
        StageAssessmentPayload.model_validate({"solutionSummary": "Ok", "difficultyOpinion": ""})

    # StageOpeningPayload exposes ONLY assistantMessage, coordinateLinks, required assessment
    opening_schema = StageOpeningPayload.model_json_schema()
    assert set(opening_schema["properties"]) == {
        "assistantMessage",
        "coordinateLinks",
        "assessment",
    }
    assert "assessment" in opening_schema["required"]
    with pytest.raises(ValidationError):
        StageOpeningPayload.model_validate({"assistantMessage": "Hi", "assessment": None})

    # OrdinaryChatReplyPayload omits dead fields and narrows move enum
    ord_schema = OrdinaryChatReplyPayload.model_json_schema()
    for forbidden in ("revisionOffer", "assessment", "disagreement", "followUpQuestion"):
        assert forbidden not in ord_schema["properties"]
    assert "intentDecision" in ord_schema["required"]
    assert set(ord_schema["properties"]["move"]["enum"]) == {
        "clarify_intent",
        "offer_perspective",
        "challenge_tradeoff",
        "reflect_on_play",
    }
    # OrdinaryChatWithDisagreementPayload adds followUpQuestion
    dis_schema = OrdinaryChatWithDisagreementPayload.model_json_schema()
    assert "followUpQuestion" in dis_schema["properties"]

    # IntentDecisionPayload safely normalizes malformed optional intent cards (no 502!)
    downgraded_empty = IntentDecisionPayload.model_validate(
        {"classification": "candidate", "claims": [], "cardText": "Some card"}
    )
    assert downgraded_empty.classification == "none"
    assert downgraded_empty.claims == []
    assert downgraded_empty.card_text == ""

    downgraded_blank_card = IntentDecisionPayload.model_validate(
        {"classification": "candidate", "claims": [_sample_claim()], "cardText": "   "}
    )
    assert downgraded_blank_card.classification == "none"
    assert downgraded_blank_card.claims == []
    assert downgraded_blank_card.card_text == ""

    downgraded_blank_claim = IntentDecisionPayload.model_validate(
        {"classification": "candidate", "claims": [_sample_claim(evidence="   ")], "cardText": "Ok"}
    )
    assert downgraded_blank_claim.classification == "none"
    assert downgraded_blank_claim.claims == []

    cleaned_none = IntentDecisionPayload.model_validate(
        {"classification": "none", "claims": [_sample_claim()], "cardText": "unexpected"}
    )
    assert cleaned_none.claims == []
    assert cleaned_none.card_text == ""

    cleaned_ref = IntentDecisionPayload.model_validate(
        {"classification": "reference_only", "claims": [], "cardText": "unexpected"}
    )
    assert cleaned_ref.card_text == ""

    assert IntentClaimPayload.model_validate(_sample_claim(evidence="   ")).evidence_span == "   "

    # OrdinaryChatReplyPayload clears leaked intentConfidence when classification != candidate
    parsed_ord = OrdinaryChatReplyPayload.model_validate(
        {
            "assistantMessage": "This looks balanced.",
            "move": "offer_perspective",
            "intentDecision": {"classification": "none", "claims": [], "cardText": ""},
            "intentConfidence": "high",
        }
    )
    assert parsed_ord.intent_confidence is None

    # CoordinateLinkPayload & PatchQuestionEntry do NOT hard-reject optional flaws (dropped safely)
    same_pt = CoordinateLinkPayload.model_validate(
        {"text": "loop", "from": {"row": 2, "column": 2}, "to": {"row": 2, "column": 2}}
    )
    assert same_pt.from_coord == same_pt.to_coord
    long_link = CoordinateLinkPayload.model_validate(
        {"text": "x" * 230, "from": {"row": 2, "column": 2}, "to": {"row": 3, "column": 3}}
    )
    assert len(long_link.text) == 230
    unresolved_ev = PatchQuestionEntry.model_validate(
        {"question": "Keep water?", "status": "resolved", "evidenceText": "  "}
    )
    assert unresolved_ev.status == "resolved"


async def test_chat_pipeline_selects_split_schemas_and_joins_multi_grounding_issues() -> None:
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            # 1. Stage opening using StageOpeningPayload
            {
                "assistantMessage": "P starts near B1 with an open lane.",
                "coordinateLinks": [],
                "assessment": {
                    "solutionSummary": "Cleanly solvable.",
                    "difficultyOpinion": "In my view approachable.",
                    "features": ["Central wall"],
                    "suggestions": ["Try opening push"],
                },
            },
            # 2. Ordinary chat attempt 1 fails with 2 grounding mismatches -> joined in repair
            {
                "assistantMessage": "P is at (9,9) and B1 is at (8,8).",
                "move": "offer_perspective",
                "intentDecision": {"classification": "none", "claims": [], "cardText": ""},
                "intentConfidence": None,
                "uiCues": [],
                "coordinateLinks": [],
                "designContextPatch": None,
            },
            # 3. Ordinary chat attempt 2 succeeds
            {
                "assistantMessage": "P is at (3,3) and B1 is at (3,5).",
                "move": "offer_perspective",
                "intentDecision": {"classification": "none", "claims": [], "cardText": ""},
                "intentConfidence": None,
                "uiCues": [],
                "coordinateLinks": [],
                "designContextPatch": None,
            },
            # 4. Ordinary chat with active disagreement accepts followUpQuestion
            {
                "assistantMessage": "Let us align on the central wall.",
                "move": "clarify_intent",
                "followUpQuestion": "Do you want to keep the wall removed?",
                "intentDecision": {"classification": "none", "claims": [], "cardText": ""},
                "intentConfidence": None,
                "uiCues": [],
                "coordinateLinks": [],
                "designContextPatch": None,
            },
        ]
    )
    pipeline = ChatPipeline(transport, model="kimi-k2.6", clock=FakeClock())

    opening = await pipeline.generate_stage_opening(grid, stage_number=2, language="en")
    assert opening.source == "llm"
    opening_props = transport.calls[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert set(opening_props) == {"assistantMessage", "coordinateLinks", "assessment"}

    reply = await pipeline.generate_chat_reply(
        conversation=[{"role": "user", "content": "Where are P and B1?"}],
        grid_or_rows=grid,
        language="en",
    )
    assert reply.attempts_used == 2
    ord_props = transport.calls[1]["response_format"]["json_schema"]["schema"]["properties"]
    assert "followUpQuestion" not in ord_props
    assert "revisionOffer" not in ord_props
    repair_msg = transport.calls[2]["messages"][-1]["content"]
    assert "assistantMessage" in repair_msg
    assert "(9,9)" in repair_msg or "P" in repair_msg
    assert ";" in repair_msg  # multiple grounding issues joined

    dis_reply = await pipeline.generate_chat_reply(
        conversation=[{"role": "user", "content": "About my edit"}],
        grid_or_rows=grid,
        design_context={"activeDisagreement": {"status": "active", "subject": "human_edit"}},
        language="en",
    )
    assert dis_reply.guidance["followUpQuestion"] == "Do you want to keep the wall removed?"
    dis_props = transport.calls[3]["response_format"]["json_schema"]["schema"]["properties"]
    assert "followUpQuestion" in dis_props


# ---------------------------------------------------------------------------
# 2. Turn Routing split & Dedicated Recovery Schema
# ---------------------------------------------------------------------------


def test_turn_routing_split_and_recovery_schemas() -> None:
    base_schema = TurnRoutingPayload.model_json_schema()
    assert "humanEditEngagement" not in base_schema["properties"]
    assert set(base_schema["properties"]["routeTarget"]["enum"]) == {
        "ordinary",
        "binary_discovery",
    }
    assert "gameplay" in base_schema["properties"]["elements"]["items"]["enum"]

    with_he_schema = TurnRoutingWithHumanEditPayload.model_json_schema()
    assert "humanEditEngagement" in with_he_schema["required"]

    # Rejects dead routeTarget values
    with pytest.raises(ValidationError):
        TurnRoutingPayload.model_validate(
            {
                "acts": ["revision_request"],
                "elements": ["gameplay"],
                "mapRelated": True,
                "directionSufficient": True,
                "routeTarget": "revision",
                "evidenceSpan": "change it",
                "changes": [],
            }
        )

    # mapRelated=True requires non-blank evidenceSpan; mapRelated=False permits ""
    with pytest.raises(ValidationError):
        TurnRoutingPayload.model_validate(
            {
                "acts": ["evaluation"],
                "elements": ["gameplay"],
                "mapRelated": True,
                "directionSufficient": False,
                "routeTarget": "ordinary",
                "evidenceSpan": "   ",
                "changes": [],
            }
        )
    off_topic = TurnRoutingPayload.model_validate(
        {
            "acts": ["unclear"],
            "elements": ["gameplay"],
            "mapRelated": False,
            "directionSufficient": False,
            "routeTarget": "ordinary",
            "evidenceSpan": "",
            "changes": [],
        }
    )
    assert off_topic.elements == ["gameplay"]

    # HumanEditEngagementPayload & HumanEditEngagementRecoveryPayload enforce engage -> evidenceSpan
    with pytest.raises(ValidationError):
        HumanEditEngagementPayload.model_validate({"decision": "engage", "evidenceSpan": ""})
    with pytest.raises(ValidationError):
        HumanEditEngagementRecoveryPayload.model_validate(
            {"decision": "engage", "evidenceSpan": "   "}
        )
    rec_schema = HumanEditEngagementRecoveryPayload.model_json_schema()
    assert set(rec_schema["properties"]) == {"decision", "evidenceSpan"}


async def test_route_turn_schema_selection_and_enriched_span_errors() -> None:
    transport = FakeAsyncTransport(
        [
            # 1. Without active human-edit: attempt 1 bad change span -> attempt 2 fixed
            {
                "acts": ["revision_request"],
                "elements": ["water"],
                "mapRelated": True,
                "directionSufficient": False,
                "routeTarget": "binary_discovery",
                "evidenceSpan": "remove water",
                "changes": [
                    {
                        "component": "water",
                        "property": "count",
                        "operation": "change",
                        "evidenceSpan": "NOT VERBATIM",
                    }
                ],
            },
            {
                "acts": ["revision_request"],
                "elements": ["water"],
                "mapRelated": True,
                "directionSufficient": False,
                "routeTarget": "binary_discovery",
                "evidenceSpan": "remove water",
                "changes": [
                    {
                        "component": "water",
                        "property": "count",
                        "operation": "change",
                        "evidenceSpan": "remove water",
                    }
                ],
            },
            # 2. With active human-edit: TurnRoutingWithHumanEditPayload selected
            {
                "acts": ["evaluation"],
                "elements": ["internal_walls"],
                "mapRelated": True,
                "directionSufficient": True,
                "routeTarget": "ordinary",
                "evidenceSpan": "kept that wall",
                "changes": [],
                "humanEditEngagement": {
                    "decision": "engage",
                    "evidenceSpan": "kept that wall",
                },
            },
        ]
    )
    pipeline = UnderstandingPipeline(transport, model="kimi-k2.6", clock=FakeClock())

    res1 = await pipeline.route_turn("Please remove water here.")
    assert res1.attempts_used == 2
    assert not isinstance(res1.payload, TurnRoutingWithHumanEditPayload)
    assert "humanEditEngagement" not in (
        transport.calls[0]["response_format"]["json_schema"]["schema"]["properties"]
    )
    repair_msg = transport.calls[1]["messages"][-1]["content"]
    assert "changes[0].evidenceSpan" in repair_msg
    assert "NOT VERBATIM" in repair_msg

    res2 = await pipeline.route_turn(
        "I kept that wall on purpose.",
        active_disagreement={"status": "active", "subject": "human_edit", "nextQuestion": "Why?"},
    )
    assert isinstance(res2.payload, TurnRoutingWithHumanEditPayload)
    assert res2.payload.human_edit_engagement.decision == "engage"
    assert "humanEditEngagement" in (
        transport.calls[2]["response_format"]["json_schema"]["schema"]["required"]
    )


async def test_recovery_service_uses_dedicated_schema_and_retries_on_bad_span(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    db = SQLiteDatabase(tmp_path / "rec.sqlite3")
    db.initialize()
    sessions_repo = SessionsRepository(db)
    dialogue_repo = DialogueRepository(db)
    op_repo = OperationalRepository(db)
    read_model = SessionReadModelService(sessions_repo, dialogue_repo, op_repo)

    now = clock.utc_now()
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO design_sessions("
            "session_id, mode, status, language, access_token_hash, integration_token_hash, "
            "bootstrap_token_hash, current_version_id, created_at, updated_at"
            ") VALUES (?, 'formal', 'active', 'en', 'a', 'i', 'b', 'v1', ?, ?)",
            ("s1", now, now),
        )
        design_ctx = {
            "activeDisagreement": {
                "status": "active",
                "subject": "human_edit",
                "disagreementId": "dis-1",
                "userPosition": "Edit",
                "aiPosition": "AI",
                "coreDisagreement": "Diff",
                "nextQuestion": "Why?",
            }
        }
        conn.execute(
            "INSERT INTO level_versions("
            "version_id, session_id, version_number, source, summary, rows_json, solver_json, "
            "design_context_json, created_at"
            ") VALUES ('v1', 's1', 1, 'initial', 'Stage 1', ?, '{}', ?, ?)",
            (dump_json(list(SAMPLE_ROWS)), dump_json(design_ctx), now),
        )

    transport = FakeAsyncTransport(
        [
            {"decision": "engage", "evidenceSpan": "NOT IN MESSAGE"},
            {"decision": "engage", "evidenceSpan": "changed my mind"},
        ]
    )
    understanding = UnderstandingPipeline(transport, model="kimi-k2.6", clock=clock)
    rec_svc = RecoveryService(
        db,
        sessions_repo,
        op_repo,
        read_model,
        understanding_pipeline=understanding,
        clock=clock,
    )
    await rec_svc._recover_human_edit_engagement(
        session_id="s1",
        task_base_vid="v1",
        state={"userMessage": "I changed my mind about that wall.", "disagreementId": "dis-1"},
        request_id="req-rec",
        deadline=clock.monotonic() + 10.0,
    )
    assert len(transport.calls) == 2
    schema_props = transport.calls[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert set(schema_props) == {"decision", "evidenceSpan"}
    assert "NOT IN MESSAGE" in transport.calls[1]["messages"][-1]["content"]
    v1 = sessions_repo.get_version("s1", "v1")
    assert v1 is not None
    assert (v1.design_context or {}).get("activeDisagreement") is None


# ---------------------------------------------------------------------------
# 3. Manual-Edit Review split, Classification & Challenge Review
# ---------------------------------------------------------------------------


def test_manual_edit_review_split_and_dynamic_classification_schemas() -> None:
    assert "conflict" in ManualEditReviewPayload.model_json_schema()["properties"]
    no_conflict_schema = ManualEditReviewNoConflictPayload.model_json_schema()
    assert "conflict" not in no_conflict_schema["properties"]

    with_conflict_schema = ManualEditReviewWithConflictPayload.model_json_schema()
    assert "conflict" in with_conflict_schema["required"]
    with pytest.raises(ValidationError):
        ManualEditReviewWithConflictPayload.model_validate(
            {
                "openingMessage": "Stage updated.",
                "assessment": {"solutionSummary": "Ok.", "difficultyOpinion": "Fair."},
                "reviewMessage": "Aligned.",
                "conflict": None,
            }
        )

    # Dynamic conflict classification constrains directionId and supports >24 IDs
    ids = [f"dir-{i}" for i in range(26)]
    dyn_model = build_manual_edit_conflict_classification_model(ids)
    dyn_schema = dyn_model.model_json_schema()
    comp_prop = dyn_schema["properties"]["comparisons"]
    assert comp_prop["minItems"] == 26
    assert comp_prop["maxItems"] == 26
    with pytest.raises(ValidationError):
        dyn_model.model_validate(
            {
                "comparisons": [
                    {"directionId": "unknown-id", "relation": "aligned", "explanation": "x"}
                ]
            }
        )


async def test_understanding_manual_edit_review_classification_and_challenge_validation() -> None:
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            # 1. classify_manual_edit_conflict attempt 1 duplicates dir-1 -> attempt 2 fixes
            {
                "comparisons": [
                    {"directionId": "dir-1", "relation": "aligned", "explanation": "Ok."},
                    {"directionId": "dir-1", "relation": "aligned", "explanation": "Dup."},
                ]
            },
            {
                "comparisons": [
                    {"directionId": "dir-1", "relation": "aligned", "explanation": "Ok."},
                    {"directionId": "dir-2", "relation": "unrelated", "explanation": "Unrelated."},
                ]
            },
            # 2. generate_manual_edit_review (no conflict): attempt 1 has '?' in openingMessage
            {
                "openingMessage": "Does this feel tighter?",
                "assessment": {"solutionSummary": "Solvable.", "difficultyOpinion": "Fair."},
                "reviewMessage": "Wall count stayed balanced.",
            },
            {
                "openingMessage": "The saved Stage keeps both boxes solvable.",
                "assessment": {"solutionSummary": "Solvable.", "difficultyOpinion": "Fair."},
                "reviewMessage": "Wall count stayed balanced.",
            },
            # 3. review_challenge_reason: attempt 1 leaks internal token -> attempt 2 succeeds
            {
                "relation": "primary",
                "merit": "reasonable",
                "comparison": "Inspect entityBindings before editing.",
            },
            {
                "relation": "primary",
                "merit": "reasonable",
                "comparison": "The reason directly addresses the central wall tradeoff.",
            },
        ]
    )
    pipeline = UnderstandingPipeline(transport, model="kimi-k2.6", clock=FakeClock())

    cls_res = await pipeline.classify_manual_edit_conflict(
        directions=[{"id": "dir-1", "fact": "A"}, {"id": "dir-2", "fact": "B"}],
        effect_evidence=[{"id": "diff-1", "fact": "Edited wall"}],
        language="en",
    )
    assert cls_res.attempts_used == 2
    assert cls_res.verdict == "no_conflict"
    cls_repair = transport.calls[1]["messages"][-1]["content"]
    assert "missing=['dir-2']" in cls_repair
    assert "duplicates=['dir-1']" in cls_repair

    rev_res = await pipeline.generate_manual_edit_review(
        grid_or_rows=grid,
        conflict_decision=ManualEditConflictResult(
            "no_conflict", "llm", ("dir-1",), ("diff-1",), ()
        ),
        review_context={"changeSummary": {"changedCellCount": 1}},
        language="en",
    )
    assert rev_res.attempts_used == 2
    assert rev_res.conflict is None
    assert "conflict" not in (
        transport.calls[2]["response_format"]["json_schema"]["schema"]["properties"]
    )
    assert "openingMessage" in transport.calls[3]["messages"][-1]["content"]

    ch_res = await pipeline.review_challenge_reason(
        user_reason="One wall is not enough.",
        proposal_summary="Remove wall",
        primary_hypothesis="Sufficiency",
        secondary_hypothesis="Water",
        language="en",
    )
    assert ch_res.attempts_used == 2
    assert "comparison" in transport.calls[5]["messages"][-1]["content"]


def test_manual_edit_prompts_convert_zero_based_diff_coords_without_mutation() -> None:
    raw_diff_item = {"x": 3, "y": 1, "before": ".", "after": "#"}
    effect_evidence = [
        {
            "id": "diff:v2",
            "kind": "verified_diff",
            "diff": [raw_diff_item],
        }
    ]
    review_context = {
        "stageNumber": 2,
        "diff": [raw_diff_item],
        "changeSummary": {"changedCellCount": 1},
    }

    conflict_msgs = build_manual_edit_conflict_messages(
        directions=[{"id": "dir-1", "fact": "Keep center open"}],
        effect_evidence=effect_evidence,
        language="en",
    )
    conflict_payload = json.loads(conflict_msgs[1]["content"])
    conflict_diff = conflict_payload["effectEvidence"][0]["diff"]
    assert conflict_diff == [{"row": 2, "column": 4, "before": ".", "after": "#"}]
    assert "x" not in conflict_diff[0] and "y" not in conflict_diff[0]

    review_msgs = build_manual_edit_review_messages(
        stage_snapshot={"rows": list(SAMPLE_ROWS)},
        conflict_verdict="no_conflict",
        review_context=review_context,
        language="en",
    )
    assert "1-based (row, column)" in review_msgs[0]["content"]
    assert "reviewContext.diff" in review_msgs[0]["content"]
    review_payload = json.loads(review_msgs[1]["content"])
    review_diff = review_payload["reviewContext"]["diff"]
    assert review_diff == [{"row": 2, "column": 4, "before": ".", "after": "#"}]
    assert "x" not in review_diff[0] and "y" not in review_diff[0]

    # Caller dicts and lists must NOT be mutated
    assert raw_diff_item == {"x": 3, "y": 1, "before": ".", "after": "#"}
    assert effect_evidence[0]["diff"][0] == {"x": 3, "y": 1, "before": ".", "after": "#"}
    assert review_context["diff"][0] == {"x": 3, "y": 1, "before": ".", "after": "#"}


async def test_manual_edit_review_rejects_void_coordinate_and_recovers_on_retry() -> None:
    # Inset playable shell so (row=3, col=1) is VOID ' ' while (row=2, col=4) [x=3, y=1] is '#'
    inset_rows: tuple[str, ...] = (
        "  ########  ",
        "  ##....##  ",
        "  #..p...#  ",
        "  #..s.t.#  ",
        "  #......#  ",
        "  #..s.t.#  ",
        "  #....@.#  ",
        "  #......#  ",
        "  ########  ",
        "            ",
    )
    grid = parse_grid(inset_rows)
    assert grid.tile_at(0, 2) == " "  # row 3, column 1 is VOID
    assert grid.tile_at(3, 1) == "#"  # row 2, column 4 (x=3, y=1) is wall

    # Direct verify_grounding checks on void claims and safe non-void/negated/prose cases
    void_issues = verify_grounding(
        "The internal wall cell changed from floor to wall at row 3, column 1.", grid
    )
    assert any(issue.code == "VOID_TILE_MISMATCH" for issue in void_issues)
    assert any(
        issue.code == "TILE_MISMATCH"
        for issue in verify_grounding("Row 3, column 1 is a wall.", grid)
    )
    for safe_prose in (
        "There is no wall at row 3, column 1.",
        "Add a wall at (3, 5).",
        "Previously P was at (3, 7).",
        "The space outside the wall at row 1, column 1 stays empty.",
        "Beyond the outer wall at row 1, column 1 is exterior void.",
        "You cannot place a wall at row 1, column 1 because it is outside the outer shell.",
        "If you added a wall at (1, 1), it would break the outer shell.",
        "Adding water at (1, 1) would breach the map boundary.",
        "The background at (1, 1) is void.",
        "地图外的边界墙位于第1行第1列旁。",
        "第2行第5列是水平走廊的入口。",
        "第2行第5列是墙体旁边的空地。",
        "Row 2, column 5 is a wall-free corridor.",
        "Row 2, column 5 is the player's first step.",
        "Row 2, column 5 is the start of the upper corridor.",
        "Row 2, column 5 has water nearby.",
        "The internal wall cell changed from wall to floor at row 2, column 5.",
    ):
        assert verify_grounding(safe_prose, grid) == [], safe_prose

    transport = FakeAsyncTransport(
        [
            # Attempt 1: swaps 0-based (x=3, y=1) into void row 3, column 1
            {
                "openingMessage": "Stage 2 adds an interior wall and keeps the puzzle solvable.",
                "assessment": {"solutionSummary": "Solvable.", "difficultyOpinion": "Balanced."},
                "reviewMessage": (
                    "The internal wall cell changed from floor to wall at row 3, column 1, "
                    "tightening the upper lane."
                ),
            },
            # Attempt 2: cites row 2, column 4 plus benign metric/dimension/void-description prose
            {
                "openingMessage": (
                    "Map dimensions (10, 12) remain unchanged. "
                    "Stage 2 keeps (1, 1) as exterior void."
                ),
                "assessment": {"solutionSummary": "Solvable.", "difficultyOpinion": "Balanced."},
                "reviewMessage": (
                    "Solved in fewer steps (24, 6); the internal wall cell changed from floor "
                    "to wall at row 2, column 4, tightening the upper lane."
                ),
            },
        ]
    )
    pipeline = UnderstandingPipeline(transport, model="kimi-k2.6", clock=FakeClock())
    caller_diff = [{"x": 3, "y": 1, "before": ".", "after": "#"}]

    result = await pipeline.generate_manual_edit_review(
        grid_or_rows=grid,
        conflict_decision=ManualEditConflictResult(
            "no_conflict", "llm_adjudication", ("dir-1",), ("diff:v2",), ()
        ),
        review_context={
            "stageNumber": 2,
            "diff": caller_diff,
            "changeSummary": {"changedCellCount": 1},
        },
        language="en",
    )
    assert result.attempts_used == 2
    assert "row 2, column 4" in result.review_message
    assert caller_diff == [{"x": 3, "y": 1, "before": ".", "after": "#"}]

    sent_payload = json.loads(transport.calls[0]["messages"][1]["content"])
    assert sent_payload["reviewContext"]["diff"] == [
        {"row": 2, "column": 4, "before": ".", "after": "#"}
    ]

    repair_msg = transport.calls[1]["messages"][-1]["content"]
    assert "row 3, column 1" in repair_msg
    assert "row 2, column 4" in repair_msg


# ---------------------------------------------------------------------------
# 4. Translation metadata strip/merge & extra="forbid" fix
# ---------------------------------------------------------------------------


def test_translation_slim_schemas_and_verification_validator() -> None:
    dis_props = TranslatedDisagreementTextPayload.model_json_schema()["properties"]
    assert set(dis_props) == {"userPosition", "aiPosition", "coreDisagreement", "nextQuestion"}
    q_props = TranslatedBinaryQuestionTextPayload.model_json_schema()["properties"]
    assert set(q_props) == {"question", "note", "choices"}
    item_props = TranslatedTurnItemPayload.model_json_schema()["properties"]
    assert "proposalSummary" not in item_props

    with pytest.raises(ValidationError):
        TranslationVerificationPayload.model_validate({"accepted": False, "reason": "   "})
    ok_reject = TranslationVerificationPayload.model_validate(
        {"accepted": False, "reason": "Dropped choice B."}
    )
    assert ok_reject.accepted is False


async def test_translation_plain_schema_and_disagreement_metadata_merge() -> None:
    # 1. Plain turn uses PlainTranslationPayload
    plain_transport = FakeAsyncTransport(
        [{"translations": [{"turnId": "t-plain", "body": "你好，世界。"}]}]
    )
    plain_pipeline = TranslationPipeline(plain_transport, model="kimi-k2.6", clock=FakeClock())
    plain_res = await plain_pipeline.translate_turns(
        [{"turnId": "t-plain", "body": "Hello, world.", "uiCueTexts": []}],
        target_language="zh-CN",
    )
    assert plain_res.translations[0]["body"] == "你好，世界。"
    plain_schema = plain_transport.calls[0]["response_format"]["json_schema"]["schema"]
    item_def = plain_schema["$defs"]["TranslatedPlainTurnItemPayload"]
    assert set(item_def["properties"]) == {"turnId", "body"}

    # 2. Structured turn with disagreementId/challengeId/proposalSummary/acceptedReason
    #    MUST NOT crash extra="forbid", strips metadata in prompt, and preserves it on merge!
    turn = ConversationTurnRecord(
        turn_id="t-dis",
        session_id="s1",
        sequence_number=1,
        request_id="r1",
        role="assistant",
        content="Let us compare the proposal.",
        language="en",
        version_id="v1",
        created_at="2026-01-01T00:00:00Z",
        guidance={
            "disagreement": {
                "status": "active",
                "subject": "ai_revision_challenge",
                "phase": "choice_pending",
                "challengeId": "ch-99",
                "disagreementId": "dis-99",
                "userPosition": "Wall change is too small.",
                "aiPosition": "Removing one wall keeps balance.",
                "coreDisagreement": "Scope of central wall removal.",
                "nextQuestion": "Keep this proposal direction?",
                "proposalSummary": "Remove one central wall.",
                "acceptedReason": "Wall change is too small.",
                "displayCard": True,
                "resolution": None,
            },
            "proposalQuestion": {
                "questionId": "q-1",
                "topicId": "top-1",
                "baseVersionId": "v1",
                "question": "Which side first?",
                "note": "Both stay solvable.",
                "number": 1,
                "selectedChoiceId": "c1",
                "status": "selected",
                "choices": [
                    {"choiceId": "c1", "letter": "A", "label": "Open wall"},
                    {"choiceId": "c2", "letter": "B", "label": "Open water"},
                ],
            },
        },
    )
    version = LevelVersionRecord(
        version_id="v1",
        session_id="s1",
        version_number=1,
        source="initial",
        summary="Stage 1",
        rows=list(SAMPLE_ROWS),
        solver={},
        design_context={},
        created_at="2026-01-01T00:00:00Z",
    )
    src_item = _build_translation_item(turn, version)
    assert "proposalSummary" not in src_item

    # Prompt payload sent to LLM strips immutable metadata IDs
    prompt_msgs = build_translation_messages(items=[src_item], target_language="zh-CN")
    prompt_user = json.loads(prompt_msgs[1]["content"])
    prompt_item = prompt_user["items"][0]
    assert set(prompt_item["disagreement"]) == {
        "userPosition",
        "aiPosition",
        "coreDisagreement",
        "nextQuestion",
    }
    assert set(prompt_item["proposalQuestion"]) == {"question", "note", "choices"}
    assert set(prompt_item["proposalQuestion"]["choices"][0]) == {"choiceId", "label"}

    struct_transport = FakeAsyncTransport(
        [
            {
                "translations": [
                    {
                        "turnId": "t-dis",
                        "body": "我们来对比一下这个方案。",
                        "uiCueTexts": [],
                        "disagreement": {
                            "userPosition": "墙体改动太小。",
                            "aiPosition": "移除一面墙能保持平衡。",
                            "coreDisagreement": "中间墙体移除的范围。",
                            "nextQuestion": "是否继续保留该方案方向？",
                        },
                        "proposalQuestion": {
                            "question": "先调整哪一侧？",
                            "note": "两者都可解。",
                            "choices": [
                                {"choiceId": "c1", "label": "清理内墙"},
                                {"choiceId": "c2", "label": "清理水域"},
                            ],
                        },
                    }
                ]
            },
            {"accepted": True, "reason": "Faithful translation."},
        ]
    )
    pipeline = TranslationPipeline(struct_transport, model="kimi-k2.6", clock=FakeClock())
    res = await pipeline.translate_turns([src_item], target_language="zh-CN")
    merged = res.translations[0]
    # Authoritative metadata preserved verbatim!
    assert merged["disagreement"]["challengeId"] == "ch-99"
    assert merged["disagreement"]["disagreementId"] == "dis-99"
    assert merged["disagreement"]["proposalSummary"] == "Remove one central wall."
    assert merged["disagreement"]["acceptedReason"] == "Wall change is too small."
    assert merged["disagreement"]["userPosition"] == "墙体改动太小。"
    assert merged["proposalQuestion"]["questionId"] == "q-1"
    assert merged["proposalQuestion"]["topicId"] == "top-1"
    assert merged["proposalQuestion"]["selectedChoiceId"] == "c1"
    assert merged["proposalQuestion"]["status"] == "selected"
    assert merged["proposalQuestion"]["choices"][0]["letter"] == "A"
    assert merged["proposalQuestion"]["choices"][0]["label"] == "清理内墙"

    applied = _apply_translated_guidance(turn, merged)
    assert applied["disagreement"]["challengeId"] == "ch-99"
    assert applied["proposalQuestion"]["questionId"] == "q-1"


# ---------------------------------------------------------------------------
# 5. Constraint-aware LevelPlanPayload, validation.py fix & DGGuideSummaryPayload
# ---------------------------------------------------------------------------


async def test_level_plan_constrained_schema_preferences_and_validation_error() -> None:
    # Default schema locks water to 1..2 and minWallObstacleBlocks=2, maxWallObstacleBlocks in 2..3
    default_model = build_constrained_level_plan_model()
    with pytest.raises(ValidationError):
        default_model.model_validate(_base_level_plan(minWaterAreas=0, maxWaterAreas=0))
    with pytest.raises(ValidationError):
        default_model.model_validate(_base_level_plan(minWallObstacleBlocks=1))
    assert default_model.model_validate(_base_level_plan()).min_wall_obstacle_blocks == 2

    # no_internal_walls locks walls=0 and corridor fields to none/0/any/visual_only/preferred
    no_wall_model = build_constrained_level_plan_model(no_internal_walls=True)
    nw_schema = no_wall_model.model_json_schema()
    assert nw_schema["properties"]["minWallObstacleBlocks"]["maximum"] == 0
    assert nw_schema["properties"]["corridorWidth"]["maximum"] == 0
    with pytest.raises(ValidationError):
        no_wall_model.model_validate(
            _base_level_plan(
                minWallObstacleBlocks=0,
                maxWallObstacleBlocks=0,
                corridorPlacement="center",
                corridorWidth=1,
            )
        )

    # corridorPlacement='none' with custom preferences preserves valid orientation/role/priority
    no_corr_prefs_model = build_constrained_level_plan_model(
        generation_preferences={
            "corridorPlacement": "none",
            "corridorOrientation": "horizontal",
            "corridorRole": "player_route",
            "corridorPriority": "preferred",
        }
    )
    nc_props = no_corr_prefs_model.model_json_schema()["properties"]
    assert nc_props["corridorOrientation"]["const"] == "horizontal"
    assert nc_props["corridorRole"]["const"] == "player_route"
    assert nc_props["corridorPriority"]["const"] == "preferred"
    assert (
        no_corr_prefs_model.model_validate(
            _base_level_plan(
                corridorPlacement="none",
                corridorWidth=0,
                corridorOrientation="horizontal",
                corridorRole="player_route",
                corridorPriority="preferred",
            )
        ).corridor_role
        == "player_route"
    )

    # Forbidden combinations (required_box_route / required) with placement='none' still fall back
    fallback_no_corr_model = build_constrained_level_plan_model(
        generation_preferences={
            "corridorPlacement": "none",
            "corridorRole": "required_box_route",
            "corridorPriority": "required",
        }
    )
    fb_props = fallback_no_corr_model.model_json_schema()["properties"]
    assert fb_props["corridorRole"]["const"] == "visual_only"
    assert fb_props["corridorPriority"]["const"] == "preferred"

    # validation.py fixed error message (no longer misleading 'can be zero only' for value 1)
    with pytest.raises(ValueError) as exc_info:
        validate_level_plan(_base_level_plan(minWallObstacleBlocks=1, maxWallObstacleBlocks=2))
    assert "minWallObstacleBlocks must be 2" in str(exc_info.value)
    assert "minWallObstacleBlocks=1" in str(exc_info.value)

    # LevelPlanPipeline clears noWater/noInternalWalls when generationPreferences sets non-zero!
    transport = FakeAsyncTransport(
        [
            _base_level_plan(
                minWaterAreas=1,
                maxWaterAreas=1,
                minWallObstacleBlocks=2,
                maxWallObstacleBlocks=2,
            )
        ]
    )
    lp_pipe = LevelPlanPipeline(transport, model="deepseek-chat", clock=FakeClock())
    res = await lp_pipe.generate_plan(
        variation_seed=1,
        creative_context={"originalIdeaText": "no water and no internal walls"},
        generation_preferences={
            "minWaterAreas": 1,
            "maxWaterAreas": 1,
            "minWallObstacleBlocks": 2,
            "maxWallObstacleBlocks": 2,
        },
    )
    assert res.feature_constraints == {"noWater": False, "noInternalWalls": False}
    assert res.plan["minWaterAreas"] == 1
    assert res.plan["minWallObstacleBlocks"] == 2


async def test_dg_guide_constrained_schema_prompt_fix_and_split_errors() -> None:
    # Prompt sentence truncation fixed
    assert "one concise or '我" not in _DG_GUIDE_SYSTEM
    assert "one concise first-person sentence" in _DG_GUIDE_SYSTEM

    # Split length vs sentence-count error messages
    with pytest.raises(ValueError) as short_err:
        normalize_dg_partner_rationale("Too short.", "difficulty", "en")
    assert "too short" in str(short_err.value).lower()
    with pytest.raises(ValueError) as multi_err:
        normalize_dg_partner_rationale(
            "I like quick starts here. I also like simple pushes here.", "difficulty", "en"
        )
    assert "exactly one sentence" in str(multi_err.value).lower()

    # Constrained model narrows allowed enum values and min_length by locale
    en_model = build_constrained_dg_guide_model(
        allowed_difficulties=["Easy"], allowed_layouts=["Compact"], language="en"
    )
    en_props = en_model.model_json_schema()["properties"]
    assert en_props["difficultyRationale"]["minLength"] == 30
    with pytest.raises(ValidationError):
        en_model.model_validate(
            {
                "summary": "Got it! Let us try an easy compact draft.",
                "recommendedDifficulty": "Hard",
                "difficultyRationale": "I think Easy matches quick starts and independent pushes.",
                "recommendedLayout": "Compact",
                "layoutRationale": "I think Compact matches focused space and short paths.",
            }
        )

    zh_model = build_constrained_dg_guide_model(
        allowed_difficulties=["Easy"], allowed_layouts=["Compact"], language="zh-CN"
    )
    assert zh_model.model_json_schema()["properties"]["difficultyRationale"]["minLength"] == 12

    msgs = build_dg_guide_messages(
        {
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
            "language": "en",
        },
        allowed_difficulties=["Easy"],
        allowed_layouts=["Compact"],
    )
    payload = json.loads(msgs[1]["content"])
    assert payload["allowedDifficulties"] == ["Easy"]
    assert payload["allowedLayouts"] == ["Compact"]

    # Pipeline passes constrained schema into transport
    transport = FakeAsyncTransport(
        [
            {
                "summary": (
                    "Got it! Let us start with quick independent moves. "
                    "Everything stays close in one compact area."
                ),
                "recommendedDifficulty": "Easy",
                "difficultyRationale": (
                    "I picked Easy to match quick starts and easy-to-adjust pushes."
                ),
                "recommendedLayout": "Compact",
                "layoutRationale": (
                    "I picked Compact for a focused area with short, direct routes."
                ),
            }
        ]
    )
    dg_pipe = DGGuidePipeline(transport, model="deepseek-chat", clock=FakeClock())
    res = await dg_pipe.create_summary(
        {
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
            "language": "en",
        }
    )
    assert res.source == "llm"
    sent_schema = transport.calls[0]["response_format"]["json_schema"]["schema"]
    assert sent_schema["properties"]["recommendedDifficulty"].get("const") == "Easy"
    assert sent_schema["properties"]["recommendedLayout"].get("const") == "Compact"


# ---------------------------------------------------------------------------
# 6. llm_client schema normalization over real split schemas
# ---------------------------------------------------------------------------


def test_real_split_schemas_normalize_cleanly_for_all_modes() -> None:
    models = (
        StageOpeningPayload,
        OrdinaryChatReplyPayload,
        OrdinaryChatWithDisagreementPayload,
        TurnRoutingPayload,
        TurnRoutingWithHumanEditPayload,
        HumanEditEngagementRecoveryPayload,
        ManualEditReviewNoConflictPayload,
        ManualEditReviewWithConflictPayload,
        PlainTranslationPayload,
    )
    for model in models:
        raw = model.model_json_schema()
        strict = _prepare_schema_for_mode(raw, "json_schema")
        assert strict["additionalProperties"] is False
        assert set(strict["required"]) == set(strict["properties"])
        for def_node in (strict.get("$defs") or {}).values():
            if isinstance(def_node, dict) and "properties" in def_node:
                assert def_node["additionalProperties"] is False
                assert set(def_node["required"]) == set(def_node["properties"])

        compact = _prepare_schema_for_mode(raw, "json_object")
        assert "title" not in compact
        for prop_schema in compact["properties"].values():
            assert "title" not in prop_schema
