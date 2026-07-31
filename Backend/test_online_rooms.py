import unittest
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
        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS.clear()
        self.client = TestClient(backend.app)

    def tearDown(self):
        self.client.close()
        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS.clear()

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
                },
                {
                    "playerNumber": 2,
                    "ready": False,
                    "challengeSubmitted": False,
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

    def submit_challenge(self, match_id, player_token, rows):
        return self.client.post(
            "/online/rooms/" + match_id + "/challenge",
            json={"rows": rows},
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

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "waiting_for_challenges")
        self.assertTrue(first.json()["players"][0]["challengeSubmitted"])
        self.assertNotIn("opponentChallengeRows", first.json())
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(changed.status_code, 409)

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


if __name__ == "__main__":
    unittest.main()
