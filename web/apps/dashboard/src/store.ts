import { batch, computed, signal, type ReadonlySignal, type Signal } from "@preact/signals";
import type { NoticeVariant } from "@sokoban/ui-core";
import { isAbortError, type DashboardApi } from "./api";
import {
  buildTimelineStages,
  clean,
  compactId,
  formatTimestamp,
  getDeleteErrorMessage,
  hasFirstStage,
  isCompletedMatch,
  numeric,
} from "./format";
import type {
  DeleteActionConfig,
  MatchPlayer,
  MatchRecord,
  MatchmakingRecordsResponse,
  NoticeState,
  SummaryStats,
  TimelineStage,
} from "./types";

export interface DashboardStoreOptions {
  debounceMs?: number;
  windowObj?: Window;
}

export interface DashboardStore {
  readonly api: DashboardApi;
  readonly payload: ReadonlySignal<MatchmakingRecordsResponse | null>;
  readonly loading: ReadonlySignal<boolean>;
  readonly loadError: ReadonlySignal<string | null>;
  readonly statusLine: ReadonlySignal<string>;
  readonly notice: ReadonlySignal<NoticeState | null>;
  readonly searchInput: ReadonlySignal<string>;
  readonly debouncedQuery: ReadonlySignal<string>;
  readonly deleteDialog: ReadonlySignal<DeleteActionConfig | null>;
  readonly deleteBusy: ReadonlySignal<boolean>;
  readonly deleteError: ReadonlySignal<string>;

  readonly allMatches: ReadonlySignal<MatchRecord[]>;
  readonly firstStageMatches: ReadonlySignal<MatchRecord[]>;
  readonly summaryStats: ReadonlySignal<SummaryStats>;
  readonly filteredMatches: ReadonlySignal<MatchRecord[]>;
  readonly selectedMatch: ReadonlySignal<MatchRecord | null>;
  readonly timelineStages: ReadonlySignal<TimelineStage[]>;
  readonly activeStage: ReadonlySignal<TimelineStage | null>;
  readonly challengePlayers: ReadonlySignal<MatchPlayer[]>;
  readonly activeChallengePlayer: ReadonlySignal<MatchPlayer | null>;

  loadData(manual?: boolean): Promise<void>;
  setSearchInput(value: string): void;
  flushSearch(): void;
  clearSearch(): void;
  selectMatch(matchId: string): void;
  selectStage(stage: TimelineStage): void;
  selectChallengePlayer(playerNumber: number): void;
  openDeleteMatch(match: MatchRecord): void;
  openClearAll(): void;
  closeDeleteDialog(): void;
  confirmDelete(password: string): Promise<boolean>;
  showNotice(message: string, variant?: NoticeVariant): void;
  dismissNotice(): void;
  dispose(): void;
}

export function createDashboardStore(
  api: DashboardApi,
  options: DashboardStoreOptions = {},
): DashboardStore {
  const debounceMs = options.debounceMs ?? 180;
  const win =
    options.windowObj ??
    (typeof window !== "undefined" ? window : undefined);

  const payload: Signal<MatchmakingRecordsResponse | null> = signal(null);
  const loading = signal(false);
  const loadError = signal<string | null>(null);
  const statusLine = signal("Preparing dashboard...");
  const notice = signal<NoticeState | null>(null);
  const searchInput = signal("");
  const debouncedQuery = signal("");
  const selectedMatchId = signal("");
  const selectedStageKey = signal("");
  const selectedChallengePlayer = signal(0);
  const deleteDialog = signal<DeleteActionConfig | null>(null);
  const deleteBusy = signal(false);
  const deleteError = signal("");

  let noticeCounter = 0;
  let loadSequence = 0;
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  const allMatches = computed<MatchRecord[]>(
    () => payload.value?.matches ?? [],
  );

  const firstStageMatches = computed<MatchRecord[]>(() =>
    allMatches.value.filter(hasFirstStage),
  );

  const summaryStats = computed<SummaryStats>(() => {
    const visibleMatches = firstStageMatches.value;
    const total = visibleMatches.length;
    const completed = visibleMatches.filter(isCompletedMatch).length;
    const completionRate = total > 0 ? Math.round((completed / total) * 100) : 0;
    const attention = visibleMatches.filter(
      (match) => match.status === "cancelled" || match.status === "expired",
    ).length;
    const malformed = numeric(
      payload.value?.summary?.malformedCount ?? payload.value?.malformedCount,
    );
    const hidden = Math.max(0, allMatches.value.length - total);

    const durations = visibleMatches.flatMap((match) =>
      (match.players ?? [])
        .map((player) => player.result?.durationSeconds)
        .filter(
          (duration): duration is number =>
            typeof duration === "number" && Number.isFinite(duration),
        ),
    );
    const averageDuration =
      durations.length > 0
        ? durations.reduce((sum, duration) => sum + duration, 0) /
          durations.length
        : null;

    const dataHealthText = malformed
      ? `${malformed} malformed records`
      : hidden
        ? `${hidden} rooms without First Stage hidden`
        : attention
          ? `${attention} cancelled or expired`
          : "no data issues";

    return {
      total,
      completed,
      completionRate,
      averageDuration,
      attention,
      malformed,
      hidden,
      dataHealthText,
    };
  });

  const filteredMatches = computed<MatchRecord[]>(() => {
    const query = clean(debouncedQuery.value).toLowerCase();
    const visible = firstStageMatches.value;
    if (!query) {
      return visible;
    }
    return visible.filter((match) => {
      const studySessionIds = (match.players ?? []).flatMap((player) => [
        player.studySessionId ?? "",
        compactId(player.studySessionId),
      ]);
      return [
        match.matchId,
        match.roomCode ?? "",
        compactId(match.matchId),
        ...studySessionIds,
      ]
        .join(" ")
        .toLowerCase()
        .includes(query);
    });
  });

  const selectedMatch = computed<MatchRecord | null>(() => {
    const matches = filteredMatches.value;
    if (matches.length === 0) {
      return null;
    }
    return (
      matches.find((match) => match.matchId === selectedMatchId.value) ??
      matches[0]
    );
  });

  const timelineStages = computed<TimelineStage[]>(() => {
    const match = selectedMatch.value;
    return match ? buildTimelineStages(match) : [];
  });

  const activeStage = computed<TimelineStage | null>(() => {
    const stages = timelineStages.value;
    if (stages.length === 0) {
      return null;
    }
    return (
      stages.find((stage) => stage.key === selectedStageKey.value) ??
      stages[stages.length - 1]
    );
  });

  const challengePlayers = computed<MatchPlayer[]>(() => {
    const match = selectedMatch.value;
    if (!match) return [];
    return (match.players ?? []).filter((player) => Boolean(player.challenge));
  });

  const activeChallengePlayer = computed<MatchPlayer | null>(() => {
    const players = challengePlayers.value;
    if (players.length === 0) return null;
    return (
      players.find(
        (player) => player.playerNumber === selectedChallengePlayer.value,
      ) ?? players[0]
    );
  });

  function syncUrlMatch(matchId: string): void {
    if (!win || !win.location || !win.history) {
      return;
    }
    if (
      win.location.protocol === "file:" ||
      typeof win.history.replaceState !== "function"
    ) {
      return;
    }
    try {
      const loc = win.location;
      const fallbackHref =
        (loc.origin && loc.origin !== "null" ? loc.origin : api.apiBase) +
        (loc.pathname || "/frontend/") +
        (loc.search || "");
      const url = new URL(loc.href || fallbackHref);
      if (matchId) {
        url.searchParams.set("match", matchId);
      } else {
        url.searchParams.delete("match");
      }
      win.history.replaceState(null, "", url.toString());
    } catch {
      // Safely ignore SecurityError or sandboxed history restrictions.
    }
  }

  function restoreSelectionFromUrl(matches: readonly MatchRecord[]): void {
    if (selectedMatchId.value || !win?.location?.search) {
      return;
    }
    try {
      const matchId = clean(new URLSearchParams(win.location.search).get("match"));
      if (
        matchId &&
        matches.some((match) => hasFirstStage(match) && match.matchId === matchId)
      ) {
        selectedMatchId.value = matchId;
      }
    } catch {
      // Ignore malformed search query strings.
    }
  }

  function cancelDebounce(): void {
    if (debounceTimer !== null) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
  }

  function setSearchInput(nextValue: string): void {
    searchInput.value = nextValue;
    cancelDebounce();
    if (debounceMs <= 0) {
      debouncedQuery.value = nextValue;
      return;
    }
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      debouncedQuery.value = searchInput.value;
    }, debounceMs);
  }

  function flushSearch(): void {
    cancelDebounce();
    debouncedQuery.value = searchInput.value;
  }

  function clearSearch(): void {
    cancelDebounce();
    batch(() => {
      searchInput.value = "";
      debouncedQuery.value = "";
    });
  }

  function selectMatch(matchId: string): void {
    if (selectedMatchId.value !== matchId) {
      batch(() => {
        selectedMatchId.value = matchId;
        selectedStageKey.value = "";
        selectedChallengePlayer.value = 0;
      });
    }
    syncUrlMatch(matchId);
  }

  function selectStage(stage: TimelineStage): void {
    batch(() => {
      selectedStageKey.value = stage.key;
      if (stage.record.eventType === "challenge_submitted") {
        selectedChallengePlayer.value = stage.playerNumber;
      }
    });
  }

  function selectChallengePlayer(playerNumber: number): void {
    selectedChallengePlayer.value = playerNumber;
    const match = selectedMatch.value;
    if (match) {
      syncUrlMatch(match.matchId);
    }
  }

  function showNotice(message: string, variant: NoticeVariant = "info"): void {
    noticeCounter += 1;
    notice.value = {
      key: noticeCounter,
      message,
      variant,
    };
  }

  function dismissNotice(): void {
    notice.value = null;
  }

  async function loadData(manual = false): Promise<void> {
    const seq = ++loadSequence;
    batch(() => {
      loading.value = true;
      statusLine.value = manual
        ? "Refreshing MatchMaking records..."
        : "Loading MatchMaking records...";
    });

    try {
      const data = await api.fetchMatchmakingRecords();
      if (seq !== loadSequence) {
        return;
      }
      batch(() => {
        payload.value = data;
        loadError.value = null;
        loading.value = false;
        restoreSelectionFromUrl(data.matches);
        statusLine.value = `Last loaded ${formatTimestamp(data.generatedAt)}`;
      });
    } catch (error) {
      if (seq !== loadSequence || isAbortError(error)) {
        return;
      }
      const message =
        error instanceof Error && error.message
          ? error.message
          : "Unknown error";
      batch(() => {
        loadError.value = message;
        loading.value = false;
        statusLine.value = `Could not load MatchMaking records: ${message}`;
      });
    }
  }

  function openDeleteMatch(match: MatchRecord): void {
    batch(() => {
      deleteError.value = "";
      deleteBusy.value = false;
      deleteDialog.value = {
        kind: "match",
        matchId: match.matchId,
        roomCode: clean(match.roomCode) || "------",
      };
    });
  }

  function openClearAll(): void {
    batch(() => {
      deleteError.value = "";
      deleteBusy.value = false;
      deleteDialog.value = { kind: "clear_all" };
    });
  }

  function closeDeleteDialog(): void {
    if (deleteBusy.value) {
      return;
    }
    batch(() => {
      deleteDialog.value = null;
      deleteError.value = "";
    });
  }

  async function confirmDelete(password: string): Promise<boolean> {
    const config = deleteDialog.value;
    if (!config || deleteBusy.value) {
      return false;
    }

    if (!password) {
      deleteError.value = "Enter the deletion password.";
      return false;
    }

    batch(() => {
      deleteError.value = "";
      deleteBusy.value = true;
      statusLine.value =
        config.kind === "match"
          ? "Deleting match..."
          : "Clearing MatchMaking data...";
    });

    try {
      if (config.kind === "match") {
        await api.deleteOnlineMatch(config.matchId, password);
      } else {
        await api.clearMatchmakingRecords(password);
      }

      batch(() => {
        selectedMatchId.value = "";
        selectedStageKey.value = "";
        selectedChallengePlayer.value = 0;
        deleteBusy.value = false;
        deleteDialog.value = null;
        deleteError.value = "";
      });
      syncUrlMatch("");
      showNotice(
        config.kind === "match"
          ? "Match deleted successfully."
          : "MatchMaking data cleared.",
        "success",
      );
      await loadData(true);
      return true;
    } catch (error) {
      const message = getDeleteErrorMessage(error);
      batch(() => {
        deleteError.value = message;
        deleteBusy.value = false;
        statusLine.value = `Could not delete records: ${message}`;
      });
      return false;
    }
  }

  function dispose(): void {
    cancelDebounce();
    api.abortPendingLoad();
  }

  return {
    api,
    payload,
    loading,
    loadError,
    statusLine,
    notice,
    searchInput,
    debouncedQuery,
    deleteDialog,
    deleteBusy,
    deleteError,
    allMatches,
    firstStageMatches,
    summaryStats,
    filteredMatches,
    selectedMatch,
    timelineStages,
    activeStage,
    challengePlayers,
    activeChallengePlayer,
    loadData,
    setSearchInput,
    flushSearch,
    clearSearch,
    selectMatch,
    selectStage,
    selectChallengePlayer,
    openDeleteMatch,
    openClearAll,
    closeDeleteDialog,
    confirmDelete,
    showNotice,
    dismissNotice,
    dispose,
  };
}
