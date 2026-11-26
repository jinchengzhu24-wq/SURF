import { TILE_ORDER, getTileClass, type SokobanTile } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { t, tileDisplayKey } from "../../i18n";
import { selectedTile } from "../../store/editorStore";
import {
  busy,
  canEditSelected,
  isDeadlineExpired,
  isHistoricalStage,
  language,
  session,
  unityPlayActive,
} from "../../store/sessionStore";
import { tooltipHandlers } from "../Common/Tooltip";

const PALETTE_SYMBOLS: Record<string, string> = {
  ".": "·",
  "#": "",
  "@": "~",
  p: "P",
  s: "B",
  t: "T",
  " ": "×",
};

export function TilePalette(): JSX.Element {
  const lang = language.value;
  const activeTile = selectedTile.value;
  const disabled = !canEditSelected.value || busy.value || unityPlayActive.value;

  const blockedStatus = isDeadlineExpired.value
    ? t(lang, "deadlineExpiredAnnouncement")
    : session.value && session.value.status !== "active"
      ? t(lang, "hint.closed")
      : unityPlayActive.value
        ? t(lang, "hint.playActive")
        : isHistoricalStage.value
          ? t(lang, "hint.history")
          : busy.value
            ? t(lang, "hint.busy")
            : "";

  return (
    <div class="map-toolbar" aria-label={t(lang, "editorTitle")}>
      {TILE_ORDER.map((tile: SokobanTile) => {
        const selected = activeTile === tile;
        const symbol = PALETTE_SYMBOLS[tile] ?? "";
        const name = t(lang, tileDisplayKey(tile));
        return (
          <button
            key={tile}
            type="button"
            class={`palette-button ${getTileClass(tile)}${selected ? " selected" : ""}`}
            aria-pressed={selected ? "true" : "false"}
            disabled={disabled}
            aria-disabled={disabled ? "true" : undefined}
            onClick={() => {
              if (!disabled) selectedTile.value = tile;
            }}
            {...tooltipHandlers({
              kind: "tile",
              tile,
              mode: "palette",
              disabled,
              status: disabled ? blockedStatus : undefined,
            })}
          >
            <span aria-hidden="true">{symbol}</span>
            <small>{name}</small>
          </button>
        );
      })}
    </div>
  );
}
