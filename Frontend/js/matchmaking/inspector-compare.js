function formatChallengeOutcome(outcome) {
    if (outcome === "timed_out") return "Timed out";
    if (outcome === "completed") return "Completed";
    return outcome ? titleCase(outcome) : "-";
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
        const tabStatus = player.challenge.runResult
            ? (player.challenge.runResult.outcome === "timed_out" ? "timed out" : "played")
            : "pending";
        button.append(
            textNode("span", "P" + player.playerNumber + " challenge"),
            textNode("span", tabStatus)
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
    const hasResult = Boolean(challenge.runResult);
    const outcome = hasResult ? (result.outcome || "completed") : "";
    const isCompleted = hasResult && outcome !== "timed_out";
    const overhead = isCompleted && typeof result.moveCount === "number" && typeof result.minimumMoves === "number"
        ? result.moveCount - result.minimumMoves
        : null;
    [
        ["Created by", "Player " + challenge.createdByPlayerNumber],
        ["Played by", "Player " + challenge.playedByPlayerNumber],
        ["Outcome", hasResult ? formatChallengeOutcome(outcome) : "-"],
        ["Play time", formatSeconds(result.durationSeconds)],
        ["Restarts", value(result.restartCount)],
        ["Moves", value(result.moveCount)],
        ["Minimum moves", value(result.minimumMoves)],
        ["Move overhead", overhead === null ? "-" : (overhead >= 0 ? "+" : "") + overhead]
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
    appendRecordGrid(rows);
    if (record.eventType === "draft") appendDraftDetails(record);
    if (["first_stage", "stage", "final"].includes(record.eventType)) {
        appendFlowStageDetails(match, record);
    }
    if (record.eventType === "node") appendDashboardNodeDetails(record);
    if (record.eventType === "turn") appendFlowTurnDetails(record);
    if (record.eventType === "message") appendFlowMessageDetails(record);
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
    const hasResult = Boolean(challenge.runResult);
    const table = document.createElement("table");
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
        const row = document.createElement("tr");
        const valueCell = document.createElement("td");
        valueCell.appendChild(copyable ? createIdControl(label, metricValue) : textNode("span", metricValue));
        row.append(textNode("td", label), valueCell);
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
    elements.selectedMatchCopy.setAttribute("aria-label", "Copy full Match ID");
}

function createIdControl(label, rawValue) {
    const fullValue = clean(rawValue);
    if (!fullValue) return textNode("strong", "-");
    const control = document.createElement("span");
    control.className = "id-control";
    const display = textNode("strong", compactId(fullValue), "id-value");
    display.title = "Full " + label + ": " + fullValue;
    const copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.className = "copy-id-button";
    copyButton.textContent = "Copy";
    copyButton.title = "Copy full " + label;
    copyButton.setAttribute("aria-label", "Copy full " + label);
    copyButton.dataset.fullValue = fullValue;
    copyButton.addEventListener("click", () => copyFullId(copyButton));
    control.append(display, copyButton);
    return control;
}

async function copyFullId(button) {
    const fullValue = clean(button && button.dataset.fullValue);
    if (!fullValue) return;
    let copied = false;
    try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
            await navigator.clipboard.writeText(fullValue);
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
    window.setTimeout(() => {
        button.textContent = originalLabel;
        button.disabled = false;
    }, 1200);
}

function copyWithFallback(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } catch (error) {
        copied = false;
    }
    textarea.remove();
    return copied;
}

function formatMode(input) {
    const labels = {
        description_generation: "Description Generation",
    };
    return labels[input] || titleCase(input);
}
