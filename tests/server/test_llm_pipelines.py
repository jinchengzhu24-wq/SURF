"""Zero-paid-LLM unit tests for server/llm pipelines, schemas, and grounding."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from server.core.clock import FakeClock
from server.core.grid import parse_grid
from server.core.llm_client import LLMValidationError, TransportResponse, UpstreamLLMError
from server.core.solver import solve_min_pushes as real_solve_min_pushes
from server.llm import (
    BinaryDiscoveryPipeline,
    ChatPipeline,
    DGGuidePipeline,
    LevelPlanPipeline,
    ProposalPipeline,
    TranslationPipeline,
    UnderstandingPipeline,
    extract_coordinate_links,
    sanitize_visible_text,
    validate_visible_language,
    verify_grounding,
)
from server.llm.pipelines import _proposal_eval
from server.llm.pipelines import proposal as proposal_mod

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

    def __init__(self, responses: Sequence[dict[str, Any] | str | Exception]) -> None:
        self._queue: list[dict[str, Any] | str | Exception] = list(responses)
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
            prompt_tokens=12,
            completion_tokens=24,
            total_tokens=36,
        )


def _chat_payload(
    message: str,
    *,
    move: str = "offer_perspective",
    intent_decision: dict[str, Any] | None = None,
    intent_confidence: str | None = None,
    assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "assistantMessage": message,
        "move": move,
        "followUpQuestion": None,
        "intentDecision": intent_decision,
        "intentConfidence": intent_confidence,
        "revisionOffer": None,
        "disagreement": None,
        "uiCues": [],
        "coordinateLinks": [],
        "assessment": assessment,
        "designContextPatch": None,
    }


# ---------------------------------------------------------------------------
# ChatPipeline
# ---------------------------------------------------------------------------


async def test_chat_pipeline_stage_opening_and_ordinary_structured_json() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            _chat_payload(
                "This Stage keeps P near B1 with an open lane toward T1.",
                move="observe_stage",
                assessment={
                    "solutionSummary": "Cleanly solvable.",
                    "difficultyOpinion": "In my view this feels approachable.",
                    "features": ["Central wall and water"],
                    "suggestions": ["Try the top push first"],
                    "satisfactionQuestion": None,
                },
            ),
            _chat_payload(
                "Pushing B1 toward T1 opens the upper corridor while keeping the lower box free.",
                intent_decision={
                    "classification": "candidate",
                    "claims": [
                        {
                            "normalizedMeaning": "Keep upper lane uncluttered",
                            "subjectType": "route",
                            "subjectText": "upper lane",
                            "attributeType": "clarity",
                            "attributeText": "uncluttered",
                            "direction": "increase",
                            "degree": "neutral",
                            "aspect": "gameplay",
                            "scopeType": "stage",
                            "scopeText": "stage",
                            "evidenceSpan": "keep the top open",
                        }
                    ],
                    "cardText": "It sounds like you want the upper lane to stay open.",
                },
                intent_confidence="medium",
            ),
        ]
    )
    pipeline = ChatPipeline(transport, model="kimi-k2.6", clock=clock)

    opening = await pipeline.generate_stage_opening(grid, stage_number=1, language="en")
    assert opening.source == "llm"
    assert "<GUIDANCE>" not in opening.assistant_message
    assert "Share a reaction or try playing" in opening.assistant_message
    assert transport.calls[0]["response_format"]["type"] == "json_schema"
    assert transport.calls[0]["response_format"]["json_schema"]["strict"] is True

    reply = await pipeline.generate_chat_reply(
        conversation=[{"role": "user", "content": "I want to keep the top open."}],
        grid_or_rows=grid,
        language="en",
    )
    assert "<GUIDANCE>" not in reply.assistant_message
    assert reply.guidance["intentHypothesis"] == (
        "It sounds like you want the upper lane to stay open."
    )
    assert len(reply.guidance["coordinateLinks"]) == 1
    assert reply.guidance["coordinateLinks"][0]["from"] == {"row": 3, "column": 5}
    assert reply.guidance["coordinateLinks"][0]["to"] == {"row": 3, "column": 8}


async def test_chat_pipeline_stage_opening_snapshot_fallback_and_ordinary_no_fallback() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)

    opening_transport = FakeAsyncTransport(
        [
            UpstreamLLMError("UPSTREAM_TIMEOUT", "timeout 1", retryable=True),
            UpstreamLLMError("UPSTREAM_TIMEOUT", "timeout 2", retryable=True),
        ]
    )
    pipeline = ChatPipeline(opening_transport, model="kimi-k2.6", clock=clock)
    fallback = await pipeline.generate_stage_opening(
        grid,
        stage_number=1,
        language="zh-CN",
        solver_metrics={"solutionSteps": 10, "solutionPushes": 4},
    )
    assert fallback.source == "snapshot_fallback"
    assert "Stage 1" in fallback.assistant_message
    assert "先聊聊或试玩" in fallback.assistant_message

    ordinary_transport = FakeAsyncTransport(
        [
            UpstreamLLMError("UPSTREAM_TIMEOUT", "t1", retryable=True),
            UpstreamLLMError("UPSTREAM_TIMEOUT", "t2", retryable=True),
            UpstreamLLMError("UPSTREAM_TIMEOUT", "t3", retryable=True),
        ]
    )
    ordinary_pipeline = ChatPipeline(ordinary_transport, model="kimi-k2.6", clock=clock)
    with pytest.raises(UpstreamLLMError):
        await ordinary_pipeline.generate_chat_reply(
            conversation=[{"role": "user", "content": "What do you think?"}],
            grid_or_rows=grid,
            language="en",
        )


# ---------------------------------------------------------------------------
# UnderstandingPipeline
# ---------------------------------------------------------------------------


async def test_understanding_pipeline_routing_manual_edit_and_challenge() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            {
                "acts": ["revision_request"],
                "elements": ["boxes"],
                "mapRelated": True,
                "directionSufficient": True,
                "routeTarget": "binary_discovery",
                "evidenceSpan": "move B1 right",
                "changes": [
                    {
                        "component": "boxes",
                        "property": "position",
                        "operation": "change",
                        "evidenceSpan": "move B1 right",
                    }
                ],
                "humanEditEngagement": None,
            },
            {
                "comparisons": [
                    {
                        "directionId": "dir-1",
                        "relation": "conflict",
                        "explanation": "Adding walls narrows the space instead of opening it.",
                    }
                ]
            },
            {
                "openingMessage": "The saved Stage keeps both boxes solvable.",
                "assessment": {
                    "solutionSummary": "Solvable.",
                    "difficultyOpinion": "In my view tighter now.",
                    "features": ["Central obstacle"],
                    "suggestions": ["Compare the opening move"],
                    "satisfactionQuestion": None,
                },
                "reviewMessage": "This edit adds a barrier where you earlier wanted open space.",
                "conflict": {
                    "userPosition": "You wanted the middle more open.",
                    "aiPosition": "The saved edit makes the center tighter.",
                    "coreDisagreement": (
                        "The new obstacle reverses the earlier open-space priority."
                    ),
                    "nextQuestion": "Would you like to revise the edit or switch priorities?",
                },
            },
            {
                "relation": "primary",
                "merit": "reasonable",
                "comparison": "The designer directly questions whether removing one wall suffices.",
            },
        ]
    )
    pipeline = UnderstandingPipeline(transport, model="kimi-k2.6", clock=clock)

    routing = await pipeline.route_turn("Please move B1 right")
    assert routing.protected_change_requested is True
    assert routing.protected_entities == ("boxes",)

    # Deterministic short-circuit when no directions exist
    no_dir = await pipeline.classify_manual_edit_conflict(directions=[], effect_evidence=[])
    assert no_dir.verdict == "unrelated"
    assert no_dir.decision_source == "deterministic_no_comparable_evidence"

    conflict = await pipeline.classify_manual_edit_conflict(
        directions=[{"id": "dir-1", "fact": "Keep center open"}],
        effect_evidence=[{"id": "diff-1", "fact": "Added wall"}],
        language="en",
    )
    assert conflict.verdict == "conflict"
    assert conflict.confirmed_direction_ids == ("dir-1",)

    review = await pipeline.generate_manual_edit_review(
        grid_or_rows=grid,
        conflict_decision=conflict,
        review_context={"changeSummary": {"changedCellCount": 1}},
        language="en",
    )
    assert review.conflict is not None
    assert review.conflict["subject"] == "human_edit"

    challenge = await pipeline.review_challenge_reason(
        user_reason="Removing one wall does not change push order.",
        proposal_summary="Remove center wall",
        primary_hypothesis="Sufficiency of single-wall removal",
        secondary_hypothesis="Preservation of water",
    )
    assert challenge.relation == "primary"
    assert challenge.merit == "reasonable"


# ---------------------------------------------------------------------------
# ProposalPipeline (lazy solve_min_pushes + LLM ops + beam-search fallback)
# ---------------------------------------------------------------------------


async def test_proposal_pipeline_lazy_min_pushes_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    push_calls: list[int] = []

    def _spy_min_pushes(*args: Any, **kwargs: Any) -> Any:
        push_calls.append(1)
        return real_solve_min_pushes(*args, **kwargs)

    monkeypatch.setattr(_proposal_eval, "solve_min_pushes", _spy_min_pushes)
    monkeypatch.setattr(proposal_mod, "solve_min_pushes", _spy_min_pushes)

    # Case 1: Requirements do NOT mention pushes -> solve_min_pushes MUST NOT be called!
    t1 = FakeAsyncTransport(
        [
            {
                "strategies": [
                    {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_wall"],
                        "preserve": [
                            "outer_shell",
                            "player",
                            "boxes",
                            "targets",
                            "water",
                            "unrelated_areas",
                        ],
                        "editBudget": 2,
                        "metricGoals": [],
                        "requiredTransitions": [],
                        "anchorEntities": [],
                        "playObjective": "Open center",
                        "displayReason": "Remove central wall",
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Remove wall at (4,6)",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
        ]
    )
    p1 = ProposalPipeline(t1, model="kimi-k2.6", clock=clock)
    res1 = await p1.generate_proposal(grid_or_rows=grid, authorized_brief="Clear the middle wall")
    assert res1.source == "llm_operations"
    assert res1.diagnostics["lazyMinPushes"] is False
    assert len(push_calls) == 0
    assert len(res1.changes) == 1

    # Case 2: Requirements mention minimumPushes AND LLM map ops fail -> fallback beam search
    # runs AND solve_min_pushes IS called lazily!
    t2 = FakeAsyncTransport(
        [
            {
                "strategies": [
                    {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_wall"],
                        "preserve": [
                            "outer_shell",
                            "player",
                            "boxes",
                            "targets",
                            "water",
                            "unrelated_areas",
                        ],
                        "editBudget": 2,
                        "metricGoals": [
                            {"metric": "minimumPushes", "direction": "preserve", "minimumDelta": 1}
                        ],
                        "requiredTransitions": [],
                        "anchorEntities": [],
                        "playObjective": "Keep pushes while clearing wall",
                        "displayReason": "Remove wall",
                    }
                ]
            },
            UpstreamLLMError("UPSTREAM_TIMEOUT", "ops timeout 1", retryable=True),
            UpstreamLLMError("UPSTREAM_TIMEOUT", "ops timeout 2", retryable=True),
        ]
    )
    p2 = ProposalPipeline(t2, model="kimi-k2.6", clock=clock)
    res2 = await p2.generate_proposal(
        grid_or_rows=grid,
        authorized_brief="Open center without changing minimum pushes",
    )
    assert res2.source == "deterministic_beam_search"
    assert res2.diagnostics["lazyMinPushes"] is True
    assert len(push_calls) > 0
    assert res2.solve_result.solvable is True


# ---------------------------------------------------------------------------
# BinaryDiscoveryPipeline (Policy-2 only + deterministic preflight)
# ---------------------------------------------------------------------------


async def test_binary_discovery_pipeline_ask_preflight_and_protected_refusal() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    transport = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Which obstacle should we clear first?",
                "note": "Both options keep the Stage solvable.",
                "branches": [
                    {
                        "label": "Clear center wall",
                        "strategy": {
                            "effect": "open_route",
                            "focus": None,
                            "operators": ["remove_wall"],
                            "preserve": [
                                "outer_shell",
                                "player",
                                "boxes",
                                "targets",
                                "water",
                                "unrelated_areas",
                            ],
                            "editBudget": 2,
                            "metricGoals": [],
                            "requiredTransitions": [],
                            "anchorEntities": [],
                            "playObjective": None,
                            "displayReason": None,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": None,
                            "corridor": None,
                        },
                    },
                    {
                        "label": "Clear center water",
                        "strategy": {
                            "effect": "open_route",
                            "focus": None,
                            "operators": ["remove_water"],
                            "preserve": [
                                "outer_shell",
                                "player",
                                "boxes",
                                "targets",
                                "walls",
                                "unrelated_areas",
                            ],
                            "editBudget": 2,
                            "metricGoals": [],
                            "requiredTransitions": [],
                            "anchorEntities": [],
                            "playObjective": None,
                            "displayReason": None,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": None,
                            "corridor": None,
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": {
                    "requirements": [
                        {
                            "component": "gameplay",
                            "property": "experience",
                            "relation": "seek",
                            "strength": "goal",
                            "entities": [],
                            "value": None,
                            "unit": "none",
                            "priorityEvidenceSpan": "",
                            "focused": False,
                            "sourceTurnId": "t1",
                            "evidenceSpan": "open up the center",
                            "statement": "Open up the center",
                            "scope": "all",
                        }
                    ],
                    "automaticBindings": [],
                    "exactTransitions": [],
                },
            },
            {
                "action": "protected_refusal",
                "question": "",
                "note": "",
                "branches": [],
                "planStrategies": [],
                "protectedEntities": ["boxes"],
                "initialRequirements": None,
            },
        ]
    )
    pipeline = BinaryDiscoveryPipeline(transport, model="kimi-k2.6", clock=clock)

    ask_res = await pipeline.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "Please open up the center"}],
        language="en",
    )
    assert ask_res.step["action"] == "ask"
    assert len(ask_res.branch_candidates) == 2
    assert all(c.solve_result.solvable for c in ask_res.branch_candidates)

    refusal_res = await pipeline.run_step(
        grid_or_rows=grid,
        initial_request="move the box left",
        initial_designer_turns=[{"id": "t2", "content": "move the box left"}],
        language="en",
    )
    assert refusal_res.step["action"] == "protected_refusal"
    assert refusal_res.refusal_message is not None
    assert "boxes" in refusal_res.refusal_message.lower()


# ---------------------------------------------------------------------------
# TranslationPipeline (independent generation vs verification budgets!)
# ---------------------------------------------------------------------------


async def test_translation_pipeline_independent_generation_and_verification_budgets() -> None:
    """Generation retry on attempt 1 MUST NOT exhaust independent verification budget."""
    clock = FakeClock()
    source_item = {
        "turnId": "t1",
        "body": "Choose which obstacle to clear.",
        "uiCueTexts": [],
        "proposalQuestion": {
            "questionId": "q1",
            "topicId": "top1",
            "baseVersionId": "v1",
            "question": "Which obstacle first?",
            "note": "Both keep the map solvable.",
            "number": 1,
            "choices": [
                {"choiceId": "c1", "letter": "A", "label": "Clear center wall"},
                {"choiceId": "c2", "letter": "B", "label": "Clear center water"},
            ],
            "selectedChoiceId": None,
            "status": "pending",
        },
    }
    valid_translation = {
        "translations": [
            {
                "turnId": "t1",
                "body": "请先选择清理哪个障碍。",
                "uiCueTexts": [],
                "proposalQuestion": {
                    "questionId": "q1",
                    "topicId": "top1",
                    "baseVersionId": "v1",
                    "question": "先清理哪一类障碍？",
                    "note": "两者都保持地图可解。",
                    "number": 1,
                    "choices": [
                        {"choiceId": "c1", "letter": "A", "label": "清理中间内墙"},
                        {"choiceId": "c2", "letter": "B", "label": "清理中间水域"},
                    ],
                    "selectedChoiceId": None,
                    "status": "pending",
                },
            }
        ]
    }
    # 1. Gen attempt 1 fails (wrong turnId)
    # 2. Gen attempt 2 succeeds (exhausting gen budget 2/2)
    # 3. Verify attempt 1 fails (malformed JSON)
    # 4. Verify attempt 2 succeeds (proving verification has its own independent 2-attempt budget!)
    transport = FakeAsyncTransport(
        [
            {"translations": [{"turnId": "WRONG_ID", "body": "bad", "uiCueTexts": []}]},
            valid_translation,
            "not-valid-json",
            {"accepted": True, "reason": "Faithful and preserves permissions."},
        ]
    )
    pipeline = TranslationPipeline(
        transport,
        model="kimi-k2.6",
        clock=clock,
        generation_max_attempts=2,
        verification_max_attempts=2,
    )
    result = await pipeline.translate_turns([source_item], target_language="zh-CN")
    assert result.generation_attempts == 2
    assert result.verification_attempts == 2
    assert result.verified_independently is True
    assert result.translations[0]["turnId"] == "t1"
    assert len(transport.calls) == 4


async def test_translation_pipeline_verifier_rejection_raises() -> None:
    clock = FakeClock()
    source_item = {
        "turnId": "t1",
        "body": "Clear a wall.",
        "uiCueTexts": [],
        "proposalVerifiedActions": ["Clear center wall"],
    }
    transport = FakeAsyncTransport(
        [
            {
                "translations": [
                    {
                        "turnId": "t1",
                        "body": "移动箱子。",
                        "uiCueTexts": [],
                        "proposalVerifiedActions": ["移动箱子"],
                    }
                ]
            },
            {"accepted": False, "reason": "Mentions moving boxes instead of clearing a wall."},
        ]
    )
    pipeline = TranslationPipeline(transport, model="kimi-k2.6", clock=clock)
    with pytest.raises(LLMValidationError) as exc_info:
        await pipeline.translate_turns([source_item], target_language="zh-CN")
    assert exc_info.value.code == "TRANSLATION_VERIFICATION_REJECTED"


# ---------------------------------------------------------------------------
# DGGuidePipeline
# ---------------------------------------------------------------------------


async def test_dg_guide_pipeline_aligned_prefix_unspaced_zh_and_fallback() -> None:
    clock = FakeClock()
    transport = FakeAsyncTransport(
        [
            {
                "summary": (
                    "We can keep the opening punchy with independent pushes. "
                    "Key spots stay together in one room."
                ),
                "recommendedDifficulty": "Easy",
                "difficultyRationale": (
                    "Quick start and independent pushes align cleanly with Easy complexity."
                ),
                "recommendedLayout": "Compact",
                "layoutRationale": "Focused spots and short paths suit a Compact layout.",
            },
            {
                # Unspaced multi-sentence Chinese MUST pass without falling back!
                "summary": "第一句直接开局，推动彼此独立。第二句关键位置集中在一个紧凑空间里。",
                "recommendedDifficulty": "Easy",
                "difficultyRationale": "首步快速开始加上独立推动适合简单规划难度。",
                "recommendedLayout": "Compact",
                "layoutRationale": "集中空间加上短路径适合紧凑布局。",
            },
            LLMValidationError("MODEL_JSON_INVALID", "simulated failure"),
        ]
    )
    pipeline = DGGuidePipeline(transport, model="deepseek-chat", clock=clock)
    base_ctx = {
        "firstMovePreference": "quick_start",
        "pushPlanningPreference": "easy_to_adjust",
        "spacePreference": "focused_area",
        "routeRhythmPreference": "short_routes",
    }

    en_res = await pipeline.create_summary({**base_ctx, "language": "en"})
    assert en_res.source == "llm"
    assert en_res.summary.startswith("Got it, ")
    assert "My read is" not in en_res.summary

    zh_res = await pipeline.create_summary({**base_ctx, "language": "zh-CN"})
    assert zh_res.source == "llm"
    assert zh_res.summary.startswith("明白，")
    assert "我" in zh_res.difficulty_rationale

    fb_res = await pipeline.create_summary({**base_ctx, "language": "zh-CN"})
    assert fb_res.source == "deterministic_fallback"
    assert fb_res.recommended_difficulty == "Easy"
    assert fb_res.recommended_layout == "Compact"


# ---------------------------------------------------------------------------
# LevelPlanPipeline & Grounding
# ---------------------------------------------------------------------------


async def test_level_plan_pipeline_honors_zero_water_and_preferences() -> None:
    clock = FakeClock()
    transport = FakeAsyncTransport(
        [
            {
                "minSolutionSteps": 22,
                "maxSolutionSteps": 38,
                "minWaterAreas": 0,
                "maxWaterAreas": 0,
                "minWallObstacleBlocks": 2,
                "maxWallObstacleBlocks": 2,
                "minPushes": 10,
                "maxPushes": 20,
                "minReversePulls": 16,
                "maxReversePulls": 30,
                "style": "dry compact goal room",
                "archetype": "goal_room",
                "targetLayout": "clustered",
                "obstacleStyle": "central_baffle",
                "waterStyle": "corner_pool",
                "designNote": "Dry compact room with central wall pressure.",
                "corridorPlacement": "none",
                "corridorWidth": 0,
                "corridorOrientation": "any",
                "corridorRole": "visual_only",
                "corridorPriority": "preferred",
            }
        ]
    )
    pipeline = LevelPlanPipeline(transport, model="deepseek-chat", clock=clock)
    res = await pipeline.generate_plan(
        variation_seed=7,
        creative_context={"originalIdeaText": "Tight puzzle with no water"},
        generation_preferences={"archetype": "goal_room"},
    )
    assert res.feature_constraints["noWater"] is True
    assert res.plan["minWaterAreas"] == 0
    assert res.plan["maxWaterAreas"] == 0
    assert res.plan["archetype"] == "goal_room"


def test_grounding_precompiled_regexes_coordinate_links_and_visible_language() -> None:
    # AST check: ZERO re.compile() inside any function under server/llm/
    llm_root = Path("server/llm")
    for path in sorted(llm_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "compile"
                    ):
                        pytest.fail(f"Found re.compile() inside {node.name} in {path}")

    grid = parse_grid(SAMPLE_ROWS)
    assert verify_grounding("P is at (3,3) and B1 is at (3,5).", grid) == []
    assert verify_grounding("Previously P was at (9,9).", grid) == []
    mismatch = verify_grounding("P is at (9,9).", grid)
    assert len(mismatch) == 1 and mismatch[0].code == "ENTITY_POSITION_MISMATCH"

    links = extract_coordinate_links("Push B1 toward T1 along row 3.", grid)
    assert len(links) == 1
    assert links[0]["from"] == {"row": 3, "column": 5}
    assert links[0]["to"] == {"row": 3, "column": 8}

    cleaned = sanitize_visible_text("Inspect `solutionSteps` on the first level.", "en")
    assert "solutionSteps" not in cleaned
    assert "this Stage" in cleaned

    # Natural English/names are NOT rejected; leaked internal schema tokens ARE detected.
    assert validate_visible_language("在 Stage 1 里讨论 Sokoban 布局和 P/B1/T1。", "zh-CN") == []
    leaks = validate_visible_language("查看 designContextPatch 和 stageSnapshot。", "zh-CN")
    assert len(leaks) == 2
