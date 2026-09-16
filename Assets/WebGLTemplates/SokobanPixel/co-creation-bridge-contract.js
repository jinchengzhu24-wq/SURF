(function (root, factory) {
  var contract = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = contract;
  }
  if (root) {
    root.SokobanCoCreationBridgeContract = contract;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function prepareRejectionReason(state, message) {
    if (!message || message.protocolVersion !== state.protocolVersion
      || state.activeProtocolVersion !== state.protocolVersion) {
      return "protocol_mismatch";
    }
    if (!message.sessionId || message.sessionId !== state.activeSessionId) {
      return "session_mismatch";
    }
    if (!state.unityReady) return "unity_not_ready";
    if (!state.receiverReady) return "receiver_not_ready";
    return "";
  }

  function requestRejectionReason(state, message, preparedRequest, now) {
    var prepareReason = prepareRejectionReason(state, message);
    if (prepareReason) return prepareReason;
    if (!preparedRequest) return "prepare_missing";
    if (preparedRequest.expiresAt < now) return "prepare_expired";
    if (preparedRequest.requestId !== message.prepareRequestId
      || preparedRequest.sessionId !== message.sessionId
      || preparedRequest.source !== state.source
      || preparedRequest.origin !== state.origin) {
      return "request_mismatch";
    }
    if (!message.regenerationRequestId) return "request_mismatch";
    return "";
  }

  function bridgeStateChanged(previousSessionId, previousProtocolVersion, sessionId, protocolVersion) {
    return (previousSessionId || "") !== (sessionId || "")
      || Number(previousProtocolVersion || 0) !== Number(protocolVersion || 0);
  }

  function shouldHandleSource(isKnownSource) {
    return isKnownSource === true;
  }

  return {
    prepareRejectionReason: prepareRejectionReason,
    requestRejectionReason: requestRejectionReason,
    bridgeStateChanged: bridgeStateChanged,
    shouldHandleSource: shouldHandleSource
  };
});
