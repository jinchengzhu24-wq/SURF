const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("dashboard Tab traversal wraps in both directions while the gate is open", () => {
    const html = fs.readFileSync(
        path.join(__dirname, "../../Assets/WebGLTemplates/SokobanPixel/index.html"),
        "utf8",
    );
    const start = html.indexOf("function captureDashboardPasswordKey(event)");
    const end = html.indexOf('["keydown", "keypress", "keyup"].forEach', start);
    assert(start >= 0 && end > start, "dashboard keyboard handler is present");

    let focused = null;
    const password = { focus: () => { focused = password; } };
    const cancel = {};
    const submit = { disabled: false, focus: () => { focused = submit; } };
    const modal = {
        hidden: false,
        contains: target => [password, cancel, submit].includes(target),
    };
    const context = vm.createContext({
        dashboardAccessModal: modal,
        dashboardAccessPassword: password,
        dashboardAccessCancel: cancel,
        dashboardAccessSubmit: submit,
    });
    vm.runInContext(html.slice(start, end), context);

    function pressTab(target, shiftKey = false) {
        let prevented = false;
        let stopped = false;
        focused = null;
        context.captureDashboardPasswordKey({
            type: "keydown",
            key: "Tab",
            target,
            shiftKey,
            preventDefault: () => { prevented = true; },
            stopImmediatePropagation: () => { stopped = true; },
        });
        return { prevented, stopped, focused };
    }

    assert.deepEqual(pressTab(submit), { prevented: true, stopped: true, focused: password });
    assert.deepEqual(pressTab(password, true), { prevented: true, stopped: true, focused: submit });
    assert.deepEqual(pressTab(password), { prevented: false, stopped: true, focused: null });
    assert.deepEqual(pressTab(cancel, true), { prevented: false, stopped: true, focused: null });
    assert.deepEqual(pressTab({}), { prevented: true, stopped: true, focused: password });
    assert.deepEqual(pressTab({}, true), { prevented: true, stopped: true, focused: submit });

    submit.disabled = true;
    assert.deepEqual(pressTab(password), { prevented: true, stopped: true, focused: null });
    assert.deepEqual(pressTab({}), { prevented: true, stopped: true, focused: null });
    modal.hidden = true;
    assert.deepEqual(pressTab({}), { prevented: false, stopped: false, focused: null });

    // Non-Tab keys outside modal controls (backdrop/body) must still be intercepted while modal is open
    modal.hidden = false;
    let escPrevented = false;
    let escStopped = false;
    context.captureDashboardPasswordKey({
        type: "keydown",
        key: "Escape",
        target: {},
        preventDefault: () => { escPrevented = true; },
        stopImmediatePropagation: () => { escStopped = true; },
    });
    assert.deepEqual({ escPrevented, escStopped }, { escPrevented: true, escStopped: true });

    let leakPrevented = false;
    let leakStopped = false;
    context.captureDashboardPasswordKey({
        type: "keydown",
        key: "w",
        target: {},
        preventDefault: () => { leakPrevented = true; },
        stopImmediatePropagation: () => { leakStopped = true; },
    });
    assert.deepEqual({ leakPrevented, leakStopped }, { leakPrevented: true, leakStopped: true });
});
