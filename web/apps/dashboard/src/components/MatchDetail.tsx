import type { JSX } from "preact";
import {
  clean,
  formatTimestamp,
  statusClass,
  statusLabel,
} from "../format";
import type { DashboardStore } from "../store";
import { ChallengeCompare } from "./ChallengeCompare";
import { ChallengeMapPanel } from "./ChallengeMapPanel";
import { CopyIdControl } from "./CopyIdControl";
import { PlayerFlowTimeline } from "./PlayerFlowTimeline";
import { StageInspector } from "./StageInspector";

export interface MatchDetailProps {
  store: DashboardStore;
}

export function MatchDetail({ store }: MatchDetailProps): JSX.Element {
  const match = store.selectedMatch.value;

  if (!match) {
    return (
      <section class="detail-shell">
        <div class="stone-panel empty-detail">
          <div class="empty-illustration" aria-hidden="true">
            ↔
          </div>
          <h2>Select a match</h2>
          <p>
            Choose a room on the left to inspect players, submitted challenges,
            results, and questionnaires.
          </p>
        </div>
      </section>
    );
  }

  const challenges = (match.players ?? []).filter((p) =>
    Boolean(p.challenge),
  ).length;
  const results = (match.players ?? []).filter((p) => Boolean(p.result)).length;

  return (
    <section class="detail-shell">
      <div class="journey-detail">
        <section class="stone-panel match-overview">
          <div class="match-heading">
            <div class="match-heading-main">
              <div class="heading-line">
                <h2>ROOM {clean(match.roomCode) || "------"}</h2>
                <span class={`status-chip ${statusClass(match.status)}`}>
                  {statusLabel(match.status)}
                </span>
              </div>

              <div class="match-id-line">
                <CopyIdControl
                  label="Match ID"
                  rawValue={match.matchId}
                  prefix="Match ID "
                />
              </div>

              <div class="meta-line">
                <span>{match.playerCount} players</span>
                <span>{challenges}/2 challenges</span>
                <span>{results}/2 results</span>
                <span>Created {formatTimestamp(match.createdAt)}</span>
              </div>
            </div>

            <button
              type="button"
              class="pixel-btn pixel-btn--danger delete-match-button"
              onClick={() => store.openDeleteMatch(match)}
            >
              Delete match
            </button>
          </div>
        </section>

        <PlayerFlowTimeline store={store} />

        <section class="detail-grid">
          <ChallengeMapPanel store={store} />
          <StageInspector store={store} />
        </section>

        <ChallengeCompare store={store} />
      </div>
    </section>
  );
}
