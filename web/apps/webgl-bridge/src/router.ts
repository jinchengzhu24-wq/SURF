import {
  PROTOCOL_VERSION,
  isLoopbackHost,
  isSafeNavigationUrl,
  parseBridgeEvent,
  prepareRejectionReason,
  requestRejectionReason,
  shouldHandleSource,
} from "@sokoban/ui-core";
import type { CoCreationHostController } from "./cocreation-host";
import type { BridgeState } from "./state";
import type { UnitySendTarget } from "./unity-exports";

const PRODUCTION_ORIGINS = [
  "https://sokobanaidemo.top",
  "https://www.sokobanaidemo.top",
] as const;

const LOCAL_COCREATION_ORIGINS = [
  "http://127.0.0.1:8000",
  "http://localhost:8000",
] as const;

export const PREPARE_TTL_MS = 30_000;

export function buildAllowedCoCreationOrigins(
  loc: Pick<Location, "origin" | "hostname">,
): string[] {
  const origins = new Set<string>(PRODUCTION_ORIGINS);
  if (loc.origin && loc.origin !== "null") {
    origins.add(loc.origin);
  }
  if (isLoopbackHost(loc.hostname)) {
    for (const localOrigin of LOCAL_COCREATION_ORIGINS) {
      origins.add(localOrigin);
    }
  }
  return Array.from(origins);
}

export function isAllowedCoCreationOrigin(
  origin: string,
  allowedOrigins: readonly string[],
): boolean {
  if (!origin || origin === "null") {
    return false;
  }
  return allowedOrigins.includes(origin);
}

export interface BridgeRouterOptions {
  win?: Window;
  state: BridgeState;
  host: CoCreationHostController;
  sendToUnity: (target: UnitySendTarget) => boolean;
  allowedOrigins?: readonly string[];
  now?: () => number;
}

export interface BridgeRouterController {
  isAllowedOrigin: (origin: string) => boolean;
  destroy: () => void;
}

/**
 * Single `window.addEventListener("message", ...)` dispatcher for all
 * `sokoban:cocreation-*` iframe bridge events. Rejects `"null"` origins and
 * non-iframe sources.
 */
export function createBridgeRouter({
  win = window,
  state,
  host,
  sendToUnity,
  allowedOrigins = buildAllowedCoCreationOrigins(win.location),
  now = () => Date.now(),
}: BridgeRouterOptions): BridgeRouterController {
  const isAllowedOrigin = (origin: string) =>
    isAllowedCoCreationOrigin(origin, allowedOrigins);

  const isEmbeddedSource = (source: MessageEventSource | null): boolean => {
    const frameWindow = host.getFrameWindow();
    return Boolean(frameWindow && source === frameWindow);
  };

  const handleMessage = (event: MessageEvent): void => {
    if (!isAllowedOrigin(event.origin)) return;
    if (!shouldHandleSource(isEmbeddedSource(event.source))) return;

    const message = parseBridgeEvent(event.data);
    if (!message) return;

    const frameWindow = host.getFrameWindow();
    if (!frameWindow) return;

    switch (message.type) {
      case "sokoban:cocreation-lab-ready": {
        if (
          message.protocolVersion !== PROTOCOL_VERSION ||
          message.sessionId !== state.sessionId
        ) {
          return;
        }
        state.labOrigin = event.origin;
        host.markReady();
        return;
      }

      case "sokoban:cocreation-show-unity": {
        if (
          message.protocolVersion !== PROTOCOL_VERSION ||
          message.sessionId !== state.sessionId
        ) {
          return;
        }
        host.park();
        return;
      }

      case "sokoban:cocreation-show-lab": {
        if (
          message.protocolVersion !== PROTOCOL_VERSION ||
          message.sessionId !== state.sessionId
        ) {
          return;
        }
        host.show();
        sendToUnity({
          gameObject: "CoCreationStagePlayController",
          methodName: "ReceiveBrowserReturnToLab",
          value: "",
        });
        return;
      }

      case "sokoban:cocreation-play-request": {
        const playUrlSafe = isSafeNavigationUrl(message.playUrl, {
          baseUrl: win.location.href,
          allowedOrigins,
        });
        let accepted =
          state.playBridgeReady &&
          Boolean(state.unityInstance) &&
          playUrlSafe &&
          message.protocolVersion === PROTOCOL_VERSION &&
          message.sessionId === state.sessionId;

        if (accepted) {
          state.labOrigin = event.origin;
          const sent = sendToUnity({
            gameObject: "CoCreationPlayBootstrap",
            methodName: "ReceiveBrowserPlayRequest",
            value: JSON.stringify({
              sessionId: message.sessionId,
              playUrl: message.playUrl,
            }),
          });
          if (sent) {
            host.park();
          } else {
            accepted = false;
          }
        }

        frameWindow.postMessage(
          {
            type: "sokoban:cocreation-play-ack",
            requestId: message.requestId,
            accepted,
            protocolVersion: PROTOCOL_VERSION,
          },
          event.origin,
        );
        return;
      }

      case "sokoban:cocreation-draft-regenerate-prepare": {
        const reason = prepareRejectionReason(
          {
            protocolVersion: PROTOCOL_VERSION,
            activeProtocolVersion: state.draftRegenerationProtocolVersion,
            activeSessionId: state.draftRegenerationSessionId,
            unityReady: Boolean(state.unityInstance),
            receiverReady: state.draftRegenerationReceiverReady,
          },
          message,
        );
        const accepted = !reason;
        if (accepted) {
          state.preparedDraftRegeneration = {
            requestId: message.requestId,
            sessionId: message.sessionId,
            source: event.source,
            origin: event.origin,
            expiresAt: now() + PREPARE_TTL_MS,
          };
          state.labOrigin = event.origin;
          state.sessionId = message.sessionId;
          host.park();
        }

        frameWindow.postMessage(
          {
            type: "sokoban:cocreation-draft-regenerate-prepare-ack",
            requestId: message.requestId,
            sessionId: message.sessionId,
            accepted,
            reason,
            protocolVersion: PROTOCOL_VERSION,
          },
          event.origin,
        );
        return;
      }

      case "sokoban:cocreation-draft-regenerate-request": {
        let reason = requestRejectionReason(
          {
            protocolVersion: PROTOCOL_VERSION,
            activeProtocolVersion: state.draftRegenerationProtocolVersion,
            activeSessionId: state.draftRegenerationSessionId,
            unityReady: Boolean(state.unityInstance),
            receiverReady: state.draftRegenerationReceiverReady,
            source: event.source,
            origin: event.origin,
          },
          message,
          state.preparedDraftRegeneration,
          now(),
        );
        let accepted = !reason;
        if (accepted) {
          state.labOrigin = event.origin;
          state.sessionId = message.sessionId;
          const sent = sendToUnity({
            gameObject: "CoCreationEntryController",
            methodName: "ReceiveBrowserDraftRegenerationRequest",
            value: JSON.stringify({
              sessionId: message.sessionId,
              requestId: message.regenerationRequestId,
            }),
          });
          if (sent) {
            host.park();
          } else {
            accepted = false;
            reason = "receiver_not_ready";
          }
        }

        if (
          state.preparedDraftRegeneration &&
          state.preparedDraftRegeneration.requestId === message.prepareRequestId &&
          state.preparedDraftRegeneration.source === event.source &&
          state.preparedDraftRegeneration.origin === event.origin
        ) {
          state.preparedDraftRegeneration = null;
        }

        frameWindow.postMessage(
          {
            type: "sokoban:cocreation-draft-regenerate-ack",
            requestId: message.requestId,
            sessionId: message.sessionId,
            regenerationRequestId: message.regenerationRequestId || "",
            accepted,
            reason,
            protocolVersion: PROTOCOL_VERSION,
          },
          event.origin,
        );
        return;
      }

      case "sokoban:cocreation-draft-regenerate-abort": {
        if (message.protocolVersion !== PROTOCOL_VERSION) return;
        const prepared = state.preparedDraftRegeneration;
        if (
          prepared &&
          prepared.source === event.source &&
          prepared.origin === event.origin &&
          prepared.sessionId === message.sessionId &&
          prepared.requestId === message.prepareRequestId
        ) {
          state.preparedDraftRegeneration = null;
          host.show();
        }
        return;
      }

      default:
        return;
    }
  };

  win.addEventListener("message", handleMessage);

  return {
    isAllowedOrigin,
    destroy(): void {
      win.removeEventListener("message", handleMessage);
    },
  };
}
