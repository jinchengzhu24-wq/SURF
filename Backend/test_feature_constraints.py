import json
import unittest

from Backend.app import (
    DEFAULT_PLAN,
    apply_selected_ha_plan,
    validate_human_adjustment_clarity_payload,
    validate_ha_revision_plan_options,
    validate_plan,
)
from Backend.prompt import (
    build_ha_revision_plan_messages,
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

    def test_ha_revision_options_validate_distinct_hidden_deltas(self):
        previous_plan = self.make_plan()
        payload = {
            "options": [
                {
                    "id": "A",
                    "title": "More pushes",
                    "description": "Increase push pressure while preserving the existing layout.",
                    "promptText": {
                        "changes": {"minPushes": 12},
                        "preserveUnlisted": True,
                    },
                },
                {
                    "id": "B",
                    "title": "Split targets",
                    "description": "Separate the targets while retaining the other obstacles.",
                    "promptText": {
                        "changes": {"targetLayout": "clustered"},
                        "preserveUnlisted": True,
                    },
                },
                {
                    "id": "C",
                    "title": "Open structure",
                    "description": "Open the main structure while retaining the target layout.",
                    "promptText": {
                        "changes": {"archetype": "open_workshop"},
                        "preserveUnlisted": True,
                    },
                },
            ]
        }

        options = validate_ha_revision_plan_options(
            payload,
            previous_plan,
            "Make the revision more deliberate.",
        )

        self.assertEqual(len(options), 3)
        self.assertIn('"preserveUnlisted":true', options[0]["promptText"])

    def test_ha_revision_contract_rejects_unknown_field(self):
        previous_plan = self.make_plan()
        payload = {
            "options": [
                {
                    "id": option_id,
                    "title": "Option " + option_id,
                    "description": "A concrete supported revision.",
                    "promptText": {
                        "changes": {"teleporterCount": index + 1},
                        "preserveUnlisted": True,
                    },
                }
                for index, option_id in enumerate(("A", "B", "C"))
            ]
        }

        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            validate_ha_revision_plan_options(
                payload,
                previous_plan,
                "Add teleporters.",
            )

    def test_selected_ha_delta_preserves_unlisted_previous_fields(self):
        previous_plan = self.make_plan()
        selected_option = {
            "id": "A",
            "title": "Split targets",
            "description": "Separate the two goals and retain the other structure.",
            "promptText": (
                '{"changes":{"targetLayout":"clustered"},'
                '"preserveUnlisted":true}'
            ),
        }
        generated_plan = self.make_plan()
        generated_plan["archetype"] = "open_workshop"
        context = {
            "revisionMode": "ha",
            "latestAdjustmentText": "Change the goal layout.",
            "previousLevelPlan": json.dumps(previous_plan),
            "selectedHAPlan": json.dumps(selected_option),
        }

        result = apply_selected_ha_plan(
            generated_plan,
            context,
            {"noWater": False, "noInternalWalls": False},
        )

        self.assertEqual(result["targetLayout"], "clustered")
        self.assertEqual(result["archetype"], previous_plan["archetype"])
        self.assertTrue(result["designNote"].startswith("Human-AI revision:"))

    def test_ha_revision_prompt_uses_blueprint_and_regeneration_context(self):
        context = {
            "adjustmentText": "Separate the goals.",
            "previousLevelPlan": self.make_plan(),
            "corridorValidation": {"verified": True},
            "regenerationAttempt": 2,
            "previousOptions": [
                {
                    "id": "A",
                    "title": "Old option",
                    "description": "Previously shown.",
                    "promptText": "{}",
                }
            ],
        }
        messages = build_ha_revision_plan_messages(context)
        prompt = messages[1]["content"]

        self.assertIn("Separate the goals", prompt)
        self.assertIn("Previous LevelDesignPlan JSON", prompt)
        self.assertIn("Corridor verification JSON", prompt)
        self.assertIn("Regeneration attempt: 2", prompt)
        self.assertIn("preserveUnlisted", prompt)

    def test_ha_level_prompt_describes_collaborative_authority(self):
        context = {
            "revisionMode": "ha",
            "latestAdjustmentText": "Separate the goals.",
            "previousLevelPlan": '{"targetLayout":"split_pair"}',
            "selectedHAPlan": '{"title":"Goal split","promptText":"{}"}',
        }
        messages = build_level_plan_messages(1, "none", context)
        prompt = messages[1]["content"]

        self.assertIn("HUMAN-AI collaborative", prompt)
        self.assertIn("selected HA revision plan", prompt)
        self.assertIn("Human-AI revision:", prompt)


if __name__ == "__main__":
    unittest.main()
