"""Conservative logical entity identity bindings and StageSnapshot construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.core.grid import (
    HEIGHT,
    TILE_BOX,
    TILE_PLAYER,
    TILE_TARGET,
    TILE_WATER,
    WIDTH,
    map_fingerprint,
    parse_grid,
    parse_structural_grid,
)
from server.domain.sessions import ENTITY_BINDINGS_SCHEMA_VERSION, TILE_LEGEND


def _new_entity_id(kind: str, position: tuple[int, int]) -> str:
    seed = f"{kind}:{position[1] + 1}:{position[0] + 1}".encode()
    return f"{kind}:{hashlib.sha256(seed).hexdigest()[:16]}"


def _binding_entities(bindings: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(bindings, Mapping) or not isinstance(bindings.get("entities"), list):
        return []
    return [dict(item) for item in bindings["entities"] if isinstance(item, Mapping)]


def entity_binding_fingerprint(bindings: Mapping[str, Any] | None) -> str:
    """Return a deterministic SHA-256 fingerprint for an entity bindings snapshot."""
    payload = {
        "schemaVersion": (bindings or {}).get("schemaVersion", ENTITY_BINDINGS_SCHEMA_VERSION),
        "identityStatus": (bindings or {}).get("identityStatus", "unknown"),
        "entities": [
            {
                key: item.get(key)
                for key in ("entityId", "label", "kind", "row", "column", "identityConfidence")
            }
            for item in _binding_entities(bindings)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def derive_entity_transitions(
    before_rows: Sequence[str],
    after_rows: Sequence[str],
) -> list[dict[str, Any]]:
    """Return 1-based untagged cell transitions between two 12x10 grids."""
    before = parse_structural_grid(before_rows).rows
    after = parse_structural_grid(after_rows).rows
    return [
        {"row": y + 1, "column": x + 1, "from": before_tile, "to": after_tile}
        for y, (b_row, a_row) in enumerate(zip(before, after, strict=True))
        for x, (before_tile, after_tile) in enumerate(zip(b_row, a_row, strict=True))
        if before_tile != after_tile
    ]


def build_entity_bindings(
    rows: Sequence[str],
    *,
    parent_bindings: Mapping[str, Any] | None = None,
    entity_transitions: Sequence[Mapping[str, Any]] | None = None,
    source: str = "initial",
) -> dict[str, Any]:
    """Derive conservative entity identities across Stage edits without guessing swaps."""
    grid = parse_grid(rows, validate_playable=True)
    normalized = grid.rows
    current_positions = {
        "player": grid.find_tiles(TILE_PLAYER),
        "box": grid.find_tiles(TILE_BOX),
        "target": grid.find_tiles(TILE_TARGET),
    }
    tile_by_kind = {"player": "p", "box": "s", "target": "t"}
    label_prefix = {"player": "P", "box": "B", "target": "T"}
    parent_entities = _binding_entities(parent_bindings)
    transition_items = [
        dict(item) for item in (entity_transitions or ()) if isinstance(item, Mapping)
    ]

    ambiguous_kinds: set[str] = set()
    for kind, tile in tile_by_kind.items():
        sources = {
            (item.get("column"), item.get("row"))
            for item in transition_items
            if item.get("from") == tile and item.get("to") != tile
        }
        dests = {
            (item.get("column"), item.get("row"))
            for item in transition_items
            if item.get("to") == tile and item.get("from") != tile
        }
        if len(sources) > 1 and len(dests) > 1 and any(
            not item.get("anchorEntity")
            for item in transition_items
            if item.get("from") == tile or item.get("to") == tile
        ):
            ambiguous_kinds.add(kind)

    assignments: dict[tuple[str, tuple[int, int]], dict[str, Any]] = {}
    used_parent_ids: set[str] = set()

    def _assign(entity: Mapping[str, Any], position: tuple[int, int], confidence: str) -> None:
        key = (str(entity["kind"]), position)
        eid = entity.get("entityId")
        if key in assignments or (eid and eid in used_parent_ids):
            return
        assignments[key] = {
            "entityId": eid or _new_entity_id(str(entity["kind"]), position),
            "label": entity.get("label"),
            "kind": entity["kind"],
            "row": position[1] + 1,
            "column": position[0] + 1,
            "identityConfidence": confidence,
        }
        if eid:
            used_parent_ids.add(str(eid))

    for entity in parent_entities:
        kind = str(entity.get("kind") or "")
        if kind not in current_positions or kind in ambiguous_kinds:
            continue
        pos = (int(entity.get("column", 0)) - 1, int(entity.get("row", 0)) - 1)
        if pos in current_positions[kind]:
            conf = "exact" if entity.get("identityConfidence") == "exact" else "unknown"
            _assign(entity, pos, conf)

    for transition in transition_items:
        label = transition.get("anchorEntity")
        if not isinstance(label, str):
            continue
        entity = next((item for item in parent_entities if item.get("label") == label), None)
        if entity is None:
            continue
        kind = str(entity.get("kind") or "")
        if kind not in current_positions or transition.get("to") != tile_by_kind[kind]:
            continue
        pos = (int(transition.get("column", 0)) - 1, int(transition.get("row", 0)) - 1)
        if pos in current_positions[kind]:
            _assign(entity, pos, "exact")

    for kind, positions in current_positions.items():
        assigned_pos = {pos for (ak, pos) in assignments if ak == kind}
        unmatched_cur = [p for p in positions if p not in assigned_pos]
        unmatched_par = [
            e
            for e in parent_entities
            if e.get("kind") == kind and e.get("entityId") not in used_parent_ids
        ]
        if len(unmatched_cur) == 1 and len(unmatched_par) == 1:
            _assign(unmatched_par[0], unmatched_cur[0], "exact")

    records: list[dict[str, Any]] = []
    for kind in ("player", "box", "target"):
        positions = sorted(current_positions[kind], key=lambda item: (item[1], item[0]))
        used_labels = {
            item.get("label")
            for item in assignments.values()
            if item.get("kind") == kind and item.get("label")
        }
        available = [
            "P" if kind == "player" else f"{label_prefix[kind]}{idx}"
            for idx in range(1, len(positions) + 1)
        ]
        for pos in positions:
            rec = assignments.get((kind, pos))
            if rec is None:
                lbl = next(
                    (cand for cand in available if cand not in used_labels),
                    f"{label_prefix[kind]}{len(records) + 1}",
                )
                rec = {
                    "entityId": _new_entity_id(kind, pos),
                    "label": lbl,
                    "kind": kind,
                    "row": pos[1] + 1,
                    "column": pos[0] + 1,
                    "identityConfidence": "unknown" if parent_entities else "exact",
                }
                assignments[(kind, pos)] = rec
            used_labels.add(rec["label"])
            records.append(rec)

    status = (
        "exact"
        if all(item["identityConfidence"] == "exact" for item in records)
        else "partial"
        if any(item["identityConfidence"] == "exact" for item in records)
        else "unknown"
    )
    binding: dict[str, Any] = {
        "schemaVersion": ENTITY_BINDINGS_SCHEMA_VERSION,
        "mapFingerprint": map_fingerprint(normalized),
        "identityStatus": status,
        "source": source,
        "entities": records,
    }
    binding["bindingFingerprint"] = entity_binding_fingerprint(binding)
    return binding


def entity_bindings_match_rows(bindings: Mapping[str, Any] | None, rows: Sequence[str]) -> bool:
    """Verify stored entity bindings match the map rows and integrity fingerprint."""
    if not isinstance(bindings, Mapping):
        return False
    try:
        normalized = parse_structural_grid(rows).rows
    except ValueError:
        return False
    entities = _binding_entities(bindings)
    if not entities or bindings.get("schemaVersion") != ENTITY_BINDINGS_SCHEMA_VERSION:
        return False
    if bindings.get("mapFingerprint") != map_fingerprint(normalized):
        return False
    if bindings.get("bindingFingerprint") != entity_binding_fingerprint(bindings):
        return False
    tile_by_kind = {"player": "p", "box": "s", "target": "t"}
    for entity in entities:
        kind = entity.get("kind")
        r, c = entity.get("row"), entity.get("column")
        if kind not in tile_by_kind or not isinstance(r, int) or not isinstance(c, int):
            return False
        if not (1 <= r <= HEIGHT and 1 <= c <= WIDTH):
            return False
        if normalized[r - 1][c - 1] != tile_by_kind[kind]:
            return False
    return True


def build_stage_snapshot(
    rows: Sequence[str],
    *,
    version_id: str | None = None,
    stage_number: int | None = None,
    entity_bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the authoritative StageSnapshot dict for prompts, verification, and UI."""
    grid = parse_structural_grid(rows)
    bindings = (
        dict(entity_bindings)
        if entity_bindings_match_rows(entity_bindings, grid.rows)
        else build_entity_bindings(grid.rows, source="derived")
    )
    entities = _binding_entities(bindings)
    player = next((e for e in entities if e.get("kind") == "player"), {})
    boxes = [e for e in entities if e.get("kind") == "box"]
    targets = [e for e in entities if e.get("kind") == "target"]
    water_cells = [{"row": y + 1, "column": x + 1} for x, y in grid.find_tiles(TILE_WATER)]
    tile_at = {
        f"{y + 1},{x + 1}": tile
        for y, row in enumerate(grid.rows)
        for x, tile in enumerate(row)
    }
    return {
        "schemaVersion": 1,
        "legend": dict(TILE_LEGEND),
        "versionId": version_id,
        "stageNumber": stage_number,
        "rows": grid.to_rows(),
        "dimensions": {"rows": HEIGHT, "columns": WIDTH},
        "coordinateSystem": "one-based row,column; row 1 is top and column 1 is left",
        "mapFingerprint": map_fingerprint(grid.rows),
        "entityBindingFingerprint": bindings.get("bindingFingerprint", ""),
        "identityStatus": bindings.get("identityStatus", "unknown"),
        "entities": entities,
        "player": player,
        "boxes": boxes,
        "targets": targets,
        "waterCells": water_cells,
        "tileAt": tile_at,
    }


__all__ = [
    "build_entity_bindings",
    "build_stage_snapshot",
    "derive_entity_transitions",
    "entity_binding_fingerprint",
    "entity_bindings_match_rows",
]
