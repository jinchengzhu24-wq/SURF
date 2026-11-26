import { SokobanMap } from "@sokoban/ui-core";
import type { JSX } from "preact";
import {
  formatChallengeOutcome,
  formatSeconds,
  value,
} from "../format";
import type { DashboardStore } from "../store";
import type { MatchPlayer } from "../types";
import { CopyIdControl } from "./CopyIdControl";

export interface ChallengeCompareProps {
  store: DashboardStore;
}

function CompareCard({ player }: { player: MatchPlayer }): JSX.Element | null {
  const challenge = player.challenge;
  if (!challenge) return null;

  const result = challenge.runResult;
  const hasResult = Boolean(result);
  const rows: ReadonlyArray<{
    label: string;
    value: string;
    copyable?: boolean;
  }> = [
    {
      label: "Study session ID",
      value: player.studySessionId ?? "",
      copyable: true,
    },
    { label: "Message", value: player.designerIntention || "-" },
    {
      label: "Played by",
      value: `Player ${value(challenge.playedByPlayerNumber)}`,
    },
    {
      label: "Outcome",
      value: hasResult
        ? formatChallengeOutcome(result?.outcome || "completed")
        : "-",
    },
    { label: "Play time", value: formatSeconds(result?.durationSeconds) },
    { label: "Restarts", value: value(result?.restartCount) },
    { label: "Moves", value: value(result?.moveCount) },
    { label: "Minimum", value: value(result?.minimumMoves) },
  ];

  return (
    <article class="compare-version">
      <h3>Player {player.playerNumber} challenge</h3>
      <div class="sokoban-map-frame mini-map-frame">
        {challenge.rows.length > 0 ? (
          <SokobanMap
            rows={challenge.rows}
            mini
            mode="readonly"
            label={`Player ${player.playerNumber} challenge comparison mini map`}
          />
        ) : (
          <div class="map-empty">No map rows recorded.</div>
        )}
      </div>

      <table class="delta-table">
        <tbody>
          {rows.map((row) => (
            <tr key={row.label}>
              <td>{row.label}</td>
              <td>
                {row.copyable ? (
                  <CopyIdControl label={row.label} rawValue={row.value} />
                ) : (
                  <span>{row.value}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}

export function ChallengeCompare({
  store,
}: ChallengeCompareProps): JSX.Element {
  const challengePlayers = store.challengePlayers.value;

  return (
    <section class="stone-panel compare-panel">
      <div class="stone-panel-header stone-panel-header--purple compact-header">
        <div>
          <p class="eyebrow">Challenge analysis</p>
          <h2>Compare player challenges</h2>
        </div>
        <span class="hint">Both submitted maps appear side by side</span>
      </div>

      <div class="compare-content">
        {challengePlayers.length !== 2 ? (
          <div class="compare-placeholder">
            Both players must submit challenges before comparison is available.
          </div>
        ) : (
          <div class="compare-grid">
            {challengePlayers.map((player) => (
              <CompareCard player={player} key={player.playerNumber} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
