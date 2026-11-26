import { PROTOCOL_VERSION } from "@sokoban/ui-core";
import {
  CO_CREATION_HOST_RELEASE,
  exitCanvasFullscreenIfNeeded,
} from "./state";

export const HANDSHAKE_INTERVAL_MS = 250;
export const HANDSHAKE_TIMEOUT_MS = 12_000;
export const OUTER_LOAD_TIMEOUT_MS = 20_000;

export interface CoCreationHostElements {
  host: HTMLElement;
  frame: HTMLIFrameElement;
  loading: HTMLElement;
  error: HTMLElement;
  retryButton: HTMLButtonElement;
  returnButton: HTMLButtonElement | null;
  canvas: HTMLCanvasElement;
}

export interface CoCreationHostOptions {
  elements: CoCreationHostElements;
  win?: Window;
  doc?: Document;
  onSessionReset?: () => void;
  handshakeIntervalMs?: number;
  handshakeTimeoutMs?: number;
  outerLoadTimeoutMs?: number;
}

export interface CoCreationHostController {
  open(url: string, sessionId: string, sessionChanged: boolean): void;
  show(): void;
  park(): void;
  markReady(): void;
  isReady(): boolean;
  getFrameWindow(): Window | null;
  destroy(): void;
}

export function canonicalCoCreationFrameUrl(
  value: string,
  baseUrl: string,
): string {
  try {
    const targetUrl = new URL(value, baseUrl);
    targetUrl.searchParams.delete("workspaceReload");
    const hashParams = new URLSearchParams(
      String(targetUrl.hash || "").replace(/^#/, ""),
    );
    const sessionParam = hashParams.get("session");
    if (sessionParam) {
      return `${targetUrl.origin}${targetUrl.pathname}#session=${sessionParam}`;
    }
    return targetUrl.href;
  } catch {
    return "";
  }
}

export function createCoCreationHost({
  elements,
  win = window,
  doc = document,
  onSessionReset,
  handshakeIntervalMs = HANDSHAKE_INTERVAL_MS,
  handshakeTimeoutMs = HANDSHAKE_TIMEOUT_MS,
  outerLoadTimeoutMs = OUTER_LOAD_TIMEOUT_MS,
}: CoCreationHostOptions): CoCreationHostController {
  let currentFrame = elements.frame;
  let hostUrl = "";
  let currentSessionId = "";
  let hostReady = false;
  let frameLoaded = false;
  let navigationToken = 0;
  let reloadSequence = 0;
  let handshakeTimerId = 0;
  let loadTimerId = 0;
  let frameAbort: AbortController | null = null;
  const hostAbort = new AbortController();

  function stopTimers(): void {
    if (handshakeTimerId) {
      win.clearInterval(handshakeTimerId);
      handshakeTimerId = 0;
    }
    if (loadTimerId) {
      win.clearTimeout(loadTimerId);
      loadTimerId = 0;
    }
  }

  function showErrorState(): void {
    stopTimers();
    elements.loading.hidden = true;
    elements.error.hidden = false;
  }

  function postHostInit(): void {
    const targetWindow = currentFrame.contentWindow;
    if (!targetWindow || !currentSessionId) return;
    let targetOrigin: string;
    try {
      targetOrigin = new URL(
        currentFrame.src || hostUrl,
        win.location.href,
      ).origin;
    } catch {
      return;
    }
    if (!targetOrigin || targetOrigin === "null") return;
    targetWindow.postMessage(
      {
        type: "sokoban:cocreation-host-init",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: currentSessionId,
      },
      targetOrigin,
    );
  }

  function startHandshake(): void {
    stopTimers();
    if (hostReady || !frameLoaded) return;
    const startedAt = Date.now();
    handshakeTimerId = win.setInterval(() => {
      if (hostReady) {
        stopTimers();
        return;
      }
      if (Date.now() - startedAt >= handshakeTimeoutMs) {
        showErrorState();
        return;
      }
      postHostInit();
    }, handshakeIntervalMs);
    postHostInit();
  }

  function buildFrameUrl(forceReload: boolean): URL {
    const targetUrl = new URL(hostUrl, win.location.href);
    targetUrl.searchParams.set("hostRelease", CO_CREATION_HOST_RELEASE);
    if (forceReload) {
      reloadSequence += 1;
      targetUrl.searchParams.set(
        "workspaceReload",
        `${Date.now().toString(36)}-${reloadSequence}`,
      );
    } else {
      targetUrl.searchParams.delete("workspaceReload");
    }
    return targetUrl;
  }

  function mountFrame(forceReload: boolean): void {
    if (!hostUrl) return;
    stopTimers();
    frameAbort?.abort();
    frameAbort = new AbortController();

    navigationToken += 1;
    const activeToken = navigationToken;

    onSessionReset?.();
    hostReady = false;
    frameLoaded = false;
    elements.loading.hidden = false;
    elements.error.hidden = true;

    const replacement = currentFrame.cloneNode(false) as HTMLIFrameElement;
    replacement.removeAttribute("src");
    currentFrame.replaceWith(replacement);
    currentFrame = replacement;

    const { signal } = frameAbort;

    currentFrame.addEventListener(
      "load",
      () => {
        if (activeToken !== navigationToken || !currentSessionId) return;
        if (loadTimerId) {
          win.clearTimeout(loadTimerId);
          loadTimerId = 0;
        }
        frameLoaded = true;
        if (hostReady) {
          elements.loading.hidden = true;
          elements.error.hidden = true;
          return;
        }
        elements.loading.hidden = false;
        elements.error.hidden = true;
        startHandshake();
      },
      { signal },
    );

    currentFrame.addEventListener(
      "error",
      () => {
        if (activeToken !== navigationToken) return;
        showErrorState();
      },
      { signal },
    );

    loadTimerId = win.setTimeout(() => {
      if (activeToken !== navigationToken || frameLoaded) return;
      showErrorState();
    }, outerLoadTimeoutMs);

    currentFrame.src = buildFrameUrl(forceReload).href;
  }

  function show(): void {
    exitCanvasFullscreenIfNeeded(elements.canvas, doc);
    elements.host.hidden = false;
    elements.host.classList.remove("parked");
    elements.host.setAttribute("aria-hidden", "false");
    elements.canvas.blur();
    if (hostReady) {
      elements.loading.hidden = true;
      elements.error.hidden = true;
      currentFrame.focus();
    }
  }

  function park(): void {
    if (elements.host.hidden) return;
    stopTimers();
    elements.host.classList.add("parked");
    elements.host.setAttribute("aria-hidden", "true");
    elements.canvas.focus();
  }

  function open(url: string, sessionId: string, sessionChanged: boolean): void {
    const targetUrl = new URL(url, win.location.href);
    targetUrl.searchParams.set("hostRelease", CO_CREATION_HOST_RELEASE);
    targetUrl.searchParams.delete("workspaceReload");

    hostUrl = targetUrl.href;
    currentSessionId = sessionId;
    if (sessionChanged) {
      hostReady = false;
    }
    elements.loading.hidden = hostReady;
    elements.error.hidden = true;
    show();

    const currentCanonical = canonicalCoCreationFrameUrl(
      currentFrame.src,
      win.location.href,
    );
    const nextCanonical = canonicalCoCreationFrameUrl(
      hostUrl,
      win.location.href,
    );
    if (sessionChanged || currentCanonical !== nextCanonical) {
      mountFrame(false);
    } else if (!hostReady && frameLoaded) {
      startHandshake();
    }
  }

  function markReady(): void {
    hostReady = true;
    frameLoaded = true;
    stopTimers();
    elements.loading.hidden = true;
    elements.error.hidden = true;
    if (
      !elements.host.hidden &&
      !elements.host.classList.contains("parked")
    ) {
      show();
    }
  }

  elements.retryButton.addEventListener(
    "click",
    () => {
      mountFrame(true);
    },
    { signal: hostAbort.signal },
  );

  elements.returnButton?.addEventListener(
    "click",
    () => {
      park();
    },
    { signal: hostAbort.signal },
  );

  win.addEventListener(
    "keydown",
    (event) => {
      if (
        event.key === "Escape" &&
        !elements.host.hidden &&
        !elements.host.classList.contains("parked") &&
        !elements.error.hidden
      ) {
        event.preventDefault();
        park();
      }
    },
    { signal: hostAbort.signal },
  );

  return {
    open,
    show,
    park,
    markReady,
    isReady: () => hostReady,
    getFrameWindow: () => currentFrame.contentWindow,
    destroy(): void {
      stopTimers();
      frameAbort?.abort();
      hostAbort.abort();
      currentFrame.removeAttribute("src");
    },
  };
}
