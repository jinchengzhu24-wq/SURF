"""Canonical V2 workflow and deterministic semantic checks for map revisions.

This module deliberately has no LLM or database dependency.  It compiles only
designer-authored language into a small, auditable constraint vocabulary and
evaluates every candidate against the same before/after map facts.
"""

from __future__ import annotations

import hashlib
import os
import re


WORKFLOW_SCHEMA_VERSION = 2
MODES = {"off", "shadow", "enforce"}
COMPONENT_TILES = {
    "water": {"@"},
    "wall": {"#"},
    "player": {"p", "+"},
    "box": {"s", "*"},
    "target": {"t", "+", "*"},
}
COMPONENT_PATTERNS = {
    "water": r"(?:水域|水块|水|water)",
    "wall": r"(?:墙体|墙|wall)",
    "player": r"(?:玩家|起点|player|start)",
    "box": r"(?:箱子|box|crate)",
    "target": r"(?:目标点|目标|target|goal)",
}


class SemanticConstraintError(ValueError):
    def __init__(self, results):
        self.results = list(results or [])
        failed = [item.get("constraintId") for item in self.results if not item.get("passed")]
        super().__init__("semantic contract failed: " + ", ".join(filter(None, failed)))


def modification_v2_mode(demo_mode=False):
    name = (
        "COCREATION_DEMO_MODIFICATION_V2_MODE"
        if demo_mode
        else "COCREATION_FORMAL_MODIFICATION_V2_MODE"
    )
    default = "enforce" if demo_mode else "shadow"
    value = str(os.getenv(name, default)).strip().lower()
    return value if value in MODES else default


def _component_mentions(text):
    return [
        component
        for component, pattern in COMPONENT_PATTERNS.items()
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]


def _constraint_id(kind, component, ordinal):
    return f"{kind}:{component or 'map'}:{ordinal}"


def compile_semantic_constraints(text):
    """Compile conservative, general constraints from explicit designer text.

    Unsupported or ambiguous prose remains a soft objective.  We never invent
    a coordinate, region, entity identity, or numeric threshold.
    """
    source = re.sub(r"\s+", " ", str(text or "")).strip()[:4000]
    lowered = source.casefold()
    components = _component_mentions(source)
    constraints = []

    minimum_match = re.search(
        r"(?:至少|不少于|minimum|at\s+least)\s*([1-9]|1[0-2])\s*(?:处|个格|格|cells?|changes?)?",
        source,
        flags=re.IGNORECASE,
    )
    too_small = bool(re.search(r"(?:一处|一个格|one\s+(?:cell|change)).{0,12}(?:太少|不够|too\s+(?:little|small)|not\s+enough)", source, re.I))
    if minimum_match or too_small:
        minimum = int(minimum_match.group(1)) if minimum_match else 2
        constraints.append({
            "constraintId": _constraint_id("change_scope", None, len(constraints) + 1),
            "kind": "change_scope",
            "hard": True,
            "minimumChangedCells": minimum,
            "sourceText": source,
        })

    relocation = bool(re.search(
        r"(?:重新分布|再分布|从.{0,30}(?:移到|挪到|改到|分布到)|"
        r"从.{0,24}(?:集中|分布).{0,24}改为|(?:集中|分布).{0,24}(?:改为|移到)|"
        r"移开|搬到|relocat|redistribut|move\s+from)",
        source,
        re.I,
    ))
    spread = bool(re.search(
        r"(?:不只|不再只|不仅|别只|分散|扩展到|spread|not\s+only|beyond|outside)",
        source,
        re.I,
    ))
    preserve_count = bool(re.search(r"(?:数量不变|保持数量|不增不减|same\s+count|preserve\s+(?:the\s+)?count)", source, re.I))

    for component in components:
        if relocation:
            constraints.append({
                "constraintId": _constraint_id("paired_transition", component, len(constraints) + 1),
                "kind": "paired_transition",
                "hard": True,
                "component": component,
                "requiresRemoval": True,
                "requiresAddition": True,
                "preserveCount": True,
                "sourceText": source,
            })
        if spread:
            constraints.append({
                "constraintId": _constraint_id("component_distribution", component, len(constraints) + 1),
                "kind": "component_distribution",
                "hard": True,
                "component": component,
                "mustExpandFootprint": True,
                "sourceText": source,
            })
        if preserve_count and not relocation:
            constraints.append({
                "constraintId": _constraint_id("component_count", component, len(constraints) + 1),
                "kind": "component_count",
                "hard": True,
                "component": component,
                "relation": "equal",
                "sourceText": source,
            })
        if re.search(rf"(?:增加|增多|add|increase).{{0,12}}{COMPONENT_PATTERNS[component]}|{COMPONENT_PATTERNS[component]}.{{0,12}}(?:增加|增多|add|increase)", source, re.I):
            constraints.append({
                "constraintId": _constraint_id("component_count", component, len(constraints) + 1),
                "kind": "component_count", "hard": True, "component": component,
                "relation": "increase", "sourceText": source,
            })
        if re.search(rf"(?:减少|删掉|移除|remove|decrease).{{0,12}}{COMPONENT_PATTERNS[component]}|{COMPONENT_PATTERNS[component]}.{{0,12}}(?:减少|删掉|移除|remove|decrease)", source, re.I):
            constraints.append({
                "constraintId": _constraint_id("component_count", component, len(constraints) + 1),
                "kind": "component_count", "hard": True, "component": component,
                "relation": "decrease", "sourceText": source,
            })

    preserve_matches = re.findall(
        r"(?:保留|保持|不要改|不改变|preserve|keep|do\s+not\s+change)\s*([^，。；;,.]{1,40})",
        source,
        flags=re.I,
    )
    for phrase in preserve_matches[:4]:
        named = _component_mentions(phrase)
        for component in named:
            constraints.append({
                "constraintId": _constraint_id("preservation", component, len(constraints) + 1),
                "kind": "preservation", "hard": True, "component": component,
                "sourceText": phrase.strip(),
            })

    spatial_relation = None
    if re.search(r"(?:靠近|更近|nearer|closer|near\b)", source, re.I):
        spatial_relation = "nearer"
    elif re.search(r"(?:远离|更远|farther|further|away\s+from)", source, re.I):
        spatial_relation = "farther"
    if spatial_relation and len(components) >= 2:
        constraints.append({
            "constraintId": _constraint_id("spatial_relation", components[0], len(constraints) + 1),
            "kind": "spatial_relation",
            "hard": True,
            "component": components[0],
            "anchorComponent": components[1],
            "relation": spatial_relation,
            "sourceText": source,
        })

    # Exact duplicates can arise from bilingual or repeated wording.
    unique = []
    signatures = set()
    for item in constraints:
        signature = tuple((key, str(item.get(key))) for key in sorted(item) if key not in {"constraintId", "sourceText"})
        if signature in signatures:
            continue
        signatures.add(signature)
        item["constraintId"] = _constraint_id(item["kind"], item.get("component"), len(unique) + 1)
        unique.append(item)
    return unique


def build_revision_workflow(authorized_brief, stage_context=None):
    context = dict(stage_context or {})
    brief = re.sub(r"\s+", " ", str(authorized_brief or "")).strip()[:4000]
    base_version_id = str(context.get("versionId") or context.get("currentVersionId") or "current")
    source_turn_ids = [str(value) for value in context.get("revisionSourceTurnIds") or [] if value]
    digest = hashlib.sha256(
        (base_version_id + "\n" + brief + "\n" + "|".join(source_turn_ids)).encode("utf-8")
    ).hexdigest()[:24]
    constraints = compile_semantic_constraints(brief)
    minimum = max(
        [1] + [
            int(item.get("minimumChangedCells") or 1)
            for item in constraints if item.get("kind") == "change_scope"
        ]
    )
    return {
        "schemaVersion": WORKFLOW_SCHEMA_VERSION,
        "revisionWorkflowId": "rw-" + digest,
        "baseVersionId": base_version_id,
        "status": "authorized",
        "objective": brief[:1200],
        "targetBindings": [],
        "semanticConstraints": constraints,
        "softObjectives": [],
        "preserve": [],
        "scope": {"minimumChangedCells": minimum, "maximumChangedCells": 12},
        "sourceTurnIds": source_turn_ids,
        "mode": modification_v2_mode(bool(context.get("demoMode"))),
    }


def _positions(rows, component):
    tiles = COMPONENT_TILES.get(component, set())
    return {
        (row_index + 1, column_index + 1)
        for row_index, row in enumerate(rows or [])
        for column_index, tile in enumerate(row)
        if tile in tiles
    }


def _span(positions):
    if not positions:
        return (0, 0, 0)
    rows = [item[0] for item in positions]
    columns = [item[1] for item in positions]
    return (max(rows) - min(rows), max(columns) - min(columns), len(positions))


def _minimum_distance(left, right):
    if not left or not right:
        return None
    return min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in left for b in right)


def evaluate_semantic_constraints(base_rows, candidate_rows, workflow):
    constraints = list((workflow or {}).get("semanticConstraints") or [])
    changed = [
        (r + 1, c + 1)
        for r, (before, after) in enumerate(zip(base_rows or [], candidate_rows or []))
        for c, (left, right) in enumerate(zip(before, after))
        if left != right
    ]
    results = []
    for constraint in constraints:
        kind = constraint.get("kind")
        component = constraint.get("component")
        before = _positions(base_rows, component) if component else set()
        after = _positions(candidate_rows, component) if component else set()
        removed = sorted(before - after)
        added = sorted(after - before)
        passed = True
        reason = "satisfied"
        if kind == "change_scope":
            minimum = int(constraint.get("minimumChangedCells") or 1)
            passed = len(changed) >= minimum
            reason = "changed_cell_minimum" if not passed else reason
        elif kind == "paired_transition":
            passed = (not constraint.get("requiresRemoval") or bool(removed)) and (
                not constraint.get("requiresAddition") or bool(added)
            )
            if constraint.get("preserveCount"):
                passed = passed and len(before) == len(after)
            reason = "paired_source_and_destination_required" if not passed else reason
        elif kind == "component_distribution":
            before_span = _span(before)
            after_span = _span(after)
            passed = bool(added) and (
                after_span[0] > before_span[0] or after_span[1] > before_span[1]
            )
            reason = "distribution_footprint_did_not_expand" if not passed else reason
        elif kind == "component_count":
            relation = constraint.get("relation")
            passed = (
                (relation == "equal" and len(after) == len(before))
                or (relation == "increase" and len(after) > len(before))
                or (relation == "decrease" and len(after) < len(before))
            )
            reason = "component_count_relation_failed" if not passed else reason
        elif kind == "preservation":
            passed = before == after
            reason = "preserved_component_changed" if not passed else reason
        elif kind == "spatial_relation":
            anchor = constraint.get("anchorComponent")
            anchor_before = _positions(base_rows, anchor)
            anchor_after = _positions(candidate_rows, anchor)
            before_distance = _minimum_distance(before, anchor_before)
            after_distance = _minimum_distance(after, anchor_after)
            relation = constraint.get("relation")
            passed = (
                before_distance is not None
                and after_distance is not None
                and (
                    (relation == "nearer" and after_distance < before_distance)
                    or (relation == "farther" and after_distance > before_distance)
                )
            )
            reason = "spatial_relation_did_not_change_as_requested" if not passed else reason
        results.append({
            "constraintId": constraint.get("constraintId"),
            "kind": kind,
            "component": component,
            "passed": bool(passed),
            "reason": reason,
            "changedCellCount": len(changed),
            "removed": removed[:12],
            "added": added[:12],
        })
    return results


def validate_semantic_constraints(base_rows, candidate_rows, workflow):
    results = evaluate_semantic_constraints(base_rows, candidate_rows, workflow)
    if (workflow or {}).get("mode") == "enforce" and any(
        item.get("passed") is False for item in results
    ):
        raise SemanticConstraintError(results)
    return results
