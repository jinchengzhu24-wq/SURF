"""Pure requirement evaluation against before/after grids and solver/feature facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import REQ_METRICS, REQ_TILES, RequirementRecord


def component_positions(rows: Sequence[str], component: str) -> set[tuple[int, int]]:
    tiles = REQ_TILES.get(component, set())
    return {
        (r + 1, c + 1)
        for r, row in enumerate(rows)
        for c, tile in enumerate(row)
        if tile in tiles
    }


def evaluate_requirements(
    base_rows: Sequence[str],
    candidate_rows: Sequence[str],
    record: Mapping[str, Any] | RequirementRecord | None,
    before_features: Mapping[str, Any] | None = None,
    after_features: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pure requirement evaluation against before/after grids and solver/feature facts."""
    rec = (
        record.model_dump(by_alias=True)
        if isinstance(record, RequirementRecord)
        else (record or {})
    )
    before_f = before_features or {}
    after_f = after_features or {}
    results: list[dict[str, Any]] = []

    for item in rec.get("requirements") or ():
        prop, relation = item["property"], item["relation"]
        entities = item["entities"] or [
            x["entity"] for x in rec.get("automaticBindings") or ()
        ]
        before = component_positions(base_rows, item["component"])
        after = component_positions(candidate_rows, item["component"])
        value_before: Any = None
        value_after: Any = None
        if prop == "count":
            value_before = len(before)
            value_after = (
                len(before & after)
                if item.get("scope") == "original_cells"
                else len(after)
            )
        elif prop in REQ_METRICS:
            value_before, value_after = before_f.get(prop), after_f.get(prop)
        elif prop == "change_scope":
            value_before = 0
            value_after = sum(
                a != b
                for left, right in zip(base_rows, candidate_rows, strict=True)
                for a, b in zip(left, right, strict=True)
            )
        elif prop == "positions":
            value_before, value_after = before, after
        elif prop == "distribution":
            def _extent(cells: set[tuple[int, int]]) -> int:
                return sum(max(v) - min(v) for v in zip(*cells, strict=True)) if cells else 0

            value_before, value_after = _extent(before), _extent(after)
        elif prop == "distance" and entities:
            anchors = before_f.get("entityPositions") or {}
            point = anchors.get(entities[0])
            if point and before and after:
                value_before = min(abs(r - point[0]) + abs(c - point[1]) for r, c in before)
                value_after = min(abs(r - point[0]) + abs(c - point[1]) for r, c in after)
        elif prop == "timing" and entities:
            runs_before = before_f.get("firstRuns") or {}
            runs_after = after_f.get("firstRuns") or {}
            value_before, value_after = runs_before.get(entities[0]), runs_after.get(entities[0])

        status, passed, reason = "deferred", True, "not_verified"
        if value_before is not None and value_after is not None:
            if relation in {"preserve", "equal"}:
                expected = (
                    item["value"]
                    if item["value"] is not None and item["unit"] == "absolute"
                    else value_before
                )
                passed = value_after == expected
                status, reason = (
                    ("fulfilled", "verified")
                    if passed
                    else ("deferred", "locked_requirement_changed")
                )
                if (
                    item["strength"] == "goal"
                    and relation == "equal"
                    and isinstance(expected, (int, float))
                ):
                    d_before, d_after = abs(value_before - expected), abs(value_after - expected)
                    passed = d_after <= d_before
                    status = (
                        "fulfilled"
                        if d_after == 0
                        else "partial"
                        if d_after < d_before
                        else "deferred"
                    )
                    reason = (
                        "verified"
                        if status == "fulfilled"
                        else "opposite_direction"
                        if not passed
                        else "unchanged"
                        if d_after == d_before
                        else "below_requested_amount"
                    )
            elif prop == "positions" and relation == "relocate":
                status = "fulfilled" if value_after != value_before else "deferred"
                reason = "verified" if status == "fulfilled" else "unchanged"
            elif isinstance(value_before, (int, float)) and isinstance(value_after, (int, float)):
                sign = -1 if relation in {"decrease", "nearer", "early"} else 1
                delta = sign * (value_after - value_before)
                target = item["value"]
                if item["unit"] == "absolute" and target is not None:
                    satisfied = value_after >= target if sign == 1 else value_after <= target
                    if prop == "change_scope":
                        passed = satisfied if sign == -1 else delta >= 0
                        status = (
                            "fulfilled"
                            if satisfied
                            else "partial"
                            if sign == 1 and value_after > 0
                            else "deferred"
                        )
                        reason = (
                            "verified"
                            if satisfied
                            else "below_requested_amount"
                            if sign == 1 and value_after > 0
                            else "opposite_direction"
                        )
                    else:
                        d_before = (
                            max(0, target - value_before)
                            if sign == 1
                            else max(0, value_before - target)
                        )
                        d_after = (
                            max(0, target - value_after)
                            if sign == 1
                            else max(0, value_after - target)
                        )
                        passed = satisfied or d_after <= d_before
                        status = (
                            "fulfilled"
                            if satisfied
                            else "partial"
                            if d_after < d_before
                            else "deferred"
                        )
                        reason = (
                            "verified"
                            if satisfied
                            else "opposite_direction"
                            if not passed
                            else "unchanged"
                            if value_after == value_before
                            else "below_requested_amount"
                        )
                else:
                    passed = delta >= 0
                    satisfied = delta >= (target if target is not None else 1)
                    status = (
                        "fulfilled"
                        if passed and satisfied
                        else "partial"
                        if delta > 0
                        else "deferred"
                    )
                    reason = (
                        "opposite_direction"
                        if not passed
                        else "verified"
                        if satisfied
                        else "unchanged"
                        if delta == 0
                        else "below_requested_amount"
                    )
        elif prop in REQ_METRICS | {"distance", "timing"} or item["strength"] == "invariant":
            passed, reason = False, "required_fact_unavailable"

        if prop == "timing" and entities and "interruptions" in after_f:
            interrupted_before = (before_f.get("interruptions") or {}).get(entities[0], False)
            interrupted_after = (after_f.get("interruptions") or {}).get(entities[0], False)
            if not interrupted_after:
                status, passed = "deferred", not interrupted_before
                reason = (
                    "interruption_removed"
                    if interrupted_before
                    else "interruption_not_verified"
                )

        if item["strength"] == "invariant" and status != "fulfilled":
            passed = False
        elif item["strength"] == "preference":
            passed = True

        results.append(
            {
                "requirementId": item["requirementId"],
                "statement": item["statement"],
                "strength": item["strength"],
                "property": prop,
                "status": status,
                "passed": bool(passed),
                "reason": reason,
                "before": sorted(value_before) if isinstance(value_before, set) else value_before,
                "after": sorted(value_after) if isinstance(value_after, set) else value_after,
                "prioritized": bool(item["priorityEvidenceSpan"]),
                "focused": item["focused"],
            }
        )

    for ordinal, item in enumerate(rec.get("exactTransitions") or ()):
        r, c = item["row"] - 1, item["column"] - 1
        valid = 0 <= r < len(base_rows) and 0 <= c < len(base_rows[r])
        passed = valid and base_rows[r][c] == item["from"] and candidate_rows[r][c] == item["to"]
        results.append(
            {
                "requirementId": f"exact:{ordinal}",
                "statement": f"({r + 1}, {c + 1}): {item['from']} → {item['to']}",
                "strength": "invariant",
                "property": "exactTransition",
                "status": "fulfilled" if passed else "deferred",
                "passed": bool(passed),
                "reason": "verified" if passed else "exact_transition_changed",
                "before": item["from"],
                "after": item["to"],
                "prioritized": False,
                "focused": False,
            }
        )
    return results
