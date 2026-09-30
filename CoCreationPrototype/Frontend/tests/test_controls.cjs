const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadApp() {
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
        submitPendingMessage = async () => {};
    `, context);
    return { context, created };
}

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
