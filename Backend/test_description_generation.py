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
            "summary": (
                "You may be aiming for clear planning pressure in a readable space. "
                "That combination would let the opponent read nearby choices before tracing a longer return path."
            ),
            "difficultyRationale": (
                "I would keep the first useful move readable while making some push order matter. "
                "I therefore recommend Medium difficulty."
            ),
            "recommendedDifficulty": "Medium",
            "layoutRationale": (
                "I would keep key positions distributed across connected areas with direct routes. "
                "I therefore recommend a Balanced layout."
            ),
            "recommendedLayout": "Balanced",
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
                    "firstMovePreference": "observe_then_decide",
                    "pushPlanningPreference": "consider_order",
                    "spacePreference": "connected_areas",
                    "routeRhythmPreference": "occasional_detours",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Medium")
        self.assertEqual(response.json()["recommendedLayout"], "Balanced")
        self.assertEqual(response.json()["source"], "llm")
        self.assertTrue(response.json()["summary"].startswith("My read is that"))

    def test_dg_prompt_explains_new_answer_meanings_and_no_preference(self):
        messages = backend.build_dg_guide_summary_messages({
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
        })
        system_prompt = messages[0]["content"]

        self.assertIn("quick_start means little inspection before acting", system_prompt)
        self.assertIn("easy_to_adjust means most pushes can be considered independently", system_prompt)
        self.assertIn("no_preference means no stated preference", system_prompt)
        self.assertIn("use Medium difficulty and Balanced layout", system_prompt)

    def test_dg_fallback_combines_neutral_answers_by_parameter_group(self):
        focused = backend.build_dg_fallback_summary({
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "no_preference",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
        })
        open_layout = backend.build_dg_fallback_summary({
            "firstMovePreference": "no_preference",
            "pushPlanningPreference": "connected_pushes",
            "spacePreference": "wide_area",
            "routeRhythmPreference": "long_routes",
        })
        defaults = backend.build_dg_fallback_summary({
            "firstMovePreference": "no_preference",
            "pushPlanningPreference": "no_preference",
            "spacePreference": "no_preference",
            "routeRhythmPreference": "no_preference",
        })

        self.assertEqual(focused["recommendedDifficulty"], "Easy")
        self.assertEqual(focused["recommendedLayout"], "Compact")
        self.assertEqual(open_layout["recommendedDifficulty"], "Hard")
        self.assertEqual(open_layout["recommendedLayout"], "Open")
        self.assertEqual(defaults["recommendedDifficulty"], "Medium")
        self.assertEqual(defaults["recommendedLayout"], "Balanced")
        self.assertIn("you want the opponent", focused["summary"])
        self.assertIn("get moving with little inspection", focused["summary"])
        self.assertNotIn("Tell me if I am reading", focused["summary"])
        self.assertNotEqual(focused["summary"], open_layout["summary"])

    def test_dg_fallback_rounds_mixed_preferences_up_like_unity(self):
        result = backend.build_dg_fallback_summary({
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "consider_order",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "occasional_detours",
        })

        self.assertEqual(result["recommendedDifficulty"], "Medium")
        self.assertEqual(result["recommendedLayout"], "Balanced")

    def test_dg_rationale_accepts_contractions_and_adds_first_person_prefix_when_needed(self):
        self.assertIn(
            "I'd recommend Hard",
            backend.normalize_dg_rationale(
                "The push order raises pressure across several decisions and makes early choices matter. "
                "That is why I'd recommend Hard for this planning rhythm.",
                "difficulty",
            ),
        )
        self.assertTrue(
            backend.normalize_dg_rationale(
                "The wide routes support an Open layout and give the player room to inspect distant choices. "
                "The space gives exploration and return paths room to breathe.",
                "layout",
            ).startswith("I read this as: ")
        )

    def test_dg_guide_rejects_unknown_choice(self):
        response = self.client.post(
                "/dg/guide/summary",
                json={
                    "firstMovePreference": "unknown",
                    "pushPlanningPreference": "consider_order",
                    "spacePreference": "connected_areas",
                    "routeRhythmPreference": "occasional_detours",
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
                    "firstMovePreference": "plan_ahead",
                    "pushPlanningPreference": "connected_pushes",
                    "spacePreference": "wide_area",
                    "routeRhythmPreference": "long_routes",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Hard")
        self.assertEqual(response.json()["recommendedLayout"], "Open")
        self.assertEqual(response.json()["source"], "deterministic_fallback")
        self.assertTrue(response.json()["summary"].startswith("My read is that"))
        self.assertTrue(response.json()["difficultyRationale"].startswith("I would"))

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
