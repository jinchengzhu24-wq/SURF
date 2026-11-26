import type { JSX } from "preact";

export const MAP_WIDTH = 12;
export const MAP_HEIGHT = 10;
export const TILE_ORDER = [".", "#", "@", "p", "s", "t", " "] as const;

export type SokobanTile = (typeof TILE_SYMBOLS)[number] | (string & {});
const TILE_SYMBOLS = TILE_ORDER;

export interface TileCoord {
  x: number;
  y: number;
}

export function getTileClass(tile: string): string {
  switch (tile) {
    case "#":
      return "tile-wall";
    case "@":
      return "tile-water";
    case "p":
      return "tile-player";
    case "s":
      return "tile-box";
    case "t":
      return "tile-target";
    case ".":
      return "tile-floor";
    default:
      return "tile-empty";
  }
}

export function getTileName(tile: string): string {
  switch (tile) {
    case "#":
      return "wall";
    case "@":
      return "water";
    case "p":
      return "player";
    case "s":
      return "box";
    case "t":
      return "target";
    case ".":
      return "floor";
    default:
      return "empty";
  }
}

export function getTileLabel(tile: string): string {
  switch (tile) {
    case "p":
      return "P";
    case "s":
      return "B";
    case "t":
      return "T";
    case "@":
      return "~";
    default:
      return "";
  }
}

export function formatTileCoordinate(x: number, y: number): string {
  return `(${y + 1}, ${x + 1})`;
}

export function formatEntityCoordinate(
  entityLabel: string | undefined,
  x: number,
  y: number,
): string {
  return entityLabel
    ? `${entityLabel} (row ${y + 1}, column ${x + 1})`
    : formatTileCoordinate(x, y);
}

/**
 * Computes deterministic entity labels (`P`, `B1`, `B2`, `T1`, `T2`) keyed by `"x,y"`.
 */
export function buildEntityLabels(rows: readonly string[]): Map<string, string> {
  const labels = new Map<string, string>();
  const counts = { s: 0, t: 0 };
  rows.forEach((row, y) => {
    const chars = [...String(row || "")];
    chars.forEach((tile, x) => {
      if (tile === "p") {
        labels.set(`${x},${y}`, "P");
      } else if (tile === "s") {
        counts.s += 1;
        labels.set(`${x},${y}`, `B${counts.s}`);
      } else if (tile === "t") {
        counts.t += 1;
        labels.set(`${x},${y}`, `T${counts.t}`);
      }
    });
  });
  return labels;
}

/**
 * Generates an accessible text summary for a 12x10 Sokoban map.
 */
export function summarizeMap(rows: readonly string[]): string {
  if (!rows.length) {
    return "Empty 12 by 10 Sokoban map";
  }

  let players = 0;
  let boxes = 0;
  let targets = 0;
  let walls = 0;
  let water = 0;

  for (let y = 0; y < MAP_HEIGHT; y += 1) {
    const row = String(rows[y] || "");
    for (let x = 0; x < MAP_WIDTH; x += 1) {
      const tile = row[x] || " ";
      if (tile === "p") players += 1;
      else if (tile === "s") boxes += 1;
      else if (tile === "t") targets += 1;
      else if (tile === "#") walls += 1;
      else if (tile === "@") water += 1;
    }
  }

  return `${MAP_WIDTH} by ${MAP_HEIGHT} Sokoban map: ${players} player, ${boxes} boxes, ${targets} targets, ${walls} walls, ${water} water tiles`;
}

export interface TileInspectInfo {
  anchor: HTMLElement;
  coord: TileCoord;
  tile: string;
  entityLabel?: string;
  immediate: boolean;
}

export interface SokobanMapProps {
  rows: readonly string[];
  mode?: "readonly" | "editor";
  diff?: readonly TileCoord[];
  mini?: boolean;
  disabled?: boolean;
  label?: string;
  className?: string;
  onTileClick?: (coord: TileCoord, currentTile: string) => void;
  onTileInspect?: (info: TileInspectInfo | null) => void;
  tileNameMap?: Partial<Record<string, string>>;
}

function getCellTile(rows: readonly string[], x: number, y: number): string {
  const row = rows[y];
  if (!row) return " ";
  return row[x] ?? " ";
}

/**
 * Accessible 12x10 Sokoban grid supporting readonly display (`role="img"`) and
 * interactive editor mode using container-level event delegation so tile edits
 * preserve button DOM nodes and keyboard focus.
 */
export function SokobanMap({
  rows,
  mode = "readonly",
  diff,
  mini = false,
  disabled = false,
  label,
  className,
  onTileClick,
  onTileInspect,
  tileNameMap,
}: SokobanMapProps): JSX.Element {
  const entityLabels = buildEntityLabels(rows);
  const changedCoords = new Set(
    (diff || []).map((coord) => `${coord.x},${coord.y}`),
  );
  const resolvedLabel = label || summarizeMap(rows);
  const isEditor = mode === "editor";

  const gridClassName = [
    "sokoban-map",
    isEditor ? "sokoban-map--editor" : "sokoban-map--readonly",
    mini ? "sokoban-map--mini" : "",
    className || "",
  ]
    .filter(Boolean)
    .join(" ");

  const gridStyle = {
    gridTemplateColumns: mini
      ? `repeat(${MAP_WIDTH}, 18px)`
      : `repeat(${MAP_WIDTH}, var(--tile-size, 28px))`,
  };

  const handleGridClick = (event: MouseEvent) => {
    if (!isEditor || disabled || !onTileClick) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest<HTMLButtonElement>("button[data-x][data-y]");
    if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") return;
    const x = Number(button.dataset.x);
    const y = Number(button.dataset.y);
    if (!Number.isInteger(x) || !Number.isInteger(y)) return;
    onTileClick({ x, y }, getCellTile(rows, x, y));
  };

  const handleGridKeyDown = (event: KeyboardEvent) => {
    if (!isEditor) return;
    const target = event.target;
    if (!(target instanceof HTMLButtonElement)) return;
    const x = Number(target.dataset.x);
    const y = Number(target.dataset.y);
    if (!Number.isInteger(x) || !Number.isInteger(y)) return;

    let nextX = x;
    let nextY = y;
    if (event.key === "ArrowRight") nextX = Math.min(MAP_WIDTH - 1, x + 1);
    else if (event.key === "ArrowLeft") nextX = Math.max(0, x - 1);
    else if (event.key === "ArrowDown") nextY = Math.min(MAP_HEIGHT - 1, y + 1);
    else if (event.key === "ArrowUp") nextY = Math.max(0, y - 1);
    else return;

    event.preventDefault();
    const grid = event.currentTarget as HTMLDivElement | null;
    const nextButton = grid?.querySelector<HTMLButtonElement>(
      `button[data-x="${nextX}"][data-y="${nextY}"]`,
    );
    nextButton?.focus();
  };

  const inspectCell = (cell: HTMLElement | null, immediate: boolean) => {
    if (!onTileInspect) return;
    if (!cell) {
      onTileInspect(null);
      return;
    }
    const x = Number(cell.dataset.x);
    const y = Number(cell.dataset.y);
    if (!Number.isInteger(x) || !Number.isInteger(y)) {
      onTileInspect(null);
      return;
    }
    onTileInspect({
      anchor: cell,
      coord: { x, y },
      tile: getCellTile(rows, x, y),
      entityLabel: cell.dataset.entityId || undefined,
      immediate,
    });
  };

  const handleGridPointerOver = (event: PointerEvent) => {
    if (!onTileInspect || event.pointerType === "touch") return;
    const target = event.target;
    const cell = target instanceof Element ? target.closest<HTMLElement>("[data-x][data-y]") : null;
    if (cell && event.relatedTarget instanceof Node && cell.contains(event.relatedTarget)) return;
    inspectCell(cell, false);
  };

  const handleGridPointerLeave = () => {
    onTileInspect?.(null);
  };

  const handleGridFocusIn = (event: FocusEvent) => {
    if (!onTileInspect) return;
    const target = event.target;
    const cell = target instanceof Element ? target.closest<HTMLElement>("[data-x][data-y]") : null;
    inspectCell(cell, true);
  };

  const handleGridFocusOut = (event: FocusEvent) => {
    if (!onTileInspect) return;
    const grid = event.currentTarget as HTMLDivElement | null;
    const related = event.relatedTarget;
    if (grid && related instanceof Node && grid.contains(related)) return;
    onTileInspect(null);
  };

  const cells: JSX.Element[] = [];
  for (let y = 0; y < MAP_HEIGHT; y += 1) {
    for (let x = 0; x < MAP_WIDTH; x += 1) {
      const tile = getCellTile(rows, x, y);
      const coordKey = `${x},${y}`;
      const entityLabel = entityLabels.get(coordKey);
      const isChanged = changedCoords.has(coordKey);
      const cellClass = [
        "tile",
        getTileClass(tile),
        isChanged ? "changed" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const displayLabel = entityLabel || getTileLabel(tile);
      const rawName = getTileName(tile);
      const tileLabelText = tileNameMap?.[rawName] || rawName;
      const cellDescription = entityLabel
        ? formatEntityCoordinate(entityLabel, x, y)
        : `${tileLabelText} ${formatTileCoordinate(x, y)}`;

      if (isEditor) {
        cells.push(
          <button
            key={coordKey}
            type="button"
            class={cellClass}
            data-x={x}
            data-y={y}
            data-row={y + 1}
            data-column={x + 1}
            data-tile={tile}
            data-entity-id={entityLabel}
            aria-label={cellDescription}
            aria-disabled={disabled ? "true" : undefined}
            disabled={disabled && !onTileInspect}
          >
            {displayLabel}
          </button>,
        );
      } else {
        cells.push(
          <span
            key={coordKey}
            class={cellClass}
            data-x={x}
            data-y={y}
            data-row={y + 1}
            data-column={x + 1}
            data-tile={tile}
            data-entity-id={entityLabel}
            title={cellDescription}
            aria-hidden="true"
          >
            {displayLabel}
          </span>,
        );
      }
    }
  }

  return (
    <div
      class={gridClassName}
      style={gridStyle}
      role={isEditor ? "group" : "img"}
      aria-label={resolvedLabel}
      onClick={isEditor ? handleGridClick : undefined}
      onKeyDown={isEditor ? handleGridKeyDown : undefined}
      onPointerOver={onTileInspect ? handleGridPointerOver : undefined}
      onPointerLeave={onTileInspect ? handleGridPointerLeave : undefined}
      onFocusIn={onTileInspect ? handleGridFocusIn : undefined}
      onFocusOut={onTileInspect ? handleGridFocusOut : undefined}
    >
      {cells}
    </div>
  );
}
