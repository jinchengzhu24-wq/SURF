import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient
from openai import APITimeoutError

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sokoban_backend.config import env_float  # noqa: E402
from sokoban_backend.schemas import DEFAULT_PLAN  # noqa: E402
from sokoban_backend.services.blueprint_planner import (  # noqa: E402
    apply_selected_ha_plan,
    normalize_generation_preferences,
)
from sokoban_backend.services.dashboard_projections import (  # noqa: E402
    build_creative_ideas_payload,
    build_level_records_payload,
    build_survey_records_payload,
    filter_frontend_ha_plan_records,
    merge_level_records,
    render_level_row,
)
from sokoban_backend.services.dg_guide import normalize_dg_reflection  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakeOpenAIClient,
    create_test_app,
    create_test_container,
)


SOLVABLE_ROWS = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#....p.....#",
    "#....s.t...#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]


class SystemE2EAndFaultInjection8000Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.clock = FakeClock(initial_time=2000.0)
        self.container = create_test_container(
            self.tmp_path,
            clock=self.clock,
            dashboard_delete_password="pw123",
            cocreation_intention_sync_secret="sync-secret",
        )
        self.app = create_test_app(self.tmp_path, container=self.container)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.container.close()
        self.temp_dir.cleanup()

    def _headers(self, player):
        return {"X-Player-Token": player["playerToken"]}

    def _create_ready_room(self):
        host = self.client.post(
            "/online/rooms",
            json={"studySessionId": "study-1", "menuStartedFlow": True},
        ).json()
        guest = self.client.post(
            "/online/rooms/join",
            json={
                "roomCode": host["roomCode"],
                "studySessionId": "study-2",
                "menuStartedFlow": True,
            },
        ).json()
        for player in (host, guest):
            res = self.client.post(
                f"/online/rooms/{host['matchId']}/ready",
                headers=self._headers(player),
                json={"ready": True},
            )
            self.assertEqual(res.status_code, 200)
        return host, guest

    def test_rename_routes_require_delete_password(self):
        self.client.post(
            "/record-level-start",
            json={
                "officialRound": True,
                "gameRoundId": "round-1",
                "levelRunId": "run-1",
                "rows": SOLVABLE_ROWS,
            },
        )
        unauth_round = self.client.post(
            "/rename-round",
            json={"roundId": "round-1", "displayName": "Attacked"},
        )
        unauth_run = self.client.post(
            "/rename-level-run",
            json={"levelRunId": "run-1", "displayName": "Attacked"},
        )
        self.assertIn(unauth_round.status_code, (401, 403))
        self.assertIn(unauth_run.status_code, (401, 403))

        ok_round = self.client.post(
            "/rename-round",
            headers={"X-Delete-Password": "pw123"},
            json={"roundId": "round-1", "displayName": "Renamed Round"},
        )
        ok_run = self.client.post(
            "/rename-level-run",
            headers={"X-Delete-Password": "pw123"},
            json={"levelRunId": "run-1", "displayName": "Renamed Run"},
        )
        self.assertEqual(ok_round.status_code, 200)
        self.assertEqual(ok_run.status_code, 200)

    def test_non_finite_json_rejected_and_malformed_jsonl_handled(self):
        bad_survey = self.client.post(
            "/record-survey-response",
            content='{"officialRound":true,"responseId":"s-nan","durationSeconds":NaN}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(bad_survey.status_code, 400)

        bad_level = self.client.post(
            "/record-level-end",
            content='{"officialRound":true,"levelRunId":"r-inf","durationSeconds":Infinity}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(bad_level.status_code, 400)

        # Pre-existing corrupt JSONL lines containing NaN must not crash dashboards.
        self.container.store.ensure_study_log_dir()
        self.container.paths.survey_log_file.write_text(
            '{"officialRound":true,"responseId":"bad","durationSeconds":NaN}\n'
            '{"officialRound":true,"responseId":"ok","durationSeconds":12.5,"surveyId":"post"}\n',
            encoding="utf-8",
        )
        res = self.client.get("/survey-records-data")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["summary"]["responseCount"], 1)
        self.assertEqual(body["malformedCount"], 1)

    def test_dashboard_projections_handle_weird_json_types_and_bool_metrics(self):
        events = [
            {
                "officialRound": True,
                "eventType": "level-start",
                "levelRunId": "run-weird",
                "sessionId": {"unhashable": 1},
                "gameRoundId": "g1",
                "timestamp": 1700000000,
                "structure": "not-a-dict",
                "rows": "not-a-list",
            },
            {
                "officialRound": True,
                "eventType": "level-end",
                "levelRunId": "run-weird",
                "completed": True,
                "durationSeconds": True,
                "moveCount": True,
                "pushCount": False,
            },
            {
                "officialRound": True,
                "eventType": "level-start",
                "levelRunId": "run-normal",
                "sessionId": "s-ok",
                "gameRoundId": "g2",
                "timestamp": "2026-11-25T10:00:00Z",
                "rows": SOLVABLE_ROWS,
            },
        ]
        levels = merge_level_records(events)
        payload = build_level_records_payload(events, levels, 0)
        self.assertEqual(payload["summary"]["sessionCount"], 1)
        self.assertEqual(payload["summary"]["averageDurationSeconds"], 0)
        self.assertEqual(payload["summary"]["totalMoves"], 0)
        self.assertEqual(payload["summary"]["totalPushes"], 0)
        html_row = render_level_row(levels[0])
        self.assertIn("run-weird"[:0], html_row)

        survey_payload = build_survey_records_payload(
            [{"sessionId": ["bad"], "durationSeconds": True, "surveyId": "s"}],
            0,
        )
        self.assertEqual(survey_payload["summary"]["sessionCount"], 0)
        self.assertEqual(survey_payload["summary"]["averageDurationSeconds"], 0)

        ideas_payload = build_creative_ideas_payload(
            [{"sessionId": {"bad": 1}, "ideaId": "i1"}],
            0,
        )
        self.assertEqual(ideas_payload["summary"]["sessionCount"], 0)

    def test_online_ready_regression_blocked_and_sync_atomicity(self):
        host, _ = self._create_ready_room()
        match_id = host["matchId"]

        # Once both players are ready (waiting_for_challenges), ready=False is blocked.
        regress = self.client.post(
            f"/online/rooms/{match_id}/ready",
            headers=self._headers(host),
            json={"ready": False},
        )
        self.assertEqual(regress.status_code, 409)

        # Record pending draft, then send invalid first_stage (stageNumber=2).
        draft_res = self.client.post(
            f"/online/rooms/{match_id}/draft",
            headers=self._headers(host),
            json={"finalDifficulty": "Easy", "finalLayout": "Compact"},
        )
        self.assertEqual(draft_res.status_code, 200)

        sync_headers = {"X-CoCreation-Sync-Secret": "sync-secret"}
        bad_first = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers=sync_headers,
            json={
                "eventId": "ev-bad-first",
                "playerNumber": 1,
                "eventType": "first_stage",
                "sessionId": "sess-1",
                "versionId": "v1",
                "stageNumber": 2,
                "initialDraftMethod": "description_generation",
                "rows": SOLVABLE_ROWS,
            },
        )
        self.assertEqual(bad_first.status_code, 400)
        flow_path = self.container.paths.online_match_flow_dir / match_id / "player-1.jsonl"
        self.assertFalse(flow_path.exists())

        # Retry with valid first_stage succeeds and materializes pending draft once.
        good_first = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers=sync_headers,
            json={
                "eventId": "ev-good-first",
                "playerNumber": 1,
                "eventType": "first_stage",
                "sessionId": "sess-1",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "description_generation",
                "rows": SOLVABLE_ROWS,
            },
        )
        self.assertEqual(good_first.status_code, 200)

        # Negative stageNumber, NaN diff, and bool playerNumber rejected cleanly.
        neg_stage = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers=sync_headers,
            json={
                "eventId": "ev-neg-stage",
                "playerNumber": 1,
                "eventType": "stage",
                "sessionId": "sess-1",
                "versionId": "v2",
                "stageNumber": 0,
                "source": "manual",
                "rows": SOLVABLE_ROWS,
            },
        )
        self.assertEqual(neg_stage.status_code, 400)

        nan_diff = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers=sync_headers,
            content=json.dumps({
                "eventId": "ev-nan-diff",
                "playerNumber": 1,
                "eventType": "stage",
                "sessionId": "sess-1",
                "versionId": "v2",
                "stageNumber": 2,
                "source": "manual",
                "rows": SOLVABLE_ROWS,
                "diff": [{"x": 1}],
            }).replace('"x": 1', '"x": NaN'),
            headers_extra=None,
        ) if False else self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers={**sync_headers, "Content-Type": "application/json"},
            content='{"eventId":"ev-nan-diff","playerNumber":1,"eventType":"stage","sessionId":"sess-1","versionId":"v2","stageNumber":2,"source":"manual","rows":' + json.dumps(SOLVABLE_ROWS) + ',"diff":[{"x":NaN}]}',
        )
        self.assertEqual(nan_diff.status_code, 400)

        bool_player = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers=sync_headers,
            json={
                "eventId": "ev-bool",
                "playerNumber": True,
                "eventType": "opening",
                "sessionId": "sess-1",
                "versionId": "v1",
                "stageNumber": 1,
                "assistantTurnId": "t1",
                "assistantText": "Hello",
                "language": "en",
            },
        )
        self.assertEqual(bool_player.status_code, 422)

        # Invalid eventId in designer-intention must not mutate room intention.
        bad_intent = self.client.post(
            f"/online/rooms/{match_id}/designer-intention",
            headers=sync_headers,
            json={
                "playerNumber": 1,
                "designerIntention": "First draft",
                "eventId": "bad event id!",
                "sessionId": "sess-1",
            },
        )
        self.assertEqual(bad_intent.status_code, 400)
        ok_intent = self.client.post(
            f"/online/rooms/{match_id}/designer-intention",
            headers=sync_headers,
            json={
                "playerNumber": 1,
                "designerIntention": "Updated intention after retry",
                "eventId": "good-event-id",
                "sessionId": "sess-1",
            },
        )
        self.assertEqual(ok_intent.status_code, 200)
        self.assertEqual(
            self.container.room_manager.rooms[match_id]["players"][0]["designerIntention"],
            "Updated intention after retry",
        )

    def test_planner_ha_prevalidation_and_dg_guide_fixes(self):
        # LLM hallucinating (0, 0) water/walls must fail validate_plan without explicit request.
        hallucinated = copy.deepcopy(DEFAULT_PLAN)
        hallucinated.update(minWaterAreas=0, maxWaterAreas=0, minWallObstacleBlocks=0, maxWallObstacleBlocks=0)
        with self.assertRaisesRegex(ValueError, "explicitly requests no water"):
            apply_selected_ha_plan(hallucinated, {}, {"noWater": False, "noInternalWalls": False}, {})

        # Explicit generationPreferences overrides work.
        prefs = normalize_generation_preferences({"minWaterAreas": 0, "maxWaterAreas": 0})
        allowed = apply_selected_ha_plan(hallucinated | {"minWallObstacleBlocks": 2, "maxWallObstacleBlocks": 3}, {}, {"noWater": False, "noInternalWalls": False}, prefs)
        self.assertEqual((allowed["minWaterAreas"], allowed["maxWaterAreas"]), (0, 0))

        # Invalid corridorPlacement='none' combinations fail 400 before LLM call.
        for bad_pref in (
            {"corridorPlacement": "none", "corridorPriority": "required"},
            {"corridorPlacement": "none", "corridorRole": "required_box_route"},
        ):
            res = self.client.post("/generate-level-plan", json={"generationPreferences": bad_pref})
            self.assertEqual(res.status_code, 400)
        self.assertEqual(len(self.container.llm_gateway.calls), 0)

        # Explicit corridor overrides styleDescription="no internal walls".
        corridor_plan = copy.deepcopy(DEFAULT_PLAN)
        corridor_plan.update(
            corridorPlacement="center",
            corridorWidth=1,
            corridorOrientation="vertical",
            corridorRole="player_route",
            corridorPriority="required",
        )
        self.container.llm_gateway.queue(corridor_plan)
        plan_res = self.client.post(
            "/generate-level-plan",
            json={
                "styleDescription": "An open map with no internal walls.",
                "generationPreferences": {"corridorPlacement": "center", "corridorWidth": 1},
            },
        )
        self.assertEqual(plan_res.status_code, 200)
        self.assertEqual(plan_res.json()["corridorPlacement"], "center")

        # Invalid previousLevelPlan / missing option id/title fail 400 before LLM calls.
        self.container.llm_gateway.calls.clear()
        bad_ha = self.client.post(
            "/generate-ha-revision-plans",
            json={"adjustmentText": "Make it harder", "previousLevelPlan": {"invalidKey": 1}},
        )
        self.assertEqual(bad_ha.status_code, 400)
        bad_edit = self.client.post(
            "/revise-ha-revision-plan",
            json={
                "adjustmentText": "Make it harder",
                "editedDescription": "Add more steps",
                "previousLevelPlan": DEFAULT_PLAN,
                "originalOption": {"id": "", "title": "Opt", "promptText": '{"changes":{"minPushes":12},"preserveUnlisted":true}'},
            },
        )
        self.assertEqual(bad_edit.status_code, 400)
        self.assertEqual(len(self.container.llm_gateway.calls), 0)

        # All 6 DG guide interjections accepted without mangling.
        for interjection in ("Got it!", "Nice!", "Sure!", "Okay!", "Alright!", "Sounds good!"):
            text = f"{interjection} Keep moving early. One box push matters. Short route fits."
            normalized = normalize_dg_reflection(text)
            self.assertEqual(normalized, text)

    def test_delete_idea_records_isolation_and_data_hygiene(self):
        self.container.paths.creative_idea_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.container.paths.creative_idea_log_file.write_text(
            json.dumps({"id": "idea-legacy", "ideaText": "Legacy"}) + "\n"
            + json.dumps({"ideaId": "idea-a", "ideaText": "A"}) + "\n",
            encoding="utf-8",
        )
        self.container.paths.survey_log_file.write_text(
            json.dumps({"responseId": "s-a", "creativeIdeaId": "idea-a", "sessionId": "shared-s"}) + "\n"
            + json.dumps({"responseId": "s-b", "creativeIdeaId": "idea-b", "sessionId": "shared-s"}) + "\n"
            + json.dumps({"responseId": "s-untagged", "sessionId": "shared-s"}) + "\n"
            + json.dumps({"responseId": "s-c", "ideaId": "idea-c", "sessionId": "other-s"}) + "\n",
            encoding="utf-8",
        )
        res_a = self.client.post(
            "/delete-idea-records",
            headers={"X-Delete-Password": "pw123"},
            json={"ideaId": "idea-a"},
        )
        self.assertEqual(res_a.status_code, 200)
        remaining_ids = {
            r["responseId"]
            for r in json.loads(
                json.dumps(self.container.store.read_survey_response_events()[0])
            )
        }
        self.assertEqual(remaining_ids, {"s-b", "s-c"})

        res_c = self.client.post(
            "/delete-idea-records",
            headers={"X-Delete-Password": "pw123"},
            json={"ideaId": "idea-c"},
        )
        self.assertEqual(res_c.status_code, 200)

        res_legacy = self.client.post(
            "/delete-idea-records",
            headers={"X-Delete-Password": "pw123"},
            json={"ideaId": "idea-legacy"},
        )
        self.assertEqual(res_legacy.status_code, 200)

        # filter_frontend_ha_plan_records does not leak unofficial records when official record has empty ideaId
        filtered = filter_frontend_ha_plan_records([
            {"eventType": "ha-plan-generation", "ideaId": "", "officialRound": True},
            {"eventType": "ha-plan-generation", "ideaId": "", "officialRound": False},
        ])
        self.assertEqual(len(filtered), 1)

        # append_creative_idea_record never stores empty ideaId
        created_idea = self.client.post(
            "/record-creative-idea",
            json={"ideaId": "", "ideaText": "Non-empty idea", "officialRound": True},
        )
        self.assertEqual(created_idea.status_code, 200)
        self.assertTrue(created_idea.json()["ideaId"].startswith("idea-"))

        # env_float rejects inf and nan
        self.assertEqual(env_float("T_INF", 1800.0, 1.0, None, env={"T_INF": "inf"}), 1800.0)
        self.assertEqual(env_float("T_NAN", 0.9, 0.0, 2.0, env={"T_NAN": "nan"}), 0.9)

    def test_llm_gateway_fault_injection_infinity_recovery_and_dg_fallback(self):
        # Exercise the REAL LLMGateway retry loop + _reject_non_finite_json_constant
        # with FakeOpenAIClient: attempt 1 returns Infinity (must trigger retry, not OverflowError/500),
        # attempt 2 returns valid clarity JSON -> overall 200.
        real_client = FakeOpenAIClient([
            '{"problemScore": Infinity, "targetScore": 1, "directionScore": 1, "detailScore": 0, "reason": "bad"}',
            '{"problemScore": 1, "targetScore": 1, "directionScore": 1, "detailScore": 1, "totalScore": 4, "isClear": true, "reason": "clear adjustment"}',
            APITimeoutError(request=None),
            APITimeoutError(request=None),
        ])
        real_container = create_test_container(
            self.tmp_path / "real_gw",
            clock=self.clock,
            deepseek_api_key="test-key",
            client_factory=real_client.as_factory(),
        )
        real_test_client = TestClient(create_test_app(self.tmp_path / "real_gw", container=real_container))
        clarity_res = real_test_client.post(
            "/validate-human-adjustment",
            json={"adjustmentText": "Make the corridor wider so pushing is clearer."},
        )
        self.assertEqual(clarity_res.status_code, 200, clarity_res.text)
        self.assertTrue(clarity_res.json()["isClear"])
        self.assertEqual(len(real_client.calls), 2)

        # DG summary upstream timeout fault injection falls back cleanly to deterministic_fallback
        dg_res = real_test_client.post(
            "/dg/guide/summary",
            json={
                "firstMovePreference": "observe_then_decide",
                "pushPlanningPreference": "consider_order",
                "spacePreference": "connected_areas",
                "routeRhythmPreference": "occasional_detours",
            },
        )
        self.assertEqual(dg_res.status_code, 200, dg_res.text)
        self.assertEqual(dg_res.json()["source"], "deterministic_fallback")
        self.assertEqual(len(real_client.calls), 3)


if __name__ == "__main__":
    unittest.main()
