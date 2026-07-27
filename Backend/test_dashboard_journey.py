import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import Backend.app as backend


TEST_DELETE_PASSWORD = "test-delete-password"


class DashboardJourneyTests(unittest.TestCase):
    def test_all_delete_routes_require_password(self):
        routes = [
            ("/delete-round", {"roundId": "round-a"}),
            ("/delete-level-run", {"levelRunId": "run-a"}),
            ("/delete-survey-response", {"responseId": "response-a"}),
            ("/delete-creative-idea", {"ideaId": "idea-a"}),
            ("/delete-expansion-choice", {"choiceId": "choice-a"}),
            ("/delete-ha-plan-event", {"haEventId": "ha-a"}),
            ("/delete-journey-event", {"journeyEventId": "journey-a"}),
            ("/delete-idea-records", {"ideaId": "idea-a"}),
            ("/clear-level-records", {}),
        ]

        with (
            patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": TEST_DELETE_PASSWORD}),
            TestClient(backend.app) as client,
        ):
            for path, payload in routes:
                with self.subTest(path=path):
                    response = client.post(path, json=payload)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["detail"], "Incorrect delete password")

    def test_delete_route_fails_closed_without_configured_password(self):
        with (
            patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": ""}),
            TestClient(backend.app) as client,
        ):
            response = client.post(
                "/delete-journey-event",
                json={"journeyEventId": "event-a"},
                headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Delete password is not configured")

    def test_delete_route_rejects_wrong_password_without_changing_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            journey_file = log_dir / "journey.jsonl"
            records = [
                {
                    "journeyEventId": "event-a",
                    "eventType": "journey-event",
                    "ideaId": "idea-a",
                    "phase": "review",
                }
            ]

            with (
                patch.multiple(
                    backend,
                    STUDY_LOG_DIR=log_dir,
                    JOURNEY_EVENT_LOG_FILE=journey_file,
                ),
                patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": TEST_DELETE_PASSWORD}),
                TestClient(backend.app) as client,
            ):
                backend.write_jsonl_records(journey_file, records)
                response = client.post(
                    "/delete-journey-event",
                    json={"journeyEventId": "event-a"},
                    headers={"X-Delete-Password": "wrong"},
                )
                remaining, malformed = backend.read_journey_events()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(malformed, 0)
        self.assertEqual(remaining, records)

    def test_delete_route_accepts_correct_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            journey_file = log_dir / "journey.jsonl"

            with (
                patch.multiple(
                    backend,
                    STUDY_LOG_DIR=log_dir,
                    JOURNEY_EVENT_LOG_FILE=journey_file,
                ),
                patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": TEST_DELETE_PASSWORD}),
                TestClient(backend.app) as client,
            ):
                backend.write_jsonl_records(journey_file, [
                    {
                        "journeyEventId": "event-a",
                        "eventType": "journey-event",
                        "ideaId": "idea-a",
                        "phase": "review",
                    }
                ])
                response = client.post(
                    "/delete-journey-event",
                    json={"journeyEventId": "event-a"},
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )
                remaining, malformed = backend.read_journey_events()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deletedJourneyEventCount"], 1)
        self.assertEqual(malformed, 0)
        self.assertEqual(remaining, [])

    def test_delete_idea_records_accepts_correct_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            paths = {
                "STUDY_LOG_DIR": log_dir,
                "STUDY_LOG_FILE": log_dir / "levels.jsonl",
                "SURVEY_LOG_FILE": log_dir / "surveys.jsonl",
                "CREATIVE_IDEA_LOG_FILE": log_dir / "ideas.jsonl",
                "CREATIVE_EXPANSION_CHOICE_LOG_FILE": log_dir / "choices.jsonl",
                "HA_PLAN_EVENT_LOG_FILE": log_dir / "ha.jsonl",
                "JOURNEY_EVENT_LOG_FILE": log_dir / "journey.jsonl",
            }

            with (
                patch.multiple(backend, **paths),
                patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": TEST_DELETE_PASSWORD}),
                TestClient(backend.app) as client,
            ):
                backend.write_jsonl_records(paths["STUDY_LOG_FILE"], [
                    {"creativeIdeaId": "idea-a"},
                    {"creativeIdeaId": "idea-b"},
                ])
                backend.write_jsonl_records(paths["SURVEY_LOG_FILE"], [
                    {"creativeIdeaId": "idea-a", "sessionId": "session-a"},
                    {"creativeIdeaId": "idea-b", "sessionId": "session-b"},
                ])
                backend.write_jsonl_records(paths["CREATIVE_IDEA_LOG_FILE"], [
                    {"ideaId": "idea-a"},
                    {"ideaId": "idea-b"},
                ])
                backend.write_jsonl_records(
                    paths["CREATIVE_EXPANSION_CHOICE_LOG_FILE"],
                    [{"ideaId": "idea-a"}, {"ideaId": "idea-b"}],
                )
                backend.write_jsonl_records(paths["HA_PLAN_EVENT_LOG_FILE"], [
                    {"ideaId": "idea-a"},
                    {"ideaId": "idea-b"},
                ])
                backend.write_jsonl_records(paths["JOURNEY_EVENT_LOG_FILE"], [
                    {"ideaId": "idea-a"},
                    {"ideaId": "idea-b"},
                ])

                response = client.post(
                    "/delete-idea-records",
                    json={"ideaId": "idea-a"},
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )
                remaining_records = {
                    name: backend.read_jsonl_records(path)[0]
                    for name, path in paths.items()
                    if name != "STUDY_LOG_DIR"
                }

        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(len(records) == 1 for records in remaining_records.values()))
        self.assertTrue(all(
            "idea-b" in json_value
            for records in remaining_records.values()
            for json_value in [str(records[0])]
        ))

    def test_clear_records_accepts_correct_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            paths = {
                "STUDY_LOG_DIR": log_dir,
                "STUDY_LOG_FILE": log_dir / "levels.jsonl",
                "SURVEY_LOG_FILE": log_dir / "surveys.jsonl",
                "CREATIVE_IDEA_LOG_FILE": log_dir / "ideas.jsonl",
                "CREATIVE_EXPANSION_CHOICE_LOG_FILE": log_dir / "choices.jsonl",
                "HA_PLAN_EVENT_LOG_FILE": log_dir / "ha.jsonl",
                "JOURNEY_EVENT_LOG_FILE": log_dir / "journey.jsonl",
            }

            with (
                patch.multiple(backend, **paths),
                patch.dict(os.environ, {"DASHBOARD_DELETE_PASSWORD": TEST_DELETE_PASSWORD}),
                TestClient(backend.app) as client,
            ):
                for name, path in paths.items():
                    if name != "STUDY_LOG_DIR":
                        backend.write_jsonl_records(path, [{"record": name}])

                response = client.post(
                    "/clear-level-records",
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                    follow_redirects=False,
                )
                remaining_records = [
                    backend.read_jsonl_records(path)[0]
                    for name, path in paths.items()
                    if name != "STUDY_LOG_DIR"
                ]

        self.assertEqual(response.status_code, 303)
        self.assertTrue(all(records == [] for records in remaining_records))

    def test_ha_filter_keeps_generation_linked_to_official_choice(self):
        records = [
            {
                "eventType": "ha-plan-generation",
                "ideaId": "idea-a",
                "officialRound": False,
            },
            {
                "eventType": "ha-plan-choice",
                "ideaId": "idea-a",
                "officialRound": True,
            },
            {
                "eventType": "ha-plan-generation",
                "ideaId": "idea-b",
                "officialRound": False,
            },
        ]

        filtered = backend.filter_frontend_ha_plan_records(records)

        self.assertEqual(len(filtered), 2)
        self.assertTrue(all(record["ideaId"] == "idea-a" for record in filtered))

    def test_dashboard_payload_exposes_ha_and_journey_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            paths = {
                "STUDY_LOG_DIR": log_dir,
                "STUDY_LOG_FILE": log_dir / "levels.jsonl",
                "SURVEY_LOG_FILE": log_dir / "surveys.jsonl",
                "CREATIVE_IDEA_LOG_FILE": log_dir / "ideas.jsonl",
                "CREATIVE_EXPANSION_CHOICE_LOG_FILE": log_dir / "choices.jsonl",
                "HA_PLAN_EVENT_LOG_FILE": log_dir / "ha.jsonl",
                "JOURNEY_EVENT_LOG_FILE": log_dir / "journey.jsonl",
            }

            with patch.multiple(backend, **paths):
                backend.write_jsonl_records(paths["CREATIVE_IDEA_LOG_FILE"], [
                    {
                        "eventType": "creative-idea",
                        "ideaId": "idea-a",
                        "ideaText": "A compact route.",
                        "sessionId": "session-a",
                        "officialRound": True,
                    }
                ])
                backend.write_jsonl_records(paths["HA_PLAN_EVENT_LOG_FILE"], [
                    {
                        "eventType": "ha-plan-choice",
                        "ideaId": "idea-a",
                        "selectedOptionTitle": "Keep the route",
                        "officialRound": True,
                    }
                ])
                backend.write_jsonl_records(paths["JOURNEY_EVENT_LOG_FILE"], [
                    {
                        "eventType": "journey-event",
                        "ideaId": "idea-a",
                        "phase": "review",
                        "action": "retry",
                        "officialRound": True,
                    }
                ])

                payload = backend.get_level_records_data()

        self.assertEqual(payload["haPlanSummary"]["choiceCount"], 1)
        self.assertEqual(payload["journeyEventSummary"]["phaseCounts"]["review"], 1)
        self.assertTrue(payload["haPlanEvents"][0]["haEventId"])
        self.assertTrue(payload["journeyEvents"][0]["journeyEventId"])

    def test_delete_journey_event_removes_only_target_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            journey_file = log_dir / "journey.jsonl"
            records = [
                {
                    "journeyEventId": "event-a",
                    "eventType": "journey-event",
                    "ideaId": "idea-a",
                    "phase": "review",
                },
                {
                    "journeyEventId": "event-b",
                    "eventType": "journey-event",
                    "ideaId": "idea-a",
                    "phase": "routing",
                },
            ]

            with patch.multiple(
                backend,
                STUDY_LOG_DIR=log_dir,
                JOURNEY_EVENT_LOG_FILE=journey_file,
            ):
                backend.write_jsonl_records(journey_file, records)
                result = backend.delete_journey_event(
                    backend.DeleteJourneyEventRequest(journeyEventId="event-a")
                )
                remaining, malformed = backend.read_journey_events()

        self.assertEqual(result["deletedJourneyEventCount"], 1)
        self.assertEqual(malformed, 0)
        self.assertEqual([record["journeyEventId"] for record in remaining], ["event-b"])


if __name__ == "__main__":
    unittest.main()
