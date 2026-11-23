import assert from "node:assert/strict";
import test from "node:test";
import {
    REPLY_DIAGNOSTICS_KEY,
    REPLY_PIPELINE_VERSION,
    createWorkbenchApp,
} from "../js/main.js";

function createStorage(initialEntries = []) {
    const data = new Map(initialEntries);
    return {
        data,
        getItem(key) { return data.has(key) ? data.get(key) : null; },
        setItem(key, value) { data.set(key, String(value)); },
        removeItem(key) { data.delete(key); },
    };
}

function escapeHtmlText(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function createMockDom() {
    const created = [];
    const byId = new Map();

    function hasClass(node, cls) {
        return String(node?.className || "").split(/\s+/).filter(Boolean).includes(cls);
    }

    function matchesSimpleSelector(node, token) {
        if (!node || typeof node !== "object") return false;
        if (token.startsWith(".")) return hasClass(node, token.slice(1));
        return node.tagName === token;
    }

    function collectDescendants(root, predicate, results = []) {
        for (const child of root.children || []) {
            if (!child || typeof child !== "object") continue;
            if (predicate(child)) results.push(child);
            collectDescendants(child, predicate, results);
        }
        return results;
    }

    function element(tagName = "div") {
        const attributes = new Map();
        const listeners = new Map();
        const classes = new Set();
        let classNameValue = "";
        let textValue = "";
        let htmlValue = "";

        const syncClassesFromString = value => {
            classNameValue = String(value ?? "");
            classes.clear();
            classNameValue.split(/\s+/).filter(Boolean).forEach(name => classes.add(name));
        };
        const syncStringFromClasses = () => {
            classNameValue = Array.from(classes).join(" ");
        };

        const node = {
            tagName,
            id: "",
            dataset: {},
            disabled: false,
            open: false,
            value: "",
            hidden: false,
            isConnected: true,
            children: [],
            style: { setProperty() {} },
            get className() { return classNameValue; },
            set className(value) { syncClassesFromString(value); },
            get textContent() { return textValue; },
            set textContent(value) {
                textValue = String(value ?? "");
                htmlValue = escapeHtmlText(textValue);
                this.children = [];
            },
            get innerHTML() { return htmlValue; },
            set innerHTML(value) {
                htmlValue = String(value ?? "");
                textValue = htmlValue;
            },
            addEventListener(type, handler) { listeners.set(type, handler); },
            setAttribute(name, value) { attributes.set(name, String(value)); },
            removeAttribute(name) { attributes.delete(name); },
            getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
            append(...children) { this.children.push(...children); },
            appendChild(child) { this.children.push(child); return child; },
            insertBefore(child, reference) {
                const idx = this.children.indexOf(reference);
                if (idx >= 0) this.children.splice(idx, 0, child);
                else this.children.push(child);
                return child;
            },
            remove() { this.isConnected = false; },
            focus() {},
            select() {},
            requestSubmit() {
                listeners.get("submit")?.({ preventDefault() {} });
            },
            click() {
                if (this.disabled) return;
                if (typeof this.onclick === "function") this.onclick();
                listeners.get("click")?.({ preventDefault() {} });
            },
            getBoundingClientRect() {
                return { left: 0, top: 0, right: 100, bottom: 40, width: 100, height: 40 };
            },
            getClientRects() { return [{ width: 100, height: 40 }]; },
            closest() { return null; },
            contains(other) { return other === this || this.children.includes(other); },
            classList: {
                toggle(name, force) {
                    const next = force === undefined ? !classes.has(name) : Boolean(force);
                    if (next) classes.add(name);
                    else classes.delete(name);
                    syncStringFromClasses();
                    return next;
                },
                add(...names) { names.forEach(n => classes.add(n)); syncStringFromClasses(); },
                remove(...names) { names.forEach(n => classes.delete(n)); syncStringFromClasses(); },
                contains(name) { return classes.has(name); },
            },
            querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
            },
            querySelectorAll(selector) {
                const parts = selector.split(",").map(s => s.trim()).filter(Boolean);
                return collectDescendants(this, candidate => parts.some(part => matchesSimpleSelector(candidate, part)));
            },
            get clickHandler() { return listeners.get("click"); },
        };
        created.push(node);
        return node;
    }

    const document = {
        hidden: false,
        documentElement: { lang: "zh-CN" },
        getElementById(id) {
            if (!byId.has(id)) {
                const el = element("div");
                el.id = id;
                byId.set(id, el);
            }
            return byId.get(id);
        },
        createElement: element,
        createTextNode(text) { return { nodeType: 3, textContent: String(text ?? "") }; },
        createElementNS(_ns, tag) { return element(tag); },
        addEventListener() {},
        querySelector() { return null; },
        querySelectorAll(selector) {
            const parts = selector.split(",").map(s => s.trim()).filter(Boolean);
            const matched = new Set();
            for (const part of parts) {
                if (part.includes(" ")) {
                    const [ancestorToken, descendantToken] = part.split(/\s+/, 2);
                    created
                        .filter(node => matchesSimpleSelector(node, ancestorToken))
                        .forEach(ancestor => {
                            collectDescendants(ancestor, child => matchesSimpleSelector(child, descendantToken))
                                .forEach(item => matched.add(item));
                        });
                } else if (part.startsWith(".")) {
                    const cls = part.slice(1).replace(/\[.*$/, "");
                    created.filter(node => hasClass(node, cls)).forEach(item => matched.add(item));
                }
            }
            return Array.from(matched);
        },
    };

    return { document, created };
}

function createDefaultSession() {
    return {
        sessionId: "session",
        status: "active",
        languageLocked: true,
        currentVersionId: "v1",
        deadlineExpired: false,
        versions: [{ versionId: "v1", stageNumber: 1, rows: [] }],
        assessments: [],
        proposals: [],
        turns: [{
            turnId: "turn-1",
            role: "assistant",
            versionId: "v1",
            guidance: {
                disagreement: { status: "active" },
                challengeState: { challengeId: "challenge-1", status: "choice_pending" },
            },
        }],
    };
}

function loadApp({
    keepSubmit = false,
    keepPersistPending = false,
    window: customWindow,
    document: customDocument,
    fetch: customFetch,
    performance: customPerformance,
    localStorage: customLocalStorage,
    sessionStorage: customSessionStorage,
    history: customHistory,
    setTimeout: customSetTimeout,
    clearTimeout: customClearTimeout,
    setInterval: customSetInterval,
    clearInterval: customClearInterval,
    overrides = {},
} = {}) {
    const dom = customDocument ? { document: customDocument, created: [] } : createMockDom();
    const history = customHistory || { replaceState() {} };
    const win = customWindow || {
        location: {
            pathname: "/cocreation/",
            origin: "http://localhost",
            href: "http://localhost/cocreation/",
            search: "",
            hash: "",
        },
        history,
        addEventListener() {},
        removeEventListener() {},
    };
    const localStorage = customLocalStorage || createStorage();
    const sessionStorage = customSessionStorage || createStorage();
    const defaultOverrides = {
        ...(keepPersistPending ? {} : { persistPendingMessage: () => {} }),
        ...(keepSubmit ? {} : { submitPendingMessage: async () => {} }),
        ...overrides,
    };

    const app = createWorkbenchApp({
        window: win,
        document: dom.document,
        history,
        crypto: { randomUUID: () => "12345678-1234-1234-1234-123456789abc" },
        localStorage,
        sessionStorage,
        fetch: customFetch,
        performance: customPerformance || { now: () => 0 },
        setTimeout: customSetTimeout || (() => 1),
        clearTimeout: customClearTimeout || (() => {}),
        setInterval: customSetInterval || (() => 1),
        clearInterval: customClearInterval || (() => {}),
        overrides: defaultOverrides,
    });

    app.state.sessionId = "session";
    app.state.selectedVersionId = "v1";
    app.state.session = createDefaultSession();

    return {
        app,
        state: app.state,
        elements: app.elements,
        ports: app.ports,
        document: dom.document,
        window: win,
        localStorage,
        sessionStorage,
        created: dom.created,
    };
}

test("entry language buttons select directly and respect pending and locked sessions", () => {
    const { state, elements } = loadApp({
        overrides: { applyTranslations: () => {} },
    });
    elements.landing.hidden = true;
    state.session.languageLocked = false;
    state.language = "zh-CN";

    elements.setupEnglishButton.click();
    elements.setupEnglishButton.click();
    assert.equal(state.language, "en");

    state.busy = true;
    elements.setupChineseButton.click();
    assert.equal(state.language, "en");

    state.busy = false;
    state.demoCreationPending = true;
    elements.setupChineseButton.click();
    assert.equal(state.language, "en");

    state.demoCreationPending = false;
    state.session.languageLocked = true;
    elements.setupChineseButton.click();
    assert.equal(state.language, "en");

    state.session.languageLocked = false;
    elements.setupChineseButton.click();
    assert.equal(state.language, "zh-CN");
});

test("proposal retry without a local request allows re-entry while captured retries stay locked", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    state.session.proposalFlowState = { active: true, status: "retry_pending" };
    state.pendingMessage = null;
    elements.messageInput.value = "My previous answer";

    app.updateControls();
    assert.equal(elements.messageInput.disabled, false);
    assert.equal(elements.sendButton.disabled, false);

    state.pendingMessage = { content: "My previous answer" };
    app.updateControls();
    assert.equal(elements.messageInput.disabled, true);
});

test("a stale proposal answer keeps its wording but drops the old question retry key", async () => {
    const { app, state, elements } = loadApp({
        keepSubmit: true,
        overrides: {
            startChatTimer: () => {},
            stopChatTimer: () => {},
            hideNotice: () => {},
            renderChatRequestStatus: () => {},
            updateControls: () => {},
            render: () => {},
            updateCharacterCount: () => {},
            api: async () => {
                const error = new Error("Question changed");
                error.code = "PROPOSAL_STALE";
                throw error;
            },
            refreshSession: async () => {
                state.session.proposalFlowState = { active: true, status: "clarifying" };
            },
        },
    });
    state.pendingMessage = {
        content: "I prefer the second direction",
        baseVersionId: "v1",
        idempotencyKey: "old-question",
    };

    await app.submitPendingMessage();
    assert.equal(state.pendingMessage, null);
    assert.equal(elements.messageInput.value, "I prefer the second direction");
    assert.equal(state.chatError.code, "PROPOSAL_STALE");
});

test("current controls use canonical disagreement state while history retains its marker", () => {
    const { app, state } = loadApp();
    state.session.interactionState = { versionId: "v1", revision: 10, proposalBlocked: false };
    state.session.turns.push({
        role: "assistant",
        versionId: "history",
        guidance: { disagreement: { status: "active" } },
    });

    assert.equal(app.selectedStageHasActiveDisagreement(), false);
    assert.equal(state.session.turns[0].guidance.disagreement.status, "active");

    state.selectedVersionId = "history";
    assert.equal(app.selectedStageHasActiveDisagreement(), true);
});

test("automatic recovery schedules one stable key and pauses while sending or hidden", () => {
    const recoveryTimers = [];
    const cancelledRecoveryTimers = [];
    const { app, state, document } = loadApp({
        setTimeout: (callback, delay) => {
            recoveryTimers.push({ callback, delay });
            return recoveryTimers.length;
        },
        clearTimeout: id => cancelledRecoveryTimers.push(id),
    });

    state.session.interactionState = {
        versionId: "v1",
        revision: 1,
        recoveries: [{
            recoveryId: "r",
            baseVersionId: "v1",
            status: "pending",
            attemptsUsed: 0,
            nextAttemptKey: "r-1",
        }],
    };
    app.scheduleComponentRecovery();
    app.scheduleComponentRecovery();
    assert.equal(recoveryTimers.length, 1);
    assert.equal(recoveryTimers[0].delay, 1000);

    state.chatBusy = true;
    app.scheduleComponentRecovery();
    assert.equal(app.componentRecovery.timer, null);

    state.chatBusy = false;
    document.hidden = true;
    app.scheduleComponentRecovery();
    assert.equal(recoveryTimers.length, 1);

    document.hidden = false;
    state.session.interactionState.recoveries[0].status = "retry_pending";
    state.session.interactionState.recoveries[0].attemptsUsed = 1;
    state.session.interactionState.recoveries[0].nextAttemptKey = "r-2";
    app.scheduleComponentRecovery();
    assert.equal(recoveryTimers[1].delay, 3000);
});

test("recovery merges canonical state without altering composer, map draft, selection or retry", () => {
    const { app, state, elements } = loadApp({
        overrides: { render: () => {} },
    });
    state.session.sessionId = "session";
    state.session.turns[0].sequence = 2;
    state.session.interactionState = { versionId: "v1", revision: 10, proposalBlocked: true, recoveries: [] };
    state.draftRows = ["unsaved"];
    state.dirty = true;
    state.proposalMode = true;
    state.pendingMessage = { idempotencyKey: "original-key", content: "retry text" };
    elements.messageInput.value = "new text";

    const capturedRecovery = { sessionId: "session", selectedVersionId: "v1", versionId: "v1" };
    const recoveredSession = {
        ...state.session,
        interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] },
    };

    assert.equal(app.mergeRecoverySession(recoveredSession, capturedRecovery), true);
    assert.equal(app.selectedStageHasActiveDisagreement(), false);
    assert.equal(elements.messageInput.value, "new text");
    assert.equal(state.draftRows[0], "unsaved");
    assert.equal(state.dirty && state.proposalMode, true);
    assert.equal(state.pendingMessage.idempotencyKey, "original-key");
    assert.equal(state.selectedVersionId, "v1");
});

test("late recovery cannot overwrite newer turns, state or another selected Stage", () => {
    const { app, state } = loadApp({
        overrides: { render: () => {} },
    });
    state.session.sessionId = "session";
    state.session.turns[0].sequence = 5;
    state.session.interactionState = { versionId: "v1", revision: 12, proposalBlocked: true, recoveries: [] };

    const capturedRecovery = { sessionId: "session", selectedVersionId: "v1", versionId: "v1" };
    const oldRecovery = {
        ...state.session,
        interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] },
    };

    assert.equal(app.mergeRecoverySession(oldRecovery, capturedRecovery), false);

    oldRecovery.interactionState.revision = 13;
    oldRecovery.turns = [{ sequence: 4 }];
    assert.equal(app.mergeRecoverySession(oldRecovery, capturedRecovery), false);

    state.selectedVersionId = "history";
    oldRecovery.turns = [{ sequence: 6 }];
    assert.equal(app.mergeRecoverySession(oldRecovery, capturedRecovery), false);
    assert.equal(state.session.interactionState.proposalBlocked, true);
});

test("state replay uses separate POST and keeps chat usable without duplicate calls", async () => {
    const replayCalls = [];
    const { app, state, elements } = loadApp({
        overrides: {
            render: () => {},
            scheduleComponentRecovery: () => {},
            api: async (path, options) => {
                replayCalls.push({ path, options });
                return {
                    ...state.session,
                    interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] },
                };
            },
        },
    });
    state.session.sessionId = "session";
    state.session.interactionState = { versionId: "v1", revision: 10, recoveries: [] };
    state.draftRows = ["unsaved"];
    state.dirty = true;
    elements.messageInput.value = "typing";

    const replayTask = { recoveryId: "r", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-1" };
    await app.recoverComponent(replayTask);
    await app.recoverComponent(replayTask);

    assert.equal(replayCalls.length, 1);
    assert.equal(replayCalls[0].options.body.idempotencyKey, "r-1");
    assert.equal(replayCalls[0].options.timeoutMs, 12000);
    assert.match(replayCalls[0].path, /component-recoveries\/r\/retry$/);
    assert.equal(state.busy || state.chatBusy, false);
    assert.equal(elements.messageInput.value, "typing");
    assert.equal(state.draftRows[0], "unsaved");
});

test("claimed recovery refreshes without a model POST and old normal responses retain newer authority", async () => {
    const claimRefreshCalls = [];
    const { app, state } = loadApp({
        overrides: {
            render: () => {},
            scheduleComponentRecovery: () => {},
            api: async (path, options) => {
                claimRefreshCalls.push({ path, options });
                return state.session;
            },
        },
    });
    state.session.sessionId = "session";
    state.session.interactionState = { versionId: "v1", revision: 12, proposalBlocked: false, recoveries: [] };
    state.session.turns[0].proposalState = { actionable: true };

    const olderNormalResponse = {
        ...state.session,
        turns: [{ turnId: state.session.turns[0].turnId, proposalState: { actionable: false } }],
        interactionState: { versionId: "v1", revision: 11, proposalBlocked: true },
    };

    await app.recoverComponent({ recoveryId: "r", baseVersionId: "v1", status: "claimed" });
    assert.equal(claimRefreshCalls.length, 1);
    assert.equal(claimRefreshCalls[0].options.method, undefined);
    assert.equal(app.retainNewerInteractionState(olderNormalResponse).interactionState.proposalBlocked, false);
    assert.equal(app.retainNewerInteractionState(olderNormalResponse).turns[0].proposalState.actionable, true);
});

test("a reply arriving while hidden can replay its same key on return without losing the composer", async () => {
    const hiddenCalls = [];
    const { app, state, elements, document } = loadApp({
        overrides: {
            render: () => {},
            scheduleComponentRecovery: () => {},
            api: async (_path, options) => {
                hiddenCalls.push(options.body.idempotencyKey);
                if (hiddenCalls.length === 1) document.hidden = true;
                return {
                    ...state.session,
                    interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] },
                };
            },
        },
    });
    state.session.sessionId = "session";
    state.session.interactionState = { versionId: "v1", revision: 10, recoveries: [] };
    elements.messageInput.value = "typing";

    const hiddenTask = { recoveryId: "r", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-1" };
    await app.recoverComponent(hiddenTask);
    assert.equal(state.session.interactionState.revision, 10);

    document.hidden = false;
    await app.recoverComponent(hiddenTask);
    assert.equal(hiddenCalls.length, 2);
    assert.equal(hiddenCalls.every(key => key === "r-1"), true);
    assert.equal(state.session.interactionState.revision, 11);
    assert.equal(elements.messageInput.value, "typing");
});

test("choice buttons recover after a reply is rendered while busy", () => {
    const { app, state, created } = loadApp();
    state.busy = true;
    app.createDisagreementCard({
        subject: "ai_revision_challenge",
        phase: "choice_pending",
        challengeId: "challenge-1",
    });

    const buttons = created.filter(node => node.className.includes("challenge-choice-button"));
    assert.equal(buttons.length, 2);
    assert.ok(buttons.every(button => button.disabled && button.clickHandler));

    state.busy = false;
    app.updateControls();
    assert.ok(buttons.every(button => !button.disabled && button.getAttribute("aria-disabled") === null));

    buttons[0].click();
    assert.equal(state.pendingMessage.challengeChoice, "ai");
    assert.equal(state.pendingMessage.challengeId, "challenge-1");

    state.busy = true;
    app.updateControls();
    assert.ok(buttons.every(button => button.disabled));
});

test("old, resolved, historical, and expired challenge choices stay locked", () => {
    const { app, state, created } = loadApp();
    app.createDisagreementCard({
        subject: "ai_revision_challenge",
        phase: "choice_pending",
        challengeId: "older",
    });

    const buttons = created.filter(node => node.className.includes("challenge-choice-button"));
    assert.ok(buttons.every(button => button.disabled));
    app.sendChallengeChoice("older", "ai");
    assert.equal(state.pendingMessage, null);

    state.session.turns[0].guidance.challengeState.status = "resolved";
    app.updateControls();
    assert.ok(buttons.every(button => button.disabled));

    state.session.turns[0].guidance.challengeState.status = "choice_pending";
    state.selectedVersionId = "history";
    app.updateControls();
    assert.ok(buttons.every(button => button.disabled));

    state.selectedVersionId = "v1";
    state.session.deadlineExpired = true;
    app.updateControls();
    assert.ok(buttons.every(button => button.disabled));
});

test("map controls lock during a request and recover afterward", () => {
    const { app, state, document } = loadApp();
    const tile = document.createElement("button");
    tile.className = "tile";
    const palette = document.createElement("button");
    palette.className = "palette-button";
    const revision = document.createElement("button");
    revision.className = "revision-action-button";
    revision.dataset.actionable = "true";
    const staleRevision = document.createElement("button");
    staleRevision.className = "revision-action-button";
    staleRevision.dataset.actionable = "false";

    state.draftRows = ["............"];
    state.busy = true;
    app.updateControls();
    app.editTile(0, 0);
    assert.ok(tile.disabled && palette.disabled && revision.disabled && staleRevision.disabled);
    assert.equal(state.draftRows[0], "............");

    state.busy = false;
    app.updateControls();
    assert.ok(!tile.disabled && !palette.disabled);
    assert.ok(revision.disabled && staleRevision.disabled);

    state.session.turns[0].guidance.disagreement.status = "resolved";
    app.updateControls();
    assert.ok(!revision.disabled && staleRevision.disabled);
});

test("idempotent message requests keep retry on unreadable 5xx or network failure", async () => {
    let fetchImpl = async () => ({
        ok: false,
        status: 502,
        async json() { throw new SyntaxError("not JSON"); },
    });
    const { app } = loadApp({
        fetch: (...args) => fetchImpl(...args),
    });

    const request = () => app.api("/api/sessions/session/messages", {
        method: "POST",
        body: { idempotencyKey: "same-key" },
    });
    await assert.rejects(request, error =>
        error.code === "RETRYABLE_REQUEST_FAILED" && error.retryable === true);

    fetchImpl = async () => { throw new TypeError("network interrupted"); };
    await assert.rejects(request, error =>
        error.code === "RETRYABLE_REQUEST_FAILED" && error.retryable === true);
});

test("unreadable or incomplete 2xx is never an empty successful message", async () => {
    let fetchImpl = async () => ({ ok: true, status: 200, async json() { return null; } });
    const { app, state } = loadApp({
        fetch: (...args) => fetchImpl(...args),
    });
    state.pendingMessage = { content: "keep this", idempotencyKey: "same-key" };

    for (const payload of [null, {}, { sessionId: "session" }, [], { turns: [] }]) {
        fetchImpl = async () => ({ ok: true, status: 200, async json() { return payload; } });
        await assert.rejects(
            () => app.api("/api/sessions/session/messages", {
                method: "POST",
                body: { idempotencyKey: "same-key" },
            }),
            error => error.code === "INVALID_RESPONSE" && error.retryable,
        );
    }

    fetchImpl = async () => ({ ok: true, status: 200, async json() { throw new SyntaxError("truncated"); } });
    await assert.rejects(
        () => app.api("/api/sessions/session/messages", {
            method: "POST",
            body: { idempotencyKey: "same-key" },
        }),
        error => error.code === "INVALID_RESPONSE" && error.retryable,
    );
    assert.equal(state.pendingMessage.content, "keep this");
});

test("timeout remains active while reading response body", async () => {
    let abortTimer;
    let cleared = 0;
    const { app } = loadApp({
        setTimeout: callback => { abortTimer = callback; return 1; },
        clearTimeout: () => { cleared += 1; },
        fetch: async (_path, request) => ({
            ok: true,
            status: 200,
            json: () => new Promise((_resolve, reject) => request.signal.addEventListener("abort", () => {
                const error = new Error("body interrupted");
                error.name = "AbortError";
                reject(error);
            })),
        }),
    });

    const result = app.api("/api/sessions/session/messages", {
        method: "POST",
        body: { idempotencyKey: "same-key" },
        timeoutMs: 10,
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(cleared, 0);
    abortTimer();
    await assert.rejects(result, error => error.code === "CLIENT_TIMEOUT" && error.retryable);
    assert.equal(cleared, 1);
});

test("known endpoints validate their own response contracts", async () => {
    let currentPayload = null;
    const { app } = loadApp({
        fetch: async () => ({ ok: true, status: 200, async json() { return currentPayload; } }),
    });
    const session = { sessionId: "session", status: "active", currentVersionId: "v1", versions: [], turns: [] };
    const cases = [
        ["/api/sessions/session/messages", session],
        ["/api/sessions/session/questions/q/feedback", { outcome: "ignored", session }],
        ["/api/sessions/session/intent-hypotheses/i/feedback", { outcome: "compatible", session }],
        ["/api/sessions/session/versions/v1/play-attempts", { attemptId: "attempt", playUrl: "/play" }],
    ];
    for (const [path, payload] of cases) {
        currentPayload = payload;
        assert.ok(await app.api(path));
    }
});

test("manual save retry uses the captured rows, base Stage and idempotency key", async () => {
    const bodies = [];
    let retryOperation;
    const { app, state } = loadApp({
        overrides: {
            withBusy: async action => {
                retryOperation = action;
                try { await action(); } catch (_) { /* inspect retry */ }
            },
            api: async (_path, options) => {
                bodies.push(JSON.parse(JSON.stringify(options.body)));
                throw { code: "UPSTREAM_TIMEOUT", retryable: true };
            },
            renderMap: () => {},
            render: () => {},
        },
    });
    state.dirty = true;
    state.draftRows = ["............"];

    await app.saveManualStage();
    state.draftRows = ["############"];
    state.session.currentVersionId = "v2";
    await assert.rejects(() => retryOperation());
    assert.equal(bodies.length, 2);
    assert.deepEqual(bodies[0], bodies[1]);
    assert.equal(bodies[1].baseVersionId, "v1");
});

test("diagnostic error details retain the server request ID and phase", async () => {
    const { app } = loadApp({
        fetch: async () => ({
            ok: false,
            status: 502,
            headers: { get: name => name === "X-Request-ID" ? "diagnostic-id" : "application/json" },
            async json() {
                return {
                    code: "REQUIREMENT_INTERPRETATION_INVALID",
                    retryable: true,
                    details: { task: "revision_requirements", failureStage: "evidence" },
                };
            },
        }),
    });

    await assert.rejects(
        () => app.api("/api/sessions/session/messages", {
            method: "POST",
            body: { idempotencyKey: "same-key" },
        }),
        error => error.details.requestId === "diagnostic-id"
            && error.details.failureStage === "evidence"
            && error.details.httpStatus === 502,
    );
});

test("unreadable response recovers only the matching current request diagnostic", () => {
    const { app } = loadApp();
    const unreadable = Object.assign(new Error("generic"), {
        code: "RETRYABLE_REQUEST_FAILED",
        retryable: true,
        details: { httpStatus: 502 },
    });
    const pending = { idempotencyKey: "original-key", baseVersionId: "stage" };
    const latest = {
        lastMessageFailure: {
            messageKey: "original-key",
            baseVersionId: "stage",
            code: "REQUIREMENT_INTERPRETATION_INVALID",
            retryable: true,
            committed: false,
            requestId: "server-id",
            details: { failureStage: "review", failureKind: "truncated_json" },
        },
    };
    const recovered = app.recoverMessageFailure(unreadable, latest, pending);
    assert.deepEqual(
        [recovered.code, recovered.retryable, recovered.details.requestId, recovered.details.failureKind, recovered.details.httpStatus],
        ["REQUIREMENT_INTERPRETATION_INVALID", true, "server-id", "truncated_json", 502],
    );
    for (const changes of [{ messageKey: "other" }, { baseVersionId: "history" }, { committed: true }]) {
        assert.equal(
            app.recoverMessageFailure(unreadable, { lastMessageFailure: { ...latest.lastMessageFailure, ...changes } }, pending) === unreadable,
            true,
        );
    }
    assert.equal(app.recoverMessageFailure({ code: "INVALID_CARD_SOURCE" }, latest, pending).code, "INVALID_CARD_SOURCE");
});

test("classification diagnostics show actual attempts without claiming three body attempts", () => {
    const { app, state } = loadApp();
    for (const language of ["en", "zh-CN"]) {
        state.language = language;
        const message = app.technicalErrorMessage({
            code: "MODEL_RESPONSE_INVALID",
            details: { task: "turn_understanding", attemptsUsed: 2, maximumAttempts: 2 },
        });
        assert.match(message, /2\/2/);
        assert.doesNotMatch(message, /three|三次/);
    }
});

test("review diagnostics separate component repair from generation and do not invent missing counts", () => {
    const { app, state } = loadApp();
    for (const language of ["en", "zh-CN"]) {
        state.language = language;
        const message = app.technicalErrorMessage({
            code: "MODEL_RESPONSE_INVALID",
            details: {
                failureStage: "clarification_review",
                attemptsUsed: 1,
                maximumAttempts: 2,
                repairAttemptsUsed: 1,
                maximumRepairAttempts: 1,
            },
        });
        assert.match(message, /1\/2/);
        assert.match(message, /1\/1/);
        assert.match(message, language === "en" ? /clarification review/ : /澄清的复核/);
        assert.doesNotMatch(message, /2\/3/);

        const unknown = app.technicalErrorMessage({ code: "MODEL_RESPONSE_INVALID", details: { attemptsUsed: 2 } });
        assert.doesNotMatch(unknown, /\(\d+\/\d+\)/);

        const invalid = app.technicalErrorMessage({
            code: "MODEL_RESPONSE_INVALID",
            details: { attemptsUsed: 3, maximumAttempts: 2 },
        });
        assert.doesNotMatch(invalid, /3\/2/);
    }
});

test("message refresh recovers a diagnostic without changing retry input, then clears it on success", async () => {
    for (const committed of [false, true]) {
        const apiCalls = [];
        const { app, state, elements } = loadApp({
            keepSubmit: true,
            overrides: {
                startChatTimer: () => {},
                stopChatTimer: () => {},
                hideNotice: () => {},
                renderChatRequestStatus: () => {},
                updateControls: () => {},
                render: () => {},
                updateCharacterCount: () => {},
                api: async (path, options = {}) => {
                    apiCalls.push({ path, body: options.body && JSON.stringify(options.body) });
                    if (apiCalls.length === 1) {
                        const error = new Error("Unreadable HTTP error");
                        error.code = "RETRYABLE_REQUEST_FAILED";
                        error.retryable = true;
                        throw error;
                    }
                    const success = apiCalls.length > 2 || committed;
                    return {
                        ...state.session,
                        turns: success ? [{ role: "assistant", versionId: "v1", requestId: "same-key" }] : [],
                        lastMessageFailure: success ? null : {
                            messageKey: "same-key",
                            baseVersionId: "v1",
                            code: "REQUIREMENT_INTERPRETATION_INVALID",
                            retryable: true,
                            committed: false,
                            requestId: "server-request",
                            details: { failureStage: "review", failureKind: "truncated_json" },
                        },
                    };
                },
            },
        });

        state.pendingMessage = {
            content: "否",
            baseVersionId: "v1",
            idempotencyKey: "same-key",
            requestProposal: true,
            action: "continue_challenge",
            challengeId: "challenge-1",
            challengeChoice: "user",
        };
        elements.messageInput.value = "否";
        const capturedRetry = JSON.stringify(state.pendingMessage);

        await app.submitPendingMessage();
        if (!committed) {
            assert.equal(state.chatError.code, "REQUIREMENT_INTERPRETATION_INVALID");
            assert.equal(state.chatError.details.failureKind, "truncated_json");
            assert.equal(JSON.stringify(state.pendingMessage), capturedRetry);
            assert.equal(elements.messageInput.value, "否");
            await app.retryPendingMessage();
            assert.equal(apiCalls[2].body, capturedRetry);
        }
        assert.equal(state.pendingMessage, null);
        assert.equal(state.chatError, null);
        assert.equal(state.chatStatus, "idle");
        assert.equal(state.busy, false);
    }
});

test("reply diagnostics are bounded, survive storage, and never include prose", () => {
    let storageBroken = false;
    const baseStorage = createStorage();
    const sessionStorage = {
        getItem(key) { return baseStorage.getItem(key); },
        setItem(key, value) {
            if (storageBroken) throw new Error("storage unavailable");
            baseStorage.setItem(key, value);
        },
        removeItem(key) { baseStorage.removeItem(key); },
    };
    const { app } = loadApp({ sessionStorage });

    for (let i = 0; i < 205; i += 1) {
        app.recordReplyDiagnostic("render", "completed", {
            messageKey: `m-${i}`,
            renderedCard: true,
            content: "private chat",
            prompt: "private prompt",
            token: "secret",
        });
    }

    const storedDiagnostics = JSON.parse(sessionStorage.getItem(REPLY_DIAGNOSTICS_KEY));
    assert.equal(storedDiagnostics.length, 200);
    assert.equal(storedDiagnostics[0].messageKey, "m-5");
    assert.equal(JSON.stringify(storedDiagnostics).includes("private"), false);
    assert.equal(storedDiagnostics[199].frontendVersion, REPLY_PIPELINE_VERSION);

    storageBroken = true;
    app.recordReplyDiagnostic("render", "completed");
});

test("reply rendering distinguishes card family filtering and renderer failures", () => {
    let shouldFailBodyRender = false;
    const { app, document } = loadApp({
        overrides: {
            localizedAssistantTurn: turn => turn,
            proposalForTurn: () => null,
            renderAssistantBody: () => {
                if (shouldFailBodyRender) throw new Error("private rendering error");
            },
            assistantBodyWithoutCues: body => body,
        },
    });
    const turnWithCard = {
        turnId: "a1",
        requestId: "m1",
        versionId: "v1",
        content: "private body",
        guidance: {
            intentHypothesis: "private card",
            uiCues: [{ type: "manual_edit", text: "manual guidance" }],
        },
    };
    const diagnosticBubble = document.createElement("div");

    app.renderAssistantBubble(turnWithCard, diagnosticBubble);
    const diagnosticsAfterPass = app.getReplyDiagnostics();
    assert.equal(diagnosticsAfterPass.find(item => item.stage === "display_policy").code, "MANUAL_CARD_FAMILY");
    assert.equal(diagnosticsAfterPass.find(item => item.stage === "render").renderedCard, false);

    shouldFailBodyRender = true;
    assert.throws(() => app.renderAssistantBubble(turnWithCard, diagnosticBubble));
    const diagnosticsAfterFail = app.getReplyDiagnostics();
    assert.equal(diagnosticsAfterFail.at(-1).code, "RENDER_EXCEPTION");
    assert.equal(JSON.stringify(diagnosticsAfterFail).includes("private"), false);
});

test("message diagnostics retain HTTP identities and distinguish unreadable success", async () => {
    const { app } = loadApp({
        fetch: async () => ({
            ok: true,
            status: 200,
            headers: { get: name => name === "X-Request-ID" ? "http-1" : REPLY_PIPELINE_VERSION },
            json: async () => { throw new Error("private invalid body"); },
        }),
    });

    await assert.rejects(
        app.api("/api/sessions/session/messages", {
            method: "POST",
            body: { idempotencyKey: "m1", baseVersionId: "v1", content: "private input" },
        }),
    );
    const diagnostics = app.getReplyDiagnostics();
    assert.equal(diagnostics.at(-1).stage, "response_body");
    assert.equal(diagnostics.at(-1).code, "INVALID_RESPONSE");
    assert.equal(diagnostics.at(-1).requestId, "http-1");
    assert.equal(JSON.stringify(diagnostics).includes("private"), false);
});

test("friendly buttons preserve the original request wording in both languages", () => {
    const { app, state, elements } = loadApp({
        overrides: {
            canEditSelected: () => true,
            isLatestRevisionOfferTurn: () => true,
            handleComposerInput: () => {},
        },
    });
    const expected = {
        en: ["Execute the bound proposal.", "Challenge this plan", "Regenerate proposal"],
        "zh-CN": ["执行已绑定的方案。", "质疑这个方案", "重新生成方案"],
    };
    state.session.turns = [];

    for (const language of ["en", "zh-CN"]) {
        state.language = language;
        for (const [index, action] of ["execute_revision", "challenge_revision", "alternative_revision"].entries()) {
            app.sendRevisionCardAction(action, { turnId: "original-card" }, {});
            assert.equal(state.pendingMessage.content, expected[language][index]);
            assert.equal(state.pendingMessage.action, action);
            assert.equal(state.pendingMessage.sourceTurnId, "original-card");
            assert.equal(state.pendingMessage.baseVersionId, "v1");
            assert.match(state.pendingMessage.idempotencyKey, /^card-/);
        }
        app.prefillProposalConsent({ summary: "KEEP THIS WORDING" });
        assert.equal(
            elements.messageInput.value,
            language === "en"
                ? "Please create a reviewable map proposal for this direction: KEEP THIS WORDING"
                : "请根据这个方向生成一份可供审查的地图提案：KEEP THIS WORDING",
        );
    }
});

test("main errors stay readable while safe diagnostics retain attempt counts", () => {
    const { app, state } = loadApp();
    for (const language of ["en", "zh-CN"]) {
        state.language = language;
        const failure = {
            code: "MODEL_RESPONSE_INVALID",
            retryable: true,
            message: "do not expose raw output or credentials",
            details: { attemptsUsed: 2, maximumAttempts: 2, rawOutput: "SECRET" },
        };
        const localized = app.localizedErrorMessage(failure);
        const markup = app.technicalDetailsMarkup(failure);
        assert.doesNotMatch(localized, /Kimi|LLM|2\/2|SECRET|credentials/);
        assert.match(markup, /<details/);
        assert.doesNotMatch(markup, /\bopen\b|SECRET|credentials/);
        assert.match(markup, /MODEL_RESPONSE_INVALID/);
        assert.match(markup, /2\/2/);
    }
});

test("waiting text follows known request type instead of elapsed-time guesses", () => {
    const { app, state } = loadApp();
    state.session.turns = [];
    state.chatStartedAt = Date.now() - 95000;
    state.pendingMessage = { content: "ordinary question", requestProposal: false };
    const ordinary = app.chatWaitingMessage();
    assert.doesNotMatch(ordinary, /方案|120|模型|服务器/);

    state.pendingMessage.requestProposal = true;
    assert.match(app.chatWaitingMessage(), /修改方案/);
});

test("reply waiting and elapsed time replace the composer hint and clear on success", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    state.busy = true;
    state.chatBusy = true;
    state.chatBusyVersionId = "v1";
    state.chatStatus = "waiting";
    state.chatStartedAt = Date.now() - 16000;
    elements.messageInput.value = "Original input";
    state.pendingMessage = { content: "Original input", baseVersionId: "v1", idempotencyKey: "same-key" };

    app.updateControls();
    app.renderChatRequestStatus();
    assert.match(elements.chatControlHint.textContent, /回复.*已等待 16 秒/);
    assert.equal(elements.replyFeedbackIcon.dataset.state, "thinking");
    assert.equal(elements.chatRequestStatus.hidden, false);
    assert.doesNotMatch(elements.chatControlHint.textContent, /正在处理刚才/);

    state.chatStartedAt = Date.now() - 22000;
    app.renderChatRequestStatus();
    assert.match(elements.chatControlHint.textContent, /22 秒/);

    state.busy = false;
    state.chatBusy = false;
    state.chatStatus = "idle";
    state.pendingMessage = null;
    app.updateControls();
    assert.equal(elements.chatRequestStatus.hidden, true);
    assert.equal(elements.messageInput.value, "Original input");
});

test("footer errors retain technical details and retry the same captured request", () => {
    let retriedKey = null;
    const { app, state, elements } = loadApp({
        overrides: {
            retryPendingMessage: () => { retriedKey = state.pendingMessage.idempotencyKey; },
        },
    });
    state.session.turns = [];
    state.chatStatus = "error";
    state.pendingMessage = {
        content: "Keep this",
        baseVersionId: "v1",
        idempotencyKey: "original-key",
        requestProposal: true,
    };
    state.chatError = { code: "CLIENT_TIMEOUT", retryable: true };

    app.renderChatRequestStatus();
    assert.equal(elements.replyFeedbackIcon.dataset.state, "error");
    assert.equal(elements.chatRetryButton.hidden, false);
    assert.equal(elements.chatTechnicalDetails.hidden, false);

    elements.chatRetryButton.click();
    assert.equal(retriedKey, "original-key");
    assert.equal(state.pendingMessage.requestProposal, true);

    state.selectedVersionId = "history";
    app.renderChatRequestStatus();
    assert.equal(elements.chatRetryButton.hidden, true);
    assert.equal(elements.replyFeedbackIcon.dataset.state, "info");
});

test("opening, translation and translation failure share the lower status area", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    state.assessing.add("v1");
    app.renderControlHints();
    assert.match(elements.chatControlHint.textContent, /AI.*地图/);
    assert.equal(elements.replyFeedbackIcon.dataset.state, "thinking");

    state.assessing.clear();
    state.translationInProgress = true;
    state.translationRemainingCount = 1;
    state.session.turns = [{ turnId: "t1", role: "assistant", versionId: "v1", language: "en", content: "Source" }];
    app.renderTranslationStatus();
    assert.match(elements.chatControlHint.textContent, /翻译.*1/);
    assert.equal(elements.replyFeedbackIcon.dataset.state, "translation");

    state.translationInProgress = false;
    state.translationFailures.add("t1:zh-CN");
    app.renderTranslationStatus();
    assert.equal(elements.replyFeedbackIcon.dataset.state, "error");
    assert.equal(elements.chatControlHint.textContent, app.t("translationUnavailable"));

    state.session.turns[0].translations = { "zh-CN": { content: "Translated" } };
    app.renderTranslationStatus();
    assert.equal(elements.chatRequestStatus.hidden, true);
});

test("workspace errors appear below the composer and keep their original retry callback", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    elements.workspace.hidden = false;
    let retryCount = 0;

    app.showError({ code: "MODEL_RESPONSE_INVALID", retryable: true }, () => { retryCount += 1; });
    assert.equal(elements.chatRequestStatus.hidden, false);
    assert.equal(elements.replyFeedbackIcon.dataset.state, "error");
    assert.equal(state.retryAction, null);

    elements.chatRetryButton.click();
    assert.equal(retryCount, 1);
    assert.equal(state.replyFeedback, null);
});

test("completion copy follows session type without changing actions", () => {
    const { app, state, elements } = loadApp();
    for (const demoMode of [true, false]) {
        state.session.status = "completed";
        state.session.demoMode = demoMode;
        app.renderSessionState();
        assert.equal(elements.returnUnityButton.hidden, demoMode);
        if (demoMode) assert.doesNotMatch(elements.sessionCompleteMessage.textContent, /Unity|Challenge|游戏页面/);
        else assert.match(elements.sessionCompleteMessage.textContent, /原游戏页面/);
    }
});

test("UI label updates do not rewrite stored turns", () => {
    const { app, state } = loadApp();
    const savedTurns = JSON.stringify(state.session.turns);
    for (const language of ["en", "zh-CN"]) {
        state.language = language;
        app.createGuidanceCue("revision", "UNCHANGED BODY", "UNCHANGED DETAIL");
    }
    assert.equal(JSON.stringify(state.session.turns), savedTurns);
});

test("idle guidance stays quiet and blocked play explains itself beside the Play button", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    app.updateControls();
    assert.equal(elements.mapControlHint.hidden, true);
    assert.equal(elements.chatControlHint.hidden, true);
    assert.equal(elements.playControlHint.hidden, true);

    state.dirty = true;
    app.updateControls();
    assert.equal(elements.playButton.disabled, true);
    assert.match(elements.playControlHint.textContent, /保存或撤销/);
    assert.equal(elements.mapControlHint.hidden, true);

    state.selectedVersionId = "history";
    app.updateControls();
    assert.match(elements.chatControlHint.textContent, /当前 Stage/);
});

test("proposal mode captions explain the next message without submitting or changing a turn", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    const beforeCaptionTurns = JSON.stringify(state.session.turns);
    app.toggleProposalMode();
    assert.equal(state.proposalMode, true);
    assert.match(elements.proposalModeCaption.textContent, /下一条/);
    assert.equal(state.pendingMessage, null);
    assert.equal(JSON.stringify(state.session.turns), beforeCaptionTurns);
});

test("tile and tool help describe the displayed tile without modifying map or request state", () => {
    const { app, state, document } = loadApp();
    const helpTile = document.createElement("button");
    state.session.turns = [];
    state.draftRows = ["KEEP MAP"];
    state.selectedTile = "#";
    const beforeHover = JSON.stringify([state.draftRows, state.session.turns, state.pendingMessage]);

    app.markTileHelp(helpTile, "s", 3, 2, "B1", "map");
    const zhTile = app.uiHelpFor(helpTile);
    helpTile.dataset.uiHelpKind = "preview";
    const previewTile = app.uiHelpFor(helpTile);
    helpTile.dataset.uiHelpKind = "palette";
    const tool = app.uiHelpFor(helpTile);
    state.language = "en";
    const enTool = app.uiHelpFor(helpTile);

    assert.match(zhTile.title, /B1/);
    assert.match(zhTile.body, /第 3 行，第 4 列/);
    assert.match(zhTile.body, /墙/);
    assert.match(previewTile.body, /只供查看/);
    assert.doesNotMatch(tool.body, /第 3 行/);
    assert.match(tool.body, /选中这个工具/);
    assert.match(enTool.body, /Select this tool/);
    assert.equal(JSON.stringify([state.draftRows, state.session.turns, state.pendingMessage]), beforeHover);
});

test("hover help on disabled actions retains the existing reason and button state", () => {
    const { app, state, elements, document } = loadApp();
    state.session.turns = [];
    app.updateControls();

    const saveHover = document.createElement("button");
    saveHover.id = "saveStageButton";
    saveHover.disabled = true;
    const saveExplanation = app.uiHelpFor(saveHover);

    state.dirty = true;
    app.updateControls();
    const playHover = document.createElement("button");
    playHover.id = "playButton";
    playHover.disabled = true;
    const playExplanation = app.uiHelpFor(playHover);

    assert.match(saveExplanation.status, /没有新改动/);
    assert.match(playExplanation.status, /保存或撤销/);
    assert.equal(elements.playButton.disabled, true);
    assert.equal(state.pendingMessage, null);
});

test("binary questions render separated text and deterministic short buttons", () => {
    let selectedBinaryAction = null;
    const { app, state, document, created } = loadApp({
        overrides: {
            submitBinaryAction: async action => { selectedBinaryAction = action; },
        },
    });
    state.language = "zh-CN";
    state.session.proposalFlowState = {
        active: true,
        interactionMode: "binary_choice",
        status: "clarifying",
        pendingQuestion: { questionId: "q1" },
    };
    const q = {
        questionId: "q1",
        topicId: "topic",
        baseVersionId: "v1",
        question: "先松开哪边？",
        note: "另一边保持。",
        status: "pending",
        sourceLanguage: "zh-CN",
        choices: [
            { choiceId: "a", letter: "A", label: "左边松口气！" },
            { choiceId: "b", letter: "B", label: "右边宽一点！" },
        ],
    };
    app.renderBinaryQuestion(q, document.createElement("div"), { language: "en" });

    const buttons = created.filter(n => n.className.includes("binary-choice-button"));
    assert.equal(buttons.length, 2);
    assert.equal(buttons[0].textContent, "A: 左边松口气！");
    buttons[0].click();
    assert.equal(selectedBinaryAction.choiceId, "a");
    assert.equal(selectedBinaryAction.displayLanguage, "zh-CN");
    assert.equal("label" in selectedBinaryAction, false);
});

test("binary controls block free text, dirty/stale choices and cancel during generation", async () => {
    const { app, state, elements } = loadApp();
    state.session.proposalFlowState = {
        active: true,
        interactionMode: "binary_choice",
        status: "clarifying",
        pendingQuestion: { questionId: "q1" },
    };
    elements.messageInput.value = "This must not send";
    app.updateControls();

    assert.equal(elements.messageInput.disabled && elements.sendButton.disabled, true);
    assert.equal(app.canChooseBinary("q1"), true);
    assert.equal(app.canChooseBinary("old"), false);

    state.dirty = true;
    assert.equal(app.canChooseBinary("q1"), false);

    state.dirty = false;
    state.busy = true;
    assert.equal(app.canChooseBinary("q1"), false);

    await app.sendMessage({ preventDefault() {} });
    assert.equal(state.pendingMessage, null);
});

test("compact binary purple card keeps coordinates and history in details", () => {
    const { app } = loadApp();
    const node = app.createProposalPresentationV2({
        schemaVersion: 2,
        binaryCompact: true,
        verifiedActions: ["Remove left water"],
        requestText: "Make it simpler",
        answerTexts: ["A: left"],
        reason: "Open this area",
        changes: [{ row: 3, column: 3, before: "@", after: "." }],
        solution: { minimumMoves: 4, pushesOnMinimumMoveSolution: 2 },
    });
    assert.equal(node.children.some(n => n.tagName === "details"), true);
    assert.equal(node.children.filter(n => n.tagName === "p").some(n => n.textContent.includes("(3, 3)")), false);
});

test("phase5 fixes preserve reject draft, v2 offers, pending binary guard, translation retry cap, and deadline cleanup", async () => {
    let clickedResolved = false;
    let assessedOnAccept = false;
    let clearedTimers = 0;
    let requestedHostView = "";
    let scheduledRetries = 0;
    let apiImpl = async () => ({});

    const { app, state, elements, document, created } = loadApp({
        performance: { now: () => 0 },
        setTimeout: () => { scheduledRetries += 1; return scheduledRetries; },
        clearTimeout: () => { clearedTimers += 1; },
        clearInterval: () => {},
        overrides: {
            render: () => {},
            applyTranslations: () => {},
            submitBinaryAction: async () => { clickedResolved = true; },
            ensureAssessment: async () => { assessedOnAccept = true; },
            requestEmbeddedHostView: type => { requestedHostView = type; },
            api: (...args) => apiImpl(...args),
        },
    });

    // 1. v2 proposalPresentation counts as revision offer turn even without offer.summary
    assert.equal(
        app.isRevisionOfferTurn({
            role: "assistant",
            guidance: { proposalOffer: { proposalPresentation: { schemaVersion: 2 } } },
        }),
        true,
    );

    // 2. Binary button status guard prevents click and updateControls keeps non-pending disabled
    state.session.proposalFlowState = {
        active: true,
        interactionMode: "binary_choice",
        status: "clarifying",
        pendingQuestion: { questionId: "q2" },
    };
    const q = {
        questionId: "q2",
        topicId: "topic",
        baseVersionId: "v1",
        question: "Pick one",
        status: "resolved",
        sourceLanguage: "en",
        choices: [{ choiceId: "c1", letter: "A", label: "Left" }],
    };
    app.renderBinaryQuestion(q, document.createElement("div"), { language: "en" });
    const resolvedBtn = document.querySelectorAll(".binary-choice-button").find(b => b.dataset.questionId === "q2");
    resolvedBtn.click();
    app.updateControls();
    assert.equal(resolvedBtn.dataset.questionStatus, "resolved");
    assert.equal(clickedResolved, false);
    assert.equal(resolvedBtn.disabled, true);

    // 3. decideProposal reject preserves same-stage dirty draft & activeCoordinateLink
    state.selectedVersionId = "v1";
    state.dirty = true;
    state.draftRows = ["#####", "#@$.#", "#####"];
    state.activeCoordinateLink = { row: 2, column: 2 };
    state.session.versions = [{ versionId: "v1", stageNumber: 1, rows: ["#####", "#@$.#", "#####"] }];
    apiImpl = async () => ({
        ...state.session,
        currentVersionId: "v1",
        versions: state.session.versions,
    });
    await app.decideProposal({ proposalId: "p1" }, "reject");
    assert.equal(state.dirty, true);
    assert.equal(Boolean(state.activeCoordinateLink), true);
    assert.equal(state.draftRows[1], "#@$.#");

    await app.decideProposal({ proposalId: "p1" }, "accept");
    assert.equal(state.dirty, false);
    assert.equal(assessedOnAccept, true);

    // 4. finalizeSession omits rows when deadlineExpired && !state.dirty
    let finalizePayload = null;
    apiImpl = async (_url, opts) => {
        finalizePayload = opts.body;
        return state.session;
    };
    state.dirty = false;
    state.session.deadlineExpired = true;
    state.draftRows = ["###"];
    await app.finalizeSession();
    assert.equal(finalizePayload.rows, null);

    // 5. renderDeadline clears assessmentRetryTimers & unityPlayActive on expiration transition,
    // and finalizeButton remains unlocked after deadline even while assessing is active
    state.assessmentRetryTimers.set("v1", 99);
    state.assessing.add("v1");
    state.unityPlayActive = true;
    state.deadlinePresentationState = "normal";
    state.session.deadlineAt = "2026-11-18T12:00:00Z";
    state.session.remainingSeconds = 0;
    state.session.deadlineActive = true;
    state.session.deadlineExpired = true;
    state.session.status = "active";
    app.renderDeadline();
    assert.equal(clearedTimers, 1);
    assert.equal(state.assessmentRetryTimers.size, 0);
    assert.equal(state.unityPlayActive, false);
    assert.equal(requestedHostView, "sokoban:cocreation-show-lab");
    assert.equal(elements.finalizeButton.disabled, false);
    state.assessing.clear();

    // 6. submitIntentFeedback reuses the identical idempotencyKey and baseVersionId across retries
    state.session.deadlineExpired = false;
    const baseIntentSession = state.session;
    const intentKeys = [];
    let intentAttempts = 0;
    apiImpl = async (_url, opts) => {
        if (!opts || opts.method !== "POST") {
            return baseIntentSession;
        }
        intentKeys.push(opts?.body?.idempotencyKey);
        intentAttempts += 1;
        if (intentAttempts === 1) {
            const err = new Error("temporary");
            err.retryable = true;
            throw err;
        }
        return { outcome: "recorded", session: baseIntentSession };
    };
    const cueNode = document.createElement("div");
    await app.submitIntentFeedback("confirm", { turnId: "t-intent" }, { hypothesisId: "h1", actionable: true }, "", cueNode);
    const intentRetry = state.replyFeedback?.retryAction || state.retryAction;
    if (typeof intentRetry === "function") {
        await intentRetry();
    }
    assert.equal(intentKeys.length, 2);
    assert.equal(intentKeys[0], intentKeys[1]);

    // 7. translateVisibleBatch caps retries at 3
    scheduledRetries = 0;
    apiImpl = async () => { throw new Error("translation down"); };
    const batchTurn = [{ turnId: "t1", role: "assistant", language: "en" }];
    await app.translateVisibleBatch(batchTurn, "zh-CN");
    await app.translateVisibleBatch(batchTurn, "zh-CN");
    state.translationRetryTimerId = null;
    await app.translateVisibleBatch(batchTurn, "zh-CN");
    assert.equal(scheduledRetries, 1);
    assert.equal(state.translationRetryAttempts.get("t1:zh-CN"), 3);
    assert.ok(created.length > 0);
});

test("multiple createWorkbenchApp instances remain isolated without touching globals", () => {
    const first = loadApp();
    const second = loadApp();
    first.state.language = "en";
    first.state.dirty = true;
    first.app.recordReplyDiagnostic("render", "completed", { messageKey: "m-first" });

    assert.equal(second.state.language, "zh-CN");
    assert.equal(second.state.dirty, false);
    assert.equal(second.app.getReplyDiagnostics().length, 0);
    assert.equal(first.app.getReplyDiagnostics().length, 1);
    assert.equal(typeof globalThis.window, "undefined");
    assert.equal(typeof globalThis.document, "undefined");
});

test("binary retry hides stale failure while in flight and distinguishes saved request vs choices", () => {
    const { app, state, elements } = loadApp();
    state.session.turns = [];
    state.session.proposalFlowState = {
        active: true,
        interactionMode: "binary_choice",
        status: "retry_pending",
        retryable: true,
        clarificationQuestionCount: 0,
        retryAction: {
            kind: "start",
            baseVersionId: "v1",
            content: "Make it simpler",
            requestProposal: true,
            idempotencyKey: "k1",
        },
    };
    state.chatStatus = "error";
    state.chatError = { code: "BINARY_SEMANTIC_REJECTED", retryable: true };

    app.renderChatRequestStatus();
    app.updateControls();
    assert.match(app.selectedReplyFailure().message, /已保存你的请求/);
    assert.equal(elements.chatRetryButton.hidden, false);
    assert.equal(elements.chatRetryButton.disabled, false);

    state.busy = true;
    state.chatBusy = true;
    state.chatStatus = "waiting";
    state.chatBusyVersionId = "v1";
    app.renderChatRequestStatus();
    app.updateControls();
    assert.equal(app.selectedReplyFailure(), null);
    assert.match(elements.chatControlHint.textContent, /正在准备修改方案/);
    assert.equal(elements.replyFeedbackIcon.dataset.state, "thinking");
    assert.equal(elements.chatRetryButton.hidden, true);
    assert.equal(elements.chatRetryButton.disabled, true);

    state.busy = false;
    state.chatBusy = false;
    state.chatStatus = "error";
    state.session.proposalFlowState.clarificationQuestionCount = 1;
    app.renderChatRequestStatus();
    app.updateControls();
    assert.match(app.selectedReplyFailure().message, /已保存你的选择/);
    assert.equal(elements.chatRetryButton.hidden, false);
    assert.equal(elements.chatRetryButton.disabled, false);
});

test("manual_edit and clarification cards match the unified PREFIX · subtitle format in both languages", () => {
    const { app, state } = loadApp();
    state.language = "en";
    assert.equal(
        app.createGuidanceCue("manual_edit", "Try editing around row 4.").children[0].textContent,
        "MANUAL EDIT · Try tweaking it yourself",
    );
    assert.equal(
        app.createGuidanceCue("clarification", "Clarify area?").children[0].textContent,
        "NEEDS CLARIFICATION · Tell me a bit more",
    );

    state.language = "zh-CN";
    assert.equal(
        app.createGuidanceCue("manual_edit", "试试调整第 4 行周边区域。").children[0].textContent,
        "MANUAL EDIT · 也可以自己动手改改",
    );
    assert.equal(
        app.createGuidanceCue("clarification", "请先明确方向").children[0].textContent,
        "NEEDS CLARIFICATION · 这里再明确一下",
    );
});

function makeValidRows(fill = ".") {
    return Array.from({ length: 10 }, (_, y) =>
        Array.from({ length: 12 }, (_, x) => (y === 0 || y === 9 || x === 0 || x === 11 ? "#" : fill)).join(""),
    );
}

function makeRichVersion(versionId = "v1", stageNumber = 1, rows = makeValidRows()) {
    return {
        versionId,
        stageNumber,
        parentVersionId: null,
        source: "initial",
        rows,
        summary: "Stage",
        diff: [],
        validation: { solutionSteps: 3, solutionPushes: 1, searchedStates: 12 },
        playAttempts: [],
    };
}

test("P0-1: discardDraftButton stays enabled after deadline and finalizeSession surfaces level validation errors", async () => {
    const baseRows = makeValidRows();
    const { app, state, elements } = loadApp({
        overrides: {
            scheduleComponentRecovery: () => {},
            ensureVisibleTranslations: async () => {},
            api: async () => {
                const err = new Error("Unsolvable");
                err.code = "UNSOLVABLE_LEVEL";
                err.retryable = false;
                err.details = { searchedStates: 42 };
                throw err;
            },
        },
    });
    state.session.versions = [makeRichVersion("v1", 1, baseRows)];
    state.session.deadlineExpired = true;
    state.dirty = true;
    state.draftRows = makeValidRows("@");

    app.updateControls();
    assert.equal(elements.saveStageButton.disabled, true);
    assert.equal(elements.discardDraftButton.disabled, false);
    assert.equal(elements.finalizeButton.disabled, false);

    await app.finalizeSession();
    assert.equal(state.validationError?.code, "UNSOLVABLE_LEVEL");
    assert.match(elements.validationCard.className, /\binvalid\b/);
    assert.match(elements.validationCard.innerHTML, /42/);

    app.discardDraft();
    assert.equal(state.dirty, false);
    assert.equal(state.validationError, null);
    assert.equal(elements.discardDraftButton.disabled, true);
    assert.equal(elements.finalizeButton.disabled, false);
});

test("P0-2: withBusyImpl handles PATCH /language retryable failures without half-initializing workspace", async () => {
    const landingSession = {
        ...createDefaultSession(),
        demoMode: true,
        languageLocked: false,
        language: "zh-CN",
        versions: [makeRichVersion("v1", 1)],
        turns: [],
        draftPreview: { rows: makeValidRows(), generation: 1, mutable: true },
    };
    const apiErrors = { patchShouldFail: true, getLocked: false };
    let assessedVersion = "";
    let replacedUrl = "";
    const { app, state, elements, document } = loadApp({
        history: { replaceState: (_s, _t, url) => { replacedUrl = url; } },
        overrides: {
            applyTranslations: () => {},
            ensureVisibleTranslations: async () => {},
            ensureAssessment: async versionId => { assessedVersion = versionId; },
            api: async (path, options = {}) => {
                if (options.method === "PATCH" && apiErrors.patchShouldFail) {
                    const err = new Error("upstream down");
                    err.code = "UPSTREAM_SERVER_ERROR";
                    err.retryable = true;
                    throw err;
                }
                return {
                    ...landingSession,
                    languageLocked: apiErrors.getLocked,
                    language: apiErrors.getLocked ? "en" : "zh-CN",
                };
            },
        },
    });
    elements.emptyChat.append(document.createElement("h3"), document.createElement("p"));
    state.session = { ...landingSession };
    state.selectedVersionId = "";
    state.draftRows = [];
    state.language = "en";
    elements.workspace.hidden = true;
    elements.landing.hidden = false;

    await app.confirmSessionLanguage();
    assert.equal(elements.workspace.hidden, true);
    assert.equal(elements.landing.hidden, false);
    assert.equal(state.language, "en");
    assert.equal(elements.retryButton.hidden, false);

    apiErrors.getLocked = true;
    await app.confirmSessionLanguage();
    assert.equal(elements.workspace.hidden, false);
    assert.equal(state.selectedVersionId, "v1");
    assert.equal(state.draftRows.length, 10);
    assert.match(replacedUrl, /stage=v1/);
    assert.equal(assessedVersion, "v1");
});

test("P0-3: finalizeSession and submitIntention reuse stable idempotency keys until success or payload change", async () => {
    const sent = [];
    let shouldFail = true;
    let idSeq = 0;
    const { app, state, elements } = loadApp({
        overrides: {
            uniqueId: prefix => `${prefix}-${++idSeq}`,
            render: () => {},
            renderMap: () => {},
            ensureVisibleTranslations: async () => {},
            api: async (path, options = {}) => {
                if (options.method === "POST") {
                    sent.push({ path, body: options.body });
                }
                if (shouldFail && options.method === "POST") {
                    const err = new Error("timeout");
                    err.code = "CLIENT_TIMEOUT";
                    err.retryable = true;
                    throw err;
                }
                return { ...state.session };
            },
        },
    });
    state.session.deadlineExpired = true;
    state.dirty = true;
    state.draftRows = ["###"];

    await app.finalizeSession();
    await app.finalizeSession();
    assert.equal(sent.length, 2);
    assert.equal(sent[0].body.idempotencyKey, sent[1].body.idempotencyKey);

    state.draftRows = ["@@@"];
    await app.finalizeSession();
    assert.notEqual(sent[2].body.idempotencyKey, sent[0].body.idempotencyKey);

    shouldFail = false;
    await app.finalizeSession();
    assert.equal(sent[3].body.idempotencyKey, sent[2].body.idempotencyKey);
    assert.equal(state.pendingFinalize, null);

    sent.length = 0;
    shouldFail = true;
    elements.intentionInput.value = "  Keep boxes apart  ";
    await app.submitIntention({ preventDefault() {} });
    await app.submitIntention({ preventDefault() {} });
    assert.equal(sent[0].body.idempotencyKey, sent[1].body.idempotencyKey);

    elements.intentionInput.value = "Keep boxes together";
    await app.submitIntention({ preventDefault() {} });
    assert.notEqual(sent[2].body.idempotencyKey, sent[0].body.idempotencyKey);

    shouldFail = false;
    await app.submitIntention({ preventDefault() {} });
    assert.equal(sent[3].body.idempotencyKey, sent[2].body.idempotencyKey);
    assert.equal(state.pendingIntention, null);
});

test("P1: binary action reconciles committed failure and scheduleBinaryRefresh polls safely without wiping draft", async () => {
    const scheduled = [];
    const cleared = [];
    const windowListeners = new Map();
    let mode = "committed_turn";
    const { app, state, document, localStorage } = loadApp({
        window: {
            location: { pathname: "/cocreation/", origin: "http://localhost", href: "http://localhost/cocreation/", search: "", hash: "" },
            history: { replaceState() {} },
            addEventListener: (type, handler) => windowListeners.set(type, handler),
            removeEventListener() {},
        },
        setTimeout: (cb, delay) => { scheduled.push({ cb, delay }); return scheduled.length; },
        clearTimeout: id => cleared.push(id),
        overrides: {
            render: () => {},
            startChatTimer: () => {},
            stopChatTimer: () => {},
            ensureVisibleTranslations: async () => {},
            api: async (_path, options = {}) => {
                if (options.method === "POST") {
                    const err = new Error("timeout");
                    err.code = "CLIENT_TIMEOUT";
                    err.retryable = true;
                    throw err;
                }
                if (mode === "committed_turn") {
                    return {
                        ...state.session,
                        turns: [{ role: "assistant", versionId: "v1", requestId: "bin-1" }],
                        proposalFlowState: { active: true, interactionMode: "binary_choice", status: "clarifying" },
                    };
                }
                if (mode === "cancelled") {
                    return {
                        ...state.session,
                        proposalFlowState: { active: false, interactionMode: "binary_choice", status: "cancelled" },
                    };
                }
                if (mode === "recovered_failure") {
                    return {
                        ...state.session,
                        proposalFlowState: { active: true, interactionMode: "binary_choice", status: "clarifying" },
                        lastMessageFailure: {
                            messageKey: "bin-2",
                            baseVersionId: "v1",
                            code: "PROPOSAL_TRANSLATION_STALE",
                            retryable: false,
                            committed: false,
                        },
                    };
                }
                return {
                    ...state.session,
                    proposalFlowState: { active: false, interactionMode: "binary_choice", status: "proposal_ready" },
                };
            },
        },
    });
    app.initDom();
    state.session.turns = [];
    state.session.proposalFlowState = { active: true, interactionMode: "binary_choice", status: "clarifying" };

    await app.submitBinaryAction({ kind: "choice", baseVersionId: "v1", topicId: "t1", questionId: "q1", choiceId: "c1", idempotencyKey: "bin-1" });
    assert.equal(state.pendingBinary, null);
    assert.equal(localStorage.getItem(app.binaryStorageKey()), null);
    assert.equal(state.chatStatus, "idle");
    assert.equal(state.chatError, null);

    mode = "cancelled";
    await app.submitBinaryAction({ kind: "cancel", baseVersionId: "v1", topicId: "t1", idempotencyKey: "bin-cancel" });
    assert.equal(state.pendingBinary, null);
    assert.equal(state.chatStatus, "idle");

    mode = "recovered_failure";
    state.session.proposalFlowState = { active: true, interactionMode: "binary_choice", status: "clarifying" };
    await app.submitBinaryAction({ kind: "choice", baseVersionId: "v1", topicId: "t1", questionId: "q1", choiceId: "c1", idempotencyKey: "bin-2" });
    assert.equal(state.chatError?.code, "PROPOSAL_TRANSLATION_STALE");
    assert.equal(state.pendingBinary, null);

    // scheduleBinaryRefresh guards & lightweight polling preserve draftRows and activeCoordinateLink
    state.session.proposalFlowState = { active: true, interactionMode: "binary_choice", status: "planning" };
    state.dirty = true;
    state.draftRows = ["unsaved-map"];
    state.activeCoordinateLink = { versionId: "v1", turnId: "t1", linkIndex: 0 };
    state.pendingBinary = { kind: "choice", baseVersionId: "v1", idempotencyKey: "bin-poll" };
    state.chatStatus = "error";
    state.chatError = { code: "CLIENT_TIMEOUT", retryable: true };

    document.hidden = true;
    app.scheduleBinaryRefresh();
    assert.equal(scheduled.length, 0);

    document.hidden = false;
    app.scheduleBinaryRefresh();
    assert.equal(scheduled.length, 1);

    document.hidden = true;
    app.scheduleBinaryRefresh();
    assert.deepEqual(cleared, [1]);

    document.hidden = false;
    app.scheduleBinaryRefresh();
    assert.equal(scheduled.length, 2);

    mode = "proposal_ready";
    await scheduled[1].cb();
    assert.equal(state.draftRows[0], "unsaved-map");
    assert.equal(state.dirty, true);
    assert.equal(state.activeCoordinateLink.turnId, "t1");
    assert.equal(state.pendingBinary, null);
    assert.equal(state.chatStatus, "idle");
    assert.equal(state.chatError, null);

    state.session.proposalFlowState = { active: true, interactionMode: "binary_choice", status: "planning" };
    app.scheduleBinaryRefresh();
    const timerBeforePagehide = state.binaryRefreshTimer;
    windowListeners.get("pagehide")();
    assert.ok(timerBeforePagehide !== null && state.binaryRefreshTimer === null);
});

test("P1: binary/cancel/revision guards and selectedReplyFailure enforce editable & dirty constraints", async () => {
    const apiCalls = [];
    const { app, state, elements, document } = loadApp({
        overrides: {
            api: async path => { apiCalls.push(path); return state.session; },
        },
    });
    state.session.turns = [];
    state.session.proposalFlowState = {
        active: true,
        interactionMode: "binary_choice",
        status: "retry_pending",
        retryable: true,
        retryAction: { kind: "choice", baseVersionId: "v1", questionId: "q1", choiceId: "c1", idempotencyKey: "k1" },
    };
    const revBtn = document.createElement("button");
    revBtn.className = "revision-action-button";
    revBtn.dataset.actionable = "true";

    app.updateControls();
    assert.equal(revBtn.disabled, true);
    const cancelBtn = document.getElementById("cancelProposalButton");
    assert.equal(cancelBtn.hidden, false);

    state.dirty = true;
    app.updateControls();
    assert.equal(elements.chatRetryButton.disabled, true);
    await app.retryBinaryProposal();
    assert.equal(apiCalls.length, 0);

    state.dirty = false;
    state.session.deadlineExpired = true;
    state.replyFeedback = { message: "stale feedback", versionId: "v1" };
    app.updateControls();
    assert.equal(cancelBtn.disabled, true);
    assert.equal(app.selectedReplyFailure(), null);
    await app.retryBinaryProposal();
    assert.equal(apiCalls.length, 0);

    state.session.deadlineExpired = false;
    state.session.status = "awaiting_intention";
    app.updateControls();
    assert.equal(cancelBtn.disabled, true);
    assert.equal(app.selectedReplyFailure(), null);
});

test("P1: card/challenge retries preserve composer draft, set requestProposal=false, and clear non-retryable errors", async () => {
    let apiBehavior = "succeed";
    const { app, state, elements, localStorage } = loadApp({
        keepSubmit: true,
        keepPersistPending: true,
        overrides: {
            render: () => {},
            startChatTimer: () => {},
            stopChatTimer: () => {},
            hideNotice: () => {},
            renderChatRequestStatus: () => {},
            updateControls: () => {},
            updateCharacterCount: () => {},
            isLatestRevisionOfferTurn: () => true,
            api: async () => {
                if (apiBehavior === "retryable_commit") {
                    const err = new Error("dropped");
                    err.code = "CLIENT_TIMEOUT";
                    err.retryable = true;
                    throw err;
                }
                if (apiBehavior === "non_retryable") {
                    const err = new Error("blocked");
                    err.code = "DISAGREEMENT_ACTIVE";
                    err.retryable = false;
                    throw err;
                }
                return { ...state.session };
            },
        },
    });
    state.session.turns = [];
    elements.messageInput.value = "My unsent composer note";
    localStorage.setItem(app.composerKey(), "My unsent composer note");
    state.proposalMode = true;

    app.sendRevisionCardAction("execute_revision", { turnId: "t-offer" }, {});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(state.pendingMessage, null); // cleared on success
    assert.equal(elements.messageInput.value, "My unsent composer note");
    assert.equal(localStorage.getItem(app.composerKey()), "My unsent composer note");
    assert.equal(state.proposalMode, true);

    // Reload recovery must not discard failed card-action retry when composer draft differs, nor overwrite composer
    const failedCard = {
        content: "Execute the bound proposal.",
        baseVersionId: "v1",
        idempotencyKey: "card-1",
        requestProposal: false,
        action: "execute_revision",
        sourceTurnId: "t-offer",
    };
    localStorage.setItem(app.pendingMessageKey(), JSON.stringify(failedCard));
    app.recoverPendingMessage();
    assert.equal(state.pendingMessage?.idempotencyKey, "card-1");
    assert.equal(elements.messageInput.value, "My unsent composer note");
    assert.equal(state.proposalMode, true);

    await app.retryPendingMessage();
    assert.equal(elements.messageInput.value, "My unsent composer note");

    // sendMessage compares Boolean(requestProposal) !== proposalMode and ignores card pendingMessage
    state.pendingMessage = { ...failedCard, content: "My unsent composer note" };
    state.proposalMode = false;
    await app.sendMessage({ preventDefault() {} });
    assert.equal(state.pendingMessage, null);

    // Non-retryable error on normal composer message clears pendingMessage but keeps composer text
    apiBehavior = "non_retryable";
    elements.messageInput.value = "Keep my draft";
    await app.sendMessage({ preventDefault() {} });
    assert.equal(state.pendingMessage, null);
    assert.equal(elements.messageInput.value, "Keep my draft");
});

test("P1: draft regeneration guards concurrency, recovers on RETRYABLE_REQUEST_FAILED/INVALID_RESPONSE, and preserves non-retryable 4xx", async () => {
    let calls = 0;
    let mode = "retryable_then_pending";
    const { app, state } = loadApp({
        overrides: {
            renderDraftPreview: () => {},
            updateControls: () => {},
            api: async (_path, options = {}) => {
                calls += 1;
                if (options.method === "POST" && mode === "retryable_then_pending") {
                    const err = new Error("bad gateway");
                    err.code = "RETRYABLE_REQUEST_FAILED";
                    err.retryable = true;
                    throw err;
                }
                if (mode === "retryable_then_pending") {
                    return {
                        ...state.session,
                        draftPreview: { rows: makeValidRows(), mutable: true, regenerationStatus: "pending", regenerationRequestId: "reg-1" },
                    };
                }
                if (options.method === "POST" && mode === "4xx") {
                    const err = new Error("locked");
                    err.code = "SESSION_LOCKED";
                    err.retryable = false;
                    throw err;
                }
                return state.session;
            },
        },
    });
    state.session.demoMode = true;
    state.session.languageLocked = false;
    state.session.draftPreview = { rows: makeValidRows(), mutable: true };

    const recovered = await app.createOrRecoverDraftRegeneration("reg-1");
    assert.equal(recovered.draftPreview.regenerationStatus, "pending");
    assert.equal(calls, 2);

    // Also enters recovery on INVALID_RESPONSE and retries once cleanly
    let invalidCalls = 0;
    const invApp = loadApp({
        overrides: {
            api: async (_path, options = {}) => {
                invalidCalls += 1;
                if (options.method === "POST" && invalidCalls === 1) {
                    const err = new Error("bad json");
                    err.code = "INVALID_RESPONSE";
                    err.retryable = true;
                    throw err;
                }
                return {
                    ...invApp.state.session,
                    draftPreview: { rows: makeValidRows(), mutable: true, regenerationStatus: "pending", regenerationRequestId: "reg-inv" },
                };
            },
        },
    });
    const invRecovered = await invApp.app.createOrRecoverDraftRegeneration("reg-inv");
    assert.equal(invRecovered.draftPreview.regenerationRequestId, "reg-inv");
    assert.equal(invalidCalls, 2);

    mode = "4xx";
    await assert.rejects(
        () => app.createOrRecoverDraftRegeneration("reg-2"),
        err => err.code === "SESSION_LOCKED" && err.retryable === false,
    );

    state.draftRegenerationPending = true;
    const prevCalls = calls;
    await app.regenerateDraft();
    assert.equal(calls, prevCalls);
});

test("P2: error codes, challenge choice language, play composer guard, selectVersion early-return, deadline timer, stage cards, component recovery, question feedback, and bridge fixes", async () => {
    const clearedIntervals = [];
    const { app, state, elements, document, localStorage } = loadApp({
        clearInterval: id => clearedIntervals.push(id),
        overrides: {
            render: () => {},
            ensureVisibleTranslations: async () => {},
            api: async (path, options = {}) => {
                if (path.includes("/questions/") && options.method === "POST") {
                    return { outcome: "ignored", session: state.session };
                }
                return state.session;
            },
        },
    });

    // 1. Error mappings & zh-CN CLIENT_TIMEOUT & draftRegenerationFailureMessage
    const requiredCodes = [
        "VERSION_CONFLICT", "UNCHANGED_LEVEL", "PENDING_PROPOSAL", "PROPOSAL_PENDING",
        "SESSION_LOCKED", "SESSION_ACCESS_DENIED", "INVALID_BOOTSTRAP_TOKEN", "BOOTSTRAP_TOKEN_USED",
        "SESSION_DEADLINE_EXPIRED", "PROPOSAL_CHOICE_REQUIRED", "BINARY_BUSY",
        "PROPOSAL_TRANSLATION_STALE", "BINARY_PROMISE_NOT_REALIZED", "BINARY_DIRECTIONS_UNAVAILABLE",
        "BINARY_PREFLIGHT_BUDGET", "BINARY_PLANNING_BUDGET", "BINARY_VALIDATION_FAILED",
        "CHALLENGE_STALE", "CHALLENGE_REVIEW_BUSY", "UPSTREAM_RATE_LIMIT", "UPSTREAM_ERROR",
    ];
    for (const lang of ["zh-CN", "en"]) {
        state.language = lang;
        for (const code of requiredCodes) {
            assert.notEqual(app.localizedErrorMessage({ code }), app.t("errorGeneric"), `${lang} missing ${code}`);
            assert.notEqual(app.technicalErrorMessage({ code }), app.t("errorGeneric"), `${lang} tech missing ${code}`);
        }
        assert.notEqual(app.t("error_CLIENT_TIMEOUT"), "");
        assert.match(app.draftRegenerationFailureMessage("generation_failed"), /draft|生成/i);
        assert.match(app.draftRegenerationFailureMessage("llm_failed"), /AI|planner/i);
    }
    state.language = "zh-CN";
    assert.doesNotMatch(app.t("error_CLIENT_TIMEOUT"), /browser safety limit/);

    // 2. sendChallengeChoice uses Yes/No in English and 是/否 in zh-CN
    state.language = "en";
    app.sendChallengeChoice("challenge-1", "ai");
    assert.equal(state.pendingMessage.content, "Yes");
    assert.equal(state.pendingMessage.requestProposal, false);
    state.pendingMessage = null;
    state.language = "zh-CN";
    app.sendChallengeChoice("challenge-1", "user");
    assert.equal(state.pendingMessage.content, "否");
    state.pendingMessage = null;

    // 3. playSelectedStage on historical stage does not overwrite current composer draft
    state.session.demoMode = true;
    state.session.versions = [makeRichVersion("v0", 1), makeRichVersion("v1", 2)];
    state.session.currentVersionId = "v1";
    state.selectedVersionId = "v0";
    localStorage.setItem(app.composerKey(), "Keep current draft");
    elements.messageInput.value = "";
    await app.playSelectedStage();
    assert.equal(localStorage.getItem(app.composerKey()), "Keep current draft");

    // 4. selectVersion early-returns on same versionId without forceReset
    state.selectedVersionId = "v1";
    state.proposalMode = true;
    state.dirty = true;
    state.draftRows = ["custom"];
    app.selectVersion("v1");
    assert.equal(state.proposalMode, true);
    assert.equal(state.draftRows[0], "custom");
    app.selectVersion("v1", false, { forceReset: true });
    assert.equal(state.proposalMode, false);
    assert.equal(state.dirty, false);

    // 5. deadline update clears interval and updateControls syncs .stage-card disabled state
    let intervalTick = null;
    let perfNowMs = 2000;
    const timerApp = loadApp({
        performance: { now: () => perfNowMs },
        setInterval: cb => { intervalTick = cb; return 77; },
        clearInterval: id => clearedIntervals.push(id),
    });
    const stageCard = timerApp.document.createElement("button");
    stageCard.className = "stage-card";
    timerApp.state.session.deadlineAt = "2026-11-23T16:00:00Z";
    timerApp.state.session.remainingSeconds = 1;
    timerApp.app.renderDeadline();
    assert.equal(stageCard.disabled, false);
    perfNowMs += 1000;
    intervalTick();
    assert.equal(clearedIntervals.includes(77), true);
    assert.equal(timerApp.state.deadlineTimerId, null);
    assert.equal(stageCard.disabled, true);

    // 6. component recovery: unityPlayActive, merge rejection during busy, unclaimed fallback GET
    state.selectedVersionId = "v1";
    state.unityPlayActive = true;
    assert.equal(app.recoveryPageEligible(), false);
    state.unityPlayActive = false;
    state.busy = true;
    assert.equal(app.mergeRecoverySession(state.session, { sessionId: "session", selectedVersionId: "v1", versionId: "v1" }), false);
    state.busy = false;

    const busyDeferred = loadApp({
        overrides: {
            render: () => {},
            scheduleComponentRecovery: () => {},
            api: async () => {
                busyDeferred.state.chatBusy = true;
                return {
                    ...busyDeferred.state.session,
                    interactionState: { versionId: "v1", revision: 2, recoveries: [] },
                };
            },
        },
    });
    busyDeferred.state.session.interactionState = { versionId: "v1", revision: 1, recoveries: [] };
    await busyDeferred.app.recoverComponent({ recoveryId: "r-busy", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-busy-1" });
    assert.equal(busyDeferred.app.componentRecovery.dispatched.has("r-busy-1"), false);
    assert.equal(busyDeferred.state.session.interactionState.revision, 1);

    const recoveryHarness = loadApp({
        overrides: {
            render: () => {},
            scheduleComponentRecovery: () => {},
            api: async (_path, options = {}) => {
                if (options.method === "POST") throw new Error("network dropped");
                return {
                    ...state.session,
                    interactionState: {
                        versionId: "v1",
                        revision: 1,
                        recoveries: [{ recoveryId: "r-unclaimed", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-unclaimed-1" }],
                    },
                };
            },
        },
    });
    recoveryHarness.state.session.interactionState = { versionId: "v1", revision: 1, recoveries: [] };
    await recoveryHarness.app.recoverComponent({ recoveryId: "r-unclaimed", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-unclaimed-1" });
    assert.equal(recoveryHarness.app.componentRecovery.dispatched.has("r-unclaimed-1"), false);

    // 7. submitQuestionFeedback captures baseVersionId + requestKey, serializes via withBusy, preserves proposalMode
    const qCalls = [];
    let qAttempt = 0;
    const qApp = loadApp({
        overrides: {
            render: () => {},
            ensureVisibleTranslations: async () => {},
            api: async (_path, options = {}) => {
                if (options.method === "POST") {
                    qCalls.push(options.body);
                    qAttempt += 1;
                    if (qAttempt === 1) {
                        const err = new Error("retry");
                        err.retryable = true;
                        throw err;
                    }
                    return { outcome: "ignored", session: qApp.state.session };
                }
                return qApp.state.session;
            },
        },
    });
    qApp.state.session.turns = [];
    qApp.state.proposalMode = true;
    const record = qApp.document.createElement("article");
    // Same-stage question feedback preserves state.proposalMode
    qAttempt = 1;
    await qApp.app.submitQuestionFeedback("ignore", { questionId: "q-same" }, record);
    assert.equal(qApp.state.proposalMode, true);
    // Retryable failure preserves baseVersionId + idempotencyKey even if currentVersionId later changes
    qAttempt = 0;
    qCalls.length = 0;
    await qApp.app.submitQuestionFeedback("ignore", { questionId: "q1" }, record);
    qApp.state.session.currentVersionId = "v2";
    await qApp.state.replyFeedback.retryAction();
    assert.equal(qCalls.length, 2);
    assert.equal(qCalls[0].baseVersionId, "v1");
    assert.equal(qCalls[1].baseVersionId, "v1");
    assert.equal(qCalls[0].idempotencyKey, qCalls[1].idempotencyKey);

    // 8. regenerateDraftButton disabled when sessionId && !session; uiHelpBlockedStatus; revision_needed binary placeholder
    const landingBroken = loadApp();
    landingBroken.state.sessionId = "existing-session";
    landingBroken.state.session = null;
    landingBroken.app.updateControls();
    assert.equal(landingBroken.elements.regenerateDraftButton.disabled, true);

    state.session.proposalFlowState = { active: true, interactionMode: "binary_choice", status: "revision_needed" };
    state.session.turns = [];
    app.updateControls();
    assert.equal(elements.messageInput.placeholder, app.t("binaryRevisionNeededPlaceholder"));
    const propBtn = document.createElement("button");
    propBtn.id = "proposalRequestButton";
    propBtn.disabled = true;
    assert.equal(app.uiHelpBlockedStatus(propBtn, "proposalRequestButton"), app.t("proposalTopicActive"));

    // 9. play-return protocolVersion validation and control restoration on refresh error
    let parentMsg;
    const bridgeWin = {
        location: { pathname: "/cocreation/", origin: "http://localhost", href: "http://localhost/cocreation/", search: "", hash: "" },
        history: { replaceState() {} },
        parent: { postMessage: m => { parentMsg = m; } },
        addEventListener() {},
        removeEventListener() {},
    };
    let shouldFailRefresh = true;
    let recoveredCount = 0;
    const bridgeApp = loadApp({
        window: bridgeWin,
        overrides: {
            renderMap: () => {},
            render: () => {},
            restoreComposerDraft: () => {},
            recoverPendingMessage: () => { recoveredCount += 1; },
            ensureVisibleTranslations: async () => {},
            ensureAssessment: async () => {},
            api: async () => {
                if (shouldFailRefresh) {
                    const err = new Error("refresh failed");
                    err.retryable = true;
                    throw err;
                }
                return bridgeApp.state.session;
            },
        },
    });
    bridgeApp.state.session.demoMode = false;
    bridgeApp.elements.formalStandaloneBlock.hidden = false;
    shouldFailRefresh = false;
    bridgeApp.app.handleUnityHostInit({ source: bridgeWin.parent, origin: "http://localhost" }, {
        protocolVersion: 5,
        sessionId: "session",
    });
    assert.equal(parentMsg.type, "sokoban:cocreation-lab-ready");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(recoveredCount, 1);

    bridgeApp.state.unityPlayActive = true;
    bridgeApp.app.updateControls();
    assert.equal(bridgeApp.elements.playButton.disabled, true);
    // Mismatched protocolVersion is ignored
    await bridgeApp.app.handleUnityBridgeMessage({
        source: bridgeWin.parent,
        origin: "http://localhost",
        data: { type: "sokoban:cocreation-play-return", protocolVersion: 4, sessionId: "session" },
    });
    assert.equal(bridgeApp.state.unityPlayActive, true);
    // Valid protocolVersion with succeeding refresh calls recoverPendingMessage
    await bridgeApp.app.handleUnityBridgeMessage({
        source: bridgeWin.parent,
        origin: "http://localhost",
        data: { type: "sokoban:cocreation-play-return", protocolVersion: 5, sessionId: "session" },
    });
    assert.equal(recoveredCount, 2);
    // Valid protocolVersion with failing refresh still restores controls
    bridgeApp.state.unityPlayActive = true;
    bridgeApp.app.updateControls();
    shouldFailRefresh = true;
    await bridgeApp.app.handleUnityBridgeMessage({
        source: bridgeWin.parent,
        origin: "http://localhost",
        data: { type: "sokoban:cocreation-play-return", protocolVersion: 5, sessionId: "session" },
    });
    assert.equal(bridgeApp.state.unityPlayActive, false);
    assert.equal(bridgeApp.elements.playButton.disabled, false);
});
