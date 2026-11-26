import { SokobanMap } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useId } from "preact/hooks";
import { formatChallengeMetrics } from "../format";
import type { DashboardStore } from "../store";

export interface ChallengeMapPanelProps {
  store: DashboardStore;
}

export function ChallengeMapPanel({
  store,
}: ChallengeMapPanelProps): JSX.Element {
  const players = store.challengePlayers.value;
  const activePlayer = store.activeChallengePlayer.value;
  const activeChallenge = activePlayer?.challenge ?? null;
  const panelId = useId();

  const handleTabKeyDown = (event: KeyboardEvent, currentIndex: number) => {
    if (players.length < 2) return;
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % players.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + players.length) % players.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = players.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const targetPlayer = players[nextIndex];
    store.selectChallengePlayer(targetPlayer.playerNumber);
    const list = (event.currentTarget as HTMLElement).parentElement;
    const nextButton = list?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[
      nextIndex
    ];
    nextButton?.focus();
  };

  return (
    <section class="stone-panel level-panel">
      <div class="stone-panel-header compact-header">
        <div>
          <p class="eyebrow">Generated artifacts</p>
          <h2>Challenge maps</h2>
        </div>
        <span class="count-pill">{players.length} challenges</span>
      </div>

      <div
        class="level-tabs"
        role="tablist"
        aria-label="Submitted player challenges"
      >
        {players.length === 0 ? (
          <div class="empty-state tabs-empty">No challenge maps submitted.</div>
        ) : (
          players.map((player, index) => {
            const isSelected = activePlayer?.playerNumber === player.playerNumber;
            const runResult = player.challenge?.runResult;
            const tabStatus = runResult
              ? runResult.outcome === "timed_out"
                ? "timed out"
                : "played"
              : "pending";
            return (
              <button
                key={player.playerNumber}
                id={`${panelId}-tab-${player.playerNumber}`}
                type="button"
                role="tab"
                class={`level-tab${isSelected ? " selected" : ""}`}
                aria-selected={isSelected ? "true" : "false"}
                aria-controls={panelId}
                tabIndex={isSelected ? 0 : -1}
                onClick={() => store.selectChallengePlayer(player.playerNumber)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
              >
                <span>P{player.playerNumber} challenge</span>
                <span class="level-tab-status">{tabStatus}</span>
              </button>
            );
          })
        )}
      </div>

      <div
        id={panelId}
        class="map-content"
        role="tabpanel"
        aria-labelledby={
          activePlayer
            ? `${panelId}-tab-${activePlayer.playerNumber}`
            : undefined
        }
      >
        <div class="sokoban-map-frame dashboard-map-frame">
          {activeChallenge && activeChallenge.rows.length > 0 ? (
            <SokobanMap
              rows={activeChallenge.rows}
              mode="readonly"
              label={`Player ${activePlayer?.playerNumber} submitted Sokoban challenge map`}
            />
          ) : (
            <div class="map-empty">
              {activeChallenge
                ? "No map rows recorded."
                : "No challenge selected."}
            </div>
          )}
        </div>

        <div class="legend" aria-label="Tile legend">
          <span>
            <i class="swatch tile-floor" aria-hidden="true" />
            floor
          </span>
          <span>
            <i class="swatch tile-wall" aria-hidden="true" />
            wall
          </span>
          <span>
            <i class="swatch tile-water" aria-hidden="true" />
            water
          </span>
          <span>
            <i class="swatch tile-player" aria-hidden="true" />
            player
          </span>
          <span>
            <i class="swatch tile-box" aria-hidden="true" />
            box
          </span>
          <span>
            <i class="swatch tile-target" aria-hidden="true" />
            target
          </span>
        </div>

        {activeChallenge ? (
          <div class="metrics">
            {formatChallengeMetrics(activeChallenge).map(
              ([metricLabel, metricValue]) => (
                <div class="metric" key={metricLabel}>
                  <span>{metricLabel}</span>
                  <strong>{metricValue}</strong>
                </div>
              ),
            )}
          </div>
        ) : null}
      </div>
    </section>
  );
}
