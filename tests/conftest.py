"""Shared pytest fixtures, fake async LLM transports, and JSON payload builders."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from server.container import AppContainer, build_container
from server.core.clock import FakeClock
from server.core.llm_client import TransportResponse
from server.main import create_app
from server.settings import SokobanSettings

BASE_ROWS: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@.....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

ALT_ROWS: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#..........#",
    "#....@.....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


class FakeLLMTransport:
    """Deterministic fake async LLM transport with zero private attributes."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses: deque[Any] = deque(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.closed: bool = False

    def queue(self, *items: Any) -> None:
        self.responses.extend(items)

    async def complete(self, **kwargs: Any) -> TransportResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeLLMTransport received an unexpected LLM call.")
        item = self.responses.popleft()
        if isinstance(item, Exception):
            raise item
        content = item(kwargs) if callable(item) else item
        return TransportResponse(content=content, finish_reason="stop")

    async def close(self) -> None:
        self.closed = True


@dataclass
class AppTestBundle:
    """Unified FastAPI app wired to FakeClock, temp SQLite, and fake LLM transports."""

    app: Any
    settings: SokobanSettings
    container: AppContainer
    clock: FakeClock
    deepseek_transport: FakeLLMTransport
    cocreation_transport: FakeLLMTransport

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def build_test_app_bundle(
    tmp_path: Path,
    *,
    token_secret: str = "unit-test-strong-secret-key-987654",
    delete_password: str = "delete-pass-123",
    deepseek_responses: list[Any] | None = None,
    cocreation_responses: list[Any] | None = None,
) -> AppTestBundle:
    """Build an isolated FastAPI test app with fake transports and temp SQLite DB."""
    clock = FakeClock()
    deepseek_transport = FakeLLMTransport(deepseek_responses)
    cocreation_transport = FakeLLMTransport(cocreation_responses)
    settings = SokobanSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "test_sokoban.sqlite3",
        study_logs_dir=tmp_path / "study_logs",
        token_secret=token_secret,
        dashboard_delete_password=delete_password,
        reaper_enabled=False,
    )
    container = build_container(
        settings=settings,
        clock=clock,
        deepseek_transport=deepseek_transport,
        cocreation_transport=cocreation_transport,
    )
    container.db.initialize()
    app = create_app(settings=settings, container=container)
    return AppTestBundle(
        app=app,
        settings=settings,
        container=container,
        clock=clock,
        deepseek_transport=deepseek_transport,
        cocreation_transport=cocreation_transport,
    )


@pytest.fixture()
def app_bundle_factory(tmp_path: Path):
    """Factory fixture producing isolated AppTestBundle instances inside tmp_path."""
    counter = 0

    def _factory(
        *,
        token_secret: str = "unit-test-strong-secret-key-987654",
        delete_password: str = "delete-pass-123",
        deepseek_responses: list[Any] | None = None,
        cocreation_responses: list[Any] | None = None,
    ) -> AppTestBundle:
        nonlocal counter
        counter += 1
        return build_test_app_bundle(
            tmp_path / f"case_{counter}",
            token_secret=token_secret,
            delete_password=delete_password,
            deepseek_responses=deepseek_responses,
            cocreation_responses=cocreation_responses,
        )

    return _factory


def extract_hash_param(url: str, key: str) -> str:
    fragment = urlparse(url).fragment
    return parse_qs(fragment)[key][0]


def extract_query_param(url: str, key: str) -> str:
    return parse_qs(urlparse(url).query)[key][0]


def make_dg_summary_json(language: str = "en") -> str:
    if language == "zh-CN":
        return json.dumps(
            {
                "summary": (
                    "明白，你的偏好比较偏中等节奏和均衡布局。"
                    "我们可以先试试有明显通道的中等关卡，看看推动顺序和空间感是否合拍。"
                ),
                "recommendedDifficulty": "Medium",
                "difficultyRationale": "这个难度保留了调整空间，又需要思考推动顺序。",
                "recommendedLayout": "Balanced",
                "layoutRationale": "均衡布局既有聚拢推箱区也有过渡走廊。",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "summary": (
                "Got it, you lean toward steady planning and balanced spacing. "
                "Let's try a medium challenge with a clear central route so you can judge the flow."
            ),
            "recommendedDifficulty": "Medium",
            "difficultyRationale": (
                "Medium keeps recovery forgiving while still rewarding look-ahead."
            ),
            "recommendedLayout": "Balanced",
            "layoutRationale": "Balanced spacing offers breathing room without sprawling paths.",
        }
    )


def make_level_plan_json(*, design_note: str = "Base two-box route.") -> str:
    return json.dumps(
        {
            "minSolutionSteps": 22,
            "maxSolutionSteps": 42,
            "minWaterAreas": 1,
            "maxWaterAreas": 2,
            "minWallObstacleBlocks": 2,
            "maxWallObstacleBlocks": 3,
            "minPushes": 10,
            "maxPushes": 22,
            "minReversePulls": 18,
            "maxReversePulls": 34,
            "style": "hard classic choke route",
            "archetype": "bottleneck_corridor",
            "targetLayout": "split_pair",
            "obstacleStyle": "side_choke",
            "waterStyle": "side_pool",
            "designNote": design_note,
            "corridorPlacement": "none",
            "corridorWidth": 0,
            "corridorOrientation": "any",
            "corridorRole": "visual_only",
            "corridorPriority": "preferred",
        }
    )


def make_opening_json(msg: str = "Stage 1 ready.") -> str:
    return json.dumps(
        {
            "assistantMessage": msg,
            "coordinateLinks": [],
            "assessment": {
                "solutionSummary": "Direct push from (3,6) to (3,9).",
                "difficultyOpinion": "Straightforward layout.",
                "features": ["Open center"],
                "suggestions": ["Consider adding a small detour."],
            },
        }
    )


def make_routing_json(target: str = "ordinary", *, evidence_span: str = "the center") -> str:
    return json.dumps(
        {
            "acts": ["evaluation"],
            "elements": ["internal_walls"],
            "mapRelated": True,
            "directionSufficient": target != "binary_discovery",
            "routeTarget": target,
            "evidenceSpan": evidence_span,
            "changes": [],
        }
    )


def make_chat_json(
    msg: str = "Let us keep the path clear.",
    *,
    hyp: str = "",
    evidence_span: str = "the center",
    follow_up: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "assistantMessage": msg,
        "move": "clarify_intent",
        "intentDecision": (
            {
                "classification": "candidate",
                "claims": [
                    {
                        "normalizedMeaning": "Widen the center area",
                        "subjectType": "space",
                        "subjectText": "center",
                        "attributeType": "clarity",
                        "attributeText": "openness",
                        "direction": "increase",
                        "degree": "neutral",
                        "aspect": "gameplay",
                        "scopeType": "region",
                        "scopeText": "center",
                        "evidenceSpan": evidence_span,
                    }
                ],
                "cardText": hyp,
            }
            if hyp
            else {"classification": "none", "claims": [], "cardText": ""}
        ),
        "intentConfidence": "medium" if hyp else None,
        "uiCues": [],
        "coordinateLinks": [],
        "designContextPatch": {
            "goals": [{"text": "Keep route brisk", "evidenceText": msg}],
            "constraints": [],
            "openQuestions": [
                {
                    "question": "Should water stay centered?",
                    "status": "open",
                    "evidenceText": msg,
                }
            ],
        },
    }
    if follow_up is not None:
        payload["followUpQuestion"] = follow_up
    return json.dumps(payload)


def make_binary_ask_json(kwargs: dict[str, Any]) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("initialDesignerTurns") or []
    turn_id = str(turns[-1]["id"]) if turns else "turn-hint"
    return json.dumps(
        {
            "action": "ask",
            "question": "Which simplification works better?",
            "note": "Pick one obstacle change.",
            "branches": [
                {
                    "label": "Remove internal wall",
                    "strategy": {
                        "effect": "adjust_internal_walls",
                        "focus": {"row": 4, "column": 6, "radius": 1},
                        "operators": ["remove_wall"],
                        "preserve": ["boxes", "targets", "player"],
                        "editBudget": 1,
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "internal_walls",
                        "focus": {"row": 4, "column": 6, "radius": 1},
                    },
                },
                {
                    "label": "Remove water tile",
                    "strategy": {
                        "effect": "reshape_water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": ["boxes", "targets", "player"],
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
                        "component": "wall",
                        "property": "count",
                        "relation": "decrease",
                        "strength": "goal",
                        "entities": [],
                        "value": None,
                        "unit": "none",
                        "priorityEvidenceSpan": "",
                        "focused": False,
                        "sourceTurnId": turn_id,
                        "evidenceSpan": "Make center simpler",
                        "statement": "Remove an obstacle in the center",
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            },
        }
    )


def make_binary_ask_water_json(kwargs: dict[str, Any]) -> str:
    payload = json.loads(kwargs["messages"][-1]["content"])
    turns = payload.get("initialDesignerTurns") or []
    turn_id = str(turns[-1]["id"]) if turns else "turn-hint"
    return json.dumps(
        {
            "action": "ask",
            "question": "Which water or wall edit do you want?",
            "note": "Pick one direction.",
            "branches": [
                {
                    "label": "Remove water tile",
                    "strategy": {
                        "effect": "reshape_water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                        "operators": ["remove_water"],
                        "preserve": ["boxes", "targets", "player"],
                        "editBudget": 1,
                    },
                    "promise": {
                        "kind": "reduce_obstacles",
                        "component": "water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                    },
                },
                {
                    "label": "Reshape water nearby",
                    "strategy": {
                        "effect": "reshape_water",
                        "focus": {"row": 5, "column": 6, "radius": 1},
                        "operators": ["remove_water", "add_water"],
                        "preserve": ["boxes", "targets", "player"],
                        "editBudget": 2,
                    },
                    "promise": {
                        "kind": "reshape_water",
                        "component": "water",
                        "focus": {"row": 5, "column": 6, "radius": 2},
                    },
                },
            ],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": {
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
                        "sourceTurnId": turn_id,
                        "evidenceSpan": "Adjust water",
                        "statement": "Adjust water obstacle",
                        "scope": "all",
                    }
                ],
                "automaticBindings": [],
                "exactTransitions": [],
            },
        }
    )


def make_binary_plan_json() -> str:
    return json.dumps(
        {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [
                {
                    "effect": "adjust_internal_walls",
                    "focus": {"row": 4, "column": 6, "radius": 1},
                    "operators": ["remove_wall"],
                    "preserve": ["water"],
                    "editBudget": 1,
                }
            ],
            "protectedEntities": [],
        }
    )


def make_map_ops_json() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "strategyIndex": 1,
                    "rationale": "Remove center wall",
                    "operations": [
                        {"row": 4, "column": 6, "op": "remove_wall", "from": "#", "to": "."}
                    ],
                }
            ]
        }
    )


def make_translation_json(session_payload: dict[str, Any], target_language: str = "zh-CN") -> str:
    pending_turns = [t for t in session_payload["turns"] if t["language"] != target_language]
    items: list[dict[str, Any]] = []
    for turn in pending_turns:
        guidance = turn["guidance"]
        raw_cues = [f"提示:{cue}" for cue in (guidance.get("uiCueTexts") or [])]
        has_structured = bool(
            raw_cues
            or guidance.get("followUpQuestion")
            or guidance.get("intentHypothesis")
            or isinstance(guidance.get("proposalOffer"), dict)
            or guidance.get("proposalQuestion")
            or guidance.get("disagreement")
            or guidance.get("coordinateLinks")
        )
        item: dict[str, Any] = {
            "turnId": turn["turnId"],
            "body": f"中文:{turn['content'][:40]}",
        }
        if has_structured:
            item["uiCueTexts"] = raw_cues
        if guidance.get("intentHypothesis"):
            item["intentHypothesis"] = "中文意图"
        if isinstance(guidance.get("proposalOffer"), dict) and guidance["proposalOffer"].get(
            "summary"
        ):
            item["proposalOfferSummary"] = "中文提案摘要"
        if guidance.get("proposalQuestion"):
            pq = guidance["proposalQuestion"]
            item["proposalQuestion"] = {
                "question": "中文问题？",
                "note": "说明",
                "choices": [
                    {
                        "choiceId": choice["choiceId"],
                        "label": f"选项{choice['letter']}",
                    }
                    for choice in pq["choices"]
                ],
            }
        items.append(item)
    return json.dumps({"translations": items}, ensure_ascii=False)
