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
    add_confirmed_decision,
    add_rejected_decision,
    empty_design_context,
    merge_chat_update,
    revision_projection,
    validate_design_context_patch,
)


class DesignContextUnitTests(unittest.TestCase):
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
        self.assertEqual(resolved["openQuestions"][0]["status"], "resolved")
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
                    self.assertEqual(
                        child_progress["unresolvedQuestions"][0]["sourceStageNumber"],
                        1,
                    )
                    self.assertNotIn("designContext", payload)
            finally:
                repository.DATABASE_PATH = original_path


if __name__ == "__main__":
    unittest.main()
