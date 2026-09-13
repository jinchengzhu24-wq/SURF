import sys
import unittest
from pathlib import Path


COCREATION_BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

if str(COCREATION_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(COCREATION_BACKEND_DIR))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.append(str(REPOSITORY_ROOT))

import app as cocreation_backend
import Backend.app as online_backend


ROWS = [
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


class OnlineSyncContractTests(unittest.TestCase):
    def setUp(self):
        self.room = {
            "matchId": "a" * 32,
            "roomCode": "ABC123",
            "players": [{"playerNumber": 1, "studySessionId": "study-a"}],
        }

    def test_real_proposal_clarification_statuses_pass_8000_validation(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                status = cocreation_backend._dashboard_node_status(
                    "proposal",
                    {
                        "proposalDiscovery": {
                            "status": "clarifying",
                            "clarificationQuestionCount": count,
                        }
                    },
                )
                payload = online_backend.OnlineCoCreationFlowEventRequest(
                    eventId=f"node:proposal:topic-{count}:fingerprint",
                    playerNumber=1,
                    eventType="node",
                    sessionId="session-a",
                    versionId="version-a",
                    stageNumber=2,
                    nodeId=f"proposal:topic-{count}",
                    nodeType="proposal",
                    nodeStatus=status,
                    nodeEntries=[{
                        "entryId": f"llm:turn-{count}",
                        "kind": "llm_message",
                        "text": "Which play experience should guide this revision?",
                        "label": "LLM",
                        "occurredAt": "2026-09-13T05:08:18Z",
                        "language": "en",
                    }],
                )
                event = online_backend.build_cocreation_flow_event(self.room, payload)
                self.assertEqual(event["nodeStatus"], f"clarifying_{count}")

    def test_full_twenty_minute_duration_passes_8000_validation(self):
        duration = cocreation_backend.calculate_cocreation_duration_seconds({
            "deadline_at": "2026-09-13T05:20:00Z",
            "finalized_at": "2026-09-13T05:20:00Z",
        })
        self.assertEqual(duration, 1200)
        payload = online_backend.OnlineCoCreationFlowEventRequest(
            eventId="final:version-a",
            playerNumber=1,
            eventType="final",
            sessionId="session-a",
            versionId="version-a",
            stageNumber=2,
            rows=ROWS,
            coCreationDurationSeconds=duration,
        )
        event = online_backend.build_cocreation_flow_event(self.room, payload)
        self.assertEqual(event["coCreationDurationSeconds"], 1200)


if __name__ == "__main__":
    unittest.main()
