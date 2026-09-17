const test = require("node:test");
const assert = require("node:assert/strict");
const contract = require("../../Assets/WebGLTemplates/SokobanPixel/co-creation-bridge-contract.js");

const source = {};
const baseState = {
  protocolVersion: 4,
  activeProtocolVersion: 4,
  activeSessionId: "session-a",
  unityReady: true,
  receiverReady: true,
  source,
  origin: "http://sokobanaidemo.top"
};

test("prepare requires a registered regeneration receiver", () => {
  const reason = contract.prepareRejectionReason(
    { ...baseState, receiverReady: false },
    { protocolVersion: 4, sessionId: "session-a" }
  );
  assert.equal(reason, "receiver_not_ready");
});

test("prepare distinguishes protocol, session and Unity failures", () => {
  assert.equal(contract.prepareRejectionReason(baseState, {
    protocolVersion: 3, sessionId: "session-a"
  }), "protocol_mismatch");
  assert.equal(contract.prepareRejectionReason(baseState, {
    protocolVersion: 4, sessionId: "session-b"
  }), "session_mismatch");
  assert.equal(contract.prepareRejectionReason({ ...baseState, unityReady: false }, {
    protocolVersion: 4, sessionId: "session-a"
  }), "unity_not_ready");
});

test("request is bound to the prepared source, session and expiry", () => {
  const prepared = {
    requestId: "prepare-a",
    sessionId: "session-a",
    source,
    origin: baseState.origin,
    expiresAt: 2000
  };
  const message = {
    protocolVersion: 4,
    sessionId: "session-a",
    prepareRequestId: "prepare-a",
    regenerationRequestId: "regeneration-a"
  };
  assert.equal(contract.requestRejectionReason(baseState, message, prepared, 1000), "");
  assert.equal(contract.requestRejectionReason(baseState, message, prepared, 2001), "prepare_expired");
  assert.equal(contract.requestRejectionReason(baseState, {
    ...message, prepareRequestId: "prepare-b"
  }, prepared, 1000), "request_mismatch");
});

test("changing session or protocol invalidates prepared state", () => {
  assert.equal(contract.bridgeStateChanged("session-a", 4, "session-a", 4), false);
  assert.equal(contract.bridgeStateChanged("session-a", 4, "session-b", 4), true);
  assert.equal(contract.bridgeStateChanged("session-a", 4, "session-a", 3), true);
});

test("unknown message sources are silently ignored before acknowledgement", () => {
  assert.equal(contract.shouldHandleSource(true), true);
  assert.equal(contract.shouldHandleSource(false), false);
});
