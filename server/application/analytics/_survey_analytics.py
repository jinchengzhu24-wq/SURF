"""Pure data-driven survey analytics projection and wide-table CSV builder."""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from server.domain.errors import ValidationError
from server.persistence.models import StudyRecord

from ._cocreation_projection import redact_secret_keys
from ._projection import clean_id

VALID_SURVEY_SCOPES: tuple[str, ...] = ("active_matches", "formal", "standalone", "all")

_SURVEY_PRIORITY: dict[str, int] = {
    "online_pre_match_survey": 0,
    "online_post_match_survey": 1,
}

_FORMULA_INJECTION_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")

_CSV_METADATA_COLUMNS: tuple[str, ...] = (
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
)


def _clean_survey_id(value: Any) -> str:
    text = clean_id(value)
    return "" if text == "-" else text


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_player_slot(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw in (1, 2) else None
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped in {"1", "2"}:
            return int(stripped)
    return None


def _parse_non_negative_float(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    val: float
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            val = float(stripped)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(val) or val < 0:
        return None
    return round(val, 3)


def _parse_score(raw: Any) -> int | float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        return int(raw) if raw.is_integer() else raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            val = float(stripped)
        except ValueError:
            return None
        if not math.isfinite(val):
            return None
        return int(val) if val.is_integer() else val
    return None


def _parse_optional_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if math.isfinite(raw) and raw.is_integer() else None
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.lstrip("-").isdigit() and stripped not in {"", "-"}:
            return int(stripped)
    return None


def _survey_sort_key(survey_id: str) -> tuple[int, str]:
    return (_SURVEY_PRIORITY.get(survey_id, 2), survey_id)


def _index_formal_surveys(
    survey_records: list[StudyRecord],
) -> tuple[
    dict[str, list[tuple[StudyRecord, tuple[str, ...]]]],
    dict[str, list[tuple[StudyRecord, tuple[str, ...]]]],
]:
    """Index non-standalone survey rows by matchId and fallback session IDs."""
    by_match_id: dict[str, list[tuple[StudyRecord, tuple[str, ...]]]] = {}
    by_session_id: dict[str, list[tuple[StudyRecord, tuple[str, ...]]]] = {}

    for rec in survey_records:
        payload = rec.payload if isinstance(rec.payload, Mapping) else {}
        if str(payload.get("entrySource") or "").strip().casefold() == "standalone":
            continue

        rec_match_ids = {
            mid
            for mid in (
                _clean_survey_id(payload.get("matchId")),
                _clean_survey_id(rec.match_id),
            )
            if mid
        }
        candidate_sids = tuple(
            dict.fromkeys(
                sid
                for sid in (
                    _clean_survey_id(payload.get("studySessionId")),
                    _clean_survey_id(payload.get("sessionId")),
                    _clean_survey_id(rec.participant_id),
                )
                if sid
            )
        )
        if rec_match_ids:
            for rmid in rec_match_ids:
                by_match_id.setdefault(rmid, []).append((rec, candidate_sids))
        else:
            for sid in candidate_sids:
                by_session_id.setdefault(sid, []).append((rec, candidate_sids))

    return by_match_id, by_session_id


def _match_surveys_for_match(
    match_record: Mapping[str, Any],
    *,
    by_match_id: Mapping[str, list[tuple[StudyRecord, tuple[str, ...]]]],
    by_session_id: Mapping[str, list[tuple[StudyRecord, tuple[str, ...]]]],
) -> list[dict[str, Any]]:
    """Associate formal surveys for one match, deduped, ordered by id ASC, and redacted."""
    mid = _clean_survey_id(match_record.get("matchId"))
    p1_sid = ""
    p2_sid = ""
    for player in match_record.get("players") or []:
        if not isinstance(player, Mapping):
            continue
        pnum = player.get("playerNumber")
        psid = _clean_survey_id(player.get("studySessionId"))
        if pnum == 1 and psid:
            p1_sid = psid
        elif pnum == 2 and psid:
            p2_sid = psid

    candidates: list[tuple[StudyRecord, tuple[str, ...], str]] = []
    if mid:
        for rec, c_sids in by_match_id.get(mid, ()):
            candidates.append((rec, c_sids, "matchId"))
    for sid in dict.fromkeys(s for s in (p1_sid, p2_sid) if s):
        for rec, c_sids in by_session_id.get(sid, ()):
            candidates.append((rec, c_sids, "studySessionId"))

    candidates.sort(key=lambda item: (item[0].id, 0 if item[2] == "matchId" else 1))

    matched: list[dict[str, Any]] = []
    seen_record_ids: set[int] = set()
    seen_response_ids: set[str] = set()

    for rec, c_sids, matched_by in candidates:
        if rec.id in seen_record_ids:
            continue
        payload = rec.payload if isinstance(rec.payload, Mapping) else {}
        resp_id = _clean_survey_id(payload.get("responseId"))
        if resp_id and resp_id in seen_response_ids:
            continue

        seen_record_ids.add(rec.id)
        if resp_id:
            seen_response_ids.add(resp_id)

        player_slot = _parse_player_slot(payload.get("playerNumber"))
        if player_slot is None:
            for sid in c_sids:
                if p1_sid and sid == p1_sid:
                    player_slot = 1
                    break
                if p2_sid and sid == p2_sid:
                    player_slot = 2
                    break

        matched.append(
            redact_secret_keys(
                {
                    "recordId": rec.id,
                    "surveyId": _clean_survey_id(payload.get("surveyId")) or None,
                    "surveyVersion": _clean_survey_id(payload.get("surveyVersion")) or None,
                    "entrySource": _clean_survey_id(payload.get("entrySource")) or None,
                    "playerSlot": player_slot,
                    "matchedBy": matched_by,
                    "createdAt": rec.created_at,
                    "payload": payload,
                }
            )
        )

    return matched


def normalize_survey_scope(scope: str | None) -> str:
    """Validate and return normalized survey analytics scope."""
    normalized = "active_matches" if scope is None else str(scope).strip()
    if normalized not in VALID_SURVEY_SCOPES:
        raise ValidationError(
            "INVALID_SURVEY_SCOPE",
            "error.validation_failed",
            message=(
                f"Invalid survey scope {scope!r}. "
                f"Expected one of: {', '.join(VALID_SURVEY_SCOPES)}."
            ),
        )
    return normalized


def build_active_survey_links(
    matches: Sequence[Mapping[str, Any]],
    survey_records: Sequence[StudyRecord],
) -> dict[int, list[dict[str, Any]]]:
    """Link formal non-standalone survey records to active matches."""
    by_match_id, by_session_id = _index_formal_surveys(list(survey_records))
    links_by_record_id: dict[int, list[dict[str, Any]]] = {}
    for match_record in matches:
        mid = _clean_survey_id(match_record.get("matchId"))
        rc = _clean_survey_id(match_record.get("roomCode"))
        matched = _match_surveys_for_match(
            match_record,
            by_match_id=by_match_id,
            by_session_id=by_session_id,
        )
        for item in matched:
            rec_id = int(item["recordId"])
            links_by_record_id.setdefault(rec_id, []).append(
                {
                    "matchId": mid or None,
                    "roomCode": rc or None,
                    "playerSlot": item.get("playerSlot"),
                    "matchedBy": item.get("matchedBy"),
                }
            )
    return links_by_record_id


def _is_standalone(rec: StudyRecord) -> bool:
    payload = rec.payload if isinstance(rec.payload, Mapping) else {}
    return str(payload.get("entrySource") or "").strip().casefold() == "standalone"


def _record_in_scope(
    rec: StudyRecord,
    scope: str,
    active_links_by_record_id: Mapping[int, list[dict[str, Any]]],
) -> bool:
    if scope == "active_matches":
        return rec.id in active_links_by_record_id
    if scope == "formal":
        return not _is_standalone(rec)
    if scope == "standalone":
        return _is_standalone(rec)
    return True


def _dedupe_survey_records(records: Sequence[StudyRecord]) -> list[StudyRecord]:
    """Deduplicate survey records by record id and non-empty responseId (earliest id wins)."""
    ordered = sorted(records, key=lambda r: r.id)
    deduped: list[StudyRecord] = []
    seen_record_ids: set[int] = set()
    seen_response_ids: set[str] = set()
    for rec in ordered:
        if rec.id in seen_record_ids:
            continue
        payload = rec.payload if isinstance(rec.payload, Mapping) else {}
        resp_id = _clean_survey_id(payload.get("responseId"))
        if resp_id and resp_id in seen_response_ids:
            continue
        seen_record_ids.add(rec.id)
        if resp_id:
            seen_response_ids.add(resp_id)
        deduped.append(rec)
    return deduped


def _record_survey_id(rec: StudyRecord) -> str:
    payload = rec.payload if isinstance(rec.payload, Mapping) else {}
    return _clean_survey_id(payload.get("surveyId")) or "unknown"


def _record_survey_version(rec: StudyRecord) -> str | None:
    payload = rec.payload if isinstance(rec.payload, Mapping) else {}
    return _clean_survey_id(payload.get("surveyVersion")) or None


def _normalize_response_record(
    rec: StudyRecord,
    active_links_by_record_id: Mapping[int, list[dict[str, Any]]],
    *,
    include_raw_csv_fields: bool = False,
) -> dict[str, Any]:
    """Normalize one StudyRecord with active-match metadata fallback and secret redaction."""
    raw_payload = rec.payload if isinstance(rec.payload, Mapping) else {}
    payload = redact_secret_keys(dict(raw_payload))

    raw_match_id = (
        _clean_survey_id(payload.get("matchId"))
        or _clean_survey_id(rec.match_id)
        or None
    )
    raw_room_code = _clean_survey_id(payload.get("roomCode")) or None
    raw_player_slot = _parse_player_slot(payload.get("playerNumber"))

    active_links = active_links_by_record_id.get(rec.id, ())
    matched_link: dict[str, Any] | None = None
    if active_links:
        if raw_match_id:
            matched_link = next(
                (lnk for lnk in active_links if lnk.get("matchId") == raw_match_id),
                None,
            )
        if matched_link is None and not raw_match_id:
            matched_link = active_links[0]

    effective_match_id = raw_match_id or (
        matched_link.get("matchId") if matched_link is not None else None
    )
    effective_room_code = raw_room_code or (
        matched_link.get("roomCode") if matched_link is not None else None
    )
    effective_player_slot = (
        raw_player_slot
        if raw_player_slot is not None
        else (matched_link.get("playerSlot") if matched_link is not None else None)
    )
    linked_active_match_id = (
        matched_link.get("matchId") if matched_link is not None else None
    )

    raw_answers = payload.get("answers")
    answers = (
        [dict(ans) for ans in raw_answers if isinstance(ans, Mapping)]
        if isinstance(raw_answers, list)
        else []
    )
    submitted_at = _clean_text(payload.get("submittedAt")) or rec.created_at or None

    normalized: dict[str, Any] = {
        "recordId": rec.id,
        "responseId": _clean_survey_id(payload.get("responseId")) or None,
        "surveyId": _record_survey_id(rec),
        "surveyVersion": _record_survey_version(rec),
        "surveyTitle": _clean_text(payload.get("surveyTitle")) or None,
        "entrySource": _clean_text(payload.get("entrySource")) or None,
        "matchId": effective_match_id,
        "roomCode": effective_room_code,
        "playerSlot": effective_player_slot,
        "linkedActiveMatchId": linked_active_match_id,
        "studySessionId": (
            _clean_survey_id(payload.get("studySessionId"))
            or _clean_survey_id(payload.get("sessionId"))
            or None
        ),
        "participantId": (
            _clean_survey_id(payload.get("participantId"))
            or _clean_survey_id(rec.participant_id)
            or None
        ),
        "experimentCondition": (
            _clean_text(payload.get("experimentCondition"))
            or _clean_text(payload.get("condition"))
            or None
        ),
        "language": _clean_text(payload.get("language")) or None,
        "submittedAt": submitted_at,
        "createdAt": rec.created_at,
        "durationSeconds": _parse_non_negative_float(payload.get("durationSeconds")),
        "answerCount": len(answers),
        "answers": answers,
    }
    if include_raw_csv_fields:
        normalized["rawParticipantId"] = (
            _raw_non_blank_str(payload.get("participantId"))
            or _raw_non_blank_str(rec.participant_id)
        )
        normalized["rawExperimentCondition"] = (
            _raw_non_blank_str(payload.get("experimentCondition"))
            or _raw_non_blank_str(payload.get("condition"))
        )
    return normalized


def _extract_choice_label(ans: Mapping[str, Any]) -> str:
    zh = _clean_text(ans.get("optionTextZh"))
    en = _clean_text(ans.get("optionTextEn"))
    if zh and en:
        return f"{zh} / {en}" if zh != en else zh
    if zh:
        return zh
    if en:
        return en
    return _clean_text(ans.get("optionId"))


def _is_answered_item(q_type: str, ans: Mapping[str, Any]) -> bool:
    if q_type == "textarea":
        return bool(_clean_text(ans.get("textValue")))
    if q_type == "likert_5":
        return _parse_score(ans.get("score")) is not None
    return bool(
        _clean_text(ans.get("optionId"))
        or _clean_text(ans.get("optionTextZh"))
        or _clean_text(ans.get("optionTextEn"))
        or _clean_text(ans.get("textValue"))
        or _parse_score(ans.get("score")) is not None
    )


def _aggregate_survey_group(
    survey_id: str,
    group_records: Sequence[StudyRecord],
    responses_newest_first: Sequence[Mapping[str, Any]],
    active_links_by_record_id: Mapping[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compute KPI, sections, questions, and responses for one surveyId."""
    response_count = len(responses_newest_first)
    linked_match_ids = {
        str(link["matchId"])
        for rec in group_records
        for link in active_links_by_record_id.get(rec.id, ())
        if link.get("matchId")
    }
    durations = [
        float(resp["durationSeconds"])
        for resp in responses_newest_first
        if resp.get("durationSeconds") is not None
    ]
    versions = sorted(
        {
            str(resp["surveyVersion"])
            for resp in responses_newest_first
            if resp.get("surveyVersion")
        }
    )
    survey_title = next(
        (str(resp["surveyTitle"]) for resp in responses_newest_first if resp.get("surveyTitle")),
        None,
    )
    latest_submitted_at = max(
        (
            str(resp.get("submittedAt") or resp.get("createdAt") or "")
            for resp in responses_newest_first
        ),
        default="",
    ) or None

    kpi = {
        "responseCount": response_count,
        "linkedMatchCount": len(linked_match_ids),
        "avgDurationSeconds": round(sum(durations) / len(durations), 2) if durations else None,
        "medianDurationSeconds": (
            round(float(statistics.median(durations)), 2) if durations else None
        ),
        "latestSubmittedAt": latest_submitted_at,
        "versions": versions,
    }

    # Preserve oldest->newest pass for deterministic metadata discovery while reading entries
    # from responses_newest_first when collecting textarea comments.
    question_meta: dict[str, dict[str, Any]] = {}
    section_order: dict[str, tuple[int, int]] = {}
    observed_answers: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    seen_per_response: set[tuple[int, str]] = set()
    q_seen_seq = 0

    for resp in responses_newest_first:
        rec_id = int(resp["recordId"])
        for ans in resp.get("answers") or ():
            q_id = _clean_text(ans.get("questionId"))
            q_idx = _parse_optional_int(ans.get("questionIndex"))
            if not q_id:
                if q_idx is None:
                    continue
                q_id = f"q_{q_idx}"

            if (rec_id, q_id) in seen_per_response:
                continue
            seen_per_response.add((rec_id, q_id))

            sec_id = _clean_text(ans.get("sectionId")) or "default"
            q_type = _clean_text(ans.get("questionType")) or (
                "likert_5"
                if _parse_score(ans.get("score")) is not None
                else "textarea"
                if _clean_text(ans.get("textValue"))
                else "single_choice"
            )

            if q_id not in question_meta:
                q_seen_seq += 1
                question_meta[q_id] = {
                    "sectionId": sec_id,
                    "questionIndex": q_idx,
                    "questionId": q_id,
                    "questionType": q_type,
                    "questionTextZh": _clean_text(ans.get("questionTextZh")) or None,
                    "questionTextEn": _clean_text(ans.get("questionTextEn")) or None,
                    "_sortIndex": q_idx if q_idx is not None else 10**9,
                    "_seenSeq": q_seen_seq,
                }
            else:
                meta = question_meta[q_id]
                if meta["questionIndex"] is None and q_idx is not None:
                    meta["questionIndex"] = q_idx
                    meta["_sortIndex"] = q_idx
                if not meta["questionTextZh"] and _clean_text(ans.get("questionTextZh")):
                    meta["questionTextZh"] = _clean_text(ans.get("questionTextZh"))
                if not meta["questionTextEn"] and _clean_text(ans.get("questionTextEn")):
                    meta["questionTextEn"] = _clean_text(ans.get("questionTextEn"))

            sort_idx = q_idx if q_idx is not None else 10**9
            if sec_id not in section_order:
                section_order[sec_id] = (sort_idx, len(section_order))
            else:
                prev_idx, prev_seq = section_order[sec_id]
                if sort_idx < prev_idx:
                    section_order[sec_id] = (sort_idx, prev_seq)

            if _is_answered_item(q_type, ans):
                observed_answers.setdefault(q_id, []).append((resp, ans))

    ordered_q_metas = sorted(
        question_meta.values(),
        key=lambda m: (m["_sortIndex"], m["_seenSeq"], m["questionId"]),
    )

    questions_out: list[dict[str, Any]] = []
    questions_by_section: dict[str, list[dict[str, Any]]] = {}
    section_scores: dict[str, list[float]] = {}
    section_likert_q_count: dict[str, int] = {}

    for meta in ordered_q_metas:
        q_id = meta["questionId"]
        sec_id = meta["sectionId"]
        q_type = meta["questionType"]
        ans_pairs = observed_answers.get(q_id, [])
        answered_count = len(ans_pairs)
        skipped_count = max(0, response_count - answered_count)

        q_out: dict[str, Any] = {
            "sectionId": sec_id,
            "questionIndex": meta["questionIndex"],
            "questionId": q_id,
            "questionType": q_type,
            "questionTextZh": meta["questionTextZh"],
            "questionTextEn": meta["questionTextEn"],
            "answeredCount": answered_count,
            "skippedCount": skipped_count,
            "totalResponses": response_count,
            "responseRate": (
                round((answered_count / response_count) * 100, 1) if response_count > 0 else 0.0
            ),
        }

        if q_type == "single_choice":
            option_stats: dict[str, dict[str, Any]] = {}
            opt_seq = 0
            for _, ans in ans_pairs:
                opt_id = _clean_text(ans.get("optionId")) or _extract_choice_label(ans) or "(empty)"
                opt_idx = _parse_optional_int(ans.get("optionIndex"))
                opt_zh = _clean_text(ans.get("optionTextZh")) or None
                opt_en = _clean_text(ans.get("optionTextEn")) or None
                if opt_id not in option_stats:
                    opt_seq += 1
                    option_stats[opt_id] = {
                        "optionId": opt_id,
                        "optionIndex": opt_idx,
                        "optionTextZh": opt_zh,
                        "optionTextEn": opt_en,
                        "count": 0,
                        "_sortIdx": opt_idx if opt_idx is not None else 10**9,
                        "_seq": opt_seq,
                    }
                entry = option_stats[opt_id]
                entry["count"] += 1
                if entry["optionIndex"] is None and opt_idx is not None:
                    entry["optionIndex"] = opt_idx
                    entry["_sortIdx"] = opt_idx
                if not entry["optionTextZh"] and opt_zh:
                    entry["optionTextZh"] = opt_zh
                if not entry["optionTextEn"] and opt_en:
                    entry["optionTextEn"] = opt_en

            ordered_options = sorted(
                option_stats.values(),
                key=lambda o: (o["_sortIdx"], o["_seq"], o["optionId"]),
            )
            q_out["options"] = [
                {
                    "optionId": o["optionId"],
                    "optionIndex": o["optionIndex"],
                    "optionTextZh": o["optionTextZh"],
                    "optionTextEn": o["optionTextEn"],
                    "count": o["count"],
                    "percentage": (
                        round((o["count"] / answered_count) * 100, 1)
                        if answered_count > 0
                        else 0.0
                    ),
                }
                for o in ordered_options
            ]

        elif q_type == "likert_5":
            bucket_counts: dict[int | float, dict[str, Any]] = {
                score: {
                    "score": score,
                    "optionId": str(score),
                    "optionTextZh": None,
                    "optionTextEn": None,
                    "count": 0,
                }
                for score in (1, 2, 3, 4, 5)
            }
            numeric_scores: list[float] = []
            for _, ans in ans_pairs:
                score_val = _parse_score(ans.get("score"))
                if score_val is None:
                    continue
                numeric_scores.append(float(score_val))
                if score_val not in bucket_counts:
                    bucket_counts[score_val] = {
                        "score": score_val,
                        "optionId": _clean_text(ans.get("optionId")) or str(score_val),
                        "optionTextZh": _clean_text(ans.get("optionTextZh")) or None,
                        "optionTextEn": _clean_text(ans.get("optionTextEn")) or None,
                        "count": 0,
                    }
                bucket = bucket_counts[score_val]
                bucket["count"] += 1
                if _clean_text(ans.get("optionId")):
                    bucket["optionId"] = _clean_text(ans.get("optionId"))
                if not bucket["optionTextZh"] and _clean_text(ans.get("optionTextZh")):
                    bucket["optionTextZh"] = _clean_text(ans.get("optionTextZh"))
                if not bucket["optionTextEn"] and _clean_text(ans.get("optionTextEn")):
                    bucket["optionTextEn"] = _clean_text(ans.get("optionTextEn"))

            ordered_buckets = [bucket_counts[k] for k in sorted(bucket_counts.keys())]
            q_out["buckets"] = [
                {
                    **b,
                    "percentage": (
                        round((b["count"] / answered_count) * 100, 1)
                        if answered_count > 0
                        else 0.0
                    ),
                }
                for b in ordered_buckets
            ]
            n_scores = len(numeric_scores)
            if n_scores == 0:
                q_out["stats"] = {
                    "mean": None,
                    "sd": None,
                    "median": None,
                    "min": None,
                    "max": None,
                }
            else:
                min_s = min(numeric_scores)
                max_s = max(numeric_scores)
                q_out["stats"] = {
                    "mean": round(statistics.mean(numeric_scores), 3),
                    "sd": round(statistics.stdev(numeric_scores), 3) if n_scores >= 2 else None,
                    "median": round(float(statistics.median(numeric_scores)), 3),
                    "min": int(min_s) if min_s.is_integer() else min_s,
                    "max": int(max_s) if max_s.is_integer() else max_s,
                }
                section_scores.setdefault(sec_id, []).extend(numeric_scores)
                section_likert_q_count[sec_id] = section_likert_q_count.get(sec_id, 0) + 1

        elif q_type == "textarea":
            # responses_newest_first is already ordered newest-first
            entries = [
                {
                    "recordId": resp["recordId"],
                    "text": _clean_text(ans.get("textValue")),
                    "matchId": resp["matchId"],
                    "roomCode": resp["roomCode"],
                    "playerSlot": resp["playerSlot"],
                    "linkedActiveMatchId": resp["linkedActiveMatchId"],
                    "studySessionId": resp["studySessionId"],
                    "entrySource": resp["entrySource"],
                    "submittedAt": resp["submittedAt"],
                }
                for resp, ans in ans_pairs
                if _clean_text(ans.get("textValue"))
            ]
            q_out["nonEmptyCount"] = len(entries)
            q_out["entries"] = entries

        questions_out.append(q_out)
        questions_by_section.setdefault(sec_id, []).append(q_out)

    sections_out: list[dict[str, Any]] = []
    for sec_id, _ in sorted(section_order.items(), key=lambda item: item[1]):
        sec_questions = questions_by_section.get(sec_id, [])
        scores = section_scores.get(sec_id, [])
        likert_summary: dict[str, Any] | None = None
        if scores:
            score_count = len(scores)
            likert_summary = {
                "questionCount": section_likert_q_count.get(sec_id, 0),
                "scoreCount": score_count,
                "mean": round(statistics.mean(scores), 3),
                "sd": round(statistics.stdev(scores), 3) if score_count >= 2 else None,
                "median": round(float(statistics.median(scores)), 3),
                "rawScoresNote": "Raw scores (unreversed)",
            }
        sections_out.append(
            {
                "sectionId": sec_id,
                "questionCount": len(sec_questions),
                "questionIds": [q["questionId"] for q in sec_questions],
                "questions": sec_questions,
                "likertSummary": likert_summary,
            }
        )

    return {
        "surveyId": survey_id,
        "surveyTitle": survey_title,
        "kpi": kpi,
        "sections": sections_out,
        "questions": questions_out,
        "responses": list(responses_newest_first),
    }


def _select_scoped_and_filtered_records(
    *,
    matches: Sequence[Mapping[str, Any]],
    survey_records: Sequence[StudyRecord],
    scope: str,
    survey_id: str | None,
    survey_version: str | None,
) -> tuple[
    str,
    str | None,
    str | None,
    dict[int, list[dict[str, Any]]],
    dict[str, int],
    list[StudyRecord],
    list[StudyRecord],
]:
    normalized_scope = normalize_survey_scope(scope)
    clean_sid = _clean_survey_id(survey_id) or None
    clean_ver = _clean_survey_id(survey_version) or None

    active_links_by_record_id = build_active_survey_links(matches, survey_records)

    scoped_by_name: dict[str, list[StudyRecord]] = {
        s: _dedupe_survey_records(
            [
                rec
                for rec in survey_records
                if _record_in_scope(rec, s, active_links_by_record_id)
            ]
        )
        for s in VALID_SURVEY_SCOPES
    }
    scope_counts = {s: len(recs) for s, recs in scoped_by_name.items()}
    scoped_records = scoped_by_name[normalized_scope]

    filtered_records = [
        rec
        for rec in scoped_records
        if (clean_sid is None or _record_survey_id(rec) == clean_sid)
        and (clean_ver is None or _record_survey_version(rec) == clean_ver)
    ]
    return (
        normalized_scope,
        clean_sid,
        clean_ver,
        active_links_by_record_id,
        scope_counts,
        scoped_records,
        filtered_records,
    )


def build_survey_analytics(
    *,
    matches: Sequence[Mapping[str, Any]],
    survey_records: Sequence[StudyRecord],
    generated_at: str,
    scope: str = "active_matches",
    survey_id: str | None = None,
    survey_version: str | None = None,
) -> dict[str, Any]:
    """Build complete data-driven survey analytics projection."""
    (
        normalized_scope,
        clean_sid,
        clean_ver,
        active_links_by_record_id,
        scope_counts,
        scoped_records,
        filtered_records,
    ) = _select_scoped_and_filtered_records(
        matches=matches,
        survey_records=survey_records,
        scope=scope,
        survey_id=survey_id,
        survey_version=survey_version,
    )

    # Compute availableSurveys across current scope (before survey_id / survey_version filters)
    avail_by_sid: dict[str, dict[str, Any]] = {}
    for rec in scoped_records:
        sid = _record_survey_id(rec)
        payload = rec.payload if isinstance(rec.payload, Mapping) else {}
        ver = _record_survey_version(rec)
        title = _clean_text(payload.get("surveyTitle")) or None
        bucket = avail_by_sid.setdefault(
            sid,
            {
                "surveyId": sid,
                "surveyTitle": title,
                "responseCount": 0,
                "_versions": set(),
            },
        )
        bucket["responseCount"] += 1
        if not bucket["surveyTitle"] and title:
            bucket["surveyTitle"] = title
        if ver:
            bucket["_versions"].add(ver)

    available_surveys = [
        {
            "surveyId": bucket["surveyId"],
            "surveyTitle": bucket["surveyTitle"],
            "responseCount": bucket["responseCount"],
            "versions": sorted(bucket["_versions"]),
        }
        for sid in sorted(avail_by_sid.keys(), key=_survey_sort_key)
        for bucket in [avail_by_sid[sid]]
    ]

    # Compute availableVersions across current scope + optional survey_id
    version_pool = (
        [rec for rec in scoped_records if _record_survey_id(rec) == clean_sid]
        if clean_sid is not None
        else scoped_records
    )
    available_versions = sorted(
        {ver for rec in version_pool if (ver := _record_survey_version(rec))}
    )

    # Normalize & group filtered records by surveyId
    records_by_sid: dict[str, list[StudyRecord]] = {}
    responses_by_sid: dict[str, list[dict[str, Any]]] = {}
    for rec in filtered_records:
        resp = _normalize_response_record(rec, active_links_by_record_id)
        sid = resp["surveyId"]
        records_by_sid.setdefault(sid, []).append(rec)
        responses_by_sid.setdefault(sid, []).append(resp)

    surveys_out: list[dict[str, Any]] = []
    for sid in sorted(records_by_sid.keys(), key=_survey_sort_key):
        resps = sorted(
            responses_by_sid[sid],
            key=lambda r: (
                str(r.get("submittedAt") or r.get("createdAt") or ""),
                int(r["recordId"]),
            ),
            reverse=True,
        )
        surveys_out.append(
            _aggregate_survey_group(
                sid,
                records_by_sid[sid],
                resps,
                active_links_by_record_id,
            )
        )

    return redact_secret_keys(
        {
            "generatedAt": generated_at,
            "scope": normalized_scope,
            "filters": {
                "scope": normalized_scope,
                "surveyId": clean_sid,
                "surveyVersion": clean_ver,
            },
            "scopeCounts": scope_counts,
            "availableSurveys": available_surveys,
            "availableVersions": available_versions,
            "totalResponses": len(filtered_records),
            "surveys": surveys_out,
        }
    )


def _raw_non_blank_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _is_pure_number_string(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    try:
        val = float(stripped)
    except ValueError:
        return False
    return math.isfinite(val)


def _sanitize_csv_text_cell(value: Any) -> str:
    """Apply Excel/Sheets formula-injection escaping ONLY to non-numeric string cells."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if not text:
        return ""
    if _is_pure_number_string(text):
        return text
    if text.startswith(_FORMULA_INJECTION_PREFIXES) or text.lstrip().startswith(
        _FORMULA_INJECTION_PREFIXES
    ):
        return f"'{text}"
    return text


def _format_numeric_csv_cell(value: Any) -> str | int | float:
    """Format numeric CSV cells cleanly without leading quote escaping."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return int(value) if value.is_integer() else value
    parsed = _parse_score(value)
    if parsed is None:
        return ""
    return parsed


def _sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "part"


def build_surveys_csv(
    *,
    matches: Sequence[Mapping[str, Any]],
    survey_records: Sequence[StudyRecord],
    scope: str = "active_matches",
    survey_id: str | None = None,
    survey_version: str | None = None,
) -> tuple[str, str]:
    """Build UTF-8 BOM CRLF wide-table CSV across filtered survey responses."""
    (
        normalized_scope,
        clean_sid,
        clean_ver,
        active_links_by_record_id,
        _,
        _,
        filtered_records,
    ) = _select_scoped_and_filtered_records(
        matches=matches,
        survey_records=survey_records,
        scope=scope,
        survey_id=survey_id,
        survey_version=survey_version,
    )

    normalized_rows = [
        _normalize_response_record(
            rec, active_links_by_record_id, include_raw_csv_fields=True
        )
        for rec in filtered_records
    ]
    normalized_rows.sort(
        key=lambda r: (str(r.get("submittedAt") or r.get("createdAt") or ""), int(r["recordId"])),
        reverse=True,
    )

    # Discover dynamic question columns ordered by survey priority -> questionIndex -> seen order
    discovered_questions: dict[tuple[str, str], dict[str, Any]] = {}
    q_seq = 0
    for resp in normalized_rows:
        sid = str(resp["surveyId"])
        for ans in resp.get("answers") or ():
            q_id = _clean_text(ans.get("questionId"))
            q_idx = _parse_optional_int(ans.get("questionIndex"))
            if not q_id:
                if q_idx is None:
                    continue
                q_id = f"q_{q_idx}"
            q_type = _clean_text(ans.get("questionType")) or (
                "likert_5"
                if _parse_score(ans.get("score")) is not None
                else "textarea"
                if _clean_text(ans.get("textValue"))
                else "single_choice"
            )
            key = (sid, q_id)
            if key not in discovered_questions:
                q_seq += 1
                discovered_questions[key] = {
                    "surveyId": sid,
                    "questionId": q_id,
                    "questionType": q_type,
                    "_sortIndex": q_idx if q_idx is not None else 10**9,
                    "_seq": q_seq,
                }
            elif q_idx is not None and discovered_questions[key]["_sortIndex"] == 10**9:
                discovered_questions[key]["_sortIndex"] = q_idx

    ordered_questions = sorted(
        discovered_questions.values(),
        key=lambda q: (
            _survey_sort_key(q["surveyId"]),
            q["_sortIndex"],
            q["_seq"],
            q["questionId"],
        ),
    )

    # Build deduplicated dynamic column specs: (header_name, question_id, field_kind)
    question_columns: list[tuple[str, str, str]] = []
    seen_headers: set[str] = set()
    for q in ordered_questions:
        q_id = q["questionId"]
        q_type = q["questionType"]
        if q_type == "single_choice":
            candidates = [
                (f"q_{q_id}__option_id", q_id, "option_id"),
                (f"q_{q_id}__label", q_id, "label"),
            ]
        elif q_type == "likert_5":
            candidates = [
                (f"q_{q_id}__score", q_id, "score"),
                (f"q_{q_id}__label", q_id, "label"),
            ]
        elif q_type == "textarea":
            candidates = [(f"q_{q_id}__text", q_id, "text")]
        else:
            candidates = [(f"q_{q_id}__value", q_id, "value")]

        for col_name, col_qid, col_kind in candidates:
            if col_name not in seen_headers:
                seen_headers.add(col_name)
                question_columns.append((col_name, col_qid, col_kind))

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(_CSV_METADATA_COLUMNS) + [col[0] for col in question_columns])

    for resp in normalized_rows:
        answers_by_qid: dict[str, Mapping[str, Any]] = {}
        for ans in resp.get("answers") or ():
            q_id = _clean_text(ans.get("questionId"))
            q_idx = _parse_optional_int(ans.get("questionIndex"))
            if not q_id:
                if q_idx is None:
                    continue
                q_id = f"q_{q_idx}"
            if q_id not in answers_by_qid:
                answers_by_qid[q_id] = ans

        row: list[Any] = [
            _format_numeric_csv_cell(resp.get("recordId")),
            _sanitize_csv_text_cell(resp.get("responseId")),
            _sanitize_csv_text_cell(resp.get("surveyId")),
            _sanitize_csv_text_cell(resp.get("surveyVersion")),
            _sanitize_csv_text_cell(resp.get("entrySource")),
            _sanitize_csv_text_cell(resp.get("matchId")),
            _sanitize_csv_text_cell(resp.get("roomCode")),
            _format_numeric_csv_cell(resp.get("playerSlot")),
            _sanitize_csv_text_cell(resp.get("studySessionId")),
            _sanitize_csv_text_cell(resp.get("rawParticipantId") or resp.get("participantId")),
            _sanitize_csv_text_cell(
                resp.get("rawExperimentCondition") or resp.get("experimentCondition")
            ),
            _sanitize_csv_text_cell(resp.get("language")),
            _sanitize_csv_text_cell(resp.get("submittedAt")),
            _format_numeric_csv_cell(resp.get("durationSeconds")),
        ]

        for _, col_qid, col_kind in question_columns:
            ans = answers_by_qid.get(col_qid)
            if ans is None:
                row.append("")
                continue
            if col_kind == "score":
                row.append(_format_numeric_csv_cell(ans.get("score")))
            elif col_kind == "option_id":
                row.append(_sanitize_csv_text_cell(_clean_text(ans.get("optionId")) or None))
            elif col_kind == "label":
                row.append(_sanitize_csv_text_cell(_extract_choice_label(ans) or None))
            elif col_kind == "text":
                row.append(_sanitize_csv_text_cell(_raw_non_blank_str(ans.get("textValue"))))
            else:
                fallback = (
                    _raw_non_blank_str(ans.get("textValue"))
                    or _clean_text(ans.get("optionId"))
                    or _extract_choice_label(ans)
                    or ans.get("score")
                )
                row.append(_sanitize_csv_text_cell(fallback))

        writer.writerow(row)

    parts = ["surveys", _sanitize_filename_part(normalized_scope)]
    if clean_sid:
        parts.append(_sanitize_filename_part(clean_sid))
    if clean_ver:
        parts.append(_sanitize_filename_part(clean_ver))
    filename = f"{'-'.join(parts)}.csv"
    return buf.getvalue(), filename
