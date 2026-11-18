from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from sokoban_backend.routes.dashboard import (
    delete_journey_event,
    get_level_records_data,
)
from sokoban_backend.schemas import DeleteJourneyEventRequest
from sokoban_backend.services.dashboard_projections import filter_frontend_ha_plan_records
from tests.fakes import create_test_app, create_test_container


TEST_DELETE_PASSWORD = "test-delete-password"


class DashboardJourneyTests(unittest.TestCase):
    def test_dashboard_password_verification_reuses_delete_password_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_test_app(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
            with TestClient(app) as client:
                success = client.post(
                    "/verify-dashboard-password",
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )
                wrong = client.post(
                    "/verify-dashboard-password",
                    headers={"X-Delete-Password": "wrong"},
                )

        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json(), {"ok": True})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["detail"], "Incorrect delete password")

    def test_dashboard_password_verification_fails_closed_without_configured_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_test_app(temp_dir, dashboard_delete_password="")
            with TestClient(app) as client:
                response = client.post(
                    "/verify-dashboard-password",
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Delete password is not configured")

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
            ("/delete-online-match", {"matchId": "match-a"}),
            ("/clear-level-records", {}),
            ("/clear-matchmaking-records", {}),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_test_app(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
            with TestClient(app) as client:
                for path, payload in routes:
                    with self.subTest(path=path):
                        response = client.post(path, json=payload)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(response.json()["detail"], "Incorrect delete password")

    def test_delete_route_fails_closed_without_configured_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_test_app(temp_dir, dashboard_delete_password="")
            with TestClient(app) as client:
                response = client.post(
                    "/delete-journey-event",
                    json={"journeyEventId": "event-a"},
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Delete password is not configured")

    def test_delete_route_rejects_wrong_password_without_changing_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            container = create_test_container(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
            records = [
                {
                    "journeyEventId": "event-a",
                    "eventType": "journey-event",
                    "ideaId": "idea-a",
                    "phase": "review",
                }
            ]
            container.store.write_jsonl_records(
                container.paths.journey_event_log_file,
                records,
            )
            with TestClient(create_test_app(temp_dir, container=container)) as client:
                response = client.post(
                    "/delete-journey-event",
                    json={"journeyEventId": "event-a"},
                    headers={"X-Delete-Password": "wrong"},
                )
            remaining, malformed = container.store.read_journey_events()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(malformed, 0)
        self.assertEqual(remaining, records)

    def test_delete_route_accepts_correct_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            container = create_test_container(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
            container.store.write_jsonl_records(
                container.paths.journey_event_log_file,
                [
                    {
                        "journeyEventId": "event-a",
                        "eventType": "journey-event",
                        "ideaId": "idea-a",
                        "phase": "review",
                    }
                ],
            )
            with TestClient(create_test_app(temp_dir, container=container)) as client:
                response = client.post(
                    "/delete-journey-event",
                    json={"journeyEventId": "event-a"},
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )
            remaining, malformed = container.store.read_journey_events()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deletedJourneyEventCount"], 1)
        self.assertEqual(malformed, 0)
        self.assertEqual(remaining, [])

    def test_delete_idea_records_accepts_correct_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            container = create_test_container(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
            paths = container.paths
            container.store.write_jsonl_records(paths.study_log_file, [
                {"creativeIdeaId": "idea-a"},
                {"creativeIdeaId": "idea-b"},
            ])
            container.store.write_jsonl_records(paths.survey_log_file, [
                {"creativeIdeaId": "idea-a", "sessionId": "session-a"},
                {"creativeIdeaId": "idea-b", "sessionId": "session-b"},
            ])
            container.store.write_jsonl_records(paths.creative_idea_log_file, [
                {"ideaId": "idea-a"},
                {"ideaId": "idea-b"},
            ])
            container.store.write_jsonl_records(
                paths.creative_expansion_choice_log_file,
                [{"ideaId": "idea-a"}, {"ideaId": "idea-b"}],
            )
            container.store.write_jsonl_records(paths.ha_plan_event_log_file, [
                {"ideaId": "idea-a"},
                {"ideaId": "idea-b"},
            ])
            container.store.write_jsonl_records(paths.journey_event_log_file, [
                {"ideaId": "idea-a"},
                {"ideaId": "idea-b"},
            ])

            with TestClient(create_test_app(temp_dir, container=container)) as client:
                response = client.post(
                    "/delete-idea-records",
                    json={"ideaId": "idea-a"},
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                )

            tracked_files = (
                paths.study_log_file,
                paths.survey_log_file,
                paths.creative_idea_log_file,
                paths.creative_expansion_choice_log_file,
                paths.ha_plan_event_log_file,
                paths.journey_event_log_file,
            )
            remaining_records = {
                path.name: container.store.read_jsonl_records(path)[0]
                for path in tracked_files
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
            container = create_test_container(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
            paths = container.paths
            tracked_files = (
                paths.study_log_file,
                paths.survey_log_file,
                paths.creative_idea_log_file,
                paths.creative_expansion_choice_log_file,
                paths.ha_plan_event_log_file,
                paths.journey_event_log_file,
            )
            for path in tracked_files:
                container.store.write_jsonl_records(path, [{"record": path.name}])

            with TestClient(create_test_app(temp_dir, container=container)) as client:
                response = client.post(
                    "/clear-level-records",
                    headers={"X-Delete-Password": TEST_DELETE_PASSWORD},
                    follow_redirects=False,
                )
            remaining_records = [
                container.store.read_jsonl_records(path)[0]
                for path in tracked_files
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

        filtered = filter_frontend_ha_plan_records(records)

        self.assertEqual(len(filtered), 2)
        self.assertTrue(all(record["ideaId"] == "idea-a" for record in filtered))

    def test_dashboard_payload_exposes_ha_and_journey_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            container = create_test_container(Path(temp_dir))
            paths = container.paths
            container.store.write_jsonl_records(paths.creative_idea_log_file, [
                {
                    "eventType": "creative-idea",
                    "ideaId": "idea-a",
                    "ideaText": "A compact route.",
                    "sessionId": "session-a",
                    "officialRound": True,
                }
            ])
            container.store.write_jsonl_records(paths.ha_plan_event_log_file, [
                {
                    "eventType": "ha-plan-choice",
                    "ideaId": "idea-a",
                    "selectedOptionTitle": "Keep the route",
                    "officialRound": True,
                }
            ])
            container.store.write_jsonl_records(paths.journey_event_log_file, [
                {
                    "eventType": "journey-event",
                    "ideaId": "idea-a",
                    "phase": "review",
                    "action": "retry",
                    "officialRound": True,
                }
            ])

            payload = get_level_records_data(container)

        self.assertEqual(payload["haPlanSummary"]["choiceCount"], 1)
        self.assertEqual(payload["journeyEventSummary"]["phaseCounts"]["review"], 1)
        self.assertTrue(payload["haPlanEvents"][0]["haEventId"])
        self.assertTrue(payload["journeyEvents"][0]["journeyEventId"])

    def test_delete_journey_event_removes_only_target_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            container = create_test_container(
                temp_dir,
                dashboard_delete_password=TEST_DELETE_PASSWORD,
            )
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
            container.store.write_jsonl_records(
                container.paths.journey_event_log_file,
                records,
            )
            result = delete_journey_event(
                DeleteJourneyEventRequest(journeyEventId="event-a"),
                container,
            )
            remaining, malformed = container.store.read_journey_events()

        self.assertEqual(result["deletedJourneyEventCount"], 1)
        self.assertEqual(malformed, 0)
        self.assertEqual([record["journeyEventId"] for record in remaining], ["event-b"])


if __name__ == "__main__":
    unittest.main()
