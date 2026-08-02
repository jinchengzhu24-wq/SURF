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

    def test_ready_is_idempotent_and_both_ready_choose_mode(self):
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
        self.assertEqual(guest_ready.json()["status"], "choosing_mode")
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
        competition_mode="competitive",
        ai_assistant_mode="description_generation",
    ):
        return self.client.post(
            "/online/rooms/" + match_id + "/challenge",
            json={
                "rows": rows,
                "competitionMode": competition_mode,
                "aiAssistantMode": ai_assistant_mode,
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
    ):
        return self.client.post(
            "/online/rooms/" + match_id + "/result",
            json={
                "durationSeconds": duration_seconds,
                "moveCount": move_count,
                "minimumMoves": minimum_moves,
            },
            headers=self.auth_headers(player_token),
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

    def test_challenge_rejects_unknown_modes(self):
        host, _ = self.ready_both_players()

        bad_competition = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
            competition_mode="unknown",
        )
        bad_assistant = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
            ai_assistant_mode="unknown",
        )

        self.assertEqual(bad_competition.status_code, 400)
        self.assertEqual(bad_assistant.status_code, 400)

    def test_legacy_rows_only_challenge_uses_safe_mode_defaults(self):
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
                "competitionMode": "competitive",
                "aiAssistantMode": "description_generation",
            },
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
        changed_mode = self.submit_challenge(
            host["matchId"],
            host["playerToken"],
            SOLVABLE_ROWS_A,
            competition_mode="supportive",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "waiting_for_challenges")
        self.assertTrue(first.json()["players"][0]["challengeSubmitted"])
        self.assertNotIn("opponentChallengeRows", first.json())
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed_mode.status_code, 409)

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
            competition_mode="supportive",
            ai_assistant_mode="partial_completion",
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
                "competitionMode": "competitive",
                "aiAssistantMode": "description_generation",
            },
        )
        self.assertEqual(
            host_status.json()["opponentChallengeMetadata"],
            {
                "competitionMode": "supportive",
                "aiAssistantMode": "partial_completion",
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
            competition_mode="supportive",
            ai_assistant_mode="partial_completion",
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
            },
        )
        self.assertEqual(
            guest_result.json()["opponentResult"],
            {
                "durationSeconds": 12.34,
                "moveCount": 20,
                "minimumMoves": 18,
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
