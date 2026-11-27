"""TDD tests for self-hosted /survey/ mount, atomic responseId dedup, export surveys, and deploy."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from server.application import DashboardService, OnlineRoomService
from server.core.clock import FakeClock
from server.main import create_app
from server.persistence import RoomsRepository, SQLiteDatabase, StudyRepository
from server.settings import SokobanSettings
from tests.conftest import BASE_ROWS, extract_hash_param

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_survey_payload(
    *,
    survey_id: str = "online_pre_match_survey",
    survey_version: str = "v1.0-wjx-migrated",
    entry_source: str = "unity_matchmaking",
    response_id: str | None = "resp-001",
    study_session_id: str = "",
    match_id: str = "",
    room_code: str = "",
    player_number: int = 0,
    participant_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "survey-response",
        "surveyId": survey_id,
        "surveyVersion": survey_version,
        "surveyTitle": (
            "匹配前简短问卷 / Pre-Match Short Questionnaire"
            if survey_id == "online_pre_match_survey"
            else "赛后问卷 / Post-Game Questionnaire"
        ),
        "entrySource": entry_source,
        "studySessionId": study_session_id,
        "sessionId": study_session_id,
        "matchId": match_id,
        "roomCode": room_code,
        "playerNumber": player_number,
        "language": "bilingual",
        "submittedAt": "2026-11-27T10:00:00Z",
        "durationSeconds": 42.5,
        "answers": [
            {
                "sectionId": "demographics",
                "questionIndex": 1,
                "questionId": "gender",
                "questionType": "single_choice",
                "questionTextZh": "您的性别是？",
                "questionTextEn": "What is your gender?",
                "optionId": "man",
                "optionIndex": 1,
                "optionTextZh": "男",
                "optionTextEn": "Man",
                "score": None,
                "textValue": None,
            }
        ],
    }
    if response_id is not None:
        payload["responseId"] = response_id
    if participant_id is not None:
        payload["participantId"] = participant_id
    if extra:
        payload.update(extra)
    return payload


def test_survey_static_mount_no_cache_and_query_preserving_redirect(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    survey_dir = static_dir / "survey"
    (survey_dir / "assets").mkdir(parents=True)
    (survey_dir / "index.html").write_text(
        "<!doctype html><html><body><div id='app'>Survey</div></body></html>",
        encoding="utf-8",
    )
    (survey_dir / "assets" / "app.js").write_text("console.log('survey');", encoding="utf-8")

    settings = SokobanSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "survey_static.sqlite3",
        static_dir=static_dir,
        token_secret="unit-test-strong-secret-key-987654",
        reaper_enabled=False,
    )
    assert settings.survey_dist_dir == survey_dir

    app = create_app(settings=settings)
    with TestClient(app) as client:
        for path in ("/survey/", "/survey/index.html"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 200, (path, resp.status_code)
            assert "Survey" in resp.text
            assert resp.headers.get("Cache-Control") == "no-cache, must-revalidate", path

        # Bare /survey?type=pre MUST preserve query parameters across trailing-slash redirect!
        redir = client.get(
            "/survey?type=pre&source=unity&studySessionId=st-123",
            follow_redirects=False,
        )
        assert redir.status_code in (301, 302, 307, 308)
        assert redir.headers.get("Cache-Control") == "no-cache, must-revalidate"
        location = redir.headers.get("location", "")
        parsed = urlparse(location)
        assert parsed.path.endswith("/survey/")
        assert parse_qs(parsed.query) == {
            "type": ["pre"],
            "source": ["unity"],
            "studySessionId": ["st-123"],
        }

        followed = client.get("/survey?type=pre", follow_redirects=True)
        assert followed.status_code == 200
        assert "Survey" in followed.text
        assert followed.url.path == "/survey/"
        assert followed.url.query == b"type=pre"
        assert followed.headers.get("Cache-Control") == "no-cache, must-revalidate"

        asset = client.get("/survey/assets/app.js")
        assert asset.status_code == 200
        assert "survey" in asset.text


def test_deploy_configs_include_survey_workspace_and_preserve_query_strings() -> None:
    nginx_conf = (REPO_ROOT / "deploy" / "nginx-sokoban.conf").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    deploy_ps1 = (REPO_ROOT / "deploy" / "deploy.ps1").read_text(encoding="utf-8")

    # Verify all four symmetric /survey location blocks exist in nginx-sokoban.conf.
    for exact_path in ("/survey", "/survey/", "/survey/index.html"):
        pattern = re.compile(
            rf"location\s*=\s*{re.escape(exact_path)}\s*\{{(?P<body>[^}}]*)\}}",
            re.MULTILINE,
        )
        match = pattern.search(nginx_conf)
        assert match is not None, f"Missing nginx block: location = {exact_path}"
        body = match.group("body")
        assert "proxy_pass http://sokoban_backend;" in body
        assert "proxy_hide_header Cache-Control;" in body
        assert 'add_header Cache-Control "no-cache, must-revalidate" always;' in body
        # Ensure we NEVER emit a query-stripping redirect like `return 301 https://$host/survey/;`
        assert "return 301" not in body and "return 302" not in body and "return 308" not in body

    prefix_match = re.search(r"location\s+/survey/\s*\{(?P<body>[^}]*)\}", nginx_conf)
    assert prefix_match is not None, "Missing nginx block: location /survey/"
    assert "proxy_pass http://sokoban_backend;" in prefix_match.group("body")

    # Verify Dockerfile copies web/apps/survey/package.json and builds @sokoban/survey.
    assert "COPY web/apps/survey/package.json ./apps/survey/package.json" in dockerfile
    build_lines = [line for line in dockerfile.splitlines() if "npm run build" in line]
    assert build_lines and any("--workspace=@sokoban/survey" in line for line in build_lines)

    # Verify deploy.ps1 describes @sokoban/survey alongside the other workspaces.
    assert "@sokoban/survey" in deploy_ps1


@pytest.mark.asyncio
async def test_record_survey_response_full_payload_and_atomic_response_id_deduplication(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    payload = _make_survey_payload(
        survey_id="online_post_match_survey",
        entry_source="unity_matchmaking",
        response_id="resp-atomic-001",
        study_session_id="st-dedup-1",
        match_id="match-dedup-1",
        room_code="ABCD12",
        player_number=2,
        participant_id="part-dedup-1",
    )

    async with bundle.client() as client:
        first = await client.post("/record-survey-response", json=payload)
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "ok"
        assert first_body["recordType"] == "survey"
        assert first_body["eventType"] == "survey-response"
        assert first_body["deduplicated"] is False
        original_id = first_body["id"]

        # Replay with the same responseId returns the original row id and deduplicated=True.
        replay = await client.post("/record-survey-response", json=payload)
        assert replay.status_code == 200
        replay_body = replay.json()
        assert replay_body["id"] == original_id
        assert replay_body["deduplicated"] is True
        assert replay_body["recordType"] == "survey"

        # Concurrent submissions with a fresh responseId must atomically persist exactly ONE row.
        concurrent_payload = _make_survey_payload(
            response_id="resp-concurrent-999",
            study_session_id="st-concurrent-1",
        )
        responses = await asyncio.gather(
            *[
                client.post("/record-survey-response", json=concurrent_payload)
                for _ in range(6)
            ]
        )
        assert all(r.status_code == 200 for r in responses)
        bodies = [r.json() for r in responses]
        returned_ids = {b["id"] for b in bodies}
        assert len(returned_ids) == 1
        assert sum(1 for b in bodies if b["deduplicated"] is False) == 1
        assert sum(1 for b in bodies if b["deduplicated"] is True) == 5

        # Legacy survey submissions without responseId still insert distinct rows.
        legacy_a = await client.post(
            "/record-survey-response",
            json=_make_survey_payload(response_id=None, study_session_id="st-legacy"),
        )
        legacy_b = await client.post(
            "/record-survey-response",
            json=_make_survey_payload(response_id=None, study_session_id="st-legacy"),
        )
        assert legacy_a.status_code == 200 and legacy_b.status_code == 200
        assert legacy_a.json()["id"] != legacy_b.json()["id"]
        assert legacy_a.json()["deduplicated"] is False
        assert legacy_b.json()["deduplicated"] is False

    stored_surveys = bundle.container.study_repo.query_records(record_type="survey")
    assert len(stored_surveys) == 4
    first_stored = next(r for r in stored_surveys if r.id == original_id)
    assert first_stored.match_id == "match-dedup-1"
    assert first_stored.participant_id == "part-dedup-1"
    assert first_stored.payload["studySessionId"] == "st-dedup-1"
    assert first_stored.payload["responseId"] == "resp-atomic-001"
    assert len(first_stored.payload["answers"]) == 1

    # Existing StudyRepository.insert_record() signature stays 100% backwards-compatible.
    direct = bundle.container.study_repo.insert_record(
        "survey",
        {"eventType": "survey-response", "q1": "ok"},
        match_id=None,
        participant_id="direct-p1",
        round_id=None,
        created_at="2026-11-27T10:05:00Z",
    )
    assert direct.id > 0 and direct.record_type == "survey"


@pytest.mark.asyncio
async def test_matchmaking_research_export_associates_surveys_excludes_standalone_and_option_a(
    app_bundle_factory,
    tmp_path: Path,
) -> None:
    bundle = app_bundle_factory()
    # Explicit DI: container must wire study_repo into DashboardService.
    assert bundle.container.dashboard_service._study_repo is bundle.container.study_repo

    async def _create_active_match(
        client: Any,
        *,
        p1_sid: str,
        p2_sid: str,
        idem_prefix: str,
    ) -> tuple[str, str]:
        r1 = (
            await client.post(
                "/online/rooms",
                json={"studySessionId": p1_sid, "menuStartedFlow": True},
            )
        ).json()
        m_id = r1["matchId"]
        r_code = r1["roomCode"]
        r2 = (
            await client.post(
                "/online/rooms/join",
                json={"roomCode": r_code, "studySessionId": p2_sid, "menuStartedFlow": True},
            )
        ).json()
        await client.post(
            f"/online/rooms/{m_id}/ready",
            json={"ready": True},
            headers={"X-Player-Token": r1["playerToken"]},
        )
        await client.post(
            f"/online/rooms/{m_id}/ready",
            json={"ready": True},
            headers={"X-Player-Token": r2["playerToken"]},
        )
        co = (
            await client.post(
                "/api/sessions",
                json={
                    "rows": BASE_ROWS,
                    "initialDraftMethod": "partial_completion",
                    "language": "en",
                    "idempotencyKey": f"{idem_prefix}-co",
                    "matchId": m_id,
                    "playerNumber": 1,
                },
            )
        ).json()
        sid = co["sessionId"]
        assert (
            await client.post(
                f"/api/sessions/{sid}/browser-access",
                json={"bootstrapToken": extract_hash_param(co["launchUrl"], "bootstrap")},
            )
        ).status_code == 200
        assert (
            await client.patch(f"/api/sessions/{sid}/language", json={"language": "en"})
        ).status_code == 200
        return m_id, r_code

    async with bundle.client() as client:
        # Two formal matches sharing P1 studySessionId to prove matchId isolation!
        m1, rc1 = await _create_active_match(
            client,
            p1_sid="study-p1-shared",
            p2_sid="study-p2-match1",
            idem_prefix="m1",
        )
        m2, rc2 = await _create_active_match(
            client,
            p1_sid="study-p1-shared",
            p2_sid="study-p2-match2",
            idem_prefix="m2",
        )

        # 1. Online1 pre-match for P1 with participantId ALSO set (SQLite column != studySessionId)
        #    plus an accidental secret key to verify redaction.
        pre_p1 = (
            await client.post(
                "/record-survey-response",
                json=_make_survey_payload(
                    survey_id="online_pre_match_survey",
                    entry_source="unity_matchmaking",
                    response_id="resp-pre-p1",
                    study_session_id="study-p1-shared",
                    participant_id="EXTERNAL-PID-999",
                    match_id="",
                    player_number=0,
                    extra={"accessToken": "secret-must-be-stripped"},
                ),
            )
        ).json()

        # 2. Online2 post-match for P1 in Match 1 (MUST attach ONLY to m1, NEVER cross-leak to m2!)
        post_m1_p1 = (
            await client.post(
                "/record-survey-response",
                json=_make_survey_payload(
                    survey_id="online_post_match_survey",
                    entry_source="unity_matchmaking",
                    response_id="resp-post-m1-p1",
                    study_session_id="study-p1-shared",
                    match_id=m1,
                    room_code=rc1,
                    player_number=1,
                ),
            )
        ).json()

        # 3. Online2 post-match fallback WITHOUT matchId for P2 in Match 1 (studySessionId match)
        post_m1_p2_fb = (
            await client.post(
                "/record-survey-response",
                json=_make_survey_payload(
                    survey_id="online_post_match_survey",
                    entry_source="unity_matchmaking",
                    response_id="resp-post-m1-p2-fb",
                    study_session_id="study-p2-match1",
                    match_id="",
                    player_number=0,
                ),
            )
        ).json()

        # 4. Online2 post-match for P2 in Match 2
        post_m2_p2 = (
            await client.post(
                "/record-survey-response",
                json=_make_survey_payload(
                    survey_id="online_post_match_survey",
                    entry_source="unity_matchmaking",
                    response_id="resp-post-m2-p2",
                    study_session_id="study-p2-match2",
                    match_id=m2,
                    room_code=rc2,
                    player_number=2,
                ),
            )
        ).json()

        # 5. Standalone test survey carrying matching IDs: MUST be excluded from research export,
        #    while preserved in /export/study-logs.jsonl!
        await client.post(
            "/record-survey-response",
            json=_make_survey_payload(
                survey_id="online_pre_match_survey",
                entry_source="Standalone ",
                response_id="resp-standalone-1",
                study_session_id="study-p1-shared",
                match_id=m1,
                room_code=rc1,
                player_number=1,
            ),
        )

        # Verify /export/matchmaking-research.jsonl
        export_resp = await client.get("/export/matchmaking-research.jsonl")
        assert export_resp.status_code == 200
        rows = [json.loads(line) for line in export_resp.text.splitlines() if line.strip()]
        assert len(rows) == 2
        by_match = {row["match"]["matchId"]: row for row in rows}
        assert set(by_match.keys()) == {m1, m2}

        # Match 1 checks
        m1_row = by_match[m1]
        assert m1_row["schemaVersion"] == 1
        assert m1_row["exportedAt"]
        assert isinstance(m1_row["roomEvents"], list)
        assert isinstance(m1_row["coCreationSessions"], dict)
        m1_surveys = m1_row["surveys"]
        assert [s["recordId"] for s in m1_surveys] == [
            pre_p1["id"],
            post_m1_p1["id"],
            post_m1_p2_fb["id"],
        ]
        assert m1_surveys[0]["surveyId"] == "online_pre_match_survey"
        assert m1_surveys[0]["surveyVersion"] == "v1.0-wjx-migrated"
        assert m1_surveys[0]["entrySource"] == "unity_matchmaking"
        assert m1_surveys[0]["playerSlot"] == 1
        assert m1_surveys[0]["matchedBy"] == "studySessionId"
        assert m1_surveys[0]["createdAt"]
        assert "accessToken" not in m1_surveys[0]["payload"]
        assert "secret-must-be-stripped" not in export_resp.text

        assert m1_surveys[1]["surveyId"] == "online_post_match_survey"
        assert m1_surveys[1]["playerSlot"] == 1
        assert m1_surveys[1]["matchedBy"] == "matchId"

        assert m1_surveys[2]["surveyId"] == "online_post_match_survey"
        assert m1_surveys[2]["playerSlot"] == 2
        assert m1_surveys[2]["matchedBy"] == "studySessionId"

        # Match 2 checks: pre_p1 + post_m2_p2 ONLY (no cross-leak from post_m1_p1, no standalone!)
        m2_surveys = by_match[m2]["surveys"]
        assert [s["recordId"] for s in m2_surveys] == [pre_p1["id"], post_m2_p2["id"]]
        assert m2_surveys[0]["matchedBy"] == "studySessionId"
        assert m2_surveys[0]["playerSlot"] == 1
        assert m2_surveys[1]["matchedBy"] == "matchId"
        assert m2_surveys[1]["playerSlot"] == 2
        assert "resp-standalone-1" not in export_resp.text

        # Legacy /export/study-logs.jsonl remains unchanged and INCLUDES standalone response.
        study_logs_resp = await client.get("/export/study-logs.jsonl")
        assert study_logs_resp.status_code == 200
        study_log_rows = [
            json.loads(line) for line in study_logs_resp.text.splitlines() if line.strip()
        ]
        assert len(study_log_rows) == 5
        assert any(r.get("responseId") == "resp-standalone-1" for r in study_log_rows)

        # Option A: delete_online_match removes m1 from matchmaking export, preserves study_records!
        del_resp = await client.post(
            "/delete-online-match",
            json={"matchId": m1},
            headers={"X-Delete-Password": "delete-pass-123"},
        )
        assert del_resp.status_code == 200

        export_after = await client.get("/export/matchmaking-research.jsonl")
        after_rows = [json.loads(line) for line in export_after.text.splitlines() if line.strip()]
        assert [r["match"]["matchId"] for r in after_rows] == [m2]
        assert len(bundle.container.study_repo.query_records(record_type="survey")) == 5

    # DashboardService constructed without study_repo (backwards-compatible default None) emits [].
    clock = FakeClock()
    db_no_repo = SQLiteDatabase(tmp_path / "no_study_repo.sqlite3")
    db_no_repo.initialize()
    rooms_repo = RoomsRepository(db_no_repo)
    StudyRepository(db_no_repo).insert_record(
        "survey",
        _make_survey_payload(study_session_id="s-only"),
        created_at=clock.utc_now(),
    )
    room_svc = OnlineRoomService(rooms_repo, clock=clock)
    r_only = await room_svc.create_room(study_session_id="s-only", menu_started_flow=True)
    with db_no_repo.write_tx() as conn:
        room_svc.record_cocreation_event_tx(
            conn,
            match_id=r_only["matchId"],
            player_slot=1,
            event_payload={
                "eventId": "ev-fs-only",
                "eventType": "first_stage",
                "sessionId": "co-only",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "partial_completion",
                "rows": BASE_ROWS,
            },
        )
    dash_no_study = DashboardService(rooms_repo, db=db_no_repo, clock=clock)
    lines = [json.loads(line) async for line in dash_no_study.export_matchmaking_research_jsonl()]
    assert len(lines) == 1
    assert lines[0]["surveys"] == []
