import {
    DEFAULT_API_BASE,
    clean,
    createApiClient,
    createDeleteDialogController,
    createDomHelpers,
    createStatusController,
    firstValue,
    formatSeconds,
    formatShortDate,
    formatTimestamp,
    numeric,
    plural,
    safeArray,
    shortId
} from "../shared/api-utils.js";
import {
    buildTimelineStages,
    createStudyTimelineInspector,
    getLevelStatus,
    getLevelTimestamp,
    getRecordTimestamp,
    getSurveyNickname,
    sortByTimestamp,
    statusLabel,
    surveyStageLabel
} from "./timeline-inspector.js";

export {
    buildTimelineStages,
    getLevelStatus,
    getLevelTimestamp,
    getRecordTimestamp,
    getSurveyNickname,
    sortByTimestamp,
    statusLabel,
    surveyStageLabel
};

export const DASHBOARD_LEVEL_SCENE = "Custom_Level";

export function isDashboardLevel(level) {
    const start = (level && level.start) || {};
    const end = (level && level.end) || {};
    return (start.sceneName || end.sceneName) === DASHBOARD_LEVEL_SCENE;
}

export function stableShortHash(input) {
    let hash = 2166136261;
    const text = String(input);

    for (let index = 0; index < text.length; index += 1) {
        hash ^= text.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
    }

    return (hash >>> 0).toString(16).padStart(8, "0");
}

export function getIdeaHash(ideaId, ideaText) {
    const source = clean(ideaId) || clean(ideaText);
    return source ? stableShortHash(source) : "unknown";
}

export function finalizeJourney(journey) {
    journey.levels.sort((left, right) => getLevelTimestamp(left).localeCompare(getLevelTimestamp(right)));
    journey.creativeIdeas.sort(sortByTimestamp);
    journey.expansions.sort(sortByTimestamp);
    journey.surveys.sort(sortByTimestamp);
    journey.haEvents.sort(sortByTimestamp);
    journey.journeyEvents.sort(sortByTimestamp);

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

export function buildIdeaJourneys(payload) {
    const safePayload = payload || {};
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
            if (!cleanIdeaId || !journey.ideaId || journey.ideaId === cleanIdeaId) {
                journey.sessions.add(cleanSessionId);

                if (!journey.ideaId && cleanIdeaId) {
                    journey.ideaId = cleanIdeaId;
                }

                return journey;
            }
        }

        return createJourney(cleanIdeaId, cleanSessionId);
    }

    safeArray(safePayload.creativeIdeas).forEach(record => {
        findJourney(record.ideaId, record.sessionId).creativeIdeas.push(record);
    });

    safeArray(safePayload.creativeExpansionChoices).forEach(record => {
        findJourney(record.ideaId, record.sessionId).expansions.push(record);
    });

    safeArray(safePayload.levels)
        .filter(isDashboardLevel)
        .forEach(level => {
            const start = level.start || {};
            const end = level.end || {};
            findJourney(
                start.creativeIdeaId || end.creativeIdeaId,
                start.sessionId || end.sessionId
            ).levels.push(level);
        });

    safeArray(safePayload.haPlanEvents).forEach(record => {
        findJourney(record.ideaId, record.sessionId).haEvents.push(record);
    });

    safeArray(safePayload.journeyEvents).forEach(record => {
        findJourney(record.ideaId, record.sessionId).journeyEvents.push(record);
    });

    safeArray(safePayload.surveyResponses).forEach(record => {
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

export function getSurveyDeletePayload(record) {
    if (clean(record && record.responseId)) {
        return { responseId: clean(record.responseId) };
    }

    const nickname = getSurveyNickname(record);
    return nickname ? { playerNickname: nickname } : null;
}

export function getStageDeleteConfig(stage) {
    if (!stage) return null;

    if (stage.type === "creative" && clean(stage.record && stage.record.ideaId)) {
        return {
            endpoint: "/delete-creative-idea",
            payload: { ideaId: clean(stage.record.ideaId) },
            label: "creative idea record"
        };
    }

    if (stage.type === "expansion" && clean(stage.record && stage.record.choiceId)) {
        return {
            endpoint: "/delete-expansion-choice",
            payload: { choiceId: clean(stage.record.choiceId) },
            label: "expansion choice"
        };
    }

    if (stage.type === "level" && clean(stage.level && stage.level.levelRunId)) {
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

    if (stage.type === "ha" && clean(stage.record && stage.record.haEventId)) {
        return {
            endpoint: "/delete-ha-plan-event",
            payload: { haEventId: clean(stage.record.haEventId) },
            label: "HA event"
        };
    }

    if (stage.type === "journey" && clean(stage.record && stage.record.journeyEventId)) {
        return {
            endpoint: "/delete-journey-event",
            payload: { journeyEventId: clean(stage.record.journeyEventId) },
            label: "journey event"
        };
    }

    return null;
}

function collectStudyElements(doc) {
    return {
        dataSource: doc.getElementById("dataSource"),
        notice: doc.getElementById("notice"),
        statusLine: doc.getElementById("statusLine"),
        refreshButton: doc.getElementById("refreshButton"),
        clearButton: doc.getElementById("clearButton"),
        rawLink: doc.getElementById("rawLink"),
        legacyLink: doc.getElementById("legacyLink"),
        docsLink: doc.getElementById("docsLink"),
        statIdeas: doc.getElementById("statIdeas"),
        statCompleted: doc.getElementById("statCompleted"),
        statCompletionRate: doc.getElementById("statCompletionRate"),
        statAvg: doc.getElementById("statAvg"),
        statAnomalies: doc.getElementById("statAnomalies"),
        statDataHealth: doc.getElementById("statDataHealth"),
        searchInput: doc.getElementById("searchInput"),
        statusFilter: doc.getElementById("statusFilter"),
        modeFilter: doc.getElementById("modeFilter"),
        ideaCount: doc.getElementById("ideaCount"),
        ideaList: doc.getElementById("ideaList"),
        emptyDetail: doc.getElementById("emptyDetail"),
        journeyDetail: doc.getElementById("journeyDetail"),
        selectedIdeaTitle: doc.getElementById("selectedIdeaTitle"),
        selectedIdeaStatus: doc.getElementById("selectedIdeaStatus"),
        selectedIdeaText: doc.getElementById("selectedIdeaText"),
        selectedIdeaMeta: doc.getElementById("selectedIdeaMeta"),
        deleteIdeaButton: doc.getElementById("deleteIdeaButton"),
        journeyTimeline: doc.getElementById("journeyTimeline"),
        levelCount: doc.getElementById("levelCount"),
        levelTabs: doc.getElementById("levelTabs"),
        mapGrid: doc.getElementById("mapGrid"),
        detailMetrics: doc.getElementById("detailMetrics"),
        inspectorTitle: doc.getElementById("inspectorTitle"),
        inspectorBody: doc.getElementById("inspectorBody"),
        deleteStageButton: doc.getElementById("deleteStageButton"),
        comparePanel: doc.getElementById("comparePanel"),
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

export function createStudyDashboard({
    window: win,
    document: doc,
    fetch: fetchImpl
} = {}) {
    const windowObj = win || (typeof globalThis.window !== "undefined"
        ? globalThis.window
        : { location: { search: "", protocol: "https:", origin: DEFAULT_API_BASE, hostname: "sokobanaidemo.top", pathname: "/frontend/train.html" } });
    const documentObj = doc || globalThis.document;
    const apiClient = createApiClient({
        fetchImpl,
        locationObj: windowObj.location
    });
    const dom = createDomHelpers({ document: documentObj });

    const state = {
        apiBase: apiClient.apiBase,
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

    const elements = collectStudyElements(documentObj);
    const { setStatus, showNotice } = createStatusController({ elements, window: windowObj });

    const deleteDialog = createDeleteDialogController({
        state,
        elements,
        window: windowObj,
        apiClient,
        setStatus,
        showNotice,
        onReload: loadData,
        errorStatusPrefix: "Could not delete record: ",
        defaultSuccessText: "Record deleted successfully.",
        selectOnError: true
    });

    const timelineInspector = createStudyTimelineInspector({
        document: documentObj,
        elements,
        state,
        getSelectedJourney,
        getStageDeleteConfig,
        onRenderJourney: renderJourney,
        onUpdateUrlSelection: updateUrlSelection
    });

    function wireLinks() {
        if (elements.rawLink) elements.rawLink.href = apiClient.apiUrl("/level-records");
        if (elements.legacyLink) elements.legacyLink.href = apiClient.apiUrl("/level-records-legacy");
        if (elements.docsLink) elements.docsLink.href = apiClient.apiUrl("/docs");
    }

    function wireEvents() {
        elements.refreshButton?.addEventListener?.("click", () => loadData(true));
        elements.clearButton?.addEventListener?.("click", clearAllRecords);
        elements.searchInput?.addEventListener?.("input", applyFilters);
        elements.statusFilter?.addEventListener?.("change", applyFilters);
        elements.modeFilter?.addEventListener?.("change", applyFilters);
        elements.deleteIdeaButton?.addEventListener?.("click", deleteSelectedIdea);
        elements.deleteStageButton?.addEventListener?.("click", deleteSelectedStage);
        elements.deleteDialogForm?.addEventListener?.("submit", deleteDialog.submitDeleteDialog);
        elements.deleteDialogCancel?.addEventListener?.("click", deleteDialog.closeDeleteDialog);
        elements.deleteDialog?.addEventListener?.("cancel", event => {
            event.preventDefault?.();
            if (!state.deleteDialogBusy) {
                deleteDialog.closeDeleteDialog();
            }
        });

        const summaryButtons = typeof documentObj.querySelectorAll === "function"
            ? Array.from(documentObj.querySelectorAll("[data-summary-filter]"))
            : [];
        summaryButtons.forEach(button => {
            button.addEventListener?.("click", () => {
                elements.statusFilter.value = (button.dataset && button.dataset.summaryFilter) || "all";
                applyFilters();
            });
        });
    }

    async function loadData(manual) {
        if (deleteDialog.isDashboardAccessLocked()) {
            deleteDialog.requestDashboardAccess();
            return;
        }
        setStatus(manual ? "Refreshing study journeys..." : "Loading study journeys...");

        try {
            state.payload = await apiClient.getJson("/level-records-data", { cache: "no-store" });
            state.journeys = buildIdeaJourneys(state.payload);
            restoreSelectionFromUrl();
            renderSummary();
            applyFilters();
            elements.dataSource.textContent = "API: " + state.apiBase;
            setStatus("Last loaded " + formatTimestamp(state.payload.generatedAt));
        } catch (error) {
            setStatus("Could not load study records: " + error.message);
            elements.ideaList.textContent = "";
            elements.ideaList.appendChild(dom.emptyNode("Failed to load study journeys."));
            showEmptyDetail();
        }
    }

    function applyFilters() {
        const query = clean(elements.searchInput && elements.searchInput.value).toLowerCase();
        const status = (elements.statusFilter && elements.statusFilter.value) || "all";
        const mode = (elements.modeFilter && elements.modeFilter.value) || "all";

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
        const payload = state.payload || {};
        const completed = state.journeys.filter(journey => journey.status === "completed").length;
        const anomalies = state.journeys.filter(journey => journey.status === "anomaly").length;
        const endedLevels = state.journeys
            .flatMap(journey => journey.levels)
            .filter(level => level.end && typeof level.end.durationSeconds === "number");
        const averageDuration = endedLevels.length
            ? endedLevels.reduce((sum, level) => sum + level.end.durationSeconds, 0) / endedLevels.length
            : null;
        const malformed = [
            payload.malformedCount,
            payload.surveyMalformedCount,
            payload.creativeIdeaMalformedCount,
            payload.creativeExpansionChoiceMalformedCount,
            payload.haPlanMalformedCount,
            payload.journeyEventMalformedCount
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
            elements.ideaList.appendChild(dom.emptyNode("No matching idea journeys."));
            return;
        }

        state.filteredJourneys.forEach(journey => {
            const button = documentObj.createElement("button");
            button.type = "button";
            button.className = "idea-item";

            if (journey.key === state.selectedJourneyKey) {
                button.classList?.add?.("selected");
            }

            const top = documentObj.createElement("div");
            top.className = "idea-item-top";
            top.append(
                dom.textNode("span", journey.ideaHash, "idea-hash"),
                dom.textNode("span", formatShortDate(journey.updatedAt))
            );
            const snippet = dom.textNode("p", journey.ideaText || "Idea text unavailable", "idea-snippet");
            const bottom = documentObj.createElement("div");
            bottom.className = "idea-item-bottom";
            const status = dom.textNode("span", statusLabel(journey.status), "mini-status " + journey.status);
            const counts = dom.textNode(
                "span",
                plural(journey.levels.length, "version") + " · " + plural(journey.surveys.length, "survey")
            );
            bottom.append(status, counts);
            button.append(top, snippet, bottom);
            button.addEventListener?.("click", () => selectJourney(journey.key));
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
            elements.selectedIdeaMeta.appendChild(dom.textNode("span", text));
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

        timelineInspector.renderTimeline(stages);
        timelineInspector.renderLevelVersions(journey, stages);
        timelineInspector.renderSelectedLevel(journey);
        timelineInspector.renderInspector(stages.find(stage => stage.key === state.selectedStageKey) || null);
        timelineInspector.renderComparison(journey);
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

        deleteDialog.openDeleteDialog({
            title: "Delete selected record?",
            description: "Enter the deletion password to remove only this stage.",
            scope: config.label + "\nOther records in this Idea journey will be kept.",
            confirmLabel: "Delete record",
            endpoint: config.endpoint,
            payload: config.payload,
            progressText: "Deleting " + config.label + "..."
        });
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
        deleteDialog.openDeleteDialog({
            title: "Delete entire Idea " + journey.ideaHash + "?",
            description: "This removes the complete study journey and cannot be undone.",
            scope,
            confirmLabel: "Delete entire Idea",
            endpoint: "/delete-idea-records",
            payload: { ideaId: journey.ideaId },
            progressText: "Deleting the entire idea journey...",
            afterSuccess: resetSelection
        });
    }

    function clearAllRecords() {
        deleteDialog.openDeleteDialog({
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

    function showEmptyDetail() {
        elements.emptyDetail.hidden = false;
        elements.journeyDetail.hidden = true;
    }

    function getSelectedJourney() {
        return state.journeys.find(journey => journey.key === state.selectedJourneyKey) || null;
    }

    function restoreSelectionFromUrl() {
        const search = (windowObj.location && windowObj.location.search) || "";
        const params = new URLSearchParams(search);
        const idea = clean(params.get("idea"));
        const run = clean(params.get("run"));

        if (idea && !state.selectedJourneyKey) {
            const journey = state.journeys.find(item =>
                item.ideaHash === idea || item.ideaId === idea
            );
            if (journey) state.selectedJourneyKey = journey.key;
        }

        if (run && !state.selectedRunId) {
            state.selectedRunId = run;
            state.selectedStageKey = "level:" + run;
        }
    }

    function updateUrlSelection(journey) {
        if (
            !windowObj.history
            || typeof windowObj.history.replaceState !== "function"
            || (windowObj.location && windowObj.location.protocol === "file:")
        ) {
            return;
        }

        const loc = windowObj.location || {};
        const fallbackHref = ((loc.origin && loc.origin !== "null") ? loc.origin : state.apiBase)
            + (loc.pathname || "/frontend/train.html")
            + (loc.search || "");
        const url = new URL(loc.href || fallbackHref);
        url.searchParams.set("idea", journey.ideaHash);

        if (state.selectedRunId) {
            url.searchParams.set("run", state.selectedRunId);
        } else {
            url.searchParams.delete("run");
        }

        windowObj.history.replaceState(null, "", url);
    }

    async function init() {
        wireLinks();
        wireEvents();

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
        selectJourney,
        renderSummary,
        renderIdeaList,
        renderJourney,
        renderTimeline: timelineInspector.renderTimeline,
        renderLevelVersions: timelineInspector.renderLevelVersions,
        toggleCompareRun: timelineInspector.toggleCompareRun,
        renderSelectedLevel: timelineInspector.renderSelectedLevel,
        renderInspector: timelineInspector.renderInspector,
        renderComparison: timelineInspector.renderComparison,
        deleteSelectedStage,
        deleteSelectedIdea,
        clearAllRecords,
        openDeleteDialog: deleteDialog.openDeleteDialog,
        closeDeleteDialog: deleteDialog.closeDeleteDialog,
        submitDeleteDialog: deleteDialog.submitDeleteDialog
    };
}

if (typeof window !== "undefined" && typeof document !== "undefined" && !window.__SOKOBAN_DISABLE_AUTO_INIT__) {
    void createStudyDashboard({ window, document, fetch: window.fetch.bind(window) }).init();
}
