function buildTimelineStages(match) {
    const flowStages = safeArray(match.players).flatMap(player =>
        safeArray(player.coCreationFlow)
            .filter(event => !["opening", "turn"].includes(event.eventType))
            .map((event, index) => ({
            key: "flow:" + player.playerNumber + ":" + (clean(event.eventId) || index),
            type: "flow",
            label: flowStageLabel(event),
            timestamp: getTimestamp(event),
            warning: false,
            playerNumber: player.playerNumber,
            record: event
            }))
    );
    if (flowStages.length) return flowStages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
    const stages = safeArray(match.events).map((event, index) => ({
        key: "event:" + (clean(event.eventId) || index),
        type: "event",
        label: eventStageLabel(event),
        timestamp: getTimestamp(event),
        warning: ["player_left", "room_expired"].includes(event.eventType),
        playerNumber: numeric(event.playerNumber),
        record: event
    }));
    return stages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

function flowStageLabel(event) {
    const labels = {
        draft: "Draft",
        first_stage: "First Stage",
        stage: "Stage " + value(event.stageNumber) + " · " + (event.source === "ai" ? "AI" : "Manual"),
        turn: "Q&A",
        final: "Final Stage",
        message: "Message"
    };
    if (event.eventType === "stage") {
        return "Stage " + value(event.stageNumber) + " · " + (event.source === "restored" ? "Restored" : event.source === "ai" ? "AI" : "Manual");
    }
    if (event.eventType === "node") return dashboardNodeLabel(event.nodeType);
    return labels[event.eventType] || titleCase(event.eventType || "Flow event");
}

function dashboardNodeLabel(nodeType) {
    const labels = {
        discussion: "General Discussion",
        intent: "Intent",
        intent_conflict: "Intent Conflict",
        proposal: "Proposal",
        player_challenge: "Player Challenge",
        manual_edit_review: "Manual Edit Review",
        llm_challenge: "LLM Challenge"
    };
    return labels[nodeType] || "Co-creation Node";
}

function dashboardNodeStatus(status) {
    const labels = {
        in_progress: "In progress",
        awaiting_player_choice: "Awaiting player choice",
        clarifying_1: "Proposal clarification 1/3",
        clarifying_2: "Proposal clarification 2/3",
        clarifying_3: "Proposal clarification 3/3",
        proposal_ready: "Proposal ready",
        pending: "Pending",
        accepted: "Accepted",
        rejected: "Rejected",
        replaced: "Replaced",
        resolved: "Resolved",
        confirmed: "Confirmed",
        cancelled: "Cancelled",
        reviewed: "Reviewed",
        acknowledged: "Responded · proposal block released",
        manual_reedit: "Ended through a new manual edit",
        original_selected: "Original proposal selected",
        replacement_selected: "New plan from the player's reason selected",
        awaiting_reason: "Awaiting challenge reason",
        review_pending: "Reason review pending retry",
        revised: "Revised intention"
    };
    const normalized = clean(status);
    return labels[normalized] || titleCase(normalized.replace(/_/g, " ") || "In progress");
}

function eventStageLabel(event) {
    const player = numeric(event.playerNumber) ? "P" + event.playerNumber + " " : "";
    const labels = {
        room_created: "Room created",
        player_joined: player + "joined",
        ready_changed: player + (event.ready ? "ready" : "not ready"),
        challenge_submitted: player + "challenge submitted",
        designer_intention_synchronized: player + "message confirmed",
        result_submitted: player + "result submitted",
        player_left: player + "left",
        room_expired: "Room expired"
    };
    return labels[event.eventType] || titleCase(event.eventType || "Match event");
}

function renderTimeline(match, stages) {
    elements.matchTimeline.textContent = "";
    elements.matchTimeline.classList.add("player-flow-list");
    if (!stages.length) {
        elements.matchTimeline.appendChild(emptyNode("No match stages recorded."));
        return;
    }
    const groups = new Map([[1, []], [2, []]]);
    stages.forEach(stage => groups.get(stage.playerNumber)?.push(stage));
    groups.forEach((playerStages, playerNumber) => {
        const lane = document.createElement("section");
        lane.className = "player-flow";
        lane.appendChild(textNode("h3", "Player " + playerNumber, "player-flow-title"));
        const nodes = document.createElement("div");
        nodes.className = "timeline player-flow-timeline";
        if (!playerStages.length) nodes.appendChild(emptyNode("No co-creation records."));
        playerStages.forEach((stage, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "timeline-node";
        if (stage.key === state.selectedStageKey) button.classList.add("selected");
        if (stage.warning) button.classList.add("warning");
        button.append(
            textNode("span", stage.warning ? "!" : String(index + 1), "timeline-dot"),
            textNode("span", stage.label, "timeline-label"),
            textNode("span", formatShortDate(stage.timestamp), "timeline-time")
        );
        button.addEventListener("click", () => {
            state.selectedStageKey = stage.key;
            if (stage.record.eventType === "challenge_submitted") {
                state.selectedChallengePlayer = stage.playerNumber;
            }
            renderMatch(match);
        });
            nodes.appendChild(button);
        });
        lane.appendChild(nodes);
        elements.matchTimeline.appendChild(lane);
    });
}

function appendDashboardNodeDetails(record) {
    const section = createSection(dashboardNodeLabel(record.nodeType));
    const rows = [
        ["Status", dashboardNodeStatus(record.nodeStatus)],
        ["Started", formatTimestamp(record.serverReceivedAt)],
        ["Last updated", formatTimestamp(record.nodeUpdatedAt || record.serverReceivedAt)]
    ];
    if (record.stageNumber !== undefined && record.stageNumber !== null) {
        rows.push(["Stage", "Stage " + value(record.stageNumber)]);
    }
    if (record.nodeParentId) rows.push(["Related node", record.nodeParentId]);
    const grid = document.createElement("div");
    grid.className = "record-grid";
    rows.forEach(([label, rowValue]) => {
        const row = document.createElement("div");
        row.className = "record-row";
        row.append(textNode("span", label), textNode("strong", value(rowValue)));
        grid.appendChild(row);
    });
    section.appendChild(grid);

    const labels = {
        player_message: "Player",
        llm_message: "LLM",
        card: "Card",
        player_action: "Player action",
        map_diff: "Verified map change",
        status: "Status"
    };
    const entries = safeArray(record.nodeEntries)
        .slice()
        .sort((left, right) => clean(left.occurredAt).localeCompare(clean(right.occurredAt)));
    if (!entries.length) section.appendChild(emptyNode("No public node entries recorded."));
    entries.forEach(entry => {
        const article = document.createElement("article");
        article.className = "node-entry";
        article.append(
            textNode("h4", entry.label || labels[entry.kind] || "Entry"),
            textNode("p", entry.text || "-"),
            textNode("small", formatTimestamp(entry.occurredAt))
        );
        section.appendChild(article);
    });
    elements.inspectorBody.appendChild(section);
}

function appendDraftDetails(record) {
    const section = createSection("Draft research record");
    const grid = document.createElement("div");
    grid.className = "record-grid";
    [
        ["Metadata complete", record.draftMetadataComplete ? "Yes" : "No"],
        ["Q1 answer", record.q1Answer],
        ["Q2 answer", record.q2Answer],
        ["Q3 answer", record.q3Answer],
        ["Q4 answer", record.q4Answer],
        ["AI recommended difficulty", record.aiRecommendedDifficulty],
        ["AI recommended layout", record.aiRecommendedLayout],
        ["AI recommendation source", record.aiRecommendationSource],
        ["User final difficulty", record.finalDifficulty],
        ["User final layout", record.finalLayout]
    ].forEach(([label, rowValue]) => {
        const row = document.createElement("div");
        row.className = "record-row";
        row.append(textNode("span", label), textNode("strong", value(rowValue)));
        grid.appendChild(row);
    });
    section.appendChild(grid);
    section.append(
        textNode("h4", "AI Reflection"),
        textNode("p", value(record.aiReflection)),
        textNode("h4", "AI difficulty rationale"),
        textNode("p", value(record.aiDifficultyRationale)),
        textNode("h4", "AI layout rationale"),
        textNode("p", value(record.aiLayoutRationale))
    );
    elements.inspectorBody.appendChild(section);
}

function findOpeningForStage(match, record) {
    const player = safeArray(match.players).find(item => item.playerNumber === record.playerNumber);
    return safeArray(player && player.coCreationFlow).find(event =>
        event.eventType === "opening" && event.versionId === record.versionId
    ) || null;
}

function openingIsConsumed(match, opening) {
    const player = safeArray(match.players).find(item => item.playerNumber === opening.playerNumber);
    return safeArray(player && player.coCreationFlow).some(event =>
        event.eventType === "turn" && event.openingAssistantTurnId === opening.assistantTurnId
    );
}

function appendCards(section, cards, title = "Cards") {
    const safeCards = safeArray(cards);
    if (!safeCards.length) return;
    section.appendChild(textNode("h4", title));
    safeCards.forEach(card => section.appendChild(
        textNode("p", "[" + value(card.type) + "] " + value(card.text))
    ));
}

function appendOpeningDetails(section, opening) {
    section.append(
        textNode("h4", "AI opening evaluation"),
        textNode("p", opening.assistantText || "-")
    );
    appendCards(section, opening.cards, "Opening cards");
}

function appendFlowStageDetails(match, record) {
    const titles = {
        first_stage: "First Stage map",
        final: "Final map",
        stage: "Stage change"
    };
    const section = createSection(titles[record.eventType] || "Stage map");
    if (record.source) section.appendChild(textNode("p", "Source: " + (record.source === "restored" ? "Restored" : record.source === "ai" ? "AI-assisted" : "Manual")));
    if (record.source === "restored") {
        section.appendChild(textNode("p", "Restored from Stage " + value(record.restoredFromStageNumber)
            + " · replaced Stage " + value(record.replacedStageNumber)));
    }
    if (record.initialDraftMethod) {
        section.appendChild(textNode("p", "Initial draft: " + formatMode(record.initialDraftMethod)));
    }
    const frame = document.createElement("div");
    frame.className = "mini-map-frame";
    const map = document.createElement("div");
    map.className = "mini-map";
    renderMap(map, safeArray(record.rows), true);
    frame.appendChild(map);
    section.appendChild(frame);
    const opening = findOpeningForStage(match, record);
    if (opening && !openingIsConsumed(match, opening)) appendOpeningDetails(section, opening);
    elements.inspectorBody.appendChild(section);
}

function appendFlowTurnDetails(record) {
    const section = createSection("Conversation");
    if (record.openingAssistantText) {
        section.append(
            textNode("h4", "AI opening evaluation"),
            textNode("p", record.openingAssistantText)
        );
        appendCards(section, record.openingCards, "Opening cards");
    }
    section.append(
        textNode("h4", "Player"),
        textNode("p", record.userText || "-"),
        textNode("h4", "AI"),
        textNode("p", record.assistantText || "-")
    );
    appendCards(section, record.cards);
    elements.inspectorBody.appendChild(section);
}

function appendFlowMessageDetails(record) {
    const section = createSection("Message to opponent");
    section.appendChild(textNode("p", record.message || "-"));
    elements.inspectorBody.appendChild(section);
}

function appendRecordGrid(rows) {
    const section = createSection("Record details");
    const grid = document.createElement("div");
    grid.className = "record-grid";
    rows.forEach(([label, rowValue, copyable]) => {
        const row = document.createElement("div");
        row.className = "record-row";
        row.append(
            textNode("span", label),
            copyable ? createIdControl(label, rowValue) : textNode("strong", value(rowValue))
        );
        grid.appendChild(row);
    });
    section.appendChild(grid);
    elements.inspectorBody.appendChild(section);
}

function getTimestamp(record) {
    return clean(record && (record.serverReceivedAt || record.timestamp));
}
