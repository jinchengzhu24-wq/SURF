import unittest

from Backend.app import (
    DEFAULT_PLAN,
    validate_human_adjustment_clarity_payload,
    validate_plan,
)
from Backend.prompt import (
    build_human_adjustment_clarity_messages,
    build_level_plan_messages,
    resolve_zero_feature_constraints,
)


class FeatureConstraintTests(unittest.TestCase):
    def make_plan(self):
        return dict(DEFAULT_PLAN)

    def test_default_context_keeps_existing_ranges(self):
        constraints = resolve_zero_feature_constraints(
            {"originalIdeaText": "A compact route-planning puzzle."}
        )
        plan = validate_plan(self.make_plan(), constraints)

        self.assertFalse(constraints["noWater"])
        self.assertFalse(constraints["noInternalWalls"])
        self.assertEqual((plan["minWaterAreas"], plan["maxWaterAreas"]), (1, 2))
        self.assertEqual(
            (plan["minWallObstacleBlocks"], plan["maxWallObstacleBlocks"]),
            (2, 3),
        )

    def test_default_context_rejects_unrequested_zero_values(self):
        plan = self.make_plan()
        plan["minWaterAreas"] = 0
        plan["maxWaterAreas"] = 0

        with self.assertRaisesRegex(ValueError, "explicitly requests no water"):
            validate_plan(plan, {"noWater": False, "noInternalWalls": False})

    def test_english_no_water_is_forced_to_zero(self):
        context = {"originalIdeaText": "Make a compact level without water."}
        constraints = resolve_zero_feature_constraints(context)
        plan = validate_plan(self.make_plan(), constraints)

        self.assertTrue(constraints["noWater"])
        self.assertEqual((plan["minWaterAreas"], plan["maxWaterAreas"]), (0, 0))

    def test_chinese_no_internal_walls_disables_corridor(self):
        context = {"latestAdjustmentText": "去掉内部墙，只保留开放空间"}
        constraints = resolve_zero_feature_constraints(context)
        raw_plan = self.make_plan()
        raw_plan.update(
            {
                "corridorPlacement": "center",
                "corridorWidth": 1,
                "corridorOrientation": "vertical",
                "corridorRole": "required_box_route",
                "corridorPriority": "required",
            }
        )
        plan = validate_plan(raw_plan, constraints)

        self.assertTrue(constraints["noInternalWalls"])
        self.assertEqual(
            (plan["minWallObstacleBlocks"], plan["maxWallObstacleBlocks"]),
            (0, 0),
        )
        self.assertEqual(plan["corridorPlacement"], "none")
        self.assertEqual(plan["corridorWidth"], 0)
        self.assertEqual(plan["corridorOrientation"], "any")
        self.assertEqual(plan["corridorRole"], "visual_only")
        self.assertEqual(plan["corridorPriority"], "preferred")

    def test_both_zero_constraints_are_applied(self):
        context = {"originalIdeaText": "无水域，也不要内部墙。"}
        constraints = resolve_zero_feature_constraints(context)
        plan = validate_plan(self.make_plan(), constraints)

        self.assertTrue(constraints["noWater"])
        self.assertTrue(constraints["noInternalWalls"])
        self.assertEqual((plan["minWaterAreas"], plan["maxWaterAreas"]), (0, 0))
        self.assertEqual(
            (plan["minWallObstacleBlocks"], plan["maxWallObstacleBlocks"]),
            (0, 0),
        )

    def test_latest_positive_request_overrides_original_no_water(self):
        context = {
            "originalIdeaText": "A level with no water.",
            "latestAdjustmentText": "Add water near one side.",
        }
        constraints = resolve_zero_feature_constraints(context)

        self.assertFalse(constraints["noWater"])

    def test_newest_history_entry_overrides_older_no_water_request(self):
        context = {
            "originalIdeaText": "A compact puzzle.",
            "adjustmentHistoryText": "No water.\n加入一块水域。",
        }
        constraints = resolve_zero_feature_constraints(context)

        self.assertFalse(constraints["noWater"])

    def test_explicit_any_wording_is_recognized(self):
        context = {
            "originalIdeaText": "Use an open board without any water or any internal walls."
        }
        constraints = resolve_zero_feature_constraints(context)

        self.assertTrue(constraints["noWater"])
        self.assertTrue(constraints["noInternalWalls"])

    def test_ai_selected_direction_does_not_trigger_constraint(self):
        context = {
            "originalIdeaText": "A compact puzzle.",
            "selectedDirectionText": "Use no water and no internal walls.",
        }
        constraints = resolve_zero_feature_constraints(context)

        self.assertFalse(constraints["noWater"])
        self.assertFalse(constraints["noInternalWalls"])

    def test_reduction_language_does_not_mean_zero(self):
        context = {"latestAdjustmentText": "减少水域，墙少一点。"}
        constraints = resolve_zero_feature_constraints(context)

        self.assertFalse(constraints["noWater"])
        self.assertFalse(constraints["noInternalWalls"])

    def test_dynamic_prompt_contains_zero_requirements(self):
        context = {"originalIdeaText": "No water and no internal walls."}
        constraints = resolve_zero_feature_constraints(context)
        messages = build_level_plan_messages(1, "none", context, constraints)
        user_prompt = messages[1]["content"]

        self.assertIn("minWaterAreas=0", user_prompt)
        self.assertIn("minWallObstacleBlocks=0", user_prompt)
        self.assertIn("corridorPlacement=none", user_prompt)

    def test_human_mode_uses_user_directed_minimum_change_prompt(self):
        context = {
            "revisionMode": "human",
            "latestAdjustmentText": "Separate the goals and keep the water unchanged.",
            "previousLevelPlan": '{"archetype":"goal_room"}',
        }
        messages = build_level_plan_messages(1, "none", context)
        user_prompt = messages[1]["content"]

        self.assertIn("Revision authority mode: HUMAN-led", user_prompt)
        self.assertIn("constraint translator", user_prompt)
        self.assertIn("minimum field changes", user_prompt)
        self.assertIn("User-directed revision:", user_prompt)

    def test_ai_mode_uses_feedback_and_diagnostic_context(self):
        context = {
            "revisionMode": "ai",
            "latestAdjustmentText": "The level was too easy.",
            "previousLevelPlan": '{"archetype":"open_workshop"}',
            "previousLevelMetrics": '{"solverSolutionSteps":18,"restartCount":0}',
        }
        messages = build_level_plan_messages(1, "none", context)
        user_prompt = messages[1]["content"]

        self.assertIn("Revision authority mode: AI-led", user_prompt)
        self.assertIn("diagnostic evidence", user_prompt)
        self.assertIn("Previous level diagnostic metrics JSON", user_prompt)
        self.assertIn("AI diagnosis:", user_prompt)

    def test_human_clarity_score_is_recomputed_from_dimensions(self):
        result = validate_human_adjustment_clarity_payload(
            {
                "problemScore": 2,
                "targetScore": 0,
                "directionScore": 0,
                "detailScore": 0,
                "totalScore": 8,
                "isClear": True,
                "reason": "Only an evaluation was supplied.",
            }
        )

        self.assertEqual(result["totalScore"], 2)
        self.assertFalse(result["isClear"])

    def test_human_clarity_prompt_contains_fixed_rubric(self):
        messages = build_human_adjustment_clarity_messages("Too easy")
        system_prompt = messages[0]["content"]

        self.assertIn("problemScore", system_prompt)
        self.assertIn("targetScore", system_prompt)
        self.assertIn("totalScore is the sum", system_prompt)
        self.assertIn("at least 4", system_prompt)
        self.assertIn("Clarification(Human)", system_prompt)


if __name__ == "__main__":
    unittest.main()
