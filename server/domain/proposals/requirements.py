"""Evidence-bound requirement validation, preflight checks, ranking, and notices."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from server.i18n.messages import format_tradeoff_notice

from .models import (
    _CHINESE_DIGITS,
    POLICY_VERSION,
    PROPERTY_RELATIONS,
    REQ_COMPONENTS,
    REQ_METRICS,
    REQ_PROPERTIES,
    REQ_RELATIONS,
    REQ_TILES,
    RequirementRecord,
)
from .plan_parser import connected_outer_shell
from .requirement_eval import component_positions, evaluate_requirements

_PLATFORM_LOCK_PHRASE = re.compile(
    r"(?:保持.{0,12}(?:不变|固定|原位|原地)|不要(?:移动|挪动|改动|改变|动)|不可(?:移动|改动)|(?:位置|数量)不变|"
    r"keep.{0,24}(?:fixed|unchanged|intact)|do\s+not\s+move|don't\s+move)",
    re.I,
)
_GENERIC_ENTITY_LABELS: frozenset[str] = frozenset(
    {
        "PLAYER",
        "BOX",
        "BOXES",
        "TARGET",
        "TARGETS",
        "WALL",
        "WALLS",
        "WATER",
        "OUTER_SHELL",
        "GAMEPLAY",
    }
)


def normalize_requirement_record(
    payload: Any,
    user_turns: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
) -> tuple[Any, list[str]]:
    """Prune redundant platform-protected invariants and normalize benign schema quirks."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("requirements"), list):
        return payload, []
    sources = {str(item["id"]): str(item["content"]) for item in user_turns or ()}
    labels = {
        str(item.get("id") or item.get("label") or "").upper()
        for item in (snapshot or {}).get("entities") or ()
    }
    default_turn_id = str(user_turns[0]["id"]) if user_turns else ""
    cleaned: list[Any] = []
    audit_notes: list[str] = []

    for raw in payload["requirements"]:
        if not isinstance(raw, Mapping):
            cleaned.append(raw)
            continue
        item = dict(raw)
        is_protected_component = item.get("component") in {"player", "box", "target"} and (
            item.get("property") in {"count", "positions"}
        )
        has_lock_signal = (
            item.get("relation") in {"preserve", "equal", "invariant"}
            or item.get("strength") in {"hard", "invariant"}
            or (
                item.get("value") is None
                and any(
                    _PLATFORM_LOCK_PHRASE.search(str(item.get(field) or ""))
                    for field in ("evidenceSpan", "statement")
                )
            )
        )
        if is_protected_component and has_lock_signal:
            audit_notes.append("dropped_platform_protection")
            continue
        if "entities" not in item or item.get("entities") is None:
            item["entities"] = []
            audit_notes.append("defaulted_entities")
        if isinstance(item.get("entities"), list):
            kept_entities = []
            for entity in item["entities"]:
                if (
                    isinstance(entity, str)
                    and entity.upper() in _GENERIC_ENTITY_LABELS
                    and entity.upper() not in labels
                ):
                    audit_notes.append("dropped_generic_entity_label")
                    continue
                kept_entities.append(entity)
            item["entities"] = kept_entities
        if "focused" not in item or not isinstance(item.get("focused"), bool):
            item["focused"] = False
            audit_notes.append("defaulted_focused")
        if "priorityEvidenceSpan" not in item or item.get("priorityEvidenceSpan") is None:
            item["priorityEvidenceSpan"] = ""
            audit_notes.append("defaulted_priority_span")
        if "scope" not in item or item.get("scope") is None:
            item["scope"] = "all"
        if item.get("scope") == "original_cells" and (
            item.get("property") != "count" or item.get("component") not in {"water", "wall"}
        ):
            item["scope"] = "all"
            audit_notes.append("coerced_scope_all")
        if item.get("sourceTurnId") not in sources and len(sources) == 1 and default_turn_id:
            item["sourceTurnId"] = default_turn_id
            audit_notes.append("bound_single_source_turn")
        source_text = sources.get(str(item.get("sourceTurnId") or ""))
        priority_span = item.get("priorityEvidenceSpan")
        if source_text and item.get("value") is None:
            span = item.get("evidenceSpan")
            if not isinstance(span, str) or not span.strip() or span not in source_text:
                if (
                    isinstance(priority_span, str)
                    and priority_span.strip()
                    and priority_span in source_text
                ):
                    item["evidenceSpan"] = priority_span
                else:
                    item["evidenceSpan"] = source_text
                audit_notes.append("normalized_qualitative_evidence_span")
            if not isinstance(item.get("statement"), str) or not item["statement"].strip():
                item["statement"] = str(item.get("evidenceSpan") or source_text).strip()[:400]
                audit_notes.append("defaulted_statement")
        if (
            source_text
            and isinstance(priority_span, str)
            and priority_span
            and priority_span not in source_text
        ):
            item["priorityEvidenceSpan"] = ""
            audit_notes.append("cleared_nonverbatim_priority_span")
        cleaned.append(item)

    raw_bindings = payload.get("automaticBindings")
    cleaned_bindings = raw_bindings
    if isinstance(raw_bindings, list):
        filtered_bindings = []
        for binding in raw_bindings:
            if (
                isinstance(binding, Mapping)
                and isinstance(binding.get("entity"), str)
                and binding["entity"].upper() in _GENERIC_ENTITY_LABELS
                and binding["entity"].upper() not in labels
            ):
                audit_notes.append("dropped_generic_entity_label")
                continue
            filtered_bindings.append(binding)
        if filtered_bindings and not any(
            not (isinstance(r, Mapping) and r.get("entities")) for r in cleaned
        ):
            filtered_bindings = []
            audit_notes.append("dropped_unbound_automatic_bindings")
        cleaned_bindings = filtered_bindings

    return {
        **dict(payload),
        "requirements": cleaned,
        "automaticBindings": cleaned_bindings,
    }, list(dict.fromkeys(audit_notes))


def validate_requirement_record(
    payload: Any,
    user_turns: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate an evidence-bound requirement envelope strictly."""
    if not isinstance(payload, Mapping) or set(payload) != {
        "requirements",
        "automaticBindings",
        "exactTransitions",
    }:
        raise ValueError("Requirements need an evidence-bound envelope.")
    sources = {str(item["id"]): str(item["content"]) for item in user_turns}
    labels = {
        str(item.get("id") or item.get("label") or "").upper()
        for item in (snapshot or {}).get("entities") or ()
    }
    raw_requirements = payload["requirements"]
    if not isinstance(raw_requirements, list) or len(raw_requirements) > 48:
        raise ValueError("Invalid requirements collection.")

    required_fields = {
        "component",
        "property",
        "relation",
        "strength",
        "entities",
        "value",
        "unit",
        "priorityEvidenceSpan",
        "focused",
        "sourceTurnId",
        "evidenceSpan",
        "statement",
    }
    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_requirements:
        if (
            not isinstance(raw, Mapping)
            or not required_fields.issubset(raw)
            or set(raw) - required_fields - {"scope", "sourceTurnIds"}
        ):
            raise ValueError("Invalid typed requirement.")
        item = dict(raw)
        scope = item.get("scope", "all")
        if scope not in {"all", "original_cells"}:
            raise ValueError("Invalid requirement scope.")
        if scope == "original_cells" and (
            item["property"] != "count" or item["component"] not in {"water", "wall"}
        ):
            raise ValueError("Original-cell scope is only supported for editable component counts.")
        source_id = item.get("sourceTurnId")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        span = item["evidenceSpan"]
        if (
            source is None
            or not isinstance(span, str)
            or not span.strip()
            or span not in source
        ):
            raise ValueError("Requirement lacks exact designer evidence.")
        if "sourceTurnIds" in item:
            raw_turn_ids = item["sourceTurnIds"]
            if (
                not isinstance(raw_turn_ids, list)
                or not raw_turn_ids
                or any(not isinstance(tid, str) or tid not in sources for tid in raw_turn_ids)
                or source_id not in raw_turn_ids
            ):
                raise ValueError(
                    "Requirement sourceTurnIds must reference existing designer turns."
                )
            deduped_ids = list(dict.fromkeys(raw_turn_ids))
            if len(deduped_ids) > 1:
                item["sourceTurnIds"] = deduped_ids
            else:
                item.pop("sourceTurnIds", None)
        if (
            item["component"] not in REQ_COMPONENTS
            or item["property"] not in REQ_PROPERTIES
            or item["relation"] not in REQ_RELATIONS
        ):
            raise ValueError("Unsupported requirement attribute.")
        if item["strength"] not in {"invariant", "goal", "preference"}:
            raise ValueError("Invalid requirement strength.")
        if not isinstance(item["entities"], list) or any(
            not isinstance(x, str) or x.upper() not in labels for x in item["entities"]
        ):
            raise ValueError("Requirement references an unknown current entity.")
        item["entities"] = list(dict.fromkeys(x.upper() for x in item["entities"]))
        value = item["value"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("Requirement value must be an explicit nonnegative integer.")
        if item["unit"] not in {"absolute", "delta", "none"} or not isinstance(
            item["focused"], bool
        ):
            raise ValueError("Invalid value unit or focus.")
        if item["unit"] == "delta" and item["relation"] in {"increase", "decrease"} and value == 0:
            raise ValueError(
                "Delta increase/decrease requirements must have a positive value."
            )
        if value is not None:
            without_labels = re.sub(
                r"(?<![A-Za-z0-9])(?:B\d+|T\d+|P)(?![A-Za-z0-9])", "", span, flags=re.I
            )
            if (
                not re.search(rf"(?<!\d){value}(?!\d)", without_labels)
                and _CHINESE_DIGITS.get(value, "\0") not in without_labels
                and not (value == 2 and "二" in without_labels)
            ):
                raise ValueError(
                    "Numeric requirement is not supported by designer evidence."
                )
        priority_span = item["priorityEvidenceSpan"]
        if not isinstance(priority_span, str) or (priority_span and priority_span not in source):
            raise ValueError("Priority lacks exact designer evidence.")
        if not isinstance(item["statement"], str) or not item["statement"].strip():
            raise ValueError("Requirement needs a readable statement.")
        item["statement"] = item["statement"].strip()[:400]
        prop, relation = item["property"], item["relation"]
        if item["strength"] == "invariant" and not (
            (prop in {"count", "positions", "distribution"} and relation in {"preserve", "equal"})
            or (prop == "change_scope" and value is not None)
        ):
            raise ValueError("A directional goal cannot become an immutable constraint.")
        if (
            prop in {"count", "positions", "distribution", "distance"}
            and item["component"] == "gameplay"
        ):
            raise ValueError("Map attribute needs an actual map component.")
        if prop in REQ_METRICS and relation not in PROPERTY_RELATIONS[prop]:
            raise ValueError("Metric direction is invalid.")
        if (
            prop in {"count", "distribution", "change_scope"}
            and relation not in PROPERTY_RELATIONS[prop]
        ):
            raise ValueError("Numeric map attribute needs a numeric relation.")
        if prop == "distance" and relation not in PROPERTY_RELATIONS[prop]:
            raise ValueError("Distance needs a distance relation.")
        if prop == "appearance" and (
            relation not in PROPERTY_RELATIONS[prop] or value is not None or item["unit"] != "none"
        ):
            raise ValueError(
                "Qualitative appearance uses seek/avoid without numeric units or values."
            )

        identity = json.dumps(
            {
                key: item[key]
                for key in (
                    "component",
                    "property",
                    "relation",
                    "entities",
                    "value",
                    "unit",
                    "strength",
                )
            },
            sort_keys=True,
        )
        if scope != "all":
            identity += "|scope=" + scope
        if identity in seen:
            for existing in requirements:
                existing_id = json.dumps(
                    {
                        key: existing[key]
                        for key in (
                            "component",
                            "property",
                            "relation",
                            "entities",
                            "value",
                            "unit",
                            "strength",
                        )
                    },
                    sort_keys=True,
                )
                existing_scope = existing.get("scope")
                if existing_scope and existing_scope != "all":
                    existing_id += "|scope=" + existing_scope
                if existing_id == identity:
                    turn_ids = existing.setdefault("sourceTurnIds", [existing["sourceTurnId"]])
                    for sid in item.get("sourceTurnIds") or [source_id]:
                        if sid not in turn_ids:
                            turn_ids.append(sid)
                    if (
                        item.get("priorityEvidenceSpan")
                        and not existing.get("priorityEvidenceSpan")
                    ):
                        existing["priorityEvidenceSpan"] = item["priorityEvidenceSpan"]
                    existing["focused"] = bool(existing.get("focused") or item.get("focused"))
                    break
            continue
        seen.add(identity)
        item["requirementId"] = (
            "req-" + hashlib.sha256((item["sourceTurnId"] + identity).encode()).hexdigest()[:20]
        )
        requirements.append(item)

    bindings = payload["automaticBindings"]
    if not isinstance(bindings, list) or any(
        not isinstance(x, Mapping)
        or set(x) != {"entity", "reason"}
        or not isinstance(x.get("entity"), str)
        or x["entity"].upper() not in labels
        or not isinstance(x["reason"], str)
        for x in bindings
    ):
        raise ValueError("Automatic binding must reference the current snapshot.")
    if bindings and not any(not r["entities"] for r in requirements):
        raise ValueError("Automatic binding needs an unspecified goal.")

    transitions = payload["exactTransitions"]
    if not isinstance(transitions, list) or len(transitions) > 12:
        raise ValueError("Invalid exact transitions.")
    for transition in transitions:
        if not isinstance(transition, Mapping) or set(transition) != {
            "row",
            "column",
            "from",
            "to",
            "sourceTurnId",
            "evidenceSpan",
        }:
            raise ValueError("Invalid exact transition evidence.")
        span = transition["evidenceSpan"]
        if (
            not isinstance(transition.get("sourceTurnId"), str)
            or not isinstance(span, str)
            or not span
            or span not in sources.get(transition["sourceTurnId"], "")
        ):
            raise ValueError("Exact transition lacks designer evidence.")
        r, c = transition["row"], transition["column"]
        if (
            type(r) is not int
            or type(c) is not int
            or not re.search(rf"(?<!\d){r}(?!\d)", span)
            or not re.search(rf"(?<!\d){c}(?!\d)", span)
        ):
            raise ValueError("Exact coordinates lack designer evidence.")
        if (
            transition["from"] not in {".", "#", "@"}
            or transition["to"] not in {".", "#", "@"}
            or transition["from"] == transition["to"]
        ):
            raise ValueError("Exact transition outside editable tile types.")

    return {
        "policyVersion": POLICY_VERSION,
        "requirements": requirements,
        "exactTransitions": list(transitions),
        "automaticBindings": [
            {
                "entity": x["entity"].upper(),
                "reason": x["reason"][:300],
                "authority": "proposal_assumption",
            }
            for x in bindings
        ],
    }


def preflight_requirements(
    rows: Sequence[str],
    record: Mapping[str, Any] | RequirementRecord | None,
) -> list[dict[str, Any]]:
    """Reject conflicting fixed requirements before spending search budget."""
    rec = (
        record.model_dump(by_alias=True)
        if isinstance(record, RequirementRecord)
        else (record or {})
    )
    issues: list[dict[str, Any]] = []
    locks: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    transitions: dict[tuple[int, int], str] = {}

    for item in rec.get("requirements") or ():
        key = (
            item["component"],
            item["property"],
            tuple(item["entities"]),
            item.get("scope", "all"),
        )
        if item["strength"] == "invariant":
            if key in locks and (locks[key]["value"], locks[key]["relation"]) != (
                item["value"],
                item["relation"],
            ):
                issues.append(
                    {
                        "requirementId": item["requirementId"],
                        "reason": "conflicting_fixed_requirements",
                    }
                )
            locks[key] = item
        if item["component"] in {"player", "box", "target"} and item["property"] in {
            "count",
            "positions",
        }:
            before = len(component_positions(rows, item["component"]))
            change = item["relation"] not in {"preserve", "equal"} or (
                item["property"] == "count"
                and item["value"] is not None
                and item["value"] != before
            )
            if change:
                issues.append(
                    {"requirementId": item["requirementId"], "reason": "protected_entity_change"}
                )

    shell = connected_outer_shell(rows)
    for item in rec.get("exactTransitions") or ():
        point = (item["row"], item["column"])
        r, c = point[0] - 1, point[1] - 1
        if (
            not (0 < r < len(rows) - 1 and 0 < c < len(rows[r]) - 1)
            or (c, r) in shell
            or rows[r][c] != item["from"]
        ):
            issues.append({"reason": "exact_transition_conflicts_with_snapshot", "point": point})
        if point in transitions and transitions[point] != item["to"]:
            issues.append({"reason": "conflicting_exact_transitions", "point": point})
        transitions[point] = item["to"]
        for component, tiles in REQ_TILES.items():
            if item["from"] in tiles or item["to"] in tiles:
                lock = locks.get((component, "positions", (), "all"))
                if lock:
                    issues.append(
                        {
                            "requirementId": lock["requirementId"],
                            "reason": "exact_transition_conflicts_with_preservation",
                        }
                    )
    return issues


def goal_rank(results: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    goals = [x for x in results if x["strength"] != "invariant"]
    return (
        sum(x["status"] == "fulfilled" and x["prioritized"] for x in goals),
        sum(x["status"] == "partial" and x["prioritized"] for x in goals),
        sum(x["status"] == "fulfilled" and x["focused"] for x in goals),
        sum(x["status"] == "partial" and x["focused"] for x in goals),
        sum(x["status"] == "fulfilled" for x in goals),
        sum(x["status"] == "partial" for x in goals),
    )


def build_tradeoff_notice(
    record: Mapping[str, Any] | RequirementRecord | None,
    outcomes: Sequence[Mapping[str, Any]],
    language: str,
) -> str:
    """Delegate bilingual requirement tradeoff formatting to server.i18n.messages."""
    rec = (
        record.model_dump(by_alias=True)
        if isinstance(record, RequirementRecord)
        else (record or {})
    )
    if (
        not rec.get("requirements")
        and not rec.get("automaticBindings")
        and not rec.get("exactTransitions")
    ):
        return ""
    fulfilled = [
        str(x["statement"])
        for x in outcomes
        if x["strength"] != "invariant" and x["status"] == "fulfilled"
    ]
    partial = [
        str(x["statement"])
        for x in outcomes
        if x["strength"] != "invariant" and x["status"] == "partial"
    ]
    deferred = [
        str(x["statement"])
        for x in outcomes
        if x["strength"] != "invariant" and x["status"] == "deferred"
    ]
    locked = [
        str(x["statement"])
        for x in outcomes
        if x["strength"] == "invariant" and x["passed"]
    ]
    binding_labels = [str(x["entity"]) for x in rec.get("automaticBindings") or ()]
    has_box_alt = any(
        x["property"] == "boxAlternations" and x["status"] != "deferred" for x in outcomes
    )
    return format_tradeoff_notice(
        fulfilled=fulfilled,
        partial=partial,
        deferred=deferred,
        has_box_alternations=has_box_alt,
        locked=locked,
        binding_labels=binding_labels,
        language=language,
    )


__all__ = [
    "build_tradeoff_notice",
    "component_positions",
    "evaluate_requirements",
    "goal_rank",
    "normalize_requirement_record",
    "preflight_requirements",
    "validate_requirement_record",
]
