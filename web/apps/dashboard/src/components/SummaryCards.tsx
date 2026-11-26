import type { JSX } from "preact";
import { formatSeconds } from "../format";
import type { DashboardStore } from "../store";

export interface SummaryCardsProps {
  store: DashboardStore;
}

export function SummaryCards({ store }: SummaryCardsProps): JSX.Element {
  const stats = store.summaryStats.value;

  return (
    <section class="summary" aria-label="MatchMaking summary">
      <div class="stat">
        <span>Matches</span>
        <strong>{stats.total}</strong>
        <small>flows reached First Stage</small>
      </div>

      <div class="stat">
        <span>Completed</span>
        <strong>{stats.completed}</strong>
        <small>{stats.completionRate}% completion rate</small>
      </div>

      <div class="stat">
        <span>Average play time</span>
        <strong>{formatSeconds(stats.averageDuration)}</strong>
        <small>across visible runs</small>
      </div>

      <div class="stat stat-alert">
        <span>Needs attention</span>
        <strong>{stats.attention}</strong>
        <small>{stats.dataHealthText}</small>
      </div>
    </section>
  );
}
