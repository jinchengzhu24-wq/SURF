import os
import sys
import unittest
from unittest.mock import patch


BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from revision_workflow import (  # noqa: E402
    SemanticConstraintError,
    build_revision_workflow,
    compile_semantic_constraints,
    modification_v2_mode,
    validate_semantic_constraints,
)


BASE = [
    "############",
    "#..........#",
    "#.......@@.#",
    "#..........#",
    "#...p......#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


def changed(*updates):
    rows = [list(row) for row in BASE]
    for row, column, tile in updates:
        rows[row - 1][column - 1] = tile
    return ["".join(row) for row in rows]


class RevisionWorkflowTests(unittest.TestCase):
    def test_modes_default_to_demo_enforce_and_formal_shadow(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(modification_v2_mode(True), "enforce")
            self.assertEqual(modification_v2_mode(False), "shadow")

    def test_redistribution_requires_source_and_destination(self):
        workflow = build_revision_workflow(
            "请重新分布水域，让它不只集中在右侧。",
            {"demoMode": True, "versionId": "stage-2"},
        )
        kinds = {item["kind"] for item in workflow["semanticConstraints"]}
        self.assertIn("paired_transition", kinds)
        self.assertIn("component_distribution", kinds)
        with self.assertRaises(SemanticConstraintError):
            validate_semantic_constraints(
                BASE,
                changed((3, 9, ".")),
                workflow,
            )
        results = validate_semantic_constraints(
            BASE,
            changed((3, 9, "."), (3, 4, "@")),
            workflow,
        )
        self.assertTrue(all(item["passed"] for item in results))

    def test_distribution_reframe_is_not_reduced_to_one_removal(self):
        constraints = compile_semantic_constraints(
            "将水域从右侧集中分布改为与B2活动区域局部绑定，同时保留原有节奏。"
        )
        self.assertTrue(any(
            item["kind"] == "paired_transition" and item.get("component") == "water"
            for item in constraints
        ))

    def test_one_change_too_few_is_generic_scope_constraint(self):
        constraints = compile_semantic_constraints("不是坐标问题，我觉得只改一个格太少了")
        scope = next(item for item in constraints if item["kind"] == "change_scope")
        self.assertEqual(scope["minimumChangedCells"], 2)

    def test_single_cell_remains_allowed_without_explicit_scope_requirement(self):
        workflow = build_revision_workflow(
            "请在通道旁增加一格墙体。",
            {"demoMode": True, "versionId": "stage-2"},
        )
        self.assertEqual(workflow["scope"]["minimumChangedCells"], 1)

    def test_formal_shadow_records_failure_without_rejecting_candidate(self):
        workflow = build_revision_workflow(
            "请重新分布水域。",
            {"demoMode": False, "versionId": "stage-2"},
        )
        results = validate_semantic_constraints(
            BASE,
            changed((3, 9, ".")),
            workflow,
        )
        self.assertTrue(any(item["passed"] is False for item in results))


if __name__ == "__main__":
    unittest.main()
