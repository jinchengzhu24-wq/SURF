import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from sokoban_backend.config import ONLINE_ROOM_TTL_SECONDS, env_float
from sokoban_backend.dependencies import (
    BackendSettings,
    LLMGateway,
    require_delete_password,
)
from sokoban_backend.llm.runtime import LLMServiceError
from sokoban_backend.services.dashboard_projections import (
    aggregate_dashboard_node_events,
    build_matchmaking_records_payload,
)
from sokoban_backend.services.dg_guide import (
    normalize_dg_rationale,
    normalize_dg_reflection,
)
from tests.fakes import (
    FakeClock,
    FakeOpenAIClient,
    RecordingEventLogger,
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


class BackendBugfixRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.clock = FakeClock(initial_time=1000.0)
        self.container = create_test_container(
            self.tmp_path,
            clock=self.clock,
            dashboard_delete_password="pw123",
            cocreation_intention_sync_secret="sync-secret",
        )
        self.app = create_test_app(self.tmp_path, container=self.container)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.container.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _headers(player):
        return {"X-Player-Token": player["playerToken"]}

    def _create_ready_room(self):
        host = self.client.post("/online/rooms", json={"menuStartedFlow": True}).json()
        guest = self.client.post(
            "/online/rooms/join",
            json={"roomCode": host["roomCode"], "menuStartedFlow": True},
        ).json()
        for player in (host, guest):
            res = self.client.post(
                f"/online/rooms/{host['matchId']}/ready",
                headers=self._headers(player),
                json={"ready": True},
            )
            self.assertEqual(res.status_code, 200)
        return host, guest

    def test_leave_online_room_does_not_cancel_after_result_or_results_ready(self):
        host, guest = self._create_ready_room()
        for player in (host, guest):
            self.assertEqual(
                self.client.post(
                    f"/online/rooms/{host['matchId']}/challenge",
                    headers=self._headers(player),
                    json={"rows": SOLVABLE_ROWS, "aiAssistantMode": "description_generation"},
                ).status_code,
                200,
            )
        self.assertEqual(
            self.client.post(
                f"/online/rooms/{host['matchId']}/result",
                headers=self._headers(host),
                json={"durationSeconds": 10.0, "moveCount": 5, "minimumMoves": 3, "outcome": "completed"},
            ).status_code,
            200,
        )
        # First player already submitted their own result -> leaving must not cancel the match.
        leave_first = self.client.post(
            f"/online/rooms/{host['matchId']}/leave",
            headers=self._headers(host),
        )
        self.assertEqual(leave_first.status_code, 200)
        self.assertNotEqual(leave_first.json()["status"], "cancelled")

        # Complete match for guest -> room reaches results_ready.
        self.assertEqual(
            self.client.post(
                f"/online/rooms/{host['matchId']}/result",
                headers=self._headers(guest),
                json={"durationSeconds": 12.0, "moveCount": 6, "minimumMoves": 3, "outcome": "completed"},
            ).status_code,
            200,
        )
        leave_after = self.client.post(
            f"/online/rooms/{host['matchId']}/leave",
            headers=self._headers(guest),
        )
        self.assertEqual(leave_after.status_code, 200)
        self.assertEqual(leave_after.json()["status"], "results_ready")
        events, _ = self.container.store.read_online_match_events()
        self.assertFalse(any(event.get("eventType") == "player_left" for event in events))

    def test_cocreation_sync_endpoints_refresh_room_last_activity(self):
        host, _ = self._create_ready_room()
        match_id = host["matchId"]
        with self.container.room_manager.lock:
            self.container.room_manager.rooms[match_id]["lastActivity"] = 100.0

        self.clock.set_time(1500.0)
        flow_res = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers={"X-CoCreation-Sync-Secret": "sync-secret"},
            json={
                "eventId": "first_stage:p1",
                "eventType": "first_stage",
                "playerNumber": 1,
                "sessionId": "s1",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "description_generation",
                "rows": SOLVABLE_ROWS,
                "diff": [],
            },
        )
        self.assertEqual(flow_res.status_code, 200)
        with self.container.room_manager.lock:
            self.assertEqual(
                self.container.room_manager.rooms[match_id]["lastActivity"],
                1500.0,
            )

        self.clock.set_time(2500.0)
        intent_res = self.client.post(
            f"/online/rooms/{match_id}/designer-intention",
            headers={"X-CoCreation-Sync-Secret": "sync-secret"},
            json={
                "playerNumber": 1,
                "sessionId": "s1",
                "eventId": "intent-1",
                "designerIntention": "Teach simple pushes.",
            },
        )
        self.assertEqual(intent_res.status_code, 200)
        with self.container.room_manager.lock:
            self.assertEqual(
                self.container.room_manager.rooms[match_id]["lastActivity"],
                2500.0,
            )

    def test_cleanup_expired_online_rooms_skips_room_expired_for_terminal_rooms(self):
        host, _ = self._create_ready_room()
        match_id = host["matchId"]
        with self.container.room_manager.lock:
            self.container.room_manager.rooms[match_id]["status"] = "cancelled"
            self.container.room_manager.rooms[match_id]["lastActivity"] = 0.0
            self.container.room_manager.cleanup_expired_online_rooms(
                now=ONLINE_ROOM_TTL_SECONDS + 10.0
            )
            self.assertNotIn(match_id, self.container.room_manager.rooms)

        events, _ = self.container.store.read_online_match_events()
        self.assertFalse(any(event.get("eventType") == "room_expired" for event in events))

    def test_dg_normalization_enforces_lengths_after_prefix_and_allows_got_it(self):
        long_summary = (
            "Your first sentence explores how players read obstacles cleanly across the starting room before pushing any box at all, keeping each corridor spacious and uncluttered for steady inspection across early turns and careful spatial orientation throughout the room. "
            "Your second sentence adds connected intermediate pushes across two adjacent chambers while maintaining clear sightlines toward targets without forcing premature deadlocks or tight restarts in the opening sequence of the puzzle design."
        )
        normalized_reflection = normalize_dg_reflection(long_summary)
        self.assertLessEqual(len(normalized_reflection), 480)
        self.assertTrue(normalized_reflection.startswith("My read is that "))

        got_it = normalize_dg_reflection(
            "Got it! Let's focus on clear push chains first. A spacious layout will make routes easy to follow."
        )
        self.assertTrue(got_it.startswith("Got it!"))

        long_rationale = (
            "The player asked for clear sightlines and interconnected box pushes across two rooms with gentle recovery options when testing routes during early exploration steps inside the compact puzzle grid area and clean corridor transitions toward final goals."
        )
        normalized_rationale = normalize_dg_rationale(long_rationale, "difficulty")
        self.assertLessEqual(len(normalized_rationale), 320)
        self.assertTrue(normalized_rationale.startswith("I read this as: "))

    def test_delete_and_clear_matchmaking_evict_live_rooms(self):
        host, _ = self._create_ready_room()
        match_id = host["matchId"]
        res = self.client.post(
            "/delete-online-match",
            headers={"X-Delete-Password": "pw123"},
            json={"matchId": match_id},
        )
        self.assertEqual(res.status_code, 200)
        with self.container.room_manager.lock:
            self.assertNotIn(match_id, self.container.room_manager.rooms)

        self._create_ready_room()
        clear_res = self.client.post(
            "/clear-matchmaking-records",
            headers={"X-Delete-Password": "pw123"},
        )
        self.assertEqual(clear_res.status_code, 200)
        with self.container.room_manager.lock:
            self.assertEqual(len(self.container.room_manager.rooms), 0)

    def test_set_online_ready_blocks_status_regression_after_challenges_ready(self):
        host, guest = self._create_ready_room()
        for player in (host, guest):
            self.assertEqual(
                self.client.post(
                    f"/online/rooms/{host['matchId']}/challenge",
                    headers=self._headers(player),
                    json={"rows": SOLVABLE_ROWS, "aiAssistantMode": "description_generation"},
                ).status_code,
                200,
            )
        regress = self.client.post(
            f"/online/rooms/{host['matchId']}/ready",
            headers=self._headers(host),
            json={"ready": False},
        )
        self.assertEqual(regress.status_code, 409)

    def test_draft_ordering_and_dashboard_node_tie_breaking_and_leave_priority(self):
        host, _ = self._create_ready_room()
        match_id = host["matchId"]
        draft_payload = {
            "finalDifficulty": "Medium",
            "finalLayout": "Balanced",
            "firstMovePreference": "observe_then_decide",
            "pushPlanningPreference": "consider_order",
            "spacePreference": "connected_areas",
            "routeRhythmPreference": "occasional_detours",
            "recommendedDifficulty": "Medium",
            "recommendedLayout": "Balanced",
            "difficultySource": "llm",
            "layoutSource": "llm",
        }
        draft_res = self.client.post(
            f"/online/rooms/{match_id}/draft",
            headers=self._headers(host),
            json=draft_payload,
        )
        self.assertEqual(draft_res.status_code, 200)
        self.assertTrue(draft_res.json()["pending"])

        stage_res = self.client.post(
            f"/online/rooms/{match_id}/cocreation-events",
            headers={"X-CoCreation-Sync-Secret": "sync-secret"},
            json={
                "eventId": "first_stage:p1",
                "eventType": "first_stage",
                "playerNumber": 1,
                "sessionId": "s1",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "description_generation",
                "rows": SOLVABLE_ROWS,
                "diff": [],
            },
        )
        self.assertEqual(stage_res.status_code, 200)

        # Idempotent retry of draft after first_stage succeeds cleanly without rewriting flow.
        retry_res = self.client.post(
            f"/online/rooms/{match_id}/draft",
            headers=self._headers(host),
            json=draft_payload,
        )
        self.assertEqual(retry_res.status_code, 200)
        self.assertFalse(retry_res.json()["recorded"])
        flow_events, _ = self.container.store.read_online_match_flow_events(match_id, 1)
        draft_event = next(e for e in flow_events if e["eventType"] == "draft")
        first_stage_event = next(e for e in flow_events if e["eventType"] == "first_stage")
        self.assertLessEqual(flow_events.index(draft_event), flow_events.index(first_stage_event))
        self.assertLessEqual(draft_event["serverReceivedAt"], first_stage_event["serverReceivedAt"])

        # aggregate_dashboard_node_events preserves sequence order on equal timestamps
        nodes = aggregate_dashboard_node_events(
            [
                {
                    "eventId": "n2",
                    "eventType": "node",
                    "nodeId": "node-b",
                    "nodeType": "stage",
                    "serverReceivedAt": "2026-10-10T01:00:00Z",
                },
                {
                    "eventId": "n1",
                    "eventType": "node",
                    "nodeId": "node-a",
                    "nodeType": "stage",
                    "serverReceivedAt": "2026-10-10T01:00:00Z",
                },
            ]
        )
        self.assertEqual([node["eventId"] for node in nodes], ["n2", "n1"])

        # player_left takes precedence over room_expired in matchmaking summary status
        payload = build_matchmaking_records_payload(
            [
                {
                    "eventId": "e1",
                    "eventType": "room_created",
                    "matchId": match_id,
                    "roomCode": host["roomCode"],
                    "playerNumber": 1,
                    "menuStartedFlow": True,
                    "serverReceivedAt": "2026-10-10T01:00:00Z",
                },
                {
                    "eventId": "e2",
                    "eventType": "room_expired",
                    "matchId": match_id,
                    "roomCode": host["roomCode"],
                    "playerNumber": 0,
                    "serverReceivedAt": "2026-10-10T01:05:00Z",
                },
                {
                    "eventId": "e3",
                    "eventType": "player_left",
                    "matchId": match_id,
                    "roomCode": host["roomCode"],
                    "playerNumber": 1,
                    "menuStartedFlow": True,
                    "serverReceivedAt": "2026-10-10T01:06:00Z",
                },
            ],
            0,
            store=self.container.store,
        )
        self.assertEqual(payload["matches"][0]["status"], "cancelled")

    def test_require_delete_password_handles_non_ascii_safely(self):
        non_ascii_container = create_test_container(
            self.tmp_path / "non-ascii",
            dashboard_delete_password="密码secret",
        )
        with TestClient(
            create_test_app(self.tmp_path / "non-ascii", container=non_ascii_container)
        ) as local_client:
            res = local_client.post(
                "/verify-dashboard-password",
                headers={"X-Delete-Password": "wrong-ascii-password"},
            )
            self.assertEqual(res.status_code, 401)

        fake_req = SimpleNamespace(headers={"X-Delete-Password": "密码wrong"})
        with self.assertRaises(Exception) as ctx:
            require_delete_password(fake_req, non_ascii_container)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)
        non_ascii_container.close()

    def test_invalid_json_in_record_and_level_plan_returns_400_without_llm_call(self):
        bad_record = self.client.post(
            "/record-level-start",
            content="{not-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(bad_record.status_code, 400)

        bad_ha = self.client.post(
            "/generate-level-plan",
            json={
                "ideaText": "Test idea",
                "revisionMode": "ha",
                "selectedHAPlan": "[1, 2, 3]",
            },
        )
        self.assertEqual(bad_ha.status_code, 400)
        self.assertEqual(len(self.container.llm_gateway.calls), 0)

    def test_llm_runtime_client_cache_lru_and_empty_choices_and_env_float(self):
        created_clients = []

        def make_fake_openai(api_key, base_url, timeout):
            client = FakeOpenAIClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            created_clients.append(client)
            return client

        settings = BackendSettings(deepseek_api_key="sk-test")
        logger = RecordingEventLogger(settings=settings, clock=self.clock.as_clock())
        gateway = LLMGateway(
            settings=settings,
            client_factory=make_fake_openai,
            clock=self.clock.as_clock(),
            event_logger=logger,
        )
        c1 = gateway.get_client("k1", "https://a.test", 10.0)
        c2 = gateway.get_client("k2", "https://a.test", 10.0)
        self.assertIsNot(c1, c2)
        for idx in range(18):
            gateway.get_client(f"key-{idx}", "https://b.test", 5.0)
        self.assertLessEqual(gateway.cached_client_count, 16)
        self.assertTrue(c1.closed)
        gateway.close()

        empty_client = FakeOpenAIClient([SimpleNamespace(choices=[], usage=None)])
        empty_gateway = LLMGateway(
            settings=settings,
            client_factory=empty_client.as_factory(),
            clock=self.clock.as_clock(),
            event_logger=logger,
        )
        with self.assertRaises(LLMServiceError) as ctx:
            empty_gateway.execute_json_request(
                task="test_empty",
                messages=[{"role": "user", "content": "hi"}],
                validator=lambda obj: obj,
                temperature=0.0,
                timeout_seconds=5.0,
                max_attempts=1,
            )
        self.assertEqual(ctx.exception.code, "MODEL_VALIDATION_FAILED")
        self.assertEqual(ctx.exception.status_code, 502)

        scalar_client = FakeOpenAIClient([json.dumps("scalar")])
        scalar_gateway = LLMGateway(
            settings=settings,
            client_factory=scalar_client.as_factory(),
            clock=self.clock.as_clock(),
            event_logger=logger,
        )
        with self.assertRaises(LLMServiceError) as ctx:
            scalar_gateway.execute_json_request(
                task="test_scalar",
                messages=[{"role": "user", "content": "hi"}],
                validator=lambda obj: obj,
                temperature=0.0,
                timeout_seconds=5.0,
                max_attempts=1,
            )
        self.assertEqual(ctx.exception.code, "MODEL_VALIDATION_FAILED")

        self.assertEqual(
            env_float("TEST_EMPTY_FLOAT", 3.5, env={"TEST_EMPTY_FLOAT": ""}),
            3.5,
        )


if __name__ == "__main__":
    unittest.main()
