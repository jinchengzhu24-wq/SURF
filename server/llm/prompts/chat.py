"""Prompt builders for Stage openings and ordinary co-creation chat."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_CHAT_CORE_RULES = (
    "You are a thoughtful, equal Sokoban co-creation design partner speaking in warm, natural "
    "first-person voice. Respond strictly in the requested language and return ONLY JSON matching "
    "the required schema.\n"
    "Map authority: the supplied stageSnapshot is the ONLY authoritative source for current tile "
    "and entity positions (1-based row 1..10, col 1..12; legend # wall, . floor, @ water, "
    "p player, s box, t target). Never invent coordinates, entity adjacency, or play results. "
    "Never mention internal JSON keys (like stageSnapshot, solutionSteps, designContextPatch) in "
    "visible prose. There is only ONE level in this session; Stages are saved versions of it.\n"
    "Permissions: AI revisions may edit ONLY water (@) and internal walls (#). Player, boxes, "
    "targets, and connected outer shell are fixed. Never claim an edit was applied or saved.\n"
    "Difficulty & intent: frame difficulty as your personal perspective ('in my view' / "
    "'在我看来'), never as a solver fact.\n"
    "Coordinate links: emit coordinateLinks ONLY for concise movement clauses (<=220 chars) in "
    "assistantMessage connecting two distinct real map anchors with an explicit motion connector; "
    "text must be an exact verbatim substring of assistantMessage."
)

_STAGE_OPENING_RULES = (
    "Task: Stage opening observation (StageOpeningPayload). Write 2-3 concise declarative "
    "paragraphs in assistantMessage observing layout structure, spatial distribution, first-push "
    "dynamics, and your subjective difficulty view. Always populate assessment with non-empty "
    "solutionSummary, non-empty difficultyOpinion, features, and suggestions. "
    "For Stage 1, do NOT add workflow or editor instructions—the server appends fixed guidance."
)

_ORDINARY_CHAT_RULES_BASE = (
    "Task: Ordinary co-creation reply. Answer the designer's latest message directly first, then "
    "offer a grounded observation, trade-off, or perspective. Choose move from "
    "('clarify_intent', 'offer_perspective', 'challenge_tradeoff', 'reflect_on_play'). "
    "Always populate intentDecision with classification in ('none', 'candidate', "
    "'reference_only'): use 'candidate' ONLY with non-empty claims (each copying an exact "
    "verbatim evidenceSpan from the latest user message) and non-empty cardText; for 'none' or "
    "'reference_only', set claims=[] and cardText=''. Use uiCues (max 2) sparingly and only with "
    "concrete evidence for warnings. In designContextPatch.openQuestions, status='resolved' "
    "requires non-empty evidenceText. Never emit multiple competing proposal options in one reply."
)

_ORDINARY_CHAT_NO_DISAGREEMENT = (
    " Do not ask trailing follow-up questions in ordinary non-proposal chat."
)

_ORDINARY_CHAT_WITH_DISAGREEMENT = (
    " An active disagreement is present: populate followUpQuestion only when needed to resolve "
    "that active disagreement; otherwise keep followUpQuestion=null."
)


def _language_label(language: str | None) -> str:
    return "Simplified Chinese (zh-CN)" if normalize_language(language) == "zh-CN" else "English"


def build_stage_opening_messages(
    *,
    stage_snapshot: Mapping[str, Any],
    language: str = "en",
    stage_number: int = 1,
    solver_metrics: Mapping[str, Any] | None = None,
    design_context: Mapping[str, Any] | None = None,
    change_summary: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build structured Stage-opening messages with snapshot data in the user JSON payload."""
    system_content = (
        f"{_CHAT_CORE_RULES}\n\n{_STAGE_OPENING_RULES}\n\n"
        f"Response language: {_language_label(language)}."
    )
    payload = {
        "responseLanguage": normalize_language(language),
        "stageNumber": int(stage_number),
        "stageSnapshot": dict(stage_snapshot),
        "solverMetrics": dict(solver_metrics or {}),
        "designContext": dict(design_context or {}),
        "changeSummary": dict(change_summary) if change_summary else None,
    }
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_chat_messages(
    *,
    conversation: Sequence[Mapping[str, Any]],
    stage_snapshot: Mapping[str, Any],
    language: str = "en",
    solver_metrics: Mapping[str, Any] | None = None,
    play_summary: Mapping[str, Any] | None = None,
    design_context: Mapping[str, Any] | None = None,
    routing_hint: Mapping[str, Any] | None = None,
    has_active_disagreement: bool = False,
) -> list[dict[str, str]]:
    """Build structured ordinary-chat messages keeping all user text inside JSON/user turns."""
    disagreement_rule = (
        _ORDINARY_CHAT_WITH_DISAGREEMENT
        if has_active_disagreement
        else _ORDINARY_CHAT_NO_DISAGREEMENT
    )
    system_content = (
        f"{_CHAT_CORE_RULES}\n\n{_ORDINARY_CHAT_RULES_BASE}{disagreement_rule}\n\n"
        f"Response language: {_language_label(language)}."
    )
    recent_turns = [
        {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
        for item in list(conversation)[-24:]
    ]
    payload = {
        "responseLanguage": normalize_language(language),
        "stageSnapshot": dict(stage_snapshot),
        "solverMetrics": dict(solver_metrics or {}),
        "playSummary": dict(play_summary or {}),
        "designContext": dict(design_context or {}),
        "routingHint": dict(routing_hint) if routing_hint else None,
        "conversation": recent_turns,
    }
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
