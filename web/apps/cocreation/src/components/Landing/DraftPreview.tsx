import { MAP_HEIGHT, buildEntityLabels, getTileClass, getTileLabel } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { en, t, zhCN, type TranslationKey } from "../../i18n";
import {
  demoCreationFailed,
  demoCreationPending,
  draftRegenerationPending,
  landingMode,
  language,
  session,
} from "../../store/sessionStore";
import { hideTooltip, showTooltip } from "../Common/Tooltip";

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

  const isFormal = s ? !s.demoMode : !isDemoLanding;
  const bundle = (lang === "zh-CN" ? zhCN : en) as Record<string, string>;
  const preferredTitleKey = isFormal ? "formalDraftPreviewTitle" : "demoDraftPreviewTitle";
  const previewTitle = Object.prototype.hasOwnProperty.call(bundle, preferredTitleKey)
    ? t(lang, preferredTitleKey as TranslationKey)
    : isFormal
      ? `${t(lang, "draftPreviewTitle")} · ${t(
          lang,
          s?.initialDraftMethod === "partial_completion" ||
            s?.initialDraftMethod === "description_generation"
            ? s.initialDraftMethod
            : "description_generation",
        )}`
      : `${t(lang, "draftPreviewTitle")} · ${t(lang, "algorithm_demo")}`;

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
        <h3 id="draftPreviewTitle">{previewTitle}</h3>
        <span>
          {hasRows
            ? t(lang, "draftGeneration", { generation: preview?.generation || 1 })
            : ""}
        </span>
      </div>
      <div class="draft-preview-stage">
        {hasRows && rows ? (
          <div
            class="draft-preview-grid"
            role="img"
            aria-label={previewTitle}
            onPointerOver={(event) => {
              if (event.pointerType === "touch") return;
              const target = event.target;
              const cell =
                target instanceof Element
                  ? target.closest<HTMLElement>(".draft-preview-cell[data-x][data-y]")
                  : null;
              if (!cell) {
                hideTooltip();
                return;
              }
              if (event.relatedTarget instanceof Node && cell.contains(event.relatedTarget)) return;
              const x = Number(cell.dataset.x);
              const y = Number(cell.dataset.y);
              const tile = cell.dataset.tile;
              if (!Number.isInteger(x) || !Number.isInteger(y) || typeof tile !== "string") return;
              showTooltip(
                cell,
                {
                  kind: "tile",
                  tile,
                  mode: "preview",
                  row: y + 1,
                  column: x + 1,
                  entityLabel: cell.dataset.entityId || undefined,
                },
                false,
              );
            }}
            onPointerLeave={() => hideTooltip()}
          >
            {rows.map((row, y) =>
              [...row].map((tile, x) => {
                const entity = entityLabels.get(`${x},${y}`);
                return (
                  <span
                    key={`${x},${y}`}
                    class={`draft-preview-cell ${getTileClass(tile)}`}
                    data-x={x}
                    data-y={y}
                    data-tile={tile}
                    data-entity-id={entity}
                    aria-hidden="true"
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
