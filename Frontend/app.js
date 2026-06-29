const DEFAULT_API_BASE = "http://111.231.136.4:8000";

const state = {
    apiBase: resolveApiBase(),
    payload: null,
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
    sourceFilter: document.getElementById("sourceFilter"),
    resultCount: document.getElementById("resultCount"),
    recordsBody: document.getElementById("recordsBody"),
    surveyCount: document.getElementById("surveyCount"),
    surveyBody: document.getElementById("surveyBody"),
    selectedTitle: document.getElementById("selectedTitle"),
    mapGrid: document.getElementById("mapGrid"),
    detailMetrics: document.getElementById("detailMetrics"),
    rawDetails: document.getElementById("rawDetails"),
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

        state.payload = await response.json();
        state.selectedRunId = null;
        renderSummary(state.payload.summary || {});
        renderSourceFilter(state.payload.summary && state.payload.summary.sourceCounts);
        applyFilters();
        renderSurveyTable();
        setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
        elements.dataSource.textContent = "API: " + state.apiBase;
    } catch (error) {
        setStatus("Could not load records: " + error.message);
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

        showNotice("Records cleared.");
        await loadData(true);
    } catch (error) {
        setStatus("Could not clear records: " + error.message);
    }
}

function renderSummary(summary) {
    const surveySummary = (state.payload && state.payload.surveySummary) || {};
    elements.statEvents.textContent = numberValue(summary.eventCount);
    elements.statLevels.textContent = numberValue(summary.levelCount);
    elements.statSessions.textContent = numberValue(summary.sessionCount);
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

    state.filteredLevels = (state.payload.levels || []).filter(level => {
        const start = level.start || {};
        const end = level.end || {};
        const structure = start.structure || {};
        const rowStatus = getStatusKey(level);
        const rowSource = value(start.source);
        const haystack = [
            level.levelRunId,
            start.sessionId,
            end.sessionId,
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
    });

    renderTable();
    keepOrSelectFirst();
}

function renderTable() {
    elements.recordsBody.textContent = "";
    elements.resultCount.textContent = state.filteredLevels.length + " shown";

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

    state.filteredLevels.forEach(level => {
        const row = document.createElement("tr");
        row.dataset.runId = level.levelRunId;

        if (level.levelRunId === state.selectedRunId) {
            row.classList.add("selected");
        }

        const start = level.start || {};
        const end = level.end || {};
        const structure = start.structure || {};
        const status = getStatus(level);
        const cells = [
            value(start.levelIndex || end.levelIndex),
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

            if (index === 1) {
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

        elements.recordsBody.appendChild(row);
    });
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
            shortId(response.sessionId),
            formatSeconds(response.durationSeconds),
            formatSurveyAnswers(response.answers)
        ];

        cells.forEach((text, index) => {
            const cell = document.createElement("td");

            if (index === 4) {
                cell.className = "answers-cell";
            }

            cell.textContent = text;
            row.appendChild(cell);
        });

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
        elements.rawDetails.textContent = "No level selected.";
        return;
    }

    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    const rows = Array.isArray(start.rows) ? start.rows : [];
    elements.selectedTitle.textContent = "Level " + value(start.levelIndex || end.levelIndex);
    renderMap(rows);

    [
        ["Session", shortId(start.sessionId || end.sessionId)],
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

    elements.rawDetails.textContent = JSON.stringify({
        start,
        end,
        events: level.events || []
    }, null, 2);
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

function formatSurveyAnswers(answers) {
    if (!Array.isArray(answers) || answers.length === 0) {
        return "-";
    }

    return answers.map(answer => {
        const index = value(answer.questionIndex);
        const option = value(answer.optionText || answer.optionId);
        return "Q" + index + ": " + option;
    }).join("; ");
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
