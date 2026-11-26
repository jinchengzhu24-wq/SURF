"""Prompt builder for Policy-2 single-slot binary discovery (no Policy-1 fallback)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_BINARY_DISCOVERY_SYSTEM = (
    "You are the Policy-2 Sokoban co-creation binary discovery planner. Return ONLY JSON matching "
    "BinaryDiscoveryPayload in the requested responseLanguage.\n"
    "Permissions: ONLY water (@) and internal walls (# in editableInternalWallCells) may be "
    "edited. Player, boxes, targets, and connected outer_shell are strictly FIXED.\n"
    "Choose action:\n"
    "- 'protected_refusal': ONLY when the designer explicitly requests moving/adding/removing/"
    "reshaping player, boxes, targets, or outer_shell. Set protectedEntities (1..4), question='', "
    "note='', branches=[], planStrategies=[], initialRequirements=null. Do NOT confuse landmarks "
    "or preservation wishes ('keep boxes fixed') with protected edit requests.\n"
    "- 'plan': when prior selectedChoices already suffice or when turn 0 already specifies a "
    "concrete editable direction. Set question='', note='', branches=[], protectedEntities=[]. "
    "On turn 0, populate planStrategies with 1..3 executable strategies (metricGoals=[]).\n"
    "- 'ask': ONLY for an unresolved consequential choice on broad wishes when questionCount < 3. "
    "Provide ONE short question, optional 1-sentence note, EXACTLY two materially distinct "
    "branches (different label AND promise), planStrategies=[], protectedEntities=[]. "
    "Each branch has label (short natural action without letter prefixes), strategy (operators "
    "from add_wall/remove_wall/add_water/remove_water, editBudget 1..12, metricGoals=[]), and "
    "promise (kind in reduce_obstacles/widen_corridor/shorter_solution/reshape_water; component "
    "in water/internal_walls/obstacles; corridor non-null ONLY for widen_corridor). "
    "Prefer removing obstacles when compatible. Never include 'walls' in preserve when editing "
    "walls, or 'water' when editing water. When selectedChoices is non-empty, each branch strategy "
    "MUST satisfy ALL prior promises plus its new promise on the ORIGINAL Stage.\n"
    "- 'blocked': when questionCount >= 3 or no safe editable path exists; provide brief note.\n"
    "initialRequirements rule: when verifiedInitialRequirements is null and action != "
    "'protected_refusal', populate initialRequirements using ONLY initialDesignerTurns with "
    "verbatim sourceTurnId/evidenceSpan (NEVER include player/box/target/outer_shell items); "
    "when verifiedInitialRequirements is already set, set initialRequirements=null."
)


def build_binary_discovery_messages(
    *,
    stage_snapshot: Mapping[str, Any],
    initial_request: str,
    initial_designer_turns: Sequence[Mapping[str, Any]],
    question_count: int,
    selected_choices: Sequence[Mapping[str, Any]] = (),
    verified_initial_requirements: Mapping[str, Any] | None = None,
    design_context: Mapping[str, Any] | None = None,
    language: str = "en",
) -> list[dict[str, str]]:
    """Build Policy-2 binary discovery messages with all user/topic data in JSON."""
    locale = normalize_language(language)
    payload = {
        "responseLanguage": locale,
        "stageSnapshot": dict(stage_snapshot),
        "topic": {
            "initialRequest": str(initial_request),
            "questionCount": int(question_count),
            "selectedChoices": [dict(c) for c in selected_choices],
        },
        "verifiedInitialRequirements": (
            dict(verified_initial_requirements)
            if verified_initial_requirements is not None
            else None
        ),
        "initialDesignerTurns": [dict(t) for t in list(initial_designer_turns)[:1]],
        "designContext": dict(design_context or {}),
    }
    return [
        {"role": "system", "content": _BINARY_DISCOVERY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
