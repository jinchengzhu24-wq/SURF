const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadApp({ keepSubmit = false } = {}) {
    const created = [];
    function element(tagName = "div") {
        const attributes = new Map();
        const listeners = new Map();
        const node = {
            tagName,
            className: "",
            dataset: {},
            disabled: false,
            value: "",
            hidden: false,
            children: [],
            style: {},
            addEventListener(type, handler) { listeners.set(type, handler); },
            setAttribute(name, value) { attributes.set(name, value); },
            removeAttribute(name) { attributes.delete(name); },
            getAttribute(name) { return attributes.get(name) || null; },
            append(...children) { this.children.push(...children); },
            appendChild(child) { this.children.push(child); return child; },
            click() { if (!this.disabled) listeners.get("click")?.(); },
            classList: { toggle() {} },
            get clickHandler() { return listeners.get("click"); },
        };
        created.push(node);
        return node;
    }
    const document = {
        getElementById() { return element(); },
        createElement: element,
        addEventListener() {},
        querySelectorAll(selector) {
            if (selector.startsWith(".")) {
                const classes = selector.split(",").map(item => item.trim().slice(1));
                return created.filter(node => classes.some(name => node.className.split(" ").includes(name)));
            }
            return [];
        },
    };
    const context = vm.createContext({
        document,
        window: { location: { pathname: "/cocreation/", origin: "http://localhost" }, addEventListener() {} },
        crypto: { randomUUID: () => "12345678-1234-1234-1234-123456789abc" },
        localStorage: { setItem() {}, removeItem() {} },
        console,
    });
    const appPath = path.join(__dirname, "..", "app.js");
    const source = fs.readFileSync(appPath, "utf8");
    assert.match(source, /applyTranslations\(\);\s*initialize\(\);/);
    vm.runInContext(source.replace(/applyTranslations\(\);\s*initialize\(\);/, ""), context);
    vm.runInContext(`
        state.sessionId = "session";
        state.selectedVersionId = "v1";
        state.session = {
            status: "active", languageLocked: true, currentVersionId: "v1",
            deadlineExpired: false, versions: [{ versionId: "v1" }],
            assessments: [], proposals: [], turns: [{
                role: "assistant", versionId: "v1",
                guidance: {
                    disagreement: { status: "active" },
                    challengeState: { challengeId: "challenge-1", status: "choice_pending" }
                }
            }]
        };
        persistPendingMessage = () => {};
        ${keepSubmit ? "" : "submitPendingMessage = async () => {};"}
    `, context);
    return { context, created };
}

test("current controls use canonical disagreement state while history retains its marker", () => {
    const { context } = loadApp();
    vm.runInContext(`
        state.session.interactionState = { versionId: "v1", revision: 10, proposalBlocked: false };
        state.session.turns.push({ role: "assistant", versionId: "history", guidance: { disagreement: { status: "active" } } });
    `, context);
    assert.equal(vm.runInContext("selectedStageHasActiveDisagreement()", context), false);
    assert.equal(vm.runInContext("state.session.turns[0].guidance.disagreement.status", context), "active");
    vm.runInContext("state.selectedVersionId = 'history'", context);
    assert.equal(vm.runInContext("selectedStageHasActiveDisagreement()", context), true);
});

test("automatic recovery schedules one stable key and pauses while sending or hidden", () => {
    const { context } = loadApp();
    vm.runInContext(`
        const recoveryTimers = [];
        const cancelledRecoveryTimers = [];
        window.setTimeout = (callback, delay) => { recoveryTimers.push({ callback, delay }); return recoveryTimers.length; };
        window.clearTimeout = id => cancelledRecoveryTimers.push(id);
        state.session.interactionState = { versionId: "v1", revision: 1, recoveries: [{
            recoveryId: "r", baseVersionId: "v1", status: "pending", attemptsUsed: 0, nextAttemptKey: "r-1"
        }] };
        scheduleComponentRecovery(); scheduleComponentRecovery();
    `, context);
    assert.equal(vm.runInContext("recoveryTimers.length", context), 1);
    assert.equal(vm.runInContext("recoveryTimers[0].delay", context), 1000);
    vm.runInContext("state.chatBusy = true; scheduleComponentRecovery()", context);
    assert.equal(vm.runInContext("componentRecovery.timer", context), null);
    vm.runInContext("state.chatBusy = false; document.hidden = true; scheduleComponentRecovery()", context);
    assert.equal(vm.runInContext("recoveryTimers.length", context), 1);
    vm.runInContext(`
        document.hidden = false;
        state.session.interactionState.recoveries[0].status = "retry_pending";
        state.session.interactionState.recoveries[0].attemptsUsed = 1;
        state.session.interactionState.recoveries[0].nextAttemptKey = "r-2";
        scheduleComponentRecovery();
    `, context);
    assert.equal(vm.runInContext("recoveryTimers[1].delay", context), 3000);
});

test("recovery merges canonical state without altering composer, map draft, selection or retry", () => {
    const { context } = loadApp();
    vm.runInContext(`
        state.session.sessionId = "session";
        state.session.turns[0].sequence = 2;
        state.session.interactionState = { versionId: "v1", revision: 10, proposalBlocked: true, recoveries: [] };
        state.draftRows = ["unsaved"];
        state.dirty = true;
        state.proposalMode = true;
        state.pendingMessage = { idempotencyKey: "original-key", content: "retry text" };
        elements.messageInput.value = "new text";
        render = () => {};
        const capturedRecovery = { sessionId: "session", selectedVersionId: "v1", versionId: "v1" };
        const recoveredSession = { ...state.session, interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] } };
    `, context);
    assert.equal(vm.runInContext("mergeRecoverySession(recoveredSession, capturedRecovery)", context), true);
    assert.equal(vm.runInContext("selectedStageHasActiveDisagreement()", context), false);
    assert.equal(vm.runInContext("elements.messageInput.value", context), "new text");
    assert.equal(vm.runInContext("state.draftRows[0]", context), "unsaved");
    assert.equal(vm.runInContext("state.dirty && state.proposalMode", context), true);
    assert.equal(vm.runInContext("state.pendingMessage.idempotencyKey", context), "original-key");
    assert.equal(vm.runInContext("state.selectedVersionId", context), "v1");
});

test("late recovery cannot overwrite newer turns, state or another selected Stage", () => {
    const { context } = loadApp();
    vm.runInContext(`
        state.session.sessionId = "session";
        state.session.turns[0].sequence = 5;
        state.session.interactionState = { versionId: "v1", revision: 12, proposalBlocked: true, recoveries: [] };
        render = () => {};
        const capturedRecovery = { sessionId: "session", selectedVersionId: "v1", versionId: "v1" };
        const oldRecovery = { ...state.session, interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] } };
    `, context);
    assert.equal(vm.runInContext("mergeRecoverySession(oldRecovery, capturedRecovery)", context), false);
    vm.runInContext("oldRecovery.interactionState.revision = 13; oldRecovery.turns = [{sequence: 4}]", context);
    assert.equal(vm.runInContext("mergeRecoverySession(oldRecovery, capturedRecovery)", context), false);
    vm.runInContext("state.selectedVersionId = 'history'; oldRecovery.turns = [{sequence: 6}]", context);
    assert.equal(vm.runInContext("mergeRecoverySession(oldRecovery, capturedRecovery)", context), false);
    assert.equal(vm.runInContext("state.session.interactionState.proposalBlocked", context), true);
});

test("state replay uses separate POST and keeps chat usable without duplicate calls", async () => {
    const { context } = loadApp();
    vm.runInContext(`
        state.session.sessionId = "session";
        state.session.interactionState = { versionId: "v1", revision: 10, recoveries: [] };
        state.draftRows = ["unsaved"];
        state.dirty = true;
        elements.messageInput.value = "typing";
        const replayCalls = [];
        render = () => {};
        scheduleComponentRecovery = () => {};
        api = async (path, options) => {
            replayCalls.push({ path, options });
            return { ...state.session, interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] } };
        };
        const replayTask = { recoveryId: "r", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-1" };
    `, context);
    await vm.runInContext("recoverComponent(replayTask)", context);
    await vm.runInContext("recoverComponent(replayTask)", context);
    assert.equal(vm.runInContext("replayCalls.length", context), 1);
    assert.equal(vm.runInContext("replayCalls[0].options.body.idempotencyKey", context), "r-1");
    assert.equal(vm.runInContext("replayCalls[0].options.timeoutMs", context), 12000);
    assert.match(vm.runInContext("replayCalls[0].path", context), /component-recoveries\/r\/retry$/);
    assert.equal(vm.runInContext("state.busy || state.chatBusy", context), false);
    assert.equal(vm.runInContext("elements.messageInput.value", context), "typing");
    assert.equal(vm.runInContext("state.draftRows[0]", context), "unsaved");
});

test("claimed recovery refreshes without a model POST and old normal responses retain newer authority", async () => {
    const { context } = loadApp();
    vm.runInContext(`
        state.session.sessionId = "session";
        state.session.interactionState = { versionId: "v1", revision: 12, proposalBlocked: false, recoveries: [] };
        state.session.turns[0].proposalState = { actionable: true };
        const claimRefreshCalls = [];
        render = () => {}; scheduleComponentRecovery = () => {};
        api = async (path, options) => { claimRefreshCalls.push({path, options}); return state.session; };
        const olderNormalResponse = { ...state.session, turns: [{turnId: state.session.turns[0].turnId, proposalState: { actionable: false }}],
            interactionState: {versionId: "v1", revision: 11, proposalBlocked: true} };
    `, context);
    await vm.runInContext("recoverComponent({recoveryId: 'r', baseVersionId: 'v1', status: 'claimed'})", context);
    assert.equal(vm.runInContext("claimRefreshCalls.length", context), 1);
    assert.equal(vm.runInContext("claimRefreshCalls[0].options.method", context), undefined);
    assert.equal(vm.runInContext("retainNewerInteractionState(olderNormalResponse).interactionState.proposalBlocked", context), false);
    assert.equal(vm.runInContext("retainNewerInteractionState(olderNormalResponse).turns[0].proposalState.actionable", context), true);
});

test("a reply arriving while hidden can replay its same key on return without losing the composer", async () => {
    const { context } = loadApp();
    vm.runInContext(`
        state.session.sessionId = "session";
        state.session.interactionState = { versionId: "v1", revision: 10, recoveries: [] };
        elements.messageInput.value = "typing";
        render = () => {}; scheduleComponentRecovery = () => {};
        const hiddenCalls = [];
        api = async (path, options) => {
            hiddenCalls.push(options.body.idempotencyKey);
            if (hiddenCalls.length === 1) document.hidden = true;
            return { ...state.session, interactionState: { versionId: "v1", revision: 11, proposalBlocked: false, recoveries: [] } };
        };
        const hiddenTask = { recoveryId: "r", baseVersionId: "v1", status: "pending", nextAttemptKey: "r-1" };
    `, context);
    await vm.runInContext("recoverComponent(hiddenTask)", context);
    assert.equal(vm.runInContext("state.session.interactionState.revision", context), 10);
    vm.runInContext("document.hidden = false", context);
    await vm.runInContext("recoverComponent(hiddenTask)", context);
    assert.equal(vm.runInContext("hiddenCalls.length", context), 2);
    assert.equal(vm.runInContext("hiddenCalls.every(key => key === 'r-1')", context), true);
    assert.equal(vm.runInContext("state.session.interactionState.revision", context), 11);
    assert.equal(vm.runInContext("elements.messageInput.value", context), "typing");
});

test("choice buttons recover after a reply is rendered while busy", () => {
    const { context, created } = loadApp();
    vm.runInContext("state.busy = true; createDisagreementCard({ subject: 'ai_revision_challenge', phase: 'choice_pending', challengeId: 'challenge-1' });", context);
    const buttons = created.filter(node => node.className.includes("challenge-choice-button"));
    assert.equal(buttons.length, 2);
    assert.ok(buttons.every(button => button.disabled && button.clickHandler));

    vm.runInContext("state.busy = false; updateControls();", context);
    assert.ok(buttons.every(button => !button.disabled && button.getAttribute("aria-disabled") === null));
    buttons[0].click();
    assert.equal(vm.runInContext("state.pendingMessage.challengeChoice", context), "ai");
    assert.equal(vm.runInContext("state.pendingMessage.challengeId", context), "challenge-1");

    vm.runInContext("state.busy = true; updateControls();", context);
    assert.ok(buttons.every(button => button.disabled));
});

test("old, resolved, historical, and expired challenge choices stay locked", () => {
    const { context, created } = loadApp();
    vm.runInContext("createDisagreementCard({ subject: 'ai_revision_challenge', phase: 'choice_pending', challengeId: 'older' });", context);
    const buttons = created.filter(node => node.className.includes("challenge-choice-button"));
    assert.ok(buttons.every(button => button.disabled));
    vm.runInContext("sendChallengeChoice('older', 'ai')", context);
    assert.equal(vm.runInContext("state.pendingMessage", context), null);

    vm.runInContext("state.session.turns[0].guidance.challengeState.status = 'resolved'; updateControls();", context);
    assert.ok(buttons.every(button => button.disabled));
    vm.runInContext("state.session.turns[0].guidance.challengeState.status = 'choice_pending'; state.selectedVersionId = 'history'; updateControls();", context);
    assert.ok(buttons.every(button => button.disabled));
    vm.runInContext("state.selectedVersionId = 'v1'; state.session.deadlineExpired = true; updateControls();", context);
    assert.ok(buttons.every(button => button.disabled));
});

test("map controls lock during a request and recover afterward", () => {
    const { context, created } = loadApp();
    const tile = created[0];
    tile.className = "tile";
    const palette = created[1];
    palette.className = "palette-button";
    const revision = created[2];
    revision.className = "revision-action-button";
    revision.dataset.actionable = "true";
    const staleRevision = created[3];
    staleRevision.className = "revision-action-button";
    staleRevision.dataset.actionable = "false";
    vm.runInContext("state.draftRows = ['............']; state.busy = true; updateControls(); editTile(0, 0);", context);
    assert.ok(tile.disabled && palette.disabled && revision.disabled && staleRevision.disabled);
    assert.equal(vm.runInContext("state.draftRows[0]", context), "............");
    vm.runInContext("state.busy = false; updateControls();", context);
    assert.ok(!tile.disabled && !palette.disabled);
    assert.ok(revision.disabled && staleRevision.disabled);
    vm.runInContext("state.session.turns[0].guidance.disagreement.status = 'resolved'; updateControls();", context);
    assert.ok(!revision.disabled && staleRevision.disabled);
});

test("idempotent message requests keep retry on unreadable 5xx or network failure", async () => {
    const { context } = loadApp();
    context.fetch = async () => ({
        ok: false,
        status: 502,
        async json() { throw new SyntaxError("not JSON"); },
    });
    const request = () => vm.runInContext(
        "api('/api/sessions/session/messages', { method: 'POST', body: { idempotencyKey: 'same-key' } })",
        context,
    );
    await assert.rejects(request, error =>
        error.code === "RETRYABLE_REQUEST_FAILED" && error.retryable === true);

    context.fetch = async () => { throw new TypeError("network interrupted"); };
    await assert.rejects(request, error =>
        error.code === "RETRYABLE_REQUEST_FAILED" && error.retryable === true);
});

test("unreadable or incomplete 2xx is never an empty successful message", async () => {
    const { context } = loadApp();
    vm.runInContext("state.pendingMessage = { content: 'keep this', idempotencyKey: 'same-key' };", context);
    for (const payload of [null, {}, { sessionId: "session" }, [], { turns: [] }]) {
        context.fetch = async () => ({ ok: true, status: 200, async json() { return payload; } });
        await assert.rejects(() => vm.runInContext(
            "api('/api/sessions/session/messages', { method: 'POST', body: { idempotencyKey: 'same-key' } })", context),
        error => error.code === "INVALID_RESPONSE" && error.retryable);
    }
    context.fetch = async () => ({ ok: true, status: 200, async json() { throw new SyntaxError("truncated"); } });
    await assert.rejects(() => vm.runInContext(
        "api('/api/sessions/session/messages', { method: 'POST', body: { idempotencyKey: 'same-key' } })", context),
    error => error.code === "INVALID_RESPONSE" && error.retryable);
    assert.equal(vm.runInContext("state.pendingMessage.content", context), "keep this");
});

test("timeout remains active while reading response body", async () => {
    const { context } = loadApp();
    let abortTimer;
    let cleared = 0;
    context.AbortController = AbortController;
    context.window.setTimeout = callback => { abortTimer = callback; return 1; };
    context.window.clearTimeout = () => { cleared += 1; };
    context.fetch = async (_path, request) => ({ ok: true, status: 200,
        json: () => new Promise((_resolve, reject) => request.signal.addEventListener("abort", () => {
            const error = new Error("body interrupted"); error.name = "AbortError"; reject(error);
        })) });
    const result = vm.runInContext(
        "api('/api/sessions/session/messages', { method: 'POST', body: { idempotencyKey: 'same-key' }, timeoutMs: 10 })", context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(cleared, 0);
    abortTimer();
    await assert.rejects(result, error => error.code === "CLIENT_TIMEOUT" && error.retryable);
    assert.equal(cleared, 1);
});

test("known endpoints validate their own response contracts", async () => {
    const { context } = loadApp();
    const session = { sessionId: "session", status: "active", currentVersionId: "v1", versions: [], turns: [] };
    const cases = [
        ["/api/sessions/session/messages", session],
        ["/api/sessions/session/questions/q/feedback", { outcome: "ignored", session }],
        ["/api/sessions/session/intent-hypotheses/i/feedback", { outcome: "compatible", session }],
        ["/api/sessions/session/versions/v1/play-attempts", { attemptId: "attempt", playUrl: "/play" }],
    ];
    for (const [path, payload] of cases) {
        context.fetch = async () => ({ ok: true, status: 200, async json() { return payload; } });
        context.path = path;
        assert.ok(await vm.runInContext("api(path)", context));
    }
});

test("manual save retry uses the captured rows, base Stage and idempotency key", async () => {
    const { context } = loadApp();
    const bodies = [];
    context.recordBody = body => bodies.push(JSON.parse(JSON.stringify(body)));
    vm.runInContext(`
        let retryOperation;
        withBusy = async action => { retryOperation = action; try { await action(); } catch (_) {} };
        api = async (_path, options) => { recordBody(options.body); throw { code: 'UPSTREAM_TIMEOUT', retryable: true }; };
        renderMap = () => {}; render = () => {};
        state.dirty = true; state.draftRows = ['............'];
    `, context);
    await vm.runInContext("saveManualStage()", context);
    vm.runInContext("state.draftRows = ['############']; state.session.currentVersionId = 'v2';", context);
    await assert.rejects(() => vm.runInContext("retryOperation()", context));
    assert.equal(bodies.length, 2);
    assert.deepEqual(bodies[0], bodies[1]);
    assert.equal(bodies[1].baseVersionId, "v1");
});

test("diagnostic error details retain the server request ID and phase", async () => {
    const { context } = loadApp();
    context.fetch = async () => ({ ok: false, status: 502,
        headers: { get: name => name === "X-Request-ID" ? "diagnostic-id" : "application/json" },
        async json() { return { code: "REQUIREMENT_INTERPRETATION_INVALID", retryable: true,
            details: { task: "revision_requirements", failureStage: "evidence" } }; } });
    await assert.rejects(() => vm.runInContext(
        "api('/api/sessions/session/messages', { method: 'POST', body: { idempotencyKey: 'same-key' } })", context),
    error => error.details.requestId === "diagnostic-id" && error.details.failureStage === "evidence"
        && error.details.httpStatus === 502);
});

test("unreadable response recovers only the matching current request diagnostic", () => {
    const { context } = loadApp();
    const result = vm.runInContext(`
        const unreadable = Object.assign(new Error('generic'), { code: 'RETRYABLE_REQUEST_FAILED',
            retryable: true, details: { httpStatus: 502 } });
        const pending = { idempotencyKey: 'original-key', baseVersionId: 'stage' };
        const latest = { lastMessageFailure: { messageKey: 'original-key', baseVersionId: 'stage',
            code: 'REQUIREMENT_INTERPRETATION_INVALID', retryable: true, committed: false,
            requestId: 'server-id', details: { failureStage: 'review', failureKind: 'truncated_json' } } };
        const recovered = recoverMessageFailure(unreadable, latest, pending);
        [recovered.code, recovered.retryable, recovered.details.requestId,
            recovered.details.failureKind, recovered.details.httpStatus];
    `, context);
    assert.deepEqual(Array.from(result), ['REQUIREMENT_INTERPRETATION_INVALID', true, 'server-id', 'truncated_json', 502]);
    for (const changes of [{ messageKey: 'other' }, { baseVersionId: 'history' }, { committed: true }]) {
        context.changes = changes;
        assert.equal(vm.runInContext(
            "recoverMessageFailure(unreadable, { lastMessageFailure: { ...latest.lastMessageFailure, ...changes } }, pending) === unreadable", context), true);
    }
    assert.equal(vm.runInContext(
        "recoverMessageFailure({ code: 'INVALID_CARD_SOURCE' }, latest, pending).code", context), 'INVALID_CARD_SOURCE');
});

test("classification diagnostics show actual attempts without claiming three body attempts", () => {
    const { context } = loadApp();
    for (const language of ["en", "zh-CN"]) {
        const message = vm.runInContext(`state.language = '${language}'; localizedErrorMessage({
            code: 'MODEL_RESPONSE_INVALID', details: { task: 'turn_understanding', attemptsUsed: 2, maximumAttempts: 2 }
        })`, context);
        assert.match(message, /2\/2/);
        assert.doesNotMatch(message, /three|三次/);
    }
});

test("review diagnostics separate component repair from generation and do not invent missing counts", () => {
    const { context } = loadApp();
    for (const language of ["en", "zh-CN"]) {
        const message = vm.runInContext(`state.language = '${language}'; localizedErrorMessage({
            code: 'MODEL_RESPONSE_INVALID', details: { failureStage: 'clarification_review',
                attemptsUsed: 1, maximumAttempts: 2, repairAttemptsUsed: 1, maximumRepairAttempts: 1 }
        })`, context);
        assert.match(message, /1\/2/);
        assert.match(message, /1\/1/);
        assert.match(message, language === "en" ? /clarification review/ : /澄清的复核/);
        assert.doesNotMatch(message, /2\/3/);
        const unknown = vm.runInContext("localizedErrorMessage({ code: 'MODEL_RESPONSE_INVALID', details: { attemptsUsed: 2 } })", context);
        assert.doesNotMatch(unknown, /\(\d+\/\d+\)/);
        const invalid = vm.runInContext("localizedErrorMessage({ code: 'MODEL_RESPONSE_INVALID', details: { attemptsUsed: 3, maximumAttempts: 2 } })", context);
        assert.doesNotMatch(invalid, /3\/2/);
    }
});

test("message refresh recovers a diagnostic without changing retry input, then clears it on success", async () => {
    for (const committed of [false, true]) {
        const { context } = loadApp({ keepSubmit: true });
        context.committedOnRefresh = committed;
        vm.runInContext(`
            startChatTimer = stopChatTimer = hideNotice = renderChatRequestStatus = updateControls = render = updateCharacterCount = () => {};
            state.pendingMessage = { content: "否", baseVersionId: "v1", idempotencyKey: "same-key",
                requestProposal: true, action: "continue_challenge", challengeId: "challenge-1", challengeChoice: "user" };
            elements.messageInput.value = "否";
            var capturedRetry = JSON.stringify(state.pendingMessage);
            var apiCalls = [];
            api = async (path, options = {}) => {
                apiCalls.push({ path, body: options.body && JSON.stringify(options.body) });
                if (apiCalls.length === 1) {
                    const error = new Error("Unreadable HTTP error");
                    error.code = "RETRYABLE_REQUEST_FAILED";
                    error.retryable = true;
                    throw error;
                }
                const success = apiCalls.length > 2 || committedOnRefresh;
                return { ...state.session, turns: success ? [{ role: "assistant", versionId: "v1", requestId: "same-key" }] : [],
                    lastMessageFailure: success ? null : { messageKey: "same-key", baseVersionId: "v1",
                        code: "REQUIREMENT_INTERPRETATION_INVALID", retryable: true, committed: false,
                        requestId: "server-request", details: { failureStage: "review", failureKind: "truncated_json" } } };
            };
        `, context);
        await vm.runInContext("submitPendingMessage()", context);
        if (!committed) {
            assert.equal(vm.runInContext("state.chatError.code", context), "REQUIREMENT_INTERPRETATION_INVALID");
            assert.equal(vm.runInContext("state.chatError.details.failureKind", context), "truncated_json");
            assert.equal(vm.runInContext("JSON.stringify(state.pendingMessage) === capturedRetry", context), true);
            assert.equal(vm.runInContext("elements.messageInput.value", context), "否");
            await vm.runInContext("retryPendingMessage()", context);
            assert.equal(vm.runInContext("apiCalls[2].body === capturedRetry", context), true);
        }
        assert.equal(vm.runInContext("state.pendingMessage", context), null);
        assert.equal(vm.runInContext("state.chatError", context), null);
        assert.equal(vm.runInContext("state.chatStatus", context), "idle");
        assert.equal(vm.runInContext("state.busy", context), false);
    }
});
