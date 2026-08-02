import json
import os
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


class MatchmakingRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        log_dir = Path(self.temp_dir.name)
        self.patchers = [
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

        for patcher in self.patchers:
            patcher.start()

        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS.clear()

        self.client = TestClient(backend.app)

    def tearDown(self):
        self.client.close()

        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS.clear()

        for patcher in reversed(self.patchers):
            patcher.stop()

        self.temp_dir.cleanup()

    @staticmethod
    def headers(player):
        return {"X-Player-Token": player["playerToken"]}

    def create_players(self):
        host = self.client.post("/online/rooms").json()
        guest = self.client.post(
            "/online/rooms/join",
            json={"roomCode": host["roomCode"]},
        ).json()
        return host, guest

    def make_ready(self, host, guest):
        url = "/online/rooms/" + host["matchId"] + "/ready"

        for player in (host, guest):
            response = self.client.post(
                url,
                headers=self.headers(player),
                json={"ready": True},
            )
            self.assertEqual(response.status_code, 200)

    def submit_challenge(self, player, rows, competition, assistant):
        return self.client.post(
            "/online/rooms/" + player["matchId"] + "/challenge",
            headers=self.headers(player),
            json={
                "rows": rows,
                "competitionMode": competition,
                "aiAssistantMode": assistant,
            },
        )

    def submit_result(self, player, duration, moves, minimum):
        return self.client.post(
            "/online/rooms/" + player["matchId"] + "/result",
            headers=self.headers(player),
            json={
                "durationSeconds": duration,
                "moveCount": moves,
                "minimumMoves": minimum,
            },
        )

    def complete_match(self):
        host, guest = self.create_players()
        self.make_ready(host, guest)
        self.assertEqual(
            self.submit_challenge(
                host,
                SOLVABLE_ROWS_A,
                "competitive",
                "description_generation",
            ).status_code,
            200,
        )
        self.assertEqual(
            self.submit_challenge(
                guest,
                SOLVABLE_ROWS_B,
                "supportive",
                "partial_completion",
            ).status_code,
            200,
        )
        self.assertEqual(self.submit_result(host, 12.34, 20, 18).status_code, 200)
        self.assertEqual(self.submit_result(guest, 56.78, 40, 30).status_code, 200)
        return host, guest

    def test_records_state_changes_once_and_never_logs_tokens(self):
        host, guest = self.create_players()
        ready_url = "/online/rooms/" + host["matchId"] + "/ready"
        self.client.post(ready_url, headers=self.headers(host), json={"ready": True})
        self.client.post(ready_url, headers=self.headers(host), json={"ready": True})
        self.client.post(ready_url, headers=self.headers(guest), json={"ready": True})

        challenge_response = self.submit_challenge(
            host,
            SOLVABLE_ROWS_A,
            "competitive",
            "description_generation",
        )
        self.assertEqual(challenge_response.status_code, 200)
        self.assertEqual(
            self.submit_challenge(
                host,
                SOLVABLE_ROWS_A,
                "competitive",
                "description_generation",
            ).status_code,
            200,
        )

        events, malformed = backend.read_online_match_events()
        event_types = [event["eventType"] for event in events]
        self.assertEqual(malformed, 0)
        self.assertEqual(event_types.count("room_created"), 1)
        self.assertEqual(event_types.count("player_joined"), 1)
        self.assertEqual(event_types.count("ready_changed"), 2)
        self.assertEqual(event_types.count("challenge_submitted"), 1)
        self.assertNotIn("playerToken", backend.ONLINE_MATCH_LOG_FILE.read_text())
        self.assertNotIn(host["playerToken"], backend.ONLINE_MATCH_LOG_FILE.read_text())

    def test_dashboard_maps_each_challenge_to_opponent_result(self):
        host, guest = self.complete_match()
        leave_response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/leave",
            headers=self.headers(host),
            json={},
        )
        self.assertEqual(leave_response.status_code, 200)

        response = self.client.get("/matchmaking-records-data")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"]["completedCount"], 1)
        self.assertEqual(payload["summary"]["averageRunDurationSeconds"], 34.56)
        match = payload["matches"][0]
        self.assertEqual(match["status"], "completed")
        self.assertEqual(match["players"][0]["challenge"]["rows"], SOLVABLE_ROWS_A)
        self.assertEqual(
            match["players"][0]["challenge"]["runResult"]["moveCount"],
            40,
        )
        self.assertEqual(
            match["players"][1]["challenge"]["runResult"]["moveCount"],
            20,
        )
        self.assertNotIn("playerToken", json.dumps(payload))

    def test_online_questionnaire_is_joined_by_match_and_player(self):
        host, _ = self.complete_match()
        survey = {
            "eventType": "survey-response",
            "surveyId": "online_post_match_survey",
            "surveyTitle": "Online Match Questionnaire",
            "responseId": "survey-player-one",
            "matchId": host["matchId"],
            "roomCode": host["roomCode"],
            "playerNumber": 1,
            "durationSeconds": 8.5,
            "answers": [
                {
                    "questionIndex": 1,
                    "questionText": "Fairness",
                    "optionText": "4",
                }
            ],
        }
        response = self.client.post("/record-survey-response", json=survey)
        self.assertEqual(response.status_code, 200)

        payload = self.client.get("/matchmaking-records-data").json()
        player = payload["matches"][0]["players"][0]
        self.assertEqual(player["survey"]["responseId"], "survey-player-one")
        self.assertEqual(player["survey"]["answerDetails"][0]["optionLabel"], "4")
        self.assertEqual(payload["summary"]["questionnaireCount"], 1)

    def test_invalid_token_does_not_create_a_tracking_event(self):
        host, _ = self.create_players()
        before, _ = backend.read_online_match_events()
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/ready",
            headers={"X-Player-Token": "invalid"},
            json={"ready": True},
        )
        after, _ = backend.read_online_match_events()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(after), len(before))

    def test_expired_room_and_malformed_line_are_reported(self):
        host = self.client.post("/online/rooms").json()

        with backend.ONLINE_ROOMS_LOCK:
            backend.ONLINE_ROOMS[host["matchId"]]["lastActivity"] = 100.0
            backend.cleanup_expired_online_rooms(
                now=100.0 + backend.ONLINE_ROOM_TTL_SECONDS
            )

        with backend.ONLINE_MATCH_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write("not-json\n")

        payload = self.client.get("/matchmaking-records-data").json()
        self.assertEqual(payload["matches"][0]["status"], "expired")
        self.assertEqual(payload["summary"]["expiredCount"], 1)
        self.assertEqual(payload["malformedCount"], 1)

    def test_delete_and_clear_preserve_train_surveys(self):
        host, _ = self.complete_match()
        backend.write_jsonl_records(
            backend.SURVEY_LOG_FILE,
            [
                {
                    "surveyId": "train_survey",
                    "responseId": "train-response",
                },
                {
                    "surveyId": "online_post_match_survey",
                    "responseId": "online-response",
                    "matchId": host["matchId"],
                    "playerNumber": 1,
                },
            ],
        )

        with patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": "1234"}):
            response = self.client.post(
                "/delete-online-match",
                headers={"X-Delete-Password": "1234"},
                json={"matchId": host["matchId"]},
            )
        self.assertEqual(response.status_code, 200)
        surveys, _ = backend.read_survey_response_events()
        self.assertEqual([record["responseId"] for record in surveys], ["train-response"])

        second_host = self.client.post("/online/rooms").json()
        self.assertTrue(second_host["matchId"])

        with patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": "1234"}):
            response = self.client.post(
                "/clear-matchmaking-records",
                headers={"X-Delete-Password": "1234"},
                json={},
            )
        self.assertEqual(response.status_code, 200)
        events, _ = backend.read_online_match_events()
        surveys, _ = backend.read_survey_response_events()
        self.assertEqual(events, [])
        self.assertEqual([record["responseId"] for record in surveys], ["train-response"])


if __name__ == "__main__":
    unittest.main()
