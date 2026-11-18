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

init();
