const DEFAULT_API_BASE = "http://111.231.136.4:8000";

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
    matchCount: document.getElementById("matchCount"),
    matchList: document.getElementById("matchList"),
    emptyDetail: document.getElementById("emptyDetail"),
    matchDetail: document.getElementById("matchDetail"),
    selectedMatchTitle: document.getElementById("selectedMatchTitle"),
    selectedMatchStatus: document.getElementById("selectedMatchStatus"),
    selectedMatchText: document.getElementById("selectedMatchText"),
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

init();

function init() {
    elements.rawLink.href = apiUrl("/matchmaking-records");
    elements.dataLink.href = apiUrl("/matchmaking-records-data");
    elements.docsLink.href = apiUrl("/docs");
    elements.refreshButton.addEventListener("click", () => loadData(true));
    elements.clearButton.addEventListener("click", clearAllRecords);
    elements.searchInput.addEventListener("input", applyFilters);
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

function resolveApiBase() {
    const queryApi = new URLSearchParams(window.location.search).get("api");
    if (queryApi) return queryApi.replace(/\/+$/, "");
    if (["http:", "https:"].includes(window.location.protocol)) return window.location.origin;
    return DEFAULT_API_BASE;
}

function apiUrl(path) {
    return state.apiBase + path;
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

function renderSummary() {
    const summary = state.payload.summary || {};
    const total = numeric(summary.matchCount);
    const completed = numeric(summary.completedCount);
    const attention = numeric(summary.cancelledCount) + numeric(summary.expiredCount);
    const malformed = numeric(summary.malformedCount);
    elements.statMatches.textContent = total;
    elements.statCompleted.textContent = completed;
    elements.statCompletionRate.textContent = (total ? Math.round(completed / total * 100) : 0) + "% completion rate";
    elements.statAvg.textContent = formatSeconds(summary.averageRunDurationSeconds);
    elements.statAttention.textContent = attention;
    elements.statDataHealth.textContent = malformed
        ? malformed + " malformed records"
        : attention
            ? attention + " cancelled or expired"
            : "no data issues";
}

function applyFilters() {
    const query = clean(elements.searchInput.value).toLowerCase();
    state.filteredMatches = state.matches.filter(match => {
        if (!query) return true;
        return [match.matchId, match.roomCode, shortId(match.matchId)]
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
        elements.matchList.appendChild(emptyNode("No matching rooms."));
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
            textNode("span", formatShortDate(match.updatedAt))
        );
        const snippet = textNode("p", "Match " + shortId(match.matchId), "idea-snippet");
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
    elements.selectedMatchText.textContent = "Match ID " + match.matchId;
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

function buildTimelineStages(match) {
    const flowStages = safeArray(match.players).flatMap(player =>
        safeArray(player.coCreationFlow)
            .filter(event => event.eventType !== "opening")
            .map((event, index) => ({
            key: "flow:" + player.playerNumber + ":" + (clean(event.eventId) || index),
            type: "flow",
            label: flowStageLabel(event),
            timestamp: getTimestamp(event),
            warning: false,
            playerNumber: player.playerNumber,
            record: event
            }))
    );
    if (flowStages.length) return flowStages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
    const stages = safeArray(match.events).map((event, index) => ({
        key: "event:" + (clean(event.eventId) || index),
        type: "event",
        label: eventStageLabel(event),
        timestamp: getTimestamp(event),
        warning: ["player_left", "room_expired"].includes(event.eventType),
        playerNumber: numeric(event.playerNumber),
        record: event
    }));
    return stages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

function flowStageLabel(event) {
    const labels = {
        draft: "Draft",
        first_stage: "First Stage",
        stage: "Stage " + value(event.stageNumber) + " · " + (event.source === "ai" ? "AI" : "Manual"),
        turn: "Q&A",
        final: "Final Stage",
        message: "Message"
    };
    return labels[event.eventType] || titleCase(event.eventType || "Flow event");
}

function eventStageLabel(event) {
    const player = numeric(event.playerNumber) ? "P" + event.playerNumber + " " : "";
    const labels = {
        room_created: "Room created",
        player_joined: player + "joined",
        ready_changed: player + (event.ready ? "ready" : "not ready"),
        challenge_submitted: player + "challenge submitted",
        designer_intention_synchronized: player + "message confirmed",
        result_submitted: player + "result submitted",
        player_left: player + "left",
        room_expired: "Room expired"
    };
    return labels[event.eventType] || titleCase(event.eventType || "Match event");
}

function renderTimeline(match, stages) {
    elements.matchTimeline.textContent = "";
    elements.matchTimeline.classList.add("player-flow-list");
    if (!stages.length) {
        elements.matchTimeline.appendChild(emptyNode("No match stages recorded."));
        return;
    }
    const groups = new Map([[1, []], [2, []]]);
    stages.forEach(stage => groups.get(stage.playerNumber)?.push(stage));
    groups.forEach((playerStages, playerNumber) => {
        const lane = document.createElement("section");
        lane.className = "player-flow";
        lane.appendChild(textNode("h3", "Player " + playerNumber, "player-flow-title"));
        const nodes = document.createElement("div");
        nodes.className = "timeline player-flow-timeline";
        if (!playerStages.length) nodes.appendChild(emptyNode("No co-creation records."));
        playerStages.forEach((stage, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "timeline-node";
        if (stage.key === state.selectedStageKey) button.classList.add("selected");
        if (stage.warning) button.classList.add("warning");
        button.append(
            textNode("span", stage.warning ? "!" : String(index + 1), "timeline-dot"),
            textNode("span", stage.label, "timeline-label"),
            textNode("span", formatShortDate(stage.timestamp), "timeline-time")
        );
        button.addEventListener("click", () => {
            state.selectedStageKey = stage.key;
            if (stage.record.eventType === "challenge_submitted") {
                state.selectedChallengePlayer = stage.playerNumber;
            }
            renderMatch(match);
        });
            nodes.appendChild(button);
        });
        lane.appendChild(nodes);
        elements.matchTimeline.appendChild(lane);
    });
}

function renderChallengeTabs(match) {
    elements.challengeTabs.textContent = "";
    const players = safeArray(match.players).filter(player => player.challenge);
    elements.challengeCount.textContent = players.length + " challenges";
    if (!players.length) {
        elements.challengeTabs.appendChild(emptyNode("No challenge maps submitted."));
        return;
    }
    players.forEach(player => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "level-tab";
        if (player.playerNumber === state.selectedChallengePlayer) button.classList.add("selected");
        button.append(
            textNode("span", "P" + player.playerNumber + " challenge"),
            textNode("span", player.challenge.runResult ? "played" : "pending")
        );
        button.addEventListener("click", () => {
            state.selectedChallengePlayer = player.playerNumber;
            renderMatch(match);
            updateUrlSelection(match);
        });
        elements.challengeTabs.appendChild(button);
    });
}

function renderSelectedChallenge(match) {
    const player = safeArray(match.players).find(item => item.playerNumber === state.selectedChallengePlayer);
    const challenge = player && player.challenge;
    elements.mapGrid.textContent = "";
    elements.detailMetrics.textContent = "";
    if (!challenge) {
        elements.mapGrid.textContent = "No challenge selected.";
        elements.mapGrid.style.gridTemplateColumns = "";
        return;
    }
    renderMap(elements.mapGrid, safeArray(challenge.rows), false);
    const result = challenge.runResult || {};
    const overhead = typeof result.moveCount === "number" && typeof result.minimumMoves === "number"
        ? result.moveCount - result.minimumMoves
        : null;
    [
        ["Created by", "Player " + challenge.createdByPlayerNumber],
        ["Played by", "Player " + challenge.playedByPlayerNumber],
        ["Play time", formatSeconds(result.durationSeconds)],
        ["Moves", value(result.moveCount)],
        ["Minimum moves", value(result.minimumMoves)],
        ["Move overhead", overhead === null ? "-" : "+" + overhead]
    ].forEach(([label, metricValue]) => {
        const item = document.createElement("div");
        item.className = "metric";
        item.append(textNode("span", label), textNode("strong", metricValue));
        elements.detailMetrics.appendChild(item);
    });
}

function renderInspector(match, stage) {
    elements.inspectorBody.textContent = "";
    if (!stage) {
        elements.inspectorTitle.textContent = "Match details";
        elements.inspectorBody.appendChild(emptyNode("Select a timeline stage."));
        return;
    }
    elements.inspectorTitle.textContent = stage.label;
    const record = stage.record;
    const rows = [
        ["Event", titleCase(record.eventType)],
        ["Player", record.playerNumber ? "Player " + record.playerNumber : "Room"],
        ["Recorded", formatTimestamp(getTimestamp(record))]
    ];
    if (record.statusAfter) rows.splice(2, 0, ["Status after", titleCase(record.statusAfter)]);
    if (record.eventType === "ready_changed") rows.push(["Ready", record.ready ? "Yes" : "No"]);
    if (record.eventType === "draft") {
        rows.push(["Difficulty", value(record.finalDifficulty)]);
        rows.push(["Layout", value(record.finalLayout)]);
    }
    if (record.eventType === "designer_intention_synchronized") {
        rows.push(["Message", record.designerIntention]);
    }
    if (record.eventType === "result_submitted") {
        rows.push(["Play time", formatSeconds(record.durationSeconds)]);
        rows.push(["Moves", value(record.moveCount)]);
        rows.push(["Minimum moves", value(record.minimumMoves)]);
    }
    if (record.eventType === "final") {
        rows.push(["Co-creation time", formatClockDuration(record.coCreationDurationSeconds)]);
    }
    appendRecordGrid(rows);
    if (record.eventType === "draft") appendDraftDetails(record);
    if (["first_stage", "stage", "final"].includes(record.eventType)) {
        appendFlowStageDetails(match, record);
    }
    if (record.eventType === "turn") appendFlowTurnDetails(record);
    if (record.eventType === "message") appendFlowMessageDetails(record);
}

function appendDraftDetails(record) {
    const section = createSection("Draft research record");
    const grid = document.createElement("div");
    grid.className = "record-grid";
    [
        ["Metadata complete", record.draftMetadataComplete ? "Yes" : "No"],
        ["Q1 answer", record.q1Answer],
        ["Q2 answer", record.q2Answer],
        ["Q3 answer", record.q3Answer],
        ["Q4 answer", record.q4Answer],
        ["AI recommended difficulty", record.aiRecommendedDifficulty],
        ["AI recommended layout", record.aiRecommendedLayout],
        ["AI recommendation source", record.aiRecommendationSource],
        ["User final difficulty", record.finalDifficulty],
        ["User final layout", record.finalLayout]
    ].forEach(([label, rowValue]) => {
        const row = document.createElement("div");
        row.className = "record-row";
        row.append(textNode("span", label), textNode("strong", value(rowValue)));
        grid.appendChild(row);
    });
    section.appendChild(grid);
    section.append(
        textNode("h4", "AI Reflection"),
        textNode("p", value(record.aiReflection)),
        textNode("h4", "AI difficulty rationale"),
        textNode("p", value(record.aiDifficultyRationale)),
        textNode("h4", "AI layout rationale"),
        textNode("p", value(record.aiLayoutRationale))
    );
    elements.inspectorBody.appendChild(section);
}

function findOpeningForStage(match, record) {
    const player = safeArray(match.players).find(item => item.playerNumber === record.playerNumber);
    return safeArray(player && player.coCreationFlow).find(event =>
        event.eventType === "opening" && event.versionId === record.versionId
    ) || null;
}

function openingIsConsumed(match, opening) {
    const player = safeArray(match.players).find(item => item.playerNumber === opening.playerNumber);
    return safeArray(player && player.coCreationFlow).some(event =>
        event.eventType === "turn" && event.openingAssistantTurnId === opening.assistantTurnId
    );
}

function appendCards(section, cards, title = "Cards") {
    const safeCards = safeArray(cards);
    if (!safeCards.length) return;
    section.appendChild(textNode("h4", title));
    safeCards.forEach(card => section.appendChild(
        textNode("p", "[" + value(card.type) + "] " + value(card.text))
    ));
}

function appendOpeningDetails(section, opening) {
    section.append(
        textNode("h4", "AI opening evaluation"),
        textNode("p", opening.assistantText || "-")
    );
    appendCards(section, opening.cards, "Opening cards");
}

function appendFlowStageDetails(match, record) {
    const titles = {
        first_stage: "First Stage map",
        final: "Final map",
        stage: "Stage change"
    };
    const section = createSection(titles[record.eventType] || "Stage map");
    if (record.source) section.appendChild(textNode("p", "Source: " + (record.source === "ai" ? "AI-assisted" : "Manual")));
    if (record.initialDraftMethod) {
        section.appendChild(textNode("p", "Initial draft: " + formatMode(record.initialDraftMethod)));
    }
    const frame = document.createElement("div");
    frame.className = "mini-map-frame";
    const map = document.createElement("div");
    map.className = "mini-map";
    renderMap(map, safeArray(record.rows), true);
    frame.appendChild(map);
    section.appendChild(frame);
    const opening = findOpeningForStage(match, record);
    if (opening && !openingIsConsumed(match, opening)) appendOpeningDetails(section, opening);
    elements.inspectorBody.appendChild(section);
}

function appendFlowTurnDetails(record) {
    const section = createSection("Conversation");
    if (record.openingAssistantText) {
        section.append(
            textNode("h4", "AI opening evaluation"),
            textNode("p", record.openingAssistantText)
        );
        appendCards(section, record.openingCards, "Opening cards");
    }
    section.append(
        textNode("h4", "Player"),
        textNode("p", record.userText || "-"),
        textNode("h4", "AI"),
        textNode("p", record.assistantText || "-")
    );
    appendCards(section, record.cards);
    elements.inspectorBody.appendChild(section);
}

function appendFlowMessageDetails(record) {
    const section = createSection("Message to opponent");
    section.appendChild(textNode("p", record.message || "-"));
    elements.inspectorBody.appendChild(section);
}

function renderComparison(match) {
    elements.compareContent.textContent = "";
    const players = safeArray(match.players).filter(player => player.challenge);
    if (players.length !== 2) {
        elements.compareContent.appendChild(textNode(
            "div",
            "Both players must submit challenges before comparison is available.",
            "compare-placeholder"
        ));
        return;
    }
    const grid = document.createElement("div");
    grid.className = "compare-grid";
    players.forEach(player => grid.appendChild(renderCompareChallenge(player)));
    elements.compareContent.appendChild(grid);
}

function renderCompareChallenge(player) {
    const challenge = player.challenge;
    const result = challenge.runResult || {};
    const card = document.createElement("article");
    card.className = "compare-version";
    card.appendChild(textNode("h3", "Player " + player.playerNumber + " challenge"));
    const frame = document.createElement("div");
    frame.className = "mini-map-frame";
    const map = document.createElement("div");
    map.className = "mini-map";
    renderMap(map, safeArray(challenge.rows), true);
    frame.appendChild(map);
    card.appendChild(frame);
    const table = document.createElement("table");
    table.className = "delta-table";
    [
        ["Message", player.designerIntention || "-"],
        ["Played by", "Player " + challenge.playedByPlayerNumber],
        ["Play time", formatSeconds(result.durationSeconds)],
        ["Moves", value(result.moveCount)],
        ["Minimum", value(result.minimumMoves)]
    ].forEach(([label, metricValue]) => {
        const row = document.createElement("tr");
        row.append(textNode("td", label), textNode("td", metricValue));
        table.appendChild(row);
    });
    card.appendChild(table);
    return card;
}

function renderMap(container, rows, mini) {
    container.textContent = "";
    if (!rows.length) {
        container.textContent = "No map rows recorded.";
        container.style.gridTemplateColumns = "";
        return;
    }
    const width = rows.reduce((max, row) => Math.max(max, String(row || "").length), 0);
    container.style.gridTemplateColumns = "repeat(" + width + ", " + (mini ? "18px" : "var(--tile-size)") + ")";
    rows.forEach(rowText => {
        const row = String(rowText || "");
        for (let index = 0; index < width; index += 1) {
            const tile = row[index] || " ";
            const cell = document.createElement("div");
            cell.className = "tile " + getTileClass(tile);
            cell.title = getTileName(tile);
            cell.textContent = getTileLabel(tile);
            container.appendChild(cell);
        }
    });
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

function openDeleteDialog(config) {
    state.pendingDelete = config;
    elements.deleteDialogTitle.textContent = config.title;
    elements.deleteDialogDescription.textContent = config.description;
    elements.deleteDialogScope.textContent = config.scope;
    elements.deleteDialogConfirm.textContent = config.confirmLabel;
    elements.deletePasswordInput.value = "";
    elements.deleteDialogError.textContent = "";
    setDeleteDialogBusy(false);
    if (!elements.deleteDialog.open) elements.deleteDialog.showModal();
    window.requestAnimationFrame(() => elements.deletePasswordInput.focus());
}

function closeDeleteDialog() {
    if (state.deleteDialogBusy) return;
    if (elements.deleteDialog.open) elements.deleteDialog.close();
    elements.deletePasswordInput.value = "";
    elements.deleteDialogError.textContent = "";
    state.pendingDelete = null;
}

function setDeleteDialogBusy(busy) {
    state.deleteDialogBusy = busy;
    elements.deleteDialogForm.setAttribute("aria-busy", busy ? "true" : "false");
    elements.deletePasswordInput.disabled = busy;
    elements.deleteDialogCancel.disabled = busy;
    elements.deleteDialogConfirm.disabled = busy;
    elements.deleteDialogConfirm.textContent = busy
        ? "Deleting..."
        : state.pendingDelete
            ? state.pendingDelete.confirmLabel
            : "Delete";
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
        elements.deleteDialogError.textContent = error.status === 401
            ? "Incorrect password. Please try again."
            : error.status === 503
                ? "Deletion password is not configured on the server."
                : error.message;
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
    return state.matches.find(match => match.matchId === state.selectedMatchId) || null;
}

function restoreSelectionFromUrl() {
    const matchId = clean(new URLSearchParams(window.location.search).get("match"));
    if (matchId && !state.selectedMatchId && state.matches.some(match => match.matchId === matchId)) {
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

function appendRecordGrid(rows) {
    const section = createSection("Record details");
    const grid = document.createElement("div");
    grid.className = "record-grid";
    rows.forEach(([label, rowValue]) => {
        const row = document.createElement("div");
        row.className = "record-row";
        row.append(textNode("span", label), textNode("strong", value(rowValue)));
        grid.appendChild(row);
    });
    section.appendChild(grid);
    elements.inspectorBody.appendChild(section);
}

function createSection(title) {
    const section = document.createElement("section");
    section.className = "record-section";
    section.appendChild(textNode("h3", title));
    return section;
}

function textNode(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text === null || text === undefined ? "" : String(text);
    if (className) node.className = className;
    return node;
}

function emptyNode(text) {
    return textNode("div", text, "empty-state");
}

function safeArray(input) {
    return Array.isArray(input) ? input : [];
}

function clean(input) {
    if (input === null || input === undefined) return "";
    const text = String(input).trim();
    return text === "-" ? "" : text;
}

function numeric(input) {
    return typeof input === "number" && !Number.isNaN(input) ? input : 0;
}

function value(input) {
    if (input === null || input === undefined || input === "") return "-";
    return String(input);
}

function shortId(input) {
    const text = clean(input);
    return text ? text.slice(0, 8) : "-";
}

function getTimestamp(record) {
    return clean(record && (record.serverReceivedAt || record.timestamp));
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

function titleCase(input) {
    return clean(input).replace(/[-_]+/g, " ").replace(/\b\w/g, character => character.toUpperCase()) || "-";
}

function formatMode(input) {
    const labels = {
        description_generation: "Description Generation",
    };
    return labels[input] || titleCase(input);
}

function formatSeconds(input) {
    if (typeof input !== "number" || Number.isNaN(input)) return "-";
    return input.toFixed(1) + "s";
}

function formatClockDuration(input) {
    if (typeof input !== "number" || Number.isNaN(input) || input < 0) return "-";
    const totalSeconds = Math.min(600, Math.round(input));
    const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    return minutes + ":" + seconds;
}

function formatTimestamp(input) {
    if (!input) return "-";
    const date = new Date(input);
    return Number.isNaN(date.getTime()) ? input : date.toLocaleString();
}

function formatShortDate(input) {
    if (!input) return "No time";
    const date = new Date(input);
    return Number.isNaN(date.getTime())
        ? input.slice(0, 10)
        : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function getTileClass(tile) {
    if (tile === "#") return "tile-wall";
    if (tile === "@") return "tile-water";
    if (tile === "p") return "tile-player";
    if (tile === "s") return "tile-box";
    if (tile === "t") return "tile-target";
    if (tile === ".") return "tile-floor";
    return "tile-empty";
}

function getTileName(tile) {
    return ({ "#": "wall", "@": "water", p: "player", s: "box", t: "target", ".": "floor" })[tile] || "empty";
}

function getTileLabel(tile) {
    return ({ p: "P", s: "B", t: "T", "@": "~" })[tile] || "";
}

function setStatus(text) {
    elements.statusLine.textContent = text;
}

function showNotice(text) {
    elements.notice.textContent = text;
    elements.notice.classList.add("visible");
    window.setTimeout(() => elements.notice.classList.remove("visible"), 4500);
}

async function getResponseError(response) {
    try {
        const data = await response.json();
        if (data && data.detail) return data.detail;
    } catch (error) {
        // Use the status fallback.
    }
    return "HTTP " + response.status;
}
