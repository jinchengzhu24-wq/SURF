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

    def test_level_plan_records_blueprint_handoff_in_existing_runtime_log(self):
        execution = LLMExecutionResult(
            copy.deepcopy(backend.DEFAULT_PLAN),
            1,
            "blueprint-handoff-request",
        )

        with patch.object(backend, "create_level_plan", return_value=execution), patch.object(
            backend, "log_event"
        ) as logger:
            response = self.client.post(
                "/generate-level-plan",
                json={"dgContext": {"recommendedDifficulty": "Medium"}},
                headers={"X-Request-ID": "blueprint-handoff-request"},
            )

        self.assertEqual(response.status_code, 200)
        handoff = next(
            call for call in logger.call_args_list if call.args[1] == "agent_handoff"
        )
        self.assertEqual(handoff.kwargs["fromAgent"], "blueprint_planning")
        self.assertEqual(handoff.kwargs["toAgent"], "unity_generator")
        self.assertEqual(handoff.kwargs["artifactType"], "LevelDesignPlan")
        self.assertEqual(
            handoff.kwargs["artifact"]["levelDesignPlan"],
            backend.DEFAULT_PLAN,
        )
        self.assertTrue(handoff.kwargs["evidence"][1]["present"])

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
                "Your choices suggest readable planning in a connected space. "
                "My read is that nearby decisions can lead into a manageable route between areas."
            ),
            "difficultyRationale": (
                "I connect some first-move inspection with some push-order planning, which supports Medium difficulty."
            ),
            "recommendedDifficulty": "Medium",
            "layoutRationale": (
                "I connect a few connected areas with mostly direct routes and some detours, which supports a Balanced layout."
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
        self.assertTrue(response.json()["summary"].startswith("Your choices suggest"))

    def test_dg_guide_records_draft_to_blueprint_handoff(self):
        result = {
            "summary": "Your choices suggest readable planning in a connected space. My read is that nearby decisions can lead into a manageable route between areas.",
            "difficultyRationale": "I connect some first-move inspection with some push-order planning, which supports Medium difficulty.",
            "recommendedDifficulty": "Medium",
            "layoutRationale": "I connect a few connected areas with mostly direct routes and some detours, which supports a Balanced layout.",
            "recommendedLayout": "Balanced",
        }

        def execute_json_request(**kwargs):
            return LLMExecutionResult(kwargs["validator"](result), 1, "dg-handoff-request")

        with patch.object(
            backend, "execute_json_request", side_effect=execute_json_request
        ), patch.object(backend, "log_event") as logger:
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "firstMovePreference": "observe_then_decide",
                    "pushPlanningPreference": "consider_order",
                    "spacePreference": "connected_areas",
                    "routeRhythmPreference": "occasional_detours",
                },
                headers={"X-Request-ID": "dg-handoff-request"},
            )

        self.assertEqual(response.status_code, 200)
        handoff = next(
            call for call in logger.call_args_list if call.args[1] == "agent_handoff"
        )
        self.assertEqual(handoff.kwargs["fromAgent"], "draft_understanding")
        self.assertEqual(handoff.kwargs["toAgent"], "blueprint_planning")
        self.assertEqual(handoff.kwargs["artifactType"], "dgContext")
        self.assertEqual(
            handoff.kwargs["artifact"]["dgContext"]["spacePreference"],
            "connected_areas",
        )
        self.assertEqual(handoff.kwargs["status"], "confirmed")

    def test_dg_guide_accepts_random_recommendations_for_two_no_preference_answers(self):
        result = {
            "summary": (
                "I am not hearing a strong preference for the solving pressure or the space. "
                "My read is that a little variation would keep the level feeling open-ended."
            ),
            "difficultyRationale": (
                "I do not see a directional planning preference in either difficulty answer, so I would leave Difficulty Random for Confirm."
            ),
            "recommendedDifficulty": "Random",
            "layoutRationale": (
                "I do not see a directional spatial preference in either layout answer, so I would leave Layout Random for Confirm."
            ),
            "recommendedLayout": "Random",
        }

        def execute_json_request(**kwargs):
            return LLMExecutionResult(
                kwargs["validator"](result),
                1,
                "guide-random-request",
            )

        with patch.object(backend, "execute_json_request", side_effect=execute_json_request):
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "firstMovePreference": "no_preference",
                    "pushPlanningPreference": "no_preference",
                    "spacePreference": "no_preference",
                    "routeRhythmPreference": "no_preference",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Random")
        self.assertEqual(response.json()["recommendedLayout"], "Random")

    def test_dg_prompt_explains_new_answer_meanings_and_no_preference(self):
        messages = backend.build_dg_guide_summary_messages({
            "firstMovePreference": "quick_start",
            "pushPlanningPreference": "easy_to_adjust",
            "spacePreference": "focused_area",
            "routeRhythmPreference": "short_routes",
        })
        system_prompt = messages[0]["content"]

        self.assertIn("quick_start means little inspection before the first move", system_prompt)
        self.assertIn("easy_to_adjust means most pushes can be considered independently", system_prompt)
        self.assertIn("one no_preference means ignore that answer", system_prompt)
        self.assertIn("both answers are no_preference", system_prompt)
        self.assertIn("low plus middle uses the low endpoint", system_prompt)
        self.assertIn("Only for this conflict may the AI recommend", system_prompt)
        self.assertNotIn("two explicit answers differ, use Medium", system_prompt)

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
        self.assertEqual(defaults["recommendedDifficulty"], "Random")
        self.assertEqual(defaults["recommendedLayout"], "Random")
        self.assertIn("Your choices suggest", focused["summary"])
        self.assertIn("get moving with little inspection", focused["summary"])
        self.assertNotIn("Tell me if I am reading", focused["summary"])
        self.assertNotEqual(focused["summary"], open_layout["summary"])

    def test_dg_fallback_uses_endpoints_for_adjacent_explicit_directions(self):
        result = backend.build_dg_fallback_summary({
            "firstMovePreference": "observe_then_decide",
            "pushPlanningPreference": "connected_pushes",
            "spacePreference": "connected_areas",
            "routeRhythmPreference": "long_routes",
        })

        self.assertEqual(result["recommendedDifficulty"], "Hard")
        self.assertEqual(result["recommendedLayout"], "Open")

    def test_dg_score_and_allowed_scores_handle_conflicts(self):
        difficulty = backend.DG_DIFFICULTY_ANSWERS
        self.assertEqual(
            backend.dg_preference_score("quick_start", "observe_then_decide", difficulty),
            0,
        )
        self.assertEqual(
            backend.dg_preference_score("observe_then_decide", "connected_pushes", difficulty),
            2,
        )
        self.assertEqual(
            backend.dg_preference_score("quick_start", "connected_pushes", difficulty),
            1,
        )
        self.assertEqual(
            backend.dg_preference_allowed_scores(
                "observe_then_decide", "connected_pushes", difficulty
            ),
            {1, 2},
        )
        self.assertEqual(
            backend.dg_preference_allowed_scores(
                "quick_start", "connected_pushes", difficulty
            ),
            {0, 1, 2},
        )
        self.assertEqual(
            backend.dg_preference_allowed_scores(
                "quick_start", "no_preference", difficulty
            ),
            {0},
        )
        layout = backend.DG_LAYOUT_ANSWERS
        self.assertEqual(
            backend.dg_preference_score("focused_area", "connected_areas", layout),
            0,
        )
        self.assertEqual(
            backend.dg_preference_score("connected_areas", "wide_area", layout),
            2,
        )
        self.assertEqual(
            backend.dg_preference_allowed_scores(
                "focused_area", "wide_area", layout
            ),
            {0, 1, 2},
        )
        self.assertIsNone(
            backend.dg_preference_allowed_scores(
                "no_preference", "no_preference", difficulty
            )
        )

    def test_dg_guide_accepts_one_step_ai_adjustment_only_for_conflict(self):
        result = {
            "summary": (
                "Your choices suggest a careful start with stronger connections between pushes. "
                "My read is that a little more room for planning could fit this combination."
            ),
            "difficultyRationale": (
                "I hear some inspection before acting and several dependent pushes, so I lean toward Medium while keeping the planning readable."
            ),
            "recommendedDifficulty": "Medium",
            "layoutRationale": (
                "I hear connected areas and longer routes, so I lean toward Balanced while keeping the space easy to follow."
            ),
            "recommendedLayout": "Balanced",
        }

        def execute_json_request(**kwargs):
            return LLMExecutionResult(
                kwargs["validator"](result),
                1,
                "guide-adjustment-request",
            )

        with patch.object(backend, "execute_json_request", side_effect=execute_json_request):
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "firstMovePreference": "observe_then_decide",
                    "pushPlanningPreference": "connected_pushes",
                    "spacePreference": "connected_areas",
                    "routeRhythmPreference": "long_routes",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Medium")
        self.assertEqual(response.json()["recommendedLayout"], "Balanced")
        self.assertEqual(response.json()["source"], "llm")

    def test_dg_guide_falls_back_when_ai_changes_a_non_conflict(self):
        result = {
            "summary": (
                "Your choices suggest a straightforward start and independent pushes. "
                "My read is that the map can stay focused and easy to read."
            ),
            "difficultyRationale": "I would choose Hard for this planning rhythm.",
            "recommendedDifficulty": "Hard",
            "layoutRationale": "I would choose Open for this spatial rhythm.",
            "recommendedLayout": "Open",
        }

        def execute_json_request(**kwargs):
            with self.assertRaisesRegex(ValueError, "does not match"):
                kwargs["validator"](result)
            raise backend.LLMServiceError(
                "LLM_VALIDATION_FAILED",
                "dg_guide_summary",
                "Invalid recommendation.",
                "guide-non-conflict-request",
                True,
                1,
                502,
            )

        with patch.object(backend, "execute_json_request", side_effect=execute_json_request):
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "firstMovePreference": "quick_start",
                    "pushPlanningPreference": "easy_to_adjust",
                    "spacePreference": "focused_area",
                    "routeRhythmPreference": "short_routes",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Easy")
        self.assertEqual(response.json()["recommendedLayout"], "Compact")
        self.assertEqual(response.json()["source"], "deterministic_fallback")

    def test_dg_guide_falls_back_when_ai_resolves_random(self):
        result = {
            "summary": (
                "Your choices do not point strongly in one direction for either setting. "
                "My read is that leaving both choices open keeps the draft flexible."
            ),
            "difficultyRationale": "I would choose Easy for this planning rhythm.",
            "recommendedDifficulty": "Easy",
            "layoutRationale": "I would choose Compact for this spatial rhythm.",
            "recommendedLayout": "Compact",
        }

        def execute_json_request(**kwargs):
            with self.assertRaisesRegex(ValueError, "does not match"):
                kwargs["validator"](result)
            raise backend.LLMServiceError(
                "LLM_VALIDATION_FAILED",
                "dg_guide_summary",
                "Invalid recommendation.",
                "guide-random-conversion-request",
                True,
                1,
                502,
            )

        with patch.object(backend, "execute_json_request", side_effect=execute_json_request):
            response = self.client.post(
                "/dg/guide/summary",
                json={
                    "firstMovePreference": "no_preference",
                    "pushPlanningPreference": "no_preference",
                    "spacePreference": "no_preference",
                    "routeRhythmPreference": "no_preference",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendedDifficulty"], "Random")
        self.assertEqual(response.json()["recommendedLayout"], "Random")
        self.assertEqual(response.json()["source"], "deterministic_fallback")

    def test_dg_rationale_requires_one_sentence_and_adds_first_person_prefix_when_needed(self):
        self.assertIn(
            "I'd recommend Hard",
            backend.normalize_dg_rationale(
                "The push order raises planning complexity across several decisions, so I'd recommend Hard for this planning rhythm.",
                "difficulty",
            ),
        )
        self.assertTrue(
            backend.normalize_dg_rationale(
                "The wide routes support an Open layout and give the player room to inspect distant choices.",
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
        self.assertTrue(response.json()["summary"].startswith("Your choices suggest"))
        self.assertTrue(response.json()["difficultyRationale"].startswith("I connect"))

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
