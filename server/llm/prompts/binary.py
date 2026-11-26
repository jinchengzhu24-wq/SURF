"""Prompt builder for Policy-2 single-slot binary discovery (no Policy-1 fallback)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language


def _build_binary_discovery_system(
    *,
    needs_initial_requirements: bool,
    can_ask: bool,
) -> str:
    lines = [
        "You are the Policy-2 Sokoban co-creation binary discovery planner. Return ONLY JSON "
        "matching the active schema in the requested responseLanguage.",
        "Permissions: ONLY water (@) and internal walls (# in editableInternalWallCells) may be "
        "edited. Player, boxes, targets, and connected outer_shell are strictly FIXED by the "
        "platform. In strategy.preserve, list ONLY 'water' and/or 'walls' when needed (never "
        "include 'walls' while editing walls, or 'water' while editing water).",
        "Strategy fields: effect, focus, operators (unique subset of add_wall/remove_wall/"
        "add_water/remove_water that realizes effect), preserve ([] or ['water']/['walls']), "
        "and editBudget (1..12). Do NOT output metricGoals or extra keys.",
        "Choose action:",
        "- 'protected_refusal': ONLY when the designer explicitly requests moving/adding/removing/"
        "reshaping player, boxes, targets, or outer_shell. Set protectedEntities (1..4 unique), "
        "note='', planStrategies=[]"
        + (", question='', branches=[]" if can_ask else "")
        + (", initialRequirements=null" if needs_initial_requirements else "")
        + ". Do NOT confuse landmarks or preservation wishes ('keep boxes fixed') with protected "
        "edit requests.",
        "- 'plan': when prior selectedChoices already suffice or when turn 0 already specifies a "
        "concrete editable direction. Set note='', protectedEntities=[]"
        + (", question='', branches=[]" if can_ask else "")
        + ". "
        + (
            "Populate planStrategies with 1..3 executable strategies."
            if needs_initial_requirements
            else "Leave planStrategies=[] (or 1..3 strategies if overriding)."
        ),
    ]
    if can_ask:
        lines.append(
            "- 'ask': ONLY for an unresolved consequential choice on broad wishes. Provide ONE "
            "short non-blank question, optional 1-sentence note, EXACTLY two materially distinct "
            "branches (different label AND promise), planStrategies=[], protectedEntities=[]. "
            "Each branch has label (short natural action without letter prefixes), strategy, and "
            "tagged promise: "
            "reduce_obstacles (component in water/internal_walls/obstacles, optional focus), "
            "widen_corridor (component in water/internal_walls/obstacles, REQUIRED corridor with "
            "interior row 2..9, column 2..11, axis horizontal/vertical), "
            "reshape_water (component='water', optional focus), or "
            "shorter_solution (component='obstacles'). "
            "Prefer removing obstacles when compatible. When selectedChoices is non-empty, each "
            "branch strategy MUST satisfy ALL prior promises plus its new promise on the ORIGINAL "
            "Stage."
        )
    else:
        lines.append(
            "- Asking is disabled because questionCount >= 3 (question and branches are omitted "
            "from the schema); choose 'plan', 'protected_refusal', or 'blocked'."
        )
    lines.append(
        "- 'blocked': when no safe editable path exists; provide a concise non-blank note and "
        "keep all other action fields empty"
        + (" (initialRequirements=null)." if needs_initial_requirements else ".")
    )
    if needs_initial_requirements:
        lines.append(
            "initialRequirements rule: for action='ask' or action='plan', populate "
            "initialRequirements using ONLY initialDesignerTurns with verbatim sourceTurnId/"
            "evidenceSpan (NEVER include player/box/target/outer_shell change requirements); "
            "for action='protected_refusal' or action='blocked', set initialRequirements=null."
        )
    else:
        lines.append(
            "initialRequirements is already verified and omitted from the schema; do NOT include "
            "an initialRequirements key."
        )
    return "\n".join(lines)


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
    system_prompt = _build_binary_discovery_system(
        needs_initial_requirements=verified_initial_requirements is None,
        can_ask=int(question_count) < 3,
    )
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
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
