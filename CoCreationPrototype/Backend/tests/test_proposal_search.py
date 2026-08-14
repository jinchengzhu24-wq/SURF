import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from level_validation import validate_and_solve
from proposal_search import (
    MAX_CONSTRUCTED_CANDIDATES,
    ProposalSearchExhausted,
    RevisionPlanError,
    parse_revision_plan,
    search_revision_plan,
)


BASE_ROWS = [
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


def plan_for(effect, operator, *, focus=None, edit_budget=4, metric_goals=None):
    preserved = ["outer_shell", "unrelated_areas"]
    component = {
        "move_player": "player",
        "move_box": "boxes",
        "move_target": "targets",
        "add_water": "water",
        "remove_water": "water",
    }.get(operator)
    for value in ("player", "boxes", "targets", "water"):
        if value != component:
            preserved.append(value)
    return parse_revision_plan({
        "strategies": [{
            "effect": effect,
            "focus": focus,
            "operators": [operator],
            "preserve": preserved,
            "editBudget": edit_budget,
            "metricGoals": metric_goals or [],
        }]
    })


class ProposalSearchTests(unittest.TestCase):
    def test_revision_plan_schema_is_strict_and_adds_nonnegotiable_preservation(self):
        plan = plan_for("relocate_target", "move_target")
        strategy = plan.strategies[0]
        self.assertIn("outer_shell", strategy.preserve)
        self.assertIn("unrelated_areas", strategy.preserve)

        with self.assertRaises(RevisionPlanError):
            parse_revision_plan({"strategies": [], "extra": True})

        with self.assertRaisesRegex(RevisionPlanError, "preserves every component"):
            parse_revision_plan({
                "strategies": [{
                    "effect": "relocate_target",
                    "focus": None,
                    "operators": ["move_target"],
                    "preserve": ["targets"],
                    "editBudget": 2,
                    "metricGoals": [],
                }]
            })

    def test_every_semantic_operator_returns_a_solved_map_and_preserves_counts(self):
        cases = [
            ("narrow_route", "add_wall", BASE_ROWS),
            (
                "open_route",
                "remove_wall",
                [*BASE_ROWS[:2], "#.#........#", *BASE_ROWS[3:]],
            ),
            ("relocate_start", "move_player", BASE_ROWS),
            ("relocate_box", "move_box", BASE_ROWS),
            ("relocate_target", "move_target", BASE_ROWS),
            ("reshape_water", "add_water", BASE_ROWS),
            (
                "reshape_water",
                "remove_water",
                [*BASE_ROWS[:2], "#.@........#", *BASE_ROWS[3:]],
            ),
        ]
        for effect, operator, rows in cases:
            with self.subTest(operator=operator):
                baseline = validate_and_solve(rows).as_dict()
                result = search_revision_plan(
                    rows,
                    plan_for(effect, operator),
                    validate_and_solve,
                    baseline_metrics=baseline,
                )
                validation = validate_and_solve(result.rows)
                self.assertEqual(sum(row.count("p") for row in result.rows), 1)
                self.assertEqual(
                    sum(row.count("s") for row in result.rows),
                    sum(row.count("t") for row in result.rows),
                )
                self.assertGreater(validation.solution_steps, 0)
                self.assertEqual(result.rows[0], rows[0])
                self.assertEqual(result.rows[-1], rows[-1])

    def test_search_is_deterministic_and_never_exceeds_candidate_limit(self):
        plan = plan_for("narrow_route", "add_wall", edit_budget=3)
        baseline = validate_and_solve(BASE_ROWS).as_dict()
        first = search_revision_plan(BASE_ROWS, plan, validate_and_solve, baseline)
        second = search_revision_plan(BASE_ROWS, plan, validate_and_solve, baseline)
        self.assertEqual(first.rows, second.rows)
        self.assertEqual(first.score, second.score)
        self.assertLessEqual(
            first.diagnostics["constructedCandidates"],
            MAX_CONSTRUCTED_CANDIDATES,
        )
        self.assertLessEqual(first.diagnostics["validCandidates"], 8)

    def test_requested_metric_direction_precedes_stable_map_order(self):
        checked_rows = []

        def validate_with_controlled_metrics(rows):
            checked_rows.append(tuple(rows))
            return SimpleNamespace(
                solution_steps=5 if len(checked_rows) == 1 else 15,
                solution_pushes=1,
                searched_states=1,
            )

        result = search_revision_plan(
            BASE_ROWS,
            plan_for(
                "narrow_route",
                "add_wall",
                metric_goals=[{
                    "metric": "solutionSteps",
                    "direction": "increase",
                }],
            ),
            validate_with_controlled_metrics,
            baseline_metrics={"solutionSteps": 10},
        )

        self.assertGreater(len(checked_rows), 1)
        self.assertEqual(result.score["metricMatches"], 1)
        self.assertNotEqual(result.rows, checked_rows[0])

    def test_focus_and_edit_budget_protect_unrelated_cells(self):
        focus = {"row": 3, "column": 3, "radius": 1}
        result = search_revision_plan(
            BASE_ROWS,
            plan_for("narrow_route", "add_wall", focus=focus, edit_budget=1),
            validate_and_solve,
            baseline_metrics=validate_and_solve(BASE_ROWS).as_dict(),
        )
        changed = [
            (x, y)
            for y, (before_row, after_row) in enumerate(zip(BASE_ROWS, result.rows))
            for x, (before, after) in enumerate(zip(before_row, after_row))
            if before != after
        ]
        self.assertEqual(len(changed), 1)
        x, y = changed[0]
        self.assertLessEqual(max(abs(x - 2), abs(y - 2)), 1)

    def test_solver_rejections_are_aggregated_in_search_diagnostics(self):
        calls = []

        def reject(rows):
            calls.append(tuple(rows))
            raise ValueError("The level has no Sokoban solution.")

        with self.assertRaises(ProposalSearchExhausted) as raised:
            search_revision_plan(
                BASE_ROWS,
                plan_for("relocate_target", "move_target"),
                reject,
            )
        diagnostics = raised.exception.diagnostics
        self.assertGreater(diagnostics["constructedCandidates"], 0)
        self.assertEqual(diagnostics["solverRejectedCandidates"], len(calls))
        self.assertEqual(diagnostics["failureReasons"]["UNSOLVABLE_LEVEL"], len(calls))

    def test_expired_deadline_stops_before_constructing_candidates(self):
        with self.assertRaises(ProposalSearchExhausted) as raised:
            search_revision_plan(
                BASE_ROWS,
                plan_for("narrow_route", "add_wall"),
                validate_and_solve,
                deadline=0,
            )
        self.assertTrue(raised.exception.diagnostics["deadlineReached"])
        self.assertEqual(raised.exception.diagnostics["constructedCandidates"], 0)


if __name__ == "__main__":
    unittest.main()
