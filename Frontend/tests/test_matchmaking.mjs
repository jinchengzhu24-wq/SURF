import assert from "node:assert/strict";
import test from "node:test";

import {
    clean,
    createApiClient,
    escapeHtml,
    formatClockDuration,
    formatShortDate,
    formatTimestamp,
    getResponseError,
    resolveApiBase
} from "../js/shared/api-utils.js";
import {
    formatChallengeMetrics,
    formatChallengeOutcome
} from "../js/matchmaking/inspector-compare.js";
import {
    createMatchmakingDashboard,
    dashboardNodeStatus,
    flowStageLabel,
    formatNodeMetrics,
    getMatchDisplayTimestamp,
    hasFirstStage,
    isCompletedMatch
} from "../js/matchmaking/main.js";
import { buildTimelineStages as buildMatchTimelineStages } from "../js/matchmaking/nodes-timeline.js";
import {
    buildIdeaJourneys,
    createStudyDashboard,
    getIdeaHash,
    getStageDeleteConfig
} from "../js/study/main.js";
import {
    buildTimelineStages as buildStudyTimelineStages,
    getLevelStatus,
    surveyStageLabel
} from "../js/study/timeline-inspector.js";

function createFakeDom() {
    const byId = new Map();
    const summaryFilterButtons = [];

    function createElement(tagName = "div") {
        let text = "";
        const classSet = new Set();
        const listeners = new Map();
        const attributes = new Map();
        const node = {
            tagName: String(tagName).toLowerCase(),
            children: [],
            style: {},
            dataset: {},
            value: "",
            hidden: false,
            disabled: false,
            checked: false,
            open: false,
            href: "",
            title: "",
            type: "",
            className: "",
            classList: {
                add(...names) { names.forEach(name => classSet.add(name)); },
                remove(...names) { names.forEach(name => classSet.delete(name)); },
                toggle(name, force) {
                    if (force === undefined) {
                        if (classSet.has(name)) {
                            classSet.delete(name);
                            return false;
                        }
                        classSet.add(name);
                        return true;
                    }
                    if (force) classSet.add(name);
                    else classSet.delete(name);
                    return Boolean(force);
                },
                contains(name) { return classSet.has(name); }
            },
            addEventListener(eventName, handler) {
                if (!listeners.has(eventName)) listeners.set(eventName, []);
                listeners.get(eventName).push(handler);
            },
            dispatch(eventName, eventObj = {}) {
                const handlers = listeners.get(eventName) || [];
                for (const handler of handlers) {
                    handler({
                        preventDefault() {},
                        stopPropagation() {},
                        ...eventObj
                    });
                }
            },
            setAttribute(name, attrValue) { attributes.set(name, String(attrValue)); },
            getAttribute(name) { return attributes.get(name) ?? null; },
            showModal() { this.open = true; },
            close() { this.open = false; },
            focus() { this.focused = true; },
            select() { this.selected = true; },
            remove() {},
            set textContent(nextValue) {
                text = String(nextValue ?? "");
                this.children = [];
            },
            get textContent() {
                const childText = this.children.map(child => child.textContent || "").join("\n");
                if (text && childText) return text + "\n" + childText;
                return text + childText;
            },
            append(...items) {
                this.children.push(...items);
            },
            appendChild(child) {
                this.children.push(child);
                return child;
            }
        };
        return node;
    }

    const document = {
        getElementById(id) {
            if (!byId.has(id)) {
                const el = createElement("div");
                el.id = id;
                byId.set(id, el);
            }
            return byId.get(id);
        },
        createElement,
        querySelectorAll(selector) {
            if (selector === "[data-summary-filter]") {
                return summaryFilterButtons;
            }
            return [];
        }
    };

    return { document, byId, createElement, summaryFilterButtons };
}

function createFakeWindow({
    href = "https://sokobanaidemo.top/frontend/",
    origin = "https://sokobanaidemo.top",
    protocol = "https:",
    hostname = "sokobanaidemo.top",
    pathname = "/frontend/",
    search = ""
} = {}) {
    const replacedUrls = [];
    return {
        replacedUrls,
        window: {
            location: { href, origin, protocol, hostname, pathname, search },
            history: {
                replaceState(_state, _title, nextUrl) {
                    replacedUrls.push(String(nextUrl));
                }
            },
            requestAnimationFrame(callback) {
                callback();
            },
            setTimeout() {
                return 1;
            },
            navigator: {
                clipboard: {
                    async writeText() {}
                }
            }
        }
    };
}

test("Challenge statuses distinguish engagement, direction choice and map acceptance", () => {
    const labels = [
        dashboardNodeStatus("acknowledged"),
        dashboardNodeStatus("manual_reedit"),
        dashboardNodeStatus("original_selected"),
        dashboardNodeStatus("replacement_selected"),
        dashboardNodeStatus("pending"),
        dashboardNodeStatus("accepted"),
        dashboardNodeStatus("revised")
    ];
    assert.deepEqual(labels, [
        "Responded · proposal block released",
        "Ended through a new manual edit",
        "Original proposal selected",
        "New plan from the player's reason selected",
        "Pending",
        "Accepted",
        "Revised intention"
    ]);
});

test("Node details render the visible card, user reason and reply in time order", () => {
    const { document, byId } = createFakeDom();
    const { window } = createFakeWindow();
    const dashboard = createMatchmakingDashboard({
        window,
        document,
        fetch: async () => ({ ok: true, async json() { return { matches: [] }; } })
    });

    const record = {
        nodeType: "player_challenge",
        nodeStatus: "replacement_selected",
        stageNumber: 2,
        nodeParentId: "proposal:original",
        serverReceivedAt: "2026-10-10T01:00:00Z",
        nodeEntries: [
            { kind: "player_message", text: "No; follow my reason.", occurredAt: "2026-10-10T01:03:00Z" },
            { kind: "card", text: "Keep the original proposal? Yes keeps it; no plans a new one.", occurredAt: "2026-10-10T01:02:00Z" },
            { kind: "llm_message", text: "I compare your reason with the proposal.", occurredAt: "2026-10-10T01:01:00Z" }
        ]
    };

    const metrics = formatNodeMetrics(record);
    assert.equal(metrics[0][1], "New plan from the player's reason selected");
    assert.deepEqual(metrics.find(([label]) => label === "Stage"), ["Stage", "Stage 2"]);
    assert.deepEqual(metrics.find(([label]) => label === "Related node"), ["Related node", "proposal:original"]);

    dashboard.appendDashboardNodeDetails(record);
    const text = byId.get("inspectorBody").textContent;
    assert.match(text, /New plan from the player's reason selected/);
    assert.match(text, /proposal:original/);
    assert.ok(text.indexOf("I compare") < text.indexOf("Keep the original"));
    assert.ok(text.indexOf("Keep the original") < text.indexOf("No; follow"));
});

test("Restored Stage shows both source and replaced Stage alongside its map", () => {
    const { document, byId } = createFakeDom();
    const { window } = createFakeWindow();
    const dashboard = createMatchmakingDashboard({
        window,
        document,
        fetch: async () => ({ ok: true, async json() { return { matches: [] }; } })
    });

    const record = {
        eventType: "stage",
        source: "restored",
        stageNumber: 4,
        rows: ["###", "#p#", "###"],
        restoredFromStageNumber: 1,
        replacedStageNumber: 3
    };
    dashboard.appendFlowStageDetails({ players: [] }, record);
    const text = byId.get("inspectorBody").textContent;
    assert.match(text, /Source: Restored/);
    assert.match(text, /Restored from Stage 1 · replaced Stage 3/);
    assert.equal(flowStageLabel(record), "Stage 4 · Restored");
});

test("resolveApiBase ignores untrusted cross-origin api parameter and keeps same-origin or local loopback", () => {
    assert.equal(
        resolveApiBase({
            search: "?api=https://evil.example/api",
            protocol: "https:",
            origin: "https://sokobanaidemo.top",
            hostname: "sokobanaidemo.top"
        }),
        "https://sokobanaidemo.top"
    );

    assert.equal(
        resolveApiBase({
            search: "?api=https://sokobanaidemo.top/v2/",
            protocol: "https:",
            origin: "https://sokobanaidemo.top",
            hostname: "sokobanaidemo.top"
        }),
        "https://sokobanaidemo.top/v2"
    );

    assert.equal(
        resolveApiBase({
            search: "?api=http://127.0.0.1:8000/",
            protocol: "file:",
            origin: "null",
            hostname: ""
        }),
        "http://127.0.0.1:8000"
    );

    assert.equal(
        resolveApiBase({
            origin: "http://127.0.0.1:8000",
            pathname: "/frontend/"
        }),
        "http://127.0.0.1:8000"
    );
});

test("formatClockDuration, escapeHtml, clean and getMatchDisplayTimestamp handle formatting edge cases", () => {
    assert.equal(formatClockDuration(1200), "20:00");
    assert.equal(formatClockDuration(1500), "20:00");
    assert.equal(formatClockDuration(-5), "-");
    assert.equal(clean(" - "), "");
    assert.equal(clean(" room-1 "), "room-1");
    assert.equal(escapeHtml(`<div class="x">'a' & "b"</div>`), "&lt;div class=&quot;x&quot;&gt;&#39;a&#39; &amp; &quot;b&quot;&lt;/div&gt;");
    assert.equal(
        getMatchDisplayTimestamp({ createdAt: "2026-10-01T10:00:00Z", updatedAt: "2026-10-02T10:00:00Z" }),
        "2026-10-01T10:00:00Z"
    );
    assert.equal(
        getMatchDisplayTimestamp({ createdAt: "", updatedAt: "2026-10-02T10:00:00Z" }),
        "2026-10-02T10:00:00Z"
    );
    assert.equal(formatShortDate(""), "No time");
    assert.equal(formatTimestamp(""), "-");
});

test("Timed out challenge results show Timed out outcome and omit negative move overhead", () => {
    const { document, byId } = createFakeDom();
    const { window } = createFakeWindow();
    const dashboard = createMatchmakingDashboard({
        window,
        document,
        fetch: async () => ({ ok: true, async json() { return { matches: [] }; } })
    });

    const challenge = {
        createdByPlayerNumber: 1,
        playedByPlayerNumber: 2,
        rows: ["###", "#p#", "###"],
        runResult: {
            durationSeconds: 30,
            moveCount: 2,
            minimumMoves: 10,
            restartCount: 0,
            outcome: "timed_out"
        }
    };
    assert.equal(formatChallengeOutcome("timed_out"), "Timed out");
    const metricMap = new Map(formatChallengeMetrics(challenge));
    assert.equal(metricMap.get("Outcome"), "Timed out");
    assert.equal(metricMap.get("Move overhead"), "-");

    dashboard.state.selectedChallengePlayer = 1;
    dashboard.renderSelectedChallenge({
        players: [{ playerNumber: 1, challenge }]
    });
    const metricsText = byId.get("detailMetrics").textContent;
    assert.match(metricsText, /Timed out/);
    assert.doesNotMatch(metricsText, /\+-8/);
});

test("getResponseError and createApiClient format structured errors and bind injected fetch", async () => {
    const objMsg = await getResponseError({
        status: 502,
        async json() { return { detail: { code: "E1", message: "Upstream timed out" } }; }
    });
    assert.equal(objMsg, "Upstream timed out");

    const listMsg = await getResponseError({
        status: 422,
        async json() { return { detail: [{ msg: "Field required" }] }; }
    });
    assert.equal(listMsg, "Field required");

    const calls = [];
    const client = createApiClient({
        locationObj: { origin: "http://127.0.0.1:8000", pathname: "/frontend/" },
        fetchImpl: async (url, options) => {
            calls.push({ url, options });
            return {
                ok: true,
                status: 200,
                async json() { return { ok: true }; }
            };
        }
    });
    assert.equal(client.apiBase, "http://127.0.0.1:8000");
    assert.deepEqual(await client.getJson("/matchmaking-records-data"), { ok: true });
    assert.equal(calls[0].url, "http://127.0.0.1:8000/matchmaking-records-data");
});

test("createMatchmakingDashboard initializes via DI, filters First Stage matches, searches and deletes", async () => {
    const { document, byId } = createFakeDom();
    const { window, replacedUrls } = createFakeWindow({
        href: "https://sokobanaidemo.top/frontend/",
        origin: "https://sokobanaidemo.top",
        pathname: "/frontend/"
    });

    const requests = [];
    const samplePayload = {
        generatedAt: "2026-11-18T10:00:00Z",
        summary: { malformedCount: 0 },
        matches: [
            {
                matchId: "match-alpha-12345678",
                roomCode: "AB12CD",
                status: "completed",
                playerCount: 2,
                createdAt: "2026-11-18T09:00:00Z",
                players: [
                    {
                        playerNumber: 1,
                        studySessionId: "study-session-p1",
                        coCreationFlow: [
                            {
                                eventId: "fs-1",
                                eventType: "first_stage",
                                playerNumber: 1,
                                stageNumber: 1,
                                initialDraftMethod: "description_generation",
                                rows: ["####", "#pt#", "####"],
                                serverReceivedAt: "2026-11-18T09:01:00Z"
                            }
                        ],
                        challenge: {
                            createdByPlayerNumber: 1,
                            playedByPlayerNumber: 2,
                            rows: ["####", "#pt#", "####"],
                            runResult: { durationSeconds: 12, moveCount: 5, minimumMoves: 4, restartCount: 0, outcome: "completed" }
                        },
                        result: { durationSeconds: 18 }
                    },
                    {
                        playerNumber: 2,
                        studySessionId: "study-session-p2",
                        coCreationFlow: [],
                        challenge: {
                            createdByPlayerNumber: 2,
                            playedByPlayerNumber: 1,
                            rows: ["####", "#sp#", "####"],
                            runResult: { durationSeconds: 18, moveCount: 8, minimumMoves: 6, restartCount: 1, outcome: "completed" }
                        },
                        result: { durationSeconds: 12 }
                    }
                ]
            },
            {
                matchId: "match-hidden-no-first-stage",
                roomCode: "ZZZZZZ",
                status: "in_progress",
                playerCount: 1,
                createdAt: "2026-11-18T09:05:00Z",
                players: [{ playerNumber: 1, coCreationFlow: [] }]
            }
        ]
    };

    const dashboard = createMatchmakingDashboard({
        window,
        document,
        fetch: async (url, options = {}) => {
            requests.push({ url, options });
            return {
                ok: true,
                status: 200,
                async json() {
                    return samplePayload;
                }
            };
        }
    });

    assert.equal(hasFirstStage(samplePayload.matches[0]), true);
    assert.equal(hasFirstStage(samplePayload.matches[1]), false);
    assert.equal(isCompletedMatch(samplePayload.matches[0]), true);
    assert.equal(buildMatchTimelineStages(samplePayload.matches[0]).length, 1);

    await dashboard.init();

    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "https://sokobanaidemo.top/matchmaking-records-data");
    assert.equal(String(byId.get("statMatches").textContent), "1");
    assert.equal(String(byId.get("statCompleted").textContent), "1");
    assert.equal(byId.get("statCompletionRate").textContent, "100% completion rate");
    assert.equal(byId.get("statAvg").textContent, "15.0s");
    assert.equal(byId.get("statDataHealth").textContent, "1 rooms without First Stage hidden");
    assert.equal(byId.get("matchCount").textContent, "1 shown");
    assert.equal(byId.get("selectedMatchTitle").textContent, "ROOM AB12CD");
    assert.equal(byId.get("challengeCount").textContent, "2 challenges");
    assert.match(byId.get("compareContent").textContent, /Player 1 challenge/);
    assert.match(byId.get("compareContent").textContent, /Player 2 challenge/);

    dashboard.selectMatch("match-alpha-12345678");
    assert.ok(replacedUrls.some(url => url.includes("match=match-alpha-12345678")));

    byId.get("searchInput").value = "study-session-p2";
    dashboard.applyFilters();
    assert.equal(dashboard.state.filteredMatches.length, 1);

    byId.get("searchInput").value = "nonexistent-room";
    dashboard.applyFilters();
    assert.equal(dashboard.state.filteredMatches.length, 0);
    assert.equal(byId.get("emptyDetail").hidden, false);

    byId.get("searchInput").value = "";
    dashboard.applyFilters();
    dashboard.deleteSelectedMatch();
    assert.equal(byId.get("deleteDialog").open, true);
    byId.get("deletePasswordInput").value = "secret-1234";
    await dashboard.submitDeleteDialog({ preventDefault() {} });

    assert.equal(requests.length, 3);
    assert.equal(requests[1].url, "https://sokobanaidemo.top/delete-online-match");
    assert.equal(requests[1].options.method, "POST");
    assert.equal(requests[1].options.headers["X-Delete-Password"], "secret-1234");
});

test("createStudyDashboard initializes via DI, aggregates Custom_Level journeys, filters and inspects stages", async () => {
    const { document, byId } = createFakeDom();
    const { window, replacedUrls } = createFakeWindow({
        href: "https://sokobanaidemo.top/frontend/train.html",
        origin: "https://sokobanaidemo.top",
        pathname: "/frontend/train.html"
    });

    const studyPayload = {
        generatedAt: "2026-11-18T10:30:00Z",
        malformedCount: 0,
        creativeIdeas: [
            {
                ideaId: "idea-001",
                sessionId: "sess-001",
                ideaText: "Tight corridor puzzle",
                sceneName: "Creative_Input",
                serverReceivedAt: "2026-11-18T08:00:00Z"
            }
        ],
        creativeExpansionChoices: [
            {
                choiceId: "exp-001",
                ideaId: "idea-001",
                sessionId: "sess-001",
                originalIdeaText: "Tight corridor puzzle",
                selectedOptionId: "opt-a",
                selectedOptionTitle: "Symmetric bottleneck",
                finalIdeaText: "Symmetric tight corridor",
                serverReceivedAt: "2026-11-18T08:01:00Z"
            }
        ],
        levels: [
            {
                levelRunId: "run-v1",
                start: {
                    creativeIdeaId: "idea-001",
                    sessionId: "sess-001",
                    sceneName: "Custom_Level",
                    rows: ["#####", "#pst#", "#####"],
                    solutionSteps: 2,
                    solverPushes: 1,
                    generationAttempts: 1,
                    structure: { wallDensity: 0.5, waterDensity: 0, reachableAreaRatio: 0.4, deadCornerRisk: 0.1 },
                    timestamp: "2026-11-18T08:02:00Z"
                },
                end: {
                    sceneName: "Custom_Level",
                    completed: true,
                    durationSeconds: 14.2,
                    moveCount: 3,
                    pushCount: 1,
                    restartCount: 0,
                    timestamp: "2026-11-18T08:03:00Z"
                }
            },
            {
                levelRunId: "ignored-non-dashboard-scene",
                start: {
                    creativeIdeaId: "idea-ignored",
                    sessionId: "sess-ignored",
                    sceneName: "DG_Level",
                    rows: ["###"],
                    timestamp: "2026-11-18T08:02:00Z"
                }
            }
        ],
        haPlanEvents: [
            {
                haEventId: "ha-001",
                ideaId: "idea-001",
                sessionId: "sess-001",
                eventType: "ha-plan-choice",
                selectedOptionId: "plan-1",
                selectedOptionTitle: "Add corner pocket",
                adjustmentText: "Make it trickier",
                serverReceivedAt: "2026-11-18T08:04:00Z"
            }
        ],
        journeyEvents: [
            {
                journeyEventId: "je-001",
                ideaId: "idea-001",
                sessionId: "sess-001",
                phase: "routing",
                action: "selected",
                revisionMode: "ha",
                score: 4,
                serverReceivedAt: "2026-11-18T08:03:30Z"
            }
        ],
        surveyResponses: [
            {
                responseId: "srv-001",
                ideaId: "idea-001",
                sessionId: "sess-001",
                surveyId: "pre_study_survey",
                playerNickname: "TesterOne",
                durationSeconds: 20,
                answers: [{ questionText: "Experience", optionText: "High" }],
                serverReceivedAt: "2026-11-18T07:59:00Z"
            }
        ]
    };

    const journeys = buildIdeaJourneys(studyPayload);
    assert.equal(journeys.length, 1);
    assert.equal(journeys[0].ideaHash, getIdeaHash("idea-001", "Tight corridor puzzle"));
    assert.equal(getLevelStatus(journeys[0].levels[0]).label, "Completed");
    assert.equal(surveyStageLabel(studyPayload.surveyResponses[0], 0), "Pre-survey");

    const stages = buildStudyTimelineStages(journeys[0]);
    assert.equal(stages.length, 6);
    assert.deepEqual(getStageDeleteConfig(stages[0]), {
        endpoint: "/delete-survey-response",
        payload: { responseId: "srv-001" },
        label: "survey response"
    });

    const requests = [];
    const studyDashboard = createStudyDashboard({
        window,
        document,
        fetch: async url => {
            requests.push(url);
            return {
                ok: true,
                status: 200,
                async json() { return studyPayload; }
            };
        }
    });

    await studyDashboard.init();
    assert.deepEqual(requests, ["https://sokobanaidemo.top/level-records-data"]);
    assert.equal(String(byId.get("statIdeas").textContent), "1");
    assert.equal(String(byId.get("statCompleted").textContent), "1");
    assert.equal(byId.get("ideaCount").textContent, "1 shown");
    assert.match(byId.get("selectedIdeaTitle").textContent, /^Idea /);
    assert.match(byId.get("selectedIdeaMeta").textContent, /TesterOne/);

    studyDashboard.selectJourney(journeys[0].key);
    assert.ok(replacedUrls.some(url => url.includes("idea=" + journeys[0].ideaHash)));

    byId.get("modeFilter").value = "ai";
    studyDashboard.applyFilters();
    assert.equal(studyDashboard.state.filteredJourneys.length, 0);

    byId.get("modeFilter").value = "ha";
    studyDashboard.applyFilters();
    assert.equal(studyDashboard.state.filteredJourneys.length, 1);
});

test("Dashboard access query gate opens password verification before fetching records", async () => {
    const { document, byId } = createFakeDom();
    const { window } = createFakeWindow({
        href: "https://sokobanaidemo.top/frontend/?access=1",
        origin: "https://sokobanaidemo.top",
        pathname: "/frontend/",
        search: "?access=1"
    });

    let fetchCount = 0;
    const dashboard = createMatchmakingDashboard({
        window,
        document,
        fetch: async () => {
            fetchCount += 1;
            return { ok: true, async json() { return { matches: [] }; } };
        }
    });

    await dashboard.init();
    assert.equal(fetchCount, 0);
    assert.equal(byId.get("deleteDialog").open, true);
    assert.equal(byId.get("deleteDialogTitle").textContent, "Dashboard access required");

    // Cancel + Refresh must keep the dashboard locked with 0 fetches and re-open the password gate
    byId.get("deleteDialogCancel").dispatch("click");
    assert.equal(byId.get("deleteDialog").open, false);
    assert.match(byId.get("statusLine").textContent, /password required/i);
    byId.get("refreshButton").dispatch("click");
    assert.equal(fetchCount, 0);
    assert.equal(byId.get("deleteDialog").open, true);
});

test("Study Dashboard multi-idea same-session separation, finalizeJourney sort, statCompleted, and file:// safe unlock", async () => {
    const journeys = buildIdeaJourneys({
        creativeIdeas: [
            { ideaId: "idea-2", sessionId: "shared-s", ideaText: "Newer idea", timestamp: "2026-11-25T10:05:00Z" },
            { ideaId: "idea-1", sessionId: "shared-s", ideaText: "First idea", timestamp: "2026-11-25T10:00:00Z" }
        ],
        creativeExpansionChoices: [],
        levels: [
            {
                levelRunId: "run-1",
                start: { source: "pc", sceneName: "Custom_Level", sessionId: "shared-s", creativeIdeaId: "idea-1", timestamp: "2026-11-25T10:01:00Z" },
                end: { completed: true, serverReceivedAt: "2026-11-25T10:01:20Z" }
            },
            {
                levelRunId: "run-2",
                start: { source: "pc", sceneName: "Custom_Level", sessionId: "shared-s", creativeIdeaId: "idea-1", timestamp: "2026-11-25T10:02:00Z" },
                end: null
            }
        ],
        surveyResponses: [],
        haPlanEvents: [],
        journeyEvents: []
    });
    assert.equal(journeys.length, 2);
    const j1 = journeys.find(item => item.ideaId === "idea-1");
    assert.equal(j1.ideaText, "First idea");
    assert.equal(j1.status, "anomaly");

    // Inspector should render serverReceivedAt instead of "Missing end record" when end.timestamp is absent
    const { document, byId } = createFakeDom();
    const inspector = buildStudyTimelineStages(j1);
    assert.equal(inspector[0].type, "creative");
    const collectText = node => {
        if (!node) return "";
        if (typeof node === "string") return node;
        return [node.textContent || "", ...(node.children || []).map(collectText)].join(" ");
    };
    const studyWindow = createFakeWindow({
        href: "file:///D:/Sokoban_AI_Demo/Frontend/train.html?access=1",
        origin: "null",
        protocol: "file:",
        pathname: "/D:/Sokoban_AI_Demo/Frontend/train.html",
        search: "?access=1"
    });
    studyWindow.window.history = {
        replaceState() {
            throw new Error("SecurityError in file://");
        }
    };
    const studyDashboard = createStudyDashboard({
        window: studyWindow.window,
        document,
        fetch: async (url, init = {}) => {
            if (String(url).endsWith("/verify-dashboard-password")) {
                return { ok: true, status: 200, async json() { return { status: "ok" }; } };
            }
            return {
                ok: true,
                status: 200,
                async json() {
                    return {
                        creativeIdeas: [{ ideaId: "idea-1", sessionId: "s-1", ideaText: "Oldest", timestamp: "2026-11-25T10:00:00Z" }],
                        levels: [
                            { levelRunId: "r-ok", start: { source: "pc", sceneName: "Custom_Level", sessionId: "s-1", creativeIdeaId: "idea-1", timestamp: "2026-11-25T10:01:00Z" }, end: { completed: true, serverReceivedAt: "2026-11-25T10:01:30Z" } },
                            { levelRunId: "r-miss", start: { source: "pc", sceneName: "Custom_Level", sessionId: "s-1", creativeIdeaId: "idea-1", timestamp: "2026-11-25T10:02:00Z" }, end: null }
                        ],
                        surveyResponses: [],
                        creativeExpansionChoices: [],
                        haPlanEvents: [],
                        journeyEvents: []
                    };
                }
            };
        }
    });
    await studyDashboard.init();
    byId.get("deletePasswordInput").value = "pw123";
    await byId.get("deleteDialogForm").dispatch("submit");
    // Wait a tick for async submitDeleteDialog
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(byId.get("deleteDialog").open, false);
    // statCompleted must align with status === "completed" (0 here because journey is anomaly)
    assert.equal(String(byId.get("statCompleted").textContent), "0");
    // Render inspector detail and check serverReceivedAt fallback
    studyDashboard.selectJourney(studyDashboard.state.journeys[0].key);
    const okStage = buildStudyTimelineStages(studyDashboard.state.journeys[0]).find(
        stage => stage.level && stage.level.levelRunId === "r-ok"
    );
    studyDashboard.renderInspector(okStage);
    const inspectorText = collectText(byId.get("inspectorBody"));
    assert.doesNotMatch(inspectorText, /Missing end record/);
    assert.match(inspectorText, /Ended/);
});
