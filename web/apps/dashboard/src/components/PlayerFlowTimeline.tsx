import type { JSX } from "preact";
import { formatShortDate } from "../format";
import type { DashboardStore } from "../store";
import type { TimelineStage } from "../types";

export interface PlayerFlowTimelineProps {
  store: DashboardStore;
}

export function PlayerFlowTimeline({
  store,
}: PlayerFlowTimelineProps): JSX.Element {
  const stages = store.timelineStages.value;
  const activeStage = store.activeStage.value;

  const grouped = new Map<number, TimelineStage[]>([
    [1, []],
    [2, []],
  ]);
  const roomStages: TimelineStage[] = [];

  for (const stage of stages) {
    const lane = grouped.get(stage.playerNumber);
    if (lane) {
      lane.push(stage);
    } else {
      roomStages.push(stage);
    }
  }

  return (
    <section class="stone-panel timeline-panel">
      <div class="stone-panel-header stone-panel-header--purple compact-header">
        <div>
          <p class="eyebrow">Co-creation records</p>
          <h2>Player flows</h2>
        </div>
        <span class="hint">Select a player node to inspect it</span>
      </div>

      <div class="timeline-scroll">
        {stages.length === 0 ? (
          <div class="empty-state">No match stages recorded.</div>
        ) : (
          <div class="player-flow-list">
            {([1, 2] as const).map((playerNumber) => {
              const playerStages = grouped.get(playerNumber) ?? [];
              return (
                <section class="player-flow" key={playerNumber}>
                  <h3 class="player-flow-title">Player {playerNumber}</h3>
                  <div class="timeline player-flow-timeline">
                    {playerStages.length === 0 ? (
                      <div class="empty-state timeline-empty">
                        No co-creation records.
                      </div>
                    ) : (
                      playerStages.map((stage, index) => {
                        const isSelected = activeStage?.key === stage.key;
                        return (
                          <button
                            key={stage.key}
                            type="button"
                            class={`timeline-node${isSelected ? " selected" : ""}${stage.warning ? " warning" : ""}`}
                            aria-pressed={isSelected ? "true" : "false"}
                            onClick={() => store.selectStage(stage)}
                          >
                            <span class="timeline-dot">
                              {stage.warning ? "!" : String(index + 1)}
                            </span>
                            <span class="timeline-label" title={stage.label}>
                              {stage.label}
                            </span>
                            <span class="timeline-time">
                              {formatShortDate(stage.timestamp)}
                            </span>
                          </button>
                        );
                      })
                    )}
                  </div>
                </section>
              );
            })}

            {roomStages.length > 0 ? (
              <section class="player-flow">
                <h3 class="player-flow-title">Room events</h3>
                <div class="timeline player-flow-timeline">
                  {roomStages.map((stage, index) => {
                    const isSelected = activeStage?.key === stage.key;
                    return (
                      <button
                        key={stage.key}
                        type="button"
                        class={`timeline-node${isSelected ? " selected" : ""}${stage.warning ? " warning" : ""}`}
                        aria-pressed={isSelected ? "true" : "false"}
                        onClick={() => store.selectStage(stage)}
                      >
                        <span class="timeline-dot">
                          {stage.warning ? "!" : String(index + 1)}
                        </span>
                        <span class="timeline-label" title={stage.label}>
                          {stage.label}
                        </span>
                        <span class="timeline-time">
                          {formatShortDate(stage.timestamp)}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </section>
            ) : null}
          </div>
        )}
      </div>
    </section>
  );
}
