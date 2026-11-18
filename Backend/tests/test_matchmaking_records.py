import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from sokoban_backend.config import FRONTEND_DIR, ONLINE_ROOM_TTL_SECONDS
from tests.fakes import create_test_app, create_test_container


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
        self.tmp_path = Path(self.temp_dir.name)
        self.container = create_test_container(
            self.tmp_path,
            cocreation_intention_sync_secret="test-sync-secret",
            dashboard_delete_password="1234",
        )
        self.client = TestClient(create_test_app(self.tmp_path, container=self.container))

    def tearDown(self):
        self.client.close()
        self.container.close()
        self.temp_dir.cleanup()

    def test_match_list_uses_created_date_with_legacy_updated_date_fallback(self):
        frontend_dir = Path(FRONTEND_DIR)
        script = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((frontend_dir / "js").rglob("*.js"))
        )
        index = (frontend_dir / "index.html").read_text(encoding="utf-8")

        self.assertIn(
            'formatShortDate(getMatchDisplayTimestamp(match))',
            script,
        )
        self.assertIn(
            'return clean(match && (match.createdAt || match.updatedAt));',
            script,
        )
        self.assertNotIn('formatShortDate(match.updatedAt)', script)
        self.assertIn('research-nodes-20261010-1', index)

    @staticmethod
    def headers(player):
        return {"X-Player-Token": player["playerToken"]}

    def create_players(self):
        host = self.client.post(
            "/online/rooms",
            json={"menuStartedFlow": True},
        ).json()
        guest = self.client.post(
            "/online/rooms/join",
            json={"roomCode": host["roomCode"], "menuStartedFlow": True},
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

    def submit_challenge(self, player, rows, assistant, opponent_experience_goal=""):
        return self.client.post(
            "/online/rooms/" + player["matchId"] + "/challenge",
            headers=self.headers(player),
            json={
                "rows": rows,
                "aiAssistantMode": assistant,
                "opponentExperienceGoal": opponent_experience_goal,
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
        first_stage = {
            "eventId": "first_stage:matchmaking-host",
            "eventType": "first_stage",
            "playerNumber": 1,
            "sessionId": "session-matchmaking-host",
            "versionId": "version-matchmaking-host",
            "stageNumber": 1,
            "initialDraftMethod": "description_generation",
            "rows": SOLVABLE_ROWS_A,
            "diff": [],
        }
        self.assertEqual(
            self.client.post(
                "/online/rooms/" + host["matchId"] + "/cocreation-events",
                json=first_stage,
                headers={"X-CoCreation-Sync-Secret": "test-sync-secret"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.submit_challenge(
                host,
                SOLVABLE_ROWS_A,
                "description_generation",
            ).status_code,
            200,
        )
        self.assertEqual(
            self.submit_challenge(
                guest,
                SOLVABLE_ROWS_B,
                "description_generation",
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
            "description_generation",
        )
        self.assertEqual(challenge_response.status_code, 200)
        self.assertEqual(
            self.submit_challenge(
                host,
                SOLVABLE_ROWS_A,
                "description_generation",
            ).status_code,
            200,
        )

        events, malformed = self.container.store.read_online_match_events()
        event_types = [event["eventType"] for event in events]
        self.assertEqual(malformed, 0)
        self.assertEqual(event_types.count("room_created"), 1)
        self.assertEqual(event_types.count("player_joined"), 1)
        self.assertEqual(event_types.count("ready_changed"), 2)
        self.assertEqual(event_types.count("challenge_submitted"), 1)
        raw_text = self.container.paths.online_match_log_file.read_text(encoding="utf-8")
        self.assertNotIn("playerToken", raw_text)
        self.assertNotIn(host["playerToken"], raw_text)

    def test_dashboard_maps_each_challenge_to_opponent_result(self):
        host, _ = self.complete_match()
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
        self.assertNotIn("competitionMode", json.dumps(payload))

    def test_legacy_competition_field_remains_raw_but_is_not_exposed(self):
        self.complete_match()
        events, malformed = self.container.store.read_online_match_events()
        self.assertEqual(malformed, 0)
        challenge_event = next(
            event
            for event in events
            if event.get("eventType") == "challenge_submitted"
        )
        challenge_event["competitionMode"] = "competitive"
        self.container.store.write_jsonl_records(
            self.container.paths.online_match_log_file,
            events,
        )

        raw_log = self.container.paths.online_match_log_file.read_text(encoding="utf-8")
        payload = self.client.get("/matchmaking-records-data").json()

        self.assertIn("competitionMode", raw_log)
        self.assertNotIn("competitionMode", json.dumps(payload))

    def test_online_questionnaire_is_not_joined_by_matchmaking_records(self):
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
        self.assertNotIn("survey", json.dumps(payload))
        self.assertNotIn("questionnaireCount", payload["summary"])

    def test_invalid_token_does_not_create_a_tracking_event(self):
        host, _ = self.create_players()
        before, _ = self.container.store.read_online_match_events()
        response = self.client.post(
            "/online/rooms/" + host["matchId"] + "/ready",
            headers={"X-Player-Token": "invalid"},
            json={"ready": True},
        )
        after, _ = self.container.store.read_online_match_events()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(after), len(before))

    def test_expired_room_and_malformed_line_are_reported(self):
        host = self.client.post("/online/rooms").json()

        with self.container.room_manager.lock:
            self.container.room_manager.rooms[host["matchId"]]["lastActivity"] = 100.0
            self.container.room_manager.cleanup_expired_online_rooms(
                now=100.0 + ONLINE_ROOM_TTL_SECONDS
            )

        self.container.store.ensure_study_log_dir()
        with self.container.paths.online_match_log_file.open(
            "a",
            encoding="utf-8",
        ) as log_file:
            log_file.write("not-json\n")

        payload = self.client.get("/matchmaking-records-data").json()
        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["summary"]["expiredCount"], 0)
        self.assertEqual(payload["malformedCount"], 1)

    def test_delete_and_clear_preserve_survey_records(self):
        host, _ = self.complete_match()
        self.container.store.write_jsonl_records(
            self.container.paths.survey_log_file,
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

        response = self.client.post(
            "/delete-online-match",
            headers={"X-Delete-Password": "1234"},
            json={"matchId": host["matchId"]},
        )
        self.assertEqual(response.status_code, 200)
        surveys, _ = self.container.store.read_survey_response_events()
        self.assertEqual(
            [record["responseId"] for record in surveys],
            ["train-response", "online-response"],
        )

        second_host = self.client.post("/online/rooms").json()
        self.assertTrue(second_host["matchId"])

        response = self.client.post(
            "/clear-matchmaking-records",
            headers={"X-Delete-Password": "1234"},
            json={},
        )
        self.assertEqual(response.status_code, 200)
        events, _ = self.container.store.read_online_match_events()
        surveys, _ = self.container.store.read_survey_response_events()
        self.assertEqual(events, [])
        self.assertEqual(
            [record["responseId"] for record in surveys],
            ["train-response", "online-response"],
        )


if __name__ == "__main__":
    unittest.main()
