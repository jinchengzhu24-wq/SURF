"""Map grounding verification, visible text sanitization, and route link extraction."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from server.core.grid import (
    DIRECTIONS,
    HEIGHT,
    TILE_BOX,
    TILE_LEGEND,
    TILE_PLAYER,
    TILE_TARGET,
    TILE_WALL,
    TILE_WATER,
    WALKABLE_TILES,
    WIDTH,
    Grid,
    parse_structural_grid,
)

from . import _grounding_patterns as gp

COORDINATE_LINK_LIMIT = 12


@dataclass(frozen=True, slots=True)
class GroundingIssue:
    """Single deterministic contradiction between model text and the authoritative Grid."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class StageMapFacts:
    """Authoritative map facts derived solely from Grid + optional entity bindings."""

    grid: Grid
    entities: dict[str, tuple[int, int]]
    uncertain_entities: frozenset[str]
    water_cells: frozenset[tuple[int, int]]
    editable_internal_walls: tuple[tuple[int, int], ...]

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "rows": self.grid.to_rows(),
            "dimensions": {"rows": HEIGHT, "columns": WIDTH},
            "coordinateSystem": "one-based row,column; row 1 is top and column 1 is left",
            "entities": [
                {"id": label, "row": row, "column": col, "identityConfidence": "exact"}
                for label, (row, col) in sorted(self.entities.items())
            ],
            "waterCells": [{"row": r, "column": c} for r, c in sorted(self.water_cells)],
            "editableInternalWallCells": [
                {"row": r, "column": c} for r, c in self.editable_internal_walls
            ],
            "legend": dict(TILE_LEGEND),
        }


def build_stage_facts(
    grid_or_rows: Grid | Sequence[str],
    entity_bindings: Mapping[str, Any] | None = None,
) -> StageMapFacts:
    """Derive authoritative one-based entity coordinates and editable terrain from Grid."""
    grid = parse_structural_grid(grid_or_rows)
    entities: dict[str, tuple[int, int]] = {}
    uncertain: set[str] = set()

    raw_entities = (
        entity_bindings.get("entities") if isinstance(entity_bindings, Mapping) else None
    )
    if isinstance(raw_entities, list) and raw_entities:
        for item in raw_entities:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label") or item.get("id") or "").upper()
            if not label:
                continue
            if str(item.get("identityConfidence") or "exact") != "exact":
                uncertain.add(label)
                continue
            row, col = item.get("row"), item.get("column")
            if isinstance(row, int) and isinstance(col, int):
                entities[label] = (row, col)
    else:
        for x, y in grid.find_tiles(TILE_PLAYER):
            entities["P"] = (y + 1, x + 1)
        for idx, (x, y) in enumerate(grid.find_tiles(TILE_BOX), start=1):
            entities[f"B{idx}"] = (y + 1, x + 1)
        for idx, (x, y) in enumerate(grid.find_tiles(TILE_TARGET), start=1):
            entities[f"T{idx}"] = (y + 1, x + 1)

    water_cells = frozenset((y + 1, x + 1) for x, y in grid.find_tiles(TILE_WATER))
    shell = grid.outer_shell()
    editable_walls = tuple(
        (y + 1, x + 1) for x, y in grid.find_tiles(TILE_WALL) if (x, y) not in shell
    )
    return StageMapFacts(
        grid=grid,
        entities=entities,
        uncertain_entities=frozenset(uncertain),
        water_cells=water_cells,
        editable_internal_walls=editable_walls,
    )


def _tile_from_claim(raw: str) -> str | None:
    cleaned = gp.LEADING_ARTICLE_RE.sub("", str(raw or "").strip().casefold())
    return gp.TILE_CLAIM_MAP.get(cleaned)


def _is_non_current_clause(clause: str) -> bool:
    return bool(
        gp.HISTORICAL_RE.search(clause)
        or gp.NON_CURRENT_RE.search(clause)
        or gp.NEGATION_RE.search(clause)
    )


def _extract_coord_from_match(match: re.Match[str]) -> tuple[int, int]:
    groups = match.groupdict()
    for suffix in ("p", "e", "z"):
        row_s, col_s = groups.get(f"row_{suffix}"), groups.get(f"col_{suffix}")
        if row_s is not None and col_s is not None:
            return int(row_s), int(col_s)
    raise ValueError("Unparseable coordinate match")


def verify_grounding(
    text: str,
    grid_or_rows: Grid | Sequence[str],
    entity_bindings: Mapping[str, Any] | None = None,
) -> list[GroundingIssue]:
    """Check explicit current-Stage tile, entity, and relation claims against Grid."""
    value = str(text or "").strip()
    if not value:
        return []
    facts = build_stage_facts(grid_or_rows, entity_bindings)
    issues: list[GroundingIssue] = []

    protected = gp.PROTECT_COORD_COMMA_RE.sub("\x00", value)
    protected = gp.PROTECT_ROW_COL_COMMA_RE.sub(
        lambda m: f"{m.group(1)}\x00 {m.group(2)}", protected
    )
    clauses = [piece.replace("\x00", ",").strip() for piece in gp.CLAUSE_SPLIT_RE.split(protected)]

    for clause in clauses:
        if not clause or _is_non_current_clause(clause):
            continue

        for m in gp.COORD_TILE_CLAIM_RE.finditer(clause):
            if gp.NEIGHBOR_PREFIX_RE.search(clause[max(0, m.start() - 24) : m.start()]):
                continue
            row, col, expected = int(m.group(1)), int(m.group(2)), _tile_from_claim(m.group(3))
            if not (1 <= row <= HEIGHT and 1 <= col <= WIDTH):
                issues.append(GroundingIssue("OUT_OF_BOUNDS", f"({row},{col}) out of bounds."))
                continue
            actual = facts.grid.tile_at(col - 1, row - 1)
            if expected is not None and actual != expected:
                msg = f"Claimed ({row},{col}) is {expected!r}, actual is {actual!r}."
                issues.append(GroundingIssue("TILE_MISMATCH", msg))

        for m in gp.COORD_TRANSITION_RE.finditer(clause):
            row, col, before = int(m.group(1)), int(m.group(2)), _tile_from_claim(m.group(3))
            if not (1 <= row <= HEIGHT and 1 <= col <= WIDTH):
                issues.append(GroundingIssue("OUT_OF_BOUNDS", f"({row},{col}) out of bounds."))
                continue
            actual = facts.grid.tile_at(col - 1, row - 1)
            if before is not None and actual != before:
                msg = f"Claimed ({row},{col}) starts as {before!r}, actual is {actual!r}."
                issues.append(GroundingIssue("TRANSITION_SOURCE_MISMATCH", msg))

        for pat in (gp.ENTITY_FORWARD_RE, gp.ENTITY_REVERSE_RE):
            for m in pat.finditer(clause):
                row, col = _extract_coord_from_match(m)
                entity = m.group("entity").upper()
                if entity in {"玩家", "起点"}:
                    entity = "P"
                if entity in facts.uncertain_entities:
                    issues.append(
                        GroundingIssue("ENTITY_IDENTITY_UNCERTAIN", f"{entity} identity not exact.")
                    )
                elif entity not in facts.entities:
                    issues.append(GroundingIssue("UNKNOWN_ENTITY", f"Unknown entity {entity}."))
                elif facts.entities[entity] != (row, col):
                    ar, ac = facts.entities[entity]
                    msg = f"Claimed {entity} at ({row},{col}), actual is ({ar},{ac})."
                    issues.append(GroundingIssue("ENTITY_POSITION_MISMATCH", msg))

        for m in gp.ENTITY_PAIR_PROX_RE.finditer(clause):
            a, b = m.group("first").upper(), m.group("second").upper()
            pa, pb = facts.entities.get(a), facts.entities.get(b)
            if pa and pb and abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) > 2:
                issues.append(GroundingIssue("PROXIMITY_MISMATCH", f"Claimed {a} near {b}."))

        for pat in (gp.BOX_WATER_FWD_RE, gp.BOX_WATER_REV_RE):
            for m in pat.finditer(clause):
                box = m.group("box").upper()
                pos = facts.entities.get(box)
                if pos and not any(
                    (pos[0] + dy, pos[1] + dx) in facts.water_cells for dx, dy in DIRECTIONS
                ):
                    issues.append(
                        GroundingIssue("WATER_ADJACENCY_MISMATCH", f"Claimed {box} touches water.")
                    )

    return issues


def sanitize_visible_text(text: str | None, language: str = "en") -> str:
    """Clean markdown fences, leaked internal identifiers, and multi-level wording."""
    if text is None:
        return ""
    cleaned = gp.CODE_FENCE_RE.sub("", str(text))
    cleaned = gp.INLINE_CODE_RE.sub(r"\1", cleaned)
    for pattern, zh_label, en_label in gp.INTERNAL_TERM_PATTERNS:
        cleaned = pattern.sub(zh_label if language == "zh-CN" else en_label, cleaned)
    for pattern, replacement in gp.SINGLE_LEVEL_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = gp.MULTI_SPACE_RE.sub(" ", cleaned)
    cleaned = gp.LINE_INDENT_RE.sub("\n", cleaned)
    return cleaned.strip()


def validate_visible_language(text: str | None, language: str = "en") -> list[str]:
    """Detect leaked implementation identifiers without brittle English-word allowlists."""
    _ = language
    value = str(text or "")
    if not value:
        return []
    leaked = [
        tok for tok in gp.INTERNAL_IDENT_RE.findall(value) if tok in gp.LEAKED_INTERNAL_TOKENS
    ]
    return [f"Leaked internal identifier: {tok}" for tok in dict.fromkeys(leaked)]


def route_exists(
    grid_or_rows: Grid | Sequence[str],
    start: tuple[int, int],
    end: tuple[int, int],
) -> bool:
    """BFS walkability check between two 1-based (row, col) endpoints."""
    if start == end:
        return False
    grid = parse_structural_grid(grid_or_rows)
    sr, sc = start
    er, ec = end
    if not (1 <= sr <= HEIGHT and 1 <= sc <= WIDTH and 1 <= er <= HEIGHT and 1 <= ec <= WIDTH):
        return False

    def _open(r: int, c: int) -> bool:
        tile = grid.tile_at(c - 1, r - 1)
        return tile in WALKABLE_TILES and (tile != TILE_BOX or (r, c) in {start, end})

    if not _open(sr, sc) or not _open(er, ec):
        return False
    queue: deque[tuple[int, int]] = deque([start])
    visited: set[tuple[int, int]] = {start}
    while queue:
        r, c = queue.popleft()
        if (r, c) == end:
            return True
        for dx, dy in DIRECTIONS:
            nr, nc = r + dy, c + dx
            if 1 <= nr <= HEIGHT and 1 <= nc <= WIDTH and (nr, nc) not in visited and _open(nr, nc):
                visited.add((nr, nc))
                queue.append((nr, nc))
    return False


def _anchor_matches(clause: str, facts: StageMapFacts) -> list[tuple[int, int, tuple[int, int]]]:
    matches: list[tuple[int, int, tuple[int, int]]] = []
    for m in gp.PAREN_COORD_RE.finditer(clause):
        if gp.ENTITY_FROM_PREFIX_RE.search(clause[: m.start()]):
            continue
        r, c = int(m.group(1)), int(m.group(2))
        if 1 <= r <= HEIGHT and 1 <= c <= WIDTH:
            matches.append((m.start(), m.end(), (r, c)))
    for m in gp.ENTITY_ANCHOR_RE.finditer(clause):
        label = m.group(0).upper()
        if label in {"玩家", "起点"}:
            label = "P"
        if label in facts.uncertain_entities:
            return []
        pt = facts.entities.get(label)
        if pt is not None:
            matches.append((m.start(), m.end(), pt))
    matches.sort(key=lambda item: item[0])
    return matches


def _is_complete_link_text(value: str) -> bool:
    text = value.strip()
    if not text or "\n" in text or "\r" in text or len(text) > 220:
        return False
    if len(gp.SENTENCE_PUNCT_RE.findall(text)) > 1:
        return False
    if text[0] in ",，、;；:：)）]}】" or text[-1] in ",，、;；:：(（[{【":
        return False
    return not gp.TRAILING_CONJ_RE.search(text)


def _find_free_span(
    body: str, needle: str, occupied: list[tuple[int, int]]
) -> tuple[int, int] | None:
    offset = 0
    while needle:
        start = body.find(needle, offset)
        if start < 0:
            return None
        end = start + len(needle)
        if not any(start < oe and end > os for os, oe in occupied):
            return (start, end)
        offset = start + 1
    return None


def extract_coordinate_links(
    body: str,
    grid_or_rows: Grid | Sequence[str],
    candidate_links: Sequence[Mapping[str, Any]] | None = None,
    entity_bindings: Mapping[str, Any] | None = None,
    *,
    limit: int = COORDINATE_LINK_LIMIT,
) -> list[dict[str, Any]]:
    """Filter candidate links and recover grounded movement links from visible prose."""
    visible = str(body or "")
    if not visible.strip():
        return []
    facts = build_stage_facts(grid_or_rows, entity_bindings)
    accepted: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    seen: set[tuple[str, int, int, int, int]] = set()

    def _try_add(text: str, start_pt: tuple[int, int], end_pt: tuple[int, int]) -> None:
        if len(accepted) >= limit or start_pt == end_pt or not _is_complete_link_text(text):
            return
        if not route_exists(facts.grid, start_pt, end_pt):
            return
        anchors = _anchor_matches(text, facts)
        dedup_pts: list[tuple[int, int]] = []
        for _, _, pt in anchors:
            if not dedup_pts or dedup_pts[-1] != pt:
                dedup_pts.append(pt)
        if len(dedup_pts) != 2 or dedup_pts[0] != start_pt or dedup_pts[-1] != end_pt:
            return
        between = text[anchors[0][1] : anchors[-1][0]]
        if gp.STATIC_BETWEEN_RE.search(between) or not gp.MOVEMENT_RE.search(between):
            return
        key = (text, start_pt[0], start_pt[1], end_pt[0], end_pt[1])
        if key in seen:
            return
        span = _find_free_span(visible, text, occupied)
        if span is None:
            return
        occupied.append(span)
        seen.add(key)
        accepted.append(
            {
                "text": text,
                "from": {"row": start_pt[0], "column": start_pt[1]},
                "to": {"row": end_pt[0], "column": end_pt[1]},
            }
        )

    for raw in candidate_links or ():
        if not isinstance(raw, Mapping):
            continue
        src, dst = raw.get("from"), raw.get("to")
        if not isinstance(src, Mapping) or not isinstance(dst, Mapping):
            continue
        sr, sc, er, ec = src.get("row"), src.get("column"), dst.get("row"), dst.get("column")
        if all(isinstance(v, int) and not isinstance(v, bool) for v in (sr, sc, er, ec)):
            _try_add(str(raw.get("text") or "").strip(), (sr, sc), (er, ec))

    for sentence in gp.SENTENCE_SPLIT_RE.split(visible):
        for clause in gp.ROUTE_CLAUSE_SPLIT_RE.split(sentence):
            piece = clause.strip()
            if not piece:
                continue
            collapsed: list[tuple[int, int, tuple[int, int]]] = []
            for s_idx, e_idx, pt in _anchor_matches(piece, facts):
                if collapsed and collapsed[-1][2] == pt:
                    collapsed[-1] = (collapsed[-1][0], max(collapsed[-1][1], e_idx), pt)
                else:
                    collapsed.append((s_idx, e_idx, pt))
            for left, right in zip(collapsed, collapsed[1:], strict=False):
                raw_snip = piece if len(collapsed) == 2 else piece[left[0] : right[1]]
                _try_add(raw_snip.strip(" ,，;；"), left[2], right[2])

    return accepted
