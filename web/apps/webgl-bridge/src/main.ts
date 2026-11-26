import { createCoCreationHost } from "./cocreation-host";
import { createDashboardGate } from "./dashboard-gate";
import { createLobbyOverlay } from "./lobby-overlay";
import { createBridgeRouter } from "./router";
import { createInitialBridgeState } from "./state";
import { createUnityMessenger, registerUnityExports } from "./unity-exports";
import { createUnityShell } from "./unity-shell";

function requireElement<T extends Element>(selector: string, doc: Document): T {
  const el = doc.querySelector<T>(selector);
  if (!el) {
    throw new Error(`Required Unity template element not found: ${selector}`);
  }
  return el;
}

export function bootstrapWebGLBridge(
  win: Window = window,
  doc: Document = document,
): () => void {
  const state = createInitialBridgeState();
  win.SokobanUnityInstance = null;

  const container = requireElement<HTMLElement>("#unity-container", doc);
  const canvas = requireElement<HTMLCanvasElement>("#unity-canvas", doc);
  const loadingBar = requireElement<HTMLElement>("#unity-loading-bar", doc);
  const progressBarEmpty = requireElement<HTMLElement>(
    "#unity-progress-bar-empty",
    doc,
  );
  const progressBarFull = requireElement<HTMLElement>(
    "#unity-progress-bar-full",
    doc,
  );
  const loadingPercent = requireElement<HTMLElement>(
    "#unity-loading-percent",
    doc,
  );
  const fullscreenButton = requireElement<HTMLButtonElement>(
    "#unity-fullscreen-button",
    doc,
  );
  const warningBanner = requireElement<HTMLElement>("#unity-warning", doc);

  const coCreationHostEl = requireElement<HTMLElement>("#cocreation-host", doc);
  const coCreationFrame = requireElement<HTMLIFrameElement>(
    "#cocreation-frame",
    doc,
  );
  const coCreationLoading = requireElement<HTMLElement>(
    "#cocreation-host-loading",
    doc,
  );
  const coCreationError = requireElement<HTMLElement>(
    "#cocreation-host-error",
    doc,
  );
  const coCreationRetry = requireElement<HTMLButtonElement>(
    "#cocreation-retry",
    doc,
  );
  const coCreationReturn = doc.querySelector<HTMLButtonElement>(
    "#cocreation-return-unity",
  );

  const lobbyOverlayEl = requireElement<HTMLElement>(
    "#sokoban-lobby-native-overlay",
    doc,
  );
  const lobbyGeneratedInput = requireElement<HTMLInputElement>(
    "#sokoban-lobby-generated-room-code",
    doc,
  );
  const lobbyJoinInput = requireElement<HTMLInputElement>(
    "#sokoban-lobby-join-room-code",
    doc,
  );

  const dashboardDialog = requireElement<HTMLDialogElement>(
    "#dashboard-access-modal",
    doc,
  );
  const dashboardForm = requireElement<HTMLFormElement>(
    "#dashboard-access-form",
    doc,
  );
  const dashboardPassword = requireElement<HTMLInputElement>(
    "#dashboard-access-password",
    doc,
  );
  const dashboardStatus = requireElement<HTMLElement>(
    "#dashboard-access-status",
    doc,
  );
  const dashboardCancel = requireElement<HTMLButtonElement>(
    "#dashboard-access-cancel",
    doc,
  );
  const dashboardSubmit = requireElement<HTMLButtonElement>(
    "#dashboard-access-submit",
    doc,
  );
  const dashboardLinks = doc.querySelectorAll<HTMLAnchorElement>(
    "[data-dashboard-link]",
  );

  const sendToUnity = createUnityMessenger(state);

  const shell = createUnityShell({
    elements: {
      container,
      canvas,
      loadingBar,
      progressBarEmpty,
      progressBarFull,
      loadingPercent,
      fullscreenButton,
      warningBanner,
    },
    state,
    config: win.__SOKOBAN_UNITY_CONFIG__,
    win,
    doc,
  });

  const host = createCoCreationHost({
    elements: {
      host: coCreationHostEl,
      frame: coCreationFrame,
      loading: coCreationLoading,
      error: coCreationError,
      retryButton: coCreationRetry,
      returnButton: coCreationReturn,
      canvas,
    },
    win,
    doc,
    onSessionReset: () => {
      state.preparedDraftRegeneration = null;
    },
  });

  const router = createBridgeRouter({
    win,
    state,
    host,
    sendToUnity,
  });

  const lobbyOverlay = createLobbyOverlay({
    elements: {
      overlay: lobbyOverlayEl,
      generatedInput: lobbyGeneratedInput,
      joinInput: lobbyJoinInput,
      canvas,
    },
    sendToUnity,
    win,
    doc,
  });

  const dashboardGate = createDashboardGate({
    elements: {
      dialog: dashboardDialog,
      form: dashboardForm,
      passwordInput: dashboardPassword,
      statusText: dashboardStatus,
      cancelButton: dashboardCancel,
      submitButton: dashboardSubmit,
      canvas,
      triggerLinks: dashboardLinks,
    },
    win,
    doc,
  });

  const unregisterExports = registerUnityExports({
    win,
    state,
    host,
    lobbyOverlay,
    dashboardGate,
    isAllowedOrigin: router.isAllowedOrigin,
    showErrorBanner: (message) => shell.showBanner(message, "error"),
  });

  return () => {
    unregisterExports();
    dashboardGate.destroy();
    lobbyOverlay.destroy();
    router.destroy();
    host.destroy();
    shell.destroy();
  };
}

if (typeof document !== "undefined" && document.querySelector("#unity-canvas")) {
  bootstrapWebGLBridge(window, document);
}
