import type { JSX } from "preact";
import { t } from "../../i18n";
import { language } from "../../store/sessionStore";
import type { PlayAttempt } from "../../types";
import { formatAttemptText } from "./StageSidebar";

export interface PlayAttemptsProps {
  attempts: PlayAttempt[];
}

export function PlayAttempts({ attempts }: PlayAttemptsProps): JSX.Element {
  const lang = language.value;

  return (
    <details class="play-evidence" aria-labelledby="playEvidenceTitle">
      <summary class="section-heading">
        <svg class="map-section-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 11a9 9 0 1 1 2 7 M3 5v6h6 M12 7v5l3 2" />
        </svg>
        <span id="playEvidenceTitle">{t(lang, "playEvidence")}</span>
        <span class="attempt-count">{attempts.length}</span>
      </summary>
      <div class="attempt-list">
        {attempts.length === 0
          ? t(lang, "noPlayEvidence")
          : attempts.map((attempt) => (
              <div key={attempt.attemptId} class={`attempt-item ${attempt.status}`}>
                {formatAttemptText(lang, attempt)}
              </div>
            ))}
      </div>
    </details>
  );
}
