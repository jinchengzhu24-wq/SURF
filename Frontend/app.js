const DEFAULT_API_BASE = "http://111.231.136.4:8000";
const SCENE_DISPLAY_NAMES = {
    "Algorithm_Level": "Algorithm Level",
    "Level_3(H)": "Algorithm Level",
    "LLM_Level": "LLM Level",
    "Level_4(A)": "LLM Level",
    "Custom_Level": "Custom Level",
    "Creative_WorkShop": "Creative Workshop",
    "Expansion": "Expansion",
    "Refinement": "Refinement"
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
        elements.creativeIdeasBody.innerHTML = '<tr><td colspan="4" class="empty-state">Failed to load creative ideas.</td></tr>';
        elements.expansionChoicesBody.innerHTML = '<tr><td colspan="5" class="empty-state">Failed to load expansion choices.</td></tr>';
        elements.recordsBody.innerHTML = '<tr><td colspan="8" class="empty-state">Failed to load records.</td></tr>';
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
    const ideaHash = getLevelIdeaHash(level);
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
        getLevelDisplayName(level),
        ideaHash,
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
        cell.colSpan = 8;
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
        cell.colSpan = 4;
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
            getCreativeIdeaHash(idea),
            value(idea.ideaText)
        ];

        cells.forEach((text, index) => {
            const cell = document.createElement("td");

            if (index === 1) {
                cell.className = "small";
            } else if (index === 2) {
                cell.className = "idea-cell";
            }

            cell.textContent = text;
            row.appendChild(cell);
        });

        const actionsCell = document.createElement("td");
        const deleteButton = document.createElement("button");
        const ideaId = getCreativeIdeaId(idea);
        deleteButton.className = "round-action round-action-danger";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";

        if (ideaId) {
            deleteButton.title = "Delete all records for this idea";
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
            getExpansionChoiceIdeaHash(choice),
            getExpansionChoiceLabel(choice),
            value(choice.selectedOptionDescription || choice.selectedOptionPromptText)
        ];

        cells.forEach((text, index) => {
            const cell = document.createElement("td");

            if (index === 1) {
                cell.className = "small";
            } else if (index === 2) {
                cell.className = "choice-cell";
            } else if (index === 3) {
                cell.className = "prompt-cell";
            }

            cell.textContent = text;
            row.appendChild(cell);
        });

        const actionsCell = document.createElement("td");
        const deleteButton = document.createElement("button");
        const ideaId = getExpansionChoiceIdeaId(choice);
        deleteButton.className = "round-action round-action-danger";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";

        if (ideaId) {
            deleteButton.title = "Delete all records for this idea";
            deleteButton.addEventListener("click", event => {
                event.stopPropagation();
                deleteExpansionChoice(choice);
            });
        } else {
            deleteButton.disabled = true;
            deleteButton.title = "Cannot delete without idea ID";
        }

        actionsCell.appendChild(deleteButton);
        row.appendChild(actionsCell);

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
        getLevelDisplayName(level),
        getLevelIdeaHash(level),
        value(structure.mapHash),
        status.label,
        value(end.moveCount),
        value(start.solutionSteps),
        value(end.pushCount)
    ];

    cells.forEach((text, index) => {
        const cell = document.createElement("td");

        if (index === 0) {
            cell.className = "level-index-cell";
            cell.textContent = text;
        } else if (index === 1) {
            cell.className = "small";
            cell.textContent = text;
        } else if (index === 2) {
            cell.className = "small";
            cell.textContent = text;
        } else if (index === 3) {
            const badge = document.createElement("span");
            badge.className = "badge " + status.className;
            badge.textContent = text;
            cell.appendChild(badge);
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
    const levelRunId = normalizeCreativeIdeaText(level.levelRunId);
    const ideaId = getLevelIdeaId(level);

    actions.className = "row-actions";

    const renameButton = document.createElement("button");
    renameButton.className = "round-action";
    renameButton.type = "button";
    renameButton.textContent = "Rename";

    if (!levelRunId || levelRunId.startsWith("missing-run-")) {
        renameButton.disabled = true;
        renameButton.title = "Records without a level run ID cannot be renamed";
    } else {
        renameButton.title = "Rename level";
        renameButton.addEventListener("click", event => {
            event.stopPropagation();
            renameLevelRun(level);
        });
    }

    const deleteButton = document.createElement("button");
    deleteButton.className = "round-action round-action-danger";
    deleteButton.type = "button";
    deleteButton.textContent = "Delete";

    if (ideaId) {
        deleteButton.title = "Delete all records for this idea";
        deleteButton.addEventListener("click", event => {
            event.stopPropagation();
            deleteLevelRun(level);
        });
    } else {
        deleteButton.disabled = true;
        deleteButton.title = "Cannot delete without idea ID";
    }

    actions.append(renameButton, deleteButton);
    cell.appendChild(actions);
    return cell;
}

function getLevelDisplayName(level) {
    const start = level.start || {};
    const end = level.end || {};
    return normalizeCreativeIdeaText(end.levelDisplayName)
        || normalizeCreativeIdeaText(start.levelDisplayName)
        || value(start.roundLevelIndex || end.roundLevelIndex || start.levelIndex || end.levelIndex);
}

async function renameLevelRun(level) {
    const levelRunId = normalizeCreativeIdeaText(level.levelRunId);
    const nextName = window.prompt("Rename level", getLevelDisplayName(level));

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
        const response = await fetch(apiUrl("/rename-level-run"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                levelRunId: levelRunId,
                displayName: displayName
            })
        });

        if (!response.ok) {
            throw new Error(await getResponseError(response));
        }

        showNotice("Level renamed.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not rename level: " + error.message);
    }
}

function deleteLevelRun(level) {
    return deleteIdeaRecords(getLevelIdeaId(level), getLevelIdeaHash(level));
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
    const rawResponses = (state.payload && state.payload.surveyResponses) || [];
    const responses = groupSurveyResponses(rawResponses);
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
            getSurveyIdeaHash(response),
            value(response.playerNickname || response.playerName || response.nickname)
        ];

        cells.forEach((text, index) => {
            const cell = document.createElement("td");
            if (index === 1) {
                cell.className = "small";
            }
            cell.textContent = text;
            row.appendChild(cell);
        });

        const answersCell = document.createElement("td");
        answersCell.className = "answers-cell";
        renderSurveyAnswerLines(answersCell, response);
        row.appendChild(answersCell);

        const actionsCell = document.createElement("td");
        const deleteButton = document.createElement("button");
        const ideaId = getSurveyIdeaId(response);
        deleteButton.className = "round-action round-action-danger";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";

        if (ideaId) {
            deleteButton.title = "Delete all records for this idea";
            deleteButton.addEventListener("click", event => {
                event.stopPropagation();
                deleteSurveyResponse(response);
            });
        } else {
            deleteButton.disabled = true;
            deleteButton.title = "Cannot delete without idea ID";
        }

        actionsCell.appendChild(deleteButton);
        row.appendChild(actionsCell);

        elements.surveyBody.appendChild(row);
    });
}

function groupSurveyResponses(responses) {
    const groups = new Map();

    responses.forEach((response, index) => {
        const sessionId = normalizeSurveyDeleteText(response && response.sessionId);
        const responseId = normalizeSurveyDeleteText(response && response.responseId);
        const key = sessionId
            ? "session:" + sessionId
            : "response:" + (responseId || String(index));

        if (!groups.has(key)) {
            groups.set(key, []);
        }

        groups.get(key).push(response);
    });

    return Array.from(groups.values())
        .map(mergeSurveyResponseGroup)
        .sort((left, right) => getSurveyResponseTimestamp(right)
            .localeCompare(getSurveyResponseTimestamp(left)));
}

function mergeSurveyResponseGroup(responses) {
    const ordered = responses.slice().sort((left, right) =>
        getSurveyResponseTimestamp(left).localeCompare(getSurveyResponseTimestamp(right))
    );
    const merged = Object.assign({}, ...ordered);
    const answersByQuestion = new Map();

    ordered.forEach(response => {
        const answers = response && Array.isArray(response.answerDetails)
            ? response.answerDetails
            : response && Array.isArray(response.answers)
                ? response.answers
                : [];

        answers.forEach(answer => {
            answersByQuestion.set(String(answer.questionIndex), answer);
        });
    });

    merged.answerDetails = Array.from(answersByQuestion.values()).sort((left, right) =>
        Number(left.questionIndex) - Number(right.questionIndex)
    );
    merged.answersSummary = "";

    const responseWithIdea = ordered.slice().reverse().find(response =>
        getSurveyIdeaId(response)
    );

    if (responseWithIdea) {
        merged.creativeIdeaId = getSurveyIdeaId(responseWithIdea);
        merged.ideaHash = responseWithIdea.ideaHash || responseWithIdea.creativeIdeaHash;
    }

    const nickname = ordered.map(getSurveyNickname).find(Boolean);

    if (nickname) {
        merged.playerNickname = nickname;
        merged.playerName = nickname;
    }

    return merged;
}

function getSurveyResponseTimestamp(response) {
    return String(response && (response.serverReceivedAt || response.timestamp) || "");
}

function deleteSurveyResponse(response) {
    return deleteIdeaRecords(getSurveyIdeaId(response), getSurveyIdeaHash(response));
}

function deleteCreativeIdea(idea) {
    return deleteIdeaRecords(getCreativeIdeaId(idea), getCreativeIdeaHash(idea));
}

function deleteExpansionChoice(choice) {
    return deleteIdeaRecords(
        getExpansionChoiceIdeaId(choice),
        getExpansionChoiceIdeaHash(choice)
    );
}

async function deleteIdeaRecords(ideaId, ideaHash) {
    if (!ideaId) {
        return;
    }

    const confirmed = window.confirm(
        "Delete all records for Idea Hash " + ideaHash
        + "? This permanently removes matching Level Runs, Creative Workshop Ideas, "
        + "Expansion Choices, and Survey Responses."
    );

    if (!confirmed) {
        return;
    }

    setStatus("Deleting idea records...");

    try {
        const apiResponse = await fetch(apiUrl("/delete-idea-records"), {
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

        state.selectedRunId = null;
        showNotice("All records for Idea Hash " + ideaHash + " deleted.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not delete idea records: " + error.message);
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

function getExpansionChoiceDeleteLabel(choice) {
    const choiceLabel = getExpansionChoiceLabel(choice);
    const ideaHash = getExpansionChoiceIdeaHash(choice);
    const label = [choiceLabel, ideaHash]
        .filter(text => text && text !== "-")
        .join(" / ");

    if (!label) {
        return shortId(choice.choiceId);
    }

    return label.length > 48 ? label.slice(0, 48) + "..." : label;
}

function getLevelIdeaHash(level) {
    const start = (level && level.start) || {};
    const end = (level && level.end) || {};
    return getIdeaHash(
        start.creativeIdeaId || end.creativeIdeaId,
        start.creativeIdeaText || end.creativeIdeaText
    );
}

function getLevelIdeaId(level) {
    const start = (level && level.start) || {};
    const end = (level && level.end) || {};
    return normalizeCreativeIdeaText(start.creativeIdeaId || end.creativeIdeaId);
}

function getCreativeIdeaId(idea) {
    return normalizeCreativeIdeaText(idea && idea.ideaId);
}

function getExpansionChoiceIdeaId(choice) {
    return normalizeCreativeIdeaText(choice && choice.ideaId);
}

function getSurveyIdeaId(response) {
    return normalizeCreativeIdeaText(response && response.creativeIdeaId);
}

function getCreativeIdeaHash(idea) {
    const directHash = normalizeCreativeIdeaText(
        idea && (idea.ideaHash || idea.creativeIdeaHash)
    );

    if (directHash) {
        return directHash;
    }

    return getIdeaHash(
        idea && idea.ideaId,
        idea && idea.ideaText
    );
}

function getExpansionChoiceIdeaHash(choice) {
    const directHash = normalizeCreativeIdeaText(
        choice && (choice.ideaHash || choice.creativeIdeaHash)
    );

    if (directHash) {
        return directHash;
    }

    return getIdeaHash(
        choice && choice.ideaId,
        choice && (choice.originalIdeaText || choice.finalIdeaText)
    );
}

function getSurveyIdeaHash(response) {
    const directHash = normalizeCreativeIdeaText(
        response && (response.ideaHash || response.creativeIdeaHash)
    );

    if (directHash) {
        return directHash;
    }

    const directIdeaId = normalizeCreativeIdeaText(
        response && (response.ideaId || response.creativeIdeaId)
    );

    if (directIdeaId) {
        return getIdeaHash(directIdeaId, response.creativeIdeaText || response.ideaText);
    }

    const sessionId = normalizeCreativeIdeaText(response && response.sessionId);

    if (!sessionId || !state.payload) {
        return "-";
    }

    const idea = (state.payload.creativeIdeas || []).find(candidate =>
        normalizeCreativeIdeaText(candidate && candidate.sessionId) === sessionId
    );

    if (idea) {
        return getCreativeIdeaHash(idea);
    }

    const choice = (state.payload.creativeExpansionChoices || []).find(candidate =>
        normalizeCreativeIdeaText(candidate && candidate.sessionId) === sessionId
    );

    return choice ? getExpansionChoiceIdeaHash(choice) : "-";
}

function getIdeaHash(primaryValue, fallbackValue) {
    const primary = normalizeCreativeIdeaText(primaryValue);
    const fallback = normalizeCreativeIdeaText(fallbackValue);
    const source = primary || fallback;

    if (!source) {
        return "-";
    }

    return stableShortHash(source);
}

function stableShortHash(input) {
    let hash = 2166136261;
    const text = String(input);

    for (let index = 0; index < text.length; index += 1) {
        hash ^= text.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
    }

    return (hash >>> 0).toString(16).padStart(8, "0");
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
    const displayLevel = getLevelDisplayName(level);
    elements.selectedTitle.textContent = "Level " + value(displayLevel);
    renderMap(rows);

    [
        ["Round", value(level.roundDisplayName)],
        ["Run", shortId(level.levelRunId)],
        ["Idea hash", getLevelIdeaHash(level)],
        ["Map hash", value(structure.mapHash)],
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
