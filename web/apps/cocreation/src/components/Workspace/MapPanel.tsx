import { SokobanMap } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { isLevelValidationError, localizeError, t } from "../../i18n";
import {
  activeCoordinateLink,
  discardDraft,
  draftRows,
  editTile,
  findCoordinateRoute,
  isDirty,
  validationError,
} from "../../store/editorStore";
import {
  binaryFlowActive,
  busy,
  canEditSelected,
  currentPendingProposal,
  isDeadlineExpired,
  isHistoricalStage,
  language,
  selectedVersion,
  selectedVersionId,
  session,
  unityPlayActive,
} from "../../store/sessionStore";
import {
  playSelectedStage,
  restoreSelectedStage,
  saveManualStage,
} from "../../store/workflows";
import { TechnicalDetails } from "../Common/TechnicalDetails";
import { hideTooltip, showTooltip, tooltipHandlers } from "../Common/Tooltip";
import { FinalActions } from "./FinalActions";
import { IntentionForm } from "./IntentionForm";
import { PlayAttempts } from "./PlayAttempts";
import { TilePalette } from "./TilePalette";

interface RouteOverlayGeometry {
  width: number;
  height: number;
  points: string;
}

export function MapPanel(): JSX.Element | null {
  const s = session.value;
  const version = selectedVersion.value;
  const lang = language.value;
  const boardRef = useRef<HTMLDivElement | null>(null);
  const [routeOverlay, setRouteOverlay] = useState<RouteOverlayGeometry | null>(null);

  const activeLink = activeCoordinateLink.value;
  const rows = draftRows.value;
  const vid = selectedVersionId.value;

  useEffect(() => {
    const computeOverlay = () => {
      const board = boardRef.current;
      if (!board || !activeLink || activeLink.versionId !== vid) {
        setRouteOverlay(null);
        return;
      }
      const route = findCoordinateRoute(rows, activeLink.from, activeLink.to);
      if (!route) {
        activeCoordinateLink.value = null;
        setRouteOverlay(null);
        return;
      }
      const boardRect = board.getBoundingClientRect();
      if (!boardRect.width || !boardRect.height) {
        setRouteOverlay(null);
        return;
      }
      const coords: string[] = [];
      for (const pt of route) {
        const cell = board.querySelector<HTMLElement>(
          `[data-row="${pt.row}"][data-column="${pt.column}"]`,
        );
        if (!cell) {
          setRouteOverlay(null);
          return;
        }
        const rect = cell.getBoundingClientRect();
        coords.push(
          `${rect.left + rect.width / 2 - boardRect.left},${rect.top + rect.height / 2 - boardRect.top}`,
        );
      }
      setRouteOverlay({
        width: boardRect.width,
        height: boardRect.height,
        points: coords.join(" "),
      });
    };

    computeOverlay();
    const frame = window.requestAnimationFrame(computeOverlay);
    window.addEventListener("resize", computeOverlay);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", computeOverlay);
    };
  }, [activeLink, rows, vid]);

  if (!s || !version) return null;

  const expired = isDeadlineExpired.value;
  const editable = canEditSelected.value;
  const interactionBusy = busy.value || unityPlayActive.value;
  const dirty = isDirty.value;
  const pendingProposal = Boolean(currentPendingProposal.value);
  const historical = isHistoricalStage.value;
  const valError = validationError.value;
  const failedValidation = dirty && isLevelValidationError(valError);

  const canDiscardDraft =
    s.status === "active" && !unityPlayActive.value && !historical;
  const binaryBlocked =
    binaryFlowActive.value || s.proposalFlowState?.status === "planning";

  const playDisabled = interactionBusy || expired || dirty || pendingProposal;
  const saveDisabled = !editable || !dirty || interactionBusy || binaryBlocked;
  const discardDisabled = !canDiscardDraft || !dirty || interactionBusy;
  const restoreVisible = historical && s.status === "active";
  const restoreDisabled = interactionBusy || expired;

  const commonBlockHint = expired
    ? t(lang, "deadlineExpiredAnnouncement")
    : s.status !== "active"
      ? t(lang, "hint.closed")
      : unityPlayActive.value
        ? t(lang, "hint.playActive")
        : "";

  const binaryBlockHint = binaryBlocked
    ? dirty
      ? t(lang, "hint.binaryDraftReset")
      : t(lang, "hint.binaryProposal")
    : "";

  const playHint = playDisabled
    ? (interactionBusy || expired ? commonBlockHint : "") ||
      (dirty ? t(lang, "hint.dirtyPlay") : pendingProposal ? t(lang, "hint.pending") : "")
    : "";

  const saveHint = saveDisabled
    ? commonBlockHint || (historical ? t(lang, "hint.history") : binaryBlockHint)
    : "";

  const mapDisabled = !editable || interactionBusy || binaryBlocked;
  const mapBlockedStatus =
    commonBlockHint ||
    (historical
      ? t(lang, "hint.history")
      : binaryBlockHint || (busy.value ? t(lang, "hint.busy") : ""));

  const modeText =
    (s.status !== "active"
      ? t(lang, "lockedMode")
      : historical
        ? t(lang, "readOnlyMode")
        : t(lang, "editMode")) + (dirty ? ` · ${t(lang, "unsaved")}` : "");

  const tileNameMap: Record<string, string> = {
    floor: t(lang, "floor"),
    wall: t(lang, "wall"),
    water: t(lang, "water"),
    player: t(lang, "player"),
    box: t(lang, "box"),
    target: t(lang, "target"),
    empty: t(lang, "erase"),
  };

  return (
    <aside class="panel map-panel" aria-labelledby="mapTitle">
      <header class="panel-header">
        <div>
          <p class="eyebrow">
            {t(lang, "stage").toUpperCase()} {version.stageNumber}
          </p>
          <h2 id="mapTitle">{t(lang, "currentLevel")}</h2>
        </div>
        <div class="play-control">
          <button
            class="play-button"
            type="button"
            aria-describedby={playHint ? "playControlHint" : undefined}
            disabled={playDisabled}
            aria-disabled={playDisabled ? "true" : undefined}
            onClick={() => void playSelectedStage()}
            {...tooltipHandlers({
              kind: "action",
              helpKey: "play",
              titleKey: "playStage",
              status: playDisabled ? playHint || undefined : undefined,
            })}
          >
            <span class="play-icon" aria-hidden="true">
              ▶
            </span>
            <span>{t(lang, "playStage")}</span>
          </button>
          {playHint ? (
            <p class="control-hint" id="playControlHint" role="status">
              {playHint}
            </p>
          ) : null}
        </div>
      </header>

      <div class="map-content">
        <section class="map-preview-section" aria-labelledby="mapTitle">
          <div class="map-frame">
            <div class="map-board" ref={boardRef}>
              <SokobanMap
                rows={rows}
                mode="editor"
                diff={version.diff}
                disabled={mapDisabled}
                tileNameMap={tileNameMap}
                className="map-grid"
                onTileClick={({ x, y }) => editTile(x, y)}
                onTileInspect={(info) => {
                  if (!info) {
                    hideTooltip();
                    return;
                  }
                  showTooltip(
                    info.anchor,
                    {
                      kind: "tile",
                      tile: info.tile,
                      mode: "map",
                      row: info.coord.y + 1,
                      column: info.coord.x + 1,
                      entityLabel: info.entityLabel,
                      disabled: mapDisabled,
                      status: mapDisabled ? mapBlockedStatus || undefined : undefined,
                    },
                    info.immediate,
                  );
                }}
              />
              <svg
                class="map-overlay"
                aria-hidden="true"
                focusable="false"
                viewBox={routeOverlay ? `0 0 ${routeOverlay.width} ${routeOverlay.height}` : undefined}
              >
                {routeOverlay ? (
                  <>
                    <defs>
                      <marker
                        id="coordinate-route-arrowhead"
                        viewBox="0 0 20 20"
                        refX="18"
                        refY="10"
                        markerWidth="20"
                        markerHeight="20"
                        orient="auto"
                        markerUnits="userSpaceOnUse"
                      >
                        <path
                          d="M 1 1 L 19 10 L 1 19 L 5.5 10 Z"
                          fill="#e03131"
                          stroke="#ffffff"
                          stroke-width="1.5"
                          stroke-linejoin="round"
                        />
                      </marker>
                    </defs>
                    <polyline
                      points={routeOverlay.points}
                      fill="none"
                      stroke="#e03131"
                      stroke-width="5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      stroke-dasharray="7 5"
                      marker-end="url(#coordinate-route-arrowhead)"
                    />
                  </>
                ) : null}
              </svg>
            </div>
          </div>

          <p class="map-entity-legend">{t(lang, "entityLegend")}</p>

          <div
            class={`validation-card${failedValidation ? " invalid" : ""}`}
            role={failedValidation ? "alert" : "status"}
          >
            {failedValidation ? (
              <>
                <strong>✗ {t(lang, "validationFailed")}</strong>
                <span>{localizeError(lang, valError)}</span>
                <TechnicalDetails error={valError} />
              </>
            ) : (
              <>
                <strong>✓ {t(lang, dirty ? "previousVerified" : "verified")}</strong>
                {dirty ? <span>{t(lang, "draftUnchecked")}</span> : null}
                <span>
                  {t(lang, dirty ? "savedSolution" : "solutionSummary", {
                    steps: version.validation?.solutionSteps ?? 0,
                    pushes: version.validation?.solutionPushes ?? 0,
                  })}
                </span>
                <TechnicalDetails
                  plainText={t(lang, "searchedStates", {
                    count: version.validation?.searchedStates ?? 0,
                  })}
                />
              </>
            )}
          </div>
        </section>

        <section class="editor-section" aria-labelledby="editorSectionTitle">
          <div class="editor-section-heading">
            <h3 id="editorSectionTitle">
              <svg class="map-section-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="m16 3 5 5-12 12-6 1 1-6Z M13 6l5 5" />
              </svg>
              <span>{t(lang, "editorTitle")}</span>
            </h3>
            <details class="contextual-help editor-help">
              <summary>{t(lang, "editorHelpTitle")}</summary>
              <p>{t(lang, "editorHelpBody")}</p>
            </details>
          </div>
          <div class="map-mode">{modeText}</div>
          <TilePalette />
          <div class="map-save-block">
            <div class="map-actions">
              <button
                class="primary-button"
                type="button"
                aria-describedby="saveStageHint mapControlHint"
                disabled={saveDisabled}
                aria-disabled={saveDisabled ? "true" : undefined}
                onClick={() => void saveManualStage()}
                {...tooltipHandlers({
                  kind: "action",
                  helpKey: "saveStage",
                  titleKey: "saveStage",
                  status: saveDisabled
                    ? saveHint || (!dirty ? t(lang, "hint.noChanges") : undefined)
                    : undefined,
                })}
              >
                <svg class="map-section-icon" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M5 3h12l4 4v14H3V3Z M7 3v7h10V3 M7 21v-8h10v8" />
                </svg>
                <span>{t(lang, "saveStage")}</span>
              </button>
              <button
                class="secondary-button"
                type="button"
                disabled={discardDisabled}
                aria-disabled={discardDisabled ? "true" : undefined}
                onClick={discardDraft}
                {...tooltipHandlers({
                  kind: "action",
                  helpKey: "discardDraft",
                  titleKey: "discard",
                  status: discardDisabled && !dirty ? t(lang, "hint.noChanges") : undefined,
                })}
              >
                {t(lang, "discard")}
              </button>
              {restoreVisible ? (
                <button
                  class="secondary-button restore-stage-button"
                  type="button"
                  disabled={restoreDisabled}
                  aria-disabled={restoreDisabled ? "true" : undefined}
                  onClick={() => void restoreSelectedStage()}
                  {...tooltipHandlers({
                    kind: "action",
                    helpKey: "restoreStage",
                    titleKey: "continueFromStage",
                    status: restoreDisabled ? commonBlockHint || undefined : undefined,
                  })}
                >
                  {t(lang, "continueFromStage")}
                </button>
              ) : null}
            </div>
            <p class="interface-hint save-explanation" id="saveStageHint">
              {t(lang, "saveHint")}
            </p>
            {saveHint ? (
              <p class="control-hint" id="mapControlHint" role="status">
                {saveHint}
              </p>
            ) : null}
          </div>
        </section>

        <PlayAttempts attempts={version.playAttempts || []} />
        <FinalActions />
        <IntentionForm />
      </div>
    </aside>
  );
}
