import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repository
from design_context import (
    apply_question_answer_review,
    apply_question_feedback,
    apply_hypothesis_feedback,
    add_confirmed_decision,
    add_open_question,
    add_rejected_decision,
    deduplicate_open_questions,
    empty_design_context,
    design_level_open_questions,
    extract_explicit_user_memory,
    infer_intent_topic,
    is_design_level_question,
    merge_chat_update,
    merge_intent_hypothesis,
    normalize_design_context,
    question_dedup_key,
    revision_projection,
    resolve_intent_hypothesis,
    sanitize_user_design_text,
    validate_design_context_patch,
)


class DesignContextUnitTests(unittest.TestCase):
    def test_legacy_claims_are_unverified_after_schema_upgrade(self):
        normalized = normalize_design_context({
            "schemaVersion": 5,
            "intentHypotheses": [{
                "id": "legacy-intent", "statement": "Reduce water.", "status": "confirmed",
                "semanticClaims": [{
                    "subject": "water", "attribute": "coverage", "direction": "decrease",
                    "degree": "excessive", "aspect": "unspecified", "scope": "stage",
                }],
            }],
        })
        claim = normalized["intentHypotheses"][0]["semanticClaims"][0]
        self.assertEqual(normalized["schemaVersion"], 6)
        self.assertEqual(claim["semanticSource"], "legacy_unverified")

    def test_layout_density_inclination_uses_space_distribution_topic(self):
        self.assertEqual(
            infer_intent_topic("你不喜欢当前地图布局呈现出的拥挤感。"),
            "space_distribution",
        )

    def test_question_dedup_ignores_yes_no_instruction_but_not_distinct_question(self):
        concise = "\u4f60\u662f\u5426\u4ecd\u5e0c\u671b\u6cbf\u7528\u6211\u539f\u6765\u63d0\u51fa\u7684\u529e\u6cd5\uff1f"
        instructed = concise + "\u8bf7\u56de\u7b54\u662f\u6216\u5426\u3002"
        wrapped = "\u8bf7\u53ea\u786e\u8ba4\uff1a" + concise + "\u56de\u7b54\u201c\u662f\u201d\u6216\u201c\u5426\u201d\u5373\u53ef\u3002"
        distinct = "\u4f60\u66f4\u503e\u5411\u7f29\u51cf\u6c34\u57df\uff0c\u8fd8\u662f\u6539\u53d8\u5b83\u7684\u5f62\u72b6\uff1f"
        self.assertEqual(question_dedup_key(concise), question_dedup_key(instructed))
        self.assertEqual(question_dedup_key(concise), question_dedup_key(wrapped))
        self.assertNotEqual(question_dedup_key(concise), question_dedup_key(distinct))

        context = add_open_question(
            empty_design_context(), instructed, "stage-1", "turn-1"
        )
        original_id = context["openQuestions"][0]["id"]
        context = add_open_question(
            context,
            concise,
            "stage-1",
            "turn-2",
            source_key="disagreement:challenge-1:next",
            prefer_question=True,
        )
        self.assertEqual(len(context["openQuestions"]), 1)
        self.assertEqual(context["openQuestions"][0]["id"], original_id)
        self.assertEqual(context["openQuestions"][0]["question"], concise)

    def test_question_dedup_preserves_processed_state_and_oldest_id(self):
        first = add_open_question(
            empty_design_context(), "Use the original approach?", "stage-1", "turn-1"
        )
        oldest_id = first["openQuestions"][0]["id"]
        duplicate = dict(first["openQuestions"][0])
        duplicate.update({
            "id": "later-question",
            "question": "Use the original approach? Please answer yes or no.",
            "status": "answered",
            "resolvedByTurnId": "user-turn",
            "answeredAtStageId": "stage-1",
        })
        first["openQuestions"].append(duplicate)
        repaired = deduplicate_open_questions(first)
        self.assertEqual(len(repaired["openQuestions"]), 1)
        self.assertEqual(repaired["openQuestions"][0]["id"], oldest_id)
        self.assertEqual(repaired["openQuestions"][0]["status"], "answered")
        self.assertEqual(repaired["openQuestions"][0]["resolvedByTurnId"], "user-turn")

    def test_evaluative_first_person_view_is_explicit_memory(self):
        goals, constraints = extract_explicit_user_memory(
            "\u6211\u89c9\u5f97\u4e24\u4e2a\u7bb1\u5b50\u8d77\u70b9\u6328\u5f97\u592a\u8fd1\u4e86\u3002"
        )
        self.assertEqual(goals, ["\u6211\u89c9\u5f97\u4e24\u4e2a\u7bb1\u5b50\u8d77\u70b9\u6328\u5f97\u592a\u8fd1\u4e86\u3002"])
        self.assertEqual(constraints, [])

    def test_question_review_marks_only_selected_question(self):
        context = add_open_question(
            empty_design_context(), "Which route should stay open?", "stage-1", "turn-a"
        )
        context = add_open_question(
            context, "Which box spacing should the design emphasize?", "stage-1", "turn-b"
        )
        first_id, second_id = [item["id"] for item in context["openQuestions"]]
        reviewed = apply_question_answer_review(
            context, [first_id], "stage-2", "user-turn"
        )
        self.assertEqual(reviewed["openQuestions"][0]["status"], "answered")
        self.assertEqual(reviewed["openQuestions"][0]["resolvedByTurnId"], "user-turn")
        self.assertEqual(reviewed["openQuestions"][0]["answeredAtStageId"], "stage-2")
        self.assertEqual(reviewed["openQuestions"][1]["id"], second_id)
        self.assertEqual(reviewed["openQuestions"][1]["status"], "open")

    def test_visible_output_keeps_non_design_question(self):
        context = add_open_question(
            empty_design_context(),
            "你刚才用了多少步？",
            "stage-1",
            "turn-1",
            source_kind="visible_output",
        )
        self.assertEqual(len(context["openQuestions"]), 1)
        self.assertEqual(context["openQuestions"][0]["question"], "你刚才用了多少步？")

    def test_question_ignore_and_restore_preserve_identity(self):
        context = add_open_question(
            empty_design_context(), "Which route should stay open?", "stage-1", "turn-1"
        )
        question_id = context["openQuestions"][0]["id"]
        ignored, changed = apply_question_feedback(
            context, question_id, "ignore", "stage-2", "2026-09-08T01:02:03Z"
        )
        self.assertTrue(changed)
        self.assertEqual(ignored["openQuestions"][0]["status"], "ignored")
        self.assertEqual(ignored["openQuestions"][0]["ignoredAtStageId"], "stage-2")
        reviewed = apply_question_answer_review(
            ignored, [question_id], "stage-2", "user-turn"
        )
        self.assertEqual(reviewed["openQuestions"][0]["status"], "ignored")
        repeated = add_open_question(
            ignored, "Which route should stay open?", "stage-2", "turn-2"
        )
        self.assertEqual(repeated["openQuestions"][0]["status"], "ignored")
        restored, changed = apply_question_feedback(
            repeated, question_id, "restore", "stage-2", "2026-09-08T01:03:03Z"
        )
        self.assertTrue(changed)
        self.assertEqual(restored["openQuestions"][0]["status"], "open")
        self.assertIsNone(restored["openQuestions"][0]["ignoredAtStageId"])

    def test_resolved_revision_supersedes_old_inclination_and_confirmed_goal(self):
        context, old_id = merge_intent_hypothesis(
            empty_design_context(),
            "I prefer difficulty from longer routes.",
            ["old-evidence"],
            "stage-1",
            "turn-1",
            0.5,
            displayed=True,
        )
        context, _ = resolve_intent_hypothesis(
            context,
            old_id,
            "confirm",
            evidence_id="old-feedback",
            stage_id="stage-1",
            turn_id="turn-1",
        )
        context, new_id = merge_intent_hypothesis(
            context,
            "I prefer difficulty from push-order planning.",
            ["new-evidence"],
            "stage-2",
            "turn-2",
            0.5,
            displayed=True,
        )
        context, _ = resolve_intent_hypothesis(
            context,
            new_id,
            "revise",
            candidate_text="I prefer route length only when it reinforces push-order planning.",
            supersedes_ids=[old_id],
            evidence_id="new-feedback",
            stage_id="stage-2",
            turn_id="turn-3",
        )
        statuses = {item["id"]: item["status"] for item in context["intentHypotheses"]}
        self.assertEqual(statuses[old_id], "superseded")
        self.assertEqual(statuses[new_id], "confirmed")
        active_confirmed_goals = [
            item for item in context["userGoals"]
            if item["authority"] == "confirmed" and item["status"] == "active"
        ]
        self.assertEqual(len(active_confirmed_goals), 1)
        self.assertIn("push-order planning", active_confirmed_goals[0]["goal"])

    def test_model_evidence_quote_cannot_promote_unrelated_goal_to_explicit(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={"goals": [{
                "goal": "Make the level punishingly difficult",
                "evidenceText": "I want help",
            }]},
            user_text="I want help understanding this route.",
            stage_id="stage-1",
            turn_id="turn-1",
        )

        arbitrary = next(
            item for item in context["userGoals"]
            if item["goal"] == "Make the level punishingly difficult"
        )
        self.assertEqual(arbitrary["authority"], "inferred")

    def test_route_question_is_not_an_explicit_goal(self):
        context = merge_chat_update(
            empty_design_context(),
            user_text="How do I move B1 to the target?",
            stage_id="stage-1",
            turn_id="turn-question",
        )
        self.assertEqual(context["userGoals"], [])

    def test_unrelated_explicit_goal_does_not_supersede_inferred_memory(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={"goals": [{"goal": "Maybe prioritize a winding route"}]},
            user_text="Let us inspect it.",
            stage_id="stage-1",
            turn_id="turn-1",
        )
        context = merge_chat_update(
            context,
            user_text="I want the first push to be readable.",
            stage_id="stage-1",
            turn_id="turn-2",
        )
        inferred = next(
            item for item in context["userGoals"]
            if item["authority"] == "inferred"
        )
        self.assertEqual(inferred["status"], "active")

    def test_memory_capacity_prefers_recent_active_inferences(self):
        context = empty_design_context()
        for index in range(35):
            context = merge_chat_update(
                context,
                patch={"goals": [{"goal": f"tentative goal {index}"}]},
                user_text="Observe the Stage.",
                stage_id="stage-1",
                turn_id=f"turn-{index}",
            )
        values = [item["goal"] for item in context["userGoals"]]
        self.assertIn("tentative goal 34", values)
        self.assertNotIn("tentative goal 0", values)

    def test_hypothesis_feedback_updates_same_stable_item(self):
        context, hypothesis_id = merge_intent_hypothesis(
            empty_design_context(),
            "It sounds to me like you care about route readability.",
            ["ie-1"],
            "stage-1",
            "turn-1",
            displayed=True,
        )
        context, feedback = apply_hypothesis_feedback(
            context,
            "Yes, that's what I mean.",
            "stage-1",
            "turn-2",
        )
        hypothesis = next(
            item for item in context["intentHypotheses"]
            if item["id"] == hypothesis_id
        )
        self.assertEqual(feedback, {"id": hypothesis_id, "status": "confirmed"})
        self.assertEqual(hypothesis["status"], "confirmed")
        self.assertTrue(any(
            item["authority"] == "confirmed" for item in context["userGoals"]
        ))

    def test_hidden_hypothesis_cannot_be_confirmed_by_short_reply(self):
        context, hypothesis_id = merge_intent_hypothesis(
            empty_design_context(),
            "The designer may prefer a winding route rhythm.",
            ["ie-1"],
            "stage-1",
            "turn-1",
        )
        context, feedback = apply_hypothesis_feedback(
            context,
            "Yes, that's what I mean.",
            "stage-1",
            "turn-2",
        )
        hypothesis = next(
            item for item in context["intentHypotheses"]
            if item["id"] == hypothesis_id
        )
        self.assertIsNone(feedback)
        self.assertEqual(hypothesis["status"], "tentative")

    def test_current_map_fact_is_not_persisted_as_user_direction(self):
        user_text = (
            "B1" + chr(0x5728) + "(4,4)" + chr(0xFF0C)
            + chr(0x6211) + chr(0x5E0C) + chr(0x671B)
            + chr(0x5B83) + chr(0x66F4) + chr(0x65E9)
            + chr(0x5F62) + chr(0x6210) + chr(0x7ED5) + chr(0x884C)
        )
        context = merge_chat_update(
            empty_design_context(),
            user_text=user_text,
            stage_id="stage-2",
            turn_id="turn-1",
        )

        goals = [item["goal"] for item in context["userGoals"]]
        self.assertTrue(goals)
        self.assertTrue(all("4,4" not in goal for goal in goals))
        self.assertTrue(any(chr(0x7ED5) + chr(0x884C) in goal for goal in goals))

    def test_patch_current_map_fact_is_sanitized_before_memory_merge(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={
                "goals": [{
                    "goal": "B1 is at (4,4); I want a clearer detour.",
                    "evidenceText": "B1 is at (4,4); I want a clearer detour.",
                }],
            },
            user_text="B1 is at (4,4); I want a clearer detour.",
            stage_id="stage-2",
            turn_id="turn-1",
        )

        self.assertEqual(len(context["userGoals"]), 1)
        self.assertNotIn("4,4", context["userGoals"][0]["goal"])
        self.assertIn("detour", context["userGoals"][0]["goal"])

    def test_reverse_row_claims_are_not_persisted_as_design_semantics(self):
        reverse_chinese = (
            "\u7b2c7\u884c\u7b2c5\u5217\u662fB1\uff0c"
            "\u6211\u5e0c\u671b\u5b83\u66f4\u65e9\u5f62\u6210\u7ed5\u884c\u9009\u62e9\u3002"
        )
        reverse_english = (
            "row 7, column 5 is B1; I want a clearer detour."
        )

        for source, expected in (
            (reverse_chinese, "\u7ed5\u884c"),
            (reverse_english, "detour"),
        ):
            with self.subTest(source=source):
                cleaned = sanitize_user_design_text(source)
                self.assertNotIn("7,5", cleaned)
                self.assertNotIn("B1", cleaned)
                self.assertIn(expected, cleaned)

    def test_future_coordinates_remain_design_constraints(self):
        future_chinese = "\u6211\u5e0c\u671bB1\u5728\uff08\u0034\uff0c\u0034\uff09\u65f6\u66f4\u65e9\u5f62\u6210\u7ed5\u884c\u9009\u62e9\u3002"
        future_english = "I want B1 is at (4,4) in the proposed version, with a clearer detour."

        self.assertIn("4", sanitize_user_design_text(future_chinese))
        self.assertIn("4,4", sanitize_user_design_text(future_english))

    def test_design_question_filter_keeps_design_tradeoffs_but_drops_route_mechanics(self):
        self.assertTrue(
            is_design_level_question(
                "Should the water create a visible detour, or only affect push order?"
            )
        )
        self.assertTrue(
            is_design_level_question(
                "Which first-push judgment should the player make?"
            )
        )
        self.assertFalse(
            is_design_level_question(
                "B2 goes to (8,7), then to (8,8): is that route reachable?"
            )
        )
        self.assertFalse(is_design_level_question("What do you think?"))

    def test_route_mechanics_in_patch_are_not_saved_as_open_questions(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={
                "openQuestions": [{
                    "question": "B2 goes to (8,7), then to (8,8): is that route reachable?",
                    "status": "open",
                }]
            },
            user_text="Let's inspect the route.",
            stage_id="stage-1",
            turn_id="turn-route",
        )
        self.assertEqual(context["openQuestions"], [])

    def test_legacy_route_questions_are_hidden_from_public_progress(self):
        context = empty_design_context()
        context["openQuestions"] = [{
            "question": "B2 goes to (8,7), then to (8,8): is that route reachable?",
            "status": "open",
            "sourceStageNumber": 1,
        }]

        self.assertEqual(design_level_open_questions(context), [])

    def test_assistant_open_question_is_cumulative_and_deduplicated(self):
        context = add_open_question(
            empty_design_context(),
            "Can the player read the B2 to T1 detour?",
            "stage-1",
            "turn-1",
        )
        context = add_open_question(
            context,
            "Can the player read the B2 to T1 detour?",
            "stage-1",
            "turn-2",
        )

        self.assertEqual(len(context["openQuestions"]), 1)
        self.assertEqual(
            context["openQuestions"][0]["sourceTurnId"],
            "turn-1",
        )
        self.assertEqual(
            context["openQuestions"][0]["updatedFromTurnId"],
            "turn-2",
        )

    def test_open_question_variants_update_one_cumulative_item(self):
        context = add_open_question(
            empty_design_context(),
            "Should the B1 route stay open?",
            "stage-1",
            "turn-1",
        )
        context = add_open_question(
            context,
            "Should   the B1 route stay open?？",
            "stage-1",
            "turn-2",
        )

        self.assertEqual(len(context["openQuestions"]), 1)
        self.assertEqual(
            context["openQuestions"][0]["updatedFromTurnId"],
            "turn-2",
        )

    def test_explicit_user_memory_and_inferred_patch_have_different_authority(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={"goals": [{"goal": "Prefer a readable detour", "authority": "confirmed"}]},
            user_text="I want the first push to create a readable detour.",
            stage_id="stage-1",
            turn_id="turn-1",
        )

        authorities = {item["authority"] for item in context["userGoals"]}
        self.assertIn("explicit", authorities)
        self.assertIn("inferred", authorities)
        self.assertNotIn("confirmed", authorities)

    def test_inferred_memory_does_not_appear_as_revision_hard_context(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={"goals": [{"goal": "Maybe increase planning pressure"}]},
            stage_id="stage-1",
            turn_id="turn-1",
        )
        projection = revision_projection(context)
        self.assertEqual(projection["activeGoals"], [])

    def test_chinese_user_goal_and_constraint_are_explicit(self):
        context = merge_chat_update(
            empty_design_context(),
            user_text="\u6211\u5e0c\u671b\u589e\u52a0\u56de\u7a0b\u5bb9\u9519\uff0c\u4f46\u8981\u4fdd\u6301\u516c\u5e73\u3002",
            stage_id="stage-1",
            turn_id="turn-cn",
        )
        self.assertTrue(any(item["authority"] == "explicit" for item in context["userGoals"]))
        self.assertTrue(any(item["authority"] == "explicit" for item in context["designConstraints"]))
        self.assertEqual(context["confirmedDecisions"], [])

    def test_directional_phrasing_is_recorded_as_expressed_not_confirmed(self):
        context = merge_chat_update(
            empty_design_context(),
            user_text="我倾向于让右侧路线更清晰，请保持当前的箱子数量。",
            stage_id="stage-2",
            turn_id="turn-direction",
        )

        self.assertTrue(any(
            item["authority"] == "explicit" and item["status"] == "active"
            for item in context["userGoals"] + context["designConstraints"]
        ))
        self.assertEqual(context["confirmedDecisions"], [])

    def test_correction_keeps_old_provenance_and_supersedes_inference(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={"goals": [{"goal": "Maybe make the route longer"}]},
            user_text="Maybe make the route longer.",
            stage_id="stage-1",
            turn_id="turn-1",
        )
        context = merge_chat_update(
            context,
            user_text="Actually, I want the route clearer instead.",
            stage_id="stage-1",
            turn_id="turn-2",
        )
        self.assertTrue(
            any(item["status"] == "superseded" for item in context["userGoals"])
        )
        self.assertTrue(
            any(item["authority"] == "explicit" and item["status"] == "active"
                for item in context["userGoals"])
        )

    def test_formal_accept_and_reject_are_separate(self):
        context = add_confirmed_decision(
            empty_design_context(),
            "Open the right route",
            "The designer accepted the proposal.",
            "stage-2",
            "turn-2",
            "proposal-1",
        )
        context = add_rejected_decision(
            context,
            "Move the target down",
            "It conflicts with fairness.",
            "stage-2",
            "turn-3",
            "proposal-2",
        )
        self.assertEqual(len(context["confirmedDecisions"]), 1)
        self.assertEqual(len(context["rejectedDecisions"]), 1)

    def test_patch_validator_downgrades_model_authority(self):
        patch = validate_design_context_patch({
            "goals": [{"goal": "Stable route", "authority": "confirmed"}],
            "decisions": [{"decision": "Open route", "reason": "test"}],
        })
        self.assertEqual(patch["goals"][0]["authority"], "inferred")
        with self.assertRaises(ValueError):
            validate_design_context_patch({"goals": [{"bad": "value"}]})

    def test_open_question_requires_user_evidence_to_resolve(self):
        question = "Can the player understand the B2 to T1 detour without a hint?"
        context = merge_chat_update(
            empty_design_context(),
            patch={"openQuestions": [{"question": question, "status": "open"}]},
            user_text="We should still test the B2 to T1 detour.",
            stage_id="stage-1",
            turn_id="turn-1",
        )
        self.assertEqual(context["openQuestions"][0]["status"], "open")

        unresolved = merge_chat_update(
            context,
            patch={
                "openQuestions": [{
                    "question": question,
                    "status": "resolved",
                    "evidenceText": "The detour is clear now",
                }]
            },
            user_text="I am not sure yet.",
            stage_id="stage-1",
            turn_id="turn-2",
        )
        self.assertEqual(unresolved["openQuestions"][0]["status"], "open")

        resolved = merge_chat_update(
            context,
            patch={
                "openQuestions": [{
                    "question": question,
                    "status": "resolved",
                    "evidenceText": "The detour is clear now",
                }]
            },
            user_text="The detour is clear now, so we can move on.",
            stage_id="stage-1",
            turn_id="turn-3",
        )
        self.assertEqual(resolved["openQuestions"][0]["status"], "answered")
        self.assertEqual(resolved["openQuestions"][0]["resolvedByTurnId"], "turn-3")

    def test_model_cannot_create_confirmed_decision_in_chat_patch(self):
        context = merge_chat_update(
            empty_design_context(),
            patch={
                "decisions": [{
                    "decision": "Keep the direct opening",
                    "reason": "The assistant recommends it.",
                }]
            },
            user_text="I am considering the opening.",
            stage_id="stage-1",
            turn_id="turn-1",
        )
        self.assertEqual(context["confirmedDecisions"], [])


class DesignContextRepositoryTests(unittest.TestCase):
    def test_startup_repair_merges_duplicate_progress_questions_idempotently(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "duplicate-questions.sqlite3"
            try:
                repository.initialize_database()
                session_id = uuid.uuid4().hex
                version_id = uuid.uuid4().hex
                now = "2026-09-10T00:00:00Z"
                canonical = "\u4f60\u662f\u5426\u4ecd\u5e0c\u671b\u6cbf\u7528\u6211\u539f\u6765\u63d0\u51fa\u7684\u529e\u6cd5\uff1f"
                context = empty_design_context()
                context["openQuestions"] = [
                    {
                        "id": "oldest-question",
                        "question": canonical + "\u8bf7\u56de\u7b54\u662f\u6216\u5426\u3002",
                        "status": "open",
                        "sourceKind": "visible_output",
                        "sourceStageId": version_id,
                        "sourceTurnId": "assistant-turn",
                    },
                    {
                        "id": "answered-duplicate",
                        "question": canonical,
                        "status": "open",
                        "sourceKind": "visible_output",
                        "sourceStageId": version_id,
                        "sourceTurnId": "assistant-turn",
                    },
                ]
                with repository.connect(immediate=True) as database:
                    database.execute(
                        """
                        INSERT INTO design_sessions(
                            id, creation_key, access_hash, integration_hash, bootstrap_hash,
                            initial_draft_method, language, status, current_version_id,
                            created_at, updated_at
                        ) VALUES (?, ?, 'a', 'b', 'c', 'partial_completion', 'zh-CN',
                                  'active', ?, ?, ?)
                        """,
                        (session_id, uuid.uuid4().hex, version_id, now, now),
                    )
                    database.execute(
                        """
                        INSERT INTO level_versions(
                            id, session_id, stage_number, source, rows_json, summary,
                            diff_json, validation_json, design_context_json,
                            idempotency_key, created_at
                        ) VALUES (?, ?, 1, 'initial', ?, '', '[]', '{}', ?, ?, ?)
                        """,
                        (
                            version_id,
                            session_id,
                            json.dumps(["############"] * 10),
                            json.dumps(context),
                            version_id,
                            now,
                        ),
                    )
                    database.execute(
                        """
                        INSERT INTO conversation_turns(
                            id, session_id, sequence_number, role, content, language,
                            version_id, guidance_json, created_at
                        ) VALUES ('assistant-turn', ?, 1, 'assistant', ?, 'zh-CN', ?, ?, ?)
                        """,
                        (
                            session_id,
                            canonical,
                            version_id,
                            json.dumps({"disagreement": {"nextQuestion": canonical}}),
                            now,
                        ),
                    )
                    database.execute(
                        """
                        INSERT INTO conversation_turns(
                            id, session_id, sequence_number, role, content, language,
                            version_id, request_id, created_at
                        ) VALUES ('user-turn', ?, 2, 'user', ?, 'zh-CN', ?,
                                  'challenge-choice-message', ?)
                        """,
                        (session_id, "\u5426", version_id, now),
                    )
                    repository.record_event(
                        database,
                        session_id,
                        "disagreement_started",
                        {
                            "versionId": version_id,
                            "disagreement": {
                                "status": "active",
                                "subject": "ai_revision_challenge",
                                "phase": "choice_pending",
                                "nextQuestion": canonical,
                            },
                        },
                        now,
                    )
                    repository.record_event(
                        database,
                        session_id,
                        "challenge_continued",
                        {
                            "messageKey": "challenge-choice-message",
                            "action": "continue_challenge",
                            "baseVersionId": version_id,
                        },
                        now,
                    )
                    self.assertEqual(repository.repair_duplicate_progress_questions(database), 1)
                    self.assertEqual(repository.repair_duplicate_progress_questions(database), 0)
                    self.assertEqual(
                        repository.repair_answered_challenge_choice_questions(database),
                        1,
                    )
                    self.assertEqual(
                        repository.repair_answered_challenge_choice_questions(database),
                        0,
                    )
                    repaired = repository.load_design_context(
                        database, session_id, version_id
                    )
                self.assertEqual(len(repaired["openQuestions"]), 1)
                self.assertEqual(repaired["openQuestions"][0]["id"], "oldest-question")
                self.assertEqual(repaired["openQuestions"][0]["question"], canonical)
                self.assertEqual(repaired["openQuestions"][0]["status"], "answered")
            finally:
                repository.DATABASE_PATH = original_path

    def test_repair_clears_audited_stale_disagreement_from_descendants(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "stale-disagreement.sqlite3"
            try:
                repository.initialize_database()
                session_id = uuid.uuid4().hex
                parent_id = uuid.uuid4().hex
                child_id = uuid.uuid4().hex
                message_key = "continued-choice-message"
                now = "2026-09-09T00:00:00Z"
                disagreement = {
                    "status": "active",
                    "subject": "ai_revision_challenge",
                    "userPosition": "The proposed scope is too small.",
                    "aiPosition": "Keep the local change.",
                    "coreDisagreement": "Whether one changed cell is enough.",
                    "nextQuestion": "Should the original approach remain?",
                    "resolution": None,
                    "phase": "choice_pending",
                    "displayCard": True,
                    "proposalSummary": "Change one water tile.",
                    "acceptedReason": "One changed cell is not enough.",
                }
                context = empty_design_context()
                context["activeDisagreement"] = disagreement
                child_context = json.loads(json.dumps(context))
                child_context["activeDisagreement"].update({
                    "nextQuestion": "Please answer only yes or no.",
                    "displayCard": False,
                })
                with repository.connect(immediate=True) as database:
                    database.execute(
                        """
                        INSERT INTO design_sessions(
                            id, creation_key, access_hash, integration_hash, bootstrap_hash,
                            initial_draft_method, language, status, current_version_id,
                            created_at, updated_at
                        ) VALUES (?, ?, 'a', 'b', 'c', 'partial_completion', 'en',
                                  'active', ?, ?, ?)
                        """,
                        (session_id, uuid.uuid4().hex, child_id, now, now),
                    )
                    rows = json.dumps(["############"] * 10)
                    for version_id, stage_number, parent_version_id in (
                        (parent_id, 1, None),
                        (child_id, 2, parent_id),
                    ):
                        database.execute(
                            """
                            INSERT INTO level_versions(
                                id, session_id, stage_number, parent_version_id, source,
                                rows_json, summary, diff_json, validation_json,
                                design_context_json, idempotency_key, created_at
                            ) VALUES (?, ?, ?, ?, 'llm_accepted', ?, '', '[]', '{}', ?, ?, ?)
                            """,
                            (
                                version_id,
                                session_id,
                                stage_number,
                                parent_version_id,
                                rows,
                                json.dumps(
                                    context if version_id == parent_id else child_context
                                ),
                                version_id,
                                now,
                            ),
                        )
                    repository.record_event(
                        database,
                        session_id,
                        "disagreement_started",
                        {
                            "versionId": parent_id,
                            "disagreement": disagreement,
                        },
                        now,
                    )
                    repository.record_event(
                        database,
                        session_id,
                        "challenge_continued",
                        {
                            "messageKey": message_key,
                            "action": "continue_challenge",
                            "baseVersionId": parent_id,
                        },
                        now,
                    )
                    repository.record_event(
                        database,
                        session_id,
                        "disagreement_updated",
                        {
                            "versionId": child_id,
                            "disagreement": child_context["activeDisagreement"],
                        },
                        now,
                    )
                    repository.record_event(
                        database,
                        session_id,
                        "revision_workflow_v2_evaluated",
                        {
                            "messageKey": message_key,
                            "candidateAccepted": True,
                            "workflow": {"status": "authorized"},
                        },
                        now,
                    )
                    self.assertEqual(
                        repository.repair_resolved_challenge_disagreements(database),
                        2,
                    )
                    self.assertEqual(
                        repository.repair_resolved_challenge_disagreements(database),
                        0,
                    )
                    repaired_parent = repository.load_design_context(
                        database, session_id, parent_id
                    )
                    repaired_child = repository.load_design_context(
                        database, session_id, child_id
                    )
                    event_count = database.execute(
                        """
                        SELECT COUNT(*) FROM audit_events
                        WHERE session_id = ?
                          AND event_type = 'stale_disagreement_repaired'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                self.assertIsNone(repaired_parent["activeDisagreement"])
                self.assertIsNone(repaired_child["activeDisagreement"])
                self.assertEqual(event_count, 2)
            finally:
                repository.DATABASE_PATH = original_path

    def test_schema_one_snapshot_migrates_without_model_inference(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "schema-v1.sqlite3"
            try:
                repository.initialize_database()
                session_id = uuid.uuid4().hex
                version_id = uuid.uuid4().hex
                now = "2026-09-01T00:00:00Z"
                legacy = empty_design_context()
                legacy["schemaVersion"] = 1
                legacy["userGoals"] = [{
                    "goal": "Maybe preserve a readable route",
                    "authority": "inferred",
                    "status": "active",
                    "sourceStageId": version_id,
                    "sourceTurnId": "turn-legacy",
                    "confidence": 0.8,
                }]
                legacy.pop("intentHypotheses", None)
                legacy.pop("processedEvidenceIds", None)
                with repository.connect(immediate=True) as database:
                    database.execute(
                        """
                        INSERT INTO design_sessions(
                            id, creation_key, access_hash, integration_hash, bootstrap_hash,
                            initial_draft_method, language, status, current_version_id,
                            created_at, updated_at
                        ) VALUES (?, ?, 'a', 'b', 'c', 'partial_completion', 'en',
                                  'active', ?, ?, ?)
                        """,
                        (session_id, uuid.uuid4().hex, version_id, now, now),
                    )
                    database.execute(
                        """
                        INSERT INTO level_versions(
                            id, session_id, stage_number, parent_version_id, source,
                            rows_json, summary, diff_json, validation_json,
                            design_context_json, idempotency_key, created_at
                        ) VALUES (?, ?, 1, NULL, 'initial', ?, '', '[]', '{}', ?, ?, ?)
                        """,
                        (
                            version_id,
                            session_id,
                            json.dumps(["############"] * 10),
                            json.dumps(legacy),
                            version_id,
                            now,
                        ),
                    )
                    self.assertEqual(repository.backfill_design_contexts(database), 1)
                    migrated = repository.load_design_context(
                        database, session_id, version_id
                    )
                    events = database.execute(
                        """
                        SELECT COUNT(*) FROM audit_events
                        WHERE session_id = ? AND event_type = 'design_context_migrated'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                    self.assertEqual(migrated["schemaVersion"], 6)
                self.assertEqual(events, 1)
                self.assertEqual(
                    migrated["intentHypotheses"][0]["status"],
                    "legacy_unverified",
                )
                self.assertLessEqual(
                    migrated["intentHypotheses"][0]["confidence"], 0.5
                )
            finally:
                repository.DATABASE_PATH = original_path

    def test_database_migrates_design_context_column_and_backfill_is_idempotent(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "context.sqlite3"
            try:
                legacy = sqlite3.connect(repository.DATABASE_PATH)
                legacy.executescript(
                    repository.SCHEMA.replace("    design_context_json TEXT,\n", "")
                )
                legacy.close()
                repository.initialize_database()
                with repository.connect(immediate=True) as database:
                    columns = {
                        row[1] for row in database.execute(
                            "PRAGMA table_info(level_versions)"
                        ).fetchall()
                    }
                    self.assertIn("design_context_json", columns)
                    self.assertEqual(repository.backfill_design_contexts(database), 0)
            finally:
                repository.DATABASE_PATH = original_path

    def test_backfill_copies_parent_snapshot_to_child_stage(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "inheritance.sqlite3"
            try:
                repository.initialize_database()
                session_id = uuid.uuid4().hex
                parent_id = uuid.uuid4().hex
                child_id = uuid.uuid4().hex
                now = "2026-09-01T00:00:00Z"
                with repository.connect(immediate=True) as database:
                    database.execute(
                        """
                        INSERT INTO design_sessions(
                            id, creation_key, access_hash, integration_hash, bootstrap_hash,
                            initial_draft_method, language, status, created_at, updated_at
                        ) VALUES (?, ?, 'a', 'b', 'c', 'partial_completion', 'en', 'active', ?, ?)
                        """,
                        (session_id, uuid.uuid4().hex, now, now),
                    )
                    rows = json.dumps(["############"] * 10)
                    for stage_id, stage_number, parent in (
                        (parent_id, 1, None), (child_id, 2, parent_id)
                    ):
                        database.execute(
                            """
                            INSERT INTO level_versions(
                                id, session_id, stage_number, parent_version_id, source,
                                rows_json, summary, diff_json, validation_json,
                                design_context_json, idempotency_key, created_at
                            ) VALUES (?, ?, ?, ?, 'human_edit', ?, '', '[]', '{}', ?, ?, ?)
                            """,
                            (
                                stage_id,
                                session_id,
                                stage_number,
                                parent,
                                rows,
                                None if parent else json.dumps(empty_design_context()),
                                stage_id,
                                now,
                            ),
                        )
                    parent_context = merge_chat_update(
                        empty_design_context(),
                        user_text="I want the first push to remain readable.",
                        stage_id=parent_id,
                        turn_id="turn-parent",
                    )
                    parent_context = add_confirmed_decision(
                        parent_context,
                        "Keep the direct opening",
                        "The designer accepted the opening direction.",
                        parent_id,
                        "turn-parent-decision",
                    )
                    parent_context = merge_chat_update(
                        parent_context,
                        patch={"openQuestions": [{
                            "question": "Can the player read the B2 to T1 detour?",
                            "status": "open",
                        }]},
                        user_text="We still need to test whether the player can read the B2 to T1 detour.",
                        stage_id=parent_id,
                        turn_id="turn-parent-question",
                    )
                    repository.save_design_context(database, parent_id, parent_context)
                    self.assertEqual(repository.backfill_design_contexts(database), 1)
                    child_context = repository.load_design_context(
                        database, session_id, child_id
                    )
                    self.assertEqual(child_context["userGoals"], parent_context["userGoals"])
                    payload = repository.serialize_session(database, session_id)
                    child_progress = next(
                        item for item in payload["progressContexts"]
                        if item["versionId"] == child_id
                    )
                    self.assertEqual(
                        child_progress["confirmedDecisions"][0]["sourceStageNumber"],
                        1,
                    )
                    self.assertEqual(len(child_progress["expressedDirections"]), 2)
                    self.assertEqual(
                        {item["kind"] for item in child_progress["expressedDirections"]},
                        {"goal", "constraint"},
                    )
                    for direction in child_progress["expressedDirections"]:
                        self.assertEqual(
                            direction["text"],
                            "I want the first push to remain readable.",
                        )
                        self.assertEqual(direction["sourceStageNumber"], 1)
                        self.assertEqual(direction["label"], "explicit")
                    self.assertEqual(
                        child_progress["unresolvedQuestions"][0]["sourceStageNumber"],
                        1,
                    )
                    self.assertNotIn("designContext", payload)
            finally:
                repository.DATABASE_PATH = original_path

    def test_entity_binding_column_backfill_is_idempotent_and_keeps_internal_data_private(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "entity-bindings.sqlite3"
            try:
                legacy = sqlite3.connect(repository.DATABASE_PATH)
                legacy.executescript(
                    repository.SCHEMA.replace("    entity_bindings_json TEXT,\n", "")
                )
                session_id = uuid.uuid4().hex
                version_id = uuid.uuid4().hex
                now = "2026-09-01T00:00:00Z"
                rows = [
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
                legacy.execute(
                    """
                    INSERT INTO design_sessions(
                        id, creation_key, access_hash, integration_hash, bootstrap_hash,
                        initial_draft_method, language, status, current_version_id, created_at, updated_at
                    ) VALUES (?, ?, 'a', 'b', 'c', 'partial_completion', 'en', 'active', ?, ?, ?)
                    """,
                    (session_id, uuid.uuid4().hex, version_id, now, now),
                )
                legacy.execute(
                    """
                    INSERT INTO level_versions(
                        id, session_id, stage_number, parent_version_id, source,
                        rows_json, summary, diff_json, validation_json,
                        design_context_json, idempotency_key, created_at
                    ) VALUES (?, ?, 1, NULL, 'initial', ?, '', '[]', '{}', ?, ?, ?)
                    """,
                    (
                        version_id,
                        session_id,
                        json.dumps(rows),
                        json.dumps(empty_design_context()),
                        version_id,
                        now,
                    ),
                )
                legacy.commit()
                legacy.close()

                repository.initialize_database()
                with repository.connect(immediate=True) as database:
                    columns = {
                        row[1] for row in database.execute(
                            "PRAGMA table_info(level_versions)"
                        ).fetchall()
                    }
                    self.assertIn("entity_bindings_json", columns)
                    stored = database.execute(
                        "SELECT entity_bindings_json FROM level_versions WHERE id = ?",
                        (version_id,),
                    ).fetchone()[0]
                    self.assertEqual(json.loads(stored)["identityStatus"], "exact")
                    event_count = database.execute(
                        """
                        SELECT COUNT(*) FROM audit_events
                        WHERE session_id = ? AND event_type = 'entity_binding_backfilled'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                    self.assertEqual(event_count, 1)
                    repository.backfill_entity_bindings(database)
                    repeated_count = database.execute(
                        """
                        SELECT COUNT(*) FROM audit_events
                        WHERE session_id = ? AND event_type = 'entity_binding_backfilled'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                    self.assertEqual(repeated_count, event_count)
                    payload = repository.serialize_session(database, session_id)
                    self.assertNotIn("entityBindings", payload)
                    self.assertNotIn("entity_bindings_json", json.dumps(payload))
            finally:
                repository.DATABASE_PATH = original_path

    def test_intent_semantic_backfill_is_idempotent_and_preserves_confirmed_text(self):
        original_path = repository.DATABASE_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repository.DATABASE_PATH = Path(directory) / "intent-semantics.sqlite3"
            try:
                repository.initialize_database()
                session_id = uuid.uuid4().hex
                version_id = uuid.uuid4().hex
                user_turn_id = uuid.uuid4().hex
                assistant_turn_id = uuid.uuid4().hex
                original_statement = "听起来你想让水成为路线边界。"
                context = empty_design_context()
                context["intentHypotheses"] = [{
                    "id": "legacy-water-intent",
                    "topicKey": "water_function",
                    "statement": original_statement,
                    "status": "confirmed",
                    "confidence": 1.0,
                    "sourceStageId": version_id,
                    "sourceTurnId": assistant_turn_id,
                    "displayed": True,
                }]
                now = "2026-09-11T00:00:00Z"
                with repository.connect(immediate=True) as database:
                    database.execute(
                        """INSERT INTO design_sessions(
                            id, creation_key, access_hash, integration_hash, bootstrap_hash,
                            initial_draft_method, language, status, current_version_id,
                            created_at, updated_at
                        ) VALUES (?, ?, 'a', 'b', 'c', 'partial_completion', 'zh-CN', 'active', ?, ?, ?)""",
                        (session_id, uuid.uuid4().hex, version_id, now, now),
                    )
                    database.execute(
                        """INSERT INTO level_versions(
                            id, session_id, stage_number, parent_version_id, source, rows_json,
                            summary, diff_json, validation_json, design_context_json,
                            idempotency_key, created_at
                        ) VALUES (?, ?, 1, NULL, 'initial', ?, '', '[]', '{}', ?, ?, ?)""",
                        (version_id, session_id, json.dumps(["############"] * 10), json.dumps(context, ensure_ascii=False), version_id, now),
                    )
                    for turn_id, sequence, role, content in (
                        (user_turn_id, 1, "user", "我觉得水域太多了"),
                        (assistant_turn_id, 2, "assistant", "地图分析正文"),
                    ):
                        database.execute(
                            """INSERT INTO conversation_turns(
                                id, session_id, sequence_number, role, content, language,
                                version_id, created_at, guidance_json
                            ) VALUES (?, ?, ?, ?, ?, 'zh-CN', ?, ?, '{}')""",
                            (turn_id, session_id, sequence, role, content, version_id, now),
                        )
                    self.assertEqual(repository.backfill_intent_semantics(database), 1)
                    self.assertEqual(repository.backfill_intent_semantics(database), 0)
                    repaired = repository.load_design_context(database, session_id, version_id)
                    hypothesis = repaired["intentHypotheses"][0]
                    self.assertEqual(hypothesis["statement"], original_statement)
                    self.assertEqual(hypothesis["status"], "confirmed")
                    self.assertEqual(hypothesis["semanticClaims"][0]["direction"], "decrease")
                    self.assertEqual(hypothesis["semanticClaims"][0]["sourceUserTurnId"], user_turn_id)
            finally:
                repository.DATABASE_PATH = original_path


if __name__ == "__main__":
    unittest.main()
