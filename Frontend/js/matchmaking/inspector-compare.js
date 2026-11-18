import {
    clean,
    compactId,
    createDomHelpers,
    formatClockDuration,
    formatSeconds,
    formatTimestamp,
    safeArray,
    titleCase,
    value
} from "../shared/api-utils.js";
import { formatMode, getTimestamp } from "./nodes-timeline.js";

export { formatMode };

export function formatChallengeOutcome(outcome) {
    if (outcome === "timed_out") return "Timed out";
    if (outcome === "completed") return "Completed";
    return outcome ? titleCase(outcome) : "-";
}

export function formatChallengeMetrics(challenge) {
    const result = (challenge && challenge.runResult) || {};
    const hasResult = Boolean(challenge && challenge.runResult);
    const outcome = hasResult ? (result.outcome || "completed") : "";
    const isCompleted = hasResult && outcome !== "timed_out";
    const overhead = isCompleted && typeof result.moveCount === "number" && typeof result.minimumMoves === "number"
        ? result.moveCount - result.minimumMoves
        : null;

    return [
        ["Created by", "Player " + (challenge && challenge.createdByPlayerNumber)],
        ["Played by", "Player " + (challenge && challenge.playedByPlayerNumber)],
        ["Outcome", hasResult ? formatChallengeOutcome(outcome) : "-"],
        ["Play time", formatSeconds(result.durationSeconds)],
        ["Restarts", value(result.restartCount)],
        ["Moves", value(result.moveCount)],
        ["Minimum moves", value(result.minimumMoves)],
        ["Move overhead", overhead === null ? "-" : (overhead >= 0 ? "+" : "") + overhead]
    ];
}

export function buildInspectorRecordRows(match, record) {
    const players = safeArray(match && match.players);
    const playerOne = players.find(player => player && player.playerNumber === 1);
    const playerTwo = players.find(player => player && player.playerNumber === 2);
    const rows = [
        ["Event", titleCase(record.eventType)],
        ["Player", record.playerNumber ? "Player " + record.playerNumber : "Room"],
        ["Recorded", formatTimestamp(getTimestamp(record))],
        ["Player 1 Study session ID", playerOne && playerOne.studySessionId, true],
        ["Player 2 Study session ID", playerTwo && playerTwo.studySessionId, true]
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
        rows.push(["Outcome", formatChallengeOutcome(record.outcome || "completed")]);
        rows.push(["Play time", formatSeconds(record.durationSeconds)]);
        rows.push(["Restarts", value(record.restartCount)]);
        rows.push(["Moves", value(record.moveCount)]);
        rows.push(["Minimum moves", value(record.minimumMoves)]);
    }
    if (record.eventType === "final") {
        rows.push(["Co-creation time", formatClockDuration(record.coCreationDurationSeconds)]);
        rows.push(["Opponent restarts", value(record.opponentRestartCount)]);
    }
    return rows;
}

export function createMatchmakingInspector({
    document: doc,
    window: win,
    elements,
    state,
    timelineRenderer,
    onSelectChallenge
} = {}) {
    const dom = createDomHelpers({ document: doc });

    function renderChallengeTabs(match) {
        elements.challengeTabs.textContent = "";
        const players = safeArray(match.players).filter(player => player.challenge);
        elements.challengeCount.textContent = players.length + " challenges";
        if (!players.length) {
            elements.challengeTabs.appendChild(dom.emptyNode("No challenge maps submitted."));
            return;
        }
        players.forEach(player => {
            const button = doc.createElement("button");
            button.type = "button";
            button.className = "level-tab";
            if (player.playerNumber === state.selectedChallengePlayer) button.classList?.add?.("selected");
            const tabStatus = player.challenge.runResult
                ? (player.challenge.runResult.outcome === "timed_out" ? "timed out" : "played")
                : "pending";
            button.append(
                dom.textNode("span", "P" + player.playerNumber + " challenge"),
                dom.textNode("span", tabStatus)
            );
            button.addEventListener?.("click", () => {
                state.selectedChallengePlayer = player.playerNumber;
                onSelectChallenge?.(match, player.playerNumber);
            });
            elements.challengeTabs.appendChild(button);
        });
    }

    function renderSelectedChallenge(match) {
        const player = safeArray(match && match.players).find(item => item.playerNumber === state.selectedChallengePlayer);
        const challenge = player && player.challenge;
        elements.mapGrid.textContent = "";
        elements.detailMetrics.textContent = "";
        if (!challenge) {
            elements.mapGrid.textContent = "No challenge selected.";
            elements.mapGrid.style.gridTemplateColumns = "";
            return;
        }
        dom.renderMap(elements.mapGrid, safeArray(challenge.rows), false);
        formatChallengeMetrics(challenge).forEach(([label, metricValue]) => {
            const item = doc.createElement("div");
            item.className = "metric";
            item.append(dom.textNode("span", label), dom.textNode("strong", metricValue));
            elements.detailMetrics.appendChild(item);
        });
    }

    function renderInspector(match, stage) {
        elements.inspectorBody.textContent = "";
        if (!stage) {
            elements.inspectorTitle.textContent = "Match details";
            elements.inspectorBody.appendChild(dom.emptyNode("Select a timeline stage."));
            return;
        }
        elements.inspectorTitle.textContent = stage.label;
        const record = stage.record;
        const rows = buildInspectorRecordRows(match, record);
        timelineRenderer.appendRecordGrid(rows);
        if (record.eventType === "draft") timelineRenderer.appendDraftDetails(record);
        if (["first_stage", "stage", "final"].includes(record.eventType)) {
            timelineRenderer.appendFlowStageDetails(match, record);
        }
        if (record.eventType === "node") timelineRenderer.appendDashboardNodeDetails(record);
        if (record.eventType === "turn") timelineRenderer.appendFlowTurnDetails(record);
        if (record.eventType === "message") timelineRenderer.appendFlowMessageDetails(record);
    }

    function renderComparison(match) {
        elements.compareContent.textContent = "";
        const players = safeArray(match.players).filter(player => player.challenge);
        if (players.length !== 2) {
            elements.compareContent.appendChild(dom.textNode(
                "div",
                "Both players must submit challenges before comparison is available.",
                "compare-placeholder"
            ));
            return;
        }
        const grid = doc.createElement("div");
        grid.className = "compare-grid";
        players.forEach(player => grid.appendChild(renderCompareChallenge(player)));
        elements.compareContent.appendChild(grid);
    }

    function renderCompareChallenge(player) {
        const challenge = player.challenge;
        const result = challenge.runResult || {};
        const card = doc.createElement("article");
        card.className = "compare-version";
        card.appendChild(dom.textNode("h3", "Player " + player.playerNumber + " challenge"));
        const frame = doc.createElement("div");
        frame.className = "mini-map-frame";
        const map = doc.createElement("div");
        map.className = "mini-map";
        dom.renderMap(map, safeArray(challenge.rows), true);
        frame.appendChild(map);
        card.appendChild(frame);
        const hasResult = Boolean(challenge.runResult);
        const table = doc.createElement("table");
        table.className = "delta-table";
        [
            ["Study session ID", player.studySessionId, true],
            ["Message", player.designerIntention || "-"],
            ["Played by", "Player " + challenge.playedByPlayerNumber],
            ["Outcome", hasResult ? formatChallengeOutcome(result.outcome || "completed") : "-"],
            ["Play time", formatSeconds(result.durationSeconds)],
            ["Restarts", value(result.restartCount)],
            ["Moves", value(result.moveCount)],
            ["Minimum", value(result.minimumMoves)]
        ].forEach(([label, metricValue, copyable]) => {
            const row = doc.createElement("tr");
            const valueCell = doc.createElement("td");
            valueCell.appendChild(copyable ? createIdControl(label, metricValue) : dom.textNode("span", metricValue));
            row.append(dom.textNode("td", label), valueCell);
            table.appendChild(row);
        });
        card.appendChild(table);
        return card;
    }

    function setMatchIdDisplay(matchId) {
        const fullValue = clean(matchId);
        elements.selectedMatchText.textContent = "Match ID " + compactId(fullValue);
        elements.selectedMatchText.title = fullValue ? "Full Match ID: " + fullValue : "";
        elements.selectedMatchCopy.textContent = elements.selectedMatchCopy.dataset.originalLabel || "Copy";
        elements.selectedMatchCopy.disabled = false;
        elements.selectedMatchCopy.hidden = !fullValue;
        elements.selectedMatchCopy.dataset.fullValue = fullValue;
        elements.selectedMatchCopy.title = fullValue ? "Copy full Match ID" : "";
        elements.selectedMatchCopy.setAttribute?.("aria-label", "Copy full Match ID");
    }

    function createIdControl(label, rawValue) {
        const fullValue = clean(rawValue);
        if (!fullValue) return dom.textNode("strong", "-");
        const control = doc.createElement("span");
        control.className = "id-control";
        const display = dom.textNode("strong", compactId(fullValue), "id-value");
        display.title = "Full " + label + ": " + fullValue;
        const copyButton = doc.createElement("button");
        copyButton.type = "button";
        copyButton.className = "copy-id-button";
        copyButton.textContent = "Copy";
        copyButton.title = "Copy full " + label;
        copyButton.setAttribute?.("aria-label", "Copy full " + label);
        copyButton.dataset.fullValue = fullValue;
        copyButton.addEventListener?.("click", () => copyFullId(copyButton));
        control.append(display, copyButton);
        return control;
    }

    async function copyFullId(button) {
        const fullValue = clean(button && button.dataset && button.dataset.fullValue);
        if (!fullValue) return;
        let copied = false;
        const nav = (win && win.navigator) || globalThis.navigator;
        try {
            if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
                await nav.clipboard.writeText(fullValue);
                copied = true;
            }
        } catch (error) {
            copied = false;
        }
        if (!copied) copied = copyWithFallback(fullValue);
        if (!copied) return;
        const originalLabel = button.dataset.originalLabel || button.textContent;
        button.dataset.originalLabel = originalLabel;
        button.textContent = "Copied";
        button.disabled = true;
        const restore = () => {
            button.textContent = originalLabel;
            button.disabled = false;
        };
        if (win && typeof win.setTimeout === "function") {
            win.setTimeout(restore, 1200);
        } else {
            const timer = setTimeout(restore, 1200);
            timer?.unref?.();
        }
    }

    function copyWithFallback(text) {
        if (!doc || !doc.body || typeof doc.execCommand !== "function") {
            return false;
        }
        const textarea = doc.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute?.("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        doc.body.appendChild(textarea);
        textarea.select?.();
        let copied = false;
        try {
            copied = doc.execCommand("copy");
        } catch (error) {
            copied = false;
        }
        textarea.remove?.();
        return copied;
    }

    return {
        renderChallengeTabs,
        renderSelectedChallenge,
        renderInspector,
        renderComparison,
        renderCompareChallenge,
        setMatchIdDisplay,
        createIdControl,
        copyFullId
    };
}
