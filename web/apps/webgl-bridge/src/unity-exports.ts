import {
  PROTOCOL_VERSION,
  bridgeStateChanged,
  validateSafeNavigationUrl,
} from "@sokoban/ui-core";
import type { CoCreationHostController } from "./cocreation-host";
import type { DashboardGateController } from "./dashboard-gate";
import type { LobbyOverlayController } from "./lobby-overlay";
import type { BridgeState } from "./state";

export type UnitySendTarget =
  | {
      gameObject: "CoCreationPlayBootstrap";
      methodName: "ReceiveBrowserPlayRequest";
      value: string;
    }
  | {
      gameObject: "CoCreationStagePlayController";
      methodName: "ReceiveBrowserReturnToLab";
      value: "";
    }
  | {
      gameObject: "CoCreationEntryController";
      methodName: "ReceiveBrowserDraftRegenerationRequest";
      value: string;
    }
  | {
      gameObject: "OnlineLobbyController";
      methodName: "SetNativeJoinRoomCode";
      value: string;
    }
  | {
      gameObject: "OnlineLobbyController";
      methodName: "SubmitNativeJoinRoomCode";
    };

export function createUnityMessenger(state: BridgeState) {
  return function sendToUnity(target: UnitySendTarget): boolean {
    const instance = state.unityInstance;
    if (!instance) return false;
    try {
      if ("value" in target) {
        instance.SendMessage(target.gameObject, target.methodName, target.value);
      } else {
        instance.SendMessage(target.gameObject, target.methodName);
      }
      return true;
    } catch {
      return false;
    }
  };
}

export interface UnityExportsDeps {
  win?: Window;
  state: BridgeState;
  host: CoCreationHostController;
  lobbyOverlay: LobbyOverlayController;
  dashboardGate: DashboardGateController;
  isAllowedOrigin: (origin: string) => boolean;
  showErrorBanner: (message: string) => void;
}

/**
 * Registers all 9 active `window.Sokoban*` shims invoked by `BrowserNavigation.jslib`
 * plus `window.SokobanOpenDashboardGate`.
 */
export function registerUnityExports({
  win = window,
  state,
  host,
  lobbyOverlay,
  dashboardGate,
  isAllowedOrigin,
  showErrorBanner,
}: UnityExportsDeps): () => void {
  const sendCoCreationPlayBridgeReady = (ready: boolean) => {
    state.playBridgeReady = Boolean(ready);
  };

  const returnToCoCreationLab = (status: string) => {
    const frameWindow = host.getFrameWindow();
    if (
      !frameWindow ||
      frameWindow.closed ||
      !state.labOrigin ||
      !isAllowedOrigin(state.labOrigin)
    ) {
      return;
    }
    frameWindow.postMessage(
      {
        type: "sokoban:cocreation-play-return",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: state.sessionId,
        status: status || "",
      },
      state.labOrigin,
    );
    host.show();
  };

  const showCoCreationLab = (url: string, sessionId: string) => {
    if (!sessionId) {
      showErrorBanner("The co-creation lab URL or session is not allowed.");
      return;
    }
    let validatedUrl: string;
    try {
      validatedUrl = validateSafeNavigationUrl(url, {
        baseUrl: win.location.href,
      });
    } catch {
      showErrorBanner("The co-creation lab URL is invalid.");
      return;
    }
    const parsed = new URL(validatedUrl);
    if (!isAllowedOrigin(parsed.origin)) {
      showErrorBanner("The co-creation lab URL or session is not allowed.");
      return;
    }

    const sessionChanged = state.sessionId !== sessionId;
    if (sessionChanged) {
      state.preparedDraftRegeneration = null;
    }
    state.sessionId = sessionId;
    state.labOrigin = parsed.origin;
    host.open(parsed.href, sessionId, sessionChanged);
  };

  const setDraftRegenerationBridgeSession = (
    sessionId: string,
    protocolVersion: number,
  ) => {
    const nextSessionId = sessionId || "";
    const nextProtocolVersion = protocolVersion || 0;
    if (
      bridgeStateChanged(
        state.draftRegenerationSessionId,
        state.draftRegenerationProtocolVersion,
        nextSessionId,
        nextProtocolVersion,
      )
    ) {
      state.preparedDraftRegeneration = null;
    }
    state.draftRegenerationSessionId = nextSessionId;
    state.draftRegenerationProtocolVersion = nextProtocolVersion;
    state.draftRegenerationReceiverReady =
      Boolean(nextSessionId) && nextProtocolVersion === PROTOCOL_VERSION;
    if (nextSessionId) {
      state.sessionId = nextSessionId;
    }
  };

  const returnDraftRegeneration = (
    sessionId: string,
    requestId: string,
    status: string,
  ) => {
    const frameWindow = host.getFrameWindow();
    if (
      !frameWindow ||
      frameWindow.closed ||
      !state.labOrigin ||
      !isAllowedOrigin(state.labOrigin)
    ) {
      return;
    }
    frameWindow.postMessage(
      {
        type: "sokoban:cocreation-draft-regenerate-return",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: sessionId || state.sessionId,
        requestId: requestId || "",
        status: status || "failed",
      },
      state.labOrigin,
    );
    host.show();
  };

  const setLobbyOverlayVisible = (visible: boolean) => {
    lobbyOverlay.setVisible(Boolean(visible));
  };

  const setLobbyRoomCode = (roomCode: string) => {
    lobbyOverlay.setRoomCode(roomCode);
  };

  const setLobbyJoinCode = (roomCode: string) => {
    lobbyOverlay.setJoinCode(roomCode);
  };

  const setLobbyJoinInteractable = (interactable: boolean) => {
    lobbyOverlay.setJoinInteractable(Boolean(interactable));
  };

  const openDashboardGate = (url?: string) => {
    dashboardGate.open(url);
  };

  win.SokobanSetCoCreationPlayBridgeReady = sendCoCreationPlayBridgeReady;
  win.SokobanReturnToCoCreationLab = returnToCoCreationLab;
  win.SokobanShowCoCreationLab = showCoCreationLab;
  win.SokobanSetDraftRegenerationBridgeSession =
    setDraftRegenerationBridgeSession;
  win.SokobanReturnDraftRegeneration = returnDraftRegeneration;
  win.SokobanSetLobbyOverlayVisible = setLobbyOverlayVisible;
  win.SokobanSetLobbyRoomCode = setLobbyRoomCode;
  win.SokobanSetLobbyJoinCode = setLobbyJoinCode;
  win.SokobanSetLobbyJoinInteractable = setLobbyJoinInteractable;
  win.SokobanOpenDashboardGate = openDashboardGate;

  return () => {
    delete win.SokobanSetCoCreationPlayBridgeReady;
    delete win.SokobanReturnToCoCreationLab;
    delete win.SokobanShowCoCreationLab;
    delete win.SokobanSetDraftRegenerationBridgeSession;
    delete win.SokobanReturnDraftRegeneration;
    delete win.SokobanSetLobbyOverlayVisible;
    delete win.SokobanSetLobbyRoomCode;
    delete win.SokobanSetLobbyJoinCode;
    delete win.SokobanSetLobbyJoinInteractable;
    delete win.SokobanOpenDashboardGate;
  };
}
