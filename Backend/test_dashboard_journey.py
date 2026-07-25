import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import Backend.app as backend


class DashboardJourneyTests(unittest.TestCase):
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
