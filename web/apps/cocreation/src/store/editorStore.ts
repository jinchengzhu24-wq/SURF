import { computed, signal } from "@preact/signals";
import { MAP_HEIGHT, MAP_WIDTH, TILE_ORDER, type SokobanTile } from "@sokoban/ui-core";
import { t } from "../i18n";
import { localStore, storageKeys, type UnsavedMapDraft } from "../storage/safeStorage";
import type {
  CoordinateLink,
  CoordinateLinkSelection,
  CoordinatePoint,
  StageVersion,
  StructuredErrorLike,
} from "../types";
import {
  busy,
  canEditSelected,
  language,
  selectedVersion,
  selectedVersionId,
  session,
  sessionId,
  showGlobalNotice,
} from "./sessionStore";

export const draftRows = signal<string[]>([]);
export const selectedTile = signal<SokobanTile>(".");
export const validationError = signal<StructuredErrorLike | null>(null);
export const activeCoordinateLink = signal<CoordinateLinkSelection | null>(null);

export const isDirty = computed<boolean>(() => {
  const version = selectedVersion.value;
  const rows = draftRows.value;
  if (!version || rows.length !== version.rows.length) return false;
  return rows.some((row, index) => row !== version.rows[index]);
});

const ROUTE_DIRECTIONS: ReadonlyArray<readonly [number, number]> = [
  [-1, 0],
  [1, 0],
  [0, -1],
  [0, 1],
];

export function normalizeCoordinatePoint(
  point: CoordinatePoint | null | undefined,
): CoordinatePoint | null {
  if (!point || !Number.isInteger(point.row) || !Number.isInteger(point.column)) return null;
  if (point.row < 1 || point.row > MAP_HEIGHT || point.column < 1 || point.column > MAP_WIDTH) {
    return null;
  }
  return { row: point.row, column: point.column };
}

/**
 * Pure BFS route finder between two 1-based `(row, column)` cells on the 12x10 map.
 */
export function findCoordinateRoute(
  rows: readonly string[],
  from: CoordinatePoint | null | undefined,
  to: CoordinatePoint | null | undefined,
): CoordinatePoint[] | null {
  if (
    !Array.isArray(rows) ||
    rows.length !== MAP_HEIGHT ||
    rows.some((row) => typeof row !== "string" || row.length !== MAP_WIDTH)
  ) {
    return null;
  }
  const source = normalizeCoordinatePoint(from);
  const destination = normalizeCoordinatePoint(to);
  if (!source || !destination || (source.row === destination.row && source.column === destination.column)) {
    return null;
  }

  const start: [number, number] = [source.row - 1, source.column - 1];
  const end: [number, number] = [destination.row - 1, destination.column - 1];
  const startKey = `${start[0]},${start[1]}`;
  const endKey = `${end[0]},${end[1]}`;

  const isOpen = (row: number, col: number): boolean => {
    if (row < 0 || row >= rows.length || col < 0 || col >= rows[row].length) return false;
    const tile = rows[row][col];
    if (tile === " " || tile === "#" || tile === "@") return false;
    const key = `${row},${col}`;
    return tile !== "s" || key === startKey || key === endKey;
  };

  if (!isOpen(start[0], start[1]) || !isOpen(end[0], end[1])) return null;

  const queue: Array<[number, number]> = [start];
  const previous = new Map<string, string | null>([[startKey, null]]);
  let cursor = 0;

  while (cursor < queue.length) {
    const [row, col] = queue[cursor++];
    const currentKey = `${row},${col}`;
    if (currentKey === endKey) break;

    for (const [dRow, dCol] of ROUTE_DIRECTIONS) {
      const nextRow = row + dRow;
      const nextCol = col + dCol;
      const nextKey = `${nextRow},${nextCol}`;
      if (!isOpen(nextRow, nextCol) || previous.has(nextKey)) continue;
      previous.set(nextKey, currentKey);
      queue.push([nextRow, nextCol]);
    }
  }

  if (!previous.has(endKey)) return null;
  const path: CoordinatePoint[] = [];
  let key: string | null = endKey;
  while (key !== null) {
    const [row, col] = key.split(",").map(Number);
    path.push({ row: row + 1, column: col + 1 });
    key = previous.get(key) ?? null;
  }
  return path.reverse();
}

export function resetDraftFromVersion(version: StageVersion | null = selectedVersion.value): void {
  draftRows.value = version ? version.rows.slice() : [];
  validationError.value = null;
}

export function clearUnsavedMapDraft(sid: string = sessionId.value): void {
  if (sid) {
    localStore.remove(storageKeys.unsavedMap(sid));
  }
}

export function persistUnsavedMapDraft(): void {
  const sid = sessionId.value;
  const currentVersionId = session.value?.currentVersionId;
  if (!sid || !currentVersionId || !isDirty.value) {
    clearUnsavedMapDraft(sid);
    return;
  }
  const snapshot: UnsavedMapDraft = {
    sessionId: sid,
    baseVersionId: currentVersionId,
    rows: draftRows.value.slice(),
    dirty: true,
    savedAt: new Date().toISOString(),
  };
  localStore.set(storageKeys.unsavedMap(sid), snapshot);
}

export function restoreUnsavedMapDraft(): boolean {
  const sid = sessionId.value;
  const s = session.value;
  if (!sid || !s?.currentVersionId || selectedVersionId.value !== s.currentVersionId) {
    return false;
  }
  const snapshot = localStore.get<UnsavedMapDraft>(storageKeys.unsavedMap(sid));
  const validRows =
    Array.isArray(snapshot?.rows) &&
    snapshot.rows.length === MAP_HEIGHT &&
    snapshot.rows.every(
      (row) =>
        typeof row === "string" &&
        row.length === MAP_WIDTH &&
        [...row].every((tile) => (TILE_ORDER as readonly string[]).includes(tile)),
    );

  if (
    !snapshot ||
    snapshot.sessionId !== sid ||
    snapshot.baseVersionId !== s.currentVersionId ||
    snapshot.dirty !== true ||
    !validRows
  ) {
    clearUnsavedMapDraft(sid);
    return false;
  }

  const version = selectedVersion.value;
  if (!version) {
    clearUnsavedMapDraft(sid);
    return false;
  }

  const restored = snapshot.rows.slice();
  const changed = restored.some((row, idx) => row !== version.rows[idx]);
  if (!changed) {
    clearUnsavedMapDraft(sid);
    return false;
  }

  draftRows.value = restored;
  validationError.value = null;
  showGlobalNotice(t(language.value, "unsavedDraftRestored"));
  return true;
}

export function editTile(x: number, y: number): void {
  if (!canEditSelected.value || busy.value) return;
  const currentRows = draftRows.value;
  if (y < 0 || y >= currentRows.length) return;
  const rowChars = [...currentRows[y]];
  if (x < 0 || x >= rowChars.length) return;

  activeCoordinateLink.value = null;
  rowChars[x] = selectedTile.value;
  const nextRows = currentRows.slice();
  nextRows[y] = rowChars.join("");
  draftRows.value = nextRows;
  validationError.value = null;
  persistUnsavedMapDraft();
}

export function discardDraft(): void {
  activeCoordinateLink.value = null;
  resetDraftFromVersion();
  clearUnsavedMapDraft();
}

export function toggleCoordinateLink(turnId: string, linkIndex: number, link: CoordinateLink): void {
  const active = activeCoordinateLink.value;
  const vid = selectedVersionId.value;
  const sameLink =
    active &&
    active.versionId === vid &&
    active.turnId === turnId &&
    active.linkIndex === linkIndex;

  if (sameLink) {
    activeCoordinateLink.value = null;
    return;
  }
  if (findCoordinateRoute(draftRows.value, link.from, link.to)) {
    activeCoordinateLink.value = {
      versionId: vid,
      turnId,
      linkIndex,
      from: { ...link.from },
      to: { ...link.to },
    };
  } else {
    activeCoordinateLink.value = null;
  }
}
