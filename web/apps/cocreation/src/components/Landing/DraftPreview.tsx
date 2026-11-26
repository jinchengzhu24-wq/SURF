import { MAP_HEIGHT, buildEntityLabels, getTileClass, getTileLabel } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { t } from "../../i18n";
import {
  demoCreationFailed,
  demoCreationPending,
  draftRegenerationPending,
  landingMode,
  language,
  session,
} from "../../store/sessionStore";
import { tooltipHandlers } from "../Common/Tooltip";

export function DraftPreview(): JSX.Element | null {
  const lang = language.value;
  const s = session.value;
  const preview = s?.draftPreview;

  let rows = preview?.rows;
  if (!rows && s && !s.languageLocked) {
    rows = s.versions.find((v) => v.versionId === s.currentVersionId)?.rows;
  }

  const hasRows = Array.isArray(rows) && rows.length === MAP_HEIGHT;
  const isDemoLanding = landingMode.value.startsWith("demo");
  if (!hasRows && !isDemoLanding) return null;

  const regenerationStatus = preview?.regenerationStatus;
  const loading =
    demoCreationPending.value ||
    draftRegenerationPending.value ||
    regenerationStatus === "pending" ||
    regenerationStatus === "claimed";

  const statusText = loading
    ? t(lang, demoCreationPending.value ? "draftInitialGenerating" : "draftRegenerating")
    : demoCreationFailed.value
      ? t(lang, "draftInitialFailed")
      : regenerationStatus === "failed" ||
          regenerationStatus === "cancelled" ||
          regenerationStatus === "timed_out"
        ? t(lang, "draftRegenerationFailed")
        : t(lang, "draftReady");

  const entityLabels = hasRows && rows ? buildEntityLabels(rows) : new Map<string, string>();

  return (
    <section class="draft-preview" aria-labelledby="draftPreviewTitle">
      <div class="draft-preview-heading">
        <h3 id="draftPreviewTitle">{t(lang, "draftPreviewTitle")}</h3>
        <span>
          {hasRows
            ? t(lang, "draftGeneration", { generation: preview?.generation || 1 })
            : ""}
        </span>
      </div>
      <div class="draft-preview-stage">
        {hasRows && rows ? (
          <div class="draft-preview-grid" role="img" aria-label={t(lang, "draftPreviewTitle")}>
            {rows.map((row, y) =>
              [...row].map((tile, x) => {
                const entity = entityLabels.get(`${x},${y}`);
                return (
                  <span
                    key={`${x},${y}`}
                    class={`draft-preview-cell ${getTileClass(tile)}`}
                    {...tooltipHandlers({
                      kind: "tile",
                      tile,
                      mode: "preview",
                      row: y + 1,
                      column: x + 1,
                      entityLabel: entity,
                    })}
                  >
                    {entity || getTileLabel(tile)}
                  </span>
                );
              }),
            )}
          </div>
        ) : null}

        {loading ? (
          <div
            class={`draft-preview-loading${hasRows ? " over-map" : ""}`}
            role="status"
            aria-live="polite"
          >
            <svg
              class="draft-preview-spinner"
              viewBox="0 0 48 48"
              aria-hidden="true"
              focusable="false"
            >
              <path d="M39 23a15 15 0 1 0-4.4 10.6M39 8v15H24" />
            </svg>
            <span>
              {t(lang, demoCreationPending.value ? "draftInitialGenerating" : "draftRegenerating")}
            </span>
          </div>
        ) : null}
      </div>
      <p class="draft-preview-legend">{t(lang, "draftLegend")}</p>
      <p class="draft-preview-status" role="status" aria-live="polite">
        {statusText}
      </p>
    </section>
  );
}
