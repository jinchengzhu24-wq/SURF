"""TDD tests for survey analytics, wide-table CSV export, and match.surveys."""

from __future__ import annotations

import ast
import csv
import io
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from server.application import DashboardService, OnlineRoomService
from server.application.analytics._survey_analytics import (
    build_survey_analytics,
    build_surveys_csv,
)
from server.core.clock import FakeClock
from server.persistence import RoomsRepository, SQLiteDatabase, StudyRepository
from tests.conftest import BASE_ROWS, extract_hash_param

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_rich_survey_payload(
    *,
    survey_id: str = "online_pre_match_survey",
    survey_version: str = "v1.0-wjx-migrated",
    survey_title: str | None = None,
    entry_source: str = "unity_matchmaking",
    response_id: str | None = "resp-001",
    study_session_id: str = "",
    match_id: str = "",
    room_code: str = "",
    player_number: int = 0,
    participant_id: str | None = None,
    experiment_condition: str | None = None,
    language: str = "bilingual",
    submitted_at: str = "2026-11-27T10:00:00Z",
    duration_seconds: float | None = 42.5,
    answers: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_title = (
        "匹配前简短问卷 / Pre-Match Short Questionnaire"
        if survey_id == "online_pre_match_survey"
        else "赛后问卷 / Post-Game Questionnaire"
    )
    payload: dict[str, Any] = {
        "eventType": "survey-response",
        "surveyId": survey_id,
        "surveyVersion": survey_version,
        "surveyTitle": survey_title if survey_title is not None else default_title,
        "entrySource": entry_source,
        "studySessionId": study_session_id,
        "sessionId": study_session_id,
        "matchId": match_id,
        "roomCode": room_code,
        "playerNumber": player_number,
        "language": language,
        "submittedAt": submitted_at,
    }
    if duration_seconds is not None:
        payload["durationSeconds"] = duration_seconds
    if response_id is not None:
        payload["responseId"] = response_id
    if participant_id is not None:
        payload["participantId"] = participant_id
    if experiment_condition is not None:
        payload["experimentCondition"] = experiment_condition
    if answers is not None:
        payload["answers"] = answers
    if extra:
        payload.update(extra)
    return payload


def _single_choice_answer(
    *,
    section_id: str = "pre_match_background",
    question_index: int = 1,
    question_id: str = "gender",
    zh: str = "您的性别是？",
    en: str = "What is your gender?",
    option_id: str = "man",
    option_index: int = 1,
    option_zh: str = "男",
    option_en: str = "Man",
) -> dict[str, Any]:
    return {
        "sectionId": section_id,
        "questionIndex": question_index,
        "questionId": question_id,
        "questionType": "single_choice",
        "questionTextZh": zh,
        "questionTextEn": en,
        "optionId": option_id,
        "optionIndex": option_index,
        "optionTextZh": option_zh,
        "optionTextEn": option_en,
        "score": None,
        "textValue": None,
    }


def _likert_answer(
    *,
    section_id: str = "part_a",
    question_index: int = 2,
    question_id: str = "rel_comfort",
    zh: str = "相处自在",
    en: str = "I feel comfortable",
    score: int | float = 4,
    option_zh: str = "同意",
    option_en: str = "Agree",
) -> dict[str, Any]:
    return {
        "sectionId": section_id,
        "questionIndex": question_index,
        "questionId": question_id,
        "questionType": "likert_5",
        "questionTextZh": zh,
        "questionTextEn": en,
        "optionId": str(score),
        "optionIndex": int(score) if isinstance(score, int) else None,
        "optionTextZh": option_zh,
        "optionTextEn": option_en,
        "score": score,
        "textValue": None,
    }


def _textarea_answer(
    *,
    section_id: str = "part_c",
    question_index: int = 22,
    question_id: str = "open_feedback",
    zh: str = "补充意见",
    en: str = "Additional feedback",
    text_value: str = "Great level pacing!",
) -> dict[str, Any]:
    return {
        "sectionId": section_id,
        "questionIndex": question_index,
        "questionId": question_id,
        "questionType": "textarea",
        "questionTextZh": zh,
        "questionTextEn": en,
        "optionId": None,
        "optionIndex": None,
        "optionTextZh": None,
        "optionTextEn": None,
        "score": None,
        "textValue": text_value,
    }


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


def test_survey_analytics_module_is_pure_with_zero_fastapi_or_sql_imports() -> None:
    module_path = REPO_ROOT / "server" / "application" / "analytics" / "_survey_analytics.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"fastapi", "starlette", "sqlite3", "sqlalchemy"}
    overlap = imported & forbidden
    assert not overlap, f"Forbidden imports in _survey_analytics.py: {overlap}"


@pytest.mark.asyncio
async def test_matchmaking_records_attaches_surveys_and_keeps_export_structure(
    app_bundle_factory,
    tmp_path: Path,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        m1, rc1 = await _create_active_match(
            client, p1_sid="st-p1-a", p2_sid="st-p2-a", idem_prefix="att1"
        )
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_pre_match_survey",
                response_id="resp-att-pre",
                study_session_id="st-p1-a",
                answers=[_single_choice_answer()],
                extra={"accessToken": "must-not-leak"},
            ),
        )
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_post_match_survey",
                response_id="resp-att-post",
                study_session_id="st-p2-a",
                match_id=m1,
                room_code=rc1,
                player_number=2,
                answers=[_likert_answer()],
            ),
        )
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_pre_match_survey",
                entry_source="standalone",
                response_id="resp-att-standalone",
                study_session_id="st-p1-a",
                match_id=m1,
                answers=[_single_choice_answer()],
            ),
        )

        proj_resp = await client.get("/matchmaking-records-data")
        assert proj_resp.status_code == 200
        proj = proj_resp.json()
        assert len(proj["matches"]) == 1
        match = proj["matches"][0]
        assert isinstance(match["surveys"], list)
        assert len(match["surveys"]) == 2
        assert [s["surveyId"] for s in match["surveys"]] == [
            "online_pre_match_survey",
            "online_post_match_survey",
        ]
        assert match["surveys"][0]["playerSlot"] == 1
        assert match["surveys"][0]["matchedBy"] == "studySessionId"
        assert "accessToken" not in match["surveys"][0]["payload"]
        assert match["surveys"][1]["playerSlot"] == 2
        assert match["surveys"][1]["matchedBy"] == "matchId"

        # /export/matchmaking-research.jsonl must keep top-level surveys and NOT duplicate in match
        ndjson_resp = await client.get("/export/matchmaking-research.jsonl")
        assert ndjson_resp.status_code == 200
        lines = [json.loads(ln) for ln in ndjson_resp.text.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "surveys" in lines[0]
        assert len(lines[0]["surveys"]) == 2
        assert "surveys" not in lines[0]["match"]

    # DashboardService without study_repo emits surveys: []
    clock = FakeClock()
    db_no_repo = SQLiteDatabase(tmp_path / "no_study_proj.sqlite3")
    db_no_repo.initialize()
    rooms_repo = RoomsRepository(db_no_repo)
    room_svc = OnlineRoomService(rooms_repo, clock=clock)
    r_only = await room_svc.create_room(study_session_id="s-only", menu_started_flow=True)
    with db_no_repo.write_tx() as conn:
        room_svc.record_cocreation_event_tx(
            conn,
            match_id=r_only["matchId"],
            player_slot=1,
            event_payload={
                "eventId": "ev-fs-1",
                "eventType": "first_stage",
                "sessionId": "co-only",
                "versionId": "v1",
                "stageNumber": 1,
                "initialDraftMethod": "partial_completion",
                "rows": BASE_ROWS,
            },
        )
    dash_no_study = DashboardService(rooms_repo, db=db_no_repo, clock=clock)
    no_study_proj = await dash_no_study.get_matchmaking_records()
    assert no_study_proj["matches"][0]["surveys"] == []


@pytest.mark.asyncio
async def test_survey_analytics_scopes_dedup_prematch_fallback_option_a_and_errors(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        # Two active matches sharing P1 studySessionId + one match to delete (Option A)
        m1, rc1 = await _create_active_match(
            client, p1_sid="p1-shared", p2_sid="p2-m1", idem_prefix="sc-m1"
        )
        bundle.clock.advance(10.0)
        m2, rc2 = await _create_active_match(
            client, p1_sid="p1-shared", p2_sid="p2-m2", idem_prefix="sc-m2"
        )
        bundle.clock.advance(10.0)
        m_del, rc_del = await _create_active_match(
            client, p1_sid="p1-del", p2_sid="p2-del", idem_prefix="sc-del"
        )

        # 1. Pre-match survey WITHOUT matchId/roomCode/playerNumber -> links to both m1 and m2
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_pre_match_survey",
                survey_version="v1.0-wjx-migrated",
                response_id="resp-pre-shared",
                study_session_id="p1-shared",
                match_id="",
                room_code="",
                player_number=0,
                submitted_at="2026-11-27T10:01:00Z",
                duration_seconds=25.0,
                answers=[
                    _single_choice_answer(
                        option_id="woman",
                        option_index=2,
                        option_zh="女",
                        option_en="Woman",
                    ),
                    _textarea_answer(
                        section_id="pre_match_background",
                        question_index=5,
                        question_id="pre_note",
                        text_value="Pre-match comment",
                    ),
                ],
                extra={"playerToken": "secret-tok-123"},
            ),
        )

        # 2. Post-match survey for m1 P2
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_post_match_survey",
                survey_version="v1.0-wjx-migrated",
                response_id="resp-post-m1-p2",
                study_session_id="p2-m1",
                match_id=m1,
                room_code=rc1,
                player_number=2,
                submitted_at="2026-11-27T10:02:00Z",
                duration_seconds=100.0,
                answers=[_likert_answer(score=5, option_zh="非常同意", option_en="Strongly agree")],
            ),
        )

        # 3. Post-match survey for m_del (will become orphan formal survey after Option A delete)
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_post_match_survey",
                survey_version="v2.0-pilot",
                response_id="resp-post-deleted",
                study_session_id="p1-del",
                match_id=m_del,
                room_code=rc_del,
                player_number=1,
                submitted_at="2026-11-27T10:03:00Z",
                duration_seconds=60.0,
                answers=[_likert_answer(score=2, option_zh="不同意", option_en="Disagree")],
            ),
        )

        # 4. Standalone survey
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="alpha_custom_survey",
                survey_version="v1.0-custom",
                survey_title="Alpha Custom Survey",
                entry_source="Standalone ",
                response_id="resp-standalone-alpha",
                study_session_id="p1-shared",
                match_id=m1,
                submitted_at="2026-11-27T10:04:00Z",
                duration_seconds=15.0,
                answers=[_single_choice_answer(question_id="custom_q", option_id="opt1")],
            ),
        )

        # Delete m_del (Option A preserves study_records)
        del_resp = await client.post(
            "/delete-online-match",
            json={"matchId": m_del},
            headers={"X-Delete-Password": "delete-pass-123"},
        )
        assert del_resp.status_code == 200

        # Invalid scope returns HTTP 400 INVALID_SURVEY_SCOPE
        bad_analytics = await client.get("/survey-analytics-data?scope=invalid_scope")
        assert bad_analytics.status_code == 400
        assert bad_analytics.json()["code"] == "INVALID_SURVEY_SCOPE"
        bad_csv = await client.get("/export/surveys.csv?scope=invalid_scope")
        assert bad_csv.status_code == 400
        assert bad_csv.json()["code"] == "INVALID_SURVEY_SCOPE"

        # Default active_matches scope: excludes standalone & deleted; dedupes pre-match
        active_resp = await client.get("/survey-analytics-data")
        assert active_resp.status_code == 200
        active = active_resp.json()
        assert active["scope"] == "active_matches"
        assert active["scopeCounts"] == {
            "active_matches": 2,
            "formal": 3,
            "standalone": 1,
            "all": 4,
        }
        assert [s["surveyId"] for s in active["availableSurveys"]] == [
            "online_pre_match_survey",
            "online_post_match_survey",
        ]
        assert active["availableVersions"] == ["v1.0-wjx-migrated"]
        assert active["totalResponses"] == 2

        pre_group = active["surveys"][0]
        assert pre_group["surveyId"] == "online_pre_match_survey"
        assert pre_group["kpi"]["responseCount"] == 1
        assert pre_group["kpi"]["linkedMatchCount"] == 2
        assert pre_group["kpi"]["avgDurationSeconds"] == 25.0
        assert pre_group["kpi"]["medianDurationSeconds"] == 25.0

        # Pre-match survey counted ONLY ONCE in distributions
        q_gender = pre_group["questions"][0]
        assert q_gender["answeredCount"] == 1
        assert q_gender["options"][0]["optionId"] == "woman"
        assert q_gender["options"][0]["count"] == 1
        assert q_gender["options"][0]["percentage"] == 100.0

        # LOCKED REQUIREMENT #1: Pre-match fallback metadata filled from active match (m2 is newest)
        pre_resp = pre_group["responses"][0]
        assert pre_resp["matchId"] == m2
        assert pre_resp["roomCode"] == rc2
        assert pre_resp["playerSlot"] == 1
        assert pre_resp["linkedActiveMatchId"] == m2
        assert "playerToken" not in json.dumps(pre_resp)
        assert "secret-tok-123" not in json.dumps(active)

        q_pre_note = pre_group["questions"][1]
        assert q_pre_note["nonEmptyCount"] == 1
        assert q_pre_note["entries"][0]["matchId"] == m2
        assert q_pre_note["entries"][0]["roomCode"] == rc2
        assert q_pre_note["entries"][0]["playerSlot"] == 1
        assert q_pre_note["entries"][0]["linkedActiveMatchId"] == m2

        # Formal scope includes orphan post_deleted with linkedActiveMatchId=None
        formal_resp = await client.get("/survey-analytics-data?scope=formal")
        assert formal_resp.status_code == 200
        formal = formal_resp.json()
        assert formal["availableVersions"] == ["v1.0-wjx-migrated", "v2.0-pilot"]
        post_group = next(
            s for s in formal["surveys"] if s["surveyId"] == "online_post_match_survey"
        )
        assert post_group["kpi"]["responseCount"] == 2
        # Newest first: post_deleted (10:03) before post_m1_p2 (10:02)
        assert [r["responseId"] for r in post_group["responses"]] == [
            "resp-post-deleted",
            "resp-post-m1-p2",
        ]
        deleted_row = post_group["responses"][0]
        assert deleted_row["matchId"] == m_del
        assert deleted_row["roomCode"] == rc_del
        assert deleted_row["playerSlot"] == 1
        assert deleted_row["linkedActiveMatchId"] is None

        # Filtering by survey_id + survey_version
        ver_filtered = (
            await client.get(
                "/survey-analytics-data?scope=formal&survey_id=online_post_match_survey&survey_version=v2.0-pilot"
            )
        ).json()
        assert ver_filtered["totalResponses"] == 1
        assert ver_filtered["availableVersions"] == ["v1.0-wjx-migrated", "v2.0-pilot"]
        assert ver_filtered["surveys"][0]["responses"][0]["responseId"] == "resp-post-deleted"

        # Standalone & all scopes + survey ordering (pre, post, alphabetical)
        all_data = (await client.get("/survey-analytics-data?scope=all")).json()
        assert [s["surveyId"] for s in all_data["availableSurveys"]] == [
            "online_pre_match_survey",
            "online_post_match_survey",
            "alpha_custom_survey",
        ]
        standalone_data = (await client.get("/survey-analytics-data?scope=standalone")).json()
        assert standalone_data["totalResponses"] == 1
        assert standalone_data["surveys"][0]["surveyId"] == "alpha_custom_survey"
        assert standalone_data["surveys"][0]["kpi"]["linkedMatchCount"] == 0


def test_likert_sample_sd_none_conditional_skip_legacy_rows_and_textarea_order(
    tmp_path: Path,
) -> None:
    clock = FakeClock(utc_time=datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC))
    db = SQLiteDatabase(tmp_path / "analytics_unit.sqlite3")
    db.initialize()
    study_repo = StudyRepository(db)

    # Response 1: single likert score for q_single_n (n=1 -> sd MUST be None), plus q_pair=2
    study_repo.insert_record(
        "survey",
        _make_rich_survey_payload(
            survey_id="zeta_survey",
            response_id="r1",
            submitted_at="2026-11-27T10:00:00Z",
            duration_seconds=10.0,
            answers=[
                _likert_answer(
                    section_id="sec_likert",
                    question_index=1,
                    question_id="q_single_n",
                    score=4,
                ),
                _likert_answer(
                    section_id="sec_likert",
                    question_index=2,
                    question_id="q_pair",
                    score=2,
                    option_zh="不同意",
                    option_en="Disagree",
                ),
                _single_choice_answer(
                    section_id="sec_cond",
                    question_index=3,
                    question_id="q_cond_follow",
                    option_id="branch_a",
                    option_index=1,
                    option_zh="分支A",
                    option_en="Branch A",
                ),
                _textarea_answer(
                    section_id="sec_cond",
                    question_index=4,
                    question_id="q_notes",
                    text_value="Earlier note",
                ),
            ],
        ),
        created_at="2026-11-27T10:00:00Z",
    )

    # Response 2: skips q_single_n and q_notes, answers q_pair=4 and q_cond_follow=branch_b
    study_repo.insert_record(
        "survey",
        _make_rich_survey_payload(
            survey_id="zeta_survey",
            response_id="r2",
            submitted_at="2026-11-27T10:05:00Z",
            duration_seconds=20.0,
            answers=[
                _likert_answer(
                    section_id="sec_likert",
                    question_index=2,
                    question_id="q_pair",
                    score=4,
                    option_zh="同意",
                    option_en="Agree",
                ),
                _single_choice_answer(
                    section_id="sec_cond",
                    question_index=3,
                    question_id="q_cond_follow",
                    option_id="branch_b",
                    option_index=2,
                    option_zh="分支B",
                    option_en="Branch B",
                ),
                _textarea_answer(
                    section_id="sec_cond",
                    question_index=4,
                    question_id="q_notes",
                    text_value="   ",  # whitespace only -> does not count as answered
                ),
            ],
        ),
        created_at="2026-11-27T10:05:00Z",
    )

    # Response 3: legacy row missing answers completely
    study_repo.insert_record(
        "survey",
        {"eventType": "survey-response", "surveyId": "zeta_survey", "responseId": "r3", "q1": 5},
        created_at="2026-11-27T10:10:00Z",
    )

    # Response 4: another survey with exactly 1 likert score to test section likertSummary sd=None
    study_repo.insert_record(
        "survey",
        _make_rich_survey_payload(
            survey_id="beta_single_score",
            response_id="r4",
            submitted_at="2026-11-27T10:15:00Z",
            answers=[
                _likert_answer(
                    section_id="sec_one",
                    question_index=1,
                    question_id="q_only",
                    score=6,  # also verifies extra bucket beyond 1..5
                    option_zh="超量表",
                    option_en="Extra",
                )
            ],
        ),
        created_at="2026-11-27T10:15:00Z",
    )

    # Duplicate responseId row in SQLite (higher id) -> must be deduped
    study_repo.insert_record(
        "survey",
        _make_rich_survey_payload(
            survey_id="beta_single_score",
            response_id="r4",
            submitted_at="2026-11-27T10:16:00Z",
            answers=[
                _likert_answer(
                    section_id="sec_one",
                    question_index=1,
                    question_id="q_only",
                    score=1,
                )
            ],
        ),
        created_at="2026-11-27T10:16:00Z",
    )

    records = study_repo.query_records(record_type="survey")
    analytics = build_survey_analytics(
        matches=[],
        survey_records=records,
        generated_at=clock.utc_now(),
        scope="formal",
    )
    assert [s["surveyId"] for s in analytics["surveys"]] == ["beta_single_score", "zeta_survey"]

    beta = analytics["surveys"][0]
    assert beta["kpi"]["responseCount"] == 1  # r4 duplicate ignored
    q_only = beta["questions"][0]
    assert [b["score"] for b in q_only["buckets"]] == [1, 2, 3, 4, 5, 6]
    assert q_only["stats"] == {"mean": 6, "sd": None, "median": 6, "min": 6, "max": 6}
    assert beta["sections"][0]["likertSummary"] == {
        "questionCount": 1,
        "scoreCount": 1,
        "mean": 6,
        "sd": None,
        "median": 6,
        "rawScoresNote": "Raw scores (unreversed)",
    }

    zeta = analytics["surveys"][1]
    assert zeta["kpi"]["responseCount"] == 3
    q_map = {q["questionId"]: q for q in zeta["questions"]}

    # LOCKED REQUIREMENT #3: n == 1 -> sd is None, NEVER 0.0
    assert q_map["q_single_n"]["answeredCount"] == 1
    assert q_map["q_single_n"]["skippedCount"] == 2
    assert q_map["q_single_n"]["stats"]["sd"] is None
    assert q_map["q_single_n"]["stats"]["mean"] == 4

    # n == 2 ([2, 4]) -> sample SD sqrt(((2-3)^2 + (4-3)^2) / 1) = sqrt(2) ~= 1.414
    assert q_map["q_pair"]["answeredCount"] == 2
    assert q_map["q_pair"]["skippedCount"] == 1
    assert q_map["q_pair"]["stats"]["mean"] == 3
    assert q_map["q_pair"]["stats"]["sd"] == round(math.sqrt(2), 3)

    # Conditional jump: denominator is answeredCount (2), NOT responseCount (3)
    assert q_map["q_cond_follow"]["answeredCount"] == 2
    assert q_map["q_cond_follow"]["skippedCount"] == 1
    assert [opt["percentage"] for opt in q_map["q_cond_follow"]["options"]] == [50.0, 50.0]

    # Textarea non-empty only
    assert q_map["q_notes"]["nonEmptyCount"] == 1
    assert [e["text"] for e in q_map["q_notes"]["entries"]] == ["Earlier note"]


@pytest.mark.asyncio
async def test_surveys_csv_export_wide_table_numeric_cells_formula_defense_and_empty(
    app_bundle_factory,
) -> None:
    bundle = app_bundle_factory()
    async with bundle.client() as client:
        # Empty export -> UTF-8 BOM + metadata header row + CRLF
        empty_resp = await client.get("/export/surveys.csv?scope=standalone")
        assert empty_resp.status_code == 200
        assert empty_resp.headers["content-type"].startswith("text/csv")
        assert "charset=utf-8" in empty_resp.headers["content-type"].lower()
        assert 'filename="surveys-standalone.csv"' in empty_resp.headers["content-disposition"]
        assert empty_resp.content.startswith(b"\xef\xbb\xbf")
        assert "\r\n" in empty_resp.text
        empty_rows = list(csv.reader(io.StringIO(empty_resp.text.lstrip("\ufeff"))))
        assert empty_rows == [
            [
                "record_id",
                "response_id",
                "survey_id",
                "survey_version",
                "entry_source",
                "match_id",
                "room_code",
                "player_slot",
                "study_session_id",
                "participant_id",
                "experiment_condition",
                "language",
                "submitted_at",
                "duration_seconds",
            ]
        ]

        # Seed active match + pre-match survey (no matchId/roomCode) + post-match survey
        m1, rc1 = await _create_active_match(
            client, p1_sid="csv-p1", p2_sid="csv-p2", idem_prefix="csv-m1"
        )
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_pre_match_survey",
                response_id="csv-pre-1",
                study_session_id="csv-p1",
                participant_id="=SUM(1,1)",  # formula injection test in metadata cell
                experiment_condition="-42",  # pure number string MUST NOT be escaped
                submitted_at="2026-11-27T11:00:00Z",
                duration_seconds=33.5,
                answers=[_single_choice_answer()],
            ),
        )
        await client.post(
            "/record-survey-response",
            json=_make_rich_survey_payload(
                survey_id="online_post_match_survey",
                response_id="csv-post-1",
                study_session_id="csv-p2",
                match_id=m1,
                room_code=rc1,
                player_number=2,
                submitted_at="2026-11-27T11:05:00Z",
                duration_seconds=120,
                answers=[
                    _likert_answer(score=5, option_zh="非常同意", option_en="Strongly agree"),
                    _textarea_answer(question_id="evil_ws", text_value="  +cmd|' /C calc'!A0"),
                    _textarea_answer(
                        question_id="evil_tab",
                        question_index=23,
                        text_value="\t=HYPERLINK(\"http://x\")",
                    ),
                    _textarea_answer(
                        question_id="evil_at",
                        question_index=24,
                        text_value="@SUM(1,2)",
                    ),
                    _textarea_answer(
                        question_id="evil_dash",
                        question_index=25,
                        text_value="- bullet item",
                    ),
                ],
            ),
        )

        # Multi-survey merged CSV export without survey_id filter
        csv_resp = await client.get("/export/surveys.csv?scope=active_matches")
        assert csv_resp.status_code == 200
        assert csv_resp.content.startswith(b"\xef\xbb\xbf")
        assert "\r\n" in csv_resp.text
        parsed = list(csv.DictReader(io.StringIO(csv_resp.text.lstrip("\ufeff"), newline="")))
        assert len(parsed) == 2

        headers = list(parsed[0].keys())
        assert "q_gender__option_id" in headers
        assert "q_gender__label" in headers
        assert "q_rel_comfort__score" in headers
        assert "q_rel_comfort__label" in headers
        assert "q_evil_ws__text" in headers
        assert "q_evil_tab__text" in headers
        assert "q_evil_at__text" in headers
        assert "q_evil_dash__text" in headers

        by_resp = {r["response_id"]: r for r in parsed}
        pre_row = by_resp["csv-pre-1"]
        post_row = by_resp["csv-post-1"]

        # LOCKED REQUIREMENT #1 in CSV: pre-match fallback metadata populated
        assert pre_row["match_id"] == m1
        assert pre_row["room_code"] == rc1
        assert pre_row["player_slot"] == "1"

        # LOCKED REQUIREMENT #2: numeric columns remain clean unescaped numbers
        assert pre_row["record_id"].isdigit()
        assert pre_row["player_slot"] == "1"
        assert pre_row["duration_seconds"] == "33.5"
        assert post_row["player_slot"] == "2"
        assert post_row["duration_seconds"] == "120"
        assert post_row["q_rel_comfort__score"] == "5"
        assert pre_row["q_rel_comfort__score"] == ""

        # LOCKED REQUIREMENT #2: formula injection defense on text cells (excl. pure numbers)
        assert pre_row["participant_id"] == "'=SUM(1,1)"
        assert pre_row["experiment_condition"] == "-42"
        assert post_row["q_evil_ws__text"] == "'  +cmd|' /C calc'!A0"
        assert post_row["q_evil_tab__text"] == "'\t=HYPERLINK(\"http://x\")"
        assert post_row["q_evil_at__text"] == "'@SUM(1,2)"
        assert post_row["q_evil_dash__text"] == "'- bullet item"


def test_build_surveys_csv_direct_negative_numeric_and_crlf() -> None:
    from server.persistence.models import StudyRecord

    row = StudyRecord(
        id=7,
        record_type="survey",
        match_id=None,
        participant_id="-99",
        round_id=None,
        payload={
            "surveyId": "custom_num",
            "surveyVersion": "v1",
            "responseId": "r-num",
            "playerNumber": 1,
            "durationSeconds": 12.5,
            "answers": [
                _likert_answer(question_id="neg", score=-3, option_zh="负分", option_en="Neg"),
                _textarea_answer(question_id="cr", text_value="\r=1+1"),
            ],
        },
        created_at="2026-11-27T12:00:00Z",
    )
    csv_text, filename = build_surveys_csv(
        matches=[],
        survey_records=[row],
        scope="all",
        survey_id="custom_num",
    )
    assert filename == "surveys-all-custom_num.csv"
    assert csv_text.startswith("\ufeff")
    assert "\r\n" in csv_text
    rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline="")))
    assert len(rows) == 1
    assert rows[0]["record_id"] == "7"
    assert rows[0]["player_slot"] == "1"
    assert rows[0]["duration_seconds"] == "12.5"
    assert rows[0]["q_neg__score"] == "-3"
    assert rows[0]["participant_id"] == "-99"
    assert rows[0]["q_cr__text"] == "'\r=1+1"
