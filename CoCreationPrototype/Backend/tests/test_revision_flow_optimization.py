import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as llm
from design_requirements import (
    INTERPRETATION_VERSION, REQUIREMENT_SEMANTICS, REQUIREMENT_DEFINITIONS, evaluate_requirements,
    requirement_model_view, validate_requirement_record,
)
from level_validation import build_stage_snapshot, validate_and_solve, LevelValidationError
from proposal_search import (
    _generate_primitives,
    parse_revision_plan, search_revision_plan,
)

BASE = ["############", "#..........#", "#.......@@.#", "#..........#",
        "#...p......#", "#...s.t....#", "#..........#", "#..........#", "#..........#", "############"]
VISUAL = {"id": "v", "content": "我觉得这样不好看，让水面分散一些"}
REASON = {"id": "r", "content": "原来的水域也得减少一些"}


def requirement(source=VISUAL, **extra):
    return {"component": "water", "property": "appearance", "relation": "seek", "strength": "goal",
            "entities": [], "value": None, "unit": "none", "priorityEvidenceSpan": "", "focused": True,
            "sourceTurnId": source["id"], "evidenceSpan": source["content"], "statement": source["content"], **extra}


def changed(*updates):
    rows = [list(row) for row in BASE]
    for r, c, tile in updates:
        rows[r - 1][c - 1] = tile
    return ["".join(row) for row in rows]


def completion(payload, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


def plan(operators=("add_water",), preserve=(), budget=3, effect="reshape_water"):
    return parse_revision_plan({"strategies": [{"effect": effect, "focus": None,
        "operators": list(operators), "preserve": ["outer_shell", "player", "boxes", "targets", *preserve],
        "editBudget": budget, "metricGoals": [], "requiredTransitions": [], "anchorEntities": [], "playObjective": "visual balance"}]})


class RevisionFlowOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_stage_snapshot(BASE, version_id="stage")
        self.sources = [VISUAL, REASON]

    def record(self, *items):
        return validate_requirement_record({"requirements": list(items), "automaticBindings": [],
            "exactTransitions": []}, self.sources, self.snapshot)

    def test_original_water_scope_survives_review_projection_and_replay(self):
        original = requirement(REASON, property="count", relation="decrease", scope="original_cells")
        record = self.record(original)
        candidate = changed((3, 9, "."), (2, 2, "@"), (2, 3, "@"))
        self.assertEqual(sum(row.count("@") for row in candidate), 3)
        result = evaluate_requirements(BASE, candidate, record)[0]
        self.assertEqual((result["before"], result["after"], result["status"]), (2, 1, "fulfilled"))
        replayed = validate_requirement_record(requirement_model_view(record), self.sources, self.snapshot)
        self.assertEqual(replayed, record)
        global_record = self.record({**original, "scope": "all"})
        self.assertFalse(evaluate_requirements(BASE, candidate, global_record)[0]["passed"])

    def test_legacy_scope_default_and_invalid_scope(self):
        record = self.record(requirement(REASON, property="count", relation="decrease"))
        self.assertNotIn("scope", record["requirements"][0])
        self.assertFalse(evaluate_requirements(BASE, changed((2, 2, "@")), record)[0]["passed"])
        for scope in ("invalid", "original_cells"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.record(requirement(scope=scope))

    def test_appearance_is_unverified_and_does_not_require_route_change(self):
        record = self.record(requirement())
        policy = llm._proposal_objective_policy([], {"requirementRecord": record})
        validator = llm._objective_validating_proposal_validator(None, BASE, validate_and_solve(BASE),
            policy, None, {}, {"requirementRecord": record})
        validation = validator(changed((2, 2, "@")))
        self.assertEqual(validation.solution_steps, validate_and_solve(BASE).solution_steps)
        outcome = evaluate_requirements(BASE, validation.rows, record)[0]
        self.assertEqual((outcome["status"], outcome["passed"]), ("deferred", True))
        for extra in ({"relation": "increase"}, {"value": 1}, {"unit": "absolute"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.record(requirement(**extra))

    def test_gameplay_goal_without_witness_proximity_can_be_deferred(self):
        raw = requirement(component="gameplay", property="experience")
        record = self.record(raw)
        evidence = {}
        validator = llm._objective_validating_proposal_validator(None, BASE, validate_and_solve(BASE),
            llm._proposal_objective_policy([], {"requirementRecord": record}), None,
            evidence, {"requirementRecord": record})
        validator(changed((2, 2, "@")))
        self.assertFalse(next(iter(evidence.values()))["routeAffected"])

    def test_numeric_goals_accept_partial_unchanged_but_reject_reverse(self):
        for prop in ("count", "solutionSteps", "minimumPushes"):
            item = requirement(property=prop, relation="increase", value=None)
            record = self.record(item)
            for after, status, passed in ((10, "deferred", True), (11, "fulfilled", True), (9, "deferred", False)):
                before_features, after_features = {prop: 10}, {prop: after}
                if prop == "count":
                    outcomes = evaluate_requirements(BASE, changed(*(
                        ((2, 2, "@"),) if after == 11 else ((3, 9, "."),) if after == 9 else ())), record)
                else:
                    outcomes = evaluate_requirements(BASE, BASE, record, before_features, after_features)
                self.assertEqual((outcomes[0]["status"], outcomes[0]["passed"]), (status, passed))

    def test_compensation_operators_are_added_without_cross_component_permission(self):
        record = self.record(requirement(property="count", relation="increase"))
        policy = llm._proposal_objective_policy([], {"requirementRecord": record,
            "proposalDiscovery": {"understanding": {"editScope": [{"component": "water"}]}}})
        expanded = llm._apply_objective_policy_to_plan(llm.replace(plan(), strategies=(llm.replace(plan().strategies[0], preserve=plan().strategies[0].preserve | {"water"}),)), policy)
        self.assertEqual(set(expanded.strategies[0].operators), {"add_water", "remove_water"})
        self.assertNotIn("water", expanded.strategies[0].preserve)
        preserved = llm._apply_objective_policy_to_plan(plan(), policy, {"water"})
        self.assertNotIn("remove_water", preserved.strategies[0].operators)
        exact = copy.deepcopy(plan())
        exact = llm.replace(exact, strategies=(llm.replace(exact.strategies[0],
            required_transitions=((2, 2, ".", "@"),)),))
        self.assertEqual(llm._apply_objective_policy_to_plan(exact, policy).strategies[0].operators, ("add_water",))

    def test_grouped_search_finds_net_addition_with_old_tile_removal(self):
        strategy_plan = plan(("add_water", "remove_water"))
        primitives = _generate_primitives(BASE, strategy_plan.strategies[0], validate_and_solve(BASE).as_dict())
        self.assertTrue(any(item.operator == "compensate_water" for item in primitives[:8]))
        def validator(rows):
            old = {(r, c) for r, row in enumerate(BASE) for c, tile in enumerate(row) if tile == "@"}
            new = {(r, c) for r, row in enumerate(rows) for c, tile in enumerate(row) if tile == "@"}
            if not old - new or len(new) <= len(old):
                raise ValueError("Need a net addition with an original tile removed.")
            return validate_and_solve(rows)
        result = search_revision_plan(BASE, strategy_plan, validator, validate_and_solve(BASE).as_dict(),
            deadline=time.monotonic() + 5)
        self.assertEqual(sum(a != b for left, right in zip(BASE, result.rows) for a, b in zip(left, right)), 3)
        self.assertEqual(set(result.diagnostics["selectedOperators"]), {"remove_water", "add_water"})

    def test_solver_budget_failure_is_distinct_from_unsolvable(self):
        with self.assertRaises(LevelValidationError) as captured:
            validate_and_solve(BASE, maximum_search_states=1)
        self.assertEqual(captured.exception.code, "SEARCH_BUDGET_EXCEEDED")
        self.assertEqual(llm._candidate_rejection_category(captured.exception), "solver_budget_exhausted")
        # Construct an exhaustive no-solution state by surrounding the box.
        blocked = changed((6, 4, "#"), (6, 6, "#"), (7, 5, "#"))
        with self.assertRaises(LevelValidationError) as captured:
            validate_and_solve(blocked)
        self.assertEqual(captured.exception.code, "UNSOLVABLE_LEVEL")

    def test_net_wall_search_and_both_component_operators_share_limits(self):
        base = changed((3, 9, "#"))
        wall_plan = plan(("add_wall", "remove_wall"), effect="adjust_internal_walls")
        def validator(rows):
            if rows[2][8] != "." or sum(row.count("#") for row in rows) != sum(row.count("#") for row in base) + 1:
                raise ValueError("Need removal of the old wall and a net addition.")
            return validate_and_solve(rows)
        result = search_revision_plan(base, wall_plan, validator, validate_and_solve(base).as_dict(),
            deadline=time.monotonic() + 5)
        self.assertEqual(set(result.diagnostics["selectedOperators"]), {"remove_wall", "add_wall"})
        self.assertEqual(sum(a != b for left, right in zip(base, result.rows) for a, b in zip(left, right)), 3)
        both = plan(("add_water", "remove_water", "add_wall", "remove_wall"))
        self.assertEqual(len(both.strategies[0].operators), 4)
        self.assertLessEqual(both.strategies[0].edit_budget, 12)

    def test_prohibiting_removal_still_allows_new_water(self):
        source = {"id": "lock", "content": "增加水域，但不允许删除原来的水域"}
        record = validate_requirement_record({"requirements": [
            requirement(source, property="count", relation="increase"),
            requirement(source, property="count", relation="preserve", strength="invariant", scope="original_cells")
        ], "automaticBindings": [], "exactTransitions": []}, [source], self.snapshot)
        policy = llm._proposal_objective_policy([], {"requirementRecord": record})
        modified = llm._apply_objective_policy_to_plan(plan(("add_water", "remove_water")), policy)
        self.assertEqual(modified.strategies[0].operators, ("add_water",))
        self.assertTrue(all(item["passed"] for item in evaluate_requirements(BASE, changed((2, 2, "@")), record)))
        self.assertFalse(all(item["passed"] for item in evaluate_requirements(BASE,
            changed((3, 9, "."), (2, 2, "@"), (2, 3, "@")), record)))

    def test_old_solver_route_can_fail_while_new_solution_is_verified(self):
        validation = validate_and_solve(BASE)
        candidate = changed((6, 4, "@"))
        old_trace = llm._trace_cells_for_solution(BASE, validation.solution)
        self.assertIn((3, 5), old_trace)
        new_validation = validate_and_solve(candidate)
        self.assertNotEqual(validation.solution, new_validation.solution)
        self.assertTrue(new_validation.solution)
        record = self.record(requirement(property="count", relation="increase"))
        validator = llm._objective_validating_proposal_validator(None, BASE, validation,
            llm._proposal_objective_policy([], {"requirementRecord": record}), None, {},
            {"requirementRecord": record})
        self.assertEqual(validator(candidate).solution, new_validation.solution)

    def test_illegal_json_failure_is_distinct_from_understanding_failure(self):
        async def request(*args, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="{illegal json}"))])
        context = {"stageSnapshot": self.snapshot, "requirementUserTurns": self.sources}
        with patch.object(llm, "_request_completion", side_effect=request), self.assertRaises(llm.LLMServiceError) as caught:
            llm._review_proposal_requirements("offline", "offline", context, "request", time.monotonic() + 116)
        self.assertEqual(caught.exception.details["failureKind"], "invalid_json")
        self.assertEqual(caught.exception.attempts_used, 2)

    def test_review_inherits_valid_same_stage_sources_and_uses_shared_definitions(self):
        record = self.record(requirement())
        record["interpretationVersion"] = INTERPRETATION_VERSION
        context = {"stageSnapshot": self.snapshot, "requirementUserTurns": self.sources,
            "verifiedRequirementCache": [{"baseVersionId": "stage", "record": record}],
            "responseLanguage": "zh-CN"}
        raw = {"requirements": [requirement(), requirement(REASON, property="count",
            relation="decrease", scope="original_cells")], "automaticBindings": [], "exactTransitions": []}
        responses = iter([raw, {"accepted": True, "issues": []}])
        calls = []
        async def request(*args, **kwargs):
            calls.append(args[3])
            return completion(next(responses))
        with patch.object(llm, "_request_completion", side_effect=request):
            result = llm._review_proposal_requirements("offline", "offline", context, "request", time.monotonic() + 116)
        for messages in calls:
            self.assertIn(REQUIREMENT_SEMANTICS, messages[0]["content"])
            self.assertEqual(json.loads(messages[1]["content"])["inheritedVerifiedRequirements"], [requirement()])
            self.assertEqual(json.loads(messages[1]["content"])["requirementDefinitions"], REQUIREMENT_DEFINITIONS)
        self.assertEqual(result["requirements"][1]["scope"], "original_cells")

    def test_explicit_correction_can_replace_related_scope_without_losing_appearance(self):
        prior = self.record(requirement(), requirement(REASON, property="count", relation="decrease", scope="original_cells"))
        prior["interpretationVersion"] = INTERPRETATION_VERSION
        correction = {"id": "correct", "content": "不是只减少原来的格子，我要整体水量减少一些"}
        payload = {"requirements": [requirement(), requirement(correction, property="count", relation="decrease")],
            "automaticBindings": [], "exactTransitions": []}
        context = {"stageSnapshot": self.snapshot, "requirementUserTurns": [*self.sources, correction],
            "verifiedRequirementCache": [{"baseVersionId": "stage", "record": prior},
                {"baseVersionId": "older-stage", "record": prior}]}
        responses = iter([payload, {"accepted": True, "issues": []}])
        calls = []
        async def request(*args, **kwargs):
            calls.append(args[3])
            return completion(next(responses))
        with patch.object(llm, "_request_completion", side_effect=request):
            result = llm._review_proposal_requirements("offline", "offline", context, "request", time.monotonic() + 116)
        self.assertEqual(len(json.loads(calls[1][1]["content"])["inheritedVerifiedRequirements"]), 2)
        self.assertEqual(result["requirements"][0]["property"], "appearance")
        self.assertEqual(result["requirements"][1]["sourceTurnId"], correction["id"])
        self.assertNotIn("scope", result["requirements"][1])

    def test_repair_receives_full_structured_issue_and_timeout_is_classified(self):
        record = self.record(requirement())
        instruction = "Review the exact visual meaning. " * 80
        issue = {"requirementIds": [record["requirements"][0]["requirementId"]], "kind": "meaning",
            "evidenceSpans": [VISUAL["content"]], "repairInstruction": instruction}
        responses = iter([requirement_model_view(record), {"accepted": False, "issues": [issue]},
            requirement_model_view(record), {"accepted": True, "issues": []}])
        calls = []
        async def request(*args, **kwargs):
            calls.append(args[3])
            return completion(next(responses))
        context = {"stageSnapshot": self.snapshot, "requirementUserTurns": self.sources}
        with patch.object(llm, "_request_completion", side_effect=request):
            llm._review_proposal_requirements("offline", "offline", context, "request", time.monotonic() + 116)
        self.assertEqual(json.loads(calls[2][-1]["content"])["structuredIssues"], [issue])
        async def timeout(*args, **kwargs):
            raise asyncio.TimeoutError()
        with patch.object(llm, "_request_completion", side_effect=timeout), self.assertRaises(llm.LLMServiceError) as caught:
            llm._review_proposal_requirements("offline", "offline", context, "request", time.monotonic() + 116)
        self.assertEqual(caught.exception.details["failureKind"], "timeout")
        self.assertEqual(caught.exception.attempts_used, 2)

    def test_truncated_review_is_not_accepted(self):
        async def request(*args, **kwargs):
            return completion(requirement_model_view(self.record(requirement())) if kwargs["task"] == "revision_requirements"
                else {"accepted": True, "issues": []}, "stop" if kwargs["task"] == "revision_requirements" else "length")
        context = {"stageSnapshot": self.snapshot, "requirementUserTurns": self.sources}
        with patch.object(llm, "_request_completion", side_effect=request), self.assertRaises(llm.LLMServiceError) as caught:
            llm._review_proposal_requirements("offline", "offline", context, "request", time.monotonic() + 116)
        self.assertEqual(caught.exception.details["failureKind"], "truncated_json")
