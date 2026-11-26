"""Prompt builders for proposal requirements, revision planning, map operations, and failures."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import normalize_language

_REQUIREMENTS_SYSTEM = (
    "Compile the designer's authorized proposal topic into RequirementRecordPayload JSON ONLY.\n"
    "Rules: Every requirement MUST cite a real sourceTurnId and exact verbatim substring "
    "evidenceSpan from designerTurns. NEVER emit requirements for platform-protected entities "
    "(outer_shell, player, boxes, targets)—the server enforces those automatically. "
    "Use strength='invariant' ONLY for explicit prohibitions, fixed counts, or explicit edit "
    "scope; directional wishes are strength='goal'. Use value=null and unit='none' unless the "
    "designer stated an explicit number. Keep scope='all' unless component is water/wall and "
    "property is count. exactTransitions must be empty unless the designer explicitly typed "
    "1-based row/column coordinates and tile changes. automaticBindings accepts ONLY snapshot "
    "entity IDs (P, B1, B2, T1, T2) when an unspecified goal needs an anchor."
)

_REVISION_PLAN_SYSTEM = (
    "Compile an authorized RevisionPlanPayload JSON ONLY with 1 to 3 materially different local "
    "strategies for the 10x12 Sokoban Stage. Do NOT output map rows or cell operations here.\n"
    "Allowed effects: open_route, narrow_route, adjust_internal_walls, reshape_water.\n"
    "Allowed operators ONLY: add_wall, remove_wall, add_water, remove_water. Player, boxes, "
    "targets, and connected outer_shell are strictly fixed. Only cells in "
    "editableInternalWallCells may be removed with remove_wall. Never put 'walls' in preserve "
    "when using wall operators, or 'water' when using water operators. Keep editBudget in 1..12."
)

_MAP_OPERATIONS_SYSTEM = (
    "Realize the supplied RevisionPlan strategies as concrete cell-level operations on the "
    "10x12 Sokoban Stage. Return ONLY MapOperationsPayload JSON with 1 to 3 candidates.\n"
    "STRICT PERMISSIONS: Each operation MUST use op in ('add_wall', 'remove_wall', 'add_water', "
    "'remove_water') with exact 1-based row (1..10), column (1..12), matching current 'from' tile "
    "from stageSnapshot, and target 'to' tile ('.', '#', or '@'). NEVER touch player (p), boxes "
    "(s), targets (t), void (' '), or any outer_shell wall. Only remove walls listed in "
    "editableInternalWallCells. Keep each candidate within its strategy's editBudget (<=12)."
)

_FAILURE_ANALYSIS_SYSTEM = (
    "Explain why the bounded proposal search found no candidate passing every check. Return ONLY "
    "ProposalFailureAnalysisPayload JSON ({'body': '...'}) in the requested language.\n"
    "State the verified failure reason truthfully and suggest one conceptual next direction. "
    "Never claim any map change was applied or saved, never expose internal field names, and "
    "never claim the designer's goals are permanently impossible."
)


def build_requirements_messages(
    *,
    designer_turns: Sequence[Mapping[str, Any]],
    stage_snapshot: Mapping[str, Any],
    language: str = "en",
    selected_choices: Sequence[Mapping[str, Any]] = (),
    design_context: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    payload = {
        "responseLanguage": normalize_language(language),
        "designerTurns": [dict(t) for t in designer_turns],
        "selectedInterfaceChoices": [dict(c) for c in selected_choices],
        "stageSnapshot": dict(stage_snapshot),
        "designContext": dict(design_context or {}),
        "editableComponents": ["water", "internal_walls"],
    }
    return [
        {"role": "system", "content": _REQUIREMENTS_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_revision_plan_messages(
    *,
    stage_snapshot: Mapping[str, Any],
    authorized_brief: str,
    requirements: Mapping[str, Any] | None = None,
    language: str = "en",
    design_context: Mapping[str, Any] | None = None,
    preserved_components: Sequence[str] = (),
    excluded_hint: str = "",
) -> list[dict[str, str]]:
    payload = {
        "responseLanguage": normalize_language(language),
        "authorizedBrief": str(authorized_brief),
        "requirements": dict(requirements) if requirements else None,
        "preservedComponents": list(preserved_components),
        "excludedHint": str(excluded_hint),
        "stageSnapshot": dict(stage_snapshot),
        "designContext": dict(design_context or {}),
    }
    return [
        {"role": "system", "content": _REVISION_PLAN_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_map_operations_messages(
    *,
    stage_snapshot: Mapping[str, Any],
    revision_plan: Mapping[str, Any],
    baseline_metrics: Mapping[str, Any] | None = None,
    requirements: Mapping[str, Any] | None = None,
    language: str = "en",
) -> list[dict[str, str]]:
    payload = {
        "responseLanguage": normalize_language(language),
        "stageSnapshot": dict(stage_snapshot),
        "revisionPlan": dict(revision_plan),
        "baselineMetrics": dict(baseline_metrics or {}),
        "requirements": dict(requirements) if requirements else None,
    }
    return [
        {"role": "system", "content": _MAP_OPERATIONS_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_failure_analysis_messages(
    *,
    stage_snapshot: Mapping[str, Any],
    designer_turns: Sequence[Mapping[str, Any]],
    failure_code: str,
    failure_summary: str,
    requirements: Mapping[str, Any] | None = None,
    language: str = "en",
) -> list[dict[str, str]]:
    payload = {
        "responseLanguage": normalize_language(language),
        "failureCode": str(failure_code),
        "verifiedFailure": str(failure_summary),
        "designerTurns": [dict(t) for t in designer_turns],
        "requirements": dict(requirements) if requirements else None,
        "stageSnapshot": dict(stage_snapshot),
    }
    return [
        {"role": "system", "content": _FAILURE_ANALYSIS_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
