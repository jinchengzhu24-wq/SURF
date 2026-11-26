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
from server.core.solver import solve_min_steps
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


def _opening_payload(
    message: str,
    *,
    assessment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "assistantMessage": message,
        "coordinateLinks": [],
        "assessment": assessment,
    }


def _chat_payload(
    message: str,
    *,
    move: str = "offer_perspective",
    intent_decision: dict[str, Any] | None = None,
    intent_confidence: str | None = None,
) -> dict[str, Any]:
    return {
        "assistantMessage": message,
        "move": move,
        "intentDecision": intent_decision
        if intent_decision is not None
        else {"classification": "none", "claims": [], "cardText": ""},
        "intentConfidence": intent_confidence,
        "uiCues": [],
        "coordinateLinks": [],
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
            _opening_payload(
                "This Stage keeps P near B1 with an open lane toward T1.",
                assessment={
                    "solutionSummary": "Cleanly solvable.",
                    "difficultyOpinion": "In my view this feels approachable.",
                    "features": ["Central wall and water"],
                    "suggestions": ["Try the top push first"],
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
                            "preserve": ["water"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": None,
                        },
                    },
                    {
                        "label": "Clear center water",
                        "strategy": {
                            "effect": "open_route",
                            "focus": None,
                            "operators": ["remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": None,
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
                    "question": "先清理哪一类障碍？",
                    "note": "两者都保持地图可解。",
                    "choices": [
                        {"choiceId": "c1", "label": "清理中间内墙"},
                        {"choiceId": "c2", "label": "清理中间水域"},
                    ],
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
    assert transport.calls[0]["max_tokens"] == 4096


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


def test_compute_route_features_and_evaluate_requirements() -> None:
    """Verify witness route features (alternations/firstRuns/entityPositions) & metric eval."""
    rows = [
        "############",
        "#..........#",
        "#..p.......#",
        "#....s..t..#",
        "#..........#",
        "#....s..t..#",
        "#..........#",
        "#..........#",
        "#..........#",
        "############",
    ]
    grid = parse_grid(rows)
    bindings = {
        "identityStatus": "exact",
        "entities": [
            {"id": "P", "label": "P", "kind": "player", "row": 3, "column": 4},
            {"id": "B1", "label": "B1", "kind": "box", "row": 4, "column": 6},
            {"id": "B2", "label": "B2", "kind": "box", "row": 6, "column": 6},
            {"id": "T1", "label": "T1", "kind": "target", "row": 4, "column": 9},
            {"id": "T2", "label": "T2", "kind": "target", "row": 6, "column": 9},
        ],
    }
    clock = FakeClock()
    solve_res = solve_min_steps(grid, deadline_seconds=2.5, clock=clock)
    features = _proposal_eval.compute_route_features(
        grid,
        solve_res,
        entity_bindings=bindings,
        need_min_pushes=False,
        deadline_seconds=2.5,
        clock=clock,
    )
    assert features["routeMode"] == "verified_route_summary"
    assert features["pushBlocks"] >= 2
    assert features["longestPushRun"] >= 1
    assert set(features["firstRuns"]) == {"B1", "B2"}
    assert set(features["interruptions"]) == {"B1", "B2"}
    assert features["entityPositions"]["B1"] == (4, 6)
    assert features["entityPositions"]["B2"] == (6, 6)

    outcomes = _proposal_eval.evaluate_requirements(
        grid.rows,
        grid.rows,
        {
            "requirements": [
                {
                    "requirementId": "req-alt-1",
                    "component": "gameplay",
                    "property": "boxAlternations",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "",
                    "focused": False,
                    "sourceTurnId": "t1",
                    "evidenceSpan": "fewer alternations",
                    "statement": "Fewer box alternations",
                    "scope": "all",
                }
            ]
        },
        {**features, "boxAlternations": 2},
        {**features, "boxAlternations": 0},
    )
    assert outcomes[0]["passed"] is True


async def test_normalize_requirement_record_prunes_protected_invariants_in_compile() -> None:
    """Verify redundant platform-protected invariants are pruned while editable goals remain."""
    normalized, notes = proposal_mod.normalize_requirement_record(
        {
            "requirements": [
                {
                    "component": "player",
                    "property": "count",
                    "relation": "preserve",
                    "strength": "constraint",
                    "entities": ["P"],
                    "sourceTurnId": "turn-1",
                    "evidenceSpan": "keep the player and boxes",
                    "statement": "Preserve player count",
                    "scope": "all",
                },
                {
                    "component": "box",
                    "property": "distance",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": ["box", "B1"],
                    "sourceTurnId": "turn-1",
                    "evidenceSpan": "move B1 closer",
                    "statement": "Move B1 closer to its target",
                    "scope": "all",
                },
                {
                    "component": "wall",
                    "property": "count",
                    "relation": "decrease",
                    "strength": "goal",
                    "entities": [],
                    "sourceTurnId": "turn-1",
                    "evidenceSpan": "simplify the middle",
                    "statement": "Remove a middle wall",
                    "scope": "all",
                },
            ],
            "automaticBindings": [
                {"entity": "box", "role": "box", "row": 1, "column": 1},
                {"entity": "B1", "role": "box", "row": 3, "column": 5},
            ],
            "exactTransitions": [],
        },
        user_turns=[
            {
                "id": "turn-1",
                "content": "keep the player and boxes, move B1 closer, simplify the middle",
            }
        ],
        snapshot={"entities": [{"id": "B1", "role": "box", "row": 3, "column": 5}]},
    )
    assert "dropped_platform_protection" in notes
    assert len(normalized["requirements"]) == 2
    assert normalized["requirements"][0]["entities"] == ["B1"]
    assert len(normalized["automaticBindings"]) == 1
    assert normalized["automaticBindings"][0]["entity"] == "B1"

    clock = FakeClock()
    transport = FakeAsyncTransport(
        [
            {
                "requirements": [
                    {
                        "component": "target",
                        "property": "count",
                        "relation": "preserve",
                        "strength": "invariant",
                        "entities": ["T1"],
                        "value": None,
                        "unit": "none",
                        "priorityEvidenceSpan": "",
                        "focused": False,
                        "sourceTurnId": "t1",
                        "evidenceSpan": "keep targets",
                        "statement": "Keep target count",
                        "scope": "all",
                    },
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
                        "sourceTurnId": "t1",
                        "evidenceSpan": "Reduce water",
                        "statement": "Remove a water tile",
                        "scope": "all",
                    },
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            }
        ]
    )
    pipeline = ProposalPipeline(transport, model="kimi-k2.6", clock=clock)
    compiled = await pipeline.compile_requirements(
        grid_or_rows=SAMPLE_ROWS,
        designer_turns=[{"id": "t1", "content": "Reduce water"}],
    )
    assert len(compiled["requirements"]) == 1
    assert compiled["requirements"][0]["component"] == "water"


async def test_chat_pipeline_strips_unbound_revision_offer_in_ordinary_chat() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)
    bad_payload = _chat_payload("We can open the center lane.")
    bad_payload["revisionOffer"] = {
        "summary": "Remove center wall",
        "rationale": "Clears the middle passage.",
        "objective": "Open lane",
        "changes": [
            {"row": 4, "column": 6, "before": "#", "after": ".", "operation": "remove_wall"}
        ],
        "preserve": ["boxes", "targets", "player"],
        "expectedEffect": "Shorter route",
    }
    clean_payload = _chat_payload("We can open the center lane.")
    transport = FakeAsyncTransport([bad_payload, clean_payload])
    pipeline = ChatPipeline(transport, model="kimi-k2.6", clock=clock)
    reply = await pipeline.generate_chat_reply(
        conversation=[{"role": "user", "content": "Any thoughts?"}],
        grid_or_rows=grid,
        language="en",
    )
    assert reply.attempts_used == 2
    assert reply.guidance["proposalOffer"] is None
    assert transport.calls[0]["max_tokens"] == 3200


async def test_binary_discovery_merges_strategies_normalizes_reqs_checks_lang_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(monotonic_time=10.0)
    grid = parse_grid(SAMPLE_ROWS)

    # First question attempt 1 leaks an internal token in question -> retried;
    # attempt 2 includes a redundant platform-protected invariant in initialRequirements -> pruned!
    t1 = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Should we inspect stageSnapshot or clear water?",
                "note": "Both options are safe.",
                "branches": [
                    {
                        "label": "Clear center wall",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 4, "column": 6, "radius": 1},
                            "operators": ["remove_wall"],
                            "preserve": ["water"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": {"row": 4, "column": 6, "radius": 1},
                        },
                    },
                    {
                        "label": "Clear center water",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
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
                            "statement": "Open center",
                            "scope": "all",
                        }
                    ],
                    "automaticBindings": [],
                    "exactTransitions": [],
                },
            },
            {
                "action": "ask",
                "question": "Which obstacle should we clear first?",
                "note": "Both keep the map solvable.",
                "branches": [
                    {
                        "label": "Clear center wall",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 4, "column": 6, "radius": 1},
                            "operators": ["remove_wall"],
                            "preserve": ["water"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "internal_walls",
                            "focus": {"row": 4, "column": 6, "radius": 1},
                        },
                    },
                    {
                        "label": "Clear center water",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": {
                    "requirements": [
                        {
                            "component": "box",
                            "property": "positions",
                            "relation": "preserve",
                            "strength": "invariant",
                            "entities": ["B1"],
                            "value": None,
                            "unit": "none",
                            "priorityEvidenceSpan": "",
                            "focused": False,
                            "sourceTurnId": "t1",
                            "evidenceSpan": "open up the center",
                            "statement": "Keep box fixed",
                            "scope": "all",
                        },
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
                            "statement": "Open center",
                            "scope": "all",
                        },
                    ],
                    "automaticBindings": [],
                    "exactTransitions": [],
                },
            },
        ]
    )
    b1 = BinaryDiscoveryPipeline(t1, model="kimi-k2.6", clock=clock)
    res1 = await b1.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "Please open up the center"}],
        language="en",
        deadline=10.0 + 116.0,
    )
    assert res1.attempts_used == 2
    assert t1.calls[0]["max_tokens"] == 4096
    assert t1.calls[0]["timeout_seconds"] == pytest.approx(70.0)
    assert len(res1.initial_requirements["requirements"]) == 1

    # Question 2: prior selected choice removes wall (and preserves water);
    # current branch removes water (and preserves walls) -> must merge operators & focus!
    prior_choice = {
        "questionId": "q1",
        "choiceId": "c1",
        "label": "Clear center wall",
        "promise": {
            "kind": "reduce_obstacles",
            "component": "internal_walls",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "corridor": None,
        },
        "strategy": {
            "effect": "open_route",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "operators": ["remove_wall"],
            "preserve": ["water", "boxes", "targets", "player"],
            "editBudget": 1,
            "metricGoals": [],
            "requiredTransitions": [],
            "anchorEntities": [],
            "playObjective": None,
            "displayReason": None,
        },
    }
    t2 = FakeAsyncTransport(
        [
            {
                "action": "ask",
                "question": "Also adjust water or adjust bottom?",
                "note": "Second step.",
                "branches": [
                    {
                        "label": "Also clear water",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                            "operators": ["remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 1,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": {"row": 5, "column": 6, "radius": 1},
                        },
                    },
                    {
                        "label": "Clear water widely",
                        "strategy": {
                            "effect": "open_route",
                            "focus": None,
                            "operators": ["remove_water"],
                            "preserve": ["walls"],
                            "editBudget": 2,
                        },
                        "promise": {
                            "kind": "reduce_obstacles",
                            "component": "water",
                            "focus": None,
                        },
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
            }
        ]
    )
    b2 = BinaryDiscoveryPipeline(t2, model="kimi-k2.6", clock=clock)
    res2 = await b2.run_step(
        grid_or_rows=grid,
        initial_request="open up the center",
        initial_designer_turns=[{"id": "t1", "content": "Please open up the center"}],
        question_count=1,
        selected_choices=[prior_choice],
        verified_initial_requirements=res1.initial_requirements,
        language="en",
        deadline=10.0 + 50.0,
    )
    assert len(res2.branch_candidates) == 2
    # model_deadline = deadline - 20.0 = 40.0; timeout = min(70, 40 - 10) = 30.0
    assert t2.calls[0]["timeout_seconds"] == pytest.approx(30.0)


async def test_proposal_pipeline_merges_mutually_exclusive_branches_and_enforces_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(monotonic_time=5.0)
    grid = parse_grid(SAMPLE_ROWS)
    captured_search_deadlines: list[float | None] = []
    real_search = proposal_mod.search_revision_plan

    def _spy_search(*args: Any, **kwargs: Any) -> Any:
        captured_search_deadlines.append(kwargs.get("deadline"))
        return real_search(*args, **kwargs)

    monkeypatch.setattr(proposal_mod, "search_revision_plan", _spy_search)

    # LLM MapOperations returns a candidate violating preserved_components ("walls") -> rejected;
    # fallback deterministic beam search uses merged joint strategy (remove_wall + remove_water)!
    t = FakeAsyncTransport(
        [
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Try removing wall despite preserved_components",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Retry still removes only wall",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
        ]
    )
    pipeline = ProposalPipeline(t, model="kimi-k2.6", clock=clock)
    precompiled = [
        {
            "effect": "open_route",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "operators": ["remove_wall"],
            "preserve": ["water", "boxes", "targets", "player"],
            "editBudget": 1,
            "metricGoals": [],
            "requiredTransitions": [],
            "anchorEntities": [],
            "playObjective": None,
            "displayReason": None,
        },
        {
            "effect": "open_route",
            "focus": {"row": 5, "column": 6, "radius": 1},
            "operators": ["remove_water"],
            "preserve": ["walls", "boxes", "targets", "player"],
            "editBudget": 1,
            "metricGoals": [],
            "requiredTransitions": [],
            "anchorEntities": [],
            "playObjective": None,
            "displayReason": None,
        },
    ]
    promises = [
        {
            "kind": "reduce_obstacles",
            "component": "internal_walls",
            "focus": {"row": 4, "column": 6, "radius": 1},
            "corridor": None,
        },
        {
            "kind": "reduce_obstacles",
            "component": "water",
            "focus": {"row": 5, "column": 6, "radius": 1},
            "corridor": None,
        },
    ]
    res = await pipeline.generate_proposal(
        grid_or_rows=grid,
        authorized_brief="Clear wall and water",
        precompiled_strategies=precompiled,
        binary_promises=promises,
        deadline=5.0 + 60.0,
    )
    assert res.source == "deterministic_beam_search"
    assert len(res.changes) == 2
    assert captured_search_deadlines == [pytest.approx(25.0)]

    # Verify preserved_components rejects LLM MapOperationsPayload editing preserved component
    t_pres = FakeAsyncTransport(
        [
            {
                "strategies": [
                    {
                        "effect": "open_route",
                        "focus": None,
                        "operators": ["remove_wall", "remove_water"],
                        "preserve": ["boxes", "targets", "player"],
                        "editBudget": 2,
                        "metricGoals": [],
                        "requiredTransitions": [],
                        "anchorEntities": [],
                        "playObjective": None,
                        "displayReason": None,
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Touches wall even though walls preserved",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "strategyIndex": 1,
                        "rationale": "Retry still touches wall even though walls preserved",
                        "operations": [
                            {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                        ],
                    }
                ]
            },
        ]
    )
    p_pres = ProposalPipeline(t_pres, model="kimi-k2.6", clock=clock)
    res_pres = await p_pres.generate_proposal(
        grid_or_rows=grid,
        authorized_brief="Open route without touching walls",
        preserved_components=["walls"],
    )
    assert res_pres.source == "deterministic_beam_search"
    assert all(ch.before != "#" for ch in res_pres.changes)


async def test_pipeline_max_tokens_budgets() -> None:
    clock = FakeClock()
    grid = parse_grid(SAMPLE_ROWS)

    dg_t = FakeAsyncTransport(
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
            }
        ]
    )
    await DGGuidePipeline(dg_t, model="deepseek-v4-flash", clock=clock).create_summary(
        {
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
            "language": "en",
        }
    )
    assert dg_t.calls[0]["max_tokens"] == 2048

    u_t = FakeAsyncTransport(
        [
            {
                "acts": ["evaluation"],
                "elements": ["internal_walls"],
                "mapRelated": True,
                "directionSufficient": True,
                "routeTarget": "ordinary",
                "evidenceSpan": "center",
                "changes": [],
            },
            {
                "comparisons": [
                    {"directionId": "d1", "relation": "aligned", "explanation": "Aligned."}
                ]
            },
        ]
    )
    up = UnderstandingPipeline(u_t, model="kimi-k2.6", clock=clock)
    await up.route_turn("Check the center")
    await up.classify_manual_edit_conflict(
        directions=[{"id": "d1", "fact": "Keep open"}],
        effect_evidence=[{"id": "e1", "fact": "Removed wall"}],
    )
    assert u_t.calls[0]["max_tokens"] == 1024
    assert u_t.calls[1]["max_tokens"] == 2400

    tr_t = FakeAsyncTransport(
        [
            {
                "translations": [
                    {
                        "turnId": f"t{i}",
                        "body": f"译文{i}",
                        "uiCueTexts": [],
                        "proposalVerifiedActions": ["动作"] if i == 0 else None,
                    }
                    for i in range(4)
                ]
            },
            {"accepted": True, "reason": "OK"},
        ]
    )
    # 4 items -> min(8192, max(3200, 1400 * 4)) = 5600; verification -> 600
    tr_items = [
        {
            "turnId": f"t{i}",
            "body": f"Line {i}",
            "uiCueTexts": [],
            **({"proposalVerifiedActions": ["Do"]} if i == 0 else {}),
        }
        for i in range(4)
    ]
    await TranslationPipeline(tr_t, model="kimi-k2.6", clock=clock).translate_turns(
        tr_items, target_language="zh-CN"
    )
    assert tr_t.calls[0]["max_tokens"] == 5600
    assert tr_t.calls[1]["max_tokens"] == 600

    prop_t = FakeAsyncTransport(
        [
            {
                "requirements": [
                    {
                        "component": "wall",
                        "property": "count",
                        "relation": "decrease",
                        "strength": "goal",
                        "entities": [],
                        "value": None,
                        "unit": "none",
                        "priorityEvidenceSpan": "",
                        "focused": False,
                        "sourceTurnId": "t1",
                        "evidenceSpan": "Reduce wall",
                        "statement": "Remove wall",
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            }
        ]
    )
    await ProposalPipeline(prop_t, model="kimi-k2.6", clock=clock).compile_requirements(
        grid_or_rows=grid,
        designer_turns=[{"id": "t1", "content": "Reduce wall"}],
    )
    assert prop_t.calls[0]["max_tokens"] == 3600
