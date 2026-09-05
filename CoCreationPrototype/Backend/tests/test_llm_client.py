import asyncio
import json
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

import httpx
from openai import APITimeoutError


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import llm_client
from level_validation import build_entity_bindings


OPERATION_BASE_ROWS = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


ENTITY_ROUTE_ROWS = [
    "############",
    "#p.........#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..s...t...#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


MAP_GROUNDING_ROWS = [
    "  ######### ",
    " ##...p..t# ",
    " #..s.....# ",
    " #.#......##",
    "##.#...#...#",
    "######.#####",
    "#t........##",
    "##@@@@.#s.# ",
    " #@@@@...## ",
    " #########  ",
]


def operation_payload(*operation_sets):
    return json.dumps({
        "candidates": [
            {"strategyIndex": 1, "operations": operations}
            for operations in operation_sets
        ]
    })


def revision_plan_payload(
    effect="relocate_target",
    operators=None,
    focus=None,
    preserve=None,
    edit_budget=2,
    metric_goals=None,
    required_transitions=None,
):
    if required_transitions is None:
        if effect == "reshape_water":
            required_transitions = [
                {"row": 2, "column": 6, "from": ".", "to": "@"},
                {"row": 2, "column": 7, "from": ".", "to": "@"},
                {"row": 3, "column": 6, "from": ".", "to": "@"},
            ]
        elif effect == "narrow_route":
            required_transitions = [
                {"row": 2, "column": 2, "from": ".", "to": "#"},
                {"row": 2, "column": 3, "from": ".", "to": "#"},
                {"row": 3, "column": 2, "from": ".", "to": "#"},
            ]
        elif effect == "relocate_target":
            required_transitions = [
                {"row": 6, "column": 7, "from": "t", "to": "."},
                {"row": 6, "column": 8, "from": ".", "to": "t"},
            ]
        else:
            required_transitions = []
    return json.dumps({
        "strategies": [{
            "effect": effect,
            "focus": focus or {"row": 6, "column": 8, "radius": 1},
            "operators": operators or ["move_target"],
            "preserve": preserve or [
                "outer_shell",
                "player",
                "boxes",
                "water",
                "unrelated_areas",
            ],
            "editBudget": edit_budget,
            "metricGoals": metric_goals or [],
            "requiredTransitions": required_transitions,
            "anchorEntities": [],
            "playObjective": "route_choice",
        }]
    })


TARGET_SHIFT_OPERATIONS = [
    {"row": 6, "column": 7, "to": "."},
    {"row": 6, "column": 8, "to": "t"},
]

WATER_ADD_OPERATIONS = [
    {"row": 2, "column": 6, "to": "@"},
    {"row": 2, "column": 7, "to": "@"},
    {"row": 3, "column": 6, "to": "@"},
]


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        if hasattr(outcome, "choices"):
            return outcome

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))

    async def close(self):
        return None


class SlowCompletions:
    async def create(self, **kwargs):
        await asyncio.sleep(10)


class SlowClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SlowCompletions())

    async def close(self):
        return None


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.kimi_environment = patch.dict(
            os.environ,
            {
                "COCREATION_LLM_PROVIDER": "kimi",
                "COCREATION_LLM_MODEL": "kimi-k2.6",
                "COCREATION_LLM_BASE_URL": "https://api.moonshot.cn/v1",
                "KIMI_API_KEY": "test-kimi-key",
            },
            clear=False,
        )
        self.kimi_environment.start()
        self.addCleanup(self.kimi_environment.stop)

    def execute(self, outcomes, rows=None, **reply_kwargs):
        client = FakeClient(outcomes)
        conversation = reply_kwargs.pop(
            "conversation",
            [{"role": "user", "content": "Assess the level."}],
        )

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                rows or ["############"] * 10,
                "request-test",
                **reply_kwargs,
            )

        return result, client

    def test_valid_response_is_returned(self):
        result, client = self.execute([
            "The level has a compact central route.\n\n"
            "When the box enters the water-side corridor, which route choice should the "
            "player notice first so we can judge its readability?"
        ])

        self.assertIn("compact central route", result.assistant_message)
        self.assertNotIn("box enters the water-side corridor", result.assistant_message)
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertEqual(result.guidance["move"], "offer_perspective")
        self.assertEqual(result.attempts_used, 1)
        request = client.chat.completions.calls[0]
        self.assertNotIn("response_format", request)
        self.assertFalse(request["stream"])
        self.assertEqual(request["model"], "kimi-k2.6")
        self.assertEqual(
            request["max_completion_tokens"],
            llm_client.PLAIN_CHAT_MAX_COMPLETION_TOKENS,
        )
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})

    def test_model_environment_overrides_are_ignored(self):
        client = FakeClient(["The verified route remains readable."])

        with (
            patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "DEEPSEEK_MODEL": "deepseek-v4-pro",
                    "DEEPSEEK_PROPOSAL_MODEL": "some-other-model",
                    "DEEPSEEK_FALLBACK_MODEL": "another-model",
                },
                clear=False,
            ),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "unified-model-config-test",
            )

        self.assertEqual(result.model, llm_client.UNIFIED_MODEL)
        self.assertEqual(
            [call["model"] for call in client.chat.completions.calls],
            [llm_client.UNIFIED_MODEL],
        )

    def test_missing_kimi_key_does_not_fall_back_to_deepseek(self):
        with patch.dict(
            os.environ,
            {
                "COCREATION_LLM_PROVIDER": "kimi",
                "COCREATION_LLM_API_KEY": "",
                "COCREATION_LLM_BASE_URL": "https://api.moonshot.cn/v1",
                "KIMI_API_KEY": "",
                "DEEPSEEK_API_KEY": "legacy-deepseek-key",
                "DEEPSEEK_MODEL": "deepseek-v4-flash",
            },
            clear=True,
        ):
            with self.assertRaises(llm_client.LLMServiceError) as raised:
                llm_client.generate_chat_reply(
                    [{"role": "user", "content": "Assess the route."}],
                    ["############"] * 10,
                    "no-deepseek-fallback-test",
                )

        self.assertEqual(raised.exception.code, "CONFIGURATION_ERROR")
        self.assertEqual(raised.exception.attempts_used, 0)

    def test_kimi_provider_uses_kimi_k26_configuration(self):
        client = FakeClient(["The verified route remains readable."])
        with (
            patch.dict(
                os.environ,
                {
                    "COCREATION_LLM_PROVIDER": "kimi",
                    "COCREATION_LLM_MODEL": "kimi-k2.6",
                    "COCREATION_LLM_BASE_URL": "https://api.moonshot.cn/v1",
                    "KIMI_API_KEY": "test-kimi-key",
                },
                clear=False,
            ),
            patch.object(llm_client, "_create_async_client", return_value=client) as create_client,
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "kimi-config-test",
            )

        self.assertEqual(result.model, "kimi-k2.6")
        self.assertEqual(client.chat.completions.calls[0]["model"], "kimi-k2.6")
        self.assertEqual(client.chat.completions.calls[0]["temperature"], 0.6)
        self.assertEqual(
            client.chat.completions.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        create_client.assert_called_once_with(
            "test-kimi-key",
            "https://api.moonshot.cn/v1",
            ANY,
        )

    def test_visible_output_removes_prompt_only_field_names(self):
        payload = {
            "assistantMessage": (
                "gridDistance suggests the box is close, while `_solver` and tileAt "
                "confirm the current route."
            ),
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "mapFacts and solutionSteps are internal.",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        for value in (result[0], result[3]):
            self.assertNotRegex(
                value,
                r"gridDistance|_solver|tileAt|mapFacts|solutionSteps",
            )
        self.assertIn("Manhattan distance", result[0])
        self.assertIn("current tile", result[0])

    def test_chinese_visible_output_localizes_design_jargon(self):
        payload = {
            "assistantMessage": (
                "The Peninsula creates a choke point and a playable moment, so route choice "
                "and push order shape the trade-off through the corridor."
            ),
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, language="zh-CN")

        self.assertIn("\u534a\u5c9b", result[0])
        self.assertIn("\u74f6\u9888\u70b9", result[0])
        self.assertIn("\u5173\u952e\u64cd\u4f5c\u65f6\u523b", result[0])
        self.assertIn("\u8def\u7ebf\u9009\u62e9", result[0])
        self.assertIn("\u63a8\u7bb1\u987a\u5e8f", result[0])
        self.assertNotIn("Peninsula", result[0])
        self.assertNotIn("choke point", result[0])
        self.assertNotIn("playable moment", result[0])

    def test_stage_assessment_schema_owns_opening_move(self):
        schema = llm_client._structured_response_format("stage_assessment")
        guidance = schema["json_schema"]["schema"]["properties"]["guidance"]
        assessment = schema["json_schema"]["schema"]["properties"]["assessment"]

        self.assertEqual(guidance["properties"]["move"]["enum"], ["observe_stage"])
        self.assertEqual(
            assessment["required"],
            [
                "solutionSummary",
                "difficultyOpinion",
                "features",
                "suggestions",
                "satisfactionQuestion",
            ],
        )

    def test_translation_visible_output_removes_prompt_only_field_names(self):
        source_items = [{
            "turnId": "turn-1",
            "body": "A route is visible.",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
            "disagreement": None,
        }]
        payload = {
            "translations": [{
                "turnId": "turn-1",
                "body": "gridDistance and `_solver` are internal.",
                "followUpQuestion": None,
                "intentHypothesis": None,
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "uiCueTexts": [],
                "proposalSummary": None,
                "disagreement": None,
            }]
        }

        result = llm_client.validate_translation_response(
            payload,
            source_items,
            target_language="en",
        )

        self.assertNotRegex(result[0]["body"], r"gridDistance|_solver")
        self.assertIn("Manhattan distance", result[0]["body"])

    def test_plain_coordinate_links_are_hidden_metadata_and_keep_body_unchanged(self):
        body = "I would move from (5,5) along the corridor to (5,7)."
        content = (
            body
            + "\n<GUIDANCE>COORDINATE_LINKS: "
            + json.dumps([{
                "text": "from (5,5) along the corridor to (5,7)",
                "from": {"row": 5, "column": 5},
                "to": {"row": 5, "column": 7},
            }])
            + "</GUIDANCE>"
        )

        visible, _, _, _ = llm_client._extract_plain_guidance(
            content,
            "en",
            {},
            rows=OPERATION_BASE_ROWS,
        )
        links = llm_client._extract_plain_coordinate_links(content, OPERATION_BASE_ROWS)

        self.assertEqual(visible, body)
        self.assertEqual(links[0]["text"], "from (5,5) along the corridor to (5,7)")
        self.assertEqual(links[0]["from"], {"row": 5, "column": 5})

    def test_plain_chat_persists_coordinate_links_without_visible_metadata(self):
        body = "I would move from (5,5) along the corridor to (5,7)."
        content = (
            body
            + "\n<GUIDANCE>COORDINATE_LINKS: "
            + json.dumps([{
                "text": "from (5,5) along the corridor to (5,7)",
                "from": {"row": 5, "column": 5},
                "to": {"row": 5, "column": 7},
            }])
            + "</GUIDANCE>"
        )

        result, _ = self.execute([content], rows=OPERATION_BASE_ROWS)

        self.assertEqual(result.assistant_message, body)
        self.assertEqual(
            result.guidance["coordinateLinks"],
            [{
                "text": "from (5,5) along the corridor to (5,7)",
                "from": {"row": 5, "column": 5},
                "to": {"row": 5, "column": 7},
            }],
        )

    def test_llm_metadata_accepts_natural_entity_route_without_changing_body(self):
        body = "B2 从（4,9）往 T1 走的时候，路线选择明显多了。"
        payload = {
            "assistantMessage": body,
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "disagreement": None,
                "uiCues": [],
                "coordinateLinks": [{
                    "text": "B2 从（4,9）往 T1 走",
                    "from": {"row": 6, "column": 4},
                    "to": {"row": 3, "column": 7},
                }],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            rows=ENTITY_ROUTE_ROWS,
        )

        self.assertEqual(result[0], body)
        self.assertEqual(
            result[4]["coordinateLinks"],
            [{
                "text": "B2 从（4,9）往 T1 走",
                "from": {"row": 6, "column": 4},
                "to": {"row": 3, "column": 7},
            }],
        )

    def test_route_language_is_not_inferred_without_llm_metadata(self):
        route_body = "B2 与 T2 之间的路径更长。"
        ordinary_body = "我会同时关注 B1 和 T1 的位置。"

        self.assertEqual(
            llm_client._filter_coordinate_links([], route_body, ENTITY_ROUTE_ROWS),
            [],
        )
        self.assertEqual(
            llm_client._filter_coordinate_links([], ordinary_body, ENTITY_ROUTE_ROWS),
            [],
        )

    def test_clear_coordinate_route_recovers_missing_metadata(self):
        body = "The route from B1 to T1 remains readable."

        links = llm_client._recover_coordinate_links(body, ENTITY_ROUTE_ROWS)

        self.assertEqual(
            links,
            [{
                "text": body,
                "from": {"row": 3, "column": 5},
                "to": {"row": 3, "column": 7},
            }],
        )

    def test_coordinate_link_clause_rejects_dangling_punctuation_or_connector(self):
        self.assertTrue(
            llm_client._coordinate_link_text_is_complete_clause(
                "The route from B1 to T1 remains readable"
            )
        )
        self.assertFalse(
            llm_client._coordinate_link_text_is_complete_clause(
                "The route from B1 to T1,"
            )
        )
        self.assertFalse(
            llm_client._coordinate_link_text_is_complete_clause(
                "The route from B1 to T1 and"
            )
        )

    def test_coordinate_route_recovery_rejects_entity_co_mentions(self):
        body = "I am comparing B1 and T1 positions."

        self.assertEqual(
            llm_client._recover_coordinate_links(body, ENTITY_ROUTE_ROWS),
            [],
        )

    def test_position_description_with_route_word_is_not_annotated(self):
        body = "The route layout places B1 and B2 beside water, while T1 and T2 sit in the lower-right row."

        self.assertEqual(
            llm_client._recover_coordinate_links(body, ENTITY_ROUTE_ROWS),
            [],
        )

    def test_location_relation_is_not_annotated_by_direction_word_elsewhere(self):
        body = "B2在(6,4)，紧挨着水；T2在(6,8)，玩家要从左侧绕水。"

        self.assertEqual(
            llm_client._filter_coordinate_links(
                [{
                    "text": body,
                    "from": {"row": 6, "column": 4},
                    "to": {"row": 6, "column": 8},
                }],
                body,
                ENTITY_ROUTE_ROWS,
            ),
            [],
        )
        self.assertEqual(
            llm_client._filter_coordinate_links(
                [{
                    "text": body,
                    "from": {"row": 3, "column": 5},
                    "to": {"row": 3, "column": 7},
                }],
                body,
                ENTITY_ROUTE_ROWS,
            ),
            [],
        )

    def test_coordinate_link_rejects_wrong_entity_endpoint(self):
        body = "The route from B1 to T1 remains readable."

        self.assertEqual(
            llm_client._filter_coordinate_links(
                [{
                    "text": body,
                    "from": {"row": 3, "column": 4},
                    "to": {"row": 3, "column": 7},
                }],
                body,
                ENTITY_ROUTE_ROWS,
            ),
            [],
        )

    def test_entity_coordinate_claim_must_match_map_facts(self):
        with self.assertRaisesRegex(ValueError, "places B1"):
            llm_client._validate_map_grounding_texts(
                ["B1 is at (3,9)."],
                ENTITY_ROUTE_ROWS,
            )
        with self.assertRaisesRegex(ValueError, "places B1"):
            llm_client._validate_map_grounding_texts(
                ["B1\u5728(6,3)\u7d27\u6328\u7740\u73a9\u5bb6"],
                ENTITY_ROUTE_ROWS,
            )

    def test_coordinate_grounding_supports_current_fact_forms_and_skips_future_routes(self):
        bindings = build_entity_bindings(ENTITY_ROUTE_ROWS)
        current_claims = [
            "B1 is at (3,5).",
            "B1 occupies (3,5).",
            "B1 is located in row 3, column 5.",
            "B1 sits on row 3, column 5.",
            "B1 \u5728\u7b2c 3 \u884c\u7b2c 5 \u5217\u3002",
            "\u7b2c 3 \u884c\u7b2c 5 \u5217\u662f B1\u3002",
            "B1 \u4f4d\u4e8e\uff083\uff0c5\uff09\u3002",
        ]
        for claim in current_claims:
            with self.subTest(claim=claim):
                llm_client._validate_map_grounding_texts(
                    [claim], ENTITY_ROUTE_ROWS, entity_bindings=bindings
                )

        for future_claim in (
            "B1 will move to (3,9).",
            "If B1 moved to (3,9), the detour would be longer.",
            "\u5982\u679c B1 \u79fb\u52a8\u5230\uff083\uff0c9\uff09\uff0c\u8fd9\u53ea\u662f\u5047\u8bbe\u3002",
        ):
            with self.subTest(future_claim=future_claim):
                llm_client._validate_map_grounding_texts(
                    [future_claim], ENTITY_ROUTE_ROWS, entity_bindings=bindings
                )

        with self.assertRaisesRegex(ValueError, "places B1"):
            llm_client._validate_map_grounding_texts(
                ["\u7b2c 3 \u884c\u7b2c 9 \u5217\u662f B1\u3002"],
                ENTITY_ROUTE_ROWS,
                entity_bindings=bindings,
            )

    def test_route_recovery_uses_stable_entities_and_local_waypoints(self):
        bindings = build_entity_bindings(ENTITY_ROUTE_ROWS)
        links = llm_client._recover_coordinate_links(
            "B1 -> (3,6) -> T1", ENTITY_ROUTE_ROWS, entity_bindings=bindings
        )

        self.assertEqual(
            [(item["text"], item["from"], item["to"]) for item in links],
            [
                ("B1 -> (3,6)", {"row": 3, "column": 5}, {"row": 3, "column": 6}),
                ("(3,6) -> T1", {"row": 3, "column": 6}, {"row": 3, "column": 7}),
            ],
        )
        self.assertEqual(
            llm_client._recover_coordinate_links(
                "B1 is near T1; B1 is beside T1", ENTITY_ROUTE_ROWS,
                entity_bindings=bindings,
            ),
            [],
        )
        self.assertEqual(
            llm_client._recover_coordinate_links(
                "B1 is close to T1", ENTITY_ROUTE_ROWS,
                entity_bindings=bindings,
            ),
            [],
        )

    def test_named_current_relations_are_checked_against_the_named_entities(self):
        bindings = build_entity_bindings(ENTITY_ROUTE_ROWS)
        llm_client._validate_map_grounding_texts(
            ["B1 is close to T1."],
            ENTITY_ROUTE_ROWS,
            entity_bindings=bindings,
        )
        with self.assertRaisesRegex(ValueError, "B1.*T2"):
            llm_client._validate_map_grounding_texts(
                ["B1 is near T2."],
                ENTITY_ROUTE_ROWS,
                entity_bindings=bindings,
            )

    def test_llm_metadata_supports_different_route_wording(self):
        cases = [
            (
                "B1 通往 T1 的路径上加点转折。",
                "B1 通往 T1",
                {"row": 3, "column": 5},
                {"row": 3, "column": 7},
            ),
            (
                "从 T2 沿走廊到 B2 会经过一段窄路。",
                "从 T2 沿走廊到 B2",
                {"row": 6, "column": 8},
                {"row": 6, "column": 4},
            ),
            (
                "The route from B2 to T1 can now use the open corridor.",
                "route from B2 to T1",
                {"row": 6, "column": 4},
                {"row": 3, "column": 7},
            ),
        ]

        for body, link_text, source, destination in cases:
            with self.subTest(body=body):
                links = llm_client._filter_coordinate_links(
                    [{"text": link_text, "from": source, "to": destination}],
                    body,
                    ENTITY_ROUTE_ROWS,
                )
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0]["text"], link_text)

    def test_structured_coordinate_links_are_filtered_against_final_visible_body(self):
        body = "I would guide the player from (5,5) to (5,7)."
        payload = {
            "assistantMessage": body,
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "disagreement": None,
                "uiCues": [],
                "coordinateLinks": [
                    {
                        "text": "from (5,5) to (5,7)",
                        "from": {"row": 5, "column": 5},
                        "to": {"row": 5, "column": 7},
                    },
                    {
                        "text": "not present",
                        "from": {"row": 5, "column": 5},
                        "to": {"row": 5, "column": 7},
                    },
                    {
                        "text": "from (5,5)",
                        "from": {"row": 5, "column": 5},
                        "to": {"row": 5, "column": 7},
                    },
                ],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            rows=OPERATION_BASE_ROWS,
        )

        self.assertEqual(result[0], body)
        self.assertEqual(len(result[4]["coordinateLinks"]), 1)
        self.assertEqual(result[4]["coordinateLinks"][0]["to"], {"row": 5, "column": 7})

    def test_coordinate_links_reject_void_and_out_of_range_points_without_blocking_reply(self):
        body = "The route remains worth watching."
        payload = {
            "assistantMessage": body,
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "disagreement": None,
                "uiCues": [],
                "coordinateLinks": [
                    {
                        "text": "outside",
                        "from": {"row": 0, "column": 1},
                        "to": {"row": 5, "column": 7},
                    },
                    {
                        "text": "void",
                        "from": {"row": 1, "column": 1},
                        "to": {"row": 5, "column": 7},
                    },
                ],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            rows=OPERATION_BASE_ROWS,
        )

        self.assertEqual(result[0], body)
        self.assertEqual(result[4]["coordinateLinks"], [])

    def test_non_json_natural_language_is_accepted_immediately(self):
        result, client = self.execute(["This is useful prose, not a JSON object."])

        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIn("useful prose", result.assistant_message)

    def test_map_facts_expose_exact_entities_and_verified_parent_changes(self):
        before = list(MAP_GROUNDING_ROWS)
        before[1] = " ##..p...t# "
        facts = llm_client.build_map_facts(MAP_GROUNDING_ROWS, before)

        self.assertEqual(facts["player"], {"id": "P", "row": 2, "column": 7})
        self.assertEqual(
            [(box["id"], box["row"], box["column"]) for box in facts["boxes"]],
            [("B1", 3, 5), ("B2", 8, 9)],
        )
        self.assertFalse(any(box["orthogonallyAdjacentToWater"] for box in facts["boxes"]))
        self.assertEqual(
            facts["verifiedEntityChangesFromParent"]["player"],
            {"removed": [{"row": 2, "column": 6}], "added": [{"row": 2, "column": 7}]},
        )

        prompt = llm_client.build_plain_chat_messages(
            [], MAP_GROUNDING_ROWS, stage_context={"mapFacts": facts}
        )[0]["content"]
        self.assertIn("Current Stage Snapshot (authoritative; 10 rows x 12 columns)", prompt)
        self.assertIn('"id":"B1","row":3,"column":5', prompt)
        self.assertIn("Map-grounding rule", prompt)

    def test_prompt_rebuilds_map_facts_when_supplied_snapshot_is_stale(self):
        stale_facts = llm_client.build_map_facts(MAP_GROUNDING_ROWS)

        prompt = llm_client.build_plain_chat_messages(
            [],
            OPERATION_BASE_ROWS,
            stage_context={"mapFacts": stale_facts},
        )[0]["content"]

        self.assertIn('"id":"B1","row":6,"column":5', prompt)
        self.assertNotIn('"id":"B1","row":3,"column":5', prompt)

    def test_chat_prompt_excludes_historical_assistant_map_facts(self):
        prompt = llm_client.build_plain_chat_messages(
            [
                {
                    "role": "assistant",
                    "content": "STALE_ASSISTANT_COORDINATE (9,9) says B1 is there.",
                },
                {"role": "user", "content": "Please inspect the current route."},
            ],
            OPERATION_BASE_ROWS,
            stage_context={
                "mapFacts": llm_client.build_map_facts(MAP_GROUNDING_ROWS),
                "beforeRows": MAP_GROUNDING_ROWS,
                "afterRows": MAP_GROUNDING_ROWS,
            },
        )

        self.assertEqual(len(prompt), 2)
        self.assertEqual(prompt[-1]["content"], "Please inspect the current route.")
        self.assertNotIn("STALE_ASSISTANT_COORDINATE", prompt[0]["content"])
        self.assertNotIn("beforeRows", prompt[0]["content"])
        self.assertNotIn("afterRows", prompt[0]["content"])
        self.assertEqual(prompt[0]["content"].count("Current Stage Snapshot"), 1)

    def test_chat_prompt_drops_untrusted_claims_and_raw_card_contracts(self):
        prompt = llm_client.build_plain_chat_messages(
            [],
            OPERATION_BASE_ROWS,
            stage_context={
                "userMapClaims": {
                    "conflicts": [{
                        "sourceText": "UNTRUSTED_CLAIM_MARKER",
                        "claimed": {"row": 9, "column": 9},
                    }],
                },
                "recentGuidance": {
                    "proposalOffer": {
                        "summary": "Keep the local route readable",
                        "rationale": "Preserve the route while testing the opening.",
                        "executionBrief": {
                            "requiredTransitions": [{
                                "row": 9,
                                "column": 9,
                                "from": ".",
                                "to": "#",
                            }],
                        },
                        "proposalPresentation": {
                            "changes": [{"row": 9, "column": 9}],
                        },
                    },
                },
            },
        )

        content = prompt[0]["content"]
        self.assertNotIn("UNTRUSTED_CLAIM_MARKER", content)
        self.assertNotIn("proposalPresentation", content)
        self.assertEqual(content.count("Current Stage Snapshot"), 1)

    def test_execution_brief_validates_exact_tiles_and_keeps_tile_facts(self):
        brief = {
            "schemaVersion": 1,
            "effect": "adjust_internal_walls",
            "anchors": ["B1", "T1"],
            "focus": {"row": 2, "column": 2, "radius": 1},
            "requiredTransitions": [
                {"row": 2, "column": 2, "from": ".", "to": "#"},
            ],
            "allowedOperators": ["add_wall"],
            "preserve": ["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
            "playObjective": "route_choice",
        }
        normalized = llm_client._validate_execution_brief(brief, OPERATION_BASE_ROWS)

        self.assertEqual(normalized["requiredTransitions"][0]["from"], ".")
        self.assertEqual(
            llm_client.build_map_facts(OPERATION_BASE_ROWS)["tileAt"]["2,2"],
            ".",
        )
        with self.assertRaisesRegex(ValueError, "coordinate conflict"):
            llm_client._validate_execution_brief(
                {
                    **brief,
                    "requiredTransitions": [
                        {"row": 2, "column": 2, "from": "#", "to": "."},
                    ],
                    "allowedOperators": ["remove_wall"],
                },
                OPERATION_BASE_ROWS,
            )

    def test_execution_brief_binds_both_sides_of_entity_move(self):
        bindings = build_entity_bindings(ENTITY_ROUTE_ROWS)
        brief = {
            "schemaVersion": 1,
            "effect": "relocate_box",
            "anchors": ["B1"],
            "focus": {"row": 3, "column": 6, "radius": 2},
            "requiredTransitions": [
                {"row": 3, "column": 5, "from": "s", "to": ".", "anchorEntity": "B1"},
                {"row": 3, "column": 6, "from": ".", "to": "s", "anchorEntity": "B1"},
            ],
            "allowedOperators": ["move_box"],
            "preserve": ["outer_shell", "player", "targets", "water", "unrelated_areas"],
            "playObjective": "route_choice",
        }

        normalized = llm_client._validate_execution_brief(
            brief, ENTITY_ROUTE_ROWS, bindings
        )
        self.assertEqual(
            [item["anchorEntity"] for item in normalized["requiredTransitions"]],
            ["B1", "B1"],
        )

        with self.assertRaisesRegex(ValueError, "source coordinate"):
            llm_client._validate_execution_brief(
                {
                    **brief,
                    "requiredTransitions": [
                        {"row": 3, "column": 4, "from": "s", "to": ".", "anchorEntity": "B1"},
                        {"row": 3, "column": 6, "from": ".", "to": "s", "anchorEntity": "B1"},
                    ],
                },
                ENTITY_ROUTE_ROWS,
                bindings,
            )

    def test_chat_revision_plan_is_normalized_to_an_exact_execution_brief(self):
        payload = {
            "assistantMessage": "I would close the local opening and compare the first push.",
            "guidance": {
                "move": "offer_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Add a wall at (2,2)",
                    "rationale": "The first push should reveal whether this local closure creates a meaningful detour.",
                    "revisionPlan": {
                        "objective": "Make the first route commitment more deliberate",
                        "changes": [{
                            "row": 2,
                            "column": 2,
                            "before": ".",
                            "after": "#",
                            "operation": "add_wall",
                        }],
                        "preserve": ["outer_shell", "boxes", "targets", "water"],
                        "rationale": "Reduce the direct opening while keeping the box-target relationship.",
                        "expectedEffect": "The player must compare the nearby detour before the first push.",
                    },
                },
                "uiCues": [],
                "coordinateLinks": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            language="en",
            rows=OPERATION_BASE_ROWS,
        )
        offer = result[4]["proposalOffer"]
        self.assertNotIn("revisionPlan", offer)
        self.assertEqual(
            offer["executionBrief"]["requiredTransitions"],
            [{"row": 2, "column": 2, "from": ".", "to": "#"}],
        )
        self.assertEqual(offer["executionBrief"]["allowedOperators"], ["add_wall"])
        self.assertEqual(
            offer["executionBrief"]["playObjective"],
            "Make the first route commitment more deliberate",
        )

    def test_chat_revision_plan_rejects_a_wrong_operation_or_missing_exact_fields(self):
        base = {
            "objective": "Make the first route commitment more deliberate",
            "changes": [{
                "row": 2,
                "column": 2,
                "before": ".",
                "after": "#",
                "operation": "remove_wall",
            }],
            "preserve": ["outer_shell", "boxes", "targets", "water"],
            "rationale": "Reduce the direct opening.",
            "expectedEffect": "The first route becomes less direct.",
        }
        with self.assertRaisesRegex(ValueError, "exact supported transition"):
            llm_client._validate_revision_plan_payload(base, OPERATION_BASE_ROWS)
        with self.assertRaisesRegex(ValueError, "must contain objective"):
            llm_client._validate_revision_plan_payload(
                {key: value for key, value in base.items() if key != "expectedEffect"},
                OPERATION_BASE_ROWS,
            )

    def test_modifier_receives_only_the_frozen_contract_projection(self):
        contract = {
            "schemaVersion": 1,
            "authorizedBrief": "Do not expose this prose to the modifier.",
            "revisionPlan": {"strategies": [{"effect": "wrong"}]},
            "strategies": [{
                "strategyIndex": 1,
                "effect": "adjust_internal_walls",
                "focus": {"row": 2, "column": 2, "radius": 1},
                "allowedOperators": ["add_wall"],
                "preserve": ["outer_shell", "boxes", "targets", "water"],
                "minimumChangedCells": 1,
                "maximumChangedCells": 1,
                "metricGoals": [],
                "requiredTransitions": [
                    {"row": 2, "column": 2, "from": ".", "to": "#"},
                ],
                "anchorEntities": [],
                "playObjective": "route_choice",
            }],
        }

        projected = llm_client._modifier_contract_view(contract)
        self.assertNotIn("authorizedBrief", projected)
        self.assertNotIn("revisionPlan", projected)
        self.assertEqual(
            projected["strategies"][0]["requiredTransitions"],
            [{"row": 2, "column": 2, "from": ".", "to": "#"}],
        )

    def test_modifier_contract_rejects_an_extra_operation_beyond_frozen_transitions(self):
        plan = llm_client.parse_revision_plan(json.loads(revision_plan_payload(
            effect="adjust_internal_walls",
            operators=["add_wall"],
            focus={"row": 2, "column": 2, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
            edit_budget=1,
            required_transitions=[
                {"row": 2, "column": 2, "from": ".", "to": "#"},
            ],
        )))
        contract = llm_client._build_revision_execution_contract(
            plan,
            "Close one local opening.",
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            llm_client.execute_revision_operations(
                OPERATION_BASE_ROWS,
                [
                    {"row": 2, "column": 2, "from": ".", "to": "#"},
                    {"row": 2, "column": 3, "from": ".", "to": "#"},
                ],
                contract,
                1,
            )

    def test_enriched_contract_allows_one_cell_required_wall_transition(self):
        plan = llm_client.parse_revision_plan({
            "strategies": [{
                "effect": "adjust_internal_walls",
                "focus": {"row": 2, "column": 2, "radius": 1},
                "operators": ["add_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
                "editBudget": 1,
                "metricGoals": [],
                "requiredTransitions": [
                    {"row": 2, "column": 2, "from": ".", "to": "#"},
                ],
                "anchorEntities": ["B1", "T1"],
                "playObjective": "route_choice",
            }],
        })
        llm_client.validate_revision_plan_against_map(OPERATION_BASE_ROWS, plan)
        contract = llm_client._build_revision_execution_contract(plan, "open one local route")

        self.assertEqual(contract["strategies"][0]["minimumChangedCells"], 1)
        self.assertEqual(contract["strategies"][0]["maximumChangedCells"], 1)
        changed = llm_client.execute_revision_operations(
            OPERATION_BASE_ROWS,
            [{"row": 2, "column": 2, "from": ".", "to": "#"}],
            contract,
            1,
        )
        self.assertEqual(changed[1], "##.........#")

    def test_plain_execution_brief_is_checked_against_saved_tiles(self):
        content = (
            "I would open the cited local route.\n"
            "<GUIDANCE>PROPOSAL_SUMMARY: Open the local route || "
            "PROPOSAL_RATIONALE: Compare the first push after the wall is removed. || "
            "EXECUTION_BRIEF: "
            + json.dumps({
                "schemaVersion": 1,
                "effect": "open_route",
                "anchors": ["B1"],
                "focus": {"row": 2, "column": 2, "radius": 1},
                "requiredTransitions": [
                    {"row": 2, "column": 2, "from": "#", "to": "."},
                ],
                "allowedOperators": ["remove_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
                "playObjective": "route_choice",
            })
            + "</GUIDANCE>"
        )
        with self.assertRaisesRegex(ValueError, "coordinate conflict"):
            llm_client._extract_plain_guidance(
                content,
                "en",
                {},
                rows=OPERATION_BASE_ROWS,
            )

    def test_legacy_guidance_protocol_accepts_the_exact_revision_plan_shape(self):
        revision_plan = {
            "objective": "Make the first route commitment more deliberate",
            "changes": [{
                "row": 2,
                "column": 2,
                "before": ".",
                "after": "#",
                "operation": "add_wall",
            }],
            "preserve": ["outer_shell", "boxes", "targets", "water"],
            "rationale": "Reduce the direct opening.",
            "expectedEffect": "The first route requires a nearby detour check.",
        }
        content = (
            "I would close the local opening and compare the first push.\n"
            "<GUIDANCE>PROPOSAL_SUMMARY: Add a wall at (2,2) || "
            "PROPOSAL_RATIONALE: Compare the first push after the closure. || "
            "REVISION_PLAN: "
            + json.dumps(revision_plan, separators=(",", ":"))
            + "</GUIDANCE>"
        )
        visible, _, offer, _ = llm_client._extract_plain_guidance(
            content,
            "en",
            {},
            rows=OPERATION_BASE_ROWS,
        )

        self.assertIn("first push", visible)
        self.assertEqual(
            offer["executionBrief"]["requiredTransitions"],
            [{"row": 2, "column": 2, "from": ".", "to": "#"}],
        )
        self.assertNotIn("revisionPlan", offer)

    def test_prose_coordinates_are_not_recovered_into_an_execution_brief(self):
        self.assertFalse(hasattr(llm_client, "_execution_brief_from_text"))

    def test_plain_reply_drops_invalid_optional_proposal_metadata_without_replacing_body(self):
        content = (
            "I would open the cited local route.\n"
            "<GUIDANCE>PROPOSAL_SUMMARY: Open the local route || "
            "PROPOSAL_RATIONALE: Compare the first push after the wall is removed. || "
            "EXECUTION_BRIEF: "
            + json.dumps({
                "schemaVersion": 1,
                "effect": "open_route",
                "anchors": ["B1"],
                "focus": {"row": 2, "column": 2, "radius": 1},
                "requiredTransitions": [
                    {"row": 2, "column": 2, "from": "#", "to": "."},
                ],
                "allowedOperators": ["remove_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
                "playObjective": "route_choice",
            })
            + "</GUIDANCE>"
        )
        result, client = self.execute(
            [content, content],
            rows=OPERATION_BASE_ROWS,
            language="en",
        )

        self.assertEqual(result.assistant_message, "I would open the cited local route.")
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(result.guidance["move"], "offer_perspective")
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_ordinary_grounding_failure_uses_chat_fallback_not_edit_clarification(self):
        result, client = self.execute(
            ["B1 is at (1,1), so the lower-left cluster is the key design issue."] * 2,
            rows=ENTITY_ROUTE_ROWS,
            conversation=[{
                "role": "user",
                "content": "I only feel the lower-left cluster in the first version is crowded.",
            }],
        )

        self.assertEqual(result.model, "kimi-k2.6")
        self.assertEqual(result.guidance["move"], "offer_perspective")
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertNotIn("confirm the location", result.assistant_message)
        self.assertNotIn("请重新确认要修改", result.assistant_message)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_historical_coordinate_claim_requires_a_historical_snapshot(self):
        with self.assertRaisesRegex(ValueError, "historical Stage map claim"):
            llm_client._validate_map_grounding_texts(
                ["In the first version B1 was at (1,1)."],
                ENTITY_ROUTE_ROWS,
                historical_reference=True,
            )

        llm_client._validate_map_grounding_texts(
            ["The first version felt crowded in the lower-left."],
            ENTITY_ROUTE_ROWS,
            historical_reference=True,
        )

    def test_recovery_preserves_a_valid_route_after_spatial_context(self):
        body = (
            "B1 is beside P, while B2 is near T2; the player should go around the water "
            "from B1 toward T1."
        )

        links = llm_client._recover_coordinate_links(body, ENTITY_ROUTE_ROWS)
        self.assertEqual(len(links), 1)
        self.assertIn("from B1 toward T1", links[0]["text"])
        self.assertEqual(links[0]["from"], {"row": 3, "column": 5})
        self.assertEqual(links[0]["to"], {"row": 3, "column": 7})

    def test_route_recovery_splits_waypoints_and_independent_routes(self):
        body = (
            "B1 moves through (6,3) toward T1. "
            "B2 moves from (6,4) to T2."
        )

        links = llm_client._recover_coordinate_links(body, ENTITY_ROUTE_ROWS)

        self.assertEqual(len(links), 3)
        self.assertEqual(
            [(item["from"], item["to"]) for item in links],
            [
                ({"row": 3, "column": 5}, {"row": 6, "column": 3}),
                ({"row": 6, "column": 3}, {"row": 3, "column": 7}),
                ({"row": 6, "column": 4}, {"row": 6, "column": 8}),
            ],
        )

    def test_route_recovery_does_not_mark_position_only_relations(self):
        body = "B1 is to the left of T1, while B2 is beside T2."

        self.assertEqual(
            llm_client._recover_coordinate_links(body, ENTITY_ROUTE_ROWS),
            [],
        )

        same_sentence = "B1 moves to T1, while B2 moves to T2."
        links = llm_client._recover_coordinate_links(
            same_sentence,
            ENTITY_ROUTE_ROWS,
        )
        self.assertEqual(len(links), 2)

    def test_map_grounding_rejects_the_reported_wrong_corner_and_water_claim(self):
        with self.assertRaisesRegex(ValueError, "upper-right"):
            llm_client._validate_map_grounding_texts(
                ["The upper-right box is next to water."], MAP_GROUNDING_ROWS
            )

    def test_map_grounding_rejects_wrong_explicit_coordinate_tile(self):
        with self.assertRaisesRegex(ValueError, "row 2, column 2"):
            llm_client._validate_map_grounding_texts(
                ["把(2,2)从墙变成地板。"],
                OPERATION_BASE_ROWS,
            )

        with self.assertRaisesRegex(ValueError, "touches water"):
            llm_client._validate_map_grounding_texts(
                ["The box is next to water."], MAP_GROUNDING_ROWS
            )

    def test_plain_reply_retries_once_after_a_map_grounding_error(self):
        result, client = self.execute(
            [
                "The upper-right box is next to water.",
                "B1 is at row 3, column 5, away from the water; I would watch its first push.",
            ],
            rows=MAP_GROUNDING_ROWS,
        )

        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertNotIn("next to water", result.assistant_message)

    def test_plain_reply_normalizes_stages_that_are_mistaken_for_separate_levels(self):
        result, _ = self.execute([
            "这种留一点犹豫空间的做法，很符合第二关该有的手感。"
            "这个判断会影响后面关卡里的目标摆放。"
        ], language="zh-CN")

        self.assertIn("这个版本现在呈现出的手感", result.assistant_message)
        self.assertIn("后续版本里的目标摆放", result.assistant_message)
        self.assertNotIn("第二关", result.assistant_message)
        self.assertNotIn("后面关卡", result.assistant_message)

    def test_plain_chat_cannot_claim_an_unsaved_map_change_is_finished(self):
        result, _ = self.execute([
            "我会让右侧水域贴近推动路线。\n\n改好了，你去试试看。"
        ], language="zh-CN")

        self.assertIn("生成可审查的方案后再由你决定是否采用", result.assistant_message)
        self.assertNotIn("改好了", result.assistant_message)
        self.assertNotIn("去试试看", result.assistant_message)

    def test_plain_chat_cannot_describe_an_uncreated_pending_proposal_as_real(self):
        result, _ = self.execute([
            "好，那我把第7行第6到第8列连成一片水，其余格子不动。"
            "这份提案会保持待审查状态。"
        ], language="zh-CN")

        self.assertIn("还没有实际落到地图上", result.assistant_message)
        self.assertNotIn("会保持待审查状态", result.assistant_message)

    def test_generic_change_authorization_is_not_saved_as_design_intent(self):
        visible, intent, _, _ = llm_client._extract_plain_guidance(
            "我先把方向说清楚。\n<GUIDANCE>INTENT: 我暂时把你的方向理解为：你帮我改。"
            "</GUIDANCE>",
            "zh-CN",
            {},
        )

        self.assertEqual(visible, "我先把方向说清楚。")
        self.assertIsNone(intent)

        _, intent, _, _ = llm_client._extract_plain_guidance(
            "我先把方向说清楚。\n<GUIDANCE>INTENT: 我读到的倾向是，你希望后续设计回应“帮我做”。"
            "</GUIDANCE>",
            "zh-CN",
            {},
        )
        self.assertIsNone(intent)

    def test_plain_discuss_card_can_hold_a_first_person_design_insight(self):
        result, _ = self.execute([
            "这个版本的下半区水边多了一点回旋空间。\n"
            "<GUIDANCE>DISCUSS: 我更喜欢水边这次留下的路线犹豫，它让第一次推动的选择更有分量。</GUIDANCE>"
        ], language="zh-CN")

        focus = result.guidance["followUpQuestion"]
        self.assertIn("我更喜欢", focus)
        self.assertIn("第一次推动", focus)
        self.assertNotIn("GUIDANCE", result.assistant_message)

    def test_repeated_plain_discuss_card_is_omitted_when_no_new_focus_is_needed(self):
        content = (
            "这个版本的下半区多了一点回旋空间。\n"
            "<GUIDANCE>DISCUSS: 我更喜欢水边留下的路线犹豫，它让第一次推动的选择更有分量。</GUIDANCE>"
        )
        result, _ = self.execute(
            [content],
            language="zh-CN",
            stage_context={
                "recentGuidance": {
                    "discussionFocus": (
                        "我更喜欢水边留下的路线犹豫，它让第一次推动的选择更有分量。"
                    )
                }
            },
        )

        focus = result.guidance["followUpQuestion"]
        self.assertIsNone(focus)

    def test_plain_reply_extracts_intent_card(self):
        result, _ = self.execute([
            "The water now reads as part of the route.\n\n"
            "<GUIDANCE>\n"
            "INTENT: The designer wants the water to affect the route.\n"
            "</GUIDANCE>"
        ])

        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertEqual(result.guidance["intentConfidence"], "medium")
        self.assertTrue(result.guidance["intentHypothesis"].startswith("I read your preference"))
        self.assertNotIn("GUIDANCE", result.assistant_message)

    def test_plain_reply_extracts_proposal_card(self):
        result, _ = self.execute([
            "A small route linkage would make the water consequential.\n\n"
            "<GUIDANCE>\n"
            "PROPOSAL_SUMMARY: Link the lower target to the water edge\n"
            "PROPOSAL_RATIONALE: Make the first push depend on reading the water route\n"
            "</GUIDANCE>"
        ])

        self.assertNotEqual(result.guidance["move"], "offer_revision")
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertNotIn("PROPOSAL_SUMMARY", result.assistant_message)

    def test_plain_reply_can_extract_intent_and_proposal_cards_together(self):
        result, _ = self.execute([
            "That gives us a focused next move.\n\n"
            "<GUIDANCE>\n"
            "INTENT: You want the water to shape the route without dominating it\n"
            "PROPOSAL_SUMMARY: Move the target beneath the water edge\n"
            "PROPOSAL_RATIONALE: Tie one push decision to the water while preserving the main route\n"
            "</GUIDANCE>"
        ])

        self.assertNotEqual(result.guidance["move"], "offer_revision")
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_guidance_card_policy_keeps_only_the_confirmed_families(self):
        discussion = llm_client._apply_guidance_card_policy({
            "intentHypothesis": "I read your preference as preserving the upper route.",
            "intentConfidence": "medium",
            "followUpQuestion": "Which first push should carry the route judgment?",
            "proposalOffer": None,
            "uiCues": [{"type": "warning", "text": "The box may lose its return route."}],
        })
        self.assertIsNotNone(discussion["intentHypothesis"])
        self.assertIsNotNone(discussion["followUpQuestion"])
        self.assertEqual([cue["type"] for cue in discussion["uiCues"]], ["warning"])

        action = llm_client._apply_guidance_card_policy({
            "intentHypothesis": "This must be removed.",
            "intentConfidence": "medium",
            "followUpQuestion": "This must be removed too?",
            "proposalOffer": {"summary": "Move the target", "rationale": "Change the route"},
            "uiCues": [
                {"type": "manual_edit", "text": "Try the same area in the editor."},
                {"type": "warning", "text": "The box may lose its return route."},
            ],
        })
        self.assertIsNone(action["intentHypothesis"])
        self.assertIsNone(action["followUpQuestion"])
        self.assertEqual(
            [cue["type"] for cue in action["uiCues"]],
            ["manual_edit", "warning"],
        )

    def test_non_opening_replies_do_not_force_a_discussion_card(self):
        result, _ = self.execute(["The route remains readable."])
        self.assertIsNone(result.guidance["followUpQuestion"])

        client = FakeClient(["当然，我们可以先聊点别的。"])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            off_topic = llm_client.generate_chat_reply(
                [{"role": "user", "content": "这个与地图无关，我们换个话题。"}],
                ["############"] * 10,
                "off-topic-card-test",
                language="zh-CN",
            )
        self.assertIsNone(off_topic.guidance["followUpQuestion"])
        self.assertIsNone(off_topic.guidance["intentHypothesis"])
        self.assertEqual(off_topic.guidance["uiCues"], [])

    def test_proposal_card_distills_body_copy_into_title_and_play_rationale(self):
        copied = (
            "这个想法挺自然的——把其中一个目标点挪到下方空旷区域，确实能直接平衡两边的密度。"
        )
        client = FakeClient([
            copied + "\n<GUIDANCE>PROPOSAL_SUMMARY: " + copied
            + " || PROPOSAL_RATIONALE: 你说得对，上下半区的密度落差确实一眼就能看出来。"
            + "</GUIDANCE>"
        ])
        conversation = [
            {"role": "assistant", "content": "你说得对，上下半区的密度落差确实一眼就能看出来。"},
            {"role": "user", "content": "展开讲讲"},
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                ["############"] * 10,
                "distilled-proposal-card-test",
                language="zh-CN",
            )

        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIsNone(result.guidance["intentHypothesis"])

    def test_proposal_card_rejects_a_bare_confirmation_as_its_title(self):
        content = (
            "好，那就往旁边挪一格。我倾向于挪右边那个箱子，让左边箱子先有可读的下推空间。\n"
            "<GUIDANCE>PROPOSAL_SUMMARY: 好 || "
            "PROPOSAL_RATIONALE: 让两个箱子的推进顺序更清楚。</GUIDANCE>"
        )
        result, _ = self.execute([content], language="zh-CN")

        self.assertIsNone(result.guidance["proposalOffer"])

    def test_revision_direction_ignores_transition_meta_language(self):
        meta = "\u8fd9\u4e2a\u5224\u65ad\u4f1a\u76f4\u63a5\u5f71\u54cd\u6211\u63a5\u4e0b\u6765\u5efa\u8bae\u600e\u4e48\u8c03\u6574\u8def\u7ebf\u3002"
        concrete = (
            "\u5982\u679c\u60f3\u8ba9\u6574\u4f53\u65f6\u95f4\u62c9\u957f\uff0c\u53ef\u4ee5\u628a B2 \u7684\u8def\u7ebf\u7a0d\u5fae\u7ed5\u4e00\u70b9\uff0c"
            "\u8ba9\u73a9\u5bb6\u5148\u7ecf\u8fc7\u4e2d\u8f6c\u4f4d\u518d\u7ee7\u7eed\u63a8\u3002"
        )

        direction = llm_client._revision_direction_sentence(meta + "\n" + concrete)

        self.assertEqual(direction, concrete)
        self.assertFalse(llm_client._proposal_card_is_meta_language(direction))
        for transition in (
            "\u63a5\u4e0b\u6765\u6211\u5efa\u8bae\u5148\u770b\u8def\u7ebf\u3002",
            "\u6211\u4f1a\u6839\u636e\u8fd9\u4e2a\u5224\u65ad\u8c03\u6574\u8def\u7ebf\u3002",
            "\u6211\u6ce8\u610f\u5230\u4f60\u8fdb\u884c\u4e86\u4fee\u6539\u3002",
        ):
            self.assertTrue(llm_client._proposal_card_is_meta_language(transition))
            self.assertEqual(llm_client._revision_direction_sentence(transition), "")

    def test_invalid_proposal_card_is_retried_and_repaired_in_the_same_reply(self):
        meta = "\u8fd9\u4e2a\u5224\u65ad\u4f1a\u76f4\u63a5\u5f71\u54cd\u6211\u63a5\u4e0b\u6765\u5efa\u8bae\u600e\u4e48\u8c03\u6574\u8def\u7ebf"
        body = (
            "\u5982\u679c\u60f3\u8ba9\u6574\u4f53\u65f6\u95f4\u62c9\u957f\uff0c\u6211\u5efa\u8bae\u628a\u5173\u952e\u7bb1\u5b50\u7684\u8def\u7ebf\u7a0d\u5fae\u7ed5\u4e00\u70b9\uff0c"
            "\u8ba9\u73a9\u5bb6\u5148\u7ecf\u8fc7\u4e2d\u8f6c\u4f4d\u518d\u7ee7\u7eed\u63a8\u3002\n\n"
            + meta
        )
        invalid = (
            body
            + "\n<GUIDANCE>\nPROPOSAL_SUMMARY: "
            + meta
            + "\nPROPOSAL_RATIONALE: \u5177\u4f53\u505a\u6cd5\u662f\uff1a"
            + meta
            + "\u3002\u6211\u6ce8\u610f\u5230\u4f60\u8fdb\u884c\u4e86\u4fee\u6539\u3002\n</GUIDANCE>"
        )
        conversation = [{
            "role": "user",
            "content": "\u6211\u5e0c\u671b\u8ba9\u7528\u6237\u53ef\u4ee5\u82b1\u66f4\u591a\u65f6\u95f4\u6765\u6e38\u73a9\uff0c\u8bf7\u7ed9\u6211\u4e00\u4e2a\u65b9\u6848",
        }]

        result, client = self.execute(
            [invalid, invalid],
            rows=MAP_GROUNDING_ROWS,
            conversation=conversation,
            language="zh-CN",
        )

        offer = result.guidance["proposalOffer"]
        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIsNone(offer)
        self.assertIn("\u5173\u952e\u7bb1\u5b50", result.assistant_message)
        self.assertNotEqual(result.guidance["move"], "offer_revision")

    def test_composing_a_reply_removes_repeated_card_text_but_keeps_analysis(self):
        body = (
            "\u6211\u4f1a\u5148\u770b\u5173\u952e\u7bb1\u5b50\u7684\u7b2c\u4e00\u6b21\u63a8\u8fdb\uff0c\u56e0\u4e3a\u8fd9\u91cc\u80fd\u770b\u51fa\u8def\u7ebf\u662f\u5426\u771f\u7684\u53d8\u5f97\u66f4\u6709\u9009\u62e9\u3002\n\n"
            "\u8ba9\u5173\u952e\u7bb1\u5b50\u8def\u7ebf\u5f62\u6210\u7ed5\u884c\u9009\u62e9\u3002"
        )
        summary = "\u8ba9\u5173\u952e\u7bb1\u5b50\u8def\u7ebf\u5f62\u6210\u7ed5\u884c\u9009\u62e9"
        message = llm_client._compose_assistant_message(
            body,
            {
                "move": "offer_revision",
                "proposalOffer": {
                    "summary": summary,
                    "rationale": "\u8ba9\u73a9\u5bb6\u5728\u63a8\u7bb1\u524d\u6bd4\u8f83\u76f4\u8def\u548c\u7ed5\u884c\u8def\u7ebf\u3002",
                },
                "uiCues": [],
            },
            "zh-CN",
            False,
            {},
        )

        self.assertIn("\u7b2c\u4e00\u6b21\u63a8\u8fdb", message)
        self.assertEqual(message.count(summary), 0)

        embedded = (
            "\u8ba9\u5173\u952e\u7bb1\u5b50\u8def\u7ebf\u5f62\u6210\u7ed5\u884c\u9009\u62e9\uff0c\u5e76\u4e14\u8ba9\u73a9\u5bb6\u5728\u7b2c\u4e00\u6b21\u63a8\u8fdb\u524d\u8bfb\u61c2\u4e2d\u8f6c\u4f4d\u3002"
        )
        embedded_message = llm_client._compose_assistant_message(
            embedded,
            {
                "move": "offer_revision",
                "proposalOffer": {
                    "summary": summary,
                    "rationale": "\u8ba9\u73a9\u5bb6\u5728\u63a8\u7bb1\u524d\u6bd4\u8f83\u76f4\u8def\u548c\u7ed5\u884c\u8def\u7ebf\u3002",
                },
                "uiCues": [],
            },
            "zh-CN",
            False,
            {},
        )
        self.assertIn("\u4e2d\u8f6c\u4f4d", embedded_message)

    def test_malformed_guidance_is_hidden_without_failing_reply(self):
        for reply in (
            "Visible reply.\n<GUIDANCE>\nINTENT: You want a tighter route",
            "Visible reply.\n<GUIDANCE>\nUNKNOWN: hidden\n</GUIDANCE>",
            "Visible reply.\n<GUIDANCE>\nPROPOSAL_SUMMARY: Incomplete\n</GUIDANCE>",
        ):
            with self.subTest(reply=reply):
                result, _ = self.execute([reply])
                self.assertTrue(result.assistant_message.startswith("Visible reply."))
                self.assertIsNone(result.guidance["intentHypothesis"])
                self.assertIsNone(result.guidance["proposalOffer"])
                self.assertIsNone(result.guidance["followUpQuestion"])

    def test_repeated_guidance_cards_are_suppressed_but_changed_intent_remains(self):
        content = (
            "This direction is now concrete.\n"
            "<GUIDANCE>\n"
            "INTENT: I think you may want water to shape the route\n"
            "PROPOSAL_SUMMARY: Link the target to the water edge\n"
            "PROPOSAL_RATIONALE: Make water influence the first push\n"
            "</GUIDANCE>"
        )
        client = FakeClient([content])
        stage_context = {
            "recentGuidance": {
                "intentHypothesis": "I think you may want water to shape the route.",
                "proposalOffer": {
                    "summary": "Link the target to the water edge",
                    "rationale": "Make water influence the first push.",
                },
            }
        }

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Yes, do that."}],
                ["############"] * 10,
                "guidance-dedup-test",
                stage_context=stage_context,
            )

        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["proposalOffer"])

        _, changed_intent, _, _ = llm_client._extract_plain_guidance(
            "Reply.\n<GUIDANCE>\nINTENT: I think you may want water to control two route decisions\n</GUIDANCE>",
            "en",
            stage_context,
        )
        self.assertIsNotNone(changed_intent)

    def test_plain_prompt_asks_at_real_decision_points_without_a_fixed_template(self):
        messages = llm_client.build_plain_chat_messages(
            [{"role": "user", "content": "Make the choice smaller but consequential."}],
            ["############"] * 10,
        )

        self.assertIn("genuine map-specific unresolved", messages[0]["content"])
        self.assertIn("At a real decision point", messages[0]["content"])
        self.assertIn("<GUIDANCE>", messages[0]["content"])
        self.assertIn("recentGuidance", messages[0]["content"])
        self.assertIn("whenever no card is warranted", messages[0]["content"])
        self.assertIn("never produce four cards", messages[0]["content"])
        self.assertIn("2-4 paragraphs with 2-4 sentences per paragraph", messages[0]["content"])
        self.assertIn("up to four compact route passages", messages[0]["content"])
        self.assertIn("Connect specific map details to a playable moment", messages[0]["content"])
        self.assertIn("Post-opening progress and route rule", messages[0]["content"])
        self.assertIn("COORDINATE_LINKS", messages[0]["content"])

    def test_chat_prompt_carries_confirmed_decisions_and_open_questions_naturally(self):
        stage_context = {
            "progressContext": {
                "currentStage": {"stageNumber": 2, "versionId": "stage-2"},
                "confirmedDecisions": [{
                    "decision": "Keep the direct opening",
                    "sourceStageNumber": 1,
                }],
                "unresolvedQuestions": [{
                    "question": "Can the player read the B2 to T1 detour?",
                    "sourceStageNumber": 2,
                }],
            }
        }

        structured = llm_client.build_chat_messages(
            [{"role": "user", "content": "The extra corridor is now open."}],
            OPERATION_BASE_ROWS,
            stage_context=stage_context,
        )
        plain = llm_client.build_plain_chat_messages(
            [{"role": "user", "content": "The extra corridor is now open."}],
            OPERATION_BASE_ROWS,
            stage_context=stage_context,
        )

        for messages in (structured, plain):
            prompt = messages[0]["content"]
            self.assertIn("Continuous progress context", prompt)
            self.assertIn("Keep the direct opening", prompt)
            self.assertIn("Can the player read the B2 to T1 detour?", prompt)
            self.assertIn("do not add a fixed progress heading", prompt.lower())
            self.assertIn("Post-opening", prompt)
            self.assertTrue(
                "coordinateLinks" in prompt or "COORDINATE_LINKS" in prompt
            )

    def test_stage_opening_prompt_does_not_request_continuous_progress(self):
        structured = llm_client.build_chat_messages(
            [],
            OPERATION_BASE_ROWS,
            assessment_only=True,
        )
        plain = llm_client.build_plain_chat_messages(
            [],
            OPERATION_BASE_ROWS,
            stage_opening=True,
        )

        self.assertNotIn("Post-opening progress", structured[0]["content"])
        self.assertNotIn("Post-opening progress", plain[0]["content"])

    def test_pure_generic_question_uses_fallback_model(self):
        result, client = self.execute([
            "What do you think?",
            "The water-side route could carry more of the decision.",
        ])

        self.assertIn("water-side route", result.assistant_message)
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(client.chat.completions.calls[1]["model"], "kimi-k2.6")
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_two_pure_generic_questions_return_low_quality_error(self):
        client = FakeClient(["What do you think?", "Is this direction okay?"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            with self.assertRaises(llm_client.LLMServiceError) as raised:
                llm_client.generate_chat_reply(
                    [{"role": "user", "content": "Assess the level."}],
                    ["############"] * 10,
                    "low-quality-test",
                )

        self.assertEqual(raised.exception.code, "MODEL_LOW_QUALITY_RESPONSE")
        self.assertEqual(raised.exception.attempts_used, 2)

    def test_multiple_questions_stay_in_body_without_failing(self):
        reply = "What should stay? What should change?"
        result, _ = self.execute([reply])

        self.assertTrue(result.assistant_message.startswith(reply))
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_redundant_question_is_removed_after_an_explicit_direction(self):
        client = FakeClient([
            "That would make the opening commitment more legible. What else do you prefer?"
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Keep it compact, but make the first push consequential."}],
                ["############"] * 10,
                "no-forced-question-test",
            )

        self.assertTrue(result.assistant_message.startswith(
            "That would make the opening commitment more legible."
        ))
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertEqual(result.guidance["uiCues"], [])

    def test_explicit_direction_does_not_keep_a_routine_question_when_the_reply_is_clear(self):
        client = FakeClient([
            "That makes the first commitment more legible. When the box enters the "
            "water-side corridor, which route should the player notice first so we can "
            "judge the opening's readability?"
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Keep it compact, but make the first push consequential."}],
                ["############"] * 10,
                "deeper-question-test",
            )

        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertNotIn("water-side corridor", result.assistant_message.split("\n\n")[0])

    def test_plain_guidance_extracts_warning_and_manual_edit(self):
        result, _ = self.execute([
            "The water can shape the first route decision.\n"
            "<GUIDANCE>WARNING: The box may lose its escape route beside the water after "
            "the first push || MANUAL_EDIT: Try a small experiment around the water edge "
            "and watch whether the route choice becomes clearer</GUIDANCE>"
        ], rows=["############", "#p.s @  t  #"] + ["############"] * 8)

        self.assertEqual(
            [cue["type"] for cue in result.guidance["uiCues"]],
            ["manual_edit"],
        )
        self.assertNotIn("GUIDANCE", result.assistant_message)
        self.assertLessEqual(len(result.guidance["uiCues"]), 2)

    def test_invalid_or_duplicate_ui_cue_fields_do_not_break_visible_reply(self):
        visible, _, _, cues = llm_client._extract_plain_guidance(
            "Visible reply.\n<GUIDANCE>WARNING: The water colors look dull || "
            "WARNING: The box may be stuck beside the water || UNKNOWN: hidden || "
            "MANUAL_EDIT: Move the wall at row 3 and observe it</GUIDANCE>",
            "en",
            {},
        )

        self.assertEqual(visible, "Visible reply.")
        self.assertEqual(cues, [])

    def test_ui_cue_dedup_allows_same_warning_after_evidence_changes(self):
        content = (
            "Visible reply.\n<GUIDANCE>WARNING: The box may lose its escape route "
            "beside the water after the first push</GUIDANCE>"
        )
        recent = {
            "recentGuidance": {
                "uiCues": {
                    "warning": {
                        "text": "The box may lose its escape route beside the water after the first push.",
                        "evidenceSignature": "same",
                    }
                }
            },
            "guidanceEvidenceSignature": "same",
        }
        _, _, _, same_cues = llm_client._extract_plain_guidance(content, "en", recent)
        recent["guidanceEvidenceSignature"] = "changed"
        _, _, _, changed_cues = llm_client._extract_plain_guidance(content, "en", recent)

        self.assertEqual(same_cues, [])
        self.assertEqual(changed_cues[0]["type"], "warning")

    def test_human_edit_without_play_evidence_does_not_get_automatic_warning(self):
        self.assertIsNone(llm_client._deterministic_play_warning("en", None))

    def test_contextual_manual_edit_avoids_exact_coordinates(self):
        cue = llm_client._contextual_manual_edit(
            ["############", "#p   @     #", "#    @ s t #"] + ["############"] * 7,
            "en",
        )

        self.assertIn("water edge", cue)
        self.assertIn("watch", cue)
        self.assertNotRegex(cue, r"\b(?:row|column)\s+\d+")

    def test_evidence_grounded_warning_is_extracted_but_aesthetic_opinion_is_not(self):
        rows = ["############", "#p.s @  t  #"] + ["############"] * 8
        warning = llm_client._warning_has_strong_evidence(
            "I notice the box may lose its escape route beside the water after the first push.",
            "en",
            rows,
            {},
            None,
        )
        aesthetic = llm_client._warning_has_strong_evidence(
            "The water looks too plain and the colors feel dull.",
            "en",
            rows,
            {},
            None,
        )

        self.assertTrue(warning)
        self.assertFalse(aesthetic)

    def test_general_route_uncertainty_is_not_promoted_to_warning(self):
        strong = llm_client._warning_has_strong_evidence(
            "I am concerned the route may feel unclear after the first push.",
            "en",
            ["############", "#p.s @  t  #"] + ["############"] * 8,
            {},
            None,
        )

        self.assertFalse(strong)

    def test_play_restart_produces_a_first_person_warning(self):
        warning = llm_client._deterministic_play_warning(
            "zh-CN",
            {"restartCount": 2, "moveCount": 30, "minimumMoves": 20},
        )

        self.assertTrue(warning.startswith("我注意到"))
        self.assertIn("重开了 2 次", warning)

    def test_intent_fallback_varies_naturally_with_context(self):
        first = llm_client._natural_intent_candidate(
            "a route with water",
            "en",
            False,
        )
        second = llm_client._natural_intent_candidate(
            "b route with water",
            "en",
            False,
        )

        self.assertNotEqual(first.split(":", 1)[0], second.split(":", 1)[0])
        self.assertNotIn("designer", (first + second).casefold())
        self.assertNotIn("player wants", (first + second).casefold())

    def test_specific_vivid_question_is_suppressed_when_no_uncertainty_is_stated(self):
        result, _ = self.execute([
            "The water edge can make the route legible. When the box enters the corridor "
            "beside the water, which route choice should the player notice first so we can "
            "judge its readability?"
        ])

        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertNotIn("?", result.assistant_message.split("\n\n", 1)[0])

    def test_consecutive_question_repeating_the_same_judgment_is_suppressed(self):
        client = FakeClient([
            "The route remains focused. When the box enters the water-side corridor, "
            "which route choice should the player notice first so we can judge readability?"
        ])
        conversation = [
            {
                "role": "assistant",
                "content": (
                    "I am looking at the same moment. When the box enters the water-side "
                    "corridor, which route choice should the player notice first so we can "
                    "judge readability?"
                ),
            },
            {"role": "user", "content": "The upper route."},
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                ["############"] * 10,
                "question-dedup-test",
            )

        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_generic_question_is_removed_when_declarative_body_exists(self):
        result, client = self.execute([
            "Moving the target beside the water would create a route decision. "
            "Does this direction work?"
        ])

        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertNotIn("Does this direction work", result.assistant_message)

    def test_ordinary_question_stays_in_body_when_new_discussion_cards_are_disabled(self):
        result, _ = self.execute(
            [
                "The lower box now has a more deliberate approach. "
                "Which first push should carry the route judgment?"
            ],
            stage_context={"discussionCardMode": "disagreement_only"},
        )

        self.assertIsNone(result.guidance["disagreement"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIn("Which first push should carry the route judgment?", result.assistant_message)

    def test_clear_first_person_evaluation_gets_an_intent_card_when_model_omits_one(self):
        client = FakeClient(["我更倾向于让水域真正参与路线，而不是只做背景。"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "我觉得水域现在只是摆设。"}],
                ["############"] * 10,
                "question-frequency-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_user_difficulty_reframe_gets_a_tentative_intent_card(self):
        result, _ = self.execute(
            [
                "我会继续看第一次推箱时的路线判断，避免把难度只理解成增加障碍。"
            ],
            language="zh-CN",
            conversation=[
                {
                    "role": "assistant",
                    "content": "在我看来，这张图目前更像顺着流程完成，而不是被很难的谜题卡住。",
                },
                {"role": "user", "content": "我认为还是太简单了。"},
            ],
        )

        intent = result.guidance["intentHypothesis"]
        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertEqual(result.guidance["intentConfidence"], "medium")
        self.assertIsNotNone(intent)
        self.assertIn("你", intent)
        self.assertIn("推", intent)

    def test_tentative_intent_rephrases_an_echoed_water_instruction(self):
        user_message = "我倒是认为得改动水域的形状"
        result, _ = self.execute(
            [
                "我同意，水现在更像装饰而不是路线边界。\n"
                "<GUIDANCE>INTENT: 我暂时把你的方向理解为：我倒是认为得改动水域的形状</GUIDANCE>"
            ],
            language="zh-CN",
            conversation=[
                {"role": "assistant", "content": "我原本认为补一个缺口就够。"},
                {"role": "user", "content": user_message},
            ],
        )

        intent = result.guidance["intentHypothesis"]
        self.assertIsNotNone(intent)
        self.assertNotIn(user_message, intent)
        self.assertIn("水", intent)
        self.assertTrue(any(marker in intent for marker in ("路线", "推进", "绕行", "选择")))
        self.assertNotIn("设计者", intent)

    def test_direction_question_goes_deeper_instead_of_asking_for_approval(self):
        question = llm_client._deterministic_key_question(
            [{"role": "user", "content": "我想让箱子贴着水边推进时更有路线判断。"}],
            "zh-CN",
            ["############"] * 10,
        )

        self.assertIn("第一次", question)
        self.assertIn("路线", question)
        self.assertNotIn("方向如何", question)
        self.assertNotIn("可行吗", question)

    def test_chinese_agreement_is_treated_as_an_explicit_direction(self):
        self.assertTrue(llm_client._latest_user_states_direction([
            {"role": "user", "content": "做点联动吧"},
        ]))

    def test_guidance_request_classifier_prefers_revision_advice_for_a_concrete_goal(self):
        messages = (
            "给我一个方案，我想让玩家花更多时间绕过障碍物。",
            "I want the player to spend more time detouring around obstacles; can you suggest a plan?",
            "Please suggest a concrete revision: change (2,4) from floor to wall.",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    llm_client.classify_guidance_request([
                        {"role": "user", "content": message},
                    ]),
                    "revision_advice",
                )

    def test_guidance_request_classifier_routes_open_ended_help_to_discussion(self):
        messages = (
            "我有点迷茫，不知道这关怎么改，给我点思路。",
            "你觉得水域应该怎么改？",
            "I am not sure where to start. Can you give me some ideas?",
            "How would you change the route?",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    llm_client.classify_guidance_request([
                        {"role": "user", "content": message},
                    ]),
                    "discussion",
                )

    def test_guidance_request_classifier_keeps_undirected_edit_command_on_existing_path(self):
        self.assertEqual(
            llm_client.classify_guidance_request([
                {"role": "user", "content": "帮我改"},
            ]),
            "none",
        )

    def test_concrete_advice_request_gets_a_proposal_card_when_model_omits_metadata(self):
        result, client = self.execute(
            [
                "我会先看这张图的路线节奏，再说明可以怎样调整。",
                "我会先看这张图的路线节奏，再说明可以怎样调整。",
            ],
            rows=MAP_GROUNDING_ROWS,
            language="zh-CN",
            conversation=[{
                "role": "user",
                "content": "给我一个方案，我想让玩家花更多时间绕过障碍物。",
            }],
        )

        self.assertEqual(len(client.chat.completions.calls), 2)
        self.assertNotEqual(result.guidance["move"], "offer_revision")
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertEqual(result.guidance["uiCues"], [])
        self.assertNotIn("could not form a proposal", result.assistant_message.casefold())
        self.assertNotIn("fully consistent with the saved map", result.assistant_message.casefold())
        self.assertTrue(result.assistant_message.strip())
        self.assertIn(
            "REVISION_ADVICE",
            client.chat.completions.calls[0]["messages"][0]["content"],
        )
        self.assertIn(
            "required guidance card",
            client.chat.completions.calls[1]["messages"][0]["content"],
        )

    def test_concrete_english_coordinate_request_keeps_execution_brief_when_model_omits_metadata(self):
        result, client = self.execute(
            [
                "I would focus on the local opening.",
                "I would focus on the local opening.",
            ],
            rows=OPERATION_BASE_ROWS,
            language="en",
            conversation=[{
                "role": "user",
                "content": (
                    "Please suggest a concrete revision: change (2,2) from floor to wall, "
                    "while keeping the boxes and targets unchanged."
                ),
            }],
        )

        self.assertNotEqual(result.guidance["move"], "offer_revision")
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_revision_advice_rejects_multiple_options_and_shows_clarification(self):
        multi_option_reply = (
            "Option A: close the upper opening. Option B: move the target instead."
        )
        result, client = self.execute(
            [multi_option_reply, multi_option_reply],
            rows=OPERATION_BASE_ROWS,
            language="en",
            conversation=[{
                "role": "user",
                "content": (
                    "Please suggest a concrete revision: change (2,2) from floor to wall, "
                    "while keeping the boxes and targets unchanged."
                ),
            }],
        )

        self.assertEqual(len(client.chat.completions.calls), 2)
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertEqual(result.guidance["uiCues"], [])
        self.assertNotIn("could not form a proposal", result.assistant_message.casefold())
        self.assertNotIn("fully consistent with the saved map", result.assistant_message.casefold())
        self.assertNotIn("Option A", result.assistant_message)

    def test_response_paragraphs_are_balanced_without_dropping_route_detail(self):
        body = (
            "The first route choice is visible. It gives the player a clear comparison. "
            "The water then changes the push order.\n\n"
            "The next moment should remain testable.\n\n"
            "The target approach stays local.\n\n"
            "The player can compare the detour.\n\n"
            "The result should change play.\n\n"
            "The final check is whether the choice reads.\n\n"
            "The map should remain solvable."
        )

        normalized = llm_client._normalize_response_paragraphs(body)
        paragraphs = normalized.split("\n\n")

        self.assertLessEqual(len(paragraphs), llm_client.CHAT_MAX_PARAGRAPHS)
        self.assertTrue(all(1 <= len(re.findall(r"[.!?]", paragraph)) <= 4 for paragraph in paragraphs))
        for sentence in (
            "The first route choice is visible.",
            "The map should remain solvable.",
        ):
            self.assertIn(sentence, normalized)

    def test_open_ended_help_gets_a_grounded_discussion_card_when_model_omits_metadata(self):
        result, client = self.execute(
            ["我可以先陪你看看这张图。"],
            rows=MAP_GROUNDING_ROWS,
            language="zh-CN",
            conversation=[{
                "role": "user",
                "content": "我有点迷茫，不知道这关怎么改，给我点思路。",
            }],
        )

        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertEqual(result.guidance["move"], "clarify_intent")
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertTrue(result.guidance["followUpQuestion"])
        self.assertFalse(any(
            cue["type"] == "manual_edit"
            for cue in result.guidance["uiCues"]
        ))
        self.assertIn(
            "DISCUSSION",
            client.chat.completions.calls[0]["messages"][0]["content"],
        )

    def test_any_first_person_stance_gets_a_correctable_intent_without_a_forced_discussion_card(self):
        guidance = llm_client._ensure_required_guidance_card(
            {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            [{"role": "user", "content": "我认为中间的水域仍然太像装饰。"}],
            "zh-CN",
            OPERATION_BASE_ROWS,
            False,
            {},
        )
        self.assertIsNotNone(guidance["intentHypothesis"])
        self.assertEqual(guidance["intentConfidence"], "medium")
        self.assertIsNone(guidance["followUpQuestion"])

    def test_stage_one_opening_states_the_small_change_boundary(self):
        body = llm_client._ensure_stage_one_orientation(
            "我先看到水域让中间路线有了分隔。",
            OPERATION_BASE_ROWS,
            "zh-CN",
        )
        self.assertIn("小范围更改、提供可审查的改动内容", body)
        self.assertIn("较大的改动我建议由你亲手试一试", body)

    def test_stage_one_opening_keeps_map_observation_but_compacts_process_guidance(self):
        body = llm_client._ensure_stage_one_orientation(
            "我觉得中间水域让箱子第一次靠近目标时需要重新判断路线。\n\n"
            "你可以先试玩，或者直接在右侧面板里做局部调整。"
            "你可以先从直觉说起；想验证时，右侧的试玩和局部编辑都可以随时接上。",
            OPERATION_BASE_ROWS,
            "zh-CN",
        )

        self.assertIn("中间水域让箱子第一次靠近目标", body)
        self.assertEqual(body.count("你可以先说说你的第一反应或试玩当前关卡"), 1)
        self.assertEqual(body.count("右侧面板进行局部编辑"), 1)

    def test_stage_one_opening_removes_model_written_scope_paragraph(self):
        body = llm_client._ensure_stage_one_orientation(
            "我觉得右下角被水和墙夹住的箱子，会让处理顺序变得更敏感。\n\n"
            "你可以先试玩，或者直接告诉我你对这个布局的第一印象；小调整我可以帮你改，"
            "但大改方向还是由你来定。",
            OPERATION_BASE_ROWS,
            "zh-CN",
        )

        self.assertIn("右下角被水和墙夹住的箱子", body)
        self.assertNotIn("小调整我可以帮你改", body)
        self.assertEqual(body.count("你可以先说说你的第一反应或试玩当前关卡"), 1)

    def test_revision_card_uses_the_concrete_plan_not_a_confirmation_word(self):
        visible = (
            "当然，我会先处理右上角贴水的两个箱子。我的想法是：把中间那格水往左挪一格，"
            "让箱子之间多出一条纵向通道。"
        )

        summary = llm_client._revision_direction_sentence(visible)

        self.assertIn("把中间那格水往左挪一格", summary)
        self.assertNotEqual(summary, "当然")

    def test_water_discussion_focus_is_direct_and_explains_the_observation(self):
        focus = llm_client._deterministic_reply_discussion_focus(
            "把中间水格往左挪一格，让右上角的箱子贴着水边推进。",
            "zh-CN",
        )

        self.assertIn("试玩时，请直接看", focus)
        self.assertIn("先向上绕开", focus)
        self.assertIn("如果还是只有一条很显眼的走法", focus)

    def test_discussion_card_keeps_a_grounded_model_question_instead_of_generic_replacement(self):
        visible = (
            "右上角目标移到左下角后，箱子需要先绕过水塘和墙边通道，才能决定最后一箱的处理顺序。"
            "我担心开局会不会因此变得不够明确。"
        )
        original_focus = (
            "试玩时，请看玩家刚进入右上角时，能不能先判断目标已经不在附近、需要暂时放下这只箱子？"
            "如果这一点仍不明显，说明开局提示还不够。"
        )

        refined = llm_client._refine_discussion_focus(original_focus, visible, "zh-CN")

        self.assertEqual(refined, original_focus)
        self.assertNotIn("贴着调整后的水边", refined)

    def test_discussion_card_summarizes_a_clear_assistant_judgment_instead_of_asking(self):
        visible = (
            "这版把中间两堵墙打通，又把右下角的一只箱子挪到了上方。"
            "我倾向于认为这个改动让开局更直接，但中段的推箱顺序会更依赖玩家对墙缝的判断。"
        )
        generic_question = (
            "当箱子第一次穿过调整后的内部通道时，你最想观察哪个转折，"
            "来判断这个版本的推箱顺序是否更容易读懂？"
        )

        refined = llm_client._refine_discussion_focus(generic_question, visible, "zh-CN")

        self.assertIn("开局更直接", refined)
        self.assertIn("墙缝", refined)
        self.assertIn("试玩时", refined)
        self.assertNotIn("最想观察哪个转折", refined)

    def test_discussion_question_is_suppressed_without_an_unresolved_judgment(self):
        visible = "右侧墙缝让中段推箱顺序更值得判断，开局也更直接。"
        generic_question = "当箱子第一次经过墙缝时，你最想观察哪个转折？"

        refined = llm_client._refine_discussion_focus(generic_question, visible, "zh-CN")

        self.assertIsNone(refined)

    def test_revision_card_strips_a_personal_leadin_and_keeps_the_actual_action(self):
        offer = llm_client._semantics_preserving_proposal_offer(
            "如果是我，我会考虑把其中一格水往旁边挪一格",
            "让箱子贴水推进时多一个明确的转向选择。",
            "我会把 (7,6) 的水移到 (6,6)，让水带不再形成直角。",
            "zh-CN",
        )

        self.assertTrue(offer["summary"].startswith("把其中一格水往旁边挪一格"))
        self.assertNotIn("如果是我", offer["summary"])

    def test_revision_card_has_a_concrete_change_and_playable_text_explanation(self):
        offer = llm_client._distill_proposal_offer(
            {
                "summary": "如果是我",
                "rationale": "我会把它作为唯一改动方向，并用实际格子变化判断是否成立。",
            },
            "把 (7,6) 的水移到 (6,6)，让水带从直角变成斜线。这样箱子贴水推进时会多一个转向选择。",
            "",
            "zh-CN",
        )

        self.assertIn("(7,6)", offer["summary"])
        self.assertIn("具体做法是", offer["rationale"])
        self.assertIn("转向选择", offer["rationale"])

    def test_explicit_agreement_gets_deterministic_cards_and_no_questions(self):
        client = FakeClient([
            "我会把右下目标与水塘做局部联动。这样能让水域影响第一次推动。"
            "你还想改别的区域吗？要不要扩大水域？"
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "user", "content": "我认为水域纯摆设"},
                    {"role": "assistant", "content": "可以让水塘与目标点形成一个小联动。"},
                    {"role": "user", "content": "做点联动吧"},
                ],
                ["############"] * 10,
                "deterministic-guidance-test",
                language="zh-CN",
                stage_context={"recentGuidance": {}},
            )

        self.assertNotEqual(result.guidance["move"], "offer_revision")
        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertNotIn("？", result.assistant_message)
        self.assertNotIn("吗", result.assistant_message)

    def test_invalid_stage_json_falls_back_to_server_snapshot_opening(self):
        client = FakeClient([
            "   ",
            "The water narrows the central route in an interesting way. "
            "When the box enters that corridor, which route should read first?",
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-fallback-test",
                {
                    "stageNumber": 1,
                    "source": "initial",
                    "initialDraftMethod": "description_generation",
                },
            )

        self.assertIn("first readable corridor", result.assistant_message)
        self.assertNotIn("did not finish cleanly", result.assistant_message)
        self.assertNotIn("?", result.assistant_message)
        self.assertIn("play the Stage", result.assistant_message)
        self.assertIn("small, reviewable edits", result.assistant_message)
        self.assertEqual(
            result.assistant_message.count("You can share a first reaction or play the Stage"),
            1,
        )
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIn("deterministic solver", result.assessment["solutionSummary"])
        self.assertEqual(len(client.chat.completions.calls), 3)
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"]["type"],
            "json_schema",
        )
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"]["json_schema"]["name"],
            "cocreation_stage_assessment",
        )
        self.assertEqual(
            client.chat.completions.calls[1]["response_format"]["type"],
            "json_schema",
        )

    def test_invalid_stage_guidance_move_is_normalized_without_a_structured_retry(self):
        payload = json.dumps({
            "assistantMessage": "The water makes the central route feel deliberately tense.",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The saved map is solvable.",
                "difficultyOpinion": "In my view, the opening asks for careful reading.",
                "features": ["A central water route"],
                "suggestions": ["Watch the first approach"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        })

        client = FakeClient([payload])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-invalid-guidance-move-test",
                {"stageNumber": 1, "source": "initial"},
            )

        self.assertEqual(result.guidance["move"], "observe_stage")
        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_stage_route_overflow_is_salvaged_without_plain_fallback(self):
        payload = json.dumps({
            "assistantMessage": (
                "The lower corridor gives the opening a meaningful approach. "
                "B2 can move through "
                "(6,4) \u2192 (6,5) \u2192 (6,6) \u2192 (6,7) \u2192 "
                "(6,8) \u2192 (6,7) \u2192 (6,8) toward T2, and that route "
                "makes the push order part of the intended rhythm."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
                "coordinateLinks": [],
            },
            "assessment": {
                "solutionSummary": "The saved map is solvable.",
                "difficultyOpinion": "The opening asks for careful reading.",
                "features": ["A lower corridor"],
                "suggestions": ["Watch the push order"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        })
        client = FakeClient([payload])

        with patch.object(llm_client, "_create_async_client", return_value=client):
            result = llm_client.generate_stage_assessment(
                [],
                ENTITY_ROUTE_ROWS,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-route-salvage-test",
                {"stageNumber": 2, "source": "accepted_proposal"},
            )

        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIn("meaningful approach", result.assistant_message)
        self.assertIn("push order part", result.assistant_message)
        self.assertIn("\u2192 \u2026 \u2192", result.assistant_message)
        self.assertEqual(
            result.guidance["coordinateLinks"],
            [{
                "text": (
                    "B2 can move through (6,4) \u2192 \u2026 \u2192 (6,8) toward T2, "
                    "and that route makes the push order part of the intended rhythm."
                ),
                "from": {"row": 6, "column": 4},
                "to": {"row": 6, "column": 8},
            }],
        )

    def test_stage_opening_invalid_intent_metadata_is_dropped_without_plain_fallback(self):
        payload = json.dumps({
            "assistantMessage": "The central water route gives the opening a clear rhythm.",
            "guidance": {
                "move": "offer_revision",
                "intentHypothesis": "You want a difficult level.",
                "intentConfidence": "not-a-level",
                "followUpQuestion": None,
                "proposalOffer": {
                    "summary": "Add a wall",
                    "rationale": "This would make the route harder.",
                },
                "uiCues": [{"type": "warning", "text": "The opening is risky."}],
            },
            "assessment": {
                "solutionSummary": "The saved map is solvable.",
                "difficultyOpinion": "The opening asks for careful reading.",
                "features": ["A central water route"],
                "suggestions": ["Watch the first approach"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        })

        client = FakeClient([payload])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-invalid-intent-metadata-test",
                {"stageNumber": 2, "source": "accepted_proposal"},
            )

        self.assertIn("central water route", result.assistant_message)
        self.assertEqual(result.guidance["move"], "observe_stage")
        self.assertIsNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["intentConfidence"])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(result.guidance["uiCues"], [])
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_stage_opening_wrong_coordinate_sentence_is_removed_but_other_analysis_survives(self):
        payload = json.dumps({
            "assistantMessage": (
                "T1 is at (8,6), so the lower route is the main entrance. "
                "The water still creates a meaningful pause before the first push."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The saved map is solvable.",
                "difficultyOpinion": "The opening asks for careful reading.",
                "features": ["The water creates a pause"],
                "suggestions": ["Watch the first push"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        })

        result = llm_client.validate_chat_response(
            json.loads(payload),
            assessment_only=True,
            language="en",
            rows=ENTITY_ROUTE_ROWS,
            stage_context={"stageNumber": 2},
        )

        self.assertNotIn("T1 is at (8,6)", result[0])
        self.assertIn("water still creates a meaningful pause", result[0])

    def test_plain_stage_fallback_removes_wrong_coordinate_sentence_and_keeps_body(self):
        client = FakeClient([
            "not valid JSON",
            (
                "T1 is at (8,6), so the lower route is the main entrance. "
                "The water still creates a meaningful pause before the first push."
            ),
        ])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ENTITY_ROUTE_ROWS,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-plain-grounding-recovery-test",
                {"stageNumber": 2, "source": "accepted_proposal"},
            )

        self.assertIn("first readable corridor", result.assistant_message)
        self.assertNotIn("did not finish cleanly", result.assistant_message)
        self.assertEqual(result.model, "kimi-k2.6-safe-opening")
        self.assertEqual(len(client.chat.completions.calls), 3)

    def test_stage_fallback_skips_plain_request_when_remaining_budget_is_too_short(self):
        client = FakeClient(["not valid JSON"])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "LLM_INTERNAL_DEADLINE_SECONDS", 0.01),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-fallback-budget-test",
                {"stageNumber": 1, "source": "initial"},
            )

        self.assertEqual(result.model, "kimi-k2.6-safe-opening")
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIn("first readable corridor", result.assistant_message)

    def test_later_human_edit_invalid_opening_uses_grounded_plain_recovery(self):
        client = FakeClient([
            "   ",
            "not a complete JSON object",
            (
                "我唯一有点拿不准的是T1在(2,10)那个角落。"
                "右上角现在被墙收窄了，入口只有(2,9)那一条。"
                "这个判断会影响我理解整张图的推箱顺序。"
            ),
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "zh-CN",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "later-human-edit-plain-opening-test",
                {
                    "stageNumber": 2,
                    "source": "human_edit",
                    "changeSummary": {"components": ["water", "internalWalls"]},
                },
        )

        self.assertEqual(result.model, "kimi-k2.6")
        focus = result.guidance["followUpQuestion"]
        self.assertIsNotNone(focus)
        self.assertIn("水域、内部墙体", focus)
        self.assertTrue(any(marker in focus for marker in ("想让", "希望", "想加强")))
        self.assertNotIn("试玩时", focus)

    def test_structured_stage_one_opening_receives_rows_for_scope_normalization(self):
        payload = json.dumps({
            "assistantMessage": "The central water area makes the first route split easy to read.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a valid route.",
                "difficultyOpinion": "I think the opening is readable.",
                "features": ["Central water"],
                "suggestions": ["Try the opening route."],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        })

        client = FakeClient([payload])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_stage_assessment(
                [],
                ["############"] * 10,
                "en",
                {"solvable": True, "solutionSteps": 24, "solutionPushes": 6},
                {},
                "stage-structured-opening-test",
                {"stageNumber": 1, "source": "initial"},
            )

        self.assertIn("small, reviewable edits", result.assistant_message)
        self.assertEqual(result.attempts_used, 1)

    def test_length_truncation_uses_fallback_model(self):
        truncated = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content=""),
            )]
        )
        valid = "A grounded fallback response."
        client = FakeClient([truncated, valid])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "length-fallback-test",
            )

        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(result.model, "kimi-k2.6")

    def test_repeated_length_truncation_returns_a_complete_safe_reply(self):
        truncated = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="The layout suggests that the route"),
            )]
        )
        client = FakeClient([truncated, truncated])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the design trade-off."}],
                ["############"] * 10,
                "repeated-length-safe-fallback-test",
            )

        self.assertEqual(result.attempts_used, 2)
        self.assertIn("current saved Stage", result.assistant_message)
        self.assertNotIn("The layout suggests", result.assistant_message)
        self.assertIsNone(result.proposed_rows)
        self.assertEqual(result.guidance["coordinateLinks"], [])
        self.assertNotIn("designContextPatch", result.guidance)

    def test_route_reasoning_is_bounded_without_limiting_design_analysis(self):
        long_route = (
            "B2 moves from (2,2) to (2,3) -> (2,4) -> (2,5) -> (3,5) -> "
            "(4,5) -> (5,5) -> (6,5), then reaches T2."
        )
        detailed_design = (
            "The water changes the design rhythm and makes the first push more legible. "
            "At (3,4) the player can see the intended corridor, while (5,6) remains a "
            "useful decision point for box order. This is a meaningful trade-off, not a "
            "complete solver trace."
        )

        self.assertTrue(llm_client._route_reasoning_is_overlong(long_route))
        self.assertFalse(llm_client._route_reasoning_is_overlong(detailed_design))

    def test_overlong_route_reasoning_is_salvaged_without_discarding_the_reply(self):
        long_route = (
            "B2 moves from (2,2) to (2,3) -> (2,4) -> (2,5) -> (3,5) -> "
            "(4,5) -> (5,5) -> (6,5), then reaches T2."
        )
        client = FakeClient([long_route])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Explain the map design."}],
                ENTITY_ROUTE_ROWS,
                "route-compaction-retry-test",
            )

        self.assertEqual(result.attempts_used, 1)
        self.assertNotIn("did not finish cleanly", result.assistant_message)
        self.assertIn("B2 moves from", result.assistant_message)
        self.assertIn("\u2192", result.assistant_message)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_overlong_route_keeps_design_sentences_and_drops_later_trace(self):
        content = (
            "The water makes the first push more deliberate, so the box order becomes a "
            "useful design choice.\n\n"
            "B2 moves from (2,2) -> (2,3) -> (2,4) -> (2,5) -> (3,5) -> "
            "(4,5) -> (5,5) -> (6,5), then reaches T2.\n"
            "Then it continues through (6,6) -> (6,7) -> (6,8) before the next push."
        )
        result, client = self.execute([content], rows=ENTITY_ROUTE_ROWS)

        self.assertEqual(result.attempts_used, 1)
        self.assertIn("first push more deliberate", result.assistant_message)
        self.assertIn("B2 moves from", result.assistant_message)
        self.assertIn("Then it continues", result.assistant_message)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_coordinate_route_is_recovered_after_final_text_cleanup(self):
        content = (
            "The route runs from (2,2) -> (2,3) -> (2,4) -> (2,5) -> "
            "(2,6) -> (2,7) -> (2,8)."
        )
        result, _ = self.execute([content], rows=ENTITY_ROUTE_ROWS)

        self.assertEqual(len(result.guidance["coordinateLinks"]), 1)
        link = result.guidance["coordinateLinks"][0]
        self.assertEqual(link["from"], {"row": 2, "column": 2})
        self.assertEqual(link["to"], {"row": 2, "column": 8})
        self.assertIn(link["text"], result.assistant_message)

    def test_ordinary_grounding_drops_only_the_invalid_sentence(self):
        content = (
            "B1 is at (1,1), so the lower-left cluster is the key design issue. "
            "The water still creates a meaningful pause before the first push."
        )
        result, client = self.execute([content], rows=ENTITY_ROUTE_ROWS)

        self.assertEqual(result.attempts_used, 1)
        self.assertNotIn("B1 is at (1,1)", result.assistant_message)
        self.assertIn("water still creates a meaningful pause", result.assistant_message)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_explicit_map_proposal_uses_pro_model_and_larger_output_limit(self):
        response = revision_plan_payload()
        client = FakeClient([response, operation_payload(TARGET_SHIFT_OPERATIONS)])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please create a reviewable map proposal."},
                ],
                OPERATION_BASE_ROWS,
                "proposal-model-test",
            )

        request = client.chat.completions.calls[0]
        self.assertEqual(request["model"], "kimi-k2.6")
        self.assertEqual(
            request["max_completion_tokens"],
            llm_client.PROPOSAL_PLAN_MAX_COMPLETION_TOKENS,
        )
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertEqual(
            request["response_format"]["json_schema"]["name"],
            "cocreation_revision_plan",
        )
        self.assertIn("semantic RevisionPlan", request["messages"][0]["content"])
        self.assertIn("Do not generate map rows", request["messages"][0]["content"])

    def test_natural_chinese_request_to_help_change_uses_proposal_workflow(self):
        response = revision_plan_payload(
            effect="reshape_water",
            operators=["add_water"],
            focus={"row": 3, "column": 7, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
            edit_budget=3,
        )
        client = FakeClient([response, operation_payload(WATER_ADD_OPERATIONS)])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "我建议让水域贴近右侧推动路线。"},
                    {"role": "user", "content": "你帮我改"},
                ],
                OPERATION_BASE_ROWS,
                "natural-chinese-proposal-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(client.chat.completions.calls[0]["model"], "kimi-k2.6")
        self.assertEqual(
            client.chat.completions.calls[0]["response_format"]["type"],
            "json_schema",
        )

    def test_invalid_modifier_candidates_fall_back_to_exact_deterministic_transition(self):
        plan_payload = json.dumps({
            "strategies": [{
                "effect": "adjust_internal_walls",
                "focus": {"row": 2, "column": 2, "radius": 1},
                "operators": ["add_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
                "editBudget": 1,
                "metricGoals": [],
                "requiredTransitions": [
                    {"row": 2, "column": 2, "from": ".", "to": "#"},
                ],
                "anchorEntities": ["B1", "T1"],
                "playObjective": "route_choice",
            }],
        })
        invalid_candidates = operation_payload([
            {"row": 2, "column": 2, "to": "."},
        ])
        client = FakeClient([plan_payload, invalid_candidates, invalid_candidates])
        source_offer = {
            "summary": "Open the local route",
            "rationale": "Change only the cited wall and compare the first push.",
            "executionBrief": {
                "schemaVersion": 1,
                "effect": "adjust_internal_walls",
                "anchors": ["B1", "T1"],
                "focus": {"row": 2, "column": 2, "radius": 1},
                "requiredTransitions": [
                    {"row": 2, "column": 2, "from": ".", "to": "#"},
                ],
                "allowedOperators": ["add_wall"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
                "playObjective": "route_choice",
            },
        }

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "请按紫卡生成这个方案"}],
                OPERATION_BASE_ROWS,
                "deterministic-transition-fallback-test",
                stage_context={
                    "explicitAction": "execute_revision",
                    "sourceProposalOffer": source_offer,
                },
                proposal_validator=llm_client.validate_and_solve,
            )

        self.assertEqual(result.proposed_rows[1], "##.........#")
        self.assertEqual(result.revision_operations, [
            {"row": 2, "column": 2, "from": ".", "to": "#"},
        ])
        self.assertEqual(result.proposal_diagnostics["source"], "deterministic_search")

    def test_confirmed_concrete_chinese_plan_immediately_uses_proposal_workflow(self):
        response = revision_plan_payload(
            effect="reshape_water",
            operators=["add_water"],
            focus={"row": 3, "column": 7, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
            edit_budget=3,
        )
        client = FakeClient([response, operation_payload(WATER_ADD_OPERATIONS)])
        conversation = [
            {
                "role": "assistant",
                "content": "好，那就定三格：把第7行第6到第8列连成一片水，其余格子不动。",
            },
            {"role": "user", "content": "三格可以"},
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                OPERATION_BASE_ROWS,
                "confirmed-chinese-plan-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(client.chat.completions.calls[0]["model"], "kimi-k2.6")
        self.assertIn("第7行第6到第8列", client.chat.completions.calls[0]["messages"][1]["content"])

    def test_help_me_do_inherits_a_confirmed_concrete_chinese_plan(self):
        response = revision_plan_payload(
            effect="reshape_water",
            operators=["add_water"],
            focus={"row": 3, "column": 7, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "unrelated_areas"],
            edit_budget=3,
        )
        client = FakeClient([response, operation_payload(WATER_ADD_OPERATIONS)])
        conversation = [
            {
                "role": "assistant",
                "content": "好，那就定三格：把第7行第6到第8列连成一片水，其余格子不动。",
            },
            {"role": "user", "content": "三格可以"},
            {"role": "assistant", "content": "这个方向已经说清楚了。"},
            {"role": "user", "content": "帮我做"},
        ]

        state, brief = llm_client._classify_revision_request(conversation)

        self.assertEqual(state, "authorized")
        self.assertIn("第7行第6到第8列", brief)

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                conversation,
                OPERATION_BASE_ROWS,
                "help-me-do-chinese-plan-test",
                language="zh-CN",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(client.chat.completions.calls[0]["model"], "kimi-k2.6")

    def test_map_request_phrases_are_detected_without_matching_design_questions(self):
        positives = (
            "你帮我改",
            "帮我做",
            "你来改吧",
            "按这个思路帮我改一下",
            "请你改一下地图",
            "可以帮我修改吗？",
            "修改",
            "Can you change it?",
            "Go ahead and revise the map.",
        )
        negatives = (
            "你觉得应该怎么改？",
            "如果你来改，你会怎么改？",
            "先说说修改思路",
            "How would you change the route?",
            "How would you revise the map?",
        )

        for message in positives:
            with self.subTest(message=message):
                self.assertTrue(llm_client._requests_complete_map([
                    {"role": "assistant", "content": "把右下目标移近水域，让推动路线形成绕行。"},
                    {"role": "user", "content": message},
                ]))
        for message in negatives:
            with self.subTest(message=message):
                self.assertFalse(llm_client._requests_complete_map([
                    {"role": "user", "content": message},
                ]))

        for message in ("可以帮我修改吗？", "修改", "帮我做", "Can you change it?"):
            with self.subTest(message=f"no-basis:{message}"):
                state, brief = llm_client._classify_revision_request([
                    {"role": "user", "content": message},
                ])
                self.assertEqual(state, "needs_direction")
                self.assertIsNone(brief)

        state, brief = llm_client._classify_revision_request([
            {
                "role": "assistant",
                "content": "我觉得水域让下半区的路线更有犹豫感。",
            },
            {"role": "user", "content": "修改"},
        ])
        self.assertEqual(state, "needs_direction")
        self.assertIsNone(brief)

        state, brief = llm_client._classify_revision_request([
            {"role": "user", "content": "换个话题，帮我修改一首与地图无关的诗。"},
        ])
        self.assertEqual(state, "not_request")
        self.assertIsNone(brief)

    def test_unclear_revision_request_is_tentative_intent_only(self):
        result, _ = self.execute(
            [
                "I can help, but I still need to understand the area you mean.\n"
                "<GUIDANCE>DISCUSS: Which route should change? || "
                "INTENT: I think you may want a more deliberate route. || "
                "PROPOSAL_SUMMARY: Move the target || "
                "PROPOSAL_RATIONALE: Make the route less direct.</GUIDANCE>"
            ],
            conversation=[{"role": "user", "content": "Can you change it?"}],
        )

        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertEqual(result.guidance["intentConfidence"], "low")
        self.assertIsNone(result.guidance["followUpQuestion"])
        self.assertIsNone(result.guidance["proposalOffer"])
        self.assertEqual(result.guidance["uiCues"], [])
        self.assertIn("I would be guessing on your behalf", result.assistant_message)

    def test_explicit_map_proposal_rejects_text_only_result(self):
        text_only = json.dumps({
            "assistantMessage": "I would narrow the route.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        })
        client = FakeClient([text_only, text_only])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please create a reviewable map proposal."},
                ],
                ["############"] * 10,
                "proposal-required-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)
        retry_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("previous RevisionPlan was rejected", retry_prompt)
        self.assertIn("Do not return map rows or tile operations", retry_prompt)

    def test_structurally_invalid_revision_plan_retries_with_pro_for_correction(self):
        invalid = json.dumps({"strategies": []})
        client = FakeClient([
            invalid,
            revision_plan_payload(),
            operation_payload(TARGET_SHIFT_OPERATIONS),
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please revise the map."},
                ],
                OPERATION_BASE_ROWS,
                "proposal-pro-correction-test",
            )

        self.assertEqual(result.attempts_used, 3)
        self.assertEqual(
            [call["model"] for call in client.chat.completions.calls],
            ["kimi-k2.6", "kimi-k2.6", "kimi-k2.6"],
        )
        self.assertIn(
            "strategies must contain one to three items",
            client.chat.completions.calls[1]["messages"][0]["content"],
        )

    def test_zero_candidate_revision_plan_retries_with_safe_search_feedback(self):
        client = FakeClient([
            revision_plan_payload(),
            json.dumps({"candidates": []}),
            operation_payload(TARGET_SHIFT_OPERATIONS),
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Move the target and revise the map."}],
                OPERATION_BASE_ROWS,
                "zero-candidate-plan-correction-test",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(result.attempts_used, 3)
        self.assertEqual(len(client.chat.completions.calls), 3)
        correction_prompt = client.chat.completions.calls[2]["messages"][0]["content"]
        self.assertIn("candidates must contain one to three", correction_prompt)
        self.assertIn("authorized brief", correction_prompt)

    def test_revision_plan_search_returns_only_selected_verified_map(self):
        client = FakeClient([
            revision_plan_payload(),
            operation_payload(TARGET_SHIFT_OPERATIONS),
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "user", "content": "Move the target one cell to the right and revise the map."},
                ],
                OPERATION_BASE_ROWS,
                "candidate-selection-test",
            )

        self.assertIsNotNone(result.proposed_rows)
        self.assertEqual(result.revision_plan["strategies"][0]["effect"], "relocate_target")
        self.assertGreater(result.proposal_diagnostics["validCandidates"], 0)
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_confirmed_assistant_direction_becomes_a_hard_search_requirement(self):
        conversation = [
            {
                "role": "assistant",
                "content": "我会把左上角的目标点往右下方挪，让它成为中期目标。",
            },
            {
                "role": "user",
                "content": "请根据这个方向生成一份可供审查的地图提案。",
            },
        ]
        requirement = llm_client._authorized_movement_requirement(conversation)

        self.assertEqual(
            requirement,
            {"operator": "move_target", "direction": "lower_right"},
        )
        messages = llm_client._build_revision_plan_messages(
            conversation,
            OPERATION_BASE_ROWS,
            "zh-CN",
            {"authorizedRevisionBrief": "拉开左上角箱子与目标点的距离"},
            requirement,
        )
        self.assertIn(
            "Hard verified movement requirement: the target must move lower right",
            messages[1]["content"],
        )

    def test_direct_user_direction_overrides_an_older_assistant_direction(self):
        requirement = llm_client._authorized_movement_requirement([
            {
                "role": "assistant",
                "content": "Move the target to the left to open the route.",
            },
            {
                "role": "user",
                "content": "Please move the target to the right and generate a reviewable proposal.",
            },
        ])
        self.assertEqual(
            requirement,
            {"operator": "move_target", "direction": "right"},
        )

    def test_location_label_does_not_become_a_hard_movement_direction(self):
        conversation = [
            {
                "role": "assistant",
                "content": "As a lighter option, keep the target in its current position.",
            },
            {
                "role": "user",
                "content": (
                    "请根据这个方向生成一份可供审查的地图提案："
                    "把右上角那个目标稍微挪开一点。"
                ),
            },
        ]

        self.assertIsNone(llm_client._authorized_movement_requirement(conversation))
        self.assertEqual(
            llm_client._authorized_preserved_components(
                conversation,
                {"authorizedRevisionBrief": "Keep the target in its current position."},
            ),
            frozenset(),
        )

    def test_explicit_destination_still_becomes_a_hard_movement_direction(self):
        requirement = llm_client._authorized_movement_requirement([
            {
                "role": "user",
                "content": (
                    "请根据这个方向生成一份可供审查的地图提案："
                    "把目标向右上移动一格。"
                ),
            },
        ])

        self.assertEqual(
            requirement,
            {"operator": "move_target", "direction": "upper_right"},
        )

    def test_revision_plan_protocol_never_asks_model_for_tile_operations(self):
        client = FakeClient([
            revision_plan_payload(),
            operation_payload(TARGET_SHIFT_OPERATIONS),
        ])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [
                    {"role": "user", "content": "Move the target one cell to the right and revise the map."},
                ],
                OPERATION_BASE_ROWS,
                "server-source-operation-test",
            )

        self.assertIsNotNone(result.proposed_rows)
        plan_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
        modifier_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("Do not generate map rows", plan_prompt)
        self.assertIn("Do not generate", plan_prompt)
        self.assertNotIn('"to":"#"', plan_prompt)
        self.assertIn("level revision assistant", modifier_prompt)
        self.assertIn("execution contract", modifier_prompt)

    def test_verbose_brief_with_multiple_regions_does_not_require_every_region(self):
        rows = llm_client._apply_map_operations(
            OPERATION_BASE_ROWS,
            [
                {"row": 6, "column": 7, "to": "."},
                {"row": 6, "column": 8, "to": "t"},
            ],
            "Keep the left side connected to the right side and make one local change.",
        )

        self.assertEqual(rows[5], "#...s..t...#")

    def test_legacy_operation_normalizer_drops_noops_and_identical_duplicates(self):
        rows = llm_client._apply_map_operations(
            OPERATION_BASE_ROWS,
            [
                {"row": 2, "column": 2, "to": "."},
                {"row": 6, "column": 7, "to": "."},
                {"row": 6, "column": 8, "to": "t"},
                {"row": 6, "column": 8, "to": "t"},
            ],
            "Move the target one cell to the right.",
        )
        self.assertEqual(rows[5], "#...s..t...#")

        with self.assertRaisesRegex(ValueError, "real tile change"):
            llm_client._apply_map_operations(
                OPERATION_BASE_ROWS,
                [{"row": 2, "column": 2, "to": "."}],
                "Keep the map unchanged.",
            )

    def test_modifier_executor_enforces_contract_and_rejects_complete_map_style_output(self):
        plan = llm_client.parse_revision_plan(json.loads(revision_plan_payload(
            effect="narrow_route",
            operators=["add_wall"],
            focus={"row": 3, "column": 3, "radius": 1},
            preserve=["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
            edit_budget=3,
            required_transitions=[],
        )))
        contract = llm_client._build_revision_execution_contract(
            plan,
            "Narrow the upper-left route.",
        )

        result = llm_client.execute_revision_operations(
            OPERATION_BASE_ROWS,
            [
                {"row": 2, "column": 2, "to": "#"},
                {"row": 2, "column": 3, "to": "#"},
                {"row": 3, "column": 2, "to": "#"},
            ],
            contract,
            1,
        )
        self.assertEqual(result[1], "###........#")

        declared_before = llm_client.execute_revision_operations(
            OPERATION_BASE_ROWS,
            [{"row": 2, "column": 2, "from": ".", "to": "#"}],
            contract,
            1,
        )
        self.assertEqual(declared_before[1], "##.........#")

        with self.assertRaisesRegex(ValueError, "outside the revision focus"):
            llm_client.execute_revision_operations(
                OPERATION_BASE_ROWS,
                [
                    {"row": 2, "column": 2, "to": "#"},
                    {"row": 2, "column": 3, "to": "#"},
                    {"row": 4, "column": 5, "to": "#"},
                ],
                contract,
                1,
            )

    def test_semantic_search_keeps_outer_shell_immutable(self):
        client = FakeClient([
            revision_plan_payload(
                effect="narrow_route",
                operators=["add_wall"],
                focus={"row": 1, "column": 1, "radius": 3},
                preserve=["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
                edit_budget=3,
            ),
            operation_payload([
                {"row": 2, "column": 2, "to": "#"},
                {"row": 2, "column": 3, "to": "#"},
                {"row": 3, "column": 2, "to": "#"},
            ]),
        ])
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Narrow the upper-left route and revise the map."}],
                OPERATION_BASE_ROWS,
                "candidate-safety-test",
            )
        self.assertEqual(result.proposed_rows[0], OPERATION_BASE_ROWS[0])
        self.assertEqual(result.proposed_rows[-1], OPERATION_BASE_ROWS[-1])

    def test_flash_empty_response_falls_back_to_pro(self):
        client = FakeClient(["   \n ", "A grounded fallback response."])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "fallback-model-test",
            )

        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(result.model, "kimi-k2.6")
        self.assertEqual(
            [call["model"] for call in client.chat.completions.calls],
            ["kimi-k2.6", "kimi-k2.6"],
        )
        self.assertNotIn("response_format", client.chat.completions.calls[1])

    def test_corrective_fallback_does_not_mutate_original_messages(self):
        messages = [{"role": "system", "content": "Original contract."}]

        corrected = llm_client._messages_with_validation_feedback(
            messages,
            "guidance is required.",
        )

        self.assertEqual(messages[0]["content"], "Original contract.")
        self.assertIn("guidance is required", corrected[0]["content"])

    def test_safe_validation_reason_does_not_include_invalid_json_content(self):
        try:
            json.loads('{"assistantMessage":')
        except json.JSONDecodeError as exception:
            reason = llm_client._safe_validation_reason(exception)

        self.assertEqual(reason, "The response was not a complete valid JSON object.")
        self.assertNotIn("assistantMessage", reason)

    def test_wall_clock_limit_cancels_slow_models(self):
        started_at = llm_client.time.monotonic()

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "LLM_INTERNAL_DEADLINE_SECONDS", 0.06),
            patch.object(llm_client, "PLAIN_CHAT_TIMEOUT_SECONDS", 0.06),
            patch.object(llm_client, "PLAIN_PRIMARY_TIMEOUT_SECONDS", 0.02),
            patch.object(llm_client, "_create_async_client", return_value=SlowClient()),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the route."}],
                ["############"] * 10,
                "wall-clock-timeout-test",
            )

        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertLess(llm_client.time.monotonic() - started_at, 1.0)

    def test_retry_budget_helper_skips_a_short_remaining_window(self):
        self.assertFalse(
            llm_client._retry_budget_available(
                llm_client.time.monotonic() + 0.01,
                request_id="retry-budget-test",
                task="chat",
                attempt=1,
                max_attempts=2,
                response_mode="plain_text",
            )
        )

    def test_two_empty_plain_responses_use_fallback_then_fail(self):
        client = FakeClient(["   ", "\n\t"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "invalid-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_EMPTY_RESPONSE")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_revision_plan_timeout_uses_two_attempts_and_reserves_search_time(self):
        timeout = APITimeoutError(
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        )
        client = FakeClient([timeout, timeout])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please create a map proposal."},
                ],
                ["############"] * 10,
                "timeout-test",
            )

        self.assertEqual(llm_client.UNIFIED_MODEL, "kimi-k2.6")
        self.assertEqual(llm_client.BACKEND_REQUEST_TIMEOUT_SECONDS, 120.0)
        self.assertEqual(llm_client.LLM_INTERNAL_DEADLINE_SECONDS, 116.0)
        self.assertEqual(llm_client.CHAT_TIMEOUT_SECONDS, 120.0)
        self.assertEqual(llm_client.PLAIN_CHAT_TIMEOUT_SECONDS, 120.0)
        self.assertEqual(llm_client.PRIMARY_ATTEMPT_TIMEOUT_SECONDS, 70.0)
        self.assertEqual(llm_client.CHAT_MAX_ATTEMPTS, 2)
        self.assertEqual(llm_client.PROPOSAL_GENERATION_ATTEMPTS, 2)
        self.assertEqual(llm_client.PROPOSAL_PLAN_PRIMARY_TIMEOUT_SECONDS, 22.0)
        self.assertEqual(llm_client.PROPOSAL_PLAN_RETRY_TIMEOUT_SECONDS, 8.0)
        self.assertEqual(llm_client.PROPOSAL_LLM_PHASE_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(llm_client.PROPOSAL_SEARCH_DEADLINE_SECONDS, 56.0)
        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_missing_api_key_fails_without_model_call(self):
        with (
            patch.dict(os.environ, {"KIMI_API_KEY": ""}),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "config-test",
            )

        self.assertEqual(raised.exception.code, "CONFIGURATION_ERROR")
        self.assertEqual(raised.exception.attempts_used, 0)

    def test_chinese_response_is_supported(self):
        result = llm_client.validate_chat_response(
            {
                "assistantMessage": "我注意到中央路线比较紧凑。",
                "guidance": {
                    "move": "offer_perspective",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": "你想先讨论哪一部分？",
                    "proposalOffer": None,
                    "uiCues": [],
                },
                "assessment": None,
                "proposedRows": None,
                "modificationSummary": "",
            }
        )

        self.assertEqual(result[0], "我注意到中央路线比较紧凑。")

    def test_structured_question_is_removed_from_assistant_body(self):
        result = llm_client.validate_chat_response(
            {
                "assistantMessage": (
                    "The two targets create distinct routes.\n\n"
                    "Would you like to preserve that split?"
                ),
                "guidance": {
                    "move": "offer_perspective",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": "Which route should feel more important?",
                    "proposalOffer": None,
                    "uiCues": [],
                },
                "assessment": None,
                "proposedRows": None,
                "modificationSummary": "",
            }
        )

        self.assertEqual(result[0], "The two targets create distinct routes.")

    def test_clarification_response_can_keep_three_body_questions(self):
        result = llm_client.validate_chat_response(
            {
                "assistantMessage": (
                    "I can make this revision, but I need to pin down the intended local effect.\n\n"
                    "Which water region should carry the change?\n"
                    "Should the new opening affect the first push or the return route?\n"
                    "Which existing route must remain unchanged?"
                ),
                "guidance": {
                    "move": "clarify_intent",
                    "intentHypothesis": None,
                    "intentConfidence": None,
                    "followUpQuestion": None,
                    "proposalOffer": None,
                    "uiCues": [],
                },
                "assessment": None,
                "proposedRows": None,
                "modificationSummary": "",
            },
            stage_context={"revisionRouting": "needs_clarification"},
        )

        self.assertEqual(result[0].count("?"), 3)
        self.assertIsNone(result[4]["followUpQuestion"])

    def test_proposal_routing_bypasses_plain_chat_after_clarification(self):
        expected = llm_client.LLMExecutionResult(
            assistant_message="One verified revision proposal.",
            attempts_used=0,
            request_id="proposal-routing-test",
        )
        with patch.object(
            llm_client,
            "_generate_revision_search_proposal_sync",
            return_value=expected,
        ) as generate_proposal:
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Please give me one plan."}],
                ENTITY_ROUTE_ROWS,
                "proposal-routing-test",
                stage_context={"revisionRouting": "proposal"},
            )

        self.assertIs(result, expected)
        generate_proposal.assert_called_once()

    def test_thinking_is_enabled_only_for_revision_and_modifier_tasks(self):
        revision_client = FakeClient(["{}"])
        plain_client = FakeClient(["{}"])

        with patch.object(llm_client, "_create_async_client", return_value=revision_client):
            asyncio.run(llm_client._request_completion(
                "test-kimi-key",
                "https://api.moonshot.cn/v1",
                "kimi-k2.6",
                [{"role": "user", "content": "Create one revision plan."}],
                100,
                5,
                structured=False,
                task="revision_plan",
            ))
        with patch.object(llm_client, "_create_async_client", return_value=plain_client):
            asyncio.run(llm_client._request_completion(
                "test-kimi-key",
                "https://api.moonshot.cn/v1",
                "kimi-k2.6",
                [{"role": "user", "content": "Discuss the current Stage."}],
                100,
                5,
                structured=False,
                task="plain_chat",
            ))

        self.assertEqual(
            revision_client.chat.completions.calls[0]["extra_body"],
            {"thinking": {"type": "enabled"}},
        )
        self.assertEqual(
            plain_client.chat.completions.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_discussion_card_is_not_repeated_in_the_saved_assistant_body(self):
        focus = "我会留意水边第一次推进是否真的改变了路线判断。"
        message = (
            "水域现在贴近推进路线。\n\n"
            f"{focus}\n\n{focus}"
        )
        composed = llm_client._compose_assistant_message(
            message,
            {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": focus,
                "proposalOffer": None,
                "uiCues": [],
            },
            "zh-CN",
        )

        self.assertEqual(composed.count(focus), 1)
        self.assertIn("水域现在贴近推进路线", composed)

    def test_recent_discussion_focus_history_suppresses_an_older_repeat(self):
        focus = "我会留意水边第一次推进是否真的改变了路线判断。"
        content = (
            "水域让右侧路线有了新的转折。\n"
            f"<GUIDANCE>DISCUSS: {focus}</GUIDANCE>"
        )

        extracted = llm_client._extract_plain_discussion_focus(
            content,
            "zh-CN",
            stage_context={
                "recentGuidance": {
                    "discussionFocusHistory": [
                        "我会看玩家进入调整区域的第一步会不会重新判断顺序。",
                        focus,
                    ],
                },
            },
        )

        self.assertIsNone(extracted)

    def test_default_discussion_insights_use_play_moments_not_report_lead_ins(self):
        focuses = {
            llm_client._friendly_default_discussion_focus(
                ["############"] * 10,
                "zh-CN",
                f"水边路线 {index}",
                [],
            )
            for index in range(12)
        }

        self.assertGreaterEqual(len(focuses), 4)
        for focus in focuses:
            self.assertNotIn("我在意的是", focus)
            self.assertNotIn("我想把注意力放在", focus)
            self.assertNotIn("比单看格子摆放更能说明", focus)
            self.assertTrue(any(marker in focus for marker in ("第一次", "水边", "调整区域")))

    def test_question_without_structured_follow_up_is_extracted(self):
        payload = {
            "assistantMessage": (
                "In my view, a tighter route would make the push order more visible. "
                "Would you like to preserve that split?"
            ),
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(
            result[0],
            "In my view, a tighter route would make the push order more visible.",
        )
        self.assertIn("My reading of this version", result[4]["followUpQuestion"])
        self.assertIn("tighter route", result[4]["followUpQuestion"])
        self.assertNotIn("Would you like", result[4]["followUpQuestion"])

    def test_question_only_message_is_rejected(self):
        payload = {
            "assistantMessage": "Would you like to preserve that split?",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        with self.assertRaisesRegex(ValueError, "declarative response"):
            llm_client.validate_chat_response(payload)

    def test_structured_assessment_and_proposal_are_returned(self):
        rows = ["############"] * 10
        payload = {
            "assistantMessage": "Here is a focused alternative.",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "One box route.",
                "difficultyOpinion": "Likely easy.",
                "features": ["Compact"],
                "suggestions": ["Move the player"],
                "satisfactionQuestion": "Does this match your intention?",
            },
            "proposedRows": rows,
            "modificationSummary": "Moved the player.",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(result[1]["features"], ["Compact"])
        self.assertEqual(result[2], rows)
        self.assertEqual(result[3], "Moved the player.")
        self.assertEqual(result[4]["move"], "deliver_revision")

    def test_complete_proposal_is_not_described_as_an_already_saved_edit(self):
        payload = {
            "assistantMessage": "改好了，你先去试试看。",
            "guidance": {
                "move": "deliver_revision",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": ["############"] * 10,
            "modificationSummary": "调整路线。",
        }

        result = llm_client.validate_chat_response(payload, language="zh-CN")

        self.assertIn("可审查的修改提案", result[0])
        self.assertIn("决定是否接受", result[0])
        self.assertNotIn("改好了", result[0])

    def test_proposal_language_stays_pending_until_acceptance(self):
        normalized = llm_client._normalize_change_claims_for_proposal(
            "好，我按刚才说的方向改：把水域收拢。改完的版本你可以先看看。",
            "zh-CN",
            True,
        )

        self.assertIn("我按刚才说的方向做了一份修改提案", normalized)
        self.assertIn("这份待审查提案", normalized)
        self.assertNotIn("改完的版本", normalized)

    def test_invalid_proposal_uses_fallback_then_fails(self):
        client = FakeClient([
            revision_plan_payload(),
            operation_payload(TARGET_SHIFT_OPERATIONS),
            operation_payload(TARGET_SHIFT_OPERATIONS),
        ])
        validated_rows = []

        def reject_proposal(rows):
            validated_rows.append(rows)
            raise ValueError("The proposed map is unsolvable.")

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [
                    {"role": "assistant", "content": "Move the lower target beside the water route."},
                    {"role": "user", "content": "Please draft that revision."},
                ],
                OPERATION_BASE_ROWS,
                "invalid-proposal-test",
                proposal_validator=reject_proposal,
            )

        self.assertEqual(raised.exception.code, "PROPOSAL_SEARCH_EXHAUSTED")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.attempts_used, 3)
        self.assertEqual(len(client.chat.completions.calls), 3)
        self.assertGreater(len(validated_rows), 0)
        self.assertIn("constructedCandidates", raised.exception.proposal_diagnostics)

    def test_unsolicited_revision_offer_cannot_include_map(self):
        payload = {
            "assistantMessage": "A narrower approach lane could add commitment.",
            "guidance": {
                "move": "offer_revision",
                "intentHypothesis": "You may want the first push to feel consequential.",
                "intentConfidence": "low",
                "followUpQuestion": "Would you like me to draft that direction?",
                "proposalOffer": {
                    "summary": "Narrow the first approach lane",
                    "rationale": "It would make the opening choice more deliberate.",
                },
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": ["############"] * 10,
            "modificationSummary": "Narrowed the lane.",
        }

        with self.assertRaisesRegex(ValueError, "requires exact requiredTransitions"):
            llm_client.validate_chat_response(payload)

    def test_manual_edit_and_warning_ui_cues_are_validated(self):
        payload = {
            "assistantMessage": "You can compare both routing choices directly.",
            "guidance": {
                "move": "challenge_tradeoff",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "Which compromise fits your intention better?",
                "proposalOffer": None,
                "uiCues": [
                    {
                        "type": "warning",
                        "text": "A wider route improves freedom but reduces commitment.",
                    },
                    {
                        "type": "manual_edit",
                        "text": "Try the right-side tile editor and save the result as a new Stage.",
                    },
                ],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(
            [cue["type"] for cue in result[4]["uiCues"]],
            ["warning", "manual_edit"],
        )

    def test_legacy_guidance_without_ui_cues_defaults_to_empty(self):
        payload = {
            "assistantMessage": "A concise perspective.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload)

        self.assertEqual(result[4]["uiCues"], [])

    def test_english_intent_hypothesis_uses_direct_tentative_voice(self):
        cases = {
            "The designer wants to make the route feel risky.":
                "It sounds to me like you want to make the route feel risky.",
            "The player seems to want a tighter opening.":
                "I get the sense that you may want a tighter opening.",
            "You want the second push to be surprising.":
                "It sounds to me like you want the second push to be surprising.",
            "I think you may be emphasizing push order.":
                "I think you may be emphasizing push order.",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    llm_client._normalize_intent_hypothesis(source, "en"),
                    expected,
                )

    def test_chinese_intent_hypothesis_uses_direct_tentative_voice(self):
        cases = {
            "设计者想要让路线更紧张。": "听起来你更想要让路线更紧张。",
            "玩家希望突出推动顺序。": "听起来你更想要突出推动顺序。",
            "我猜你可能更在意路线辨识度。": "我猜你可能更在意路线辨识度。",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    llm_client._normalize_intent_hypothesis(source, "zh-CN"),
                    expected,
                )

    def test_validated_guidance_normalizes_report_style_intent(self):
        payload = {
            "assistantMessage": "The tighter route could make the opening more deliberate.",
            "guidance": {
                "move": "clarify_intent",
                "intentHypothesis": "The designer wants a more deliberate opening.",
                "intentConfidence": "low",
                "followUpQuestion": "What part of that direction matters most to you?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        self.assertEqual(
            result[4]["intentHypothesis"],
            "I read your preference as wanting a more deliberate opening.",
        )

    def test_structured_discussion_card_accepts_a_concrete_first_person_insight(self):
        payload = {
            "assistantMessage": "The lower route now has more breathing room.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": (
                    "I prefer the hesitation beside the water because it gives the first "
                    "route choice more weight."
                ),
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        self.assertIn("I prefer", result[4]["followUpQuestion"])
        self.assertNotIn("?", result[4]["followUpQuestion"])

    def test_structured_fields_normalize_false_level_progression(self):
        payload = {
            "assistantMessage": "This feels like the second level, not the first level.",
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "Tune the next level.",
        }

        result = llm_client.validate_chat_response(payload, language="en")

        self.assertEqual(result[0], "This feels like this Stage, not this Stage.")
        self.assertEqual(result[3], "Tune the next version.")

    def test_composed_message_keeps_cues_for_future_llm_context(self):
        guidance = {
            "move": "challenge_tradeoff",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": "Which direction do you prefer?",
            "proposalOffer": None,
            "uiCues": [
                {"type": "tradeoff", "text": "The open route reduces commitment."},
                {"type": "manual_edit", "text": "Compare it with the tile editor."},
            ],
        }

        message = llm_client._compose_assistant_message(
            "Both routes remain solvable.",
            guidance,
        )

        self.assertEqual(
            message,
            "Both routes remain solvable.\n\n"
            "The open route reduces commitment.\n\n"
            "Compare it with the tile editor.\n\n"
            "Which direction do you prefer?",
        )

    def test_ui_cues_reject_invalid_duplicate_and_missing_tradeoff(self):
        base_guidance = {
            "move": "offer_perspective",
            "intentHypothesis": None,
            "intentConfidence": None,
            "followUpQuestion": None,
            "proposalOffer": None,
        }
        cases = (
            (
                [{"type": "unknown", "text": "Unknown cue."}],
                "type is invalid",
                "offer_perspective",
            ),
            (
                [
                    {"type": "manual_edit", "text": "First."},
                    {"type": "manual_edit", "text": "Second."},
                ],
                "cannot repeat a type",
                "offer_perspective",
            ),
            ([], "requires a warning uiCue", "challenge_tradeoff"),
        )

        for ui_cues, expected_error, move in cases:
            with self.subTest(expected_error=expected_error):
                payload = {
                    "assistantMessage": "A grounded response.",
                    "guidance": {
                        **base_guidance,
                        "move": move,
                        "uiCues": ui_cues,
                    },
                    "assessment": None,
                    "proposedRows": None,
                    "modificationSummary": "",
                }

                with self.assertRaisesRegex(ValueError, expected_error):
                    llm_client.validate_chat_response(payload)

    def test_stage_opening_is_neutral_and_may_include_one_question(self):
        payload = {
            "assistantMessage": (
                "The box and target share a compact central route. "
                "In my view, that makes the opening relationship easy to notice."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What would you like another player to notice first?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a direct route.",
                "difficultyOpinion": "This looks approachable to me.",
                "features": ["Compact route"],
                "suggestions": ["Discuss the opening choice"],
                "satisfactionQuestion": "What would you like another player to notice first?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, assessment_only=True)

        self.assertEqual(result[4]["move"], "observe_stage")
        self.assertIsNone(result[4]["intentHypothesis"])

    def test_stage_opening_allows_no_question(self):
        payload = {
            "assistantMessage": (
                "The water turns the lower route into a deliberate detour. "
                "To me, that gives the small room a surprisingly clear identity."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "This looks deliberate to me.",
                "features": ["Lower detour"],
                "suggestions": ["Playtest the route"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, assessment_only=True)

        self.assertIsNone(result[4]["followUpQuestion"])
        self.assertIsNone(result[1]["satisfactionQuestion"])

    def test_stage_opening_repairs_blank_guidance_and_keeps_grounded_body(self):
        payload = {
            "assistantMessage": (
                "The water makes the first route feel deliberately narrow. "
                "In my view, that gives the opening a clear rhythm."
            ),
            "guidance": {
                "move": "offer_revision",
                "intentHypothesis": "The designer wants a harder map.",
                "intentConfidence": "high",
                "followUpQuestion": "   ",
                "proposalOffer": {"summary": "Change the map"},
                "uiCues": [{"type": "unknown", "text": "Ignore this."}],
            },
            "assessment": {"solutionSummary": ""},
            "proposedRows": ["############"] * 10,
            "modificationSummary": 42,
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={"stageNumber": 2},
            rows=ENTITY_ROUTE_ROWS,
        )

        self.assertIn("deliberately narrow", result[0])
        self.assertEqual(result[4]["move"], "observe_stage")
        self.assertIsNone(result[4]["proposalOffer"])
        self.assertTrue(result[4]["openingRecovery"])

    def test_stage_opening_drops_multiple_or_leading_body_questions_but_keeps_body(self):
        payload = {
            "assistantMessage": (
                "The first push is readable from the central corridor. "
                "Should the player take the water route or the wall route? "
                "Does that make the opening feel fair? "
                "In my view, the remaining space still makes the first commitment clear."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening is readable.",
                "features": ["Central corridor"],
                "suggestions": ["Observe the first push"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={"stageNumber": 2},
            rows=ENTITY_ROUTE_ROWS,
        )

        self.assertIn("first push is readable", result[0])
        self.assertIn("remaining space", result[0])
        self.assertNotIn("?", result[0])
        self.assertIsNone(result[4]["followUpQuestion"])
        self.assertIn("leading_or_extra_questions_removed", result[4]["openingRecovery"])

    def test_human_edit_opening_drops_invalid_risk_card_but_keeps_body(self):
        payload = {
            "assistantMessage": (
                "The edited corridor now gives the first push more room to read. "
                "In my view, the new spacing makes that commitment easier to notice."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [{"type": "warning", "text": "Something might be bad."}],
                "disagreement": {"not": "a valid disagreement"},
            },
            "assessment": {
                "solutionSummary": "The saved edit remains solvable.",
                "difficultyOpinion": "In my view, the opening is easier to inspect.",
                "features": ["Edited corridor"],
                "suggestions": ["Observe the first push"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={
                "stageNumber": 2,
                "source": "human_edit",
                "discussionCardMode": "disagreement_only",
            },
            rows=ENTITY_ROUTE_ROWS,
        )

        self.assertIn("first push more room", result[0])
        self.assertEqual(result[4]["uiCues"], [])
        self.assertIn(
            "invalid_human_edit_risk_metadata_dropped",
            result[4]["openingRecovery"],
        )

    def test_stage_one_opening_removes_question_and_adds_natural_orientation(self):
        payload = {
            "assistantMessage": (
                "I notice the water makes the lower route feel more deliberate. "
                "When the box reaches the edge, what should stand out first?"
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "When the box reaches the edge, what should stand out first?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening is readable.",
                "features": ["Lower route"],
                "suggestions": ["Try the opening"],
                "satisfactionQuestion": "When the box reaches the edge, what should stand out first?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={"stageNumber": 1},
        )

        self.assertNotIn("?", result[0])
        self.assertIn("play", result[0].casefold())
        self.assertIn("edit", result[0].casefold())
        self.assertIsNone(result[4]["followUpQuestion"])
        self.assertIsNone(result[1]["satisfactionQuestion"])

    def test_later_stage_opening_drops_a_routine_question_without_uncertainty(self):
        payload = {
            "assistantMessage": "I notice the water makes the lower route feel deliberate.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What changes in your reading when the box enters the water-side corridor?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening is readable.",
                "features": ["Lower route"],
                "suggestions": ["Discuss route readability"],
                "satisfactionQuestion": "What changes in your reading when the box enters the water-side corridor?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={"stageNumber": 2},
        )

        self.assertIsNone(result[4]["followUpQuestion"])
        self.assertIsNone(result[1]["satisfactionQuestion"])

    def test_later_stage_opening_summarizes_a_clear_chinese_tradeoff(self):
        body = (
            "中间那条原本被墙隔开的路现在打通了，左上角箱子到右下角目标的路线更直接，"
            "但水塘仍会让玩家在中段谨慎处理。这个选择会直接影响关卡是清晰利落还是迂回烧脑的调性。"
        )
        generic_question = "当箱子第一次贴着调整后的水域边缘推进时，你最想观察哪一处转折？"
        payload = {
            "assistantMessage": body,
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": generic_question,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "求解器找到了可行路线。",
                "difficultyOpinion": "在我看来，中段需要更谨慎地判断。",
                "features": ["水塘与内墙"],
                "suggestions": ["观察中段推箱顺序"],
                "satisfactionQuestion": generic_question,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="zh-CN",
            stage_context={"stageNumber": 2},
        )

        focus = result[4]["followUpQuestion"]
        self.assertIn("清晰利落还是迂回烧脑", focus)
        self.assertIn("试玩时", focus)
        self.assertNotIn("最想观察哪一处转折", focus)
        self.assertEqual(result[1]["satisfactionQuestion"], focus)

    def test_verified_human_edit_opening_asks_about_the_designer_intention(self):
        payload = {
            "assistantMessage": "我更喜欢这次水域和通路之间留下的回旋空间。",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "求解器找到了可行路线。",
                "difficultyOpinion": "在我看来，新的路线更有选择感。",
                "features": ["水边通路"],
                "suggestions": ["观察第一次推动"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }
        context = {
            "stageNumber": 2,
            "source": "human_edit",
            "changeSummary": {"components": ["water", "internalWalls", "targets"]},
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="zh-CN",
            stage_context=context,
        )

        focus = result[4]["followUpQuestion"]
        self.assertIn("目标点", focus)
        self.assertTrue(any(marker in focus for marker in ("想让", "希望", "想加强")))
        self.assertNotIn("试玩时", focus)
        self.assertEqual(focus.count("？"), 1)
        self.assertEqual(result[1]["satisfactionQuestion"], focus)

    def test_human_edit_opening_replaces_a_non_intent_question_with_an_intent_question(self):
        payload = {
            "assistantMessage": (
                "我唯一有点拿不准的是T1在(2,10)那个角落。"
                "右上角现在被墙收窄了，入口只有(2,9)那一条。"
                "这个判断会影响我理解整张图的推箱顺序。"
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "求解器已确认可解。",
                "difficultyOpinion": "我认为入口会影响推箱顺序。",
                "features": ["右上角目标入口"],
                "suggestions": ["观察目标入口"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="zh-CN",
            stage_context={
                "stageNumber": 2,
                "source": "human_edit",
                "changeSummary": {"components": ["water", "internalWalls"]},
            },
        )

        focus = result[4]["followUpQuestion"]
        self.assertIn("水域、内部墙体", focus)
        self.assertTrue(any(marker in focus for marker in ("想让", "希望", "想加强")))
        self.assertNotIn("T1", focus)
        self.assertEqual(focus.count("？"), 1)

    def test_human_edit_opening_preserves_a_friendly_intent_question_in_english(self):
        payload = {
            "assistantMessage": "I like how the changed player start makes the first route less immediate.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "I would love to hear your thinking: what did you hope this new player start would change for the first push?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "To me, the opening now asks for more attention.",
                "features": ["Changed player start"],
                "suggestions": ["Observe the first push"],
                "satisfactionQuestion": None,
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            language="en",
            stage_context={
                "stageNumber": 2,
                "source": "human_edit",
                "changeSummary": {"components": ["player"]},
            },
        )

        focus = result[4]["followUpQuestion"]
        self.assertIn("what did you hope", focus.casefold())
        self.assertEqual(result[1]["satisfactionQuestion"], focus)

    def test_plain_fallback_human_edit_opening_still_has_an_intent_question(self):
        guidance = llm_client._ensure_required_guidance_card(
            {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            [],
            "en",
            OPERATION_BASE_ROWS,
            True,
            {
                "stageNumber": 3,
                "source": "human_edit",
                "changeSummary": {"components": ["water"]},
            },
        )

        self.assertIn("water area", guidance["followUpQuestion"])
        self.assertRegex(guidance["followUpQuestion"], r"\?$" )

    def test_open_ended_map_question_gets_a_discussion_card(self):
        guidance = llm_client._ensure_required_guidance_card(
            {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "uiCues": [],
            },
            [{"role": "user", "content": "你觉得水域应该怎么改？"}],
            "zh-CN",
            OPERATION_BASE_ROWS,
            False,
            {"stageNumber": 2},
        )

        self.assertTrue(guidance["followUpQuestion"])
        self.assertEqual(guidance["move"], "clarify_intent")
        self.assertIsNone(guidance["proposalOffer"])

    def test_later_turn_in_stage_one_may_still_ask_a_concrete_question(self):
        client = FakeClient(["我更倾向于让水域真正参与路线，而不是只做背景。"])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_create_async_client", return_value=client),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "我觉得水域现在只是摆设。"}],
                ["############"] * 10,
                "stage-one-later-turn-question-test",
                language="zh-CN",
                stage_context={"stageNumber": 1, "source": "initial"},
            )

        self.assertIsNotNone(result.guidance["intentHypothesis"])
        self.assertIsNone(result.guidance["followUpQuestion"])

    def test_stage_opening_persists_the_normalized_discussion_card(self):
        payload = {
            "assistantMessage": (
                "The water shapes the opening route. "
                "In my view, it makes the first push worth discussing."
            ),
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "What were you exploring with the central water?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "The solver found a route.",
                "difficultyOpinion": "In my view, the opening requires attention.",
                "features": ["Central water"],
                "suggestions": ["Discuss the opening push"],
                "satisfactionQuestion": "What other part should we discuss next?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(payload, assessment_only=True)

        self.assertIn("My reading of this version", result[4]["followUpQuestion"])
        self.assertEqual(result[1]["satisfactionQuestion"], result[4]["followUpQuestion"])

    def test_stage_opening_prompt_has_a_short_non_conflicting_contract(self):
        messages = llm_client.build_chat_messages(
            [],
            ["############"] * 10,
            assessment_only=True,
        )
        prompt = messages[0]["content"]

        self.assertIn("one or two declarative observations", prompt)
        self.assertIn("your own design reaction", prompt)
        self.assertIn("do not put questions, choices", prompt)
        self.assertIn("The server owns optional discussion metadata", prompt)
        self.assertIn("satisfactionQuestion must be null", prompt)
        self.assertNotIn("At a real decision point, ask one concrete question", prompt)
        self.assertNotIn("at most 3 more may be asked", prompt)

    def test_dg_initial_provenance_does_not_attribute_exact_tiles(self):
        guidance = llm_client._build_draft_provenance_guidance(
            {
                "source": "initial",
                "initialDraftMethod": "description_generation",
            }
        )

        self.assertIn("generator produced every exact tile placement", guidance)
        self.assertIn("Never ask why the designer placed", guidance)
        self.assertIn("Do not invent or quote parameter values", guidance)
        self.assertIn("Never say or imply that the designer intended", guidance)
        self.assertIn("How does this generated result compare", guidance)
        self.assertIn("what surprised them", guidance)

    def test_algorithm_demo_provenance_does_not_attribute_generated_tiles(self):
        guidance = llm_client._build_draft_provenance_guidance(
            {
                "source": "initial",
                "initialDraftMethod": "algorithm_demo",
            }
        )

        self.assertIn("standalone algorithm-generated demo draft", guidance)
        self.assertIn("created every exact visible tile placement", guidance)
        self.assertIn("Never attribute any visible tile", guidance)
        self.assertIn("do not infer a hidden design intention", guidance)

    def test_pc_initial_provenance_separates_sketch_from_completion(self):
        guidance = llm_client._build_draft_provenance_guidance(
            {
                "source": "initial",
                "initialDraftMethod": "partial_completion",
            }
        )

        self.assertIn("box starts, targets, and broad room/wall constraints", guidance)
        self.assertIn("added the exact water, generated internal walls", guidance)
        self.assertIn("never attribute a particular internal wall", guidance)
        self.assertIn("never claim that the completion system produced or filled in all walls", guidance)
        self.assertIn("box-target relationship", guidance)

    def test_later_stage_provenance_uses_actual_stage_source(self):
        human = llm_client._build_draft_provenance_guidance(
            {"source": "human_edit", "initialDraftMethod": "description_generation"}
        )
        accepted = llm_client._build_draft_provenance_guidance(
            {"source": "llm_accepted", "initialDraftMethod": "partial_completion"}
        )
        restored = llm_client._build_draft_provenance_guidance(
            {"source": "restored", "initialDraftMethod": "partial_completion"}
        )

        self.assertIn("directly edited by the designer", human)
        self.assertIn("explicitly accepted", accepted)
        self.assertIn("restores an earlier saved version", restored)

    def test_stage_opening_removes_recoverable_english_choice_anchor(self):
        question = (
            "What drew you to place the target beside the box—were you aiming for "
            "a quick solve, or a longer sequence?"
        )

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "What drew you to place the target beside the box?",
        )

    def test_stage_opening_removes_recoverable_chinese_choice_anchor(self):
        question = "你把箱子和目标放在紧邻的位置，是希望快速完成，还是继续扩展？"

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "你把箱子和目标放在紧邻的位置时，你最先考虑的是什么？",
        )

    def test_stage_opening_chinese_anchor_does_not_duplicate_time_suffix(self):
        question = "你设计这个初始布局时，是想保持开放，还是增加墙体？"

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "你设计这个初始布局时，你最先考虑的是什么？",
        )

    def test_stage_opening_converts_recoverable_chinese_yes_no_question(self):
        question = "你选择把箱子和目标放在同一行，是刻意强调绕行吗？"

        self.assertEqual(
            llm_client._normalize_opening_question(question),
            "你选择把箱子和目标放在同一行时，你最先考虑的是什么？",
        )

    def test_stage_opening_rejects_english_yes_no_question(self):
        with self.assertRaisesRegex(ValueError, "cannot anchor"):
            llm_client._normalize_opening_question(
                "Did you intend the central water to control the first push?"
            )

    def test_stage_opening_rejects_unrecoverable_choice_anchor(self):
        with self.assertRaisesRegex(ValueError, "cannot anchor"):
            llm_client._normalize_opening_question(
                "Would you prefer a quick solve or a longer sequence?"
            )

    def test_stage_opening_single_block_remains_natural(self):
        message = (
            "The water divides the room. In my view, that makes the first push clearer. "
            "The open lower area may still invite experimentation."
        )

        result = llm_client._format_stage_opening_paragraphs(message)

        self.assertEqual(len(result.split("\n\n")), 1)
        self.assertIn("In my view", result)

    def test_stage_opening_drops_ordinary_intention_metadata_but_keeps_analysis(self):
        payload = {
            "assistantMessage": "You want a difficult level.",
            "guidance": {
                "move": "observe_stage",
                "intentHypothesis": "You want a difficult level.",
                "intentConfidence": "medium",
                "followUpQuestion": "What shaped this placement choice?",
                "proposalOffer": None,
                "uiCues": [],
            },
            "assessment": {
                "solutionSummary": "A route exists.",
                "difficultyOpinion": "It may be difficult.",
                "features": ["One route"],
                "suggestions": ["Review it"],
                "satisfactionQuestion": "What shaped this placement choice?",
            },
            "proposedRows": None,
            "modificationSummary": "",
        }

        result = llm_client.validate_chat_response(
            payload,
            assessment_only=True,
            stage_context={"stageNumber": 2},
        )

        self.assertIn("difficult level", result[0])
        self.assertIsNone(result[4]["intentHypothesis"])
        self.assertIsNone(result[4]["intentConfidence"])
        self.assertIsNone(result[4]["proposalOffer"])

    def test_human_edit_opening_acknowledges_verified_changes_in_chinese(self):
        message = llm_client._compose_assistant_message(
            "在我看来，这会让路线选择更集中。",
            {
                "move": "observe_stage",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": "这符合你想强调的体验吗？",
                "proposalOffer": None,
            },
            language="zh-CN",
            assessment_only=True,
            stage_context={
                "source": "human_edit",
                "changeSummary": {
                    "components": ["water", "internalWalls", "player"],
                    "changedCellCount": 6,
                },
            },
        )

        self.assertIn("我注意到你对水域、内部墙体、玩家位置进行了修改", message)
        self.assertIn("已通过确定性检查并确认可解", message)
        self.assertIn("在我看来", message)
        self.assertTrue(message.endswith("这符合你想强调的体验吗？"))

    def test_translation_validation_preserves_ids_nulls_and_cue_order(self):
        source = [
            {
                "turnId": "turn-1",
                "body": "A compact route.",
                "followUpQuestion": "What should stand out?",
                "intentHypothesis": None,
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "uiCueTexts": ["The corner may be tight."],
                "proposalSummary": None,
            }
        ]
        payload = {
            "translations": [
                {
                    "turnId": "turn-1",
                    "body": "一条紧凑的路线。",
                    "followUpQuestion": "你希望什么最突出？",
                    "intentHypothesis": None,
                    "proposalOfferSummary": None,
                    "proposalOfferRationale": None,
                    "uiCueTexts": ["这个角落可能较紧。"],
                    "proposalSummary": None,
                }
            ]
        }

        self.assertEqual(
            llm_client.validate_translation_response(payload, source),
            payload["translations"],
        )

        payload["translations"][0]["intentHypothesis"] = "新增的意图"

        with self.assertRaisesRegex(ValueError, "must remain null"):
            llm_client.validate_translation_response(payload, source)

    def test_translation_preserves_coordinate_endpoints_and_translates_only_link_text(self):
        source = [{
            "turnId": "turn-route",
            "body": "Move from (5,5) to (5,7).",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
            "coordinateLinks": [{
                "text": "from (5,5) to (5,7)",
                "from": {"row": 5, "column": 5},
                "to": {"row": 5, "column": 7},
            }],
        }]
        payload = {"translations": [{
            "turnId": "turn-route",
            "body": "from (5,5) to (5,7).",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
            "coordinateLinkTexts": ["from (5,5) to (5,7)"],
        }]}

        translated = llm_client.validate_translation_response(payload, source)[0]

        self.assertEqual(
            translated["coordinateLinkTexts"],
            ["from (5,5) to (5,7)"],
        )

    def test_translation_normalizes_false_multi_level_language(self):
        source = [{
            "turnId": "turn-level",
            "body": "This version has more route choice.",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
        }]
        payload = {"translations": [{
            "turnId": "turn-level",
            "body": "这很符合第二关该有的手感，也会影响后面关卡。",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
        }]}

        translated = llm_client.validate_translation_response(payload, source)[0]["body"]

        self.assertIn("这个版本现在呈现出的手感", translated)
        self.assertIn("后续版本", translated)
        self.assertNotIn("第二关", translated)


    def test_content_blocks_render_server_owned_facts_and_route(self):
        bindings = build_entity_bindings(ENTITY_ROUTE_ROWS, source="initial")
        payload = {
            "assistantMessage": "Fallback analysis.",
            "contentBlocks": [
                {"kind": "factRef", "factType": "entity_position", "entity": "B1"},
                {
                    "kind": "routeReasoning",
                    "fromEntity": "B1",
                    "toEntity": "T1",
                    "text": "the corridor makes the first push feel like a readable commitment.",
                },
                {
                    "kind": "personalReflection",
                    "text": "Personally, I like how that choice makes the route rhythm feel deliberate.",
                },
            ],
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "disagreement": None,
                "uiCues": [],
                "coordinateLinks": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        message, _assessment, _rows, _summary, guidance = llm_client.validate_chat_response(
            payload,
            rows=ENTITY_ROUTE_ROWS,
            stage_context={"entityBindings": bindings},
        )

        self.assertIn("B1 is currently at row 3, column 5", message)
        self.assertIn("B1 \u2192 T1", message)
        self.assertIn("Personally, I like", message)
        self.assertEqual(len(guidance["coordinateLinks"]), 1)

    def test_content_blocks_drop_invalid_fact_reference_without_losing_analysis(self):
        payload = {
            "assistantMessage": "The corridor still gives the first push a clear rhythm.",
            "contentBlocks": [
                {"kind": "factRef", "factType": "tile_state", "row": 99, "column": 99},
                {"kind": "analysis", "text": "The corridor still gives the first push a clear rhythm."},
            ],
            "guidance": {
                "move": "offer_perspective",
                "intentHypothesis": None,
                "intentConfidence": None,
                "followUpQuestion": None,
                "proposalOffer": None,
                "disagreement": None,
                "uiCues": [],
                "coordinateLinks": [],
            },
            "assessment": None,
            "proposedRows": None,
            "modificationSummary": "",
        }

        message, *_rest = llm_client.validate_chat_response(payload, rows=ENTITY_ROUTE_ROWS)

        self.assertIn("corridor still gives", message)
        self.assertNotIn("99", message)

    def test_overlong_content_compacts_only_route_and_personal_reflection(self):
        body = (
            "The water makes the first push a meaningful design choice. "
            "B2 moves from (2,2) -> (2,3) -> (2,4) -> (2,5) -> (3,5) -> (4,5) -> (5,5). "
            "I feel the corridor is tense and deliberate. I think the corridor is tense and deliberate. "
            "Personally, I find the same corridor tense and deliberate."
        )

        recovered, diagnostics = llm_client._recover_overlong_content(
            body,
            ENTITY_ROUTE_ROWS,
        )

        self.assertIn("first push a meaningful design choice", recovered)
        self.assertLessEqual(
            len(llm_client._personal_reflection_sentences(recovered)),
            llm_client.PERSONAL_REFLECTION_SENTENCE_LIMIT,
        )
        self.assertTrue(diagnostics["changed"])

    def test_presentation_budget_does_not_discard_non_route_analysis(self):
        body = "A" * (llm_client.CHAT_RESPONSE_MAX_LENGTH + 100)
        recovered, diagnostics = llm_client._recover_overlong_content(body, ENTITY_ROUTE_ROWS)

        self.assertEqual(recovered, body)
        self.assertFalse(diagnostics["changed"])

    def test_translation_drops_new_invalid_map_claim_using_its_stage_snapshot(self):
        source = {
            "turnId": "translation-snapshot-test",
            "body": "The first push remains a readable commitment.",
            "followUpQuestion": None,
            "intentHypothesis": None,
            "proposalOfferSummary": None,
            "proposalOfferRationale": None,
            "uiCueTexts": [],
            "proposalSummary": None,
            "stageRows": ENTITY_ROUTE_ROWS,
            "entityBindings": build_entity_bindings(ENTITY_ROUTE_ROWS, source="initial"),
        }
        payload = {
            "translations": [{
                "turnId": "translation-snapshot-test",
                "body": "B1 is at (99,99).",
                "followUpQuestion": None,
                "intentHypothesis": None,
                "proposalOfferSummary": None,
                "proposalOfferRationale": None,
                "uiCueTexts": [],
                "proposalSummary": None,
            }]
        }

        translated = llm_client.validate_translation_response(
            payload,
            [source],
            target_language="en",
        )

        self.assertEqual(translated[0]["body"], "I will continue from the current saved Stage.")
        self.assertNotIn("99", translated[0]["body"])


if __name__ == "__main__":
    unittest.main()
