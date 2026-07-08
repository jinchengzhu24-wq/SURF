const DEFAULT_API_BASE = "http://111.231.136.4:8000";
const SCENE_DISPLAY_NAMES = {
    "Algorithm_Level": "Algorithm Level",
    "Level_3(H)": "Algorithm Level",
    "LLM_Level": "LLM Level",
    "Level_4(A)": "LLM Level",
    "Custom_Level": "Custom Level",
    "Creative_WorkShop": "Creative Workshop",
    "Expansion": "Expansion"
};

const DASHBOARD_LEVEL_SCENE_NAME = "Custom_Level";

const state = {
    apiBase: resolveApiBase(),
    payload: null,
    filteredRounds: [],
    filteredLevels: [],
    selectedRunId: null
};

const elements = {
    notice: document.getElementById("notice"),
    statusLine: document.getElementById("statusLine"),
    dataSource: document.getElementById("dataSource"),
    statEvents: document.getElementById("statEvents"),
    statLevels: document.getElementById("statLevels"),
    statSessions: document.getElementById("statSessions"),
    statCompleted: document.getElementById("statCompleted"),
    statMissing: document.getElementById("statMissing"),
    statAvg: document.getElementById("statAvg"),
    statSurveys: document.getElementById("statSurveys"),
    statSurveyAvg: document.getElementById("statSurveyAvg"),
    searchInput: document.getElementById("searchInput"),
    statusFilter: document.getElementById("statusFilter"),
    creativeIdeaCount: document.getElementById("creativeIdeaCount"),
    creativeIdeasBody: document.getElementById("creativeIdeasBody"),
    expansionChoiceCount: document.getElementById("expansionChoiceCount"),
    expansionChoicesBody: document.getElementById("expansionChoicesBody"),
    resultCount: document.getElementById("resultCount"),
    recordsBody: document.getElementById("recordsBody"),
    surveyCount: document.getElementById("surveyCount"),
    surveyBody: document.getElementById("surveyBody"),
    selectedTitle: document.getElementById("selectedTitle"),
    mapGrid: document.getElementById("mapGrid"),
    detailMetrics: document.getElementById("detailMetrics"),
    refreshButton: document.getElementById("refreshButton"),
    clearButton: document.getElementById("clearButton"),
    rawLink: document.getElementById("rawLink"),
    legacyLink: document.getElementById("legacyLink"),
    docsLink: document.getElementById("docsLink")
};

init();

function init() {
    wireLinks();
    wireEvents();

    if (new URLSearchParams(window.location.search).get("cleared") === "1") {
        showNotice("Records cleared.");
    }

    loadData(false);
}

function resolveApiBase() {
    const queryApi = new URLSearchParams(window.location.search).get("api");

    if (queryApi) {
        return queryApi.replace(/\/+$/, "");
    }

    if (window.location.protocol === "http:" || window.location.protocol === "https:") {
        return window.location.origin;
    }

    return DEFAULT_API_BASE;
}

function apiUrl(path) {
    return state.apiBase + path;
}

function wireLinks() {
    elements.rawLink.href = apiUrl("/level-records");
    elements.legacyLink.href = apiUrl("/level-records-legacy");
    elements.docsLink.href = apiUrl("/docs");
}

function wireEvents() {
    elements.refreshButton.addEventListener("click", () => loadData(true));
    elements.clearButton.addEventListener("click", clearRecords);
    elements.searchInput.addEventListener("input", applyFilters);
    elements.statusFilter.addEventListener("change", applyFilters);
}

async function loadData(manual) {
    setStatus(manual ? "Refreshing records..." : "Loading records...");

    try {
        const response = await fetch(apiUrl("/level-records-data"), { cache: "no-store" });

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const previousSelectedRunId = state.selectedRunId;
        state.payload = buildDashboardPayload(await response.json());
        state.selectedRunId = previousSelectedRunId;

        renderSummary(state.payload.summary || {});
        applyFilters();
        renderCreativeIdeasTable();
        renderExpansionChoicesTable();
        renderSurveyTable();
        setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
        elements.dataSource.textContent = "API: " + state.apiBase;
    } catch (error) {
        setStatus("Could not load records: " + error.message);
        elements.creativeIdeasBody.innerHTML = '<tr><td colspan="5" class="empty-state">Failed to load creative ideas.</td></tr>';
        elements.expansionChoicesBody.innerHTML = '<tr><td colspan="5" class="empty-state">Failed to load expansion choices.</td></tr>';
        elements.recordsBody.innerHTML = '<tr><td colspan="7" class="empty-state">Failed to load records.</td></tr>';
        elements.surveyBody.innerHTML = '<tr><td colspan="5" class="empty-state">Failed to load survey responses.</td></tr>';
    }
}

async function clearRecords() {
    if (!window.confirm("Clear all records? This cannot be undone.")) {
        return;
    }

    setStatus("Clearing records...");

    try {
        const response = await fetch(apiUrl("/clear-level-records"), {
            method: "POST"
        });

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        state.selectedRunId = null;
        showNotice("Records cleared.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not clear records: " + error.message);
    }
}

function renderSummary(summary) {
    const surveySummary = (state.payload && state.payload.surveySummary) || {};
    const roundCount = typeof summary.roundCount === "number"
        ? summary.roundCount
        : getAllRounds().length;
    elements.statEvents.textContent = numberValue(summary.eventCount);
    elements.statLevels.textContent = numberValue(summary.levelCount);
    elements.statSessions.textContent = numberValue(roundCount);
    elements.statCompleted.textContent = numberValue(summary.completedCount);
    elements.statMissing.textContent = numberValue(summary.missingEndCount);
    elements.statAvg.textContent = formatSeconds(summary.averageDurationSeconds);
    elements.statSurveys.textContent = numberValue(surveySummary.responseCount);
    elements.statSurveyAvg.textContent = formatSeconds(surveySummary.averageDurationSeconds);
}

function buildDashboardPayload(payload) {
    const sourcePayload = payload || {};
    const levels = Array.isArray(sourcePayload.levels)
        ? sourcePayload.levels.filter(isDashboardLevel)
        : [];
    const rounds = Array.isArray(sourcePayload.rounds)
        ? sourcePayload.rounds
            .map(round => filterRoundForDashboard(round))
            .filter(round => round !== null)
        : [];

    return Object.assign({}, sourcePayload, {
        levels: levels,
        rounds: rounds,
        summary: buildDashboardSummary(levels, rounds, sourcePayload.summary || {})
    });
}

function filterRoundForDashboard(round) {
    const levels = Array.isArray(round.levels)
        ? round.levels.filter(isDashboardLevel)
        : [];

    if (levels.length === 0) {
        return null;
    }

    return Object.assign({}, round, {
        levels: levels,
        sceneNames: [DASHBOARD_LEVEL_SCENE_NAME]
    });
}

function isDashboardLevel(level) {
    const start = (level && level.start) || {};
    const end = (level && level.end) || {};
    return (start.sceneName || end.sceneName) === DASHBOARD_LEVEL_SCENE_NAME;
}

function buildDashboardSummary(levels, rounds, baseSummary) {
    const sessionIds = new Set();
    let eventCount = 0;
    let completedCount = 0;
    let missingEndCount = 0;
    let restartedCount = 0;
    let totalDurationSeconds = 0;
    let endedLevelCount = 0;
    let totalMoves = 0;
    let totalPushes = 0;

    levels.forEach(level => {
        const start = level.start || {};
        const end = level.end || null;
        eventCount += Array.isArray(level.events)
            ? level.events.length
            : (level.start ? 1 : 0) + (level.end ? 1 : 0);

        if (start.sessionId) {
            sessionIds.add(start.sessionId);
        } else if (end && end.sessionId) {
            sessionIds.add(end.sessionId);
        }

        if (!end) {
            missingEndCount++;
            return;
        }

        if (end.completed) {
            completedCount++;
        }

        if (end.endReason === "restarted") {
            restartedCount++;
        }

        if (typeof end.durationSeconds === "number") {
            totalDurationSeconds += end.durationSeconds;
            endedLevelCount++;
        }

        if (typeof end.moveCount === "number") {
            totalMoves += end.moveCount;
        }

        if (typeof end.pushCount === "number") {
            totalPushes += end.pushCount;
        }
    });

    return Object.assign({}, baseSummary, {
        eventCount: eventCount,
        levelCount: levels.length,
        roundCount: rounds.length,
        sessionCount: sessionIds.size,
        completedCount: completedCount,
        missingEndCount: missingEndCount,
        restartedCount: restartedCount,
        totalMoves: totalMoves,
        totalPushes: totalPushes,
        averageDurationSeconds: endedLevelCount > 0
            ? Math.round((totalDurationSeconds / endedLevelCount) * 100) / 100
            : 0
    });
}

function applyFilters() {
    if (!state.payload) {
        return;
    }

    const search = elements.searchInput.value.trim().toLowerCase();
    const status = elements.statusFilter.value;
    const allRounds = getAllRounds();

    state.filteredRounds = [];
    state.filteredLevels = [];

    allRounds.forEach(round => {
        const matchingLevels = (round.levels || []).filter(level => (
            levelMatchesFilters(level, round, search, status)
        ));

        if (matchingLevels.length === 0) {
            return;
        }

        const filteredRound = buildFilteredRound(round, matchingLevels);
        state.filteredRounds.push(filteredRound);
        state.filteredLevels.push(...matchingLevels);
    });

    renderTable();
    keepOrSelectFirst();
}

function getAllRounds() {
    const payload = state.payload || {};
    const apiRounds = Array.isArray(payload.rounds) ? payload.rounds : [];

    if (apiRounds.length > 0) {
        return apiRounds.map(normalizeRound);
    }

    const levels = Array.isArray(payload.levels) ? payload.levels : [];

    if (levels.length === 0) {
        return [];
    }

    return [
        normalizeRound({
            roundId: "legacy-round",
            displayName: "Legacy Round",
            shortId: "legacy",
            isLegacy: true,
            isInferred: true,
            levels: levels
        }, 0)
    ];
}

function normalizeRound(round, index) {
    const roundId = String(round.roundId || "round-" + (index + 1));
    const displayName = round.displayName
        || (round.isLegacy ? "Legacy Round" : "Round " + (index + 1));
    const shortRoundId = round.shortId || shortId(roundId);
    const levels = Array.isArray(round.levels) ? round.levels : [];

    levels.forEach(level => {
        level.roundId = roundId;
        level.roundDisplayName = displayName;
        level.roundShortId = shortRoundId;
    });

    return Object.assign({}, round, {
        roundId: roundId,
        displayName: displayName,
        shortId: shortRoundId,
        levels: levels,
        sceneNames: Array.isArray(round.sceneNames) ? round.sceneNames : []
    });
}

function buildFilteredRound(round, levels) {
    const summary = summarizeLevels(levels);

    return Object.assign({}, round, summary, {
        levels: levels,
        levelCount: levels.length
    });
}

function summarizeLevels(levels) {
    const summary = {
        completedCount: 0,
        missingEndCount: 0,
        failedCount: 0,
        restartedCount: 0,
        totalDurationSeconds: 0
    };

    levels.forEach(level => {
        const end = level.end || null;

        if (!end) {
            summary.missingEndCount++;
        } else if (end.completed) {
            summary.completedCount++;
        } else if (end.endReason === "restarted") {
            summary.restartedCount++;
        } else {
            summary.failedCount++;
        }

        if (end && typeof end.durationSeconds === "number") {
            summary.totalDurationSeconds += end.durationSeconds;
        }
    });

    summary.totalDurationSeconds = Math.round(summary.totalDurationSeconds * 100) / 100;
    return summary;
}

function levelMatchesFilters(level, round, search, status) {
    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    const rowStatus = getStatusKey(level);
    const haystack = [
        round.roundId,
        round.displayName,
        round.shortId,
        (round.sceneNames || []).join(" "),
        level.levelRunId,
        start.gameRoundId,
        end.gameRoundId,
        start.roundLevelIndex,
        end.roundLevelIndex,
        start.levelIndex,
        end.levelIndex,
        structure.mapHash
    ].join(" ").toLowerCase();

    if (status !== "all" && rowStatus !== status) {
        return false;
    }

    return !search || haystack.includes(search);
}

function renderTable() {
    elements.recordsBody.textContent = "";
    elements.resultCount.textContent = formatShownCount(
        state.filteredRounds.length,
        state.filteredLevels.length
    );

    if (state.filteredLevels.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 7;
        cell.className = "empty-state";
        cell.textContent = "No matching level records.";
        row.appendChild(cell);
        elements.recordsBody.appendChild(row);
        renderDetails(null);
        return;
    }

    state.filteredRounds.forEach(round => {
        round.levels.forEach(level => {
            elements.recordsBody.appendChild(renderLevelRow(level));
        });
    });
}

function renderCreativeIdeasTable() {
    const ideas = (state.payload && state.payload.creativeIdeas) || [];
    elements.creativeIdeasBody.textContent = "";
    elements.creativeIdeaCount.textContent = ideas.length + " shown";

    if (ideas.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 5;
        cell.className = "empty-state";
        cell.textContent = "No creative workshop ideas yet.";
        row.appendChild(cell);
        elements.creativeIdeasBody.appendChild(row);
        return;
    }

    ideas.forEach(idea => {
        const row = document.createElement("tr");
        const cells = [
            formatTimestamp(idea.serverReceivedAt || idea.timestamp),
            value(idea.ideaText),
            formatSceneName(value(idea.sceneName)),
            getCreativeIdeaMapHash(idea)
        ];

        cells.forEach((text, index) => {
            const cell = document.createElement("td");

            if (index === 1) {
                cell.className = "idea-cell";
            } else if (index === 3) {
                cell.className = "small";
            }

            cell.textContent = text;
            row.appendChild(cell);
        });

        const actionsCell = document.createElement("td");
        const deleteButton = document.createElement("button");
        const ideaId = normalizeCreativeIdeaText(idea.ideaId);
        deleteButton.className = "round-action round-action-danger";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";

        if (ideaId) {
            deleteButton.title = "Delete creative idea";
            deleteButton.addEventListener("click", event => {
                event.stopPropagation();
                deleteCreativeIdea(idea);
            });
        } else {
            deleteButton.disabled = true;
            deleteButton.title = "Cannot delete without idea ID";
        }

        actionsCell.appendChild(deleteButton);
        row.appendChild(actionsCell);

        elements.creativeIdeasBody.appendChild(row);
    });
}

function renderExpansionChoicesTable() {
    const choices = (state.payload && state.payload.creativeExpansionChoices) || [];
    elements.expansionChoicesBody.textContent = "";
    elements.expansionChoiceCount.textContent = choices.length + " shown";

    if (choices.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 5;
        cell.className = "empty-state";
        cell.textContent = "No expansion choices yet.";
        row.appendChild(cell);
        elements.expansionChoicesBody.appendChild(row);
        return;
    }

    choices.forEach(choice => {
        const row = document.createElement("tr");
        const cells = [
            formatTimestamp(choice.serverReceivedAt || choice.timestamp),
            getExpansionChoiceLabel(choice),
            value(choice.originalIdeaText),
            value(choice.finalIdeaText || choice.selectedOptionPromptText),
            formatSceneName(value(choice.sceneName))
        ];

        cells.forEach((text, index) => {
            const cell = document.createElement("td");

            if (index === 1 || index === 2) {
                cell.className = "choice-cell";
            } else if (index === 3) {
                cell.className = "prompt-cell";
            }

            cell.textContent = text;
            row.appendChild(cell);
        });

        elements.expansionChoicesBody.appendChild(row);
    });
}

function renderLevelRow(level) {
    const row = document.createElement("tr");
    row.className = "level-row";
    row.dataset.runId = level.levelRunId;

    if (level.levelRunId === state.selectedRunId) {
        row.classList.add("selected");
    }

    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    const status = getStatus(level);
    const cells = [
        value(start.roundLevelIndex || end.roundLevelIndex || start.levelIndex || end.levelIndex),
        status.label,
        value(end.moveCount),
        value(end.pushCount),
        value(start.solutionSteps),
        value(structure.mapHash)
    ];

    cells.forEach((text, index) => {
        const cell = document.createElement("td");

        if (index === 0) {
            cell.className = "level-index-cell";
            cell.textContent = text;
        } else if (index === 1) {
            const badge = document.createElement("span");
            badge.className = "badge " + status.className;
            badge.textContent = text;
            cell.appendChild(badge);
        } else if (index === 5) {
            cell.className = "small";
            cell.textContent = text;
        } else {
            cell.textContent = text;
        }

        row.appendChild(cell);
    });

    row.appendChild(renderLevelActionsCell(level));

    row.addEventListener("click", () => {
        state.selectedRunId = level.levelRunId;
        renderTable();
        renderDetails(level);
    });

    return row;
}

function renderLevelActionsCell(level) {
    const cell = document.createElement("td");
    const actions = document.createElement("div");
    const round = getLevelRound(level);

    actions.className = "row-actions";

    const renameButton = document.createElement("button");
    renameButton.className = "round-action";
    renameButton.type = "button";
    renameButton.textContent = "Rename";

    if (round.isLegacy) {
        renameButton.disabled = true;
        renameButton.title = "Legacy records cannot be renamed";
    } else {
        renameButton.title = "Rename record";
        renameButton.addEventListener("click", event => {
            event.stopPropagation();
            renameRound(round);
        });
    }

    const deleteButton = document.createElement("button");
    deleteButton.className = "round-action round-action-danger";
    deleteButton.type = "button";
    deleteButton.title = "Delete record";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", event => {
        event.stopPropagation();
        deleteLevelRun(level);
    });

    actions.append(renameButton, deleteButton);
    cell.appendChild(actions);
    return cell;
}

function getLevelRound(level) {
    const start = level.start || {};
    const end = level.end || {};
    const roundId = level.roundId
        || start.gameRoundId
        || end.gameRoundId
        || "legacy-round";
    const displayName = level.roundDisplayName
        || getRoundDisplayNameFromRecords(start, end)
        || (roundId === "legacy-round" ? "Legacy Round" : "Record");

    return {
        roundId: roundId,
        displayName: displayName,
        isLegacy: roundId === "legacy-round",
        levels: [level]
    };
}

function getRoundDisplayNameFromRecords(start, end) {
    return normalizeCreativeIdeaText(end.roundDisplayName)
        || normalizeCreativeIdeaText(start.roundDisplayName);
}

async function renameRound(round) {
    const nextName = window.prompt("Rename record", round.displayName);

    if (nextName === null) {
        return;
    }

    const displayName = nextName.trim();

    if (!displayName) {
        window.alert("Record name cannot be empty.");
        return;
    }

    setStatus("Renaming record...");

    try {
        const response = await fetch(apiUrl("/rename-round"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                roundId: round.roundId,
                displayName: displayName
            })
        });

        if (!response.ok) {
            throw new Error(await getResponseError(response));
        }

        showNotice("Record renamed.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not rename record: " + error.message);
    }
}

async function deleteLevelRun(level) {
    const levelRunId = value(level.levelRunId);
    const start = level.start || {};
    const end = level.end || {};
    const displayLevel = value(start.roundLevelIndex || end.roundLevelIndex || start.levelIndex || end.levelIndex);

    if (levelRunId === "-") {
        return;
    }

    const confirmed = window.confirm(
        "Delete level " + displayLevel + "? This permanently removes this level record."
    );

    if (!confirmed) {
        return;
    }

    setStatus("Deleting level record...");

    try {
        const response = await fetch(apiUrl("/delete-level-run"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                levelRunId: levelRunId
            })
        });

        if (!response.ok) {
            throw new Error(await getResponseError(response));
        }

        if (level.levelRunId === state.selectedRunId) {
            state.selectedRunId = null;
        }

        showNotice("Level record deleted.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not delete level record: " + error.message);
    }
}

async function getResponseError(response) {
    try {
        const data = await response.json();

        if (data && data.detail) {
            return data.detail;
        }
    } catch (error) {
        // Fall back to the HTTP status below.
    }

    return "HTTP " + response.status;
}

function renderSurveyTable() {
    const responses = (state.payload && state.payload.surveyResponses) || [];
    elements.surveyBody.textContent = "";
    elements.surveyCount.textContent = responses.length + " shown";

    if (responses.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 5;
        cell.className = "empty-state";
        cell.textContent = "No survey responses yet.";
        row.appendChild(cell);
        elements.surveyBody.appendChild(row);
        return;
    }

    responses.forEach(response => {
        const row = document.createElement("tr");
        const cells = [
            formatTimestamp(response.serverReceivedAt || response.timestamp),
            value(response.surveyTitle || response.surveyId),
            value(response.playerNickname || response.playerName || response.nickname)
        ];

        cells.forEach(text => {
            const cell = document.createElement("td");
            cell.textContent = text;
            row.appendChild(cell);
        });

        const answersCell = document.createElement("td");
        answersCell.className = "answers-cell";
        renderSurveyAnswerLines(answersCell, response);
        row.appendChild(answersCell);

        const actionsCell = document.createElement("td");
        const deleteButton = document.createElement("button");
        const deletePayload = getSurveyDeletePayload(response);
        deleteButton.className = "round-action round-action-danger";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";

        if (deletePayload) {
            deleteButton.title = "Delete survey response";
            deleteButton.addEventListener("click", event => {
                event.stopPropagation();
                deleteSurveyResponse(response);
            });
        } else {
            deleteButton.disabled = true;
            deleteButton.title = "Cannot delete without response ID or nickname";
        }

        actionsCell.appendChild(deleteButton);
        row.appendChild(actionsCell);

        elements.surveyBody.appendChild(row);
    });
}

async function deleteSurveyResponse(response) {
    const deletePayload = getSurveyDeletePayload(response);

    if (!deletePayload) {
        return;
    }

    const label = getSurveyDeleteLabel(response);
    const confirmed = window.confirm(
        "Delete survey response from " + label + "? This permanently removes it."
    );

    if (!confirmed) {
        return;
    }

    setStatus("Deleting survey response...");

    try {
        const apiResponse = await fetch(apiUrl("/delete-survey-response"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(deletePayload)
        });

        if (!apiResponse.ok) {
            throw new Error(await getResponseError(apiResponse));
        }

        showNotice("Survey response deleted.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not delete survey response: " + error.message);
    }
}

async function deleteCreativeIdea(idea) {
    const ideaId = normalizeCreativeIdeaText(idea.ideaId);

    if (!ideaId) {
        return;
    }

    const label = getCreativeIdeaDeleteLabel(idea);
    const confirmed = window.confirm(
        "Delete creative idea \"" + label + "\"? This permanently removes it."
    );

    if (!confirmed) {
        return;
    }

    setStatus("Deleting creative idea...");

    try {
        const apiResponse = await fetch(apiUrl("/delete-creative-idea"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ideaId: ideaId
            })
        });

        if (!apiResponse.ok) {
            throw new Error(await getResponseError(apiResponse));
        }

        showNotice("Creative idea deleted.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not delete creative idea: " + error.message);
    }
}

function getSurveyDeletePayload(response) {
    const responseId = normalizeSurveyDeleteText(response.responseId);

    if (responseId) {
        return { responseId: responseId };
    }

    const playerNickname = getSurveyNickname(response);

    if (playerNickname) {
        return { playerNickname: playerNickname };
    }

    return null;
}

function getCreativeIdeaDeleteLabel(idea) {
    const ideaText = normalizeCreativeIdeaText(idea.ideaText);

    if (!ideaText) {
        return shortId(idea.ideaId);
    }

    return ideaText.length > 40 ? ideaText.slice(0, 40) + "..." : ideaText;
}

function getExpansionChoiceLabel(choice) {
    const optionId = normalizeCreativeIdeaText(choice.selectedOptionId);
    const optionTitle = normalizeCreativeIdeaText(choice.selectedOptionTitle);

    if (optionId && optionTitle) {
        return optionId + " - " + optionTitle;
    }

    return optionTitle || optionId || "-";
}

function getCreativeIdeaMapHash(idea) {
    const directMapHash = normalizeCreativeIdeaText(
        idea.mapHash || (idea.structure && idea.structure.mapHash)
    );

    if (directMapHash) {
        return directMapHash;
    }

    const ideaId = normalizeCreativeIdeaText(idea.ideaId);
    const ideaText = normalizeCreativeIdeaText(idea.ideaText);
    const levels = state.payload && Array.isArray(state.payload.levels)
        ? state.payload.levels
        : [];
    const hashes = [];

    levels.forEach(level => {
        const start = level.start || {};
        const structure = start.structure || {};
        const mapHash = normalizeCreativeIdeaText(structure.mapHash);

        if (!mapHash) {
            return;
        }

        const levelIdeaId = normalizeCreativeIdeaText(start.creativeIdeaId);
        const levelIdeaText = normalizeCreativeIdeaText(start.creativeIdeaText);
        const matchesIdeaId = ideaId && levelIdeaId === ideaId;
        const matchesIdeaText = !ideaId && ideaText && levelIdeaText === ideaText;

        if ((matchesIdeaId || matchesIdeaText) && !hashes.includes(mapHash)) {
            hashes.push(mapHash);
        }
    });

    return hashes.length > 0 ? hashes.join(", ") : "-";
}

function getSurveyDeleteLabel(response) {
    return getSurveyNickname(response)
        || normalizeSurveyDeleteText(response.surveyTitle || response.surveyId)
        || "this player";
}

function getSurveyNickname(response) {
    return normalizeSurveyDeleteText(response.playerName)
        || normalizeSurveyDeleteText(response.playerNickname)
        || normalizeSurveyDeleteText(response.nickname);
}

function keepOrSelectFirst() {
    const selected = state.filteredLevels.find(level => level.levelRunId === state.selectedRunId);

    if (selected) {
        renderDetails(selected);
        return;
    }

    if (state.filteredLevels.length > 0) {
        state.selectedRunId = state.filteredLevels[0].levelRunId;
        renderTable();
        renderDetails(state.filteredLevels[0]);
        return;
    }

    state.selectedRunId = null;
    renderDetails(null);
}

function renderDetails(level) {
    elements.mapGrid.textContent = "";
    elements.detailMetrics.textContent = "";

    if (!level) {
        elements.selectedTitle.textContent = "No selection";
        return;
    }

    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    const rows = Array.isArray(start.rows) ? start.rows : [];
    const displayLevel = start.roundLevelIndex || end.roundLevelIndex || start.levelIndex || end.levelIndex;
    elements.selectedTitle.textContent = "Level " + value(displayLevel);
    renderMap(rows);

    [
        ["Round", value(level.roundDisplayName)],
        ["Run", shortId(level.levelRunId)],
        ["Status", getStatus(level).label],
        ["Moves", value(end.moveCount)],
        ["Pushes", value(end.pushCount)],
        ["Restarts", value(end.restartCount)],
        ["Solver steps", value(start.solutionSteps)],
        ["Solver pushes", value(start.solverPushes)],
        ["Attempts", value(start.generationAttempts)],
        ["Reverse pulls", value(start.reversePulls)],
        ["Wall density", formatRatio(structure.wallDensity)],
        ["Water density", formatRatio(structure.waterDensity)],
        ["Reachable", formatPercent(structure.reachableAreaRatio)],
        ["Dead corner risk", formatRatio(structure.deadCornerRisk)]
    ].forEach(([label, metricValue]) => {
        const item = document.createElement("div");
        item.className = "metric";
        const labelNode = document.createElement("span");
        const valueNode = document.createElement("strong");
        labelNode.textContent = label;
        valueNode.textContent = metricValue;
        item.append(labelNode, valueNode);
        elements.detailMetrics.appendChild(item);
    });

}

function renderMap(rows) {
    if (!rows.length) {
        elements.mapGrid.textContent = "No map rows.";
        return;
    }

    const width = rows.reduce((max, row) => Math.max(max, String(row || "").length), 0);
    elements.mapGrid.style.gridTemplateColumns = "repeat(" + width + ", var(--tile-size))";

    rows.forEach(rowText => {
        const row = String(rowText || "");

        for (let index = 0; index < width; index += 1) {
            const tile = row[index] || " ";
            const cell = document.createElement("div");
            cell.className = "tile " + getTileClass(tile);
            cell.title = getTileName(tile);
            cell.textContent = getTileLabel(tile);
            elements.mapGrid.appendChild(cell);
        }
    });
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
    if (tile === "#") return "wall";
    if (tile === "@") return "water";
    if (tile === "p") return "player";
    if (tile === "s") return "box";
    if (tile === "t") return "target";
    if (tile === ".") return "floor";
    return "empty";
}

function getTileLabel(tile) {
    if (tile === "p") return "P";
    if (tile === "s") return "B";
    if (tile === "t") return "T";
    if (tile === "@") return "~";
    return "";
}

function getStatus(level) {
    const end = level.end || null;

    if (!end) {
        return { key: "missing", label: "missing end", className: "badge-missing" };
    }

    if (end.completed) {
        return { key: "completed", label: "completed", className: "badge-completed" };
    }

    return { key: "failed", label: value(end.endReason), className: "badge-failed" };
}

function getStatusKey(level) {
    return getStatus(level).key;
}

function getRoundCompletionText(round) {
    const stoppedCount = (round.failedCount || 0) + (round.restartedCount || 0);
    const parts = [
        (round.completedCount || 0) + "/" + (round.levelCount || 0) + " completed"
    ];

    if (round.missingEndCount > 0) {
        parts.push(round.missingEndCount + " missing");
    }

    if (stoppedCount > 0) {
        parts.push(stoppedCount + " stopped");
    }

    return parts.join(", ");
}

function formatSceneNames(sceneNames) {
    if (!Array.isArray(sceneNames) || sceneNames.length === 0) {
        return "Scene -";
    }

    return "Scene " + sceneNames.map(formatSceneName).join(", ");
}

function formatSceneName(sceneName) {
    return SCENE_DISPLAY_NAMES[sceneName] || sceneName;
}

function formatShownCount(roundCount, levelCount) {
    return plural(roundCount, "round") + " / " + plural(levelCount, "level") + " shown";
}

function plural(count, singular) {
    return count + " " + (count === 1 ? singular : singular + "s");
}

function setStatus(text) {
    elements.statusLine.textContent = text;
}

function showNotice(text) {
    elements.notice.textContent = text;
    elements.notice.classList.add("visible");
}

function value(input) {
    if (input === null || input === undefined || input === "") {
        return "-";
    }

    return String(input);
}

function normalizeSurveyDeleteText(input) {
    if (input === null || input === undefined) {
        return "";
    }

    const text = String(input).trim();
    return text === "-" ? "" : text;
}

function normalizeCreativeIdeaText(input) {
    if (input === null || input === undefined) {
        return "";
    }

    const text = String(input).trim();
    return text === "-" ? "" : text;
}

function numberValue(input) {
    return typeof input === "number" ? String(input) : "0";
}

function shortId(input) {
    const text = value(input);
    return text === "-" ? text : text.slice(0, 8);
}

function formatSeconds(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return input.toFixed(1) + "s";
}

function formatRatio(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return input.toFixed(3);
}

function formatPercent(input) {
    if (typeof input !== "number" || Number.isNaN(input)) {
        return "-";
    }

    return Math.round(input * 100) + "%";
}

function formatOptionalTimestamp(input) {
    return input ? formatTimestamp(input) : "-";
}

function getSurveyAnswerLines(response) {
    const answers = response && Array.isArray(response.answerDetails)
        ? response.answerDetails
        : response && response.answers;

    if (!Array.isArray(answers) || answers.length === 0) {
        if (response && response.answersSummary && response.answersSummary !== "-") {
            return String(response.answersSummary).split(";").map(line => line.trim()).filter(Boolean);
        }

        return [];
    }

    return answers.map(answer => {
        const index = value(answer.questionIndex);
        const question = value(answer.questionText || answer.questionId);
        const option = value(answer.optionLabel || answer.optionText || answer.optionId);
        const label = index === "-" ? "Question" : "Q" + index;

        if (question !== "-") {
            return label + ": " + question + " -> " + option;
        }

        return label + ": " + option;
    });
}

function renderSurveyAnswerLines(cell, response) {
    const lines = getSurveyAnswerLines(response);

    if (lines.length === 0) {
        cell.textContent = "-";
        return;
    }

    lines.forEach(line => {
        const lineNode = document.createElement("div");
        lineNode.className = "answer-line";
        lineNode.textContent = line;
        cell.appendChild(lineNode);
    });
}

function formatTimestamp(input) {
    if (!input) {
        return "just now";
    }

    const date = new Date(input);

    if (Number.isNaN(date.getTime())) {
        return input;
    }

    return date.toLocaleString();
}
