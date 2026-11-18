const state = {
    apiBase: resolveApiBase(),
    payload: null,
    matches: [],
    filteredMatches: [],
    selectedMatchId: "",
    selectedStageKey: "",
    selectedChallengePlayer: 0,
    pendingDelete: null,
    deleteDialogBusy: false
};

const elements = {
    dataSource: document.getElementById("dataSource"),
    notice: document.getElementById("notice"),
    statusLine: document.getElementById("statusLine"),
    refreshButton: document.getElementById("refreshButton"),
    clearButton: document.getElementById("clearButton"),
    rawLink: document.getElementById("rawLink"),
    dataLink: document.getElementById("dataLink"),
    docsLink: document.getElementById("docsLink"),
    statMatches: document.getElementById("statMatches"),
    statCompleted: document.getElementById("statCompleted"),
    statCompletionRate: document.getElementById("statCompletionRate"),
    statAvg: document.getElementById("statAvg"),
    statAttention: document.getElementById("statAttention"),
    statDataHealth: document.getElementById("statDataHealth"),
    searchInput: document.getElementById("searchInput"),
    searchButton: document.getElementById("searchButton"),
    matchCount: document.getElementById("matchCount"),
    matchList: document.getElementById("matchList"),
    emptyDetail: document.getElementById("emptyDetail"),
    matchDetail: document.getElementById("matchDetail"),
    selectedMatchTitle: document.getElementById("selectedMatchTitle"),
    selectedMatchStatus: document.getElementById("selectedMatchStatus"),
    selectedMatchText: document.getElementById("selectedMatchText"),
    selectedMatchCopy: document.getElementById("selectedMatchCopy"),
    selectedMatchMeta: document.getElementById("selectedMatchMeta"),
    deleteMatchButton: document.getElementById("deleteMatchButton"),
    matchTimeline: document.getElementById("matchTimeline"),
    challengeCount: document.getElementById("challengeCount"),
    challengeTabs: document.getElementById("challengeTabs"),
    mapGrid: document.getElementById("mapGrid"),
    detailMetrics: document.getElementById("detailMetrics"),
    inspectorTitle: document.getElementById("inspectorTitle"),
    inspectorBody: document.getElementById("inspectorBody"),
    compareContent: document.getElementById("compareContent"),
    deleteDialog: document.getElementById("deleteDialog"),
    deleteDialogForm: document.getElementById("deleteDialogForm"),
    deleteDialogTitle: document.getElementById("deleteDialogTitle"),
    deleteDialogDescription: document.getElementById("deleteDialogDescription"),
    deleteDialogScope: document.getElementById("deleteDialogScope"),
    deletePasswordInput: document.getElementById("deletePasswordInput"),
    deleteDialogError: document.getElementById("deleteDialogError"),
    deleteDialogCancel: document.getElementById("deleteDialogCancel"),
    deleteDialogConfirm: document.getElementById("deleteDialogConfirm")
};

function init() {
    elements.rawLink.href = apiUrl("/matchmaking-records");
    elements.dataLink.href = apiUrl("/matchmaking-records-data");
    elements.docsLink.href = apiUrl("/docs");
    elements.refreshButton.addEventListener("click", () => loadData(true));
    elements.clearButton.addEventListener("click", clearAllRecords);
    elements.searchInput.addEventListener?.("search", applyFilters);
    elements.searchInput.addEventListener?.("keydown", event => {
        if (event.key === "Enter") {
            event.preventDefault();
            applyFilters();
        }
    });
    elements.searchButton.addEventListener("click", applyFilters);
    elements.selectedMatchCopy.addEventListener("click", () => copyFullId(elements.selectedMatchCopy));
    elements.deleteMatchButton.addEventListener("click", deleteSelectedMatch);
    elements.deleteDialogForm.addEventListener("submit", submitDeleteDialog);
    elements.deleteDialogCancel.addEventListener("click", closeDeleteDialog);
    elements.deleteDialog.addEventListener("cancel", event => {
        event.preventDefault();
        if (!state.deleteDialogBusy) closeDeleteDialog();
    });
    if (requiresDashboardAccess()) {
        requestDashboardAccess();
        return;
    }

    loadData(false);
}

function requiresDashboardAccess() {
    return new URLSearchParams(window.location.search).get("access") === "1";
}

function requestDashboardAccess() {
    openDeleteDialog({
        title: "Dashboard access required",
        description: "Enter the dashboard password to continue.",
        scope: "This opens the 8000 study dashboard.",
        confirmLabel: "Continue",
        endpoint: "/verify-dashboard-password",
        payload: {},
        progressText: "Checking dashboard password...",
        successText: "Dashboard access granted.",
        afterSuccess: clearDashboardAccessParameter
    });
}

function clearDashboardAccessParameter() {
    const url = new URL(window.location.href);
    url.searchParams.delete("access");
    window.history.replaceState(null, "", url.toString());
}

async function loadData(manual) {
    setStatus(manual ? "Refreshing MatchMaking records..." : "Loading MatchMaking records...");

    try {
        const response = await fetch(apiUrl("/matchmaking-records-data"), { cache: "no-store" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        state.payload = await response.json();
        state.matches = safeArray(state.payload.matches);
        restoreSelectionFromUrl();
        renderSummary();
        applyFilters();
        elements.dataSource.textContent = "API: " + state.apiBase;
        setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
    } catch (error) {
        setStatus("Could not load MatchMaking records: " + error.message);
        elements.matchList.innerHTML = '<div class="empty-state">Failed to load matches.</div>';
        showEmptyDetail();
    }
}

function hasFirstStage(match) {
    return safeArray(match && match.players).some(player =>
        safeArray(player && player.coCreationFlow).some(event =>
            event && event.eventType === "first_stage"
        )
    );
}

function getFirstStageMatches() {
    return state.matches.filter(hasFirstStage);
}

function isCompletedMatch(match) {
    if (!match || match.status !== "completed") return false;
    const players = safeArray(match.players);
    return [1, 2].every(playerNumber =>
        players.some(player =>
            player && player.playerNumber === playerNumber && player.result
        )
    );
}

function renderSummary() {
    const summary = state.payload.summary || {};
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
    const query = clean(elements.searchInput.value).toLowerCase();
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
        elements.matchList.appendChild(emptyNode(
            state.matches.some(hasFirstStage)
                ? "No matching First Stage flows."
                : "No flows have reached First Stage."
        ));
        return;
    }

    state.filteredMatches.forEach(match => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "idea-item";
        if (match.matchId === state.selectedMatchId) button.classList.add("selected");
        const top = document.createElement("div");
        top.className = "idea-item-top";
        top.append(
            textNode("span", "ROOM " + (match.roomCode || "------"), "idea-hash"),
            textNode("span", formatShortDate(getMatchDisplayTimestamp(match)))
        );
        const snippet = textNode("p", "Match " + compactId(match.matchId), "idea-snippet");
        const fullMatchId = clean(match.matchId);
        snippet.title = fullMatchId ? "Full Match ID: " + fullMatchId : "";
        const challenges = safeArray(match.players).filter(player => player.challenge).length;
        const results = safeArray(match.players).filter(player => player.result).length;
        const bottom = document.createElement("div");
        bottom.className = "idea-item-bottom";
        bottom.append(
            textNode("span", statusLabel(match.status), "mini-status " + statusClass(match.status)),
            textNode("span", challenges + "/2 challenges · " + results + "/2 results")
        );
        button.append(top, snippet, bottom);
        button.addEventListener("click", () => selectMatch(match.matchId));
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
    setMatchIdDisplay(match.matchId);
    elements.selectedMatchMeta.textContent = "";
    const challenges = safeArray(match.players).filter(player => player.challenge).length;
    const results = safeArray(match.players).filter(player => player.result).length;
    [
        match.playerCount + " players",
        challenges + "/2 challenges",
        results + "/2 results",
        "Created " + formatTimestamp(match.createdAt)
    ].forEach(value => elements.selectedMatchMeta.appendChild(textNode("span", value)));

    const stages = buildTimelineStages(match);
    if (!stages.some(stage => stage.key === state.selectedStageKey)) {
        state.selectedStageKey = stages.length ? stages[stages.length - 1].key : "";
    }
    const challengePlayers = safeArray(match.players).filter(player => player.challenge);
    if (!challengePlayers.some(player => player.playerNumber === state.selectedChallengePlayer)) {
        state.selectedChallengePlayer = challengePlayers.length ? challengePlayers[0].playerNumber : 0;
    }
    renderTimeline(match, stages);
    renderChallengeTabs(match);
    renderSelectedChallenge(match);
    renderInspector(match, stages.find(stage => stage.key === state.selectedStageKey) || null);
    renderComparison(match);
}

function deleteSelectedMatch() {
    const match = getSelectedMatch();
    if (!match) return;
    openDeleteDialog({
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
    openDeleteDialog({
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

async function submitDeleteDialog(event) {
    event.preventDefault();
    const config = state.pendingDelete;
    const password = elements.deletePasswordInput.value;
    if (!config || state.deleteDialogBusy) return;
    if (!password) {
        elements.deleteDialogError.textContent = "Enter the deletion password.";
        elements.deletePasswordInput.focus();
        return;
    }
    setDeleteDialogBusy(true);
    setStatus(config.progressText);
    try {
        const response = await fetch(apiUrl(config.endpoint), {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Delete-Password": password
            },
            body: JSON.stringify(config.payload)
        });
        if (!response.ok) throw Object.assign(new Error(await getResponseError(response)), { status: response.status });
        if (config.afterSuccess) config.afterSuccess();
        setDeleteDialogBusy(false);
        closeDeleteDialog();
        showNotice(config.successText);
        await loadData(true);
    } catch (error) {
        elements.deleteDialogError.textContent = getDeleteDialogError(error);
        setStatus("Could not delete records: " + elements.deleteDialogError.textContent);
        setDeleteDialogBusy(false);
    }
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
    const matchId = clean(new URLSearchParams(window.location.search).get("match"));
    if (matchId && !state.selectedMatchId && getFirstStageMatches().some(match => match.matchId === matchId)) {
        state.selectedMatchId = matchId;
    }
}

function updateUrlSelection(match) {
    if (!window.history || window.location.protocol === "file:") return;
    const url = new URL(window.location.href);
    url.searchParams.set("match", match.matchId);
    window.history.replaceState(null, "", url);
}

function showEmptyDetail() {
    elements.emptyDetail.hidden = false;
    elements.matchDetail.hidden = true;
}

function getMatchDisplayTimestamp(match) {
    return clean(match && (match.createdAt || match.updatedAt));
}

function statusLabel(status) {
    const labels = {
        completed: "Completed",
        in_progress: "In progress",
        cancelled: "Cancelled",
        expired: "Expired"
    };
    return labels[status] || titleCase(status);
}

function statusClass(status) {
    if (status === "completed") return "completed";
    if (["cancelled", "expired"].includes(status)) return "anomaly";
    return "progress";
}

init();
