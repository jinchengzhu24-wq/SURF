const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadDashboard({ location } = {}) {
    const byId = new Map();
    function element(tagName = "div") {
        let text = "";
        return {
            tagName, children: [], style: {}, dataset: {},
            classList: { add() {}, remove() {}, toggle() {} },
            addEventListener() {},
            setAttribute() {},
            set textContent(value) { text = String(value); this.children = []; },
            get textContent() { return text + this.children.map(child => child.textContent || "").join("\n"); },
            append(...children) { this.children.push(...children); },
            appendChild(child) { this.children.push(child); return child; }
        };
    }
    const document = {
        getElementById(id) {
            if (!byId.has(id)) byId.set(id, element());
            return byId.get(id);
        },
        createElement: element
    };
    const context = vm.createContext({
        document, URLSearchParams, URL, console,
        window: {
            location: location || { search: "", protocol: "https:", origin: "https://sokobanaidemo.top", hostname: "sokobanaidemo.top" }
        }
    });
    const scriptFiles = [
        path.join(__dirname, "..", "js", "shared", "api-utils.js"),
        path.join(__dirname, "..", "js", "matchmaking", "nodes-timeline.js"),
        path.join(__dirname, "..", "js", "matchmaking", "inspector-compare.js"),
        path.join(__dirname, "..", "js", "matchmaking", "main.js")
    ];
    for (const filePath of scriptFiles) {
        const source = fs.readFileSync(filePath, "utf8");
        vm.runInContext(source.replace(/^init\(\);\r?$/m, ""), context);
    }
    return { context, byId };
}

test("Challenge statuses distinguish engagement, direction choice and map acceptance", () => {
    const { context } = loadDashboard();
    const labels = vm.runInContext(`[
        dashboardNodeStatus("acknowledged"), dashboardNodeStatus("manual_reedit"),
        dashboardNodeStatus("original_selected"), dashboardNodeStatus("replacement_selected"),
        dashboardNodeStatus("pending"), dashboardNodeStatus("accepted"), dashboardNodeStatus("revised")
    ]`, context);
    assert.deepEqual(Array.from(labels), [
        "Responded · proposal block released", "Ended through a new manual edit",
        "Original proposal selected", "New plan from the player's reason selected",
        "Pending", "Accepted", "Revised intention"
    ]);
});

test("Node details render the visible card, user reason and reply in time order", () => {
    const { context, byId } = loadDashboard();
    context.record = {
        nodeType: "player_challenge", nodeStatus: "replacement_selected", stageNumber: 2,
        nodeParentId: "proposal:original", serverReceivedAt: "2026-10-10T01:00:00Z",
        nodeEntries: [
            { kind: "player_message", text: "No; follow my reason.", occurredAt: "2026-10-10T01:03:00Z" },
            { kind: "card", text: "Keep the original proposal? Yes keeps it; no plans a new one.", occurredAt: "2026-10-10T01:02:00Z" },
            { kind: "llm_message", text: "I compare your reason with the proposal.", occurredAt: "2026-10-10T01:01:00Z" }
        ]
    };
    vm.runInContext("appendDashboardNodeDetails(record)", context);
    const text = byId.get("inspectorBody").textContent;
    assert.match(text, /New plan from the player's reason selected/);
    assert.match(text, /proposal:original/);
    assert.ok(text.indexOf("I compare") < text.indexOf("Keep the original"));
    assert.ok(text.indexOf("Keep the original") < text.indexOf("No; follow"));
});

test("Restored Stage shows both source and replaced Stage alongside its map", () => {
    const { context, byId } = loadDashboard();
    context.record = {
        eventType: "stage", source: "restored", stageNumber: 4, rows: ["###", "#p#", "###"],
        restoredFromStageNumber: 1, replacedStageNumber: 3
    };
    vm.runInContext("appendFlowStageDetails({ players: [] }, record)", context);
    const text = byId.get("inspectorBody").textContent;
    assert.match(text, /Source: Restored/);
    assert.match(text, /Restored from Stage 1 · replaced Stage 3/);
    assert.equal(vm.runInContext("flowStageLabel(record)", context), "Stage 4 · Restored");
});

test("resolveApiBase ignores untrusted cross-origin api parameter and keeps same-origin or local loopback", () => {
    const evil = loadDashboard({
        location: { search: "?api=https://evil.example/api", protocol: "https:", origin: "https://sokobanaidemo.top", hostname: "sokobanaidemo.top" }
    });
    assert.equal(vm.runInContext("state.apiBase", evil.context), "https://sokobanaidemo.top");

    const sameOrigin = loadDashboard({
        location: { search: "?api=https://sokobanaidemo.top/v2/", protocol: "https:", origin: "https://sokobanaidemo.top", hostname: "sokobanaidemo.top" }
    });
    assert.equal(vm.runInContext("state.apiBase", sameOrigin.context), "https://sokobanaidemo.top/v2");

    const localOverride = loadDashboard({
        location: { search: "?api=http://127.0.0.1:8000/", protocol: "file:", origin: "null", hostname: "" }
    });
    assert.equal(vm.runInContext("state.apiBase", localOverride.context), "http://127.0.0.1:8000");
});

test("formatClockDuration supports 20-minute Stage 1 durations without clamping at 10 minutes", () => {
    const { context } = loadDashboard();
    assert.equal(vm.runInContext("formatClockDuration(1200)", context), "20:00");
    assert.equal(vm.runInContext("formatClockDuration(1500)", context), "20:00");
});

test("Timed out challenge results show Timed out outcome and omit negative move overhead", () => {
    const { context, byId } = loadDashboard();
    context.matchData = {
        players: [{
            playerNumber: 1,
            challenge: {
                createdByPlayerNumber: 1,
                playedByPlayerNumber: 2,
                rows: ["###", "#p#", "###"],
                runResult: { durationSeconds: 30, moveCount: 2, minimumMoves: 10, restartCount: 0, outcome: "timed_out" }
            }
        }]
    };
    vm.runInContext("state.selectedChallengePlayer = 1; renderSelectedChallenge(matchData);", context);
    const metricsText = byId.get("detailMetrics").textContent;
    assert.match(metricsText, /Timed out/);
    assert.doesNotMatch(metricsText, /\+-8/);
});

test("getResponseError formats structured object and list detail without [object Object]", async () => {
    const { context } = loadDashboard();
    const objMsg = await context.getResponseError({
        status: 502,
        async json() { return { detail: { code: "E1", message: "Upstream timed out" } }; }
    });
    assert.equal(objMsg, "Upstream timed out");

    const listMsg = await context.getResponseError({
        status: 422,
        async json() { return { detail: [{ msg: "Field required" }] }; }
    });
    assert.equal(listMsg, "Field required");
});
