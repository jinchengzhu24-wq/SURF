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
_OUTER_WALL_TOKEN_RE = re.compile(r"\bouter[\s_-]*walls?\b|外墙|外围墙体?", re.I)
_PROTECTED_ONLY_SUBJECT_RE = re.compile(
    r"\b(?:player|character|box|boxes|crate|crates|target|targets|goal|goals|"
    r"outer[\s_-]*walls?|outer[\s_-]*shell|perimeter|boundary|boundaries)\b|"
    r"(?:玩家|角色|小人|箱子|木箱|目标点?|终点|外墙|外围墙体?|外框|边界)",
    re.I,
)
_EDITABLE_OR_GAMEPLAY_SUBJECT_RE = re.compile(
    r"\b(?:water|waters|pond|pool|river|moat|wall|walls|obstacle|obstacles|"
    r"barrier|barriers|block|blocks|corridor|corridors|passage|passages|aisle|aisles|"
    r"route|routes|path|paths|step|steps|push|pushes|detour|detours|"
    r"space|room|floor|tile|tiles|cell|cells)\b|"
    r"(?:水|水域|水池|水面|墙|墙体|墙壁|内墙|障碍|阻挡|通道|走廊|过道|"
    r"路线|路径|步数|推数|推动|绕路|空间|地面|空地|格子)",
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


_DESIGN_CONTEXT_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "goal",
        "constraint",
        "decision",
        "reason",
        "question",
        "statement",
        "displayStatement",
        "evidenceSpan",
        "normalizedMeaning",
        "userPosition",
        "aiPosition",
        "coreDisagreement",
        "proposalSummary",
        "acceptedReason",
        "primaryHypothesis",
        "secondaryHypothesis",
        "observation",
    }
)


def _collect_design_context_texts(design_context: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(design_context, Mapping):
        return []
    collected: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if (
                    key in _DESIGN_CONTEXT_TEXT_KEYS
                    and isinstance(value, str)
                    and len(value.strip()) >= 4
                ):
                    collected.append(value.strip())
                elif isinstance(value, (Mapping, list, tuple)):
                    _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(design_context)
    return collected


def normalize_requirement_record(
    payload: Any,
    user_turns: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
    *,
    design_context: Mapping[str, Any] | None = None,
) -> tuple[Any, list[str]]:
    """Prune redundant platform-protected invariants and normalize benign schema quirks."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("requirements"), list):
        return payload, []
    sources = {str(item["id"]): str(item["content"]) for item in user_turns or ()}
    turn_texts = list(sources.values())
    context_texts_folded = [t.casefold() for t in _collect_design_context_texts(design_context)]
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
        item.pop("requirementId", None)
        if (
            item.get("property") == "change_scope"
            and item.get("value") is None
            and item.get("relation") in {"preserve", "equal", "decrease"}
        ):
            audit_notes.append("dropped_unbounded_change_scope")
            continue
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
        span_and_stmt = f"{item.get('evidenceSpan') or ''} {item.get('statement') or ''}"
        stripped_for_editable = _OUTER_WALL_TOKEN_RE.sub(" ", span_and_stmt)
        is_mismapped_protected_lock = (
            item.get("value") is None
            and item.get("relation") in {"preserve", "equal"}
            and bool(_PLATFORM_LOCK_PHRASE.search(span_and_stmt))
            and bool(_PROTECTED_ONLY_SUBJECT_RE.search(span_and_stmt))
            and not bool(_EDITABLE_OR_GAMEPLAY_SUBJECT_RE.search(stripped_for_editable))
        )
        if (is_protected_component and has_lock_signal) or is_mismapped_protected_lock:
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
        raw_span = item.get("evidenceSpan")
        clean_span = raw_span.strip() if isinstance(raw_span, str) else ""
        if (
            context_texts_folded
            and len(clean_span) >= 3
            and (source_text is None or clean_span not in source_text)
            and not any(clean_span in t for t in turn_texts)
            and any(clean_span.casefold() in ctx_t for ctx_t in context_texts_folded)
        ):
            audit_notes.append("dropped_design_context_echo")
            continue
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
            if isinstance(binding, Mapping):
                if (
                    isinstance(binding.get("entity"), str)
                    and binding["entity"].upper() in _GENERIC_ENTITY_LABELS
                    and binding["entity"].upper() not in labels
                ):
                    audit_notes.append("dropped_generic_entity_label")
                    continue
                filtered_bindings.append(
                    {k: v for k, v in binding.items() if k != "authority"}
                )
            else:
                filtered_bindings.append(binding)
        if filtered_bindings and not any(
            not (isinstance(r, Mapping) and r.get("entities")) for r in cleaned
        ):
            filtered_bindings = []
            audit_notes.append("dropped_unbound_automatic_bindings")
        cleaned_bindings = filtered_bindings

    normalized_envelope = {
        **dict(payload),
        "requirements": cleaned,
        "automaticBindings": cleaned_bindings,
    }
    normalized_envelope.pop("policyVersion", None)
    return normalized_envelope, list(dict.fromkeys(audit_notes))


def validate_requirement_record(
    payload: Any,
    user_turns: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate an evidence-bound requirement envelope strictly."""
    expected_envelope = {"requirements", "automaticBindings", "exactTransitions"}
    if not isinstance(payload, Mapping) or set(payload) != expected_envelope:
        got = sorted(payload.keys()) if isinstance(payload, Mapping) else type(payload).__name__
        raise ValueError(
            f"Requirements envelope must have exact keys {sorted(expected_envelope)}; got {got}."
        )
    sources = {str(item["id"]): str(item["content"]) for item in user_turns}
    labels = {
        str(item.get("id") or item.get("label") or "").upper()
        for item in (snapshot or {}).get("entities") or ()
    }
    raw_requirements = payload["requirements"]
    if not isinstance(raw_requirements, list) or len(raw_requirements) > 48:
        raise ValueError("requirements must be a list of at most 48 items.")

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
    for idx, raw in enumerate(raw_requirements):
        prefix = f"requirements[{idx}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{prefix} must be a JSON object.")
        missing = sorted(required_fields - set(raw))
        extra = sorted(set(raw) - required_fields - {"scope", "sourceTurnIds"})
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing {missing}")
            if extra:
                parts.append(f"unexpected {extra}")
            raise ValueError(f"{prefix} has invalid fields: {', '.join(parts)}.")
        item = dict(raw)
        scope = item.get("scope", "all")
        if scope not in {"all", "original_cells"}:
            raise ValueError(
                f"{prefix}.scope={scope!r} is invalid; allowed values: ['all', 'original_cells']."
            )
        if scope == "original_cells" and (
            item["property"] != "count" or item["component"] not in {"water", "wall"}
        ):
            raise ValueError(
                f"{prefix}.scope='original_cells' is only supported for component in "
                f"('water', 'wall') and property='count'; got component={item['component']!r}, "
                f"property={item['property']!r}."
            )
        source_id = item.get("sourceTurnId")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        span = item["evidenceSpan"]
        if source is None:
            raise ValueError(
                f"{prefix}.sourceTurnId={source_id!r} does not match any designer turn "
                f"(valid IDs: {sorted(sources)})."
            )
        if not isinstance(span, str) or not span.strip() or span not in source:
            raise ValueError(
                f"{prefix}.evidenceSpan={span!r} is not an exact verbatim substring of "
                f"turn {source_id!r}."
            )
        if "sourceTurnIds" in item:
            raw_turn_ids = item["sourceTurnIds"]
            if (
                not isinstance(raw_turn_ids, list)
                or not raw_turn_ids
                or any(not isinstance(tid, str) or tid not in sources for tid in raw_turn_ids)
                or source_id not in raw_turn_ids
            ):
                raise ValueError(
                    f"{prefix}.sourceTurnIds={raw_turn_ids!r} must reference existing designer "
                    f"turns (valid IDs: {sorted(sources)}) and include {source_id!r}."
                )
            deduped_ids = list(dict.fromkeys(raw_turn_ids))
            if len(deduped_ids) > 1:
                item["sourceTurnIds"] = deduped_ids
            else:
                item.pop("sourceTurnIds", None)
        if item["component"] not in REQ_COMPONENTS:
            raise ValueError(
                f"{prefix}.component={item['component']!r} is unsupported; "
                f"allowed: {sorted(REQ_COMPONENTS)}."
            )
        if item["property"] not in REQ_PROPERTIES:
            raise ValueError(
                f"{prefix}.property={item['property']!r} is unsupported; "
                f"allowed: {sorted(REQ_PROPERTIES)}."
            )
        if item["relation"] not in REQ_RELATIONS:
            raise ValueError(
                f"{prefix}.relation={item['relation']!r} is unsupported; "
                f"allowed: {sorted(REQ_RELATIONS)}."
            )
        if item["strength"] not in {"invariant", "goal", "preference"}:
            raise ValueError(
                f"{prefix}.strength={item['strength']!r} is invalid; "
                "allowed: ['goal', 'invariant', 'preference']."
            )
        if not isinstance(item["entities"], list):
            raise ValueError(f"{prefix}.entities must be a list.")
        unknown_entities = [
            x for x in item["entities"] if not isinstance(x, str) or x.upper() not in labels
        ]
        if unknown_entities:
            raise ValueError(
                f"{prefix}.entities references unknown entity {unknown_entities!r}; "
                f"available snapshot entities: {sorted(labels)}."
            )
        item["entities"] = list(dict.fromkeys(x.upper() for x in item["entities"]))
        value = item["value"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(
                f"{prefix}.value={value!r} must be a nonnegative integer or null."
            )
        if item["unit"] not in {"absolute", "delta", "none"}:
            raise ValueError(
                f"{prefix}.unit={item['unit']!r} is invalid; "
                "allowed: ['absolute', 'delta', 'none']."
            )
        if not isinstance(item["focused"], bool):
            raise ValueError(f"{prefix}.focused must be a boolean.")
        if item["unit"] == "delta" and item["relation"] in {"increase", "decrease"} and value == 0:
            raise ValueError(
                f"{prefix}: delta {item['relation']!r} requirement must have value >= 1, got 0."
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
                    f"{prefix}: numeric value={value} is not explicitly stated in "
                    f"evidenceSpan={span!r}; set value=null and unit='none' for qualitative wishes."
                )
        priority_span = item["priorityEvidenceSpan"]
        if not isinstance(priority_span, str) or (priority_span and priority_span not in source):
            raise ValueError(
                f"{prefix}.priorityEvidenceSpan={priority_span!r} is not an exact substring of "
                f"turn {source_id!r}."
            )
        if not isinstance(item["statement"], str) or not item["statement"].strip():
            raise ValueError(f"{prefix}.statement must be a non-blank string.")
        item["statement"] = item["statement"].strip()[:400]
        prop, relation = item["property"], item["relation"]
        if item["strength"] == "invariant" and not (
            (prop in {"count", "positions", "distribution"} and relation in {"preserve", "equal"})
            or (prop == "change_scope" and value is not None)
        ):
            raise ValueError(
                f"{prefix}: strength='invariant' is invalid for property={prop!r}, "
                f"relation={relation!r}; directional goals must use strength='goal'."
            )
        if (
            prop in {"count", "positions", "distribution", "distance"}
            and item["component"] == "gameplay"
        ):
            raise ValueError(
                f"{prefix}: component='gameplay' cannot use map property {prop!r}; "
                "use a map component ('water', 'wall', 'player', 'box', 'target')."
            )
        if prop in REQ_METRICS and relation not in PROPERTY_RELATIONS[prop]:
            raise ValueError(
                f"{prefix}: metric property {prop!r} does not allow relation={relation!r}; "
                f"allowed: {sorted(PROPERTY_RELATIONS[prop])}."
            )
        if (
            prop in {"count", "distribution", "change_scope"}
            and relation not in PROPERTY_RELATIONS[prop]
        ):
            raise ValueError(
                f"{prefix}: numeric property {prop!r} does not allow relation={relation!r}; "
                f"allowed: {sorted(PROPERTY_RELATIONS[prop])}."
            )
        if prop == "distance" and relation not in PROPERTY_RELATIONS[prop]:
            raise ValueError(
                f"{prefix}: property='distance' does not allow relation={relation!r}; "
                f"allowed: {sorted(PROPERTY_RELATIONS[prop])}."
            )
        if prop == "appearance" and (
            relation not in PROPERTY_RELATIONS[prop] or value is not None or item["unit"] != "none"
        ):
            raise ValueError(
                f"{prefix}: property='appearance' requires relation in ['avoid', 'seek'] with "
                f"value=null and unit='none'; got relation={relation!r}, value={value!r}, "
                f"unit={item['unit']!r}."
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
    if not isinstance(bindings, list):
        raise ValueError("automaticBindings must be a list.")
    for b_idx, binding in enumerate(bindings):
        if not isinstance(binding, Mapping) or set(binding) != {"entity", "reason"}:
            raise ValueError(
                f"automaticBindings[{b_idx}] must be an object with keys ['entity', 'reason']."
            )
        if not isinstance(binding.get("entity"), str) or binding["entity"].upper() not in labels:
            raise ValueError(
                f"automaticBindings[{b_idx}].entity={binding.get('entity')!r} is not in current "
                f"snapshot entities {sorted(labels)}."
            )
        if not isinstance(binding["reason"], str) or not binding["reason"].strip():
            raise ValueError(f"automaticBindings[{b_idx}].reason must be a non-blank string.")
    if bindings and not any(not r["entities"] for r in requirements):
        raise ValueError(
            "automaticBindings must be empty [] when every requirement already specifies entities "
            "(or when requirements is empty)."
        )

    transitions = payload["exactTransitions"]
    if not isinstance(transitions, list) or len(transitions) > 12:
        raise ValueError("exactTransitions must be a list of at most 12 items.")
    for t_idx, transition in enumerate(transitions):
        t_prefix = f"exactTransitions[{t_idx}]"
        if not isinstance(transition, Mapping) or set(transition) != {
            "row",
            "column",
            "from",
            "to",
            "sourceTurnId",
            "evidenceSpan",
        }:
            raise ValueError(
                f"{t_prefix} must have exact keys "
                "['column', 'evidenceSpan', 'from', 'row', 'sourceTurnId', 'to']."
            )
        span = transition["evidenceSpan"]
        if (
            not isinstance(transition.get("sourceTurnId"), str)
            or not isinstance(span, str)
            or not span
            or span not in sources.get(transition["sourceTurnId"], "")
        ):
            raise ValueError(
                f"{t_prefix}: evidenceSpan={span!r} is not a verbatim substring of "
                f"sourceTurnId={transition.get('sourceTurnId')!r}."
            )
        r, c = transition["row"], transition["column"]
        if (
            type(r) is not int
            or type(c) is not int
            or not re.search(rf"(?<!\d){r}(?!\d)", span)
            or not re.search(rf"(?<!\d){c}(?!\d)", span)
        ):
            raise ValueError(
                f"{t_prefix}: coordinates ({r}, {c}) are not explicitly stated in "
                f"evidenceSpan={span!r}."
            )
        if transition["from"] not in {".", "#", "@"} or transition["to"] not in {".", "#", "@"}:
            raise ValueError(
                f"{t_prefix} at ({r}, {c}) uses non-editable tile(s) "
                f"from={transition['from']!r}, to={transition['to']!r}; "
                "only '.', '#', '@' are editable."
            )
        if transition["from"] == transition["to"]:
            raise ValueError(
                f"{t_prefix} at ({r}, {c}) has identical 'from' and 'to' tile "
                f"{transition['from']!r}; 'from' and 'to' must differ."
            )

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
