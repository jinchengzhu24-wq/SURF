const DEFAULT_API_BASE = "http://111.231.136.4:8000";

const state = {
    apiBase: resolveApiBase(),
    payload: null,
    filteredRounds: [],
    filteredLevels: [],
    selectedRunId: null,
    expandedRoundIds: new Set(),
    hasInitializedRoundExpansion: false
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
    sourceFilter: document.getElementById("sourceFilter"),
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
    elements.sourceFilter.addEventListener("change", applyFilters);
}

async function loadData(manual) {
    setStatus(manual ? "Refreshing records..." : "Loading records...");

    try {
        const response = await fetch(apiUrl("/level-records-data"), { cache: "no-store" });

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const previousSelectedRunId = state.selectedRunId;
        state.payload = await response.json();
        state.selectedRunId = previousSelectedRunId;

        if (!manual) {
            state.expandedRoundIds.clear();
            state.hasInitializedRoundExpansion = false;
        }

        renderSummary(state.payload.summary || {});
        renderSourceFilter(state.payload.summary && state.payload.summary.sourceCounts);
        applyFilters();
        renderSurveyTable();
        setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
        elements.dataSource.textContent = "API: " + state.apiBase;
    } catch (error) {
        setStatus("Could not load records: " + error.message);
        elements.recordsBody.innerHTML = '<tr><td colspan="8" class="empty-state">Failed to load records.</td></tr>';
        elements.surveyBody.innerHTML = '<tr><td colspan="4" class="empty-state">Failed to load survey responses.</td></tr>';
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
        state.expandedRoundIds.clear();
        state.hasInitializedRoundExpansion = false;
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

function renderSourceFilter(sourceCounts) {
    const currentValue = elements.sourceFilter.value;
    elements.sourceFilter.textContent = "";
    elements.sourceFilter.appendChild(new Option("All sources", "all"));

    Object.keys(sourceCounts || {}).sort().forEach(source => {
        elements.sourceFilter.appendChild(new Option(source, source));
    });

    if ([...elements.sourceFilter.options].some(option => option.value === currentValue)) {
        elements.sourceFilter.value = currentValue;
    }
}

function applyFilters() {
    if (!state.payload) {
        return;
    }

    const search = elements.searchInput.value.trim().toLowerCase();
    const status = elements.statusFilter.value;
    const source = elements.sourceFilter.value;
    const allRounds = getAllRounds();

    state.filteredRounds = [];
    state.filteredLevels = [];

    allRounds.forEach(round => {
        const matchingLevels = (round.levels || []).filter(level => (
            levelMatchesFilters(level, round, search, status, source)
        ));

        if (matchingLevels.length === 0) {
            return;
        }

        const filteredRound = buildFilteredRound(round, matchingLevels);
        state.filteredRounds.push(filteredRound);
        state.filteredLevels.push(...matchingLevels);
    });

    ensureRoundExpansion();
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

function levelMatchesFilters(level, round, search, status, source) {
    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    const rowStatus = getStatusKey(level);
    const rowSource = value(start.source);
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
        rowSource,
        structure.mapHash
    ].join(" ").toLowerCase();

    if (status !== "all" && rowStatus !== status) {
        return false;
    }

    if (source !== "all" && rowSource !== source) {
        return false;
    }

    return !search || haystack.includes(search);
}

function ensureRoundExpansion() {
    if (state.filteredRounds.length === 0) {
        return;
    }

    if (!state.hasInitializedRoundExpansion) {
        state.expandedRoundIds.clear();
        state.expandedRoundIds.add(state.filteredRounds[0].roundId);
        state.hasInitializedRoundExpansion = true;
        return;
    }

    const hasExpandedVisibleRound = state.filteredRounds.some(round => (
        state.expandedRoundIds.has(round.roundId)
    ));

    if (!hasExpandedVisibleRound) {
        state.expandedRoundIds.add(state.filteredRounds[0].roundId);
    }
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
        elements.recordsBody.appendChild(renderRoundRow(round));

        if (!state.expandedRoundIds.has(round.roundId)) {
            return;
        }

        round.levels.forEach(level => {
            elements.recordsBody.appendChild(renderLevelRow(level));
        });
    });
}

function renderRoundRow(round) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    const expanded = state.expandedRoundIds.has(round.roundId);
    row.className = "round-row";
    row.dataset.roundId = round.roundId;
    row.addEventListener("click", () => toggleRound(round.roundId));

    if (expanded) {
        row.classList.add("round-expanded");
    }

    cell.colSpan = 8;

    const content = document.createElement("div");
    content.className = "round-header-content";

    const toggle = document.createElement("button");
    toggle.className = "round-toggle";
    toggle.type = "button";
    toggle.title = expanded ? "Collapse round" : "Expand round";
    toggle.setAttribute("aria-label", toggle.title);
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.textContent = ">";

    const titleWrap = document.createElement("div");
    titleWrap.className = "round-title";

    const title = document.createElement("strong");
    title.textContent = round.displayName;
    titleWrap.appendChild(title);

    if (!round.isLegacy) {
        const id = document.createElement("span");
        id.className = "round-id";
        id.textContent = "#" + shortId(round.roundId);
        titleWrap.appendChild(id);
    }

    const meta = document.createElement("div");
    meta.className = "round-meta";
    [
        formatSceneNames(round.sceneNames),
        plural(round.levelCount, "level"),
        getRoundCompletionText(round),
        formatSeconds(round.totalDurationSeconds),
        formatOptionalTimestamp(round.startedAt)
    ].forEach(text => {
        const item = document.createElement("span");
        item.textContent = text;
        meta.appendChild(item);
    });

    const actions = document.createElement("div");
    actions.className = "round-actions";

    if (!round.isLegacy) {
        const renameButton = document.createElement("button");
        renameButton.className = "round-action";
        renameButton.type = "button";
        renameButton.title = "Rename round";
        renameButton.textContent = "Rename";
        renameButton.addEventListener("click", event => {
            event.stopPropagation();
            renameRound(round);
        });
        actions.appendChild(renameButton);
    }

    const deleteButton = document.createElement("button");
    deleteButton.className = "round-action round-action-danger";
    deleteButton.type = "button";
    deleteButton.title = "Delete round";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", event => {
        event.stopPropagation();
        deleteRound(round);
    });
    actions.appendChild(deleteButton);

    content.append(toggle, titleWrap, meta, actions);
    cell.appendChild(content);
    row.appendChild(cell);
    return row;
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
        value(start.source),
        formatSeconds(end.durationSeconds),
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
        } else if (index === 7) {
            cell.className = "small";
            cell.textContent = text;
        } else {
            cell.textContent = text;
        }

        row.appendChild(cell);
    });

    row.addEventListener("click", () => {
        state.selectedRunId = level.levelRunId;
        renderTable();
        renderDetails(level);
    });

    return row;
}

function toggleRound(roundId) {
    if (state.expandedRoundIds.has(roundId)) {
        state.expandedRoundIds.delete(roundId);
    } else {
        state.expandedRoundIds.add(roundId);
    }

    renderTable();
}

async function renameRound(round) {
    const nextName = window.prompt("Rename round", round.displayName);

    if (nextName === null) {
        return;
    }

    const displayName = nextName.trim();

    if (!displayName) {
        window.alert("Round name cannot be empty.");
        return;
    }

    setStatus("Renaming round...");

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

        showNotice("Round renamed.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not rename round: " + error.message);
    }
}

async function deleteRound(round) {
    const confirmed = window.confirm(
        "Delete " + round.displayName + "? This permanently removes its level records."
    );

    if (!confirmed) {
        return;
    }

    setStatus("Deleting round...");

    try {
        const response = await fetch(apiUrl("/delete-round"), {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                roundId: round.roundId
            })
        });

        if (!response.ok) {
            throw new Error(await getResponseError(response));
        }

        if ((round.levels || []).some(level => level.levelRunId === state.selectedRunId)) {
            state.selectedRunId = null;
        }

        state.expandedRoundIds.delete(round.roundId);
        showNotice("Round deleted.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not delete round: " + error.message);
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
        cell.colSpan = 4;
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

        elements.surveyBody.appendChild(row);
    });
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
        ["Source", value(start.source)],
        ["Status", getStatus(level).label],
        ["Duration", formatSeconds(end.durationSeconds)],
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

    return "Scene " + sceneNames.join(", ");
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
