import {
    clean,
    createDomHelpers,
    firstValue,
    formatPercent,
    formatRatio,
    formatSeconds,
    formatShortDate,
    formatTimestamp,
    numeric,
    plural,
    safeArray,
    titleCase,
    value
} from "../shared/api-utils.js";

export function getRecordTimestamp(record) {
    return clean(record && (record.serverReceivedAt || record.timestamp));
}

export function getLevelTimestamp(level) {
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

export function sortByTimestamp(left, right) {
    return getRecordTimestamp(left).localeCompare(getRecordTimestamp(right));
}

export function getSurveyNickname(record) {
    return firstValue(record && record.playerName, record && record.playerNickname, record && record.nickname);
}

export function surveyStageLabel(record, index) {
    const text = (
        clean(record && record.surveyId)
        + " "
        + clean(record && record.surveyTitle)
        + " "
        + clean(record && record.sceneName)
    ).toLowerCase();

    if (text.includes("before") || text.includes("pre")) return "Pre-survey";
    if (text.includes("after") || text.includes("post")) return "Post-survey";
    return "Survey " + (index + 1);
}

export function getLevelStatus(level) {
    const end = (level && level.end) || null;

    if (!end) return { label: "Missing end", short: "missing" };
    if (end.completed) return { label: "Completed", short: "done" };
    return { label: titleCase(end.endReason || "Stopped"), short: "stopped" };
}

export function statusLabel(status) {
    if (status === "completed") return "Completed";
    if (status === "anomaly") return "Needs attention";
    return "In progress";
}

export function buildTimelineStages(journey) {
    const stages = [];

    safeArray(journey && journey.creativeIdeas).forEach((record, index) => stages.push({
        key: "creative:" + (clean(record.ideaId) || index),
        type: "creative",
        label: index === 0 ? "Original idea" : "Idea update",
        timestamp: getRecordTimestamp(record),
        record
    }));

    safeArray(journey && journey.expansions).forEach((record, index) => stages.push({
        key: "expansion:" + (clean(record.choiceId) || index),
        type: "expansion",
        label: "Expansion",
        timestamp: getRecordTimestamp(record),
        record
    }));

    safeArray(journey && journey.levels).forEach((level, index) => stages.push({
        key: "level:" + level.levelRunId,
        type: "level",
        label: "Level V" + (index + 1),
        timestamp: getLevelTimestamp(level),
        warning: !level.end,
        level,
        record: level
    }));

    safeArray(journey && journey.journeyEvents).forEach((record, index) => {
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

    safeArray(journey && journey.haEvents).forEach((record, index) => stages.push({
        key: "ha:" + (clean(record.haEventId) || index),
        type: "ha",
        label: record.eventType === "ha-plan-choice" ? "HA choice" : "HA plans",
        timestamp: getRecordTimestamp(record),
        warning: Boolean(clean(record.error)),
        record
    }));

    safeArray(journey && journey.surveys).forEach((record, index) => stages.push({
        key: "survey:" + (clean(record.responseId) || index),
        type: "survey",
        label: surveyStageLabel(record, index),
        timestamp: getRecordTimestamp(record),
        record
    }));

    return stages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

export function createStudyTimelineInspector({
    document: doc,
    elements,
    state,
    getSelectedJourney,
    getStageDeleteConfig,
    onRenderJourney,
    onUpdateUrlSelection
} = {}) {
    const dom = createDomHelpers({ document: doc });

    function renderTimeline(stages) {
        elements.journeyTimeline.textContent = "";

        if (stages.length === 0) {
            elements.journeyTimeline.appendChild(dom.emptyNode("No journey stages recorded."));
            return;
        }

        stages.forEach((stage, index) => {
            const button = doc.createElement("button");
            button.type = "button";
            button.className = "timeline-node";

            if (stage.key === state.selectedStageKey) {
                button.classList?.add?.("selected");
            }

            if (stage.warning) {
                button.classList?.add?.("warning");
            }

            button.append(
                dom.textNode("span", stage.warning ? "!" : String(index + 1), "timeline-dot"),
                dom.textNode("span", stage.label, "timeline-label"),
                dom.textNode("span", formatShortDate(stage.timestamp), "timeline-time")
            );
            button.addEventListener?.("click", () => {
                state.selectedStageKey = stage.key;

                if (stage.type === "level") {
                    state.selectedRunId = stage.level.levelRunId;
                }

                const journey = getSelectedJourney?.();
                if (journey) onRenderJourney?.(journey);
            });
            elements.journeyTimeline.appendChild(button);
        });
    }

    function renderLevelVersions(journey, stages) {
        elements.levelTabs.textContent = "";
        elements.levelCount.textContent = plural(journey.levels.length, "version");

        if (journey.levels.length === 0) {
            elements.levelTabs.appendChild(dom.emptyNode("No level versions recorded."));
            return;
        }

        journey.levels.forEach((level, index) => {
            const wrapper = doc.createElement("button");
            wrapper.type = "button";
            wrapper.className = "level-tab";

            if (level.levelRunId === state.selectedRunId) {
                wrapper.classList?.add?.("selected");
            }

            const checkbox = doc.createElement("input");
            checkbox.type = "checkbox";
            checkbox.className = "compare-check";
            checkbox.title = "Include this version in comparison";
            checkbox.checked = state.compareRunIds.includes(level.levelRunId);
            checkbox.addEventListener?.("click", event => {
                event.stopPropagation?.();
                toggleCompareRun(level.levelRunId);
            });
            wrapper.append(
                checkbox,
                dom.textNode("span", "V" + (index + 1)),
                dom.textNode("span", getLevelStatus(level).short)
            );
            wrapper.addEventListener?.("click", () => {
                state.selectedRunId = level.levelRunId;
                const levelStage = stages.find(stage =>
                    stage.type === "level" && stage.level.levelRunId === level.levelRunId
                );
                state.selectedStageKey = levelStage ? levelStage.key : state.selectedStageKey;
                onRenderJourney?.(journey);
                onUpdateUrlSelection?.(journey);
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

        const journey = getSelectedJourney?.();
        if (journey) onRenderJourney?.(journey);
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
        dom.renderMap(elements.mapGrid, safeArray(start.rows), false);

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
            const item = doc.createElement("div");
            item.className = "metric";
            item.append(dom.textNode("span", label), dom.textNode("strong", metricValue));
            elements.detailMetrics.appendChild(item);
        });
    }

    function renderInspector(stage) {
        elements.inspectorBody.textContent = "";
        elements.deleteStageButton.hidden = true;
        elements.deleteStageButton.dataset.stageKey = "";

        if (!stage) {
            elements.inspectorTitle.textContent = "Journey details";
            elements.inspectorBody.appendChild(dom.emptyNode("Select a journey stage to inspect it."));
            return;
        }

        elements.inspectorTitle.textContent = stage.label;
        const deleteConfig = getStageDeleteConfig?.(stage);

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
        const section = dom.createSection("Answers");

        if (answers.length === 0) {
            section.appendChild(dom.textNode("p", "No answers recorded."));
        } else {
            answers.forEach(answer => {
                const card = doc.createElement("div");
                card.className = "answer-card";
                card.append(
                    dom.textNode("span", answer.questionText || answer.questionId || "Question " + value(answer.questionIndex)),
                    dom.textNode("strong", answer.optionText || answer.optionLabel || answer.optionId || "-")
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
            const section = dom.createSection("Presented options");
            options.forEach(option => {
                const card = doc.createElement("div");
                card.className = "option-card";

                if (clean(option.id) === clean(record.selectedOptionId)) {
                    card.classList?.add?.("selected");
                }

                card.append(
                    dom.textNode("strong", [option.id, option.title].filter(Boolean).join(" · ")),
                    dom.textNode("span", option.description || "-")
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
            elements.compareContent.appendChild(dom.textNode(
                "div",
                "Select two version checkboxes to compare maps and player metrics.",
                "compare-placeholder"
            ));
            return;
        }

        const grid = doc.createElement("div");
        grid.className = "compare-grid";
        levels.forEach(level => grid.appendChild(renderCompareVersion(level, journey.levels.indexOf(level) + 1)));
        elements.compareContent.appendChild(grid);
    }

    function renderCompareVersion(level, versionNumber) {
        const start = level.start || {};
        const end = level.end || {};
        const card = doc.createElement("article");
        card.className = "compare-version";
        card.appendChild(dom.textNode("h3", "Level V" + versionNumber + " · " + getLevelStatus(level).label));
        const frame = doc.createElement("div");
        frame.className = "mini-map-frame";
        const map = doc.createElement("div");
        map.className = "mini-map";
        dom.renderMap(map, safeArray(start.rows), true);
        frame.appendChild(map);
        card.appendChild(frame);

        const table = doc.createElement("table");
        table.className = "delta-table";
        [
            ["Play time", formatSeconds(end.durationSeconds)],
            ["Moves", value(end.moveCount)],
            ["Pushes", value(end.pushCount)],
            ["Restarts", value(end.restartCount)],
            ["Solver steps", value(start.solutionSteps)],
            ["Solver pushes", value(start.solverPushes)]
        ].forEach(([label, metricValue]) => {
            const row = doc.createElement("tr");
            row.append(dom.textNode("td", label), dom.textNode("td", metricValue));
            table.appendChild(row);
        });
        card.appendChild(table);
        return card;
    }

    function appendTextSection(title, text) {
        const section = dom.createSection(title);
        section.appendChild(dom.textNode("p", value(text)));
        elements.inspectorBody.appendChild(section);
    }

    function appendRecordGrid(rows) {
        const section = dom.createSection("Record details");
        const grid = doc.createElement("div");
        grid.className = "record-grid";
        rows.forEach(([label, rowValue]) => {
            const row = doc.createElement("div");
            row.className = "record-row";
            row.append(dom.textNode("span", label), dom.textNode("strong", value(rowValue)));
            grid.appendChild(row);
        });
        section.appendChild(grid);
        elements.inspectorBody.appendChild(section);
    }

    return {
        renderTimeline,
        renderLevelVersions,
        toggleCompareRun,
        renderSelectedLevel,
        renderInspector,
        renderCreativeInspector,
        renderExpansionInspector,
        renderLevelInspector,
        renderSurveyInspector,
        renderHAInspector,
        renderJourneyEventInspector,
        renderComparison,
        renderCompareVersion,
        appendTextSection,
        appendRecordGrid
    };
}
