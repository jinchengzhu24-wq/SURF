"""Idempotent local development CLI for seeding, cleaning, and resetting Dashboard demo data.

Usage:
    python -m server.tools.seed_dashboard_demo {seed|clean|reset}
        [--db server/data/sokoban.sqlite3]
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from server.core.solver import solve_min_steps
from server.persistence.database import DEFAULT_DATABASE_PATH, SQLiteDatabase, dump_json

SEED_PREFIX = "seed-demo-"
SURVEY_VERSION = "v2.0-surf-study"

CHILD_TABLES_BY_SESSION: tuple[str, ...] = (
    "level_versions",
    "conversation_turns",
    "change_proposals",
    "revision_challenges",
    "play_attempts",
    "draft_regenerations",
    "binary_discovery_topics",
    "component_recovery_tasks",
    "audit_events",
    "idempotency_keys",
)

MAP_BASE: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@.....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

MAP_M1_P1_V2: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#....@..#..#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

MAP_M1_P1_V3: list[str] = [
    "############",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#.......#..#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

MAP_M1_P2_V1: list[str] = [
    "############",
    "#..........#",
    "#..........#",
    "#..p..s..t.#",
    "#....#.....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

MAP_M1_P2_V2: list[str] = [
    "############",
    "#..........#",
    "#..........#",
    "#..p..s..t.#",
    "#....#..#..#",
    "#..........#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

_LIKERT_LABELS: dict[int, tuple[str, str]] = {
    1: ("非常不同意", "Strongly disagree"),
    2: ("不同意", "Disagree"),
    3: ("一般", "Neither agree nor disagree"),
    4: ("同意", "Agree"),
    5: ("非常同意", "Strongly agree"),
}

_PRE_QUESTION_DEFS: tuple[dict[str, Any], ...] = (
    {
        "sectionId": "pre_match_background",
        "questionIndex": 2,
        "questionId": "gender",
        "zh": "您的性别是？",
        "en": "What is your gender?",
        "options": {
            "man": (1, "男", "Man"),
            "woman": (2, "女", "Woman"),
            "non_binary_self_describe": (3, "非二元或自述", "Non-binary or self-describe"),
            "prefer_not_to_say": (4, "不愿透露", "Prefer not to say"),
        },
    },
    {
        "sectionId": "pre_match_background",
        "questionIndex": 3,
        "questionId": "age_range",
        "zh": "您的年龄区间是？",
        "en": "What is your age range?",
        "options": {
            "under_18": (1, "18岁以下", "Under 18"),
            "18_24": (2, "18–24岁", "18–24"),
            "25_34": (3, "25–34岁", "25–34"),
            "35_plus": (4, "35岁及以上", "35+"),
            "prefer_not_to_say": (5, "不愿透露", "Prefer not to say"),
        },
    },
    {
        "sectionId": "pre_match_background",
        "questionIndex": 4,
        "questionId": "sokoban_experience",
        "zh": "您以前玩过推箱子（Sokoban）游戏吗？",
        "en": "Have you played Sokoban before?",
        "options": {
            "yes": (1, "是", "Yes"),
            "no": (2, "否", "No"),
        },
    },
    {
        "sectionId": "pre_match_background",
        "questionIndex": 5,
        "questionId": "genai_experience",
        "zh": "您平时使用生成式 AI 工具（如 ChatGPT、DeepSeek、Kimi 等）的频率是？",
        "en": "How frequently do you use generative AI tools (e.g., ChatGPT, DeepSeek, Kimi)?",
        "options": {
            "never": (1, "从未用过", "Never"),
            "rarely": (
                2,
                "偶尔使用（每月几次或更少）",
                "Rarely (a few times a month or less)",
            ),
            "regularly": (3, "经常使用（每周几次）", "Regularly (a few times a week)"),
            "daily": (4, "几乎每天使用", "Daily"),
        },
    },
    {
        "sectionId": "pre_match_background",
        "questionIndex": 6,
        "questionId": "strangers_to_other_group",
        "zh": "您是否与另一组的两位参与者都不认识？",
        "en": "Are you strangers to both participants in the other group?",
        "options": {
            "yes": (1, "是", "Yes"),
            "no": (2, "否", "No"),
        },
    },
)

_ROUND_NUMBER_OPTIONS: dict[str, tuple[int, str, str]] = {
    "round_1": (1, "第 1 轮", "Round 1"),
    "round_2": (2, "第 2 轮", "Round 2"),
}

_RELATIONSHIP_OPTIONS: dict[str, tuple[int, str, str]] = {
    "stranger": (1, "陌生人", "Stranger"),
    "friend": (2, "朋友", "Friend"),
    "colleague": (3, "同学或同事", "Classmate or colleague"),
    "dating_romantic": (4, "约会或恋爱关系", "Dating or romantic partner"),
    "married": (5, "婚姻关系", "Married partner"),
}

_PARTICIPANT_CODE_PROMPT: tuple[str, str] = (
    "您的实验编号是？（请填写主持者分配的编号，如 G01-A）",
    "What is your participant ID? (Enter the code assigned by the experimenter, e.g., G01-A)",
)

_POST_REL_LIKERT_SPECS: tuple[tuple[str, int, str, str, str], ...] = (
    (
        "part_a",
        4,
        "rel_comfort",
        "在日常互动中，与这位对手相处时，我通常感到自在，并且能够自然地交流。",
        "In everyday interactions, I usually feel comfortable with this opponent "
        "and can communicate naturally.",
    ),
    (
        "part_a",
        5,
        "rel_conflict",
        "在日常互动中，我和这位对手之间容易出现意见冲突、尴尬或让我感到不舒服的情况。",
        "In everyday interactions, disagreements, awkwardness, or uncomfortable moments "
        "tend to occur between this opponent and me.",
    ),
    (
        "part_a",
        6,
        "rel_positive_responsive",
        "当我向这位对手分享让我开心、兴奋或自豪的事情时，对方通常会认真回应，"
        "并表现出对这件事的兴趣或认同。",
        "When I share something that makes me happy, excited, or proud, this opponent "
        "usually responds attentively and shows interest or appreciation.",
    ),
    (
        "part_a",
        7,
        "rel_positive_downplay",
        "当我分享让我开心、兴奋或自豪的事情时，这位对手有时会淡化我的感受、"
        "转移话题，或让我觉得这件事并不重要。",
        "When I share something that makes me happy, excited, or proud, this opponent "
        "sometimes downplays my feelings, changes the subject, or makes the experience "
        "seem unimportant.",
    ),
    (
        "part_a",
        8,
        "rel_support_willing",
        "当我需要建议、理解或帮助时，这位对手通常愿意听我说明情况，并尝试理解或支持我。",
        "When I need advice, understanding, or help, this opponent is usually willing "
        "to listen to my situation and try to understand or support me.",
    ),
    (
        "part_a",
        9,
        "rel_support_dismissive",
        "当我需要建议、理解或帮助时，这位对手有时会敷衍、忽视，"
        "或在没有充分理解我的情况下批评我的做法。",
        "When I need advice, understanding, or help, this opponent sometimes dismisses "
        "or ignores me, or criticizes what I did without fully understanding my situation.",
    ),
)

_POST_CORE_LIKERT_SPECS: tuple[tuple[str, int, str, str, str], ...] = (
    (
        "part_b",
        10,
        "emo_curiosity",
        "当对手第一次看到关卡时，我希望他们对如何解开它产生好奇，并愿意继续探索。",
        "When my opponent first saw the level, I wanted them to feel curious about "
        "how it could be solved and want to explore further.",
    ),
    (
        "part_b",
        11,
        "emo_tension",
        "当对手面对一个可能影响后续解法的重要决定时，我希望他们感到一定的紧张，需要谨慎选择。",
        "When facing an important decision that could affect the rest of the solution, "
        "I wanted my opponent to feel some tension and choose carefully.",
    ),
    (
        "part_b",
        12,
        "emo_frustration_wrong_push",
        "当对手发现一次错误推动破坏了原有计划或进入不利局面时，"
        "我希望他们感到一定程度的懊恼或挫败。",
        "When my opponent realized that a wrong push had disrupted their plan or put them "
        "in an unfavorable situation, I wanted them to feel some frustration.",
    ),
    (
        "part_b",
        13,
        "emo_focus",
        "当对手逐渐理解关卡并连续推进解法时，我希望他们专注于解题过程，不容易被其他事情分散注意。",
        "As my opponent gradually understood the level and progressed through the solution, "
        "I wanted them to become deeply focused on the solving process.",
    ),
    (
        "part_b",
        14,
        "emo_achievement",
        "当对手最终完成关卡时，我希望他们因为克服了其中的困难而获得明显的成就感。",
        "When my opponent finally completed the level, I wanted them to feel a strong "
        "sense of achievement from overcoming its difficulties.",
    ),
    (
        "part_b",
        15,
        "emo_happy_satisfied",
        "在完成关卡后，我希望对手整体上对这次解题经历感到开心和满足。",
        "After completing the level, I wanted my opponent to feel happy and satisfied "
        "with the overall solving experience.",
    ),
    (
        "part_c",
        16,
        "ai_referred_goal",
        "AI 的回应提到了我明确描述的具体关卡目标，而不是只给出通用建议。",
        "The AI's responses referred to the specific level goal I described, rather than "
        "giving only generic advice.",
    ),
    (
        "part_c",
        17,
        "ai_reflected_change",
        "当我澄清或改变想法后，AI 的后续建议能够体现这一变化。",
        "After I clarified or changed my idea, the AI's next suggestions reflected this change.",
    ),
    (
        "part_c",
        18,
        "ai_preserved_constraint",
        "当我提出明确的限制或需要保留的要求时，AI 后续的建议没有忽视它。",
        "When I stated a clear constraint or requirement to preserve, the AI's later "
        "suggestions did not ignore it.",
    ),
    (
        "part_c",
        19,
        "ai_explained_relevance",
        "AI 会说明建议如何对应我表达的目标，帮助我判断是否值得采用。",
        "The AI explained how a suggestion related to my stated goal, helping me judge "
        "whether to adopt it.",
    ),
    (
        "part_c",
        20,
        "ai_practical_improvement",
        "AI 的建议帮助我找到实际可用的关卡改进方式。",
        "The AI's suggestions helped me identify practical ways to improve the level.",
    ),
    (
        "part_c",
        21,
        "ai_effective_within_time",
        "总体而言，AI 帮助我在有限的设计时间内更有效地完成或改进了关卡。",
        "Overall, the AI helped me complete or improve the level more effectively within "
        "the available design time.",
    ),
    (
        "part_c",
        22,
        "ai_distinguish_decisions",
        "我能清楚地区分自己的设计决定与 AI 提出的建议，并决定是否采纳。",
        "I could distinguish my own design decisions from AI suggestions and decide "
        "whether to adopt them.",
    ),
    (
        "part_d",
        23,
        "outcome_met_recipient_goal",
        "最终完成的关卡很好地实现了我为这位对手设定的设计目标。",
        "The final level well achieved the design goals I intended for this specific opponent.",
    ),
    (
        "part_d",
        24,
        "outcome_overall_satisfaction",
        "总体而言，我对最终完成的这个关卡感到满意。",
        "Overall, I am satisfied with the final level I created.",
    ),
)


class ProductionGuardError(RuntimeError):
    """Raised when targeting a /opt/sokoban production database without --force."""


def is_production_db_path(db_path: str | Path) -> bool:
    """Return True if raw or resolved db_path points inside /opt/sokoban."""
    raw_norm = str(db_path).replace("\\", "/").casefold()
    resolved_norm = str(Path(db_path).expanduser().resolve()).replace("\\", "/").casefold()
    return "/opt/sokoban" in raw_norm or "/opt/sokoban" in resolved_norm


def _enforce_db_guard(db_path: str | Path, *, force: bool) -> Path:
    if not force and is_production_db_path(db_path):
        raise ProductionGuardError(
            f"Refusing to modify production database path '{db_path}' without --force."
        )
    return Path(db_path).expanduser()


def _det_hash(label: str) -> str:
    return hashlib.sha256(f"seed-demo-hash:{label}".encode()).hexdigest()


def _require_solvable(rows: Sequence[str]) -> dict[str, Any]:
    solved = solve_min_steps(rows)
    if not solved.solvable:
        raise ValueError(f"Seed map must be solvable: {rows!r}")
    return solved.as_dict()


def _compute_diff(before_rows: Sequence[str], after_rows: Sequence[str]) -> list[dict[str, Any]]:
    diff: list[dict[str, Any]] = []
    for y, (b_row, a_row) in enumerate(zip(before_rows, after_rows, strict=True)):
        for x, (b_ch, a_ch) in enumerate(zip(b_row, a_row, strict=True)):
            if b_ch != a_ch:
                diff.append({"x": x, "y": y, "before": b_ch, "after": a_ch})
    return diff


def _textarea_answer(
    *,
    section_id: str,
    question_index: int,
    question_id: str,
    zh: str,
    en: str,
    text_value: str,
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


def _build_pre_answers(
    *,
    participant_code: str,
    gender: str,
    age_range: str,
    sokoban_experience: str,
    genai_experience: str,
    strangers_to_other_group: str,
) -> list[dict[str, Any]]:
    selected = {
        "gender": gender,
        "age_range": age_range,
        "sokoban_experience": sokoban_experience,
        "genai_experience": genai_experience,
        "strangers_to_other_group": strangers_to_other_group,
    }
    answers: list[dict[str, Any]] = [
        _textarea_answer(
            section_id="pre_match_background",
            question_index=1,
            question_id="participant_code",
            zh=_PARTICIPANT_CODE_PROMPT[0],
            en=_PARTICIPANT_CODE_PROMPT[1],
            text_value=participant_code,
        )
    ]
    for q in _PRE_QUESTION_DEFS:
        opt_id = selected[q["questionId"]]
        opt_idx, opt_zh, opt_en = q["options"][opt_id]
        answers.append(
            {
                "sectionId": q["sectionId"],
                "questionIndex": q["questionIndex"],
                "questionId": q["questionId"],
                "questionType": "single_choice",
                "questionTextZh": q["zh"],
                "questionTextEn": q["en"],
                "optionId": opt_id,
                "optionIndex": opt_idx,
                "optionTextZh": opt_zh,
                "optionTextEn": opt_en,
                "score": None,
                "textValue": None,
            }
        )
    return answers


def _append_likert_answers(
    answers: list[dict[str, Any]],
    specs: Sequence[tuple[str, int, str, str, str]],
    scores: Sequence[int],
) -> None:
    for (sec_id, q_idx, q_id, q_zh, q_en), score in zip(specs, scores, strict=True):
        opt_zh, opt_en = _LIKERT_LABELS[score]
        answers.append(
            {
                "sectionId": sec_id,
                "questionIndex": q_idx,
                "questionId": q_id,
                "questionType": "likert_5",
                "questionTextZh": q_zh,
                "questionTextEn": q_en,
                "optionId": str(score),
                "optionIndex": score,
                "optionTextZh": opt_zh,
                "optionTextEn": opt_en,
                "score": score,
                "textValue": None,
            }
        )


def _build_post_answers(
    *,
    participant_code: str,
    round_number: str,
    relationship: str,
    rel_scores: Sequence[int] | None,
    core_scores: Sequence[int],
    final_self_reported_intent: str | None = None,
    ai_collaboration_reflection: str | None = None,
) -> list[dict[str, Any]]:
    if len(core_scores) != len(_POST_CORE_LIKERT_SPECS):
        raise ValueError(
            f"Expected {len(_POST_CORE_LIKERT_SPECS)} core Likert scores, got {len(core_scores)}"
        )
    rnd_idx, rnd_zh, rnd_en = _ROUND_NUMBER_OPTIONS[round_number]
    rel_idx, rel_zh, rel_en = _RELATIONSHIP_OPTIONS[relationship]
    answers: list[dict[str, Any]] = [
        _textarea_answer(
            section_id="part_a",
            question_index=1,
            question_id="participant_code",
            zh=_PARTICIPANT_CODE_PROMPT[0],
            en=_PARTICIPANT_CODE_PROMPT[1],
            text_value=participant_code,
        ),
        {
            "sectionId": "part_a",
            "questionIndex": 2,
            "questionId": "round_number",
            "questionType": "single_choice",
            "questionTextZh": "这是您今天参加的第几轮正式对局？",
            "questionTextEn": "Which formal round of today's study is this?",
            "optionId": round_number,
            "optionIndex": rnd_idx,
            "optionTextZh": rnd_zh,
            "optionTextEn": rnd_en,
            "score": None,
            "textValue": None,
        },
        {
            "sectionId": "part_a",
            "questionIndex": 3,
            "questionId": "opponent_relationship",
            "questionType": "single_choice",
            "questionTextZh": "你与本轮对手的关系是？",
            "questionTextEn": "What is your relationship with the opponent in this round?",
            "optionId": relationship,
            "optionIndex": rel_idx,
            "optionTextZh": rel_zh,
            "optionTextEn": rel_en,
            "score": None,
            "textValue": None,
        },
    ]
    if relationship == "stranger":
        if rel_scores is not None:
            raise ValueError("rel_scores must be None when relationship is 'stranger'")
    else:
        if rel_scores is None or len(rel_scores) != len(_POST_REL_LIKERT_SPECS):
            raise ValueError(
                f"Expected {len(_POST_REL_LIKERT_SPECS)} relationship scores for {relationship}"
            )
        _append_likert_answers(answers, _POST_REL_LIKERT_SPECS, rel_scores)

    _append_likert_answers(answers, _POST_CORE_LIKERT_SPECS, core_scores)

    if final_self_reported_intent and final_self_reported_intent.strip():
        answers.append(
            _textarea_answer(
                section_id="part_d",
                question_index=25,
                question_id="final_self_reported_intent",
                zh=(
                    "（选填）请简要描述你为这位对手设计这关的最终设计意图（例如想让对方"
                    "体验什么、特意保留或调整了哪些难点或布局；此项仅用于研究分析，不会展示给对手）。"
                ),
                en=(
                    "(Optional) Please briefly describe your final design goals and intentions "
                    "for this opponent (e.g., what experience you aimed for and what challenges "
                    "or layout choices you intentionally kept or adjusted; this is for research "
                    "only and will not be shown to your opponent)."
                ),
                text_value=final_self_reported_intent.strip(),
            )
        )
    if ai_collaboration_reflection and ai_collaboration_reflection.strip():
        answers.append(
            _textarea_answer(
                section_id="part_d",
                question_index=26,
                question_id="ai_collaboration_reflection",
                zh=(
                    "（选填）在本轮与 AI 共创的过程中，你的想法有没有发生变化？"
                    "与 AI 协作中哪次帮助或分歧让你印象最深？"
                ),
                en=(
                    "(Optional) Did your design ideas evolve during co-creation with the AI? "
                    "What moment of AI help or disagreement stood out most to you?"
                ),
                text_value=ai_collaboration_reflection.strip(),
            )
        )
    return answers


def _delete_seed_rows_in_conn(conn: sqlite3.Connection) -> dict[str, int]:
    deleted: dict[str, int] = {}
    like_pat = f"{SEED_PREFIX}%"

    sess_rows = conn.execute(
        "SELECT session_id FROM design_sessions WHERE session_id LIKE ? OR match_id LIKE ?",
        (like_pat, like_pat),
    ).fetchall()
    seed_session_ids = [str(r["session_id"]) for r in sess_rows]

    for tbl in CHILD_TABLES_BY_SESSION:
        if seed_session_ids:
            placeholders = ",".join("?" for _ in seed_session_ids)
            cur = conn.execute(
                f"DELETE FROM {tbl} WHERE session_id IN ({placeholders}) OR session_id LIKE ?",
                (*seed_session_ids, like_pat),
            )
        else:
            cur = conn.execute(f"DELETE FROM {tbl} WHERE session_id LIKE ?", (like_pat,))
        deleted[tbl] = cur.rowcount

    cur_ds = conn.execute(
        "DELETE FROM design_sessions WHERE session_id LIKE ? OR match_id LIKE ?",
        (like_pat, like_pat),
    )
    deleted["design_sessions"] = cur_ds.rowcount

    cur_ev = conn.execute("DELETE FROM online_room_events WHERE match_id LIKE ?", (like_pat,))
    deleted["online_room_events"] = cur_ev.rowcount

    cur_rm = conn.execute("DELETE FROM online_rooms WHERE id LIKE ?", (like_pat,))
    deleted["online_rooms"] = cur_rm.rowcount

    cur_st = conn.execute(
        "DELETE FROM study_records WHERE "
        "match_id LIKE ? OR participant_id LIKE ? "
        "OR json_extract(payload_json, '$.responseId') LIKE ? "
        "OR json_extract(payload_json, '$.studySessionId') LIKE ? "
        "OR json_extract(payload_json, '$.matchId') LIKE ?",
        (like_pat, like_pat, like_pat, like_pat, like_pat),
    )
    deleted["study_records"] = cur_st.rowcount
    return deleted


def clean_seed_data(
    db_path: str | Path = DEFAULT_DATABASE_PATH, *, force: bool = False
) -> dict[str, int]:
    """Delete ONLY seed-demo-* records across all SQLite tables without touching real rows."""
    target = _enforce_db_guard(db_path, force=force)
    db = SQLiteDatabase(target)
    db.initialize()
    with db.write_tx() as conn:
        return _delete_seed_rows_in_conn(conn)


def _insert_room_event(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    player_slot: int,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO online_room_events("
        "match_id, player_slot, seq, event_type, payload_json, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (match_id, player_slot, seq, event_type, dump_json(payload), created_at),
    )


def _insert_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    session_id: str,
    version_number: int,
    source: str,
    summary: str,
    rows: list[str],
    diff: list[dict[str, Any]],
    solver: dict[str, Any],
    design_context: dict[str, Any] | None,
    assessment: dict[str, Any] | None,
    parent_version_id: str | None,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO level_versions("
        "version_id, session_id, version_number, source, summary, rows_json, diff_json, "
        "design_context_json, solver_json, entity_bindings_json, assessment_json, "
        "parent_version_id, idempotency_key, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            session_id,
            version_number,
            source,
            summary,
            dump_json(rows),
            dump_json(diff),
            dump_json(design_context) if design_context is not None else None,
            dump_json(solver),
            None,
            dump_json(assessment) if assessment is not None else None,
            parent_version_id,
            f"seed-idem-ver:{version_id}",
            created_at,
        ),
    )


def _insert_turn(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    session_id: str,
    seq: int,
    request_id: str,
    role: str,
    content: str,
    version_id: str,
    created_at: str,
    guidance: dict[str, Any] | None = None,
    proposal_binding: dict[str, Any] | None = None,
    language: str = "en",
) -> None:
    conn.execute(
        "INSERT INTO conversation_turns("
        "turn_id, session_id, sequence_number, request_id, role, content, language, "
        "guidance_json, translations_json, proposal_binding_json, model, attempts_used, "
        "latency_ms, version_id, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            turn_id,
            session_id,
            seq,
            request_id,
            role,
            content,
            language,
            dump_json(guidance) if guidance is not None else None,
            None,
            dump_json(proposal_binding) if proposal_binding is not None else None,
            "seed-demo-mock-model" if role == "assistant" else None,
            1 if role == "assistant" else None,
            120 if role == "assistant" else None,
            version_id,
            created_at,
        ),
    )


def _insert_audit(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO audit_events(session_id, event_type, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, event_type, dump_json(payload), created_at),
    )


def _insert_survey_record(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    match_id = str(payload.get("matchId") or "").strip() or None
    participant_id = (
        str(payload.get("participantId") or payload.get("studySessionId") or "").strip() or None
    )
    round_id = str(payload.get("surveyId") or "").strip() or None
    conn.execute(
        "INSERT INTO study_records("
        "record_type, match_id, participant_id, round_id, payload_json, created_at"
        ") VALUES ('survey', ?, ?, ?, ?, ?)",
        (match_id, participant_id, round_id, dump_json(payload), created_at),
    )


def _seed_all_in_conn(conn: sqlite3.Connection) -> dict[str, int]:
    solver_base = _require_solvable(MAP_BASE)
    solver_m1_p1_v2 = _require_solvable(MAP_M1_P1_V2)
    solver_m1_p1_v3 = _require_solvable(MAP_M1_P1_V3)
    solver_m1_p2_v1 = _require_solvable(MAP_M1_P2_V1)
    solver_m1_p2_v2 = _require_solvable(MAP_M1_P2_V2)

    diff_m1_p1_v2 = _compute_diff(MAP_BASE, MAP_M1_P1_V2)
    diff_m1_p1_v3 = _compute_diff(MAP_M1_P1_V2, MAP_M1_P1_V3)
    diff_m1_p2_v2 = _compute_diff(MAP_M1_P2_V1, MAP_M1_P2_V2)

    # =========================================================================
    # MATCH 1: ROOM SEED01 (completed) — rich co-creation + Pre & Post surveys
    # =========================================================================
    m1_id = "seed-demo-match-01"
    m1_code = "SEED01"
    m1_p1_sid = "seed-demo-session-m1-p1"
    m1_p2_sid = "seed-demo-session-m1-p2"
    m1_p1_co = "seed-demo-co-m1-p1"
    m1_p2_co = "seed-demo-co-m1-p2"

    m1_p1_challenge = {
        "submittedAt": "2026-11-27T10:15:00Z",
        "aiAssistantMode": "description_generation",
        "rows": MAP_M1_P1_V3,
    }
    m1_p2_challenge = {
        "submittedAt": "2026-11-27T10:14:30Z",
        "aiAssistantMode": "partial_completion",
        "rows": MAP_M1_P2_V2,
    }
    m1_p1_result = {
        "submittedAt": "2026-11-27T10:17:10Z",
        "durationSeconds": 24.5,
        "moveCount": 9,
        "minimumMoves": int(solver_m1_p2_v2["solutionSteps"] or 6),
        "restartCount": 1,
        "outcome": "completed",
    }
    m1_p2_result = {
        "submittedAt": "2026-11-27T10:19:40Z",
        "durationSeconds": 180.0,
        "moveCount": 27,
        "minimumMoves": int(solver_m1_p1_v3["solutionSteps"] or 6),
        "restartCount": 3,
        "outcome": "timed_out",
    }
    p1_msg = "Removed center water so the right-flank choke guides a clean detour."
    p2_msg = "Added a right-side anchor wall for a crisp two-turn push."

    conn.execute(
        "INSERT INTO online_rooms("
        "id, room_code, status, config_json, players_json, created_at, expires_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            m1_id,
            m1_code,
            "completed",
            dump_json({"seededDemo": True, "maxPlayers": 2}),
            dump_json(
                [
                    {
                        "playerNumber": 1,
                        "studySessionId": m1_p1_sid,
                        "menuStartedFlow": True,
                        "joinedAt": "2026-11-27T10:00:00Z",
                        "ready": True,
                        "readyAt": "2026-11-27T10:00:15Z",
                        "challenge": m1_p1_challenge,
                        "result": m1_p1_result,
                        "designerIntention": p1_msg,
                    },
                    {
                        "playerNumber": 2,
                        "studySessionId": m1_p2_sid,
                        "menuStartedFlow": True,
                        "joinedAt": "2026-11-27T10:00:10Z",
                        "ready": True,
                        "readyAt": "2026-11-27T10:00:20Z",
                        "challenge": m1_p2_challenge,
                        "result": m1_p2_result,
                        "designerIntention": p2_msg,
                    },
                ]
            ),
            "2026-11-27T10:00:00Z",
            "2026-11-28T10:00:00Z",
            "2026-11-27T10:19:40Z",
        ),
    )

    m1_p1_draft = {
        "finalDifficulty": "Medium",
        "finalLayout": "Balanced",
        "q1Answer": "observe_then_decide",
        "q2Answer": "easy_to_adjust",
        "q3Answer": "focused_area",
        "q4Answer": "short_routes",
        "aiReflection": "Balanced corridor pacing with clear push readability.",
        "aiDifficultyRationale": "Steady planning maps to Medium.",
        "aiLayoutRationale": "Focused central spacing maps to Balanced.",
        "aiRecommendedDifficulty": "Medium",
        "aiRecommendedLayout": "Balanced",
        "aiRecommendationSource": "llm",
    }
    m1_p2_draft = {
        "finalDifficulty": "Easy",
        "finalLayout": "Compact",
        "q1Answer": "quick_start",
        "q2Answer": "easy_to_adjust",
        "q3Answer": "focused_area",
        "q4Answer": "short_routes",
        "aiReflection": "Quick start puzzle focusing on direct box alignment.",
        "aiDifficultyRationale": "Quick start maps to Easy.",
        "aiLayoutRationale": "Tight push geometry maps to Compact.",
        "aiRecommendedDifficulty": "Easy",
        "aiRecommendedLayout": "Compact",
        "aiRecommendationSource": "llm",
    }

    m1_p1_events: list[tuple[str, dict[str, Any], str]] = [
        (
            "room_created",
            {
                "eventId": "seed-ev-m1-create",
                "eventType": "room_created",
                "statusAfter": "waiting",
            },
            "2026-11-27T10:00:00Z",
        ),
        (
            "ready_changed",
            {
                "eventId": "seed-ev-m1-p1-ready",
                "eventType": "ready_changed",
                "ready": True,
                "statusAfter": "waiting",
            },
            "2026-11-27T10:00:15Z",
        ),
        (
            "draft",
            {
                "eventId": "seed-ev-m1-p1-draft",
                "eventType": "draft",
                "sessionId": m1_p1_co,
                "rows": MAP_BASE,
                **m1_p1_draft,
            },
            "2026-11-27T10:01:00Z",
        ),
        (
            "first_stage",
            {
                "eventId": "seed-ev-m1-p1-s1",
                "eventType": "first_stage",
                "sessionId": m1_p1_co,
                "versionId": "seed-demo-ver-m1-p1-v1",
                "stageNumber": 1,
                "initialDraftMethod": "description_generation",
                "rows": MAP_BASE,
            },
            "2026-11-27T10:01:30Z",
        ),
        (
            "opening",
            {
                "eventId": "seed-ev-m1-p1-open1",
                "eventType": "opening",
                "sessionId": m1_p1_co,
                "versionId": "seed-demo-ver-m1-p1-v1",
                "stageNumber": 1,
                "assistantTurnId": "seed-turn-m1-p1-open1",
                "assistantText": (
                    "Stage 1 starts solvable in 6 moves with a clean center corridor."
                ),
                "language": "en",
                "cards": [
                    {
                        "type": "discussion",
                        "text": "Keep the central lane clear or add a flank detour?",
                    }
                ],
            },
            "2026-11-27T10:01:40Z",
        ),
        (
            "stage",
            {
                "eventId": "seed-ev-m1-p1-s2",
                "eventType": "stage",
                "sessionId": m1_p1_co,
                "versionId": "seed-demo-ver-m1-p1-v2",
                "stageNumber": 2,
                "source": "manual",
                "rows": MAP_M1_P1_V2,
            },
            "2026-11-27T10:04:00Z",
        ),
        (
            "stage",
            {
                "eventId": "seed-ev-m1-p1-s3",
                "eventType": "stage",
                "sessionId": m1_p1_co,
                "versionId": "seed-demo-ver-m1-p1-v3",
                "stageNumber": 3,
                "source": "ai",
                "rows": MAP_M1_P1_V3,
            },
            "2026-11-27T10:11:30Z",
        ),
        (
            "final",
            {
                "eventId": "seed-ev-m1-p1-final",
                "eventType": "final",
                "sessionId": m1_p1_co,
                "versionId": "seed-demo-ver-m1-p1-v3",
                "stageNumber": 3,
                "rows": MAP_M1_P1_V3,
                "coCreationDurationSeconds": 720.0,
            },
            "2026-11-27T10:13:30Z",
        ),
        (
            "message",
            {
                "eventId": "seed-ev-m1-p1-msg",
                "eventType": "message",
                "sessionId": m1_p1_co,
                "message": p1_msg,
            },
            "2026-11-27T10:14:00Z",
        ),
        (
            "challenge_submitted",
            {
                "eventId": "seed-ev-m1-p1-chg",
                "eventType": "challenge_submitted",
                "statusAfter": "playing",
                **m1_p1_challenge,
            },
            "2026-11-27T10:15:00Z",
        ),
        (
            "designer_intention_synchronized",
            {
                "eventId": "seed-ev-m1-p1-int",
                "eventType": "designer_intention_synchronized",
                "designerIntention": p1_msg,
                "statusAfter": "playing",
            },
            "2026-11-27T10:15:05Z",
        ),
        (
            "result_submitted",
            {
                "eventId": "seed-ev-m1-p1-res",
                "eventType": "result_submitted",
                "statusAfter": "playing",
                **m1_p1_result,
            },
            "2026-11-27T10:17:10Z",
        ),
    ]
    for seq, (ev_type, ev_payload, ts) in enumerate(m1_p1_events, start=1):
        _insert_room_event(
            conn,
            match_id=m1_id,
            player_slot=1,
            seq=seq,
            event_type=ev_type,
            payload={
                "matchId": m1_id,
                "roomCode": m1_code,
                "playerNumber": 1,
                "studySessionId": m1_p1_sid,
                "menuStartedFlow": True,
                "serverReceivedAt": ts,
                **ev_payload,
            },
            created_at=ts,
        )

    m1_p2_events: list[tuple[str, dict[str, Any], str]] = [
        (
            "player_joined",
            {
                "eventId": "seed-ev-m1-p2-join",
                "eventType": "player_joined",
                "statusAfter": "waiting",
            },
            "2026-11-27T10:00:10Z",
        ),
        (
            "ready_changed",
            {
                "eventId": "seed-ev-m1-p2-ready",
                "eventType": "ready_changed",
                "ready": True,
                "statusAfter": "co_creating",
            },
            "2026-11-27T10:00:20Z",
        ),
        (
            "draft",
            {
                "eventId": "seed-ev-m1-p2-draft",
                "eventType": "draft",
                "sessionId": m1_p2_co,
                "rows": MAP_M1_P2_V1,
                **m1_p2_draft,
            },
            "2026-11-27T10:01:10Z",
        ),
        (
            "first_stage",
            {
                "eventId": "seed-ev-m1-p2-s1",
                "eventType": "first_stage",
                "sessionId": m1_p2_co,
                "versionId": "seed-demo-ver-m1-p2-v1",
                "stageNumber": 1,
                "initialDraftMethod": "partial_completion",
                "rows": MAP_M1_P2_V1,
            },
            "2026-11-27T10:01:45Z",
        ),
        (
            "opening",
            {
                "eventId": "seed-ev-m1-p2-open1",
                "eventType": "opening",
                "sessionId": m1_p2_co,
                "versionId": "seed-demo-ver-m1-p2-v1",
                "stageNumber": 1,
                "assistantTurnId": "seed-turn-m1-p2-open1",
                "assistantText": "Stage 1 starts with an open baseline lane solvable in 6 moves.",
                "language": "en",
                "cards": [
                    {"type": "clarification", "text": "Playtest to verify first push rhythm."}
                ],
            },
            "2026-11-27T10:02:00Z",
        ),
        (
            "stage",
            {
                "eventId": "seed-ev-m1-p2-s2",
                "eventType": "stage",
                "sessionId": m1_p2_co,
                "versionId": "seed-demo-ver-m1-p2-v2",
                "stageNumber": 2,
                "source": "manual",
                "rows": MAP_M1_P2_V2,
            },
            "2026-11-27T10:06:00Z",
        ),
        (
            "final",
            {
                "eventId": "seed-ev-m1-p2-final",
                "eventType": "final",
                "sessionId": m1_p2_co,
                "versionId": "seed-demo-ver-m1-p2-v2",
                "stageNumber": 2,
                "rows": MAP_M1_P2_V2,
                "coCreationDurationSeconds": 480.0,
            },
            "2026-11-27T10:12:00Z",
        ),
        (
            "message",
            {
                "eventId": "seed-ev-m1-p2-msg",
                "eventType": "message",
                "sessionId": m1_p2_co,
                "message": p2_msg,
            },
            "2026-11-27T10:12:30Z",
        ),
        (
            "challenge_submitted",
            {
                "eventId": "seed-ev-m1-p2-chg",
                "eventType": "challenge_submitted",
                "statusAfter": "playing",
                **m1_p2_challenge,
            },
            "2026-11-27T10:14:30Z",
        ),
        (
            "designer_intention_synchronized",
            {
                "eventId": "seed-ev-m1-p2-int",
                "eventType": "designer_intention_synchronized",
                "designerIntention": p2_msg,
                "statusAfter": "playing",
            },
            "2026-11-27T10:14:35Z",
        ),
        (
            "result_submitted",
            {
                "eventId": "seed-ev-m1-p2-res",
                "eventType": "result_submitted",
                "statusAfter": "completed",
                **m1_p2_result,
            },
            "2026-11-27T10:19:40Z",
        ),
    ]
    for seq, (ev_type, ev_payload, ts) in enumerate(m1_p2_events, start=1):
        _insert_room_event(
            conn,
            match_id=m1_id,
            player_slot=2,
            seq=seq,
            event_type=ev_type,
            payload={
                "matchId": m1_id,
                "roomCode": m1_code,
                "playerNumber": 2,
                "studySessionId": m1_p2_sid,
                "menuStartedFlow": True,
                "serverReceivedAt": ts,
                **ev_payload,
            },
            created_at=ts,
        )

    conn.execute(
        "INSERT INTO design_sessions("
        "session_id, match_id, player_slot, mode, language, language_locked, "
        "language_locked_at, status, secret_hash, access_token_hash, integration_token_hash, "
        "bootstrap_token_hash, bootstrap_used_at, creation_key, current_version_id, "
        "deadline_started_at, deadline_at, finalized_version_id, finalized_at, completed_at, "
        "final_intention_json, draft_rows_json, draft_validation_json, draft_generation, "
        "is_demo, created_at, updated_at, expires_at"
        ") VALUES ("
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        ")",
        (
            m1_p1_co,
            m1_id,
            1,
            "description_generation",
            "en",
            1,
            "2026-11-27T10:01:30Z",
            "completed",
            _det_hash("m1-p1-secret"),
            _det_hash("m1-p1-access"),
            _det_hash("m1-p1-integ"),
            _det_hash("m1-p1-boot"),
            "2026-11-27T10:01:15Z",
            "seed-demo-create-m1-p1",
            "seed-demo-ver-m1-p1-v3",
            "2026-11-27T10:01:30Z",
            "2026-11-27T10:21:30Z",
            "seed-demo-ver-m1-p1-v3",
            "2026-11-27T10:13:30Z",
            "2026-11-27T10:14:00Z",
            dump_json({"content": p1_msg, "createdAt": "2026-11-27T10:14:00Z"}),
            dump_json(MAP_BASE),
            dump_json(solver_base),
            2,
            0,
            "2026-11-27T10:01:05Z",
            "2026-11-27T10:14:00Z",
            "2026-11-28T10:14:00Z",
        ),
    )

    conn.execute(
        "INSERT INTO draft_regenerations("
        "request_id, session_id, idempotency_key, status, generation, failure_code, "
        "rows_json, validation_json, requested_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "seed-demo-regen-m1-p1-1",
            m1_p1_co,
            "seed-idem-regen-1",
            "failed",
            2,
            "GENERATOR_TIMEOUT",
            None,
            None,
            "2026-11-27T10:01:16Z",
            "2026-11-27T10:01:20Z",
        ),
    )
    conn.execute(
        "INSERT INTO draft_regenerations("
        "request_id, session_id, idempotency_key, status, generation, failure_code, "
        "rows_json, validation_json, requested_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "seed-demo-regen-m1-p1-2",
            m1_p1_co,
            "seed-idem-regen-2",
            "completed",
            3,
            None,
            dump_json(MAP_BASE),
            dump_json(solver_base),
            "2026-11-27T10:01:22Z",
            "2026-11-27T10:01:26Z",
        ),
    )

    _insert_version(
        conn,
        version_id="seed-demo-ver-m1-p1-v1",
        session_id=m1_p1_co,
        version_number=1,
        source="initial",
        summary="Initial Stage 1 draft",
        rows=MAP_BASE,
        diff=[],
        solver=solver_base,
        design_context={
            "intentHypotheses": [
                {
                    "id": "seed-hyp-m1-p1",
                    "statement": "Keep the central corridor readable.",
                    "status": "confirmed",
                }
            ]
        },
        assessment={"difficultyOpinion": "Clean baseline opening."},
        parent_version_id=None,
        created_at="2026-11-27T10:01:30Z",
    )
    _insert_version(
        conn,
        version_id="seed-demo-ver-m1-p1-v2",
        session_id=m1_p1_co,
        version_number=2,
        source="human_edit",
        summary="Added right-flank wall at (8, 4)",
        rows=MAP_M1_P1_V2,
        diff=diff_m1_p1_v2,
        solver=solver_m1_p1_v2,
        design_context=None,
        assessment={"difficultyOpinion": "Tightens the right turn slightly."},
        parent_version_id="seed-demo-ver-m1-p1-v1",
        created_at="2026-11-27T10:04:00Z",
    )
    _insert_version(
        conn,
        version_id="seed-demo-ver-m1-p1-v3",
        session_id=m1_p1_co,
        version_number=3,
        source="ai_proposal",
        summary="Removed center water tile from Stage 2",
        rows=MAP_M1_P1_V3,
        diff=diff_m1_p1_v3,
        solver=solver_m1_p1_v3,
        design_context=None,
        assessment={"difficultyOpinion": "Cleaner look-ahead with water removed."},
        parent_version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:11:30Z",
    )

    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-open1",
        session_id=m1_p1_co,
        seq=1,
        request_id="seed-req-open1",
        role="assistant",
        content="Stage 1 starts solvable in 6 moves with a clean center corridor.",
        version_id="seed-demo-ver-m1-p1-v1",
        created_at="2026-11-27T10:01:40Z",
        guidance={"followUpQuestion": "Keep the central lane clear or add a flank detour?"},
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-disc-u1",
        session_id=m1_p1_co,
        seq=2,
        request_id="seed-req-disc1",
        role="user",
        content="Does the center corridor feel easy enough to read?",
        version_id="seed-demo-ver-m1-p1-v1",
        created_at="2026-11-27T10:02:20Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-disc-a1",
        session_id=m1_p1_co,
        seq=3,
        request_id="seed-req-disc1",
        role="assistant",
        content=(
            "Yes — keeping the central corridor open lets opponents read the first push clearly."
        ),
        version_id="seed-demo-ver-m1-p1-v1",
        created_at="2026-11-27T10:02:25Z",
    )

    dis_id = "seed-demo-dis-m1-p1"
    dis_card = {
        "disagreementId": dis_id,
        "subject": "human_edit",
        "userPosition": "Placed an extra wall on the right flank at (8, 4).",
        "aiPosition": "Earlier you preferred keeping the central lane open.",
        "coreDisagreement": "Does this right-flank wall add helpful tension or pinch the path?",
        "nextQuestion": "Do you want to keep this narrower right-side turn?",
    }
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-v2-open",
        session_id=m1_p1_co,
        seq=4,
        request_id="seed-req-v2-rev",
        role="assistant",
        content="You added a wall at (8, 4) on the right flank.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:04:10Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-v2-rev",
        session_id=m1_p1_co,
        seq=5,
        request_id="seed-req-v2-rev",
        role="assistant",
        content="This narrows the right side slightly after keeping the center open.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:04:15Z",
        guidance={"disagreement": dis_card},
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-v2-ack-u",
        session_id=m1_p1_co,
        seq=6,
        request_id="seed-req-v2-ack",
        role="user",
        content="Yes, keep that right-flank choke wall so players pause before turning.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:05:00Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-v2-ack-a",
        session_id=m1_p1_co,
        seq=7,
        request_id="seed-req-v2-ack",
        role="assistant",
        content="Makes sense — keeping that right-flank pause point gives deliberate pacing.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:05:05Z",
    )
    _insert_audit(
        conn,
        session_id=m1_p1_co,
        event_type="human_edit_reviewed",
        payload={
            "versionId": "seed-demo-ver-m1-p1-v2",
            "openingTurnId": "seed-turn-m1-p1-v2-open",
            "reviewTurnId": "seed-turn-m1-p1-v2-rev",
            "disagreementId": dis_id,
        },
        created_at="2026-11-27T10:04:15Z",
    )
    _insert_audit(
        conn,
        session_id=m1_p1_co,
        event_type="disagreement_started",
        payload={
            "versionId": "seed-demo-ver-m1-p1-v2",
            "subject": "human_edit",
            "disagreementId": dis_id,
        },
        created_at="2026-11-27T10:04:15Z",
    )
    _insert_audit(
        conn,
        session_id=m1_p1_co,
        event_type="disagreement_acknowledged",
        payload={
            "versionId": "seed-demo-ver-m1-p1-v2",
            "subject": "human_edit",
            "disagreementId": dis_id,
            "turnId": "seed-turn-m1-p1-v2-ack-a",
        },
        created_at="2026-11-27T10:05:05Z",
    )

    topic_id = "seed-demo-topic-m1-p1"
    conn.execute(
        "INSERT INTO binary_discovery_topics(session_id, topic_id, state_json, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (
            m1_p1_co,
            topic_id,
            dump_json(
                {
                    "topicId": topic_id,
                    "versionId": "seed-demo-ver-m1-p1-v2",
                    "status": "proposal_ready",
                    "questionCount": 2,
                    "initialRequest": "Make the center feel cleaner before the final turn.",
                    "choices": [
                        {
                            "questionId": "q1",
                            "choiceId": "c1",
                            "label": "Simplify obstacle cluster",
                        },
                        {"questionId": "q2", "choiceId": "c2", "label": "Keep right flank wall"},
                    ],
                }
            ),
            "2026-11-27T10:08:00Z",
        ),
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-bin-u1",
        session_id=m1_p1_co,
        seq=8,
        request_id="seed-req-bin1",
        role="user",
        content="Make the center feel cleaner before the final turn.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:06:00Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-bin-a1",
        session_id=m1_p1_co,
        seq=9,
        request_id="seed-req-bin1",
        role="assistant",
        content="Which direction fits better?",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:06:05Z",
        guidance={
            "proposalDiscovery": {"topicId": topic_id, "status": "clarifying"},
            "proposalQuestion": {
                "topicId": topic_id,
                "questionId": "q1",
                "question": "Focus on simplifying center obstacles or adjusting outer border?",
                "choices": [
                    {"choiceId": "c1", "letter": "A", "label": "Simplify obstacle cluster"},
                    {"choiceId": "c2", "letter": "B", "label": "Widen lower lane"},
                ],
            },
        },
    )
    _insert_audit(
        conn,
        session_id=m1_p1_co,
        event_type="binary_choice_selected",
        payload={"topicId": topic_id, "questionId": "q1", "choiceId": "c1"},
        created_at="2026-11-27T10:06:40Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-bin-a2",
        session_id=m1_p1_co,
        seq=10,
        request_id="seed-req-bin2",
        role="assistant",
        content="Should we keep your Stage 2 right-flank wall?",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:07:00Z",
        guidance={
            "proposalDiscovery": {"topicId": topic_id, "status": "clarifying"},
            "proposalQuestion": {
                "topicId": topic_id,
                "questionId": "q2",
                "question": "Keep the right-flank wall or remove the center wall?",
                "choices": [
                    {"choiceId": "c1", "letter": "A", "label": "Remove top-center wall"},
                    {"choiceId": "c2", "letter": "B", "label": "Keep right flank wall"},
                ],
            },
        },
    )
    _insert_audit(
        conn,
        session_id=m1_p1_co,
        event_type="binary_choice_selected",
        payload={"topicId": topic_id, "questionId": "q2", "choiceId": "c2"},
        created_at="2026-11-27T10:07:30Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-offer1",
        session_id=m1_p1_co,
        seq=11,
        request_id="seed-req-bin3",
        role="assistant",
        content=(
            "Proposal ready: remove the upper wall at (5, 3) while keeping your right-flank wall."
        ),
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:08:00Z",
        guidance={
            "proposalDiscovery": {"topicId": topic_id, "status": "proposal_ready"},
            "proposalOffer": {
                "offerId": "seed-offer-1",
                "summary": "Remove internal wall at (5, 3) while preserving right flank.",
            },
        },
    )

    chg_id = "seed-demo-chg-m1-p1"
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-chg-u1",
        session_id=m1_p1_co,
        seq=12,
        request_id="seed-req-chg1",
        role="user",
        content="Challenge this plan",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:08:40Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-chg-a1",
        session_id=m1_p1_co,
        seq=13,
        request_id="seed-req-chg1",
        role="assistant",
        content="What would you prefer instead of removing the wall at (5, 3)?",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:08:45Z",
        guidance={"challengeCard": {"challengeId": chg_id, "status": "awaiting_reason"}},
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-chg-u2",
        session_id=m1_p1_co,
        seq=14,
        request_id="seed-req-chg2",
        role="user",
        content="I prefer to remove the central water tile instead of that wall.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:09:20Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-chg-a2",
        session_id=m1_p1_co,
        seq=15,
        request_id="seed-req-chg2",
        role="assistant",
        content="Removing the water tile clears vertical movement while keeping the (5, 3) anchor.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:09:25Z",
        guidance={
            "challengeCard": {
                "challengeId": chg_id,
                "status": "awaiting_player_choice",
                "relation": "different",
                "merit": "reasonable",
                "comparison": (
                    "Removing the center water tile preserves the upper obstacle anchor."
                ),
            }
        },
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-chg-u3",
        session_id=m1_p1_co,
        seq=16,
        request_id="seed-req-chg3",
        role="user",
        content="Use my reason and draft the water removal plan.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:10:00Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-chg-a3",
        session_id=m1_p1_co,
        seq=17,
        request_id="seed-req-chg3",
        role="assistant",
        content="Drafted replacement plan: remove center water tile at (5, 4).",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:10:05Z",
        guidance={
            "challengeCard": {"challengeId": chg_id, "selectedChoice": "revise_proposal"},
            "proposalOffer": {
                "offerId": "seed-offer-repl-1",
                "summary": "Remove central water tile at (5, 4).",
            },
        },
    )
    conn.execute(
        "INSERT INTO revision_challenges("
        "challenge_id, session_id, base_version_id, source_proposal_turn_id, challenge_turn_id, "
        "status, current_reason_turn_id, reviews_json, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chg_id,
            m1_p1_co,
            "seed-demo-ver-m1-p1-v2",
            "seed-turn-m1-p1-offer1",
            "seed-turn-m1-p1-chg-a1",
            "resolved",
            "seed-turn-m1-p1-chg-u2",
            dump_json([{"relation": "different", "merit": "reasonable"}]),
            "2026-11-27T10:08:45Z",
            "2026-11-27T10:10:05Z",
        ),
    )

    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-exec-u",
        session_id=m1_p1_co,
        seq=18,
        request_id="seed-req-exec",
        role="user",
        content="Draft this suggestion",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:10:40Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p1-exec-a",
        session_id=m1_p1_co,
        seq=19,
        request_id="seed-req-exec",
        role="assistant",
        content="Candidate map ready and verified solvable.",
        version_id="seed-demo-ver-m1-p1-v2",
        created_at="2026-11-27T10:10:45Z",
        proposal_binding={"sourceOfferTurnId": "seed-turn-m1-p1-chg-a3", "status": "consumed"},
    )
    conn.execute(
        "INSERT INTO change_proposals("
        "proposal_id, session_id, assistant_turn_id, base_version_id, candidate_version_id, "
        "status, proposal_json, decision_json, idempotency_key, created_at, decided_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "seed-demo-prop-m1-p1",
            m1_p1_co,
            "seed-turn-m1-p1-exec-a",
            "seed-demo-ver-m1-p1-v2",
            "seed-demo-ver-m1-p1-v3",
            "accepted",
            dump_json(
                {
                    "diff": diff_m1_p1_v3,
                    "validation": {
                        "solvable": True,
                        "minSteps": solver_m1_p1_v3["solutionSteps"],
                        "minPushes": solver_m1_p1_v3["solutionPushes"],
                    },
                }
            ),
            dump_json({"decision": "accept"}),
            "seed-idem-prop-m1-p1",
            "2026-11-27T10:10:45Z",
            "2026-11-27T10:11:30Z",
        ),
    )

    conn.execute(
        "INSERT INTO play_attempts("
        "attempt_id, session_id, version_id, mode, language, status, ticket_hash, "
        "ticket_expires_at, ticket_used_at, attempt_token_hash, issued_at, loaded_at, "
        "first_move_at, finished_at, duration_seconds, move_count, push_count, restart_count, "
        "minimum_moves, minimum_pushes, idempotency_key"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "seed-demo-pa-m1-p1-1",
            m1_p1_co,
            "seed-demo-ver-m1-p1-v3",
            "test_play",
            "en",
            "abandoned",
            _det_hash("pa1-ticket"),
            "2026-11-27T10:22:00Z",
            "2026-11-27T10:11:50Z",
            _det_hash("pa1-token"),
            "2026-11-27T10:11:45Z",
            "2026-11-27T10:11:51Z",
            "2026-11-27T10:11:53Z",
            "2026-11-27T10:11:59Z",
            6.2,
            4,
            1,
            1,
            int(solver_m1_p1_v3["solutionSteps"] or 6),
            int(solver_m1_p1_v3["solutionPushes"] or 3),
            "seed-idem-pa-1",
        ),
    )
    conn.execute(
        "INSERT INTO play_attempts("
        "attempt_id, session_id, version_id, mode, language, status, ticket_hash, "
        "ticket_expires_at, ticket_used_at, attempt_token_hash, issued_at, loaded_at, "
        "first_move_at, finished_at, duration_seconds, move_count, push_count, restart_count, "
        "minimum_moves, minimum_pushes, idempotency_key"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "seed-demo-pa-m1-p1-2",
            m1_p1_co,
            "seed-demo-ver-m1-p1-v3",
            "test_play",
            "en",
            "completed",
            _det_hash("pa2-ticket"),
            "2026-11-27T10:23:00Z",
            "2026-11-27T10:12:10Z",
            _det_hash("pa2-token"),
            "2026-11-27T10:12:05Z",
            "2026-11-27T10:12:11Z",
            "2026-11-27T10:12:13Z",
            "2026-11-27T10:12:25Z",
            12.4,
            7,
            3,
            0,
            int(solver_m1_p1_v3["solutionSteps"] or 6),
            int(solver_m1_p1_v3["solutionPushes"] or 3),
            "seed-idem-pa-2",
        ),
    )

    conn.execute(
        "INSERT INTO design_sessions("
        "session_id, match_id, player_slot, mode, language, language_locked, "
        "language_locked_at, status, secret_hash, access_token_hash, integration_token_hash, "
        "bootstrap_token_hash, bootstrap_used_at, creation_key, current_version_id, "
        "deadline_started_at, deadline_at, finalized_version_id, finalized_at, completed_at, "
        "final_intention_json, draft_rows_json, draft_validation_json, draft_generation, "
        "is_demo, created_at, updated_at, expires_at"
        ") VALUES ("
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        ")",
        (
            m1_p2_co,
            m1_id,
            2,
            "partial_completion",
            "en",
            1,
            "2026-11-27T10:01:45Z",
            "completed",
            _det_hash("m1-p2-secret"),
            _det_hash("m1-p2-access"),
            _det_hash("m1-p2-integ"),
            _det_hash("m1-p2-boot"),
            "2026-11-27T10:01:25Z",
            "seed-demo-create-m1-p2",
            "seed-demo-ver-m1-p2-v2",
            "2026-11-27T10:01:45Z",
            "2026-11-27T10:21:45Z",
            "seed-demo-ver-m1-p2-v2",
            "2026-11-27T10:12:00Z",
            "2026-11-27T10:12:30Z",
            dump_json({"content": p2_msg, "createdAt": "2026-11-27T10:12:30Z"}),
            dump_json(MAP_M1_P2_V1),
            dump_json(solver_m1_p2_v1),
            1,
            0,
            "2026-11-27T10:01:15Z",
            "2026-11-27T10:12:30Z",
            "2026-11-28T10:12:30Z",
        ),
    )
    _insert_version(
        conn,
        version_id="seed-demo-ver-m1-p2-v1",
        session_id=m1_p2_co,
        version_number=1,
        source="initial",
        summary="Stage 1 open baseline",
        rows=MAP_M1_P2_V1,
        diff=[],
        solver=solver_m1_p2_v1,
        design_context=None,
        assessment={"difficultyOpinion": "Clean initial puzzle."},
        parent_version_id=None,
        created_at="2026-11-27T10:01:45Z",
    )
    _insert_version(
        conn,
        version_id="seed-demo-ver-m1-p2-v2",
        session_id=m1_p2_co,
        version_number=2,
        source="human_edit",
        summary="Added right-side anchor wall",
        rows=MAP_M1_P2_V2,
        diff=diff_m1_p2_v2,
        solver=solver_m1_p2_v2,
        design_context=None,
        assessment={"difficultyOpinion": "Compact two-turn push."},
        parent_version_id="seed-demo-ver-m1-p2-v1",
        created_at="2026-11-27T10:06:00Z",
    )
    _insert_turn(
        conn,
        turn_id="seed-turn-m1-p2-open1",
        session_id=m1_p2_co,
        seq=1,
        request_id="seed-req-m1-p2-open1",
        role="assistant",
        content="Stage 1 starts with an open baseline lane solvable in 6 moves.",
        version_id="seed-demo-ver-m1-p2-v1",
        created_at="2026-11-27T10:02:00Z",
        guidance={
            "uiCues": [{"type": "clarification", "text": "Playtest to verify first push rhythm."}]
        },
    )

    # =========================================================================
    # SURVEY RECORDS (4 total for ROOM SEED01: P1 & P2 Pre-Match + Post-Match)
    # =========================================================================
    pre_specs: tuple[tuple[str, str, str, str, str, float, dict[str, str]], ...] = (
        (
            "seed-demo-resp-m1-p1-pre",
            m1_p1_sid,
            "G01-A",
            "2026-11-27T09:58:10Z",
            "zh",
            28.4,
            {
                "participant_code": "G01-A",
                "gender": "man",
                "age_range": "25_34",
                "sokoban_experience": "yes",
                "genai_experience": "daily",
                "strangers_to_other_group": "yes",
            },
        ),
        (
            "seed-demo-resp-m1-p2-pre",
            m1_p2_sid,
            "G01-B",
            "2026-11-27T09:58:40Z",
            "bilingual",
            34.1,
            {
                "participant_code": "G01-B",
                "gender": "woman",
                "age_range": "18_24",
                "sokoban_experience": "no",
                "genai_experience": "regularly",
                "strangers_to_other_group": "yes",
            },
        ),
    )
    for resp_id, sid, participant_id, ts, lang, dur, ans_kw in pre_specs:
        _insert_survey_record(
            conn,
            payload={
                "eventType": "survey-response",
                "surveyId": "online_pre_match_survey",
                "surveyVersion": SURVEY_VERSION,
                "surveyTitle": "匹配前简短问卷 / Pre-Match Short Questionnaire",
                "entrySource": "unity_matchmaking",
                "responseId": resp_id,
                "studySessionId": sid,
                "sessionId": sid,
                "matchId": "",
                "roomCode": "",
                "playerNumber": 0,
                "participantId": participant_id,
                "language": lang,
                "submittedAt": ts,
                "durationSeconds": dur,
                "answers": _build_pre_answers(**ans_kw),
            },
            created_at=ts,
        )

    post_specs: tuple[dict[str, Any], ...] = (
        {
            "responseId": "seed-demo-resp-m1-p1-post",
            "entrySource": "unity_matchmaking",
            "studySessionId": m1_p1_sid,
            "matchId": m1_id,
            "roomCode": m1_code,
            "playerNumber": 1,
            "participantId": "G01-A",
            "roundNumber": "round_1",
            "language": "bilingual",
            "submittedAt": "2026-11-27T10:21:10Z",
            "durationSeconds": 94.6,
            "relationship": "friend",
            "relScores": (5, 1, 5, 1, 5, 1),
            "coreScores": (5, 4, 2, 5, 5, 5, 5, 5, 5, 4, 5, 5, 5, 5, 5),
            "finalSelfReportedIntent": (
                "Removed center water so the right-flank choke guides a deliberate detour."
            ),
            "aiCollaborationReflection": (
                "Challenging the first wall removal helped me keep the clean right-flank anchor."
            ),
        },
        {
            "responseId": "seed-demo-resp-m1-p2-post",
            "entrySource": "unity_matchmaking",
            "studySessionId": m1_p2_sid,
            "matchId": m1_id,
            "roomCode": m1_code,
            "playerNumber": 2,
            "participantId": "G01-B",
            "roundNumber": "round_1",
            "language": "en",
            "submittedAt": "2026-11-27T10:22:05Z",
            "durationSeconds": 71.5,
            "relationship": "stranger",
            "relScores": None,
            "coreScores": (3, 2, 4, 2, 2, 2, 2, 3, 2, 3, 2, 3, 2, 3, 3),
            "finalSelfReportedIntent": (
                "Kept the opening clean with a right-side anchor wall for a crisp two-turn push."
            ),
            "aiCollaborationReflection": None,
        },
    )
    for spec in post_specs:
        _insert_survey_record(
            conn,
            payload={
                "eventType": "survey-response",
                "surveyId": "online_post_match_survey",
                "surveyVersion": SURVEY_VERSION,
                "surveyTitle": "赛后问卷 / Post-Game Questionnaire",
                "entrySource": spec["entrySource"],
                "responseId": spec["responseId"],
                "studySessionId": spec["studySessionId"],
                "sessionId": spec["studySessionId"],
                "matchId": spec["matchId"],
                "roomCode": spec["roomCode"],
                "playerNumber": spec["playerNumber"],
                "participantId": spec["participantId"],
                "language": spec["language"],
                "submittedAt": spec["submittedAt"],
                "durationSeconds": spec["durationSeconds"],
                "answers": _build_post_answers(
                    participant_code=spec["participantId"],
                    round_number=spec["roundNumber"],
                    relationship=spec["relationship"],
                    rel_scores=spec["relScores"],
                    core_scores=spec["coreScores"],
                    final_self_reported_intent=spec["finalSelfReportedIntent"],
                    ai_collaboration_reflection=spec["aiCollaborationReflection"],
                ),
            },
            created_at=spec["submittedAt"],
        )

    return {
        "online_rooms": 1,
        "online_room_events": len(m1_p1_events) + len(m1_p2_events),
        "design_sessions": 2,
        "study_records": len(pre_specs) + len(post_specs),
    }


def seed_demo_data(
    db_path: str | Path = DEFAULT_DATABASE_PATH, *, force: bool = False
) -> dict[str, int]:
    """Idempotently replace and insert Dashboard demo matches, sessions, and surveys."""
    target = _enforce_db_guard(db_path, force=force)
    db = SQLiteDatabase(target)
    db.initialize()
    with db.write_tx() as conn:
        _delete_seed_rows_in_conn(conn)
        return _seed_all_in_conn(conn)


def reset_demo_data(
    db_path: str | Path = DEFAULT_DATABASE_PATH, *, force: bool = False
) -> dict[str, Any]:
    """Clean existing seed-demo-* rows and re-seed identically."""
    target = _enforce_db_guard(db_path, force=force)
    db = SQLiteDatabase(target)
    db.initialize()
    with db.write_tx() as conn:
        cleaned = _delete_seed_rows_in_conn(conn)
        seeded = _seed_all_in_conn(conn)
        return {"cleaned": cleaned, "seeded": seeded}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.tools.seed_dashboard_demo",
        description="Local development CLI to seed, clean, or reset Dashboard demo data.",
    )
    parser.add_argument("action", choices=("seed", "clean", "reset"))
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DATABASE_PATH),
        help=f"Path to SQLite database (default: {DEFAULT_DATABASE_PATH})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow running against a /opt/sokoban path (disabled by default).",
    )
    args = parser.parse_args(argv)

    try:
        if args.action == "clean":
            summary = {
                "action": "clean",
                "db": args.db,
                "deleted": clean_seed_data(args.db, force=args.force),
            }
        elif args.action == "seed":
            summary = {
                "action": "seed",
                "db": args.db,
                "inserted": seed_demo_data(args.db, force=args.force),
            }
        else:
            res = reset_demo_data(args.db, force=args.force)
            summary = {"action": "reset", "db": args.db, **res}
    except ProductionGuardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(dump_json(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
