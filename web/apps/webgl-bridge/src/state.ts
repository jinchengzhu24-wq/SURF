import {
  PROTOCOL_VERSION,
  type PreparedDraftRegeneration,
} from "@sokoban/ui-core";

export interface UnityInstance {
  SendMessage(
    gameObject: string,
    methodName: string,
    value?: string | number,
  ): void;
  SetFullscreen(fullscreen: number): void;
}

export interface UnityRuntimeConfig {
  buildUrl: string;
  buildVersion?: string;
  loaderUrl: string;
  dataUrl: string;
  frameworkUrl: string;
  workerUrl?: string;
  codeUrl?: string;
  memoryUrl?: string;
  symbolsUrl?: string;
  backgroundUrl?: string;
  streamingAssetsUrl: string;
  companyName: string;
  productName: string;
  productVersion: string;
}

export type CreateUnityInstanceFn = (
  canvas: HTMLCanvasElement,
  config: Record<string, unknown>,
  onProgress?: (progress: number) => void,
) => Promise<UnityInstance>;

export interface BridgeState {
  unityInstance: UnityInstance | null;
  playBridgeReady: boolean;
  /** Unified active Co-Creation session ID shared across host, play, and draft regeneration. */
  sessionId: string;
  /** Session ID registered by CoCreationEntryController while the entry scene is mounted. */
  draftRegenerationSessionId: string;
  draftRegenerationProtocolVersion: number;
  draftRegenerationReceiverReady: boolean;
  preparedDraftRegeneration: PreparedDraftRegeneration | null;
  labOrigin: string;
}

export function createInitialBridgeState(): BridgeState {
  return {
    unityInstance: null,
    playBridgeReady: false,
    sessionId: "",
    draftRegenerationSessionId: "",
    draftRegenerationProtocolVersion: 0,
    draftRegenerationReceiverReady: false,
    preparedDraftRegeneration: null,
    labOrigin: "",
  };
}

export const CO_CREATION_HOST_RELEASE = "iframe-host-v5-20261126-ts";
export const BRIDGE_PROTOCOL_VERSION = PROTOCOL_VERSION;

export function exitCanvasFullscreenIfNeeded(
  canvas: HTMLCanvasElement | null,
  doc: Document = document,
): void {
  if (!canvas) return;
  const fullscreenElement =
    doc.fullscreenElement ||
    (doc as Document & { webkitFullscreenElement?: Element | null })
      .webkitFullscreenElement;
  if (fullscreenElement === canvas && typeof doc.exitFullscreen === "function") {
    try {
      const result = doc.exitFullscreen();
      if (result && typeof result.catch === "function") {
        result.catch(() => {});
      }
    } catch {
      // Ignore fullscreen exit errors if document is no longer active.
    }
  }
}

declare global {
  interface Window {
    __SOKOBAN_UNITY_CONFIG__?: UnityRuntimeConfig;
    createUnityInstance?: CreateUnityInstanceFn;
    SokobanUnityInstance?: UnityInstance | null;
    SokobanSetCoCreationPlayBridgeReady?: (ready: boolean) => void;
    SokobanReturnToCoCreationLab?: (status: string) => void;
    SokobanShowCoCreationLab?: (url: string, sessionId: string) => void;
    SokobanSetDraftRegenerationBridgeSession?: (
      sessionId: string,
      protocolVersion: number,
    ) => void;
    SokobanReturnDraftRegeneration?: (
      sessionId: string,
      requestId: string,
      status: string,
    ) => void;
    SokobanSetLobbyOverlayVisible?: (visible: boolean) => void;
    SokobanSetLobbyRoomCode?: (roomCode: string) => void;
    SokobanSetLobbyJoinCode?: (roomCode: string) => void;
    SokobanSetLobbyJoinInteractable?: (interactable: boolean) => void;
    SokobanOpenDashboardGate?: (url?: string) => void;
  }
}
