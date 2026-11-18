import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.append(str(REPOSITORY_ROOT))

from Backend.sokoban_backend.schemas import (
    OnlineCoCreationFlowEventRequest,
    OnlineDesignerIntentionSyncRequest,
)
from Backend.sokoban_backend.services.online_rooms import build_cocreation_flow_event
from cocreation.api.config_models import ApiError
from cocreation.api.online_sync_and_dashboard import (
    _dashboard_node_status,
    calculate_cocreation_duration_seconds,
)
from cocreation.api.services import CoCreationSettings, HttpOnlineSyncGateway
from tests.fakes import FakeClock, create_test_client, create_test_services


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

    def _make_mock_gateway(
        self,
        handler,
        *,
        db_path: Path | None = None,
        base_url: str = "http://online.invalid:8000",
        secret: str = "test-sync-secret",
        timeout: float = 4.25,
    ):
        observed_timeouts: list[float | None] = []
        transport = httpx.MockTransport(handler)

        def client_factory(*, timeout: float | None = None):
            observed_timeouts.append(timeout)
            return httpx.Client(transport=transport, timeout=timeout)

        settings = CoCreationSettings(
            database_path=db_path or Path(":memory:"),
            online_backend_base_url=base_url,
            intention_sync_secret=secret,
            online_match_sync_timeout_seconds=timeout,
            sweeper_enabled=False,
        )
        gateway = HttpOnlineSyncGateway(settings, http_client_factory=client_factory)
        return gateway, settings, observed_timeouts

    def test_real_proposal_clarification_statuses_pass_8000_validation(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                status = _dashboard_node_status(
                    "proposal",
                    {
                        "proposalDiscovery": {
                            "status": "clarifying",
                            "clarificationQuestionCount": count,
                        }
                    },
                )
                payload = OnlineCoCreationFlowEventRequest(
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
                event = build_cocreation_flow_event(self.room, payload)
                self.assertEqual(event["nodeStatus"], f"clarifying_{count}")

    def test_full_twenty_minute_duration_passes_8000_validation(self):
        duration = calculate_cocreation_duration_seconds({
            "deadline_at": "2026-09-13T05:20:00Z",
            "finalized_at": "2026-09-13T05:20:00Z",
        })
        self.assertEqual(duration, 1200)
        payload = OnlineCoCreationFlowEventRequest(
            eventId="final:version-a",
            playerNumber=1,
            eventType="final",
            sessionId="session-a",
            versionId="version-a",
            stageNumber=2,
            rows=ROWS,
            coCreationDurationSeconds=duration,
        )
        event = build_cocreation_flow_event(self.room, payload)
        self.assertEqual(event["coCreationDurationSeconds"], 1200)

    def test_http_gateway_syncs_node_event_and_final_intention_over_mock_transport(self):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content.decode("utf-8")),
            })
            return httpx.Response(200, json={"status": "ok"})

        gateway, _settings, timeouts = self._make_mock_gateway(
            handler,
            secret="shared-sync-secret",
            timeout=3.5,
        )
        session = {
            "id": "session-a",
            "match_id": self.room["matchId"],
            "player_number": 1,
            "demo_mode": 0,
        }
        gateway.sync_event(
            session,
            "node",
            stage_number=2,
            version_id="version-a",
            metadata={
                "eventId": "node:proposal:topic-1:fp",
                "eventType": "node",
                "nodeId": "proposal:topic-1",
                "nodeType": "proposal",
                "nodeStatus": "clarifying_1",
                "nodeParentId": None,
                "versionId": "version-a",
                "stageNumber": 2,
                "nodeEntries": [{
                    "entryId": "llm:turn-1",
                    "kind": "llm_message",
                    "text": "Which play experience should guide this revision?",
                    "label": "LLM",
                    "occurredAt": "2026-09-13T05:08:18Z",
                    "language": "en",
                }],
            },
        )
        gateway.sync_final_intention(
            session,
            "Keep the opening move readable while guarding the right target.",
            event_id="message:session-a:intent-1",
        )

        self.assertEqual(timeouts, [3.5, 3.5])
        self.assertEqual(len(captured), 2)

        flow_req, intention_req = captured
        self.assertEqual(flow_req["method"], "POST")
        self.assertEqual(
            flow_req["url"],
            f"http://online.invalid:8000/online/rooms/{self.room['matchId']}/cocreation-events",
        )
        self.assertEqual(flow_req["headers"].get("x-cocreation-sync-secret"), "shared-sync-secret")
        validated_flow = OnlineCoCreationFlowEventRequest(**flow_req["body"])
        built_flow = build_cocreation_flow_event(self.room, validated_flow)
        self.assertEqual(built_flow["nodeStatus"], "clarifying_1")

        self.assertEqual(intention_req["method"], "POST")
        self.assertEqual(
            intention_req["url"],
            f"http://online.invalid:8000/online/rooms/{self.room['matchId']}/designer-intention",
        )
        self.assertEqual(intention_req["headers"].get("x-cocreation-sync-secret"), "shared-sync-secret")
        validated_intention = OnlineDesignerIntentionSyncRequest(**intention_req["body"])
        self.assertEqual(validated_intention.playerNumber, 1)
        self.assertEqual(validated_intention.sessionId, "session-a")
        self.assertEqual(validated_intention.eventId, "message:session-a:intent-1")
        self.assertIn("readable", validated_intention.designerIntention)

    def test_http_gateway_maps_errors_and_skips_demo_or_unlinked_sessions(self):
        calls = 0

        def reject_handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(422, json={"detail": "Invalid stage payload"})

        gateway, _settings, _timeouts = self._make_mock_gateway(reject_handler)
        demo_session = {"id": "demo-1", "match_id": self.room["matchId"], "player_number": 1, "demo_mode": 1}
        unlinked_session = {"id": "local-1", "match_id": "", "player_number": None, "demo_mode": 0}
        gateway.sync_event(demo_session, "node", metadata={"eventId": "e1", "eventType": "node"})
        gateway.sync_event(unlinked_session, "node", metadata={"eventId": "e2", "eventType": "node"})
        gateway.sync_final_intention(demo_session, "Ignored", event_id="ev-demo")
        self.assertEqual(calls, 0)

        live_session = {"id": "session-a", "match_id": self.room["matchId"], "player_number": 1, "demo_mode": 0}
        with self.assertRaises(ApiError) as rejected:
            gateway.sync_event(live_session, "node", metadata={"eventId": "e3", "eventType": "node"})
        self.assertEqual(rejected.exception.status_code, 502)
        self.assertEqual(rejected.exception.code, "ONLINE_FLOW_SYNC_REJECTED")
        self.assertFalse(rejected.exception.retryable)

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("8000 sync timed out", request=request)

        timeout_gateway, _settings, _timeouts = self._make_mock_gateway(timeout_handler)
        with self.assertRaises(ApiError) as timed_out:
            timeout_gateway.sync_final_intention(live_session, "Keep it readable.", event_id="ev-timeout")
        self.assertEqual(timed_out.exception.status_code, 503)
        self.assertEqual(timed_out.exception.code, "ONLINE_INTENTION_SYNC_UNAVAILABLE")
        self.assertTrue(timed_out.exception.retryable)

    def test_services_with_http_gateway_sync_first_stage_final_and_intention(self):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({
                "url": str(request.url),
                "secret": request.headers.get("X-CoCreation-Sync-Secret"),
                "body": json.loads(request.content.decode("utf-8")),
            })
            return httpx.Response(200, json={"status": "ok"})

        with tempfile.TemporaryDirectory() as tmp_dir:
            clock = FakeClock()
            gateway, settings, timeouts = self._make_mock_gateway(
                handler,
                db_path=Path(tmp_dir) / "cocreation.sqlite3",
                secret="e2e-sync-secret",
                timeout=6.0,
            )
            services = create_test_services(
                tmp_dir,
                settings_overrides={
                    "database_path": settings.database_path,
                    "online_backend_base_url": settings.online_backend_base_url,
                    "intention_sync_secret": settings.intention_sync_secret,
                    "online_match_sync_timeout_seconds": settings.online_match_sync_timeout_seconds,
                },
                online_sync=gateway,
                clock=clock,
            )
            with create_test_client(services) as client:
                created = client.post(
                    "/api/sessions",
                    json={
                        "rows": ROWS,
                        "initialDraftMethod": "partial_completion",
                        "language": "en",
                        "idempotencyKey": "sync-e2e-create",
                        "matchId": self.room["matchId"],
                        "playerNumber": 1,
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                session_id = created.json()["sessionId"]
                bootstrap = parse_qs(urlparse(created.json()["launchUrl"]).fragment)["bootstrap"][0]
                access = client.post(
                    f"/api/sessions/{session_id}/browser-access",
                    json={"bootstrapToken": bootstrap},
                )
                self.assertEqual(access.status_code, 200, access.text)

                lang_res = client.patch(
                    f"/api/sessions/{session_id}/language",
                    json={"language": "en"},
                )
                self.assertEqual(lang_res.status_code, 200, lang_res.text)
                first_version_id = lang_res.json()["currentVersionId"]

                clock.advance(1200)
                final_res = client.post(
                    f"/api/sessions/{session_id}/finalize",
                    json={
                        "baseVersionId": first_version_id,
                        "idempotencyKey": "sync-e2e-finalize",
                    },
                )
                self.assertEqual(final_res.status_code, 200, final_res.text)

                intention_res = client.post(
                    f"/api/sessions/{session_id}/intention",
                    json={
                        "content": "Require an early push commitment.",
                        "idempotencyKey": "sync-e2e-intention",
                    },
                )
                self.assertEqual(intention_res.status_code, 200, intention_res.text)

        self.assertEqual(timeouts, [6.0, 6.0, 6.0])
        self.assertEqual(len(captured), 3)
        self.assertTrue(all(item["secret"] == "e2e-sync-secret" for item in captured))

        first_stage_req, final_req, intention_req = captured
        self.assertEqual(
            first_stage_req["url"],
            f"http://online.invalid:8000/online/rooms/{self.room['matchId']}/cocreation-events",
        )
        first_stage_event = build_cocreation_flow_event(
            self.room,
            OnlineCoCreationFlowEventRequest(**first_stage_req["body"]),
        )
        self.assertEqual(first_stage_event["eventType"], "first_stage")
        self.assertEqual(first_stage_event["initialDraftMethod"], "partial_completion")

        final_event = build_cocreation_flow_event(
            self.room,
            OnlineCoCreationFlowEventRequest(**final_req["body"]),
        )
        self.assertEqual(final_event["eventType"], "final")
        self.assertEqual(final_event["coCreationDurationSeconds"], 1200)

        self.assertEqual(
            intention_req["url"],
            f"http://online.invalid:8000/online/rooms/{self.room['matchId']}/designer-intention",
        )
        intention_payload = OnlineDesignerIntentionSyncRequest(**intention_req["body"])
        self.assertEqual(intention_payload.designerIntention, "Require an early push commitment.")


if __name__ == "__main__":
    unittest.main()
