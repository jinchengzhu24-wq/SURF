import type { JSX } from "preact";
import {
  clean,
  compactId,
  formatShortDate,
  getMatchDisplayTimestamp,
  statusClass,
  statusLabel,
} from "../format";
import type { DashboardStore } from "../store";

export interface MatchListProps {
  store: DashboardStore;
}

export function MatchList({ store }: MatchListProps): JSX.Element {
  const matches = store.filteredMatches.value;
  const selected = store.selectedMatch.value;
  const isLoading = store.loading.value;
  const error = store.loadError.value;
  const hasAnyFirstStage = store.firstStageMatches.value.length > 0;
  const searchValue = store.searchInput.value;

  const handleSubmit = (event: Event) => {
    event.preventDefault();
    store.flushSearch();
  };

  return (
    <aside class="stone-panel match-sidebar">
      <div class="stone-panel-header">
        <div>
          <p class="eyebrow">MatchMaking flow</p>
          <h2>Match master list</h2>
        </div>
        <span class="count-pill">{matches.length} shown</span>
      </div>

      <form class="sidebar-search" onSubmit={handleSubmit} role="search">
        <div class="search-field">
          <svg
            class="search-icon icon"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input
            type="search"
            autocomplete="off"
            spellcheck={false}
            aria-label="Search room code, Match ID, or Study session ID"
            placeholder="Room code, Match ID, or Session ID"
            value={searchValue}
            onInput={(event) =>
              store.setSearchInput((event.currentTarget as HTMLInputElement).value)
            }
          />
          {searchValue ? (
            <button
              type="button"
              class="search-clear"
              aria-label="Clear search"
              onClick={() => store.clearSearch()}
            >
              ×
            </button>
          ) : null}
        </div>
        <button type="submit" class="pixel-btn search-button">
          Search
        </button>
      </form>

      <div class="match-list" role="list" aria-label="Matches">
        {isLoading && store.payload.value === null ? (
          <div class="empty-state">Loading matches...</div>
        ) : error && store.payload.value === null ? (
          <div class="empty-state">Failed to load matches.</div>
        ) : matches.length === 0 ? (
          <div class="empty-state">
            {hasAnyFirstStage
              ? "No matching First Stage flows."
              : "No flows have reached First Stage."}
          </div>
        ) : (
          matches.map((match) => {
            const isSelected = selected?.matchId === match.matchId;
            const fullMatchId = clean(match.matchId);
            const challenges = (match.players ?? []).filter((p) =>
              Boolean(p.challenge),
            ).length;
            const results = (match.players ?? []).filter((p) =>
              Boolean(p.result),
            ).length;

            return (
              <div class="match-list-entry" role="listitem" key={match.matchId}>
                <button
                  type="button"
                  class={`match-item${isSelected ? " selected" : ""}`}
                  aria-current={isSelected ? "true" : undefined}
                  onClick={() => store.selectMatch(match.matchId)}
                >
                  <div class="match-item-top">
                    <span class="match-room">
                      ROOM {clean(match.roomCode) || "------"}
                    </span>
                    <span class="match-date">
                      {formatShortDate(getMatchDisplayTimestamp(match))}
                    </span>
                  </div>
                  <p
                    class="match-id-snippet"
                    title={fullMatchId ? `Full Match ID: ${fullMatchId}` : undefined}
                  >
                    Match {compactId(match.matchId)}
                  </p>
                  <div class="match-item-bottom">
                    <span class={`mini-status ${statusClass(match.status)}`}>
                      {statusLabel(match.status)}
                    </span>
                    <span>
                      {challenges}/2 challenges · {results}/2 results
                    </span>
                  </div>
                </button>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
