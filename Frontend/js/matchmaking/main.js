import {
    DEFAULT_API_BASE,
    clean,
    compactId,
    createApiClient,
    createDeleteDialogController,
    createDomHelpers,
    createStatusController,
    formatSeconds,
    formatShortDate,
    formatTimestamp,
    numeric,
    safeArray,
    titleCase
} from "../shared/api-utils.js";
import {
    buildTimelineStages,
    createMatchmakingTimelineRenderer,
    dashboardNodeLabel,
    dashboardNodeStatus,
    eventStageLabel,
    findOpeningForStage,
    flowStageLabel,
    formatMode,
    formatNodeMetrics,
    getMatchDisplayTimestamp,
    getTimestamp,
    openingIsConsumed
} from "./nodes-timeline.js";
import {
    buildInspectorRecordRows,
    createMatchmakingInspector,
    formatChallengeMetrics,
    formatChallengeOutcome
} from "./inspector-compare.js";

export {
    buildInspectorRecordRows,
    buildTimelineStages,
    dashboardNodeLabel,
    dashboardNodeStatus,
    eventStageLabel,
    findOpeningForStage,
    flowStageLabel,
    formatChallengeMetrics,
    formatChallengeOutcome,
    formatMode,
    formatNodeMetrics,
    getMatchDisplayTimestamp,
    getTimestamp,
    openingIsConsumed
};

export function hasFirstStage(match) {
    return safeArray(match && match.players).some(player =>
        safeArray(player && player.coCreationFlow).some(event =>
            event && event.eventType === "first_stage"
        )
    );
}

export function isCompletedMatch(match) {
    if (!match || match.status !== "completed") return false;
    const players = safeArray(match.players);
    return [1, 2].every(playerNumber =>
        players.some(player =>
            player && player.playerNumber === playerNumber && player.result
        )
    );
}

export function statusLabel(status) {
    const labels = {
        completed: "Completed",
        in_progress: "In progress",
        cancelled: "Cancelled",
        expired: "Expired"
    };
    return labels[status] || titleCase(status);
}

export function statusClass(status) {
    if (status === "completed") return "completed";
    if (["cancelled", "expired"].includes(status)) return "anomaly";
    return "progress";
}

function collectMatchmakingElements(doc) {
    return {
        dataSource: doc.getElementById("dataSource"),
        notice: doc.getElementById("notice"),
        statusLine: doc.getElementById("statusLine"),
        refreshButton: doc.getElementById("refreshButton"),
        clearButton: doc.getElementById("clearButton"),
        rawLink: doc.getElementById("rawLink"),
        dataLink: doc.getElementById("dataLink"),
        docsLink: doc.getElementById("docsLink"),
        statMatches: doc.getElementById("statMatches"),
        statCompleted: doc.getElementById("statCompleted"),
        statCompletionRate: doc.getElementById("statCompletionRate"),
        statAvg: doc.getElementById("statAvg"),
        statAttention: doc.getElementById("statAttention"),
        statDataHealth: doc.getElementById("statDataHealth"),
        searchInput: doc.getElementById("searchInput"),
        searchButton: doc.getElementById("searchButton"),
        matchCount: doc.getElementById("matchCount"),
        matchList: doc.getElementById("matchList"),
        emptyDetail: doc.getElementById("emptyDetail"),
        matchDetail: doc.getElementById("matchDetail"),
        selectedMatchTitle: doc.getElementById("selectedMatchTitle"),
        selectedMatchStatus: doc.getElementById("selectedMatchStatus"),
        selectedMatchText: doc.getElementById("selectedMatchText"),
        selectedMatchCopy: doc.getElementById("selectedMatchCopy"),
        selectedMatchMeta: doc.getElementById("selectedMatchMeta"),
        deleteMatchButton: doc.getElementById("deleteMatchButton"),
        matchTimeline: doc.getElementById("matchTimeline"),
        challengeCount: doc.getElementById("challengeCount"),
        challengeTabs: doc.getElementById("challengeTabs"),
        mapGrid: doc.getElementById("mapGrid"),
        detailMetrics: doc.getElementById("detailMetrics"),
        inspectorTitle: doc.getElementById("inspectorTitle"),
        inspectorBody: doc.getElementById("inspectorBody"),
        compareContent: doc.getElementById("compareContent"),
        deleteDialog: doc.getElementById("deleteDialog"),
        deleteDialogForm: doc.getElementById("deleteDialogForm"),
        deleteDialogTitle: doc.getElementById("deleteDialogTitle"),
        deleteDialogDescription: doc.getElementById("deleteDialogDescription"),
        deleteDialogScope: doc.getElementById("deleteDialogScope"),
        deletePasswordInput: doc.getElementById("deletePasswordInput"),
        deleteDialogError: doc.getElementById("deleteDialogError"),
        deleteDialogCancel: doc.getElementById("deleteDialogCancel"),
        deleteDialogConfirm: doc.getElementById("deleteDialogConfirm")
    };
}

export function createMatchmakingDashboard({
    window: win,
    document: doc,
    fetch: fetchImpl
} = {}) {
    const windowObj = win || (typeof globalThis.window !== "undefined"
        ? globalThis.window
        : { location: { search: "", protocol: "https:", origin: DEFAULT_API_BASE, hostname: "sokobanaidemo.top", pathname: "/frontend/" } });
    const documentObj = doc || globalThis.document;
    const apiClient = createApiClient({
        fetchImpl,
        locationObj: windowObj.location
    });
    const dom = createDomHelpers({ document: documentObj });

    const state = {
        apiBase: apiClient.apiBase,
        payload: null,
        matches: [],
        filteredMatches: [],
        selectedMatchId: "",
        selectedStageKey: "",
        selectedChallengePlayer: 0,
        pendingDelete: null,
        deleteDialogBusy: false
    };

    const elements = collectMatchmakingElements(documentObj);
    const { setStatus, showNotice } = createStatusController({ elements, window: windowObj });

    let inspector = null;
    const timelineRenderer = createMatchmakingTimelineRenderer({
        document: documentObj,
        elements,
        state,
        onSelectStage(match) {
            renderMatch(match);
        },
        createIdControl(label, rawValue) {
            return inspector.createIdControl(label, rawValue);
        }
    });

    inspector = createMatchmakingInspector({
        document: documentObj,
        window: windowObj,
        elements,
        state,
        timelineRenderer,
        onSelectChallenge(match) {
            renderMatch(match);
            updateUrlSelection(match);
        }
    });

    const deleteDialog = createDeleteDialogController({
        state,
        elements,
        window: windowObj,
        apiClient,
        setStatus,
        showNotice,
        onReload: loadData,
        errorStatusPrefix: "Could not delete records: ",
        defaultSuccessText: "Record deleted successfully.",
        selectOnError: false
    });

    function getFirstStageMatches() {
        return state.matches.filter(hasFirstStage);
    }

    function renderSummary() {
        const summary = (state.payload && state.payload.summary) || {};
        const visibleMatches = getFirstStageMatches();
        const total = visibleMatches.length;
        const completed = visibleMatches.filter(isCompletedMatch).length;
        const attention = visibleMatches.filter(match =>
            ["cancelled", "expired"].includes(match.status)
        ).length;
        const malformed = numeric(summary.malformedCount);
        const hidden = Math.max(0, state.matches.length - visibleMatches.length);
        const durations = visibleMatches.flatMap(match =>
            safeArray(match.players)
                .map(player => player && player.result && player.result.durationSeconds)
                .filter(duration => typeof duration === "number" && !Number.isNaN(duration))
        );
        const averageDuration = durations.length
            ? durations.reduce((sum, duration) => sum + duration, 0) / durations.length
            : null;
        elements.statMatches.textContent = total;
        elements.statCompleted.textContent = completed;
        elements.statCompletionRate.textContent = (total ? Math.round(completed / total * 100) : 0) + "% completion rate";
        elements.statAvg.textContent = formatSeconds(averageDuration);
        elements.statAttention.textContent = attention;
        elements.statDataHealth.textContent = malformed
            ? malformed + " malformed records"
            : hidden
                ? hidden + " rooms without First Stage hidden"
                : attention
                    ? attention + " cancelled or expired"
                    : "no data issues";
    }

    function applyFilters() {
        const query = clean(elements.searchInput && elements.searchInput.value).toLowerCase();
        state.filteredMatches = getFirstStageMatches().filter(match => {
            if (!query) return true;
            const studySessionIds = safeArray(match.players).flatMap(player => [
                player && player.studySessionId,
                compactId(player && player.studySessionId)
            ]);
            return [match.matchId, match.roomCode, compactId(match.matchId), ...studySessionIds]
                .join(" ")
                .toLowerCase()
                .includes(query);
        });
        renderMatchList();
        keepOrSelectMatch();
    }

    function renderMatchList() {
        elements.matchList.textContent = "";
        elements.matchCount.textContent = state.filteredMatches.length + " shown";
        if (!state.filteredMatches.length) {
            elements.matchList.appendChild(dom.emptyNode(
                state.matches.some(hasFirstStage)
                    ? "No matching First Stage flows."
                    : "No flows have reached First Stage."
            ));
            return;
        }

        state.filteredMatches.forEach(match => {
            const button = documentObj.createElement("button");
            button.type = "button";
            button.className = "idea-item";
            if (match.matchId === state.selectedMatchId) button.classList?.add?.("selected");
            const top = documentObj.createElement("div");
            top.className = "idea-item-top";
            top.append(
                dom.textNode("span", "ROOM " + (match.roomCode || "------"), "idea-hash"),
                dom.textNode("span", formatShortDate(getMatchDisplayTimestamp(match)))
            );
            const snippet = dom.textNode("p", "Match " + compactId(match.matchId), "idea-snippet");
            const fullMatchId = clean(match.matchId);
            snippet.title = fullMatchId ? "Full Match ID: " + fullMatchId : "";
            const challenges = safeArray(match.players).filter(player => player.challenge).length;
            const results = safeArray(match.players).filter(player => player.result).length;
            const bottom = documentObj.createElement("div");
            bottom.className = "idea-item-bottom";
            bottom.append(
                dom.textNode("span", statusLabel(match.status), "mini-status " + statusClass(match.status)),
                dom.textNode("span", challenges + "/2 challenges · " + results + "/2 results")
            );
            button.append(top, snippet, bottom);
            button.addEventListener?.("click", () => selectMatch(match.matchId));
            elements.matchList.appendChild(button);
        });
    }

    function keepOrSelectMatch() {
        let match = state.filteredMatches.find(item => item.matchId === state.selectedMatchId);
        if (!match) {
            match = state.filteredMatches[0] || null;
            state.selectedMatchId = match ? match.matchId : "";
            state.selectedStageKey = "";
            state.selectedChallengePlayer = 0;
            renderMatchList();
        }
        if (match) renderMatch(match);
        else showEmptyDetail();
    }

    function selectMatch(matchId) {
        if (state.selectedMatchId !== matchId) {
            state.selectedMatchId = matchId;
            state.selectedStageKey = "";
            state.selectedChallengePlayer = 0;
        }
        renderMatchList();
        const match = getSelectedMatch();
        if (match) {
            renderMatch(match);
            updateUrlSelection(match);
        }
    }

    function renderMatch(match) {
        elements.emptyDetail.hidden = true;
        elements.matchDetail.hidden = false;
        elements.selectedMatchTitle.textContent = "ROOM " + (match.roomCode || "------");
        elements.selectedMatchStatus.textContent = statusLabel(match.status);
        elements.selectedMatchStatus.className = "status-chip " + statusClass(match.status);
        inspector.setMatchIdDisplay(match.matchId);
        elements.selectedMatchMeta.textContent = "";
        const challenges = safeArray(match.players).filter(player => player.challenge).length;
        const results = safeArray(match.players).filter(player => player.result).length;
        [
            match.playerCount + " players",
            challenges + "/2 challenges",
            results + "/2 results",
            "Created " + formatTimestamp(match.createdAt)
        ].forEach(metaText => elements.selectedMatchMeta.appendChild(dom.textNode("span", metaText)));

        const stages = buildTimelineStages(match);
        if (!stages.some(stage => stage.key === state.selectedStageKey)) {
            state.selectedStageKey = stages.length ? stages[stages.length - 1].key : "";
        }
        const challengePlayers = safeArray(match.players).filter(player => player.challenge);
        if (!challengePlayers.some(player => player.playerNumber === state.selectedChallengePlayer)) {
            state.selectedChallengePlayer = challengePlayers.length ? challengePlayers[0].playerNumber : 0;
        }
        timelineRenderer.renderTimeline(match, stages);
        inspector.renderChallengeTabs(match);
        inspector.renderSelectedChallenge(match);
        inspector.renderInspector(match, stages.find(stage => stage.key === state.selectedStageKey) || null);
        inspector.renderComparison(match);
    }

    function deleteSelectedMatch() {
        const match = getSelectedMatch();
        if (!match) return;
        deleteDialog.openDeleteDialog({
            title: "Delete ROOM " + (match.roomCode || "------") + "?",
            description: "This removes the complete MatchMaking record.",
            scope: "Match " + match.matchId,
            confirmLabel: "Delete match",
            endpoint: "/delete-online-match",
            payload: { matchId: match.matchId },
            progressText: "Deleting match...",
            successText: "Match deleted successfully.",
            afterSuccess: resetSelection
        });
    }

    function clearAllRecords() {
        deleteDialog.openDeleteDialog({
            title: "Clear all MatchMaking data?",
            description: "This removes all recorded matches. Survey records are preserved.",
            scope: "Match events, challenge maps, and results",
            confirmLabel: "Clear MatchMaking data",
            endpoint: "/clear-matchmaking-records",
            payload: {},
            progressText: "Clearing MatchMaking data...",
            successText: "MatchMaking data cleared.",
            afterSuccess: resetSelection
        });
    }

    function resetSelection() {
        state.selectedMatchId = "";
        state.selectedStageKey = "";
        state.selectedChallengePlayer = 0;
    }

    function getSelectedMatch() {
        return state.filteredMatches.find(match => match.matchId === state.selectedMatchId) || null;
    }

    function restoreSelectionFromUrl() {
        const search = (windowObj.location && windowObj.location.search) || "";
        const matchId = clean(new URLSearchParams(search).get("match"));
        if (matchId && !state.selectedMatchId && getFirstStageMatches().some(match => match.matchId === matchId)) {
            state.selectedMatchId = matchId;
        }
    }

    function updateUrlSelection(match) {
        if (
            !windowObj.history
            || typeof windowObj.history.replaceState !== "function"
            || (windowObj.location && windowObj.location.protocol === "file:")
        ) {
            return;
        }
        const loc = windowObj.location || {};
        const fallbackHref = ((loc.origin && loc.origin !== "null") ? loc.origin : state.apiBase)
            + (loc.pathname || "/frontend/")
            + (loc.search || "");
        const url = new URL(loc.href || fallbackHref);
        url.searchParams.set("match", match.matchId);
        windowObj.history.replaceState(null, "", url);
    }

    function showEmptyDetail() {
        elements.emptyDetail.hidden = false;
        elements.matchDetail.hidden = true;
    }

    async function loadData(manual) {
        if (deleteDialog.isDashboardAccessLocked()) {
            deleteDialog.requestDashboardAccess();
            return;
        }
        setStatus(manual ? "Refreshing MatchMaking records..." : "Loading MatchMaking records...");

        try {
            state.payload = await apiClient.getJson("/matchmaking-records-data", { cache: "no-store" });
            state.matches = safeArray(state.payload.matches);
            restoreSelectionFromUrl();
            renderSummary();
            applyFilters();
            elements.dataSource.textContent = "API: " + state.apiBase;
            setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
        } catch (error) {
            setStatus("Could not load MatchMaking records: " + error.message);
            elements.matchList.textContent = "";
            elements.matchList.appendChild(dom.emptyNode("Failed to load matches."));
            showEmptyDetail();
        }
    }

    function wireLinksAndEvents() {
        if (elements.rawLink) elements.rawLink.href = apiClient.apiUrl("/matchmaking-records");
        if (elements.dataLink) elements.dataLink.href = apiClient.apiUrl("/matchmaking-records-data");
        if (elements.docsLink) elements.docsLink.href = apiClient.apiUrl("/docs");
        elements.refreshButton?.addEventListener?.("click", () => loadData(true));
        elements.clearButton?.addEventListener?.("click", clearAllRecords);
        elements.searchInput?.addEventListener?.("search", applyFilters);
        elements.searchInput?.addEventListener?.("keydown", event => {
            if (event.key === "Enter") {
                event.preventDefault?.();
                applyFilters();
            }
        });
        elements.searchButton?.addEventListener?.("click", applyFilters);
        elements.selectedMatchCopy?.addEventListener?.("click", () => inspector.copyFullId(elements.selectedMatchCopy));
        elements.deleteMatchButton?.addEventListener?.("click", deleteSelectedMatch);
        elements.deleteDialogForm?.addEventListener?.("submit", deleteDialog.submitDeleteDialog);
        elements.deleteDialogCancel?.addEventListener?.("click", deleteDialog.closeDeleteDialog);
        elements.deleteDialog?.addEventListener?.("cancel", event => {
            event.preventDefault?.();
            if (!state.deleteDialogBusy) deleteDialog.closeDeleteDialog();
        });
    }

    async function init() {
        wireLinksAndEvents();
        if (deleteDialog.requiresDashboardAccess()) {
            deleteDialog.requestDashboardAccess();
            return;
        }
        await loadData(false);
    }

    return {
        state,
        elements,
        apiClient,
        init,
        loadData,
        applyFilters,
        selectMatch,
        renderSummary,
        renderMatchList,
        renderMatch,
        renderTimeline: timelineRenderer.renderTimeline,
        appendDashboardNodeDetails: timelineRenderer.appendDashboardNodeDetails,
        appendDraftDetails: timelineRenderer.appendDraftDetails,
        appendFlowStageDetails: timelineRenderer.appendFlowStageDetails,
        appendFlowTurnDetails: timelineRenderer.appendFlowTurnDetails,
        appendFlowMessageDetails: timelineRenderer.appendFlowMessageDetails,
        renderChallengeTabs: inspector.renderChallengeTabs,
        renderSelectedChallenge: inspector.renderSelectedChallenge,
        renderInspector: inspector.renderInspector,
        renderComparison: inspector.renderComparison,
        deleteSelectedMatch,
        clearAllRecords,
        openDeleteDialog: deleteDialog.openDeleteDialog,
        closeDeleteDialog: deleteDialog.closeDeleteDialog,
        submitDeleteDialog: deleteDialog.submitDeleteDialog
    };
}

if (typeof window !== "undefined" && typeof document !== "undefined" && !window.__SOKOBAN_DISABLE_AUTO_INIT__) {
    void createMatchmakingDashboard({ window, document, fetch: window.fetch.bind(window) }).init();
}
