import {
  PROTOCOL_VERSION,
  isLoopbackHost,
  parseBridgeEvent,
  validateSafeNavigationUrl,
  type CoCreationBridgeEvent,
  type DraftRegenerateReturnEvent,
  type PlayReturnEvent,
} from "@sokoban/ui-core";
import {
  DRAFT_REGENERATION_POLL_TIMEOUT_MS,
  type CocreationApiClient,
} from "../api/cocreationApi";
import type { Session } from "../types";

export const UNITY_BRIDGE_TIMEOUT_MS = 1_500;
export const DRAFT_REGENERATION_POLL_MS = 1_000;
export const DRAFT_REGENERATION_RECONNECT_MS = 2_000;
export const DRAFT_REGENERATION_MAX_POLL_MS = 180_000;

export interface PreparedDraftRegenerationHandle {
  readonly prepareRequestId: string;
  readonly hostOrigin: string;
  readonly protocolVersion: typeof PROTOCOL_VERSION;
  readonly accepted: true;
}

export interface PrepareDraftRegenerationFailure {
  readonly accepted: false;
  readonly reason: string;
}

export type PrepareDraftRegenerationResult =
  | PreparedDraftRegenerationHandle
  | PrepareDraftRegenerationFailure;

export interface DispatchDraftRegenerationResult {
  readonly received: boolean;
  readonly accepted: boolean;
  readonly reason: string;
}

export interface UnityBridgeCallbacks {
  getSessionId: () => string;
  onHostReady: (sessionId: string) => void;
  onPlayReturn: (event: PlayReturnEvent) => void;
  onDraftRegenerateReturn: (event: DraftRegenerateReturnEvent) => void;
}

export interface PollDraftRegenerationOptions {
  api: CocreationApiClient;
  sessionId: string;
  requestId: string;
  getCurrentSessionId: () => string;
  isLanguageLocked: () => boolean;
  onSessionUpdate: (session: Session) => void;
  onEnteredWorkspace: (session: Session) => void | Promise<void>;
  onReconnecting: () => void;
  onTerminalFailure: (failureCode: string | null | undefined) => void;
  onComplete: () => void;
  pollIntervalMs?: number;
  reconnectIntervalMs?: number;
  maxDurationMs?: number;
}

function isAllowedHostOrigin(origin: string, currentLocation: Location | undefined): boolean {
  if (!origin || origin === "null") return false;
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return false;
  }
  if (currentLocation?.origin && origin === currentLocation.origin) {
    return true;
  }
  if (origin === "https://sokobanaidemo.top" || origin === "https://www.sokobanaidemo.top") {
    return true;
  }
  if (currentLocation?.hostname && isLoopbackHost(currentLocation.hostname) && isLoopbackHost(parsed.hostname)) {
    return true;
  }
  return false;
}

function makeRpcId(prefix: string): string {
  const random =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID().replaceAll("-", "")
      : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${random}`.slice(0, 64);
}

function delayWithAbort(ms: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const onAbort = () => {
      clearTimeout(timerId);
      resolve(false);
    };
    const timerId = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function createUnityBridge(
  callbacks: UnityBridgeCallbacks,
  win: Window = window,
) {
  let hostOrigin = "";
  let hostSessionId = "";
  let hostReady = false;
  let destroyed = false;

  const pendingWaiters = new Set<{
    match: (event: CoCreationBridgeEvent, origin: string) => boolean;
    resolve: (event: CoCreationBridgeEvent | null) => void;
    timerId: ReturnType<typeof setTimeout>;
  }>();

  let activePollController: AbortController | null = null;
  let activePollRequestId = "";
  let activePollPromise: Promise<void> | null = null;

  function isEmbeddedParent(): boolean {
    return !destroyed && Boolean(win.parent && win.parent !== win);
  }

  function hasEmbeddedFormalHost(sessionId = callbacks.getSessionId()): boolean {
    return (
      isEmbeddedParent() &&
      hostReady &&
      Boolean(hostOrigin) &&
      Boolean(sessionId) &&
      hostSessionId === sessionId
    );
  }

  type SessionScopedBridgeEvent = Extract<CoCreationBridgeEvent, { sessionId: string }>;

  function postToHost(payload: SessionScopedBridgeEvent): boolean {
    if (!hasEmbeddedFormalHost(payload.sessionId)) return false;
    try {
      win.parent.postMessage(payload, hostOrigin);
      return true;
    } catch {
      return false;
    }
  }

  function announceLabReady(sessionId: string): boolean {
    if (!hasEmbeddedFormalHost(sessionId)) return false;
    return postToHost({
      type: "sokoban:cocreation-lab-ready",
      protocolVersion: PROTOCOL_VERSION,
      sessionId,
    });
  }

  function requestHostView(
    type: "sokoban:cocreation-show-unity" | "sokoban:cocreation-show-lab",
    sessionId: string,
    requestId = "",
  ): boolean {
    if (!hasEmbeddedFormalHost(sessionId)) return false;
    return postToHost({
      type,
      protocolVersion: PROTOCOL_VERSION,
      sessionId,
      ...(requestId ? { requestId } : {}),
    });
  }

  function waitForEvent(
    match: (event: CoCreationBridgeEvent, origin: string) => boolean,
    timeoutMs = UNITY_BRIDGE_TIMEOUT_MS,
  ): Promise<CoCreationBridgeEvent | null> {
    if (destroyed) return Promise.resolve(null);
    return new Promise((resolve) => {
      const entry = {
        match,
        resolve,
        timerId: globalThis.setTimeout(() => {
          pendingWaiters.delete(entry);
          resolve(null);
        }, timeoutMs),
      };
      pendingWaiters.add(entry);
    });
  }

  const handleMessage = (event: MessageEvent): void => {
    if (destroyed || !isEmbeddedParent()) return;
    if (event.source !== win.parent) return;

    const parsed = parseBridgeEvent(event.data);
    if (!parsed) return;

    if (parsed.type === "sokoban:cocreation-host-init") {
      if (
        !isAllowedHostOrigin(event.origin, win.location) ||
        parsed.protocolVersion !== PROTOCOL_VERSION
      ) {
        return;
      }
      const currentSessionId = callbacks.getSessionId();
      if (!currentSessionId || parsed.sessionId !== currentSessionId) {
        return;
      }
      hostOrigin = event.origin;
      hostSessionId = parsed.sessionId;
      hostReady = true;
      announceLabReady(parsed.sessionId);
      callbacks.onHostReady(parsed.sessionId);
      return;
    }

    if (!hostReady || event.origin !== hostOrigin) return;
    if (parsed.protocolVersion !== PROTOCOL_VERSION) return;

    for (const waiter of Array.from(pendingWaiters)) {
      if (waiter.match(parsed, event.origin)) {
        clearTimeout(waiter.timerId);
        pendingWaiters.delete(waiter);
        waiter.resolve(parsed);
      }
    }

    const currentSessionId = callbacks.getSessionId();

    if (parsed.type === "sokoban:cocreation-play-return") {
      if (parsed.sessionId !== currentSessionId) return;
      callbacks.onPlayReturn(parsed);
      return;
    }

    if (parsed.type === "sokoban:cocreation-draft-regenerate-return") {
      if (parsed.sessionId !== currentSessionId) return;
      callbacks.onDraftRegenerateReturn(parsed);
    }
  };

  win.addEventListener("message", handleMessage);

  function validatePlayUrl(playUrl: string): string {
    const allowedOrigins = Array.from(
      new Set([win.location?.origin, hostOrigin].filter((value): value is string => Boolean(value))),
    );
    return validateSafeNavigationUrl(playUrl, {
      baseUrl: win.location?.href,
      allowedOrigins: allowedOrigins.length > 0 ? allowedOrigins : undefined,
      allowRelative: true,
    });
  }

  async function requestPlayInUnity(sessionId: string, playUrl: string): Promise<boolean> {
    if (!hasEmbeddedFormalHost(sessionId)) return false;
    const safeUrl = validatePlayUrl(playUrl);
    const requestId = makeRpcId("unity_play");

    const ackPromise = waitForEvent(
      (event, origin) =>
        origin === hostOrigin &&
        event.type === "sokoban:cocreation-play-ack" &&
        event.requestId === requestId &&
        event.protocolVersion === PROTOCOL_VERSION,
      UNITY_BRIDGE_TIMEOUT_MS,
    );

    const posted = postToHost({
      type: "sokoban:cocreation-play-request",
      requestId,
      sessionId,
      playUrl: safeUrl,
      protocolVersion: PROTOCOL_VERSION,
    });
    if (!posted) return false;

    const ack = await ackPromise;
    const accepted = ack?.type === "sokoban:cocreation-play-ack" && ack.accepted === true;
    if (!accepted) {
      requestHostView("sokoban:cocreation-show-lab", sessionId);
    }
    return accepted;
  }

  function navigateToPlayUrl(playUrl: string): void {
    const safeUrl = validatePlayUrl(playUrl);
    win.location.assign(safeUrl);
  }

  async function prepareDraftRegeneration(
    sessionId: string,
  ): Promise<PrepareDraftRegenerationResult> {
    if (!hasEmbeddedFormalHost(sessionId)) {
      return { accepted: false, reason: "unity_not_ready" };
    }
    const prepareRequestId = makeRpcId("unity_draft_regeneration_prepare");
    const ackPromise = waitForEvent(
      (event, origin) =>
        origin === hostOrigin &&
        event.type === "sokoban:cocreation-draft-regenerate-prepare-ack" &&
        event.requestId === prepareRequestId &&
        event.sessionId === sessionId,
      UNITY_BRIDGE_TIMEOUT_MS,
    );

    const posted = postToHost({
      type: "sokoban:cocreation-draft-regenerate-prepare",
      requestId: prepareRequestId,
      sessionId,
      protocolVersion: PROTOCOL_VERSION,
    });
    if (!posted) {
      return { accepted: false, reason: "unity_not_ready" };
    }
    requestHostView("sokoban:cocreation-show-unity", sessionId);

    const ack = await ackPromise;
    if (!ack || ack.type !== "sokoban:cocreation-draft-regenerate-prepare-ack") {
      return { accepted: false, reason: "unity_not_ready" };
    }
    if (!ack.accepted || ack.protocolVersion !== PROTOCOL_VERSION) {
      return {
        accepted: false,
        reason:
          ack.reason ||
          (ack.protocolVersion === PROTOCOL_VERSION ? "receiver_not_ready" : "protocol_mismatch"),
      };
    }

    return {
      accepted: true,
      prepareRequestId,
      hostOrigin,
      protocolVersion: PROTOCOL_VERSION,
    };
  }

  async function sendDraftRegenerationRequest(
    prepared: PreparedDraftRegenerationHandle,
    sessionId: string,
    regenerationRequestId: string,
  ): Promise<DispatchDraftRegenerationResult> {
    if (!hasEmbeddedFormalHost(sessionId)) {
      return { received: true, accepted: false, reason: "unity_not_ready" };
    }
    const bridgeRequestId = makeRpcId("unity_draft_regeneration");
    const ackPromise = waitForEvent(
      (event, origin) =>
        origin === prepared.hostOrigin &&
        event.type === "sokoban:cocreation-draft-regenerate-ack" &&
        event.requestId === bridgeRequestId &&
        event.sessionId === sessionId &&
        event.regenerationRequestId === regenerationRequestId,
      UNITY_BRIDGE_TIMEOUT_MS,
    );

    const posted = postToHost({
      type: "sokoban:cocreation-draft-regenerate-request",
      requestId: bridgeRequestId,
      prepareRequestId: prepared.prepareRequestId,
      protocolVersion: prepared.protocolVersion,
      sessionId,
      regenerationRequestId,
    });
    if (!posted) {
      return { received: true, accepted: false, reason: "unity_not_ready" };
    }

    const ack = await ackPromise;
    if (!ack || ack.type !== "sokoban:cocreation-draft-regenerate-ack") {
      return { received: false, accepted: false, reason: "" };
    }
    return {
      received: true,
      accepted: ack.accepted === true && ack.protocolVersion === prepared.protocolVersion,
      reason: ack.reason || "",
    };
  }

  function abortDraftRegeneration(
    prepared: PreparedDraftRegenerationHandle,
    sessionId: string,
  ): void {
    if (!hasEmbeddedFormalHost(sessionId)) return;
    postToHost({
      type: "sokoban:cocreation-draft-regenerate-abort",
      protocolVersion: prepared.protocolVersion,
      sessionId,
      prepareRequestId: prepared.prepareRequestId,
    });
    requestHostView("sokoban:cocreation-show-lab", sessionId);
  }

  function stopDraftRegenerationPolling(): void {
    if (activePollController) {
      activePollController.abort();
      activePollController = null;
    }
    activePollRequestId = "";
    activePollPromise = null;
  }

  function pollDraftRegeneration(options: PollDraftRegenerationOptions): Promise<void> {
    const {
      api,
      sessionId: capturedSessionId,
      requestId,
      getCurrentSessionId,
      isLanguageLocked,
      onSessionUpdate,
      onEnteredWorkspace,
      onReconnecting,
      onTerminalFailure,
      onComplete,
      pollIntervalMs = DRAFT_REGENERATION_POLL_MS,
      reconnectIntervalMs = DRAFT_REGENERATION_RECONNECT_MS,
      maxDurationMs = DRAFT_REGENERATION_MAX_POLL_MS,
    } = options;

    if (!requestId || destroyed) return Promise.resolve();
    if (activePollPromise && activePollRequestId === requestId) {
      return activePollPromise;
    }

    stopDraftRegenerationPolling();
    const controller = new AbortController();
    activePollController = controller;
    activePollRequestId = requestId;

    const startedAt = Date.now();

    const run = async (): Promise<void> => {
      while (!controller.signal.aborted && !destroyed) {
        if (getCurrentSessionId() !== capturedSessionId || isLanguageLocked()) {
          return;
        }
        if (Date.now() - startedAt >= maxDurationMs) {
          onTerminalFailure("generation_timed_out");
          return;
        }

        const waited = await delayWithAbort(pollIntervalMs, controller.signal);
        if (!waited || getCurrentSessionId() !== capturedSessionId || isLanguageLocked()) {
          return;
        }

        let latest: Session;
        try {
          latest = await api.getSession(capturedSessionId, {
            timeoutMs: DRAFT_REGENERATION_POLL_TIMEOUT_MS,
            signal: controller.signal,
          });
        } catch {
          if (controller.signal.aborted || getCurrentSessionId() !== capturedSessionId) {
            return;
          }
          onReconnecting();
          requestHostView("sokoban:cocreation-show-lab", capturedSessionId, requestId);
          await delayWithAbort(reconnectIntervalMs, controller.signal);
          continue;
        }

        if (controller.signal.aborted || getCurrentSessionId() !== capturedSessionId) {
          return;
        }

        if (latest.draftPreview?.regenerationRequestId !== requestId) {
          onSessionUpdate(latest);
          if (latest.languageLocked) {
            await onEnteredWorkspace(latest);
          }
          return;
        }

        onSessionUpdate(latest);
        const status = latest.draftPreview?.regenerationStatus;
        if (status !== "pending" && status !== "claimed") {
          if (status === "failed" || status === "cancelled" || status === "timed_out") {
            onTerminalFailure(latest.draftPreview?.failureCode);
          } else {
            onComplete();
          }
          return;
        }
      }
    };

    const promise = run().finally(() => {
      if (activePollController === controller) {
        activePollController = null;
        activePollRequestId = "";
        activePollPromise = null;
      }
    });
    activePollPromise = promise;
    return promise;
  }

  function destroy(): void {
    destroyed = true;
    win.removeEventListener("message", handleMessage);
    stopDraftRegenerationPolling();
    for (const waiter of pendingWaiters) {
      clearTimeout(waiter.timerId);
      waiter.resolve(null);
    }
    pendingWaiters.clear();
    hostReady = false;
    hostOrigin = "";
    hostSessionId = "";
  }

  return {
    get protocolVersion() {
      return PROTOCOL_VERSION;
    },
    get activePollRequestId() {
      return activePollRequestId;
    },
    isEmbeddedParent,
    hasEmbeddedFormalHost,
    announceLabReady,
    requestHostView,
    requestPlayInUnity,
    navigateToPlayUrl,
    prepareDraftRegeneration,
    sendDraftRegenerationRequest,
    abortDraftRegeneration,
    pollDraftRegeneration,
    stopDraftRegenerationPolling,
    destroy,
  };
}

export type UnityBridgeClient = ReturnType<typeof createUnityBridge>;
