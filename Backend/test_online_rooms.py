import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import Backend.app as backend


SOLVABLE_ROWS_A = [
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

SOLVABLE_ROWS_B = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#...s..t...#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

FULL_DRAFT_METADATA = {
    "q1Answer": "quick_start",
    "q2Answer": "easy_to_adjust",
    "q3Answer": "focused_area",
    "q4Answer": "short_routes",
    "aiReflection": "My read is that the map should feel readable and focused.",
    "aiDifficultyRationale": "I would keep the first move readable. This supports Easy difficulty.",
    "aiLayoutRationale": "I would keep important positions close. This supports a Compact layout.",
    "aiRecommendedDifficulty": "Easy",
    "aiRecommendedLayout": "Compact",
    "aiRecommendationSource": "llm",
}


class OnlineRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        log_dir = Path(self.temp_dir.name)
        self.log_patchers = [
            patch.object(
                backend,
                "ONLINE_MATCH_LOG_FILE",
                log_dir / "online_match_events.jsonl",
            ),
            patch.object(
                backend,
                "ONLINE_MATCH_FLOW_DIR",
                log_dir / "matches",
            ),
            patch.object(
                backend,
                "SURVEY_LOG_FILE",
                log_dir / "survey_responses.jsonl",
            ),
        ]

        for patcher in self.log_patchers:
            patcher.start()

        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS.clear()
        self.client = TestClient(backend.app)

    def tearDown(self):
        self.client.close()
        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS.clear()

        for patcher in reversed(self.log_patchers):
            patcher.stop()

        self.temp_dir.cleanup()

    @staticmethod
    def auth_headers(player_token):
        return {"X-Player-Token": player_token}

    def create_room(self):
        response = self.client.post("/online/rooms")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def join_room(self, room_code):
        response = self.client.post(
            "/online/rooms/join",
            json={"roomCode": room_code},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_draft_requires_player_authentication_and_valid_final_values(self):
        host = self.create_room()
        endpoint = "/online/rooms/" + host["matchId"] + "/draft"

        missing_auth = self.client.post(
            endpoint,
            json={"finalDifficulty": "Medium", "finalLayout": "Balanced"},
        )
        invalid_value = self.client.post(
            endpoint,
            headers=self.auth_headers(host["playerToken"]),
            json={"finalDifficulty": "Random", "finalLayout": "Balanced"},
        )

        self.assertEqual(missing_auth.status_code, 401)
        self.assertEqual(invalid_value.status_code, 400)

    def test_draft_is_idempotent_and_rejects_different_confirmed_settings(self):
        host = self.create_room()
        endpoint = "/online/rooms/" + host["matchId"] + "/draft"
        headers = self.auth_headers(host["playerToken"])
        body = {
            **FULL_DRAFT_METADATA,
            "finalDifficulty": "Medium",
            "finalLayout": "Balanced",
        }

        first = self.client.post(endpoint, headers=headers, json=body)
        retry = self.client.post(endpoint, headers=headers, json=body)
        conflict = self.client.post(
            endpoint,
            headers=headers,
            json={"finalDifficulty": "Hard", "finalLayout": "Open"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["recorded"])
        self.assertTrue(first.json()["pending"])
        self.assertIsNone(first.json()["event"])
        self.assertFalse(retry.json()["recorded"])
        self.assertTrue(retry.json()["pending"])
        self.assertIsNone(retry.json()["event"])
        self.assertEqual(conflict.status_code, 409)
        flow, _ = backend.read_online_match_flow_events(host["matchId"], 1)
        self.assertEqual(flow, [])

    def test_dashboard_aggregates_draft_fields(self):
        host = self.create_room()
        body = {
            **FULL_DRAFT_METADATA,
            "finalDifficulty": "Easy",
            "finalLayout": "Compact",
        }
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/draft",
            headers=self.auth_headers(host["playerToken"]),
            json=body,
        )
        self.assertEqual(response.status_code, 200)

        first_stage = {
            "eventId": "first_stage:draft-fields",
            "eventType": "first_stage",
            "playerNumber": 1,
            "sessionId": "session-host",
            "versionId": "version-host",
            "stageNumber": 1,
            "initialDraftMethod": "description_generation",
            "rows": SOLVABLE_ROWS_A,
            "diff": [],
        }
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            self.assertEqual(
                self.client.post(
                    "/online/rooms/" + host["matchId"] + "/cocreation-events",
                    json=first_stage,
                    headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
                ).status_code,
                200,
            )

        dashboard = self.client.get("/matchmaking-records-data").json()
        record = next(item for item in dashboard["matches"] if item["matchId"] == host["matchId"])
        draft = record["players"][0]["coCreationFlow"][0]
        self.assertEqual(draft["eventType"], "draft")
        self.assertEqual(draft["finalDifficulty"], "Easy")
        self.assertEqual(draft["finalLayout"], "Compact")
        self.assertEqual(draft["q3Answer"], "focused_area")
        self.assertEqual(draft["aiRecommendedLayout"], "Compact")
        self.assertTrue(draft["draftMetadataComplete"])

    def test_old_draft_client_remains_compatible_but_is_marked_incomplete(self):
        host = self.create_room()
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/draft",
            headers=self.auth_headers(host["playerToken"]),
            json={"finalDifficulty": "Medium", "finalLayout": "Balanced"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["pending"])
        self.assertIsNone(response.json()["event"])

    def test_draft_rejects_invalid_metadata(self):
        host = self.create_room()
        endpoint = "/online/rooms/" + host["matchId"] + "/draft"
        response = self.client.post(
            endpoint,
            headers=self.auth_headers(host["playerToken"]),
            json={
                **FULL_DRAFT_METADATA,
                "q2Answer": "unknown",
                "finalDifficulty": "Medium",
                "finalLayout": "Balanced",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_draft_metadata_change_conflicts_even_when_final_values_match(self):
        host = self.create_room()
        endpoint = "/online/rooms/" + host["matchId"] + "/draft"
        headers = self.auth_headers(host["playerToken"])
        body = {
            **FULL_DRAFT_METADATA,
            "finalDifficulty": "Medium",
            "finalLayout": "Balanced",
        }
        first = self.client.post(endpoint, headers=headers, json=body)
        changed = dict(body)
        changed["aiReflection"] = "A different reflection. This must conflict."
        conflict = self.client.post(endpoint, headers=headers, json=changed)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)

    def test_create_room_returns_host_identity_and_waiting_state(self):
        room = self.create_room()

        self.assertEqual(len(room["matchId"]), 32)
        self.assertEqual(len(room["roomCode"]), 6)
        self.assertTrue(room["playerToken"])
        self.assertEqual(room["playerNumber"], 1)
        self.assertEqual(room["status"], "waiting_for_opponent")
        self.assertEqual(
            room["players"],
            [
                {
                    "playerNumber": 1,
                    "ready": False,
                    "challengeSubmitted": False,
                    "resultSubmitted": False,
                }
            ],
        )

    def test_room_events_preserve_each_players_study_session_id(self):
        host_session_id = "host-study-session"
        guest_session_id = "guest-study-session"
        host_response = self.client.post(
            "/online/rooms",
            json={"studySessionId": host_session_id},
        )
        self.assertEqual(host_response.status_code, 200)
        host = host_response.json()
        guest_response = self.client.post(
            "/online/rooms/join",
            json={
                "roomCode": host["roomCode"],
                "studySessionId": guest_session_id,
            },
        )
        self.assertEqual(guest_response.status_code, 200)
        guest = guest_response.json()

        ready_url = "/online/rooms/" + host["matchId"] + "/ready"
        self.client.post(
            ready_url,
            headers=self.auth_headers(host["playerToken"]),
            json={"ready": True},
        )
        self.client.post(
            ready_url,
            headers=self.auth_headers(guest["playerToken"]),
            json={"ready": True},
        )

        events = [
            json.loads(line)
            for line in backend.ONLINE_MATCH_LOG_FILE.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(events[0]["studySessionId"], host_session_id)
        self.assertEqual(events[1]["studySessionId"], guest_session_id)
        self.assertEqual(events[2]["studySessionId"], host_session_id)
        self.assertEqual(events[3]["studySessionId"], guest_session_id)

        records_response = self.client.get("/matchmaking-records-data")
        self.assertEqual(records_response.status_code, 200)
        records = records_response.json()["matches"]
        record = next(item for item in records if item["matchId"] == host["matchId"])
        self.assertEqual(record["players"][0]["studySessionId"], host_session_id)
        self.assertEqual(record["players"][1]["studySessionId"], guest_session_id)

    def test_join_normalizes_room_code_and_rejects_third_player(self):
        host = self.create_room()
        guest = self.join_room("  " + host["roomCode"].lower() + "  ")

        self.assertEqual(guest["matchId"], host["matchId"])
        self.assertEqual(guest["playerNumber"], 2)
        self.assertEqual(guest["status"], "briefing")
        self.assertEqual(len(guest["players"]), 2)

        third_response = self.client.post(
            "/online/rooms/join",
            json={"roomCode": host["roomCode"]},
        )
        self.assertEqual(third_response.status_code, 409)
        self.assertEqual(third_response.json()["detail"], "Room is full")

    def test_join_rejects_invalid_and_unknown_room_codes(self):
        invalid_response = self.client.post(
            "/online/rooms/join",
            json={"roomCode": "A!"},
        )
        unknown_response = self.client.post(
            "/online/rooms/join",
            json={"roomCode": "ABC123"},
        )

        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(unknown_response.status_code, 404)

    def test_room_status_requires_valid_player_token(self):
        host = self.create_room()

        missing_response = self.client.get(
            "/online/rooms/" + host["matchId"],
        )
        wrong_response = self.client.get(
            "/online/rooms/" + host["matchId"],
            headers=self.auth_headers("wrong-token"),
        )
        valid_response = self.client.get(
            "/online/rooms/" + host["matchId"],
            headers=self.auth_headers(host["playerToken"]),
        )

        self.assertEqual(missing_response.status_code, 401)
        self.assertEqual(wrong_response.status_code, 401)
        self.assertEqual(valid_response.status_code, 200)
        self.assertEqual(valid_response.json()["playerNumber"], 1)
        self.assertNotIn("playerToken", valid_response.json())

    def test_ready_is_idempotent_and_both_ready_enter_challenge_creation(self):
        host = self.create_room()
        guest = self.join_room(host["roomCode"])
        ready_url = "/online/rooms/" + host["matchId"] + "/ready"

        first_host_ready = self.client.post(
            ready_url,
            json={"ready": True},
            headers=self.auth_headers(host["playerToken"]),
        )
        repeated_host_ready = self.client.post(
            ready_url,
            json={"ready": True},
            headers=self.auth_headers(host["playerToken"]),
        )
        guest_ready = self.client.post(
            ready_url,
            json={"ready": True},
            headers=self.auth_headers(guest["playerToken"]),
        )

        self.assertEqual(first_host_ready.status_code, 200)
        self.assertEqual(first_host_ready.json()["status"], "briefing")
        self.assertEqual(repeated_host_ready.status_code, 200)
        self.assertEqual(
            repeated_host_ready.json()["players"],
            [
                {
                    "playerNumber": 1,
                    "ready": True,
                    "challengeSubmitted": False,
                    "resultSubmitted": False,
                },
                {
                    "playerNumber": 2,
                    "ready": False,
                    "challengeSubmitted": False,
                    "resultSubmitted": False,
                },
            ],
        )
        self.assertEqual(guest_ready.status_code, 200)
        self.assertEqual(guest_ready.json()["status"], "waiting_for_challenges")
        self.assertTrue(all(item["ready"] for item in guest_ready.json()["players"]))

    def test_ready_rejects_room_without_opponent(self):
        host = self.create_room()
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/ready",
            json={"ready": True},
            headers=self.auth_headers(host["playerToken"]),
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("opponent", response.json()["detail"].lower())

    def test_leave_cancels_room_for_both_players(self):
        host = self.create_room()
        guest = self.join_room(host["roomCode"])
        leave_response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/leave",
            headers=self.auth_headers(host["playerToken"]),
        )
        guest_status = self.client.get(
            "/online/rooms/" + host["matchId"],
            headers=self.auth_headers(guest["playerToken"]),
        )
        join_again = self.client.post(
            "/online/rooms/join",
            json={"roomCode": host["roomCode"]},
        )

        self.assertEqual(leave_response.status_code, 200)
        self.assertEqual(leave_response.json()["status"], "cancelled")
        self.assertEqual(guest_status.status_code, 200)
        self.assertEqual(guest_status.json()["status"], "cancelled")
        self.assertEqual(join_again.status_code, 409)

    def test_expired_room_is_removed_lazily(self):
        host = self.create_room()

        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS[host["matchId"]]["lastActivity"] = 100.0

        with patch("Backend.app.time.time", return_value=1901.0):
            response = self.client.get(
                "/online/rooms/" + host["matchId"],
                headers=self.auth_headers(host["playerToken"]),
            )

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(host["matchId"], backend.ONLINE_ROOMS)

    def ready_both_players(self):
        host = self.create_room()
        guest = self.join_room(host["roomCode"])
        ready_url = "/online/rooms/" + host["matchId"] + "/ready"
        self.client.post(
            ready_url,
            json={"ready": True},
            headers=self.auth_headers(host["playerToken"]),
        )
        self.client.post(
            ready_url,
            json={"ready": True},
            headers=self.auth_headers(guest["playerToken"]),
        )
        return host, guest

    def submit_challenge(
        self,
        match_id,
        player_token,
        rows,
        ai_assistant_mode="description_generation",
        opponent_experience_goal="",
    ):
        return self.client.post(
            "/online/rooms/" + match_id + "/challenge",
            json={
                "rows": rows,
                "aiAssistantMode": ai_assistant_mode,
                "opponentExperienceGoal": opponent_experience_goal,
            },
            headers=self.auth_headers(player_token),
        )

    def submit_result(
        self,
        match_id,
        player_token,
        duration_seconds=42.37,
        move_count=31,
        minimum_moves=24,
        outcome="completed",
        restart_count=0,
    ):
        return self.client.post(
            "/online/rooms/" + match_id + "/result",
            json={
                "durationSeconds": duration_seconds,
                "moveCount": move_count,
                "minimumMoves": minimum_moves,
                "outcome": outcome,
                "restartCount": restart_count,
            },
            headers=self.auth_headers(player_token),
        )

    def submit_first_stage(self, match_id, player_number=1, rows=None):
        event = {
            "eventId": "first_stage:test-" + str(player_number),
            "eventType": "first_stage",
            "playerNumber": player_number,
            "sessionId": "session-test-" + str(player_number),
            "versionId": "version-test-" + str(player_number),
            "stageNumber": 1,
            "initialDraftMethod": "description_generation",
            "rows": rows or SOLVABLE_ROWS_A,
            "diff": [],
        }
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            return self.client.post(
                "/online/rooms/" + match_id + "/cocreation-events",
                json=event,
                headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
            )

    def test_challenge_requires_ready_players_and_valid_token(self):
        host = self.create_room()
        guest = self.join_room(host["roomCode"])

        early_response = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        wrong_token_response = self.submit_challenge(
            host["matchId"],
            "wrong-token",
            SOLVABLE_ROWS_A,
        )

        self.assertEqual(early_response.status_code, 409)
        self.assertEqual(wrong_token_response.status_code, 401)
        self.assertFalse(guest["players"][0]["challengeSubmitted"])

    def test_challenge_rejects_invalid_rows(self):
        host, _ = self.ready_both_players()
        invalid_cases = [
            SOLVABLE_ROWS_A[:-1],
            [row[:-1] for row in SOLVABLE_ROWS_A],
            [row.replace("p", "x") for row in SOLVABLE_ROWS_A],
            [row.replace("p", ".") for row in SOLVABLE_ROWS_A],
            [row.replace("t", ".") for row in SOLVABLE_ROWS_A],
            [row.replace("s", "ss")[:12] for row in SOLVABLE_ROWS_A],
        ]

        for rows in invalid_cases:
            with self.subTest(rows=rows):
                response = self.submit_challenge(
                    host["matchId"],
                    host["playerToken"],
                    rows,
                )
                self.assertEqual(response.status_code, 400)

    def test_challenge_rejects_unknown_ai_assistant_mode(self):
        host, _ = self.ready_both_players()

        bad_assistant = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
            ai_assistant_mode="unknown",
        )

        self.assertEqual(bad_assistant.status_code, 400)

    def test_rows_only_challenge_uses_default_draft_method(self):
        host, _ = self.ready_both_players()
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/challenge",
            json={"rows": SOLVABLE_ROWS_A},
            headers=self.auth_headers(host["playerToken"]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["ownChallengeMetadata"],
            {
                "aiAssistantMode": "description_generation",
            },
        )

    def test_challenge_exposes_synchronized_designer_intention(self):
        host, guest = self.ready_both_players()
        host_goal = "I hope my opponent feels a careful route choice."
        self.assertEqual(self.submit_first_stage(host["matchId"], 1).status_code, 200)
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            synchronized = self.client.post(
                "/online/rooms/" + host["matchId"] + "/designer-intention",
                json={"playerNumber": host["playerNumber"], "designerIntention": host_goal},
                headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
            )
        self.assertEqual(synchronized.status_code, 200)
        self.submit_challenge(host["matchId"], host["playerToken"], SOLVABLE_ROWS_A)
        response = self.submit_challenge(
            host["matchId"],
            guest["playerToken"],
            SOLVABLE_ROWS_B,
            ai_assistant_mode="description_generation",
        )

        self.assertEqual(response.status_code, 200)
        host_status = self.client.get(
            "/online/rooms/" + host["matchId"],
            headers=self.auth_headers(host["playerToken"]),
        )
        self.assertEqual(
            host_status.json()["ownChallengeMetadata"]["designerIntention"],
            host_goal,
        )
        self.assertEqual(
            host_status.json()["opponentChallengeMetadata"].get("designerIntention", ""),
            "",
        )

    def test_cocreation_flow_events_are_scoped_to_each_player_and_idempotent(self):
        host = self.client.post(
            "/online/rooms",
            json={"studySessionId": "study-host"},
        ).json()
        guest = self.client.post(
            "/online/rooms/join",
            json={"roomCode": host["roomCode"], "studySessionId": "study-guest"},
        ).json()
        endpoint = "/online/rooms/" + host["matchId"] + "/cocreation-events"
        headers = {"X-CoCreation-Sync-Secret": "test-sync-secret"}
        stage = {
            "eventId": "stage:version-host",
            "eventType": "stage",
            "playerNumber": 1,
            "sessionId": "session-host",
            "versionId": "version-host",
            "stageNumber": 2,
            "source": "manual",
            "rows": SOLVABLE_ROWS_A,
            "diff": [{"x": 4, "y": 4, "from": ".", "to": "p"}],
        }
        turn = {
            "eventId": "turn:assistant-guest",
            "eventType": "turn",
            "playerNumber": 2,
            "sessionId": "session-guest",
            "versionId": "version-guest",
            "userText": "Could this route feel fair?",
            "assistantText": "I would keep the route visible.",
            "language": "en",
            "cards": [{"type": "discussion", "text": "What should stand out?"}],
        }

        self.assertEqual(self.submit_first_stage(host["matchId"], 1).status_code, 200)
        self.assertEqual(self.submit_first_stage(host["matchId"], 2).status_code, 200)
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            first = self.client.post(endpoint, json=stage, headers=headers)
            duplicate = self.client.post(endpoint, json=stage, headers=headers)
            second = self.client.post(endpoint, json=turn, headers=headers)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["recorded"])
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertFalse(duplicate.json()["recorded"])
        self.assertEqual(second.status_code, 200, second.text)
        host_events, _ = backend.read_online_match_flow_events(host["matchId"], 1)
        guest_events, _ = backend.read_online_match_flow_events(host["matchId"], 2)
        self.assertEqual([event["eventType"] for event in host_events], ["first_stage", "stage"])
        self.assertEqual([event["eventType"] for event in guest_events], ["first_stage", "turn"])
        self.assertEqual(host_events[1]["studySessionId"], "study-host")
        self.assertEqual(guest_events[1]["studySessionId"], "study-guest")
        dashboard = self.client.get("/matchmaking-records-data").json()
        record = next(item for item in dashboard["matches"] if item["matchId"] == host["matchId"])
        self.assertEqual(record["players"][0]["coCreationFlow"][1]["source"], "manual")
        self.assertEqual(record["players"][1]["coCreationFlow"][1]["cards"][0]["type"], "discussion")

    def test_cocreation_events_are_rejected_until_first_stage(self):
        host, _ = self.ready_both_players()
        endpoint = "/online/rooms/" + host["matchId"] + "/cocreation-events"
        stage = {
            "eventId": "stage:before-first-stage",
            "eventType": "stage",
            "playerNumber": 1,
            "sessionId": "session-host",
            "versionId": "version-host",
            "stageNumber": 2,
            "source": "manual",
            "rows": SOLVABLE_ROWS_A,
            "diff": [],
        }
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            response = self.client.post(
                endpoint,
                json=stage,
                headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
            )
        self.assertEqual(response.status_code, 409)
        flow, _ = backend.read_online_match_flow_events(host["matchId"], 1)
        self.assertEqual(flow, [])

    def test_final_flow_exposes_opponent_restart_count_in_dashboard(self):
        host, guest = self.ready_both_players()
        self.assertEqual(
            self.submit_challenge(host["matchId"], host["playerToken"], SOLVABLE_ROWS_A).status_code,
            200,
        )
        self.assertEqual(
            self.submit_challenge(guest["matchId"], guest["playerToken"], SOLVABLE_ROWS_B).status_code,
            200,
        )
        self.assertEqual(self.submit_first_stage(host["matchId"], 1).status_code, 200)
        final = {
            "eventId": "final:restart-count",
            "eventType": "final",
            "playerNumber": 1,
            "sessionId": "session-test-1",
            "versionId": "version-test-1",
            "stageNumber": 1,
            "rows": SOLVABLE_ROWS_A,
            "diff": [],
        }
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            self.assertEqual(
                self.client.post(
                    "/online/rooms/" + host["matchId"] + "/cocreation-events",
                    json=final,
                    headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
                ).status_code,
                200,
            )
        self.assertEqual(
            self.submit_result(
                host["matchId"], host["playerToken"], restart_count=2
            ).status_code,
            200,
        )
        self.assertEqual(
            self.submit_result(
                guest["matchId"], guest["playerToken"], restart_count=5
            ).status_code,
            200,
        )
        record = self.client.get("/matchmaking-records-data").json()["matches"][0]
        self.assertEqual(record["players"][0]["challenge"]["runResult"]["restartCount"], 5)
        final_record = record["players"][0]["coCreationFlow"][-1]
        self.assertEqual(final_record["opponentRestartCount"], 5)

    def test_final_flow_event_records_cocreation_duration_and_validates_scope(self):
        host = self.client.post(
            "/online/rooms", json={"studySessionId": "study-host"}
        ).json()
        endpoint = "/online/rooms/" + host["matchId"] + "/cocreation-events"
        headers = {"X-CoCreation-Sync-Secret": "test-sync-secret"}
        final = {
            "eventId": "final:version-host",
            "eventType": "final",
            "playerNumber": 1,
            "sessionId": "session-host",
            "versionId": "version-host",
            "stageNumber": 2,
            "rows": SOLVABLE_ROWS_A,
            "diff": [],
            "coCreationDurationSeconds": 321,
        }

        self.assertEqual(self.submit_first_stage(host["matchId"], 1).status_code, 200)
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            response = self.client.post(endpoint, json=final, headers=headers)
            invalid_value = self.client.post(
                endpoint,
                json={**final, "eventId": "final:invalid", "coCreationDurationSeconds": 601},
                headers=headers,
            )
            invalid_event = self.client.post(
                endpoint,
                json={
                    **final,
                    "eventId": "stage:invalid-duration",
                    "eventType": "stage",
                    "source": "manual",
                    "coCreationDurationSeconds": 10,
                },
                headers=headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["event"]["coCreationDurationSeconds"],
            321,
        )
        self.assertEqual(invalid_value.status_code, 400)
        self.assertEqual(invalid_event.status_code, 400)

        dashboard = self.client.get("/matchmaking-records-data").json()
        record = next(item for item in dashboard["matches"] if item["matchId"] == host["matchId"])
        self.assertEqual(
            record["players"][0]["coCreationFlow"][1]["coCreationDurationSeconds"],
            321,
        )

    def test_first_stage_and_opening_are_preserved_for_dashboard_merging(self):
        host = self.client.post(
            "/online/rooms", json={"studySessionId": "study-host"}
        ).json()
        endpoint = "/online/rooms/" + host["matchId"] + "/cocreation-events"
        headers = {"X-CoCreation-Sync-Secret": "test-sync-secret"}
        first_stage = {
            "eventId": "first_stage:version-host",
            "eventType": "first_stage",
            "playerNumber": 1,
            "sessionId": "session-host",
            "versionId": "version-host",
            "stageNumber": 1,
            "initialDraftMethod": "description_generation",
            "rows": SOLVABLE_ROWS_A,
            "diff": [],
        }
        opening = {
            "eventId": "opening:assistant-host",
            "eventType": "opening",
            "playerNumber": 1,
            "sessionId": "session-host",
            "versionId": "version-host",
            "stageNumber": 1,
            "assistantTurnId": "assistant-host",
            "assistantText": "I notice a clear route.",
            "language": "en",
            "cards": [{"type": "discussion", "text": "What should stand out?"}],
        }
        with patch.object(backend, "COCREATION_INTENTION_SYNC_SECRET", "test-sync-secret"):
            self.assertTrue(self.client.post(endpoint, json=first_stage, headers=headers).json()["recorded"])
            self.assertTrue(self.client.post(endpoint, json=opening, headers=headers).json()["recorded"])

        dashboard = self.client.get("/matchmaking-records-data").json()
        record = next(item for item in dashboard["matches"] if item["matchId"] == host["matchId"])
        flow = record["players"][0]["coCreationFlow"]
        self.assertEqual([event["eventType"] for event in flow], ["first_stage", "opening"])
        self.assertEqual(flow[0]["initialDraftMethod"], "description_generation")
        self.assertEqual(flow[1]["assistantTurnId"], "assistant-host")

    def test_synced_8010_intention_survives_an_empty_client_submission(self):
        host, _ = self.ready_both_players()
        goal = "I want my opponent to notice the route before pushing."
        self.assertEqual(self.submit_first_stage(host["matchId"], 1).status_code, 200)

        with patch.object(
            backend,
            "COCREATION_INTENTION_SYNC_SECRET",
            "test-sync-secret",
        ):
            synchronized = self.client.post(
                "/online/rooms/"
                + host["matchId"]
                + "/designer-intention",
                json={
                    "playerNumber": host["playerNumber"],
                    "designerIntention": goal,
                },
                headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
            )

        self.assertEqual(synchronized.status_code, 200)

        submitted = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )

        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(
            submitted.json()["ownChallengeMetadata"]["designerIntention"],
            goal,
        )

    def test_legacy_competition_field_is_ignored(self):
        host, _ = self.ready_both_players()
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/challenge",
            json={
                "rows": SOLVABLE_ROWS_A,
                "competitionMode": "legacy-value",
                "aiAssistantMode": "description_generation",
            },
            headers=self.auth_headers(host["playerToken"]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            "competitionMode",
            response.json()["ownChallengeMetadata"],
        )
        self.assertNotIn(
            "competitionMode",
            backend.ONLINE_MATCH_LOG_FILE.read_text(encoding="utf-8"),
        )

    def test_challenge_submission_is_idempotent_and_frozen(self):
        host, _ = self.ready_both_players()

        first = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        repeated = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        changed = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_B,
        )
        repeated_method = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
            ai_assistant_mode="description_generation",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "waiting_for_challenges")
        self.assertTrue(first.json()["players"][0]["challengeSubmitted"])
        self.assertNotIn("opponentChallengeRows", first.json())
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(repeated_method.status_code, 200)

    def test_both_challenges_are_exchanged_by_player_identity(self):
        host, guest = self.ready_both_players()
        self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        second = self.submit_challenge(
            host["matchId"],
            guest["playerToken"],
            SOLVABLE_ROWS_B,
            ai_assistant_mode="description_generation",
        )
        host_status = self.client.get(
            "/online/rooms/" + host["matchId"],
            headers=self.auth_headers(host["playerToken"]),
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "challenges_ready")
        self.assertEqual(second.json()["opponentChallengeRows"], SOLVABLE_ROWS_A)
        self.assertEqual(host_status.status_code, 200)
        self.assertEqual(
            host_status.json()["opponentChallengeRows"],
            SOLVABLE_ROWS_B,
        )
        self.assertNotEqual(
            host_status.json()["opponentChallengeRows"],
            SOLVABLE_ROWS_A,
        )
        self.assertEqual(
            host_status.json()["ownChallengeMetadata"],
            {
                "aiAssistantMode": "description_generation",
            },
        )
        self.assertEqual(
            host_status.json()["opponentChallengeMetadata"],
            {
                "aiAssistantMode": "description_generation",
            },
        )

    def test_result_requires_both_challenges_and_valid_token(self):
        host, guest = self.ready_both_players()
        self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )

        early = self.submit_result(
            host["matchId"],
            host["playerToken"],
        )
        wrong_token = self.submit_result(
            host["matchId"],
            "wrong-token",
        )

        self.assertEqual(early.status_code, 409)
        self.assertEqual(wrong_token.status_code, 401)
        self.assertFalse(guest["players"][0]["resultSubmitted"])

    def test_result_rejects_invalid_metrics(self):
        host, guest = self.ready_both_players()
        self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        self.submit_challenge(
            host["matchId"],
            guest["playerToken"],
            SOLVABLE_ROWS_B,
        )

        invalid_cases = [
            (-1, 31, 24),
            (10, -1, 0),
            (10, 3, -1),
            (10, 10, 11),
        ]

        for duration, moves, minimum in invalid_cases:
            with self.subTest(
                duration=duration,
                moves=moves,
                minimum=minimum,
            ):
                response = self.submit_result(
                    host["matchId"],
                    host["playerToken"],
                    duration,
                    moves,
                    minimum,
                )
                self.assertEqual(response.status_code, 400)

        negative_restart = self.submit_result(
            host["matchId"],
            host["playerToken"],
            restart_count=-1,
        )
        self.assertEqual(negative_restart.status_code, 400)

    def test_result_submission_is_idempotent_and_frozen(self):
        host, guest = self.ready_both_players()
        self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        self.submit_challenge(
            host["matchId"],
            guest["playerToken"],
            SOLVABLE_ROWS_B,
        )

        first = self.submit_result(
            host["matchId"],
            host["playerToken"],
        )
        repeated = self.submit_result(
            host["matchId"],
            host["playerToken"],
        )
        changed = self.submit_result(
            host["matchId"],
            host["playerToken"],
            duration_seconds=43,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "waiting_for_results")
        self.assertTrue(first.json()["players"][0]["resultSubmitted"])
        self.assertEqual(first.json()["ownResult"]["moveCount"], 31)
        self.assertNotIn("opponentResult", first.json())
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(changed.status_code, 409)

    def test_timed_out_result_allows_current_move_count(self):
        host, guest = self.ready_both_players()
        self.submit_challenge(host["matchId"], host["playerToken"], SOLVABLE_ROWS_A)
        self.submit_challenge(host["matchId"], guest["playerToken"], SOLVABLE_ROWS_B)

        response = self.submit_result(
            host["matchId"],
            host["playerToken"],
            duration_seconds=30,
            move_count=4,
            minimum_moves=18,
            outcome="timed_out",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ownResult"]["outcome"], "timed_out")
        self.assertEqual(response.json()["ownResult"]["moveCount"], 4)

    def test_both_results_are_exchanged_by_player_identity(self):
        host, guest = self.ready_both_players()
        self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
        )
        self.submit_challenge(
            host["matchId"],
            guest["playerToken"],
            SOLVABLE_ROWS_B,
            ai_assistant_mode="description_generation",
        )
        self.submit_result(
            host["matchId"],
            host["playerToken"],
            duration_seconds=12.34,
            move_count=20,
            minimum_moves=18,
        )
        guest_result = self.submit_result(
            host["matchId"],
            guest["playerToken"],
            duration_seconds=56.78,
            move_count=40,
            minimum_moves=30,
        )
        host_status = self.client.get(
            "/online/rooms/" + host["matchId"],
            headers=self.auth_headers(host["playerToken"]),
        )

        self.assertEqual(guest_result.status_code, 200)
        self.assertEqual(guest_result.json()["status"], "results_ready")
        self.assertEqual(
            guest_result.json()["ownResult"],
            {
                "durationSeconds": 56.78,
                "moveCount": 40,
                "minimumMoves": 30,
                "restartCount": 0,
                "outcome": "completed",
            },
        )
        self.assertEqual(
            guest_result.json()["opponentResult"],
            {
                "durationSeconds": 12.34,
                "moveCount": 20,
                "minimumMoves": 18,
                "restartCount": 0,
                "outcome": "completed",
            },
        )
        self.assertEqual(
            host_status.json()["ownResult"],
            guest_result.json()["opponentResult"],
        )
        self.assertEqual(
            host_status.json()["opponentResult"],
            guest_result.json()["ownResult"],
        )


if __name__ == "__main__":
    unittest.main()
