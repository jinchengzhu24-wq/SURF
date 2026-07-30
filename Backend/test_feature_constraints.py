import json
import unittest

from Backend.app import (
    DEFAULT_PLAN,
    apply_selected_ha_plan,
    build_contextual_expansion_fallback_options,
    parse_ha_revision_contract,
    validate_human_adjustment_clarity_payload,
    validate_ha_revision_plan_edit,
    validate_ha_revision_plan_options,
    validate_plan,
)
from Backend.prompt import (
    build_creative_idea_expansion_messages,
    build_ha_revision_plan_edit_messages,
    build_ha_revision_plan_messages,
    build_human_adjustment_clarity_messages,
    build_level_plan_messages,
    resolve_zero_feature_constraints,
)


class FeatureConstraintTests(unittest.TestCase):
    def make_plan(self):
        return dict(DEFAULT_PLAN)

    def test_ha_contract_repairs_wall_minimum_copied_from_maximum(self):
        contract = parse_ha_revision_contract(
            {
                "changes": {
                    "minWallObstacleBlocks": 3,
                    "maxWallObstacleBlocks": 3,
                },
                "preserveUnlisted": True,
            }
        )

        self.assertEqual(contract["changes"]["minWallObstacleBlocks"], 2)
        self.assertEqual(contract["changes"]["maxWallObstacleBlocks"], 3)

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

    def test_competitive_prompt_requires_dispersed_wall_groups(self):
        messages = build_level_plan_messages(
            1,
            "none",
            {"competitionMode": "competitive"},
        )
        user_prompt = messages[1]["content"]

        self.assertIn("at most two tiles", user_prompt)
        self.assertIn("corridorPlacement=none", user_prompt)

    def test_supportive_prompt_requires_one_connected_wall_group(self):
        messages = build_level_plan_messages(
            1,
            "none",
            {"competitionMode": "supportive"},
        )
        user_prompt = messages[1]["content"]

        self.assertIn("one orthogonally connected group", user_prompt)
        self.assertIn("corridorPlacement=none", user_prompt)

    def test_competition_mode_removes_incompatible_divider_corridor(self):
        plan = self.make_plan()
        plan.update(
            {
                "corridorPlacement": "center",
                "corridorWidth": 1,
                "corridorOrientation": "vertical",
                "corridorRole": "player_route",
                "corridorPriority": "required",
            }
        )

        result = apply_selected_ha_plan(
            plan,
            {"competitionMode": "supportive"},
            {"noWater": False, "noInternalWalls": False},
        )

        self.assertEqual(result["corridorPlacement"], "none")
        self.assertEqual(result["corridorWidth"], 0)
        self.assertEqual(result["corridorOrientation"], "any")
        self.assertEqual(result["corridorRole"], "visual_only")
        self.assertEqual(result["corridorPriority"], "preferred")

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

    def test_all_llm_prompts_require_english_ascii_output(self):
        plan = self.make_plan()
        message_sets = [
            build_creative_idea_expansion_messages({"ideaText": "中央迷宫"}),
            build_human_adjustment_clarity_messages("减少墙体"),
            build_ha_revision_plan_messages(
                {
                    "adjustmentText": "增加推箱次数",
                    "previousLevelPlan": plan,
                }
            ),
            build_ha_revision_plan_edit_messages(
                {
                    "adjustmentText": "增加推箱次数",
                    "editedDescription": "保留水域并分开目标",
                    "previousLevelPlan": plan,
                    "originalOption": {
                        "id": "A",
                        "title": "Push Pressure",
                        "description": "Increase push pressure.",
                        "promptText": (
                            '{"changes":{"minPushes":12},'
                            '"preserveUnlisted":true}'
                        ),
                    },
                }
            ),
            build_level_plan_messages(
                1,
                "none",
                {"ideaText": "设计一个水上迷宫"},
            ),
        ]

        for messages in message_sets:
            with self.subTest(system_prompt=messages[0]["content"][:60]):
                self.assertIn(
                    "every JSON string value in English using ASCII characters only",
                    messages[0]["content"],
                )
                self.assertIn(
                    "Never echo, quote, or preserve non-English user text",
                    messages[0]["content"],
                )

    def test_chinese_fallback_inputs_produce_ascii_english_options(self):
        ideas = [
            "紧凑水域障碍",
            "设计一个迷宫和绕路",
            "困难紧凑的关卡",
            "一个有趣的关卡",
        ]

        for idea in ideas:
            with self.subTest(idea=idea):
                options = build_contextual_expansion_fallback_options(idea)
                self.assertEqual(len(options), 3)

                for option in options:
                    for field in ("id", "title", "description", "promptText"):
                        value = option[field]
                        self.assertTrue(value)
                        self.assertTrue(
                            value.isascii(),
                            f"{field} was not ASCII for input {idea!r}: {value!r}",
                        )

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

    def test_ha_revision_plan_edit_preserves_identity_and_rebuilds_contract(self):
        previous_plan = self.make_plan()
        original_option = {
            "id": "A",
            "title": "More pushes",
            "description": "Increase push pressure.",
            "promptText": {
                "changes": {"minPushes": 12},
                "preserveUnlisted": True,
            },
        }
        payload = {
            "description": "Cluster the goals while preserving the other structure.",
            "promptText": {
                "changes": {"targetLayout": "clustered"},
                "preserveUnlisted": True,
            },
        }

        option = validate_ha_revision_plan_edit(
            payload,
            original_option,
            previous_plan,
            "Increase push pressure.",
            "Cluster the goals.",
        )

        self.assertEqual(option["id"], "A")
        self.assertEqual(option["title"], "More pushes")
        self.assertEqual(option["description"], payload["description"])
        self.assertIn('"targetLayout":"clustered"', option["promptText"])

    def test_ha_revision_plan_edit_rejects_unchanged_contract(self):
        previous_plan = self.make_plan()
        original_option = {
            "id": "A",
            "title": "More pushes",
            "description": "Increase push pressure.",
            "promptText": {
                "changes": {"minPushes": 12},
                "preserveUnlisted": True,
            },
        }
        payload = {
            "description": "Use different wording for the same push pressure.",
            "promptText": original_option["promptText"],
        }

        with self.assertRaisesRegex(ValueError, "different change contract"):
            validate_ha_revision_plan_edit(
                payload,
                original_option,
                previous_plan,
                "Increase push pressure.",
                payload["description"],
            )

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

    def test_ha_revision_plan_edit_prompt_contains_player_edit_and_contract(self):
        context = {
            "adjustmentText": "Increase push pressure.",
            "editedDescription": "Cluster the goals and keep the water unchanged.",
            "previousLevelPlan": self.make_plan(),
            "corridorValidation": {"verified": True},
            "originalOption": {
                "id": "A",
                "title": "More pushes",
                "description": "Increase push pressure.",
                "promptText": '{"changes":{"minPushes":12},"preserveUnlisted":true}',
            },
        }
        messages = build_ha_revision_plan_edit_messages(context)
        prompt = messages[1]["content"]

        self.assertIn(context["editedDescription"], prompt)
        self.assertIn("Previous LevelDesignPlan JSON", prompt)
        self.assertIn("preserveUnlisted", prompt)
        self.assertIn("must differ", prompt)

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
