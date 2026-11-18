import {
    clean,
    createDomHelpers,
    formatShortDate,
    formatTimestamp,
    numeric,
    safeArray,
    titleCase,
    value
} from "../shared/api-utils.js";

export function getTimestamp(record) {
    return clean(record && (record.serverReceivedAt || record.timestamp));
}

export function getMatchDisplayTimestamp(match) {
    return clean(match && (match.createdAt || match.updatedAt));
}

export function formatMode(input) {
    const labels = {
        description_generation: "Description Generation"
    };
    return labels[input] || titleCase(input);
}

export function dashboardNodeLabel(nodeType) {
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

export function dashboardNodeStatus(status) {
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

export function flowStageLabel(event) {
    const labels = {
        draft: "Draft",
        first_stage: "First Stage",
        stage: "Stage " + value(event && event.stageNumber) + " · " + (event && event.source === "ai" ? "AI" : "Manual"),
        turn: "Q&A",
        final: "Final Stage",
        message: "Message"
    };
    if (event && event.eventType === "stage") {
        return "Stage " + value(event.stageNumber) + " · " + (event.source === "restored" ? "Restored" : event.source === "ai" ? "AI" : "Manual");
    }
    if (event && event.eventType === "node") return dashboardNodeLabel(event.nodeType);
    return labels[event && event.eventType] || titleCase((event && event.eventType) || "Flow event");
}

export function eventStageLabel(event) {
    const player = numeric(event && event.playerNumber) ? "P" + event.playerNumber + " " : "";
    const labels = {
        room_created: "Room created",
        player_joined: player + "joined",
        ready_changed: player + (event && event.ready ? "ready" : "not ready"),
        challenge_submitted: player + "challenge submitted",
        designer_intention_synchronized: player + "message confirmed",
        result_submitted: player + "result submitted",
        player_left: player + "left",
        room_expired: "Room expired"
    };
    return labels[event && event.eventType] || titleCase((event && event.eventType) || "Match event");
}

export function formatNodeMetrics(record) {
    const rows = [
        ["Status", dashboardNodeStatus(record && record.nodeStatus)],
        ["Started", formatTimestamp(record && record.serverReceivedAt)],
        ["Last updated", formatTimestamp(record && (record.nodeUpdatedAt || record.serverReceivedAt))]
    ];
    if (record && record.stageNumber !== undefined && record.stageNumber !== null) {
        rows.push(["Stage", "Stage " + value(record.stageNumber)]);
    }
    if (record && record.nodeParentId) {
        rows.push(["Related node", record.nodeParentId]);
    }
    return rows;
}

export function buildTimelineStages(match) {
    const flowStages = safeArray(match && match.players).flatMap(player =>
        safeArray(player && player.coCreationFlow)
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
    if (flowStages.length) {
        return flowStages.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
    }
    const stages = safeArray(match && match.events).map((event, index) => ({
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

export function findOpeningForStage(match, record) {
    const player = safeArray(match && match.players).find(item => item.playerNumber === record.playerNumber);
    return safeArray(player && player.coCreationFlow).find(event =>
        event.eventType === "opening" && event.versionId === record.versionId
    ) || null;
}

export function openingIsConsumed(match, opening) {
    const player = safeArray(match && match.players).find(item => item.playerNumber === opening.playerNumber);
    return safeArray(player && player.coCreationFlow).some(event =>
        event.eventType === "turn" && event.openingAssistantTurnId === opening.assistantTurnId
    );
}

export function createMatchmakingTimelineRenderer({
    document: doc,
    elements,
    state,
    onSelectStage,
    createIdControl
} = {}) {
    const dom = createDomHelpers({ document: doc });

    function renderTimeline(match, stages) {
        elements.matchTimeline.textContent = "";
        elements.matchTimeline.classList?.add?.("player-flow-list");
        if (!stages.length) {
            elements.matchTimeline.appendChild(dom.emptyNode("No match stages recorded."));
            return;
        }
        const groups = new Map([[1, []], [2, []]]);
        stages.forEach(stage => groups.get(stage.playerNumber)?.push(stage));
        groups.forEach((playerStages, playerNumber) => {
            const lane = doc.createElement("section");
            lane.className = "player-flow";
            lane.appendChild(dom.textNode("h3", "Player " + playerNumber, "player-flow-title"));
            const nodes = doc.createElement("div");
            nodes.className = "timeline player-flow-timeline";
            if (!playerStages.length) nodes.appendChild(dom.emptyNode("No co-creation records."));
            playerStages.forEach((stage, index) => {
                const button = doc.createElement("button");
                button.type = "button";
                button.className = "timeline-node";
                if (stage.key === state.selectedStageKey) button.classList?.add?.("selected");
                if (stage.warning) button.classList?.add?.("warning");
                button.append(
                    dom.textNode("span", stage.warning ? "!" : String(index + 1), "timeline-dot"),
                    dom.textNode("span", stage.label, "timeline-label"),
                    dom.textNode("span", formatShortDate(stage.timestamp), "timeline-time")
                );
                button.addEventListener?.("click", () => {
                    state.selectedStageKey = stage.key;
                    if (stage.record.eventType === "challenge_submitted") {
                        state.selectedChallengePlayer = stage.playerNumber;
                    }
                    onSelectStage?.(match, stage);
                });
                nodes.appendChild(button);
            });
            lane.appendChild(nodes);
            elements.matchTimeline.appendChild(lane);
        });
    }

    function appendDashboardNodeDetails(record) {
        const section = dom.createSection(dashboardNodeLabel(record.nodeType));
        const rows = formatNodeMetrics(record);
        const grid = doc.createElement("div");
        grid.className = "record-grid";
        rows.forEach(([label, rowValue]) => {
            const row = doc.createElement("div");
            row.className = "record-row";
            row.append(dom.textNode("span", label), dom.textNode("strong", value(rowValue)));
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
        if (!entries.length) section.appendChild(dom.emptyNode("No public node entries recorded."));
        entries.forEach(entry => {
            const article = doc.createElement("article");
            article.className = "node-entry";
            article.append(
                dom.textNode("h4", entry.label || labels[entry.kind] || "Entry"),
                dom.textNode("p", entry.text || "-"),
                dom.textNode("small", formatTimestamp(entry.occurredAt))
            );
            section.appendChild(article);
        });
        elements.inspectorBody.appendChild(section);
    }

    function appendDraftDetails(record) {
        const section = dom.createSection("Draft research record");
        const grid = doc.createElement("div");
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
            const row = doc.createElement("div");
            row.className = "record-row";
            row.append(dom.textNode("span", label), dom.textNode("strong", value(rowValue)));
            grid.appendChild(row);
        });
        section.appendChild(grid);
        section.append(
            dom.textNode("h4", "AI Reflection"),
            dom.textNode("p", value(record.aiReflection)),
            dom.textNode("h4", "AI difficulty rationale"),
            dom.textNode("p", value(record.aiDifficultyRationale)),
            dom.textNode("h4", "AI layout rationale"),
            dom.textNode("p", value(record.aiLayoutRationale))
        );
        elements.inspectorBody.appendChild(section);
    }

    function appendCards(section, cards, title = "Cards") {
        const safeCards = safeArray(cards);
        if (!safeCards.length) return;
        section.appendChild(dom.textNode("h4", title));
        safeCards.forEach(card => section.appendChild(
            dom.textNode("p", "[" + value(card.type) + "] " + value(card.text))
        ));
    }

    function appendOpeningDetails(section, opening) {
        section.append(
            dom.textNode("h4", "AI opening evaluation"),
            dom.textNode("p", opening.assistantText || "-")
        );
        appendCards(section, opening.cards, "Opening cards");
    }

    function appendFlowStageDetails(match, record) {
        const titles = {
            first_stage: "First Stage map",
            final: "Final map",
            stage: "Stage change"
        };
        const section = dom.createSection(titles[record.eventType] || "Stage map");
        if (record.source) {
            section.appendChild(dom.textNode(
                "p",
                "Source: " + (record.source === "restored" ? "Restored" : record.source === "ai" ? "AI-assisted" : "Manual")
            ));
        }
        if (record.source === "restored") {
            section.appendChild(dom.textNode(
                "p",
                "Restored from Stage " + value(record.restoredFromStageNumber)
                    + " · replaced Stage " + value(record.replacedStageNumber)
            ));
        }
        if (record.initialDraftMethod) {
            section.appendChild(dom.textNode("p", "Initial draft: " + formatMode(record.initialDraftMethod)));
        }
        const frame = doc.createElement("div");
        frame.className = "mini-map-frame";
        const map = doc.createElement("div");
        map.className = "mini-map";
        dom.renderMap(map, safeArray(record.rows), true);
        frame.appendChild(map);
        section.appendChild(frame);
        const opening = findOpeningForStage(match, record);
        if (opening && !openingIsConsumed(match, opening)) {
            appendOpeningDetails(section, opening);
        }
        elements.inspectorBody.appendChild(section);
    }

    function appendFlowTurnDetails(record) {
        const section = dom.createSection("Conversation");
        if (record.openingAssistantText) {
            section.append(
                dom.textNode("h4", "AI opening evaluation"),
                dom.textNode("p", record.openingAssistantText)
            );
            appendCards(section, record.openingCards, "Opening cards");
        }
        section.append(
            dom.textNode("h4", "Player"),
            dom.textNode("p", record.userText || "-"),
            dom.textNode("h4", "AI"),
            dom.textNode("p", record.assistantText || "-")
        );
        appendCards(section, record.cards);
        elements.inspectorBody.appendChild(section);
    }

    function appendFlowMessageDetails(record) {
        const section = dom.createSection("Message to opponent");
        section.appendChild(dom.textNode("p", record.message || "-"));
        elements.inspectorBody.appendChild(section);
    }

    function appendRecordGrid(rows) {
        const section = dom.createSection("Record details");
        const grid = doc.createElement("div");
        grid.className = "record-grid";
        rows.forEach(([label, rowValue, copyable]) => {
            const row = doc.createElement("div");
            row.className = "record-row";
            const valueNode = copyable && typeof createIdControl === "function"
                ? createIdControl(label, rowValue)
                : dom.textNode("strong", value(rowValue));
            row.append(dom.textNode("span", label), valueNode);
            grid.appendChild(row);
        });
        section.appendChild(grid);
        elements.inspectorBody.appendChild(section);
    }

    return {
        renderTimeline,
        appendDashboardNodeDetails,
        appendDraftDetails,
        appendCards,
        appendOpeningDetails,
        appendFlowStageDetails,
        appendFlowTurnDetails,
        appendFlowMessageDetails,
        appendRecordGrid
    };
}
