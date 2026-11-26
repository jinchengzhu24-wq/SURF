import {
  buildEntityLabels,
  formatEntityCoordinate,
  getTileClass,
} from "@sokoban/ui-core";
import type { JSX } from "preact";
import { t, type TranslationKey } from "../../i18n";
import { translationInProgress } from "../../store/chatStore";
import {
  isDeadlineExpired,
  language,
  selectedVersionId,
  session,
  unityPlayActive,
} from "../../store/sessionStore";
import { selectVersion } from "../../store/workflows";
import type { PlayAttempt, StageVersion } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

export function formatAttemptText(lang: "en" | "zh-CN", attempt: PlayAttempt): string {
  const statusKey: TranslationKey =
    attempt.status === "completed"
      ? "attemptCompleted"
      : attempt.status === "abandoned"
        ? "attemptAbandoned"
        : attempt.status === "interrupted" || attempt.status === "expired"
          ? "attemptInterrupted"
          : "attemptStarted";
  return `${t(lang, statusKey)} · ${attempt.moveCount} ${t(lang, "moves")} · ${attempt.pushCount} ${t(lang, "pushes")} · ${attempt.restartCount} ${t(lang, "restarts")} · ${Number(attempt.durationSeconds || 0).toFixed(1)} ${t(lang, "seconds")}`;
}

function StageMiniMap({ version }: { version: StageVersion }): JSX.Element {
  const entityLabels = buildEntityLabels(version.rows);
  const changed = new Set((version.diff || []).map((item) => `${item.x},${item.y}`));

  return (
    <div class="mini-map" style={{ gridTemplateColumns: "repeat(12, 1fr)" }}>
      {version.rows.map((row, y) =>
        [...row].map((tile, x) => {
          const coordKey = `${x},${y}`;
          const entity = entityLabels.get(coordKey);
          const isChanged = changed.has(coordKey);
          return (
            <i
              key={coordKey}
              class={`${getTileClass(tile)}${isChanged ? " changed" : ""}`}
              data-entity-id={entity}
              role={entity ? "img" : undefined}
              aria-label={entity ? formatEntityCoordinate(entity, x, y) : undefined}
            />
          );
        }),
      )}
    </div>
  );
}

export function StageSidebar(): JSX.Element {
  const lang = language.value;
  const s = session.value;
  const versions = s ? [...s.versions].reverse() : [];
  const disabled = isDeadlineExpired.value || translationInProgress.value || unityPlayActive.value;
  const disabledReason = isDeadlineExpired.value
    ? t(lang, "deadlineExpiredAnnouncement")
    : unityPlayActive.value
      ? t(lang, "hint.playActive")
      : translationInProgress.value
        ? t(lang, "hint.busy")
        : "";

  return (
    <aside class="panel stage-panel" aria-labelledby="stageTitle">
      <header class="panel-header compact">
        <div>
          <p class="eyebrow">{t(lang, "versionHistory")}</p>
          <h2 id="stageTitle">{t(lang, "stages")}</h2>
          <details class="contextual-help version-help">
            <summary>{t(lang, "versionHelp")}</summary>
            <p>{t(lang, "versionHint")}</p>
          </details>
        </div>
        <span class="count-pill">{versions.length}</span>
      </header>
      <div class="stage-list">
        {versions.map((version) => {
          const isSelected = version.versionId === selectedVersionId.value;
          const isCurrent = version.versionId === s?.currentVersionId;
          const latestAttempt = version.playAttempts?.[0];
          const sourceKey: TranslationKey =
            version.source === "initial" ||
            version.source === "human_edit" ||
            version.source === "llm_accepted" ||
            version.source === "ai_proposal" ||
            version.source === "restored"
              ? version.source
              : "human_edit";

          return (
            <button
              key={version.versionId}
              type="button"
              class={`stage-card${isSelected ? " selected" : ""}${isCurrent ? " current" : ""}`}
              disabled={disabled}
              aria-disabled={disabled ? "true" : undefined}
              onClick={() => selectVersion(version.versionId)}
              {...tooltipHandlers({
                kind: "stage",
                stageNumber: version.stageNumber,
                status: disabled ? disabledReason : undefined,
              })}
            >
              <div class="stage-card-top">
                <strong>
                  {t(lang, "stage")} {version.stageNumber}
                </strong>
                <span>{isCurrent ? t(lang, "current") : ""}</span>
              </div>
              <StageMiniMap version={version} />
              <div class="stage-meta">
                <span>{t(lang, sourceKey)}</span>
                <span>✓ {t(lang, "verified")}</span>
              </div>
              <small>
                {latestAttempt ? formatAttemptText(lang, latestAttempt) : t(lang, "noPlayEvidence")}
              </small>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
