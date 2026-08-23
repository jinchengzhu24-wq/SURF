const DEFAULT_API_BASE = "http://111.231.136.4:8000";
const DASHBOARD_LEVEL_SCENE = "Custom_Level";

const state = {
    apiBase: resolveApiBase(),
    payload: null,
    journeys: [],
    filteredJourneys: [],
    selectedJourneyKey: "",
    selectedStageKey: "",
    selectedRunId: "",
    compareRunIds: [],
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
    legacyLink: document.getElementById("legacyLink"),
    docsLink: document.getElementById("docsLink"),
    statIdeas: document.getElementById("statIdeas"),
    statCompleted: document.getElementById("statCompleted"),
    statCompletionRate: document.getElementById("statCompletionRate"),
    statAvg: document.getElementById("statAvg"),
    statAnomalies: document.getElementById("statAnomalies"),
    statDataHealth: document.getElementById("statDataHealth"),
    searchInput: document.getElementById("searchInput"),
    statusFilter: document.getElementById("statusFilter"),
    modeFilter: document.getElementById("modeFilter"),
    ideaCount: document.getElementById("ideaCount"),
    ideaList: document.getElementById("ideaList"),
    emptyDetail: document.getElementById("emptyDetail"),
    journeyDetail: document.getElementById("journeyDetail"),
    selectedIdeaTitle: document.getElementById("selectedIdeaTitle"),
    selectedIdeaStatus: document.getElementById("selectedIdeaStatus"),
    selectedIdeaText: document.getElementById("selectedIdeaText"),
    selectedIdeaMeta: document.getElementById("selectedIdeaMeta"),
    deleteIdeaButton: document.getElementById("deleteIdeaButton"),
    journeyTimeline: document.getElementById("journeyTimeline"),
    levelCount: document.getElementById("levelCount"),
    levelTabs: document.getElementById("levelTabs"),
    mapGrid: document.getElementById("mapGrid"),
    detailMetrics: document.getElementById("detailMetrics"),
    inspectorTitle: document.getElementById("inspectorTitle"),
    inspectorBody: document.getElementById("inspectorBody"),
    deleteStageButton: document.getElementById("deleteStageButton"),
    comparePanel: document.getElementById("comparePanel"),
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
    wireLinks();
    wireEvents();

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
    elements.clearButton.addEventListener("click", clearAllRecords);
    elements.searchInput.addEventListener("input", applyFilters);
    elements.statusFilter.addEventListener("change", applyFilters);
    elements.modeFilter.addEventListener("change", applyFilters);
    elements.deleteIdeaButton.addEventListener("click", deleteSelectedIdea);
    elements.deleteStageButton.addEventListener("click", deleteSelectedStage);
    elements.deleteDialogForm.addEventListener("submit", submitDeleteDialog);
    elements.deleteDialogCancel.addEventListener("click", closeDeleteDialog);
    elements.deleteDialog.addEventListener("cancel", event => {
        event.preventDefault();

        if (!state.deleteDialogBusy) {
            closeDeleteDialog();
        }
    });

    document.querySelectorAll("[data-summary-filter]").forEach(button => {
        button.addEventListener("click", () => {
            elements.statusFilter.value = button.dataset.summaryFilter || "all";
            applyFilters();
        });
    });
}

async function loadData(manual) {
    setStatus(manual ? "Refreshing study journeys..." : "Loading study journeys...");

    try {
        const response = await fetch(apiUrl("/level-records-data"), { cache: "no-store" });

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        state.payload = await response.json();
        state.journeys = buildIdeaJourneys(state.payload);
        restoreSelectionFromUrl();
        renderSummary();
        applyFilters();
        elements.dataSource.textContent = "API: " + state.apiBase;
        setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
    } catch (error) {
        setStatus("Could not load study records: " + error.message);
        elements.ideaList.innerHTML = '<div class="empty-state">Failed to load study journeys.</div>';
        showEmptyDetail();
    }
}

function buildIdeaJourneys(payload) {
    const journeys = new Map();
    const sessionIndex = new Map();
    let orphanIndex = 0;

    function createJourney(ideaId, sessionId) {
        const cleanIdeaId = clean(ideaId);
        const cleanSessionId = clean(sessionId);
        const key = cleanIdeaId
            ? "idea:" + cleanIdeaId
            : cleanSessionId
                ? "session:" + cleanSessionId
                : "orphan:" + (++orphanIndex);

        if (!journeys.has(key)) {
            journeys.set(key, {
                key,
                ideaId: cleanIdeaId,
                ideaHash: "",
                ideaText: "",
                sessions: new Set(),
                creativeIdeas: [],
                expansions: [],
                levels: [],
                surveys: [],
                haEvents: [],
                journeyEvents: []
            });
        }

        const journey = journeys.get(key);

        if (cleanSessionId) {
            journey.sessions.add(cleanSessionId);
            if (!sessionIndex.has(cleanSessionId)) {
                sessionIndex.set(cleanSessionId, journey);
            }
        }

        return journey;
    }

    function findJourney(ideaId, sessionId) {
        const cleanIdeaId = clean(ideaId);
        const cleanSessionId = clean(sessionId);
        const directKey = cleanIdeaId ? "idea:" + cleanIdeaId : "";

        if (directKey && journeys.has(directKey)) {
            return createJourney(cleanIdeaId, cleanSessionId);
        }

        if (cleanSessionId && sessionIndex.has(cleanSessionId)) {
            const journey = sessionIndex.get(cleanSessionId);
            journey.sessions.add(cleanSessionId);

            if (!journey.ideaId && cleanIdeaId) {
                journey.ideaId = cleanIdeaId;
            }

            return journey;
        }

        return createJourney(cleanIdeaId, cleanSessionId);
    }

    safeArray(payload.creativeIdeas).forEach(record => {
        findJourney(record.ideaId, record.sessionId).creativeIdeas.push(record);
    });

    safeArray(payload.creativeExpansionChoices).forEach(record => {
        findJourney(record.ideaId, record.sessionId).expansions.push(record);
    });

    safeArray(payload.levels)
        .filter(isDashboardLevel)
        .forEach(level => {
            const start = level.start || {};
            const end = level.end || {};
            findJourney(
                start.creativeIdeaId || end.creativeIdeaId,
                start.sessionId || end.sessionId
            ).levels.push(level);
        });

    safeArray(payload.haPlanEvents).forEach(record => {
        findJourney(record.ideaId, record.sessionId).haEvents.push(record);
    });

    safeArray(payload.journeyEvents).forEach(record => {
        findJourney(record.ideaId, record.sessionId).journeyEvents.push(record);
    });

    safeArray(payload.surveyResponses).forEach(record => {
        findJourney(
            record.creativeIdeaId || record.ideaId,
            record.sessionId
        ).surveys.push(record);
    });

    return Array.from(journeys.values())
        .map(finalizeJourney)
        .filter(journey => journey.ideaId || journey.ideaText || journey.levels.length)
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
}

function finalizeJourney(journey) {
    const creative = journey.creativeIdeas[0] || {};
    const expansion = journey.expansions[0] || {};
    const firstLevel = journey.levels[0] || {};
    const levelStart = firstLevel.start || {};
    journey.ideaText = firstValue(
        creative.ideaText,
        expansion.originalIdeaText,
        levelStart.creativeIdeaText,
        expansion.finalIdeaText
    );
    journey.ideaId = firstValue(
        journey.ideaId,
        creative.ideaId,
        expansion.ideaId,
        levelStart.creativeIdeaId
    );
    journey.ideaHash = getIdeaHash(journey.ideaId, journey.ideaText);
    journey.levels.sort((left, right) => getLevelTimestamp(left).localeCompare(getLevelTimestamp(right)));
    journey.creativeIdeas.sort(sortByTimestamp);
    journey.expansions.sort(sortByTimestamp);
    journey.surveys.sort(sortByTimestamp);
    journey.haEvents.sort(sortByTimestamp);
    journey.journeyEvents.sort(sortByTimestamp);

    const timestamps = []
        .concat(journey.creativeIdeas, journey.expansions, journey.surveys, journey.haEvents, journey.journeyEvents)
        .map(getRecordTimestamp)
        .concat(journey.levels.map(getLevelTimestamp))
        .filter(Boolean);
    journey.updatedAt = timestamps.sort().pop() || "";
    journey.hasMissingEnd = journey.levels.some(level => !level.end);
    journey.completed = journey.levels.some(level => level.end && level.end.completed);
    journey.status = journey.hasMissingEnd
        ? "anomaly"
        : journey.completed
            ? "completed"
            : "progress";
    journey.revisionModes = new Set(
        journey.journeyEvents
            .map(event => clean(event.revisionMode).toLowerCase())
            .filter(mode => ["ai", "human", "ha"].includes(mode))
    );

    if (journey.haEvents.length > 0) {
        journey.revisionModes.add("ha");
    }
    journey.nicknames = Array.from(new Set(
        journey.surveys.map(getSurveyNickname).filter(Boolean)
    ));
    return journey;
}

function applyFilters() {
    const query = clean(elements.searchInput.value).toLowerCase();
    const status = elements.statusFilter.value;
    const mode = elements.modeFilter.value;

    state.filteredJourneys = state.journeys.filter(journey => {
        if (status !== "all" && journey.status !== status) {
            return false;
        }

        if (mode !== "all" && !journey.revisionModes.has(mode)) {
            return false;
        }

        if (!query) {
            return true;
        }

        const haystack = [
            journey.ideaHash,
            journey.ideaId,
            journey.ideaText,
            ...journey.nicknames,
            ...Array.from(journey.sessions)
        ].join(" ").toLowerCase();
        return haystack.includes(query);
    });

    renderIdeaList();
    keepOrSelectJourney();
}

function renderSummary() {
    const completed = state.journeys.filter(journey => journey.completed).length;
    const anomalies = state.journeys.filter(journey => journey.status === "anomaly").length;
    const endedLevels = state.journeys
        .flatMap(journey => journey.levels)
        .filter(level => level.end && typeof level.end.durationSeconds === "number");
    const averageDuration = endedLevels.length
        ? endedLevels.reduce((sum, level) => sum + level.end.durationSeconds, 0) / endedLevels.length
        : null;
    const malformed = [
        state.payload.malformedCount,
        state.payload.surveyMalformedCount,
        state.payload.creativeIdeaMalformedCount,
        state.payload.creativeExpansionChoiceMalformedCount,
        state.payload.haPlanMalformedCount,
        state.payload.journeyEventMalformedCount
    ].reduce((sum, count) => sum + numeric(count), 0);
    const rate = state.journeys.length > 0
        ? Math.round(completed / state.journeys.length * 100)
        : 0;

    elements.statIdeas.textContent = state.journeys.length;
    elements.statCompleted.textContent = completed;
    elements.statCompletionRate.textContent = rate + "% completion rate";
    elements.statAvg.textContent = formatSeconds(averageDuration);
    elements.statAnomalies.textContent = anomalies;
    elements.statDataHealth.textContent = malformed > 0
        ? malformed + " malformed records"
        : anomalies > 0
            ? anomalies + " incomplete journeys"
            : "no data issues";
}

function renderIdeaList() {
    elements.ideaList.textContent = "";
    elements.ideaCount.textContent = state.filteredJourneys.length + " shown";

    if (state.filteredJourneys.length === 0) {
        elements.ideaList.appendChild(emptyNode("No matching idea journeys."));
        return;
    }

    state.filteredJourneys.forEach(journey => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "idea-item";

        if (journey.key === state.selectedJourneyKey) {
            button.classList.add("selected");
        }

        const top = document.createElement("div");
        top.className = "idea-item-top";
        top.append(
            textNode("span", journey.ideaHash, "idea-hash"),
            textNode("span", formatShortDate(journey.updatedAt))
        );
        const snippet = textNode("p", journey.ideaText || "Idea text unavailable", "idea-snippet");
        const bottom = document.createElement("div");
        bottom.className = "idea-item-bottom";
        const status = textNode("span", statusLabel(journey.status), "mini-status " + journey.status);
        const counts = textNode(
            "span",
            plural(journey.levels.length, "version") + " · " + plural(journey.surveys.length, "survey")
        );
        bottom.append(status, counts);
        button.append(top, snippet, bottom);
        button.addEventListener("click", () => selectJourney(journey.key));
        elements.ideaList.appendChild(button);
    });
}

function keepOrSelectJourney() {
    let journey = state.filteredJourneys.find(item => item.key === state.selectedJourneyKey);

    if (!journey) {
        journey = state.filteredJourneys[0] || null;
        state.selectedJourneyKey = journey ? journey.key : "";
        state.selectedStageKey = "";
        state.selectedRunId = "";
        state.compareRunIds = [];
        renderIdeaList();
    }

    if (journey) {
        renderJourney(journey);
    } else {
        showEmptyDetail();
    }
}

function selectJourney(key) {
    if (state.selectedJourneyKey !== key) {
        state.selectedJourneyKey = key;
        state.selectedStageKey = "";
        state.selectedRunId = "";
        state.compareRunIds = [];
    }

    renderIdeaList();
    const journey = getSelectedJourney();

    if (journey) {
        renderJourney(journey);
        updateUrlSelection(journey);
    }
}

function renderJourney(journey) {
    elements.emptyDetail.hidden = true;
    elements.journeyDetail.hidden = false;
    elements.selectedIdeaTitle.textContent = "Idea " + journey.ideaHash;
    elements.selectedIdeaText.textContent = journey.ideaText || "Idea text unavailable.";
    elements.selectedIdeaStatus.textContent = statusLabel(journey.status);
    elements.selectedIdeaStatus.className = "status-chip " + journey.status;
    elements.selectedIdeaMeta.textContent = "";
    [
        plural(journey.levels.length, "level version"),
        plural(journey.expansions.length, "expansion choice"),
        plural(journey.haEvents.length, "HA event"),
        plural(journey.surveys.length, "survey"),
        journey.nicknames.length ? "Participant: " + journey.nicknames.join(", ") : "",
        journey.sessions.size ? "Session " + shortId(Array.from(journey.sessions)[0]) : ""
    ].filter(Boolean).forEach(text => {
        elements.selectedIdeaMeta.appendChild(textNode("span", text));
    });

    const stages = buildTimelineStages(journey);
    const selectedExists = stages.some(stage => stage.key === state.selectedStageKey);

    if (!selectedExists) {
        state.selectedStageKey = stages.length ? stages[stages.length - 1].key : "";
    }

    if (!state.selectedRunId || !journey.levels.some(level => level.levelRunId === state.selectedRunId)) {
        const latestLevel = journey.levels[journey.levels.length - 1];
        state.selectedRunId = latestLevel ? latestLevel.levelRunId : "";
    }

    renderTimeline(stages);
    renderLevelVersions(journey, stages);
    renderSelectedLevel(journey);
    renderInspector(stages.find(stage => stage.key === state.selectedStageKey) || null);
    renderComparison(journey);
}

function buildTimelineStages(journey) {
    const stages = [];

    journey.creativeIdeas.forEach((record, index) => stages.push({
        key: "creative:" + (clean(record.ideaId) || index),
        type: "creative",
        label: index === 0 ? "Original idea" : "Idea update",
        timestamp: getRecordTimestamp(record),
        record
    }));

    journey.expansions.forEach((record, index) => stages.push({
        key: "expansion:" + (clean(record.choiceId) || index),
        type: "expansion",
        label: "Expansion",
        timestamp: getRecordTimestamp(record),
        record
    }));

    journey.levels.forEach((level, index) => stages.push({
        key: "level:" + level.levelRunId,
        type: "level",
        label: "Level V" + (index + 1),
        timestamp: getLevelTimestamp(level),
        warning: !level.end,
        level,
        record: level
    }));

    journey.journeyEvents.forEach((record, index) => {
        const phase = clean(record.phase).toLowerCase();
        const labels = {
            review: "Review",
            routing: "Route choice",
            adjustment: "Adjustment"
        };
        stages.push({
            key: "journey:" + (clean(record.journeyEventId) || index),
            type: "journey",
            label: labels[phase] || titleCase(phase || "Journey event"),
            timestamp: getRecordTimestamp(record),
            record
        });
    });

    journey.haEvents.forEach((record, index) => stages.push({
        key: "ha:" + (clean(record.haEventId) || index),
        type: "ha",
        label: record.eventType === "ha-plan-choice" ? "HA choice" : "HA plans",
        timestamp: getRecordTimestamp(record),
        warning: Boolean(clean(record.error)),
        record
    }));

    journey.surveys.forEach((record, index) => stages.push({
        key: "survey:" + (clean(record.responseId) || index),
        type: "survey",
        label: surveyStageLabel(record, index),
        timestamp: getRecordTimestamp(record),
        record
    }));

    return stages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

function renderTimeline(stages) {
    elements.journeyTimeline.textContent = "";

    if (stages.length === 0) {
        elements.journeyTimeline.appendChild(emptyNode("No journey stages recorded."));
        return;
    }

    stages.forEach((stage, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "timeline-node";

        if (stage.key === state.selectedStageKey) {
            button.classList.add("selected");
        }

        if (stage.warning) {
            button.classList.add("warning");
        }

        button.append(
            textNode("span", stage.warning ? "!" : String(index + 1), "timeline-dot"),
            textNode("span", stage.label, "timeline-label"),
            textNode("span", formatShortDate(stage.timestamp), "timeline-time")
        );
        button.addEventListener("click", () => {
            state.selectedStageKey = stage.key;

            if (stage.type === "level") {
                state.selectedRunId = stage.level.levelRunId;
            }

            const journey = getSelectedJourney();
            if (journey) renderJourney(journey);
        });
        elements.journeyTimeline.appendChild(button);
    });
}

function renderLevelVersions(journey, stages) {
    elements.levelTabs.textContent = "";
    elements.levelCount.textContent = plural(journey.levels.length, "version");

    if (journey.levels.length === 0) {
        elements.levelTabs.appendChild(emptyNode("No level versions recorded."));
        return;
    }

    journey.levels.forEach((level, index) => {
        const wrapper = document.createElement("button");
        wrapper.type = "button";
        wrapper.className = "level-tab";

        if (level.levelRunId === state.selectedRunId) {
            wrapper.classList.add("selected");
        }

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.className = "compare-check";
        checkbox.title = "Include this version in comparison";
        checkbox.checked = state.compareRunIds.includes(level.levelRunId);
        checkbox.addEventListener("click", event => {
            event.stopPropagation();
            toggleCompareRun(level.levelRunId);
        });
        wrapper.append(
            checkbox,
            textNode("span", "V" + (index + 1)),
            textNode("span", getLevelStatus(level).short)
        );
        wrapper.addEventListener("click", () => {
            state.selectedRunId = level.levelRunId;
            const levelStage = stages.find(stage =>
                stage.type === "level" && stage.level.levelRunId === level.levelRunId
            );
            state.selectedStageKey = levelStage ? levelStage.key : state.selectedStageKey;
            renderJourney(journey);
            updateUrlSelection(journey);
        });
        elements.levelTabs.appendChild(wrapper);
    });
}

function toggleCompareRun(runId) {
    const position = state.compareRunIds.indexOf(runId);

    if (position >= 0) {
        state.compareRunIds.splice(position, 1);
    } else if (state.compareRunIds.length < 2) {
        state.compareRunIds.push(runId);
    } else {
        state.compareRunIds.shift();
        state.compareRunIds.push(runId);
    }

    const journey = getSelectedJourney();
    if (journey) renderJourney(journey);
}

function renderSelectedLevel(journey) {
    const level = journey.levels.find(item => item.levelRunId === state.selectedRunId) || null;
    elements.mapGrid.textContent = "";
    elements.detailMetrics.textContent = "";

    if (!level) {
        elements.mapGrid.textContent = "No level selected.";
        elements.mapGrid.style.gridTemplateColumns = "";
        return;
    }

    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    renderMap(elements.mapGrid, safeArray(start.rows), false);

    [
        ["Status", getLevelStatus(level).label],
        ["Play time", formatSeconds(end.durationSeconds)],
        ["Moves", value(end.moveCount)],
        ["Pushes", value(end.pushCount)],
        ["Restarts", value(end.restartCount)],
        ["Solver steps", value(start.solutionSteps)],
        ["Solver pushes", value(start.solverPushes)],
        ["Attempts", value(start.generationAttempts)],
        ["Wall density", formatRatio(structure.wallDensity)],
        ["Water density", formatRatio(structure.waterDensity)],
        ["Reachable", formatPercent(structure.reachableAreaRatio)],
        ["Dead-corner risk", formatRatio(structure.deadCornerRisk)]
    ].forEach(([label, metricValue]) => {
        const item = document.createElement("div");
        item.className = "metric";
        item.append(textNode("span", label), textNode("strong", metricValue));
        elements.detailMetrics.appendChild(item);
    });
}

function renderInspector(stage) {
    elements.inspectorBody.textContent = "";
    elements.deleteStageButton.hidden = true;
    elements.deleteStageButton.dataset.stageKey = "";

    if (!stage) {
        elements.inspectorTitle.textContent = "Journey details";
        elements.inspectorBody.appendChild(emptyNode("Select a journey stage to inspect it."));
        return;
    }

    elements.inspectorTitle.textContent = stage.label;
    const deleteConfig = getStageDeleteConfig(stage);

    if (deleteConfig) {
        elements.deleteStageButton.hidden = false;
        elements.deleteStageButton.dataset.stageKey = stage.key;
    }

    if (stage.type === "creative") renderCreativeInspector(stage.record);
    if (stage.type === "expansion") renderExpansionInspector(stage.record);
    if (stage.type === "level") renderLevelInspector(stage.level);
    if (stage.type === "survey") renderSurveyInspector(stage.record);
    if (stage.type === "ha") renderHAInspector(stage.record);
    if (stage.type === "journey") renderJourneyEventInspector(stage.record);
}

function renderCreativeInspector(record) {
    appendTextSection("Idea submitted", record.ideaText || record.idea || "-");
    appendRecordGrid([
        ["Idea ID", record.ideaId],
        ["Session", record.sessionId],
        ["Scene", record.sceneName],
        ["Recorded", formatTimestamp(getRecordTimestamp(record))]
    ]);
}

function renderExpansionInspector(record) {
    appendTextSection("Original idea", record.originalIdeaText || "-");
    appendTextSection(
        "Selected direction",
        [record.selectedOptionId, record.selectedOptionTitle].filter(Boolean).join(" · ") || "-"
    );
    appendTextSection("Option description", record.selectedOptionDescription || "-");
    appendTextSection("Final idea", record.finalIdeaText || "-");
}

function renderLevelInspector(level) {
    const start = level.start || {};
    const end = level.end || {};
    const structure = start.structure || {};
    appendRecordGrid([
        ["Level run", level.levelRunId],
        ["Map hash", structure.mapHash],
        ["Source", start.source],
        ["Status", getLevelStatus(level).label],
        ["Started", formatTimestamp(getLevelTimestamp(level))],
        ["Ended", end.timestamp ? formatTimestamp(end.timestamp) : "Missing end record"],
        ["Solution", value(start.solutionSteps) + " steps / " + value(start.solverPushes) + " pushes"],
        ["Player", value(end.moveCount) + " moves / " + value(end.pushCount) + " pushes"]
    ]);
    appendTextSection("Idea context", start.creativeIdeaText || end.creativeIdeaText || "-");
}

function renderSurveyInspector(record) {
    appendRecordGrid([
        ["Survey", record.surveyTitle || record.surveyId],
        ["Participant", getSurveyNickname(record) || "-"],
        ["Duration", formatSeconds(record.durationSeconds)],
        ["Scene", record.sceneName],
        ["Recorded", formatTimestamp(getRecordTimestamp(record))]
    ]);
    const answers = safeArray(record.answerDetails).length
        ? record.answerDetails
        : safeArray(record.answers);
    const section = createSection("Answers");

    if (answers.length === 0) {
        section.appendChild(textNode("p", "No answers recorded."));
    } else {
        answers.forEach(answer => {
            const card = document.createElement("div");
            card.className = "answer-card";
            card.append(
                textNode("span", answer.questionText || answer.questionId || "Question " + value(answer.questionIndex)),
                textNode("strong", answer.optionText || answer.optionLabel || answer.optionId || "-")
            );
            section.appendChild(card);
        });
    }

    elements.inspectorBody.appendChild(section);
}

function renderHAInspector(record) {
    appendRecordGrid([
        ["Event", record.eventType],
        ["Attempt", value(record.regenerationAttempt)],
        ["Selected", record.selectedOptionTitle || "-"],
        ["Recorded", formatTimestamp(getRecordTimestamp(record))]
    ]);
    appendTextSection("Adjustment request", record.adjustmentText || "-");

    if (record.selectedOptionDescription) {
        appendTextSection("Selected plan", record.selectedOptionDescription);
    }

    const options = safeArray(record.options).length
        ? record.options
        : safeArray(record.presentedOptions);

    if (options.length) {
        const section = createSection("Presented options");
        options.forEach(option => {
            const card = document.createElement("div");
            card.className = "option-card";

            if (clean(option.id) === clean(record.selectedOptionId)) {
                card.classList.add("selected");
            }

            card.append(
                textNode("strong", [option.id, option.title].filter(Boolean).join(" · ")),
                textNode("span", option.description || "-")
            );
            section.appendChild(card);
        });
        elements.inspectorBody.appendChild(section);
    }

    if (record.error) {
        appendTextSection("Generation error", record.error);
    }
}

function renderJourneyEventInspector(record) {
    appendRecordGrid([
        ["Phase", titleCase(record.phase)],
        ["Action", titleCase(record.action)],
        ["Revision mode", clean(record.revisionMode).toUpperCase() || "-"],
        ["Score", numeric(record.score) >= 0 ? value(record.score) : "-"],
        ["Scene", record.sceneName],
        ["Recorded", formatTimestamp(getRecordTimestamp(record))]
    ]);
    appendTextSection("Details", record.detailText || "-");
}

function renderComparison(journey) {
    elements.compareContent.textContent = "";
    const levels = state.compareRunIds
        .map(runId => journey.levels.find(level => level.levelRunId === runId))
        .filter(Boolean);

    if (levels.length !== 2) {
        elements.compareContent.appendChild(textNode(
            "div",
            "Select two version checkboxes to compare maps and player metrics.",
            "compare-placeholder"
        ));
        return;
    }

    const grid = document.createElement("div");
    grid.className = "compare-grid";
    levels.forEach(level => grid.appendChild(renderCompareVersion(level, journey.levels.indexOf(level) + 1)));
    elements.compareContent.appendChild(grid);
}

function renderCompareVersion(level, versionNumber) {
    const start = level.start || {};
    const end = level.end || {};
    const card = document.createElement("article");
    card.className = "compare-version";
    card.appendChild(textNode("h3", "Level V" + versionNumber + " · " + getLevelStatus(level).label));
    const frame = document.createElement("div");
    frame.className = "mini-map-frame";
    const map = document.createElement("div");
    map.className = "mini-map";
    renderMap(map, safeArray(start.rows), true);
    frame.appendChild(map);
    card.appendChild(frame);

    const table = document.createElement("table");
    table.className = "delta-table";
    [
        ["Play time", formatSeconds(end.durationSeconds)],
        ["Moves", value(end.moveCount)],
        ["Pushes", value(end.pushCount)],
        ["Restarts", value(end.restartCount)],
        ["Solver steps", value(start.solutionSteps)],
        ["Solver pushes", value(start.solverPushes)]
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

function deleteSelectedStage() {
    const journey = getSelectedJourney();
    const stage = journey
        ? buildTimelineStages(journey).find(item => item.key === elements.deleteStageButton.dataset.stageKey)
        : null;
    const config = stage ? getStageDeleteConfig(stage) : null;

    if (!config) {
        return;
    }

    openDeleteDialog({
        title: "Delete selected record?",
        description: "Enter the deletion password to remove only this stage.",
        scope: config.label + "\nOther records in this Idea journey will be kept.",
        confirmLabel: "Delete record",
        endpoint: config.endpoint,
        payload: config.payload,
        progressText: "Deleting " + config.label + "..."
    });
}

function getStageDeleteConfig(stage) {
    if (stage.type === "creative" && clean(stage.record.ideaId)) {
        return {
            endpoint: "/delete-creative-idea",
            payload: { ideaId: clean(stage.record.ideaId) },
            label: "creative idea record"
        };
    }

    if (stage.type === "expansion" && clean(stage.record.choiceId)) {
        return {
            endpoint: "/delete-expansion-choice",
            payload: { choiceId: clean(stage.record.choiceId) },
            label: "expansion choice"
        };
    }

    if (stage.type === "level" && clean(stage.level.levelRunId)) {
        return {
            endpoint: "/delete-level-run",
            payload: { levelRunId: clean(stage.level.levelRunId) },
            label: "level version"
        };
    }

    if (stage.type === "survey") {
        const surveyPayload = getSurveyDeletePayload(stage.record);
        return surveyPayload ? {
            endpoint: "/delete-survey-response",
            payload: surveyPayload,
            label: "survey response"
        } : null;
    }

    if (stage.type === "ha" && clean(stage.record.haEventId)) {
        return {
            endpoint: "/delete-ha-plan-event",
            payload: { haEventId: clean(stage.record.haEventId) },
            label: "HA event"
        };
    }

    if (stage.type === "journey" && clean(stage.record.journeyEventId)) {
        return {
            endpoint: "/delete-journey-event",
            payload: { journeyEventId: clean(stage.record.journeyEventId) },
            label: "journey event"
        };
    }

    return null;
}

function deleteSelectedIdea() {
    const journey = getSelectedJourney();

    if (!journey || !journey.ideaId) {
        return;
    }

    const scope = [
        plural(journey.creativeIdeas.length, "creative record"),
        plural(journey.expansions.length, "expansion choice"),
        plural(journey.levels.length, "level version"),
        plural(journey.surveys.length, "survey"),
        plural(journey.haEvents.length, "HA event"),
        plural(journey.journeyEvents.length, "review/routing event")
    ].join("\n");
    openDeleteDialog({
        title: "Delete entire Idea " + journey.ideaHash + "?",
        description: "This removes the complete study journey and cannot be undone.",
        scope: scope,
        confirmLabel: "Delete entire Idea",
        endpoint: "/delete-idea-records",
        payload: { ideaId: journey.ideaId },
        progressText: "Deleting the entire idea journey...",
        afterSuccess: resetSelection
    });
}

function openDeleteDialog(config) {
    state.pendingDelete = config;
    elements.deleteDialogTitle.textContent = config.title;
    elements.deleteDialogDescription.textContent = config.description;
    elements.deleteDialogScope.textContent = config.scope;
    elements.deleteDialogConfirm.textContent = config.confirmLabel || "Delete";
    elements.deletePasswordInput.value = "";
    elements.deleteDialogError.textContent = "";
    setDeleteDialogBusy(false);

    if (!elements.deleteDialog.open) {
        elements.deleteDialog.showModal();
    }

    window.requestAnimationFrame(() => elements.deletePasswordInput.focus());
}

function closeDeleteDialog() {
    if (state.deleteDialogBusy) {
        return;
    }

    if (elements.deleteDialog.open) {
        elements.deleteDialog.close();
    }

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

    if (busy) {
        elements.deleteDialogConfirm.textContent = "Deleting...";
    } else if (state.pendingDelete) {
        elements.deleteDialogConfirm.textContent = state.pendingDelete.confirmLabel || "Delete";
    }
}

async function submitDeleteDialog(event) {
    event.preventDefault();

    const config = state.pendingDelete;
    const password = elements.deletePasswordInput.value;

    if (!config || state.deleteDialogBusy) {
        return;
    }

    if (!password) {
        elements.deleteDialogError.textContent = "Enter the deletion password.";
        elements.deletePasswordInput.focus();
        return;
    }

    elements.deleteDialogError.textContent = "";
    setDeleteDialogBusy(true);
    setStatus(config.progressText);

    try {
        await postDelete(config.endpoint, config.payload, password);

        if (config.afterSuccess) {
            config.afterSuccess();
        }

        setDeleteDialogBusy(false);
        closeDeleteDialog();
        showNotice(config.successText || "Record deleted successfully.");
        await loadData(true);
    } catch (error) {
        const message = getDeleteDialogError(error);
        elements.deleteDialogError.textContent = message;
        setStatus("Could not delete record: " + message);
        setDeleteDialogBusy(false);
        elements.deletePasswordInput.focus();
        elements.deletePasswordInput.select();
    }
}

async function postDelete(endpoint, payload, password) {
    const response = await fetch(apiUrl(endpoint), {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-Delete-Password": password
        },
        body: JSON.stringify(payload)
    });

    if (!response.ok) {
        const error = new Error(await getResponseError(response));
        error.status = response.status;
        throw error;
    }
}

function getDeleteDialogError(error) {
    if (error.status === 401) {
        return "Incorrect password. Please try again.";
    }

    if (error.status === 503) {
        return "Deletion password is not configured on the server.";
    }

    return error.message || "Deletion failed. Please try again.";
}

function clearAllRecords() {
    openDeleteDialog({
        title: "Clear all study data?",
        description: "This removes every official study record and cannot be undone.",
        scope: "Ideas, levels, surveys, HA plans, and journey events",
        confirmLabel: "Clear all records",
        endpoint: "/clear-level-records",
        payload: {},
        progressText: "Clearing all study data...",
        successText: "All study data cleared.",
        afterSuccess: resetSelection
    });
}

function resetSelection() {
    state.selectedJourneyKey = "";
    state.selectedStageKey = "";
    state.selectedRunId = "";
    state.compareRunIds = [];
}

function getSurveyDeletePayload(record) {
    if (clean(record.responseId)) {
        return { responseId: clean(record.responseId) };
    }

    const nickname = getSurveyNickname(record);
    return nickname ? { playerNickname: nickname } : null;
}

function showEmptyDetail() {
    elements.emptyDetail.hidden = false;
    elements.journeyDetail.hidden = true;
}

function getSelectedJourney() {
    return state.journeys.find(journey => journey.key === state.selectedJourneyKey) || null;
}

function restoreSelectionFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const idea = clean(params.get("idea"));
    const run = clean(params.get("run"));

    if (idea && !state.selectedJourneyKey) {
        const journey = state.journeys.find(item =>
            item.ideaHash === idea || item.ideaId === idea
        );
        if (journey) state.selectedJourneyKey = journey.key;
    }

    if (run) {
        state.selectedRunId = run;
        state.selectedStageKey = "level:" + run;
    }
}

function updateUrlSelection(journey) {
    if (!window.history || window.location.protocol === "file:") {
        return;
    }

    const url = new URL(window.location.href);
    url.searchParams.set("idea", journey.ideaHash);

    if (state.selectedRunId) {
        url.searchParams.set("run", state.selectedRunId);
    } else {
        url.searchParams.delete("run");
    }

    window.history.replaceState(null, "", url);
}

function appendTextSection(title, text) {
    const section = createSection(title);
    section.appendChild(textNode("p", value(text)));
    elements.inspectorBody.appendChild(section);
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

function safeArray(value) {
    return Array.isArray(value) ? value : [];
}

function firstValue(...values) {
    return values.map(clean).find(Boolean) || "";
}

function clean(input) {
    if (input === null || input === undefined) return "";
    const text = String(input).trim();
    return text === "-" ? "" : text;
}

function value(input) {
    if (input === null || input === undefined || input === "") return "-";
    return String(input);
}

function numeric(input) {
    return typeof input === "number" && !Number.isNaN(input) ? input : 0;
}

function isDashboardLevel(level) {
    const start = (level && level.start) || {};
    const end = (level && level.end) || {};
    return (start.sceneName || end.sceneName) === DASHBOARD_LEVEL_SCENE;
}

function getIdeaHash(ideaId, ideaText) {
    const source = clean(ideaId) || clean(ideaText);
    return source ? stableShortHash(source) : "unknown";
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

function getRecordTimestamp(record) {
    return clean(record && (record.serverReceivedAt || record.timestamp));
}

function getLevelTimestamp(level) {
    const start = (level && level.start) || {};
    const end = (level && level.end) || {};
    return clean(
        start.timestamp
        || start.serverReceivedAt
        || start.gameRoundStartedAt
        || end.timestamp
        || end.serverReceivedAt
    );
}

function sortByTimestamp(left, right) {
    return getRecordTimestamp(left).localeCompare(getRecordTimestamp(right));
}

function getSurveyNickname(record) {
    return firstValue(record && record.playerName, record && record.playerNickname, record && record.nickname);
}

function surveyStageLabel(record, index) {
    const text = (
        clean(record.surveyId)
        + " "
        + clean(record.surveyTitle)
        + " "
        + clean(record.sceneName)
    ).toLowerCase();

    if (text.includes("before") || text.includes("pre")) return "Pre-survey";
    if (text.includes("after") || text.includes("post")) return "Post-survey";
    return "Survey " + (index + 1);
}

function getLevelStatus(level) {
    const end = level.end || null;

    if (!end) return { label: "Missing end", short: "missing" };
    if (end.completed) return { label: "Completed", short: "done" };
    return { label: titleCase(end.endReason || "Stopped"), short: "stopped" };
}

function statusLabel(status) {
    if (status === "completed") return "Completed";
    if (status === "anomaly") return "Needs attention";
    return "In progress";
}

function titleCase(input) {
    return clean(input)
        .replace(/[-_]+/g, " ")
        .replace(/\b\w/g, character => character.toUpperCase()) || "-";
}

function plural(count, singular) {
    return count + " " + (count === 1 ? singular : singular + "s");
}

function shortId(input) {
    const text = clean(input);
    return text ? text.slice(0, 8) : "-";
}

function formatSeconds(input) {
    if (typeof input !== "number" || Number.isNaN(input)) return "-";
    return input.toFixed(1) + "s";
}

function formatRatio(input) {
    if (typeof input !== "number" || Number.isNaN(input)) return "-";
    return input.toFixed(3);
}

function formatPercent(input) {
    if (typeof input !== "number" || Number.isNaN(input)) return "-";
    return Math.round(input * 100) + "%";
}

function formatTimestamp(input) {
    if (!input) return "-";
    const date = new Date(input);
    return Number.isNaN(date.getTime()) ? input : date.toLocaleString();
}

function formatShortDate(input) {
    if (!input) return "No time";
    const date = new Date(input);

    if (Number.isNaN(date.getTime())) {
        return input.slice(0, 10);
    }

    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
