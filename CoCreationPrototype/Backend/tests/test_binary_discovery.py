"""Offline proofs, HTTP receipts and actual provider schemas for binary discovery."""
import copy
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cocreation  # noqa: F401
from tests.fakes import RecordedCall, SessionTestCase
from cocreation.api import config_models
from cocreation.api import session_persistence_helpers
from cocreation.persistence import repository
from cocreation.domain import binary_discovery as binary
from cocreation.llm import binary_model
from cocreation.llm import facade as llm
from cocreation.domain.level_validation import validate_and_solve, minimum_pushes
import test_sessions as legacy
import test_wire_contracts as wire_tests
from test_wire_contracts import assert_schema


ROWS = list(config_models.SAMPLE_ROWS)
ROWS[2] = "#.@.....@..#"


def step():
    return {"action": "ask", "question": "Which area should get more room?", "note": "Keep the other side intact.",
        "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
        "planStrategies": [], "protectedEntities": [],
        "branches": [{"label": label, "strategy": {"effect": "open_route", "focus": {"row": 3, "column": col, "radius": 1},
            "operators": ["remove_water"], "preserve": ["outer_shell", "player", "boxes", "targets"],
            "editBudget": 2, "metricGoals": []}, "promise": {"kind": "reduce_obstacles", "component": "water",
                "focus": {"row": 3, "column": col, "radius": 1}, "corridor": None}}
            for label, col in [("Open the left water!", 3), ("Open the right water!", 9)]]}


def topic():
    return {"binding": "fixture", "choices": [], "initialRequest": "Make it simpler.", "questionCount": 0, "understanding": {},
        "userTurns": [{"id": "initial", "content": "Make it simpler."}]}


def validate(rows, deadline):
    llm.validate_ai_edit_diff(ROWS, rows)
    return validate_and_solve(rows, deadline=deadline)


class BinaryProofTests(unittest.TestCase):
    def test_both_candidates_really_remove_obstacles_and_remain_solvable(self):
        caches = binary.preflight(binary.parse_step(step(), 0), topic(), ROWS, {}, validate, time.monotonic() + 116)
        self.assertEqual(len(caches), 2)
        self.assertNotEqual(caches[0]["rows"], caches[1]["rows"])
        for cache in caches:
            self.assertTrue(all(r["passed"] for r in cache["promiseResults"]))
            self.assertLess(sum(r.count("@") for r in cache["rows"]), 2)

    def test_solvable_and_operation_name_do_not_prove_reduction(self):
        moved = list(ROWS)
        moved[2] = "#..@....@..#"
        validate_and_solve(moved)
        with self.assertRaises(binary.BinaryError):
            binary.check_promises(ROWS, moved, [step()["branches"][0]["promise"]])

    def test_cumulative_promises_cannot_be_undone(self):
        right_only = list(ROWS)
        right_only[2] = "#.@........#"
        with self.assertRaises(binary.BinaryError):
            binary.check_promises(ROWS, right_only, [b["promise"] for b in step()["branches"]])

    def test_widening_requires_the_named_corridor(self):
        rows = list(ROWS)
        rows[3] = "#...#.#....#"
        rows[4] = "#...#p#....#"
        rows[5] = "#...#s#t...#"
        promise = {"kind": "widen_corridor", "component": "internal_walls", "focus": None,
                   "corridor": {"row": 5, "column": 6, "axis": "horizontal"}}
        unrelated = list(rows); unrelated[2] = "#.......@..#"
        with self.assertRaises(binary.BinaryError): binary.check_promises(rows, unrelated, [promise])
        widened = list(rows); widened[4] = "#....p#....#"
        self.assertTrue(binary.check_promises(rows, widened, [promise])[0]["passed"])

    def test_shorter_solution_uses_actual_solver_metrics(self):
        promise = {"kind": "shorter_solution", "component": "obstacles", "focus": None, "corridor": None}
        baseline = validate_and_solve(ROWS)
        with self.assertRaises(binary.BinaryError): binary.check_promises(ROWS, ROWS, [promise], baseline, baseline)
        with self.assertRaises(binary.BinaryError): binary.check_promises(ROWS, ROWS, [promise])

    def test_reshape_preserves_count_but_changes_positions(self):
        promise = {"kind": "reshape_water", "component": "water", "focus": None, "corridor": None}
        moved = list(ROWS); moved[2] = "#..@....@..#"
        self.assertTrue(binary.check_promises(ROWS, moved, [promise])[0]["passed"])
        with self.assertRaises(binary.BinaryError): binary.check_promises(ROWS, ROWS, [promise])

    def test_deadline_reaches_both_solver_loops(self):
        for solver in (validate_and_solve, minimum_pushes):
            with self.assertRaises(TimeoutError): solver(ROWS, deadline=time.monotonic() - 1)
        with self.assertRaises(binary.BinaryError):
            binary.preflight(step(), topic(), ROWS, {}, validate, time.monotonic() + 19)

    def test_schema_counts_and_protected_operators(self):
        assert_schema(step(), binary.response_format()["json_schema"]["schema"])
        with self.assertRaises(binary.BinaryError): binary.parse_step(step(), 3)
        duplicate = step(); duplicate["branches"][1] = copy.deepcopy(duplicate["branches"][0])
        with self.assertRaises(binary.BinaryError): binary.parse_step(duplicate, 0)
        missing = step(); missing["branches"].pop()
        with self.assertRaises(binary.BinaryError): binary.parse_step(missing, 0)

    def test_missing_choice_compilation_and_hidden_locks_rejected(self):
        choice = {"sourceTurnId": "click", "promise": step()["branches"][0]["promise"]}
        with self.assertRaisesRegex(ValueError, "COVERAGE"):
            binary.require_choice_coverage({"requirements": []}, [choice])
        valid = {"requirements": [{"sourceTurnId": "click", "strength": "goal", "value": None,
            "component": "water", "property": "count", "relation": "decrease"}]}
        binary.require_choice_coverage(valid, [choice])
        valid["requirements"][0]["value"] = 3
        with self.assertRaises(ValueError): binary.require_choice_coverage(valid, [choice])

    def test_preflight_rejects_wall_or_lengthwise_widen_corridor_before_search(self):
        stage_rows = [
            "  ######### ",
            "  #....s.t# ",
            "  #....##.##",
            " ##@@@@#...#",
            " #.@@@@#...#",
            " #.@@@@...##",
            "##.@@@@p..# ",
            "#......s..# ",
            "##.t.....## ",
            " #########  ",
        ]

        def stage_validate(rows, deadline):
            llm.validate_ai_edit_diff(stage_rows, rows)
            return validate_and_solve(rows, deadline=deadline)

        wall_step = step()
        wall_step["branches"][1]["strategy"] = {
            "effect": "open_route",
            "focus": {"row": 3, "column": 8, "radius": 1},
            "operators": ["remove_wall"],
            "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
            "editBudget": 2,
            "metricGoals": [],
        }
        wall_step["branches"][1]["promise"] = {
            "kind": "widen_corridor",
            "component": "internal_walls",
            "focus": {"row": 3, "column": 8, "radius": 1},
            "corridor": {"row": 3, "column": 9, "axis": "horizontal"},
        }
        with self.assertRaises(binary.BinaryError) as wall_err:
            binary.preflight(binary.parse_step(wall_step, 0), topic(), stage_rows, {}, stage_validate, time.monotonic() + 116)
        self.assertEqual(wall_err.exception.code, "BINARY_CORRIDOR_INVALID")
        self.assertEqual(wall_err.exception.field, "branch-2")
        self.assertIn("not a walkable floor cell", str(wall_err.exception.__cause__))

        axis_step = copy.deepcopy(wall_step)
        axis_step["branches"][1]["strategy"]["focus"] = {"row": 6, "column": 8, "radius": 2}
        axis_step["branches"][1]["promise"]["focus"] = {"row": 6, "column": 8, "radius": 2}
        axis_step["branches"][1]["promise"]["corridor"] = {"row": 6, "column": 8, "axis": "vertical"}
        with self.assertRaises(binary.BinaryError) as axis_err:
            binary.preflight(binary.parse_step(axis_step, 0), topic(), stage_rows, {}, stage_validate, time.monotonic() + 116)
        self.assertEqual(axis_err.exception.code, "BINARY_CORRIDOR_INVALID")
        self.assertEqual(axis_err.exception.field, "branch-2")
        self.assertIn("measures length", str(axis_err.exception.__cause__))

    def test_preflight_rejects_incompatible_cumulative_q2_branches_before_search(self):
        stage_rows = [
            "  ######### ",
            " ##.......# ",
            " #.ps.....##",
            "##@@@@....t#",
            "#.@@@@..@@.#",
            "#...##..@@##",
            "##..s...@@# ",
            " #........# ",
            " #t......## ",
            " #########  ",
        ]

        def stage_validate(rows, deadline):
            llm.validate_ai_edit_diff(stage_rows, rows)
            return validate_and_solve(rows, deadline=deadline)

        prior_promise = {"kind": "reduce_obstacles", "component": "water", "focus": {"row": 4, "column": 4, "radius": 2}, "corridor": None}
        q2_topic = {
            **topic(),
            "questionCount": 1,
            "choices": [{"questionId": "q1", "choiceId": "c1", "question": "Which water?", "note": "", "label": "Upper-left water!", "promise": prior_promise}],
        }
        q2_step = {
            "action": "ask",
            "question": "What else should we open?",
            "note": "",
            "initialRequirements": None,
            "branches": [
                {
                    "label": "Drain right water!",
                    "strategy": {
                        "effect": "open_route",
                        "focus": {"row": 6, "column": 9, "radius": 2},
                        "operators": ["remove_water"],
                        "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                        "editBudget": 4,
                        "metricGoals": [],
                    },
                    "promise": {"kind": "reduce_obstacles", "component": "water", "focus": {"row": 6, "column": 9, "radius": 2}, "corridor": None},
                },
                {
                    "label": "Remove central walls!",
                    "strategy": {
                        "effect": "adjust_internal_walls",
                        "focus": {"row": 6, "column": 5, "radius": 1},
                        "operators": ["remove_wall"],
                        "preserve": ["outer_shell", "player", "boxes", "targets", "water"],
                        "editBudget": 2,
                        "metricGoals": [],
                    },
                    "promise": {"kind": "reduce_obstacles", "component": "internal_walls", "focus": {"row": 6, "column": 5, "radius": 1}, "corridor": None},
                },
            ],
        }
        with self.assertRaises(binary.BinaryError) as both_err:
            binary.preflight(binary.parse_step(copy.deepcopy(q2_step), 1), q2_topic, stage_rows, {}, stage_validate, time.monotonic() + 116)
        self.assertEqual(both_err.exception.code, "BINARY_CUMULATIVE_STRATEGY_INVALID")
        self.assertEqual(both_err.exception.field, "branches")
        self.assertIn("branch-1", both_err.exception.detail)
        self.assertIn("branch-2", both_err.exception.detail)

        one_fixed = copy.deepcopy(q2_step)
        one_fixed["branches"][0]["strategy"]["focus"] = None
        one_fixed["branches"][0]["strategy"]["editBudget"] = 6
        with self.assertRaises(binary.BinaryError) as single_err:
            binary.preflight(binary.parse_step(one_fixed, 1), q2_topic, stage_rows, {}, stage_validate, time.monotonic() + 116)
        self.assertEqual(single_err.exception.code, "BINARY_CUMULATIVE_STRATEGY_INVALID")
        self.assertEqual(single_err.exception.field, "branch-2")


class BinaryWireTests(unittest.TestCase):
    def test_real_client_generation_uses_single_call_schema_without_sync_review(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        with wire.provider(lambda request, number: step()):
            result, caches = binary_model.generate(topic(), context, validate, None, "binary-wire", time.monotonic() + 116)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(len(caches), 2)
        self.assertEqual(len(wire.calls), 1)
        schema = wire.calls[0]["response_format"]["json_schema"]
        self.assertEqual(schema["name"], "cocreation_binary_discovery")
        self.assertIn("planStrategies", schema["schema"]["properties"])
        self.assertIn("protectedEntities", schema["schema"]["properties"])

    def test_missing_component_is_patched_without_rewriting_question(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        from patch_fixtures import patch_fixture
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        def handler(request, number):
            if number == 1:
                bad = step(); del bad["branches"][1]["promise"]
                return (bad, "stop")
            specification = json.loads(request["messages"][-1]["content"])
            self.assertEqual(specification["targets"], ["branch-2"])
            self.assertEqual(specification["frozenCandidate"]["question"], step()["question"])
            return patch_fixture(specification, step())
        with wire.provider(handler):
            result, _ = binary_model.generate(topic(), context, validate, None, "binary-patch", time.monotonic() + 116)
        self.assertEqual(result["question"], step()["question"])
        self.assertEqual(len(wire.calls), 2)

    def test_validate_binary_step_deterministic_blocks_protected_text_and_component_mismatch(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        bad_text = step()
        bad_text["branches"][0]["label"] = "把箱子往右移动！"
        with self.assertRaises(binary.BinaryError) as caught_text:
            binary.validate_binary_step_deterministic(copy.deepcopy(bad_text), topic(), "zh-CN")
        self.assertEqual(caught_text.exception.code, "BINARY_PROTECTED_TEXT")

        bad_mismatch = step()
        bad_mismatch["branches"][0]["promise"]["component"] = "internal_walls"
        with self.assertRaises(binary.BinaryError) as caught_mismatch:
            binary.validate_binary_step_deterministic(copy.deepcopy(bad_mismatch), topic(), "en")
        self.assertEqual(caught_mismatch.exception.code, "BINARY_BRANCH_COMPONENT_MISMATCH")

        good_keep = step()
        good_keep["branches"][0]["label"] = "保留箱子和玩家，清理左侧水域！"
        binary.validate_binary_step_deterministic(copy.deepcopy(good_keep), topic(), "zh-CN")

        from patch_fixtures import patch_fixture

        def reject_patch_on_retry(call, number):
            if number == 1:
                return bad_text
            spec = json.loads(call["messages"][-1]["content"])
            return patch_fixture(spec, bad_text)

        with wire.provider(reject_patch_on_retry):
            with self.assertRaises(llm.LLMServiceError) as rejected:
                binary_model.generate(topic(), context, validate, None, "binary-det-reject", time.monotonic() + 116)
        self.assertEqual(rejected.exception.code, "BINARY_PROTECTED_TEXT")
        self.assertEqual(len(wire.calls), 2)

    def test_branch_patch_receives_consistency_guidance_after_corridor_error(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        from patch_fixtures import patch_fixture
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        patch_messages = []

        def handler(request, number):
            if number == 1:
                bad = step()
                bad["branches"][1]["strategy"]["operators"] = ["remove_wall"]
                bad["branches"][1]["strategy"]["preserve"] = ["outer_shell", "player", "boxes", "targets", "water"]
                bad["branches"][1]["promise"] = {
                    "kind": "widen_corridor",
                    "component": "internal_walls",
                    "focus": {"row": 1, "column": 1, "radius": 1},
                    "corridor": {"row": 1, "column": 1, "axis": "horizontal"},
                }
                return bad
            patch_messages.extend(request["messages"])
            specification = json.loads(request["messages"][-1]["content"])
            self.assertEqual(specification["targets"], ["branch-2"])
            return patch_fixture(specification, step())

        with wire.provider(handler):
            result, _ = binary_model.generate(topic(), context, validate, None, "binary-corridor-patch", time.monotonic() + 116)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(len(wire.calls), 2)
        system_prompt_text = "\n".join(m["content"] for m in patch_messages if m["role"] == "system")
        self.assertIn("BINARY_CORRIDOR_INVALID", system_prompt_text)
        self.assertIn("not a walkable floor cell", system_prompt_text)
        self.assertIn("Keep the repaired branch consistent with the frozen question and note", system_prompt_text)

    def test_truncated_binary_response_retries_full_generation_without_unbound_field(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}

        def handler(request, number):
            if number == 1:
                return (step(), "length")
            self.assertEqual(request["response_format"]["json_schema"]["name"], "cocreation_binary_discovery")
            return step()

        with wire.provider(handler):
            result, _ = binary_model.generate(topic(), context, validate, None, "binary-truncated-retry", time.monotonic() + 116)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(len(wire.calls), 2)

    def test_translation_preserves_whole_group_and_ids(self):
        group = {"questionId": "q", "topicId": "topic", "baseVersionId": "v", "question": "Which side?", "note": "",
            "number": 1, "choices": [{"choiceId": "a", "letter": "A", "label": "Open left"},
                {"choiceId": "b", "letter": "B", "label": "Open right"}], "status": "pending", "selectedChoiceId": None}
        source = {"turnId": "t", "body": "Which side?", "followUpQuestion": None, "intentHypothesis": None,
            "proposalOfferSummary": None, "proposalOfferRationale": None, "proposalSummary": None, "uiCueTexts": [],
            "proposalQuestion": group, "proposalVerifiedActions": ["Open left"], "stageRows": ROWS}
        translated = {k: copy.deepcopy(v) for k, v in source.items() if k != "stageRows"}
        result = llm.validate_translation_response({"translations": [translated]}, [source], "en")
        self.assertEqual(result[0]["proposalQuestion"], group)
        self.assertEqual(result[0]["proposalVerifiedActions"], ["Open left"])
        translated["proposalQuestion"]["choices"][0]["choiceId"] = "invented"
        with self.assertRaises(ValueError): llm.validate_translation_response({"translations": [translated]}, [source], "en")

    def test_locked_record_and_ready_path_use_empty_plan_protocol(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        current = topic(); current["requirements"] = step()["initialRequirements"]
        current["choices"] = [{"question": "Which side?", "note": "", "label": "Open left",
            "promise": step()["branches"][0]["promise"]}]
        current["selectedCandidate"] = {"promiseResults": [{"passed": True, "kind": "reduce_obstacles", "before": 2, "after": 1}]}
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        plan = {"action": "plan", "question": "", "note": "", "branches": [], "planStrategies": [], "protectedEntities": [], "initialRequirements": None}
        with wire.provider(lambda call, number: plan):
            result, candidates = binary_model.generate(current, context, validate, None, "binary-ready", time.monotonic()+116)
        self.assertEqual(result["action"], "plan")
        self.assertEqual(candidates, [])
        self.assertEqual(len(wire.calls), 1)
        prompt = json.loads(wire.calls[0]["messages"][1]["content"])
        self.assertTrue(prompt["topic"]["selectedFeasibility"]["allSelectedActionsPassed"])

    def test_q2_projection_strips_stale_sufficient_and_allows_plan_recovery_after_preflight_error(self):
        wire = wire_tests.WireContracts(); wire.setUp()
        current = topic()
        current["questionCount"] = 1
        current["requirements"] = step()["initialRequirements"]
        current["understanding"] = {"aspect": "gameplay", "sufficient": False, "nextQuestionDimension": "mechanism"}
        current["choices"] = [{
            "question": "Which water?", "note": "", "label": "Open left!",
            "promise": {"kind": "reduce_obstacles", "component": "water", "focus": {"row": 3, "column": 3, "radius": 1}, "corridor": None},
        }]
        current["selectedCandidate"] = {"promiseResults": [{"passed": True, "kind": "reduce_obstacles", "before": 2, "after": 1}]}
        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}

        bad_q2 = step()
        bad_q2["initialRequirements"] = None
        bad_q2["branches"][0]["strategy"]["focus"] = {"row": 3, "column": 9, "radius": 1}
        bad_q2["branches"][0]["promise"]["focus"] = {"row": 3, "column": 9, "radius": 1}
        bad_q2["branches"][1]["strategy"]["preserve"] = ["outer_shell", "player", "boxes", "targets", "water"]
        plan_step = {"action": "plan", "question": "", "note": "", "branches": [], "planStrategies": [], "protectedEntities": [], "initialRequirements": None}

        def handler(request, number):
            if number == 1:
                prompt_topic = json.loads(request["messages"][1]["content"])["topic"]
                self.assertNotIn("sufficient", prompt_topic["understanding"])
                self.assertNotIn("nextQuestionDimension", prompt_topic["understanding"])
                return bad_q2
            self.assertEqual(request["response_format"]["json_schema"]["name"], "cocreation_binary_discovery")
            return plan_step

        with wire.provider(handler):
            result, candidates = binary_model.generate(current, context, validate, None, "binary-q2-recover", time.monotonic() + 116)
        self.assertEqual(result["action"], "plan")
        self.assertEqual(candidates, [])
        self.assertEqual(len(wire.calls), 2)

    def test_prompts_exclude_platform_protections_and_single_call_schema(self):
        from types import SimpleNamespace
        from tests.fakes import RecordedAsyncCall, llm_scope

        context = {"language": "en", "rows": ROWS, "stageContext": {"stageSnapshot": {"versionId": "v1"}}}
        captured_tasks = []
        plan_step = {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
        }

        async def fake_request(*args, **kwargs):
            captured_tasks.append(kwargs["task"])
            messages = args[3]
            gen_sys = messages[0]["content"]
            self.assertIn("NEVER emit requirements or automaticBindings for outer_shell, player, box/boxes, or target/targets", gen_sys)
            self.assertIn("keep scope='all' by default", gen_sys)
            self.assertIn("action='protected_refusal'", gen_sys)
            self.assertIn("planStrategies", gen_sys)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(plan_step)))])

        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=fake_request)):
            result, _ = binary_model.generate(topic(), context, validate, None, "binary-single-call", time.monotonic() + 116)
        self.assertEqual(result["action"], "plan")
        self.assertEqual(captured_tasks, ["proposal_clarification"])

    def test_normalize_requirement_record_and_deterministic_choice_compilation(self):
        from cocreation.domain.design_requirements import normalize_requirement_record, validate_requirement_record

        turns = [{"id": "u1", "content": "加一面内墙增加绕路，保持玩家和箱子位置不变"}]
        raw_payload = {
            "requirements": [
                {
                    "component": "player", "property": "positions", "relation": "preserve",
                    "strength": "invariant", "entities": [], "value": None, "unit": "none",
                    "priorityEvidenceSpan": "", "focused": False, "sourceTurnId": "u1",
                    "evidenceSpan": "保持玩家", "statement": "保持玩家位置不变", "scope": "all",
                },
                {
                    "component": "wall", "property": "count", "relation": "increase",
                    "strength": "goal", "entities": [], "value": None, "unit": "none",
                    "priorityEvidenceSpan": "paraphrased priority", "focused": True, "sourceTurnId": "wrong-id",
                    "evidenceSpan": "non-verbatim span", "statement": "增加内部墙体", "scope": "original_cells",
                },
                {
                    "component": "gameplay", "property": "solutionSteps", "relation": "increase",
                    "strength": "goal", "entities": [], "value": None, "unit": "none",
                    "priorityEvidenceSpan": "", "focused": True, "sourceTurnId": "u1",
                    "evidenceSpan": "增加绕路", "statement": "增加绕路", "scope": "original_cells",
                },
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
        normalized, notes = normalize_requirement_record(raw_payload, turns, {})
        self.assertIn("dropped_platform_protection", notes)
        self.assertIn("bound_single_source_turn", notes)
        self.assertIn("normalized_qualitative_evidence_span", notes)
        self.assertIn("coerced_scope_all", notes)
        self.assertIn("cleared_nonverbatim_priority_span", notes)
        self.assertEqual(len(normalized["requirements"]), 2)
        self.assertEqual(normalized["requirements"][0]["sourceTurnId"], "u1")
        self.assertEqual(normalized["requirements"][0]["evidenceSpan"], turns[0]["content"])
        self.assertEqual(normalized["requirements"][1]["scope"], "all")
        record = validate_requirement_record(normalized, turns, {})
        self.assertEqual(len(record["requirements"]), 2)

        # Numeric value without exact numeric span MUST remain strict and still fail validate_requirement_record.
        strict_num = copy.deepcopy(raw_payload["requirements"][1])
        strict_num.update(sourceTurnId="u1", value=2, unit="delta", evidenceSpan="non-numeric paraphrase", scope="all")
        num_norm, _ = normalize_requirement_record({"requirements": [strict_num], "automaticBindings": [], "exactTransitions": []}, turns, {})
        with self.assertRaises(ValueError):
            validate_requirement_record(num_norm, turns, {})

        # Choice compilation covers all 4 promise kinds cleanly.
        choice_turns = [
            *turns,
            {"id": "c1", "content": "Selected A: Less water"},
            {"id": "c2", "content": "Selected A: Widen wall gap"},
            {"id": "c3", "content": "Selected B: Shorter route"},
            {"id": "c4", "content": "Selected B: Reshape water"},
        ]
        choices = [
            {"sourceTurnId": "c1", "label": "Less water", "promise": {"kind": "reduce_obstacles", "component": "water", "focus": None, "corridor": None}},
            {"sourceTurnId": "c2", "label": "Widen wall gap", "promise": {"kind": "widen_corridor", "component": "internal_walls", "focus": None, "corridor": {"row": 5, "column": 6, "axis": "horizontal"}}},
            {"sourceTurnId": "c3", "label": "Shorter route", "promise": {"kind": "shorter_solution", "component": "obstacles", "focus": None, "corridor": None}},
            {"sourceTurnId": "c4", "label": "Reshape water", "promise": {"kind": "reshape_water", "component": "water", "focus": None, "corridor": None}},
        ]
        compiled_choices = binary.compile_choice_requirements_deterministic(choices, choice_turns, "en")
        merged = validate_requirement_record({"requirements": [*normalized["requirements"], *compiled_choices], "automaticBindings": [], "exactTransitions": []}, choice_turns, {})
        binary.require_choice_coverage(merged, choices)


class BinaryHTTPTests(SessionTestCase):
    create_and_open_session = legacy.CoCreationSessionTests.create_and_open_session
    _fake_turn_understanding = staticmethod(legacy.CoCreationSessionTests._fake_turn_understanding)

    def setUp(self):
        self.open_workspace(
            settings_overrides={"binary_discovery_enabled": True},
            llm_handlers={
                "classify_turn_understanding": self._fake_turn_understanding,
                "review_proposal_requirements": {
                    "requirements": [], "automaticBindings": [], "exactTransitions": [],
                },
                "generate_binary_step": self.generate,
            },
        )
        self.session_id, self.integration_token = self.create_and_open_session()
        self.version = self.client.get(f"/api/sessions/{self.session_id}").json()["currentVersionId"]
        # Existing session fixture is an open map; add two legal blocked cells to its immutable test Stage.
        with self.services.connect(immediate=True) as db:
            db.execute("UPDATE level_versions SET rows_json=?, validation_json=? WHERE id=?",
                (json.dumps(ROWS), json.dumps(validate_and_solve(ROWS).as_dict()), self.version))

    def generate(self, current, context, validator, strategy_validator, request_id, deadline, budget=20):
        result = step()
        context["stageContext"]["requirementRecord"] = current.get("requirements") or result["initialRequirements"]
        if current.get("requirements") is not None: result["initialRequirements"] = None
        caches = binary.preflight(result, current, context["rows"], context["stageContext"]["stageSnapshot"],
            validator, deadline, strategy_validator, budget, clock=self.services.clock)
        return result, caches

    def start(self):
        response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
            "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def choice(self, state, key="binary-click"):
        question = state["proposalFlowState"]["pendingQuestion"]
        return {"baseVersionId": self.version, "topicId": question["topicId"], "questionId": question["questionId"],
                "choiceId": question["choices"][0]["choiceId"], "displayLanguage": "en", "idempotencyKey": key}

    def test_question_projection_free_text_rejection_cancel_and_start_receipt(self):
        state = self.start()
        self.assertEqual(state["proposalFlowState"]["clarificationQuestionCount"], 1)
        self.assertNotIn('"candidate"', json.dumps(state))
        replay = self.start()
        self.assertEqual(len(state["turns"]), len(replay["turns"]))
        free = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
            "content": "A", "idempotencyKey": "illegal-text"})
        self.assertEqual(free.status_code, 409, free.text)
        result = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/cancel", json={
            "baseVersionId": self.version, "topicId": state["proposalFlowState"]["topicId"], "idempotencyKey": "cancel"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["proposalFlowState"]["active"])
        self.assertEqual(result.json()["currentVersionId"], self.version)

    def test_failed_continuation_keeps_click_exactly_once_and_rejects_old_buttons(self):
        state = self.start(); payload = self.choice(state)
        failure = llm.LLMServiceError("BINARY_PREFLIGHT_NOT_FOUND", "Retry", "fixture", True, 1, 502)
        with self.using_dependencies(llm_handlers={"generate_binary_step": failure}):
            for _ in range(2):
                response = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
                self.assertEqual(response.status_code, 502, response.text)
        latest = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(latest["proposalFlowState"]["status"], "retry_pending")
        self.assertEqual(latest["proposalFlowState"]["clarificationQuestionCount"], 1)
        self.assertEqual(sum(t["requestId"] == "binary-click" for t in latest["turns"]), 1)
        payload["idempotencyKey"] = "second-tab"
        stale = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_final_purple_freezes_selected_candidate_and_preview_replays_it(self):
        state = self.start(); payload = self.choice(state)
        def planner(**kwargs):
            stage = kwargs["stage_context"]
            cached = stage.get("verifiedRequirementCache") or []
            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0]["baseVersionId"], self.version)
            self.assertEqual(cached[0]["record"].get("interpretationVersion"), 2)
            with self.services.connect() as db:
                current = binary.current_topic(db, self.session_id, self.version)
            cache = current["selectedCandidate"]
            from cocreation.domain.proposal_search import parse_revision_plan
            plan = parse_revision_plan({"strategies": [cache["strategy"]]})
            contract = llm._build_revision_execution_contract(plan, current["initialRequest"], stage)
            from cocreation.api import binary_routes
            return llm.LLMExecutionResult("", 1, "fixture-final", model="offline",
                proposed_rows=cache["rows"], revision_contract=contract, revision_plan=plan.as_dict(),
                revision_operations=binary_routes._operations(ROWS, cache["rows"]), proposal_diagnostics={"selectedStrategyIndex": 1})
        with self.using_dependencies(
            llm_handlers={
                "generate_binary_step": ({'action': 'plan', 'question': '', 'note': '', 'branches': []}, []),
            },
            llm_options={"revision_search_generator": RecordedCall(side_effect=planner)},
        ):
            response = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertEqual(state["proposalFlowState"]["status"], "proposal_ready")
        offer_turn = state["turns"][-1]
        offer = offer_turn["guidance"]["proposalOffer"]
        self.assertTrue(offer["proposalPresentation"]["binaryCompact"])
        self.assertEqual(len(offer["proposalPresentation"]["verifiedActions"]), 1)
        response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={
            "baseVersionId": self.version, "content": "Preview the verified proposal.", "idempotencyKey": "binary-preview",
            "action": "execute_revision", "sourceTurnId": offer_turn["turnId"]})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["currentVersionId"], self.version)
        self.assertEqual(len(preview["proposals"]), 1)
        proposal = preview["proposals"][0]
        source = next(t for t in preview["turns"] if t["turnId"] == offer_turn["turnId"])
        self.assertTrue(source["guidance"]["proposalOffer"]["proposalPresentation"]["binaryCompact"])
        adopted = self.client.post(f"/api/sessions/{self.session_id}/proposals/{proposal['proposalId']}/decision", json={
            "baseVersionId": self.version, "decision": "accept", "idempotencyKey": "binary-accept"})
        self.assertEqual(adopted.status_code, 200, adopted.text)
        self.assertNotEqual(adopted.json()["currentVersionId"], self.version)
        self.assertEqual(len(adopted.json()["versions"]), 2)

    def test_cancelled_failed_request_cannot_be_resumed_with_old_key(self):
        with self.using_dependencies(
            llm_handlers={
                "generate_binary_step": llm.LLMServiceError('BINARY_PREFLIGHT_NOT_FOUND', 'Retry', 'x', True, 1, 502),
            },
        ):
            failed = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
                "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(failed.status_code, 502)
        latest = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/cancel", json={
            "baseVersionId": self.version, "topicId": latest["proposalFlowState"]["topicId"], "idempotencyKey": "cancel-failed"})
        replay = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
            "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(replay.status_code, 409, replay.text)

    def test_unpublished_question_does_not_consume_count_after_commit_failure(self):
        original = session_persistence_helpers.insert_turn
        def fail_assistant(database, session, role, *args, **kwargs):
            if role == "assistant": raise RuntimeError("injected commit failure")
            return original(database, session, role, *args, **kwargs)
        with self.using_dependencies(insert_turn_fn=RecordedCall(side_effect=fail_assistant)):
            response = self.client.post(f"/api/sessions/{self.session_id}/messages", json={"baseVersionId": self.version,
                "content": "Make it simpler.", "requestProposal": True, "idempotencyKey": "binary-start"})
        self.assertEqual(response.status_code, 502, response.text)
        state = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertEqual(state["proposalFlowState"]["clarificationQuestionCount"], 0)
        self.assertIsNone(state["proposalFlowState"]["pendingQuestion"])
        retried = self.start()
        self.assertEqual(retried["proposalFlowState"]["clarificationQuestionCount"], 1)

    def test_tampered_cached_proof_rolls_back_the_choice_turn(self):
        state = self.start(); payload = self.choice(state)
        with self.services.connect(immediate=True) as db:
            current = binary.current_topic(db, self.session_id, self.version)
            current["pendingQuestion"]["choices"][0]["candidate"]["rows"] = list(ROWS)
            repository.record_event(db, self.session_id, "binary_discovery_state", current, self.services.utc_now())
        response = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        latest = self.client.get(f"/api/sessions/{self.session_id}").json()
        self.assertFalse(any(t["requestId"] == payload["idempotencyKey"] for t in latest["turns"]))
        self.assertEqual(latest["proposalFlowState"]["clarificationQuestionCount"], 1)

    def test_turn0_plan_reuses_verified_initial_requirements_cache_without_recompilation(self):
        from types import SimpleNamespace
        from cocreation.llm import proposal_planning
        from cocreation.reliability.reply_reliability import requirement_cache_key
        from tests.fakes import RecordedAsyncCall, llm_scope

        def plan_on_turn0(current, context, validator, strategy_validator, request_id, deadline, budget=20):
            return (
                {
                    "action": "plan",
                    "question": "",
                    "note": "",
                    "branches": [],
                    "initialRequirements": {
                        "requirements": [],
                        "automaticBindings": [],
                        "exactTransitions": [],
                    },
                },
                [],
            )

        reused_records = []

        def planner(**kwargs):
            stage = kwargs["stage_context"]
            with self.services.connect() as db:
                current = binary.current_topic(db, self.session_id, self.version)
            cached = stage.get("verifiedRequirementCache") or []
            self.assertEqual(len(cached), 1)
            expected_key = requirement_cache_key(stage, current["userTurns"])
            self.assertEqual(cached[0].get("cacheKey"), expected_key)
            self.assertEqual(cached[0]["record"].get("topicId"), current["topicId"])

            async def fail_if_called(*args, **kw):
                raise AssertionError("Expected cache hit with zero LLM calls")

            with llm_scope(completion_requester=RecordedAsyncCall(side_effect=fail_if_called)):
                reused = proposal_planning._review_proposal_requirements(
                    "offline",
                    "offline",
                    stage,
                    "turn0-cache-reuse",
                    time.monotonic() + 116.0,
                )
            reused_records.append(reused)
            from cocreation.domain.proposal_search import parse_revision_plan
            from cocreation.api import binary_routes

            strategy = {
                "effect": "open_route",
                "focus": {"row": 3, "column": 3, "radius": 1},
                "operators": ["remove_water"],
                "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
                "editBudget": 1,
                "metricGoals": [],
            }
            candidate_rows = list(ROWS)
            candidate_rows[2] = "#.......@..#"
            plan = parse_revision_plan({"strategies": [strategy]})
            contract = llm._build_revision_execution_contract(plan, current["initialRequest"], stage)
            return llm.LLMExecutionResult(
                "",
                1,
                "fixture-turn0-plan",
                model="offline",
                proposed_rows=candidate_rows,
                revision_contract=contract,
                revision_plan=plan.as_dict(),
                revision_operations=binary_routes._operations(ROWS, candidate_rows),
                proposal_diagnostics={"selectedStrategyIndex": 1},
            )

        with self.using_dependencies(
            llm_handlers={"generate_binary_step": plan_on_turn0},
            llm_options={"revision_search_generator": RecordedCall(side_effect=planner)},
        ):
            response = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "baseVersionId": self.version,
                    "content": "Remove the left water tile directly.",
                    "requestProposal": True,
                    "idempotencyKey": "turn0-plan-cache",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(reused_records), 1)
        self.assertEqual(response.json()["proposalFlowState"]["status"], "proposal_ready")

    def test_request_proposal_protected_refusal_runs_single_call_and_cancels_topic(self):
        understanding_calls = []

        def fail_if_understanding_called(*args, **kwargs):
            understanding_calls.append((args, kwargs))
            raise AssertionError("requestProposal=True should bypass classify_turn_understanding on direct entry")

        refusal_step = (
            {
                "action": "protected_refusal",
                "question": "",
                "note": "",
                "branches": [],
                "planStrategies": [],
                "protectedEntities": ["boxes"],
                "initialRequirements": None,
            },
            [],
        )
        with self.using_dependencies(
            llm_handlers={
                "classify_turn_understanding": fail_if_understanding_called,
                "generate_binary_step": refusal_step,
            }
        ):
            res = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "baseVersionId": self.version,
                    "content": "Move B1 two cells to the right.",
                    "requestProposal": True,
                    "idempotencyKey": "binary-protected-refusal",
                },
            )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(understanding_calls, [])
        body = res.json()
        self.assertFalse(body["proposalFlowState"]["active"])
        self.assertEqual(len(body["versions"]), 1)
        self.assertIn("boxes", body["turns"][-1]["content"].lower())
        self.assertIsNone(body["turns"][-1]["guidance"].get("proposalOffer"))

    def test_turn0_plan_reuses_plan_strategies_and_falls_back_on_operation_timeout(self):
        import asyncio
        from types import SimpleNamespace
        from cocreation.llm.service import CoCreationLLMService
        from tests.fakes import RecordedAsyncCall

        strategy = {
            "effect": "open_route",
            "focus": {"row": 3, "column": 3, "radius": 1},
            "operators": ["remove_water"],
            "preserve": ["outer_shell", "player", "boxes", "targets", "walls"],
            "editBudget": 2,
            "metricGoals": [],
        }
        plan_step = {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [strategy],
            "protectedEntities": [],
            "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
        }
        seen_tasks = []

        async def fake_request(*args, **kwargs):
            seen_tasks.append(kwargs["task"])
            if kwargs["task"] == "proposal_clarification":
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(plan_step)))])
            if kwargs["task"] == "operation_candidates":
                raise asyncio.TimeoutError("simulated operation_candidates timeout")
            raise AssertionError(f"Unexpected LLM task: {kwargs['task']}")

        with self.using_dependencies(
            llm_handlers={
                "generate_binary_step": lambda *a, **kw: CoCreationLLMService.generate_binary_step(self.services.llm, *a, **kw),
                "review_proposal_requirements": lambda *a, **kw: CoCreationLLMService.review_proposal_requirements(self.services.llm, *a, **kw),
            },
            llm_options={"completion_requester": RecordedAsyncCall(side_effect=fake_request)},
        ):
            res = self.client.post(
                f"/api/sessions/{self.session_id}/messages",
                json={
                    "baseVersionId": self.version,
                    "content": "Remove the left water obstacle.",
                    "requestProposal": True,
                    "idempotencyKey": "turn0-plan-fallback",
                },
            )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["proposalFlowState"]["status"], "proposal_ready")
        self.assertTrue(body["turns"][-1]["guidance"]["proposalOffer"])
        self.assertEqual(seen_tasks, ["proposal_clarification", "operation_candidates", "operation_candidates"])

    def test_post_click_plan_merges_choices_and_falls_back_to_selected_candidate_on_timeout(self):
        from types import SimpleNamespace
        from cocreation.llm.service import CoCreationLLMService
        from tests.fakes import RecordedAsyncCall
        import asyncio

        state = self.start()
        payload = self.choice(state)
        seen_tasks = []
        plan_step = {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": None,
        }

        async def fake_request(*args, **kwargs):
            seen_tasks.append(kwargs["task"])
            if kwargs["task"] == "proposal_clarification":
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(plan_step)))])
            if kwargs["task"] == "operation_candidates":
                raise asyncio.TimeoutError("simulated post-click operation timeout")
            raise AssertionError(f"Unexpected LLM task: {kwargs['task']}")

        with self.using_dependencies(
            llm_handlers={
                "generate_binary_step": lambda *a, **kw: CoCreationLLMService.generate_binary_step(self.services.llm, *a, **kw),
                "review_proposal_requirements": lambda *a, **kw: CoCreationLLMService.review_proposal_requirements(self.services.llm, *a, **kw),
            },
            llm_options={"completion_requester": RecordedAsyncCall(side_effect=fake_request)},
        ):
            res = self.client.post(f"/api/sessions/{self.session_id}/proposal-discovery/choices", json=payload)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["proposalFlowState"]["status"], "proposal_ready")
        offer = body["turns"][-1]["guidance"]["proposalOffer"]
        self.assertTrue(offer["proposalPresentation"]["binaryCompact"])
        self.assertEqual(seen_tasks, ["proposal_clarification", "operation_candidates", "operation_candidates"])

    def test_normalize_requirement_drops_scenario_b_platform_locks_and_generic_bindings(self):
        from cocreation.domain.design_requirements import normalize_requirement_record, validate_requirement_record

        user_turns = [{"id": "t-1", "content": "在中间多加一面内部墙增加一点绕路，保持玩家和箱子位置不变。"}]
        snapshot = {
            "entities": [
                {"id": "P", "label": "P"},
                {"id": "B1", "label": "B1"},
                {"id": "T1", "label": "T1"},
            ]
        }
        payload = {
            "requirements": [
                {
                    "component": "wall",
                    "property": "count",
                    "relation": "increase",
                    "strength": "preference",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "在中间多加一面内部墙增加一点绕路",
                    "focused": False,
                    "sourceTurnId": "t-1",
                    "evidenceSpan": "在中间多加一面内部墙增加一点绕路",
                    "statement": "在中间多加一面内部墙增加一点绕路",
                    "scope": "all",
                },
                {
                    "component": "player",
                    "property": "positions",
                    "relation": "increase",
                    "strength": "preference",
                    "entities": ["player"],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "保持玩家和箱子位置不变",
                    "focused": False,
                    "sourceTurnId": "t-1",
                    "evidenceSpan": "保持玩家和箱子位置不变",
                    "statement": "保持玩家和箱子位置不变",
                    "scope": "all",
                },
                {
                    "component": "box",
                    "property": "positions",
                    "relation": "invariant",
                    "strength": "hard",
                    "entities": [],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "保持玩家和箱子位置不变",
                    "focused": False,
                    "sourceTurnId": "t-1",
                    "evidenceSpan": "保持玩家和箱子位置不变",
                    "statement": "保持玩家和箱子位置不变",
                    "scope": "all",
                },
            ],
            "automaticBindings": [
                {"entity": "player", "reason": "explicit_mention"},
                {"entity": "box", "reason": "explicit_mention"},
            ],
            "exactTransitions": [],
        }
        normalized, notes = normalize_requirement_record(payload, user_turns, snapshot)
        self.assertIn("dropped_platform_protection", notes)
        self.assertIn("dropped_generic_entity_label", notes)
        validated = validate_requirement_record(normalized, user_turns, snapshot)
        self.assertEqual(len(validated["requirements"]), 1)
        self.assertEqual(validated["requirements"][0]["component"], "wall")
        self.assertEqual(validated["automaticBindings"], [])

        hallucinated = {
            "requirements": [
                {**payload["requirements"][0], "entities": ["B9"]},
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
        norm_hallucinated, _ = normalize_requirement_record(hallucinated, user_turns, snapshot)
        with self.assertRaises(ValueError):
            validate_requirement_record(norm_hallucinated, user_turns, snapshot)

        illegal_box_move = {
            "requirements": [
                {
                    "component": "box",
                    "property": "positions",
                    "relation": "relocate",
                    "strength": "goal",
                    "entities": ["B1"],
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "",
                    "focused": True,
                    "sourceTurnId": "t-1",
                    "evidenceSpan": "请把箱子 B1 往右移动两格",
                    "statement": "Move B1",
                    "scope": "all",
                }
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
        norm_move, _ = normalize_requirement_record(illegal_box_move, [{"id": "t-1", "content": "请把箱子 B1 往右移动两格"}], snapshot)
        self.assertEqual(len(norm_move["requirements"]), 1)

    def test_protected_edit_text_ignores_gameplay_phrases_and_catches_real_edits(self):
        self.assertFalse(binary._has_protected_edit_text("增加玩家绕路，让路线更有层次。"))
        self.assertFalse(binary._has_protected_edit_text("这样可以增加箱子的推拉难度。"))
        self.assertFalse(binary._has_protected_edit_text("把箱子的推进顺序改一下。"))
        self.assertFalse(binary._has_protected_edit_text("在中间多加一面内部墙增加一点绕路，保持玩家和箱子位置不变。"))

        self.assertTrue(binary._has_protected_edit_text("请把箱子 B1 往右移动两格。"))
        self.assertTrue(binary._has_protected_edit_text("可以删掉一个箱子吗？"))
        self.assertTrue(binary._has_protected_edit_text("我想拆掉右侧外墙。"))
        self.assertTrue(binary._has_protected_edit_text("在左上角增加一个目标点。"))

    def test_binary_model_exposes_editable_internal_walls_and_zero_candidate_detail(self):
        import time
        from types import SimpleNamespace
        from cocreation.domain.level_validation import build_stage_snapshot, validate_and_solve
        from cocreation.domain.proposal_search import parse_revision_plan
        from cocreation.llm import binary_model
        from tests.fakes import RecordedAsyncCall, llm_scope

        rich_rows = [
            "  ######### ",
            " ##...p..t# ",
            " #..s.....# ",
            " #.#......##",
            "##.#...#...#",
            "######.#####",
            "#t........##",
            "##@@@@.#s.# ",
            " #@@@@...## ",
            " #########  ",
        ]
        snapshot = build_stage_snapshot(rich_rows)
        snapshot_before = json.loads(json.dumps(snapshot, ensure_ascii=False))
        captured_user_payload = {}

        plan_step = {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
        }

        async def fake_request(*args, **kwargs):
            captured_user_payload.update(json.loads(args[3][1]["content"]))
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(plan_step)))]
            )

        topic = {
            "topicId": "t-walls",
            "versionId": "v1",
            "questionCount": 0,
            "choices": [],
            "userTurns": [{"id": "u1", "content": "把这张图改简单一点"}],
            "understanding": {"goals": []},
            "requirements": None,
        }
        topic["binding"] = binary.binding(snapshot, "把这张图改简单一点", {})
        context = {
            "language": "zh-CN",
            "rows": rich_rows,
            "stageContext": {"stageSnapshot": snapshot, "revisionDesignContext": {}, "entityBindings": None},
        }
        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=fake_request)):
            binary_model.generate(topic, context, validate_and_solve, None, "binary-walls-hint", time.monotonic() + 60)

        editable = captured_user_payload.get("editableInternalWallCells") or []
        self.assertEqual(editable, [{"row": 8, "column": 8}])
        self.assertNotIn({"row": 6, "column": 4}, editable)
        self.assertNotIn({"row": 5, "column": 3}, editable)
        self.assertEqual(captured_user_payload["currentStageSnapshot"], snapshot_before)

        bad_ask_step = binary.parse_step(
            {
                "action": "ask",
                "question": "您希望简化地图时，优先清理哪类障碍？",
                "note": "水池或内墙。",
                "branches": [
                    {
                        "label": "清理下方水域！",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 8, "column": 5, "radius": 2},
                            "operators": ["remove_water"],
                            "preserve": ["outer_shell", "player", "boxes", "targets", "walls", "unrelated_areas"],
                            "editBudget": 8,
                            "metricGoals": [],
                        },
                        "promise": {"kind": "reduce_obstacles", "component": "water", "focus": {"row": 8, "column": 5, "radius": 2}, "corridor": None},
                    },
                    {
                        "label": "拆掉中部内墙！",
                        "strategy": {
                            "effect": "open_route",
                            "focus": {"row": 5, "column": 4, "radius": 2},
                            "operators": ["remove_wall"],
                            "preserve": ["outer_shell", "player", "boxes", "targets", "water", "unrelated_areas"],
                            "editBudget": 4,
                            "metricGoals": [],
                        },
                        "promise": {"kind": "reduce_obstacles", "component": "internal_walls", "focus": {"row": 5, "column": 4, "radius": 2}, "corridor": None},
                    },
                ],
                "planStrategies": [],
                "protectedEntities": [],
                "initialRequirements": None,
            },
            0,
        )
        with self.assertRaises(binary.BinaryError) as exc_ctx:
            binary.preflight(
                bad_ask_step,
                topic,
                rich_rows,
                snapshot,
                lambda cand, _dl=None: validate_and_solve(cand),
                time.monotonic() + 30,
                None,
                15.0,
            )
        self.assertEqual(exc_ctx.exception.code, "BINARY_PREFLIGHT_NOT_FOUND")
        self.assertIn("zero constructible editable candidates", exc_ctx.exception.detail or "")

    def test_normalize_requirement_repairs_missing_patch_fields_and_turn0_prompt_rule(self):
        import time
        from types import SimpleNamespace
        from cocreation.domain.design_requirements import normalize_requirement_record, validate_requirement_record
        from cocreation.domain.level_validation import build_stage_snapshot, validate_and_solve
        from cocreation.llm import binary_model
        from tests.fakes import RecordedAsyncCall, llm_scope

        user_turns = [{"id": "turn-uuid-1", "content": "把这张图改简单一点，路线更清楚一些。"}]
        rows = [
            "  ######### ",
            " ##...p..t# ",
            " #..s.....# ",
            " #.#......##",
            "##.#...#...#",
            "######.#####",
            "#t........##",
            "##@@@@.#s.# ",
            " #@@@@...## ",
            " #########  ",
        ]
        snapshot = build_stage_snapshot(rows)
        patch_like_payload = {
            "requirements": [
                {
                    "component": "gameplay",
                    "property": "solutionSteps",
                    "relation": "decrease",
                    "strength": "goal",
                    "scope": "all",
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "把这张图改简单一点，路线更清楚一些。",
                    "entities": [],
                },
                {
                    "component": "gameplay",
                    "property": "experience",
                    "relation": "seek",
                    "strength": "goal",
                    "scope": "all",
                    "value": None,
                    "unit": "none",
                    "priorityEvidenceSpan": "路线更清楚一些",
                    "entities": [],
                },
            ],
            "automaticBindings": [],
            "exactTransitions": [],
        }
        normalized, notes = normalize_requirement_record(patch_like_payload, user_turns, snapshot)
        self.assertIn("defaulted_focused", notes)
        self.assertIn("defaulted_statement", notes)
        self.assertIn("bound_single_source_turn", notes)
        validated = validate_requirement_record(normalized, user_turns, snapshot)
        self.assertEqual(len(validated["requirements"]), 2)
        self.assertEqual(validated["requirements"][1]["evidenceSpan"], "路线更清楚一些")

        seen_systems = []
        plan_step = {
            "action": "plan",
            "question": "",
            "note": "",
            "branches": [],
            "planStrategies": [],
            "protectedEntities": [],
            "initialRequirements": {"requirements": [], "automaticBindings": [], "exactTransitions": []},
        }

        async def fake_request(*args, **kwargs):
            seen_systems.append(args[3][0]["content"])
            out = dict(plan_step)
            if len(seen_systems) == 2:
                out["initialRequirements"] = None
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(out)))]
            )

        topic = {
            "topicId": "t-turn0",
            "versionId": "v1",
            "questionCount": 0,
            "choices": [],
            "userTurns": user_turns,
            "understanding": {"goals": []},
            "requirements": None,
        }
        snap = snapshot
        topic["binding"] = binary.binding(snap, "把这张图改简单一点", {})
        context = {
            "language": "zh-CN",
            "rows": rows,
            "stageContext": {"stageSnapshot": snap, "revisionDesignContext": {}, "entityBindings": None},
        }
        with llm_scope(completion_requester=RecordedAsyncCall(side_effect=fake_request)):
            binary_model.generate(topic, context, validate_and_solve, None, "turn0-rule", time.monotonic() + 60)
            topic["requirements"] = validated
            binary_model.generate(topic, context, validate_and_solve, None, "turn1-rule", time.monotonic() + 60)

        self.assertIn("CRITICAL FOR THIS TURN: verifiedInitialRequirements is null", seen_systems[0])
        self.assertIn("CRITICAL FOR THIS TURN: verifiedInitialRequirements is already set", seen_systems[1])
