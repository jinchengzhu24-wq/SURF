import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

try:
    from . import app as backend
    from .llm_runtime import LLMExecutionResult
    from .prompt import resolve_zero_feature_constraints
except ImportError:
    import app as backend
    from llm_runtime import LLMExecutionResult
    from prompt import resolve_zero_feature_constraints


class DescriptionGenerationApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)

    def test_post_forwards_description_and_nested_preferences(self):
        captured = {}

        def create_level_plan(context, request_id, max_attempts):
            captured.update(context)
            return LLMExecutionResult(
                copy.deepcopy(backend.DEFAULT_PLAN),
                1,
                request_id,
            )

        payload = {
            "styleDescription": "A compact waterside workshop.",
            "generationPreferences": {
                "minSolutionSteps": 22,
                "maxSolutionSteps": 42,
                "archetype": "open_workshop",
            },
        }

        with patch.object(
            backend,
            "create_level_plan",
            side_effect=create_level_plan,
        ):
            response = self.client.post("/generate-level-plan", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            captured["styleDescription"],
            "A compact waterside workshop.",
        )
        self.assertEqual(
            captured["generationPreferences"]["archetype"],
            "open_workshop",
        )

    def test_manual_parameters_override_conflicting_description(self):
        context = {
            "styleDescription": "Create a level with no water.",
            "generationPreferences": {
                "minSolutionSteps": 22,
                "maxSolutionSteps": 42,
                "minPushes": 10,
                "maxPushes": 22,
                "minReversePulls": 18,
                "maxReversePulls": 34,
                "minWaterAreas": 2,
                "maxWaterAreas": 2,
                "archetype": "split_route",
            },
        }
        captured_messages = []

        def execute_json_request(**kwargs):
            captured_messages.extend(kwargs["messages"])
            value = kwargs["validator"](copy.deepcopy(backend.DEFAULT_PLAN))
            return LLMExecutionResult(value, 1, kwargs["request_id"])

        with patch.object(
            backend,
            "execute_json_request",
            side_effect=execute_json_request,
        ):
            execution = backend.create_level_plan(
                context,
                request_id="description-request",
                max_attempts=1,
            )

        self.assertEqual(execution.value["minWaterAreas"], 2)
        self.assertEqual(execution.value["maxWaterAreas"], 2)
        self.assertEqual(execution.value["archetype"], "split_route")
        prompt_text = "\n".join(message["content"] for message in captured_messages)
        self.assertIn("minWaterAreas=2", prompt_text)
        self.assertIn("authoritative hard constraints", prompt_text)

    def test_zero_walls_cannot_request_a_corridor(self):
        with self.assertRaisesRegex(ValueError, "zero internal wall"):
            backend.normalize_generation_preferences(
                {
                    "minWallObstacleBlocks": 0,
                    "maxWallObstacleBlocks": 0,
                    "corridorPlacement": "center",
                    "corridorWidth": 1,
                }
            )

    def test_invalid_api_preferences_return_400_before_llm_call(self):
        response = self.client.post(
            "/generate-level-plan",
            json={
                "generationPreferences": {
                    "minSolutionSteps": 30,
                    "maxSolutionSteps": 20,
                }
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("maxSolutionSteps", response.json()["detail"])

    def test_dg_guide_returns_validated_llm_summary(self):
        result = {
            "summary": "You are designing for a close friend.",
            "rationale": (
                "I would use a moderate amount of planning pressure so decisions matter "
                "while their consequences remain understandable. Because a friend can read "
                "this as a shared challenge, I recommend Medium without using opaque traps."
            ),
            "recommendedDifficulty": "Medium",
        }

        def execute_json_request(**kwargs):
            return LLMExecutionResult(
                kwargs["validator"](result),
                1,
                "guide-request",
            )

        with patch.object(backend, "execute_json_request", side_effect=execute_json_request):
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "opponentRelationship": "friend",
                    "opponentExperience": "challenging_fair",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Medium")
        self.assertEqual(response.json()["source"], "llm")
        self.assertTrue(response.json()["summary"].startswith("I get the sense that"))

    def test_dg_fallback_jointly_considers_relationship_and_experience(self):
        friend = backend.build_dg_fallback_summary("friend", "breakthrough")
        stranger = backend.build_dg_fallback_summary("stranger", "breakthrough")

        self.assertEqual(friend["recommendedDifficulty"], "Hard")
        self.assertEqual(stranger["recommendedDifficulty"], "Medium")
        self.assertIn("shared challenge", friend["summary"])
        self.assertIn("first encounter", stranger["summary"])
        self.assertGreaterEqual(friend["rationale"].count("."), 2)

    def test_dg_guide_rejects_unknown_choice(self):
        response = self.client.post(
            "/dg/guide/summary",
            json={
                "opponentRelationship": "unknown",
                "opponentExperience": "challenging_fair",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_dg_guide_uses_readable_fallback_after_llm_failure(self):
        with patch.object(
            backend,
            "execute_json_request",
            side_effect=backend.LLMServiceError(
                "MODEL_UNAVAILABLE",
                "dg_guide_summary",
                "Model unavailable.",
                "guide-request",
                True,
                1,
                503,
            ),
        ):
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "opponentRelationship": "acquaintance",
                    "opponentExperience": "breakthrough",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Hard")
        self.assertEqual(response.json()["source"], "deterministic_fallback")
        self.assertTrue(response.json()["summary"].startswith("I get the sense that"))
        self.assertTrue(response.json()["rationale"].startswith("I would"))

    def test_custom_single_wall_range_is_supported(self):
        preferences = backend.normalize_generation_preferences(
            {
                "minWallObstacleBlocks": 1,
                "maxWallObstacleBlocks": 1,
            }
        )
        plan = copy.deepcopy(backend.DEFAULT_PLAN)
        result = backend.apply_selected_ha_plan(
            plan,
            {},
            {"noWater": False, "noInternalWalls": False},
            preferences,
        )

        self.assertEqual(result["minWallObstacleBlocks"], 1)
        self.assertEqual(result["maxWallObstacleBlocks"], 1)

    def test_chinese_style_description_can_request_zero_water(self):
        constraints = resolve_zero_feature_constraints(
            {"styleDescription": "我希望这是一个不要水的紧凑关卡"}
        )

        self.assertTrue(constraints["noWater"])


if __name__ == "__main__":
    unittest.main()
