const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadDashboard() {
    const byId = new Map();
    function element(tagName = "div") {
        let text = "";
        return {
            tagName, children: [], style: {}, dataset: {},
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
        window: { location: { search: "", protocol: "https:", origin: "https://sokobanaidemo.top" } }
    });
    const source = fs.readFileSync(path.join(__dirname, "..", "matchmaking.js"), "utf8");
    vm.runInContext(source.replace(/^init\(\);\r?$/m, ""), context);
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
