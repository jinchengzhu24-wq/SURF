import { PROTOCOL_VERSION } from "@sokoban/ui-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  HANDSHAKE_TIMEOUT_MS,
  createCoCreationHost,
} from "../cocreation-host";
import { createDashboardGate } from "../dashboard-gate";
import { createLobbyOverlay } from "../lobby-overlay";
import { bootstrapWebGLBridge } from "../main";
import { buildAllowedCoCreationOrigins, isAllowedCoCreationOrigin } from "../router";
import { createInitialBridgeState } from "../state";
import { createUnityShell } from "../unity-shell";

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | boolean> = {},
  children: Array<Node | string> = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (typeof value === "boolean") {
      if (value) {
        node.setAttribute(key, "");
      }
    } else {
      node.setAttribute(key, value);
    }
  }
  node.append(...children);
  return node;
}

function renderTemplateDom(): void {
  document.body.replaceChildren(
    el("main", { class: "adventure-shell" }, [
      el("div", { id: "unity-container" }, [
        el("canvas", {
          id: "unity-canvas",
          width: "1920",
          height: "1080",
          tabindex: "-1",
        }),
        el("div", { id: "sokoban-lobby-native-overlay", hidden: true }, [
          el("input", {
            id: "sokoban-lobby-generated-room-code",
            type: "text",
            readonly: true,
            disabled: true,
          }),
          el("input", { id: "sokoban-lobby-join-room-code", type: "text" }),
        ]),
        el("div", { id: "unity-loading-bar" }, [
          el("div", { id: "unity-progress-bar-empty" }, [
            el("div", { id: "unity-progress-bar-full" }),
          ]),
          el("p", { id: "unity-loading-percent" }),
        ]),
        el("div", { id: "unity-warning" }),
      ]),
      el("a", { href: "/frontend/", "data-dashboard-link": true }, [
        "Dashboard",
      ]),
      el("button", { id: "unity-fullscreen-button", type: "button" }, [
        "Fullscreen",
      ]),
    ]),
    el("section", { id: "cocreation-host", hidden: true }, [
      el("iframe", { id: "cocreation-frame" }),
      el("div", { id: "cocreation-host-loading" }),
      el("div", { id: "cocreation-host-error", hidden: true }, [
        el("button", { id: "cocreation-retry", type: "button" }, ["RETRY"]),
        el("button", { id: "cocreation-return-unity", type: "button" }, [
          "RETURN",
        ]),
      ]),
    ]),
    el("dialog", { id: "dashboard-access-modal" }, [
      el("form", { id: "dashboard-access-form" }, [
        el("input", { id: "dashboard-access-password", type: "password" }),
        el("p", { id: "dashboard-access-status" }),
        el("button", { id: "dashboard-access-cancel", type: "button" }, [
          "CANCEL",
        ]),
        el("button", { id: "dashboard-access-submit", type: "submit" }, [
          "CONTINUE",
        ]),
      ]),
    ]),
  );
}

let teardown: (() => void) | null = null;

afterEach(() => {
  teardown?.();
  teardown = null;
  document.body.replaceChildren();
  (window as unknown as { happyDOM?: { setURL: (u: string) => void } }).happyDOM?.setURL(
    "http://localhost:3000/",
  );
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("webgl-bridge", () => {
  it("explicitly rejects 'null' and unlisted origins, allows loopback 8000, and rejects stale 8010", () => {
    expect(
      isAllowedCoCreationOrigin("null", ["https://sokobanaidemo.top", "null"]),
    ).toBe(false);
    expect(
      isAllowedCoCreationOrigin("", ["https://sokobanaidemo.top"]),
    ).toBe(false);
    expect(
      isAllowedCoCreationOrigin("https://evil.example", [
        "https://sokobanaidemo.top",
      ]),
    ).toBe(false);
    expect(
      isAllowedCoCreationOrigin("https://sokobanaidemo.top", [
        "https://sokobanaidemo.top",
      ]),
    ).toBe(true);

    const loopbackAllowed = buildAllowedCoCreationOrigins({
      origin: "http://localhost:5173",
      hostname: "localhost",
    });
    expect(isAllowedCoCreationOrigin("http://127.0.0.1:8000", loopbackAllowed)).toBe(true);
    expect(isAllowedCoCreationOrigin("http://localhost:8000", loopbackAllowed)).toBe(true);
    expect(isAllowedCoCreationOrigin("http://127.0.0.1:8010", loopbackAllowed)).toBe(false);
    expect(isAllowedCoCreationOrigin("http://localhost:8010", loopbackAllowed)).toBe(false);
    expect(isAllowedCoCreationOrigin("https://sokobanaidemo.top", loopbackAllowed)).toBe(false);
    expect(isAllowedCoCreationOrigin("https://www.sokobanaidemo.top", loopbackAllowed)).toBe(false);

    const prodAllowed = buildAllowedCoCreationOrigins({
      origin: "https://sokobanaidemo.top",
      hostname: "sokobanaidemo.top",
    });
    expect(isAllowedCoCreationOrigin("http://127.0.0.1:8000", prodAllowed)).toBe(false);
    expect(isAllowedCoCreationOrigin("http://localhost:8000", prodAllowed)).toBe(false);
    expect(isAllowedCoCreationOrigin("http://127.0.0.1:8010", prodAllowed)).toBe(false);
    expect(isAllowedCoCreationOrigin("http://localhost:8010", prodAllowed)).toBe(false);
  });

  it("registers all 9 Sokoban* shims + SokobanOpenDashboardGate, enforces iframe-only sources, and dispatches all 5 SendMessage targets", async () => {
    (window as unknown as { happyDOM?: { setURL: (u: string) => void } }).happyDOM?.setURL(
      "https://sokobanaidemo.top/game/",
    );
    renderTemplateDom();
    const sendMessage = vi.fn();
    window.createUnityInstance = vi.fn(async () => ({
      SendMessage: sendMessage,
      SetFullscreen: vi.fn(),
    }));
    window.__SOKOBAN_UNITY_CONFIG__ = {
      buildUrl: "Build",
      loaderUrl: "Build/test.loader.js",
      dataUrl: "Build/test.data",
      frameworkUrl: "Build/test.framework.js",
      streamingAssetsUrl: "StreamingAssets",
      companyName: "Test",
      productName: "Sokoban",
      productVersion: "1.0",
    };

    teardown = bootstrapWebGLBridge(window, document);

    expect(typeof window.SokobanSetCoCreationPlayBridgeReady).toBe("function");
    expect(typeof window.SokobanReturnToCoCreationLab).toBe("function");
    expect(typeof window.SokobanShowCoCreationLab).toBe("function");
    expect(typeof window.SokobanSetDraftRegenerationBridgeSession).toBe("function");
    expect(typeof window.SokobanReturnDraftRegeneration).toBe("function");
    expect(typeof window.SokobanSetLobbyOverlayVisible).toBe("function");
    expect(typeof window.SokobanSetLobbyRoomCode).toBe("function");
    expect(typeof window.SokobanSetLobbyJoinCode).toBe("function");
    expect(typeof window.SokobanSetLobbyJoinInteractable).toBe("function");
    expect(typeof window.SokobanOpenDashboardGate).toBe("function");

    const loaderScript = document.querySelector<HTMLScriptElement>(
      'script[src="Build/test.loader.js"]',
    )!;
    loaderScript.onload?.(new Event("load"));
    await vi.waitFor(() => {
      expect(window.SokobanUnityInstance).toBeTruthy();
    });

    // 1 & 2: OnlineLobbyController SendMessage targets
    const joinInput = document.querySelector<HTMLInputElement>(
      "#sokoban-lobby-join-room-code",
    )!;
    window.SokobanSetLobbyOverlayVisible?.(true);
    window.SokobanSetLobbyRoomCode?.("AB12CD");
    expect(
      document.querySelector<HTMLInputElement>("#sokoban-lobby-generated-room-code")!
        .value,
    ).toBe("AB12CD");
    joinInput.value = "room: xy98zz";
    joinInput.dispatchEvent(new Event("input", { bubbles: true }));
    expect(joinInput.value).toBe("XY98ZZ");
    expect(sendMessage).toHaveBeenCalledWith(
      "OnlineLobbyController",
      "SetNativeJoinRoomCode",
      "XY98ZZ",
    );
    joinInput.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
    expect(sendMessage).toHaveBeenCalledWith(
      "OnlineLobbyController",
      "SubmitNativeJoinRoomCode",
    );

    // Open Co-Creation Lab iframe and attach mock contentWindow
    window.SokobanShowCoCreationLab?.(
      "https://sokobanaidemo.top/cocreation/",
      "sess-1",
    );
    const frame = document.querySelector<HTMLIFrameElement>("#cocreation-frame")!;
    const framePostMessage = vi.fn();
    const frameWindow = {
      postMessage: framePostMessage,
      closed: false,
    } as unknown as Window;
    Object.defineProperty(frame, "contentWindow", {
      configurable: true,
      value: frameWindow,
    });
    frame.dispatchEvent(new Event("load"));

    // Rejects "null" origin and non-iframe popup/window sources
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "null",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-lab-ready",
          protocolVersion: PROTOCOL_VERSION,
          sessionId: "sess-1",
        },
      }),
    );
    const nonIframeSource = { postMessage: vi.fn() } as unknown as Window;
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: nonIframeSource,
        data: {
          type: "sokoban:cocreation-lab-ready",
          protocolVersion: PROTOCOL_VERSION,
          sessionId: "sess-1",
        },
      }),
    );
    expect(document.querySelector<HTMLElement>("#cocreation-host-loading")!.hidden).toBe(
      false,
    );

    // Accepts valid lab-ready from iframe
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-lab-ready",
          protocolVersion: PROTOCOL_VERSION,
          sessionId: "sess-1",
        },
      }),
    );
    expect(document.querySelector<HTMLElement>("#cocreation-host-loading")!.hidden).toBe(
      true,
    );

    // Prepare -> Abort flow re-shows lab and clears prepared request
    window.SokobanSetDraftRegenerationBridgeSession?.("sess-1", PROTOCOL_VERSION);
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-draft-regenerate-prepare",
          requestId: "prep-abort",
          sessionId: "sess-1",
          protocolVersion: PROTOCOL_VERSION,
        },
      }),
    );
    const hostEl = document.querySelector<HTMLElement>("#cocreation-host")!;
    expect(hostEl.classList.contains("parked")).toBe(true);
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-draft-regenerate-abort",
          prepareRequestId: "prep-abort",
          sessionId: "sess-1",
          protocolVersion: PROTOCOL_VERSION,
        },
      }),
    );
    expect(hostEl.classList.contains("parked")).toBe(false);

    // 3: CoCreationEntryController draft regeneration prepare -> request -> return
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-draft-regenerate-prepare",
          requestId: "prep-1",
          sessionId: "sess-1",
          protocolVersion: PROTOCOL_VERSION,
        },
      }),
    );
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-draft-regenerate-request",
          requestId: "req-1",
          prepareRequestId: "prep-1",
          protocolVersion: PROTOCOL_VERSION,
          sessionId: "sess-1",
          regenerationRequestId: "regen-1",
        },
      }),
    );
    expect(sendMessage).toHaveBeenCalledWith(
      "CoCreationEntryController",
      "ReceiveBrowserDraftRegenerationRequest",
      JSON.stringify({ sessionId: "sess-1", requestId: "regen-1" }),
    );
    window.SokobanReturnDraftRegeneration?.("sess-1", "regen-1", "completed");
    expect(framePostMessage).toHaveBeenCalledWith(
      {
        type: "sokoban:cocreation-draft-regenerate-return",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: "sess-1",
        requestId: "regen-1",
        status: "completed",
      },
      "https://sokobanaidemo.top",
    );

    // 4 & 5: CoCreationPlayBootstrap & CoCreationStagePlayController
    // OnDestroy resets draft receiver while preserving active sessionId for play-return
    window.SokobanSetDraftRegenerationBridgeSession?.("", 0);
    window.SokobanSetCoCreationPlayBridgeReady?.(true);

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-play-request",
          requestId: "play-1",
          sessionId: "sess-1",
          playUrl: "https://sokobanaidemo.top/play?attempt=1",
          protocolVersion: PROTOCOL_VERSION,
        },
      }),
    );
    expect(sendMessage).toHaveBeenCalledWith(
      "CoCreationPlayBootstrap",
      "ReceiveBrowserPlayRequest",
      JSON.stringify({
        sessionId: "sess-1",
        playUrl: "https://sokobanaidemo.top/play?attempt=1",
      }),
    );

    window.SokobanReturnToCoCreationLab?.("completed");
    expect(framePostMessage).toHaveBeenCalledWith(
      {
        type: "sokoban:cocreation-play-return",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: "sess-1",
        status: "completed",
      },
      "https://sokobanaidemo.top",
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-show-unity",
          protocolVersion: PROTOCOL_VERSION,
          sessionId: "sess-1",
        },
      }),
    );
    expect(hostEl.classList.contains("parked")).toBe(true);

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://sokobanaidemo.top",
        source: frameWindow,
        data: {
          type: "sokoban:cocreation-show-lab",
          protocolVersion: PROTOCOL_VERSION,
          sessionId: "sess-1",
        },
      }),
    );
    expect(hostEl.classList.contains("parked")).toBe(false);
    expect(sendMessage).toHaveBeenCalledWith(
      "CoCreationStagePlayController",
      "ReceiveBrowserReturnToLab",
      "",
    );
  });

  it("does not poll host-init before iframe load event fires and surfaces timeout errors", () => {
    vi.useFakeTimers();
    renderTemplateDom();

    const host = createCoCreationHost({
      elements: {
        host: document.querySelector("#cocreation-host")!,
        frame: document.querySelector("#cocreation-frame")!,
        loading: document.querySelector("#cocreation-host-loading")!,
        error: document.querySelector("#cocreation-host-error")!,
        retryButton: document.querySelector("#cocreation-retry")!,
        returnButton: document.querySelector("#cocreation-return-unity"),
        canvas: document.querySelector("#unity-canvas")!,
      },
    });

    host.open("https://sokobanaidemo.top/cocreation/", "sess-1", true);
    const frame = document.querySelector<HTMLIFrameElement>("#cocreation-frame")!;
    const postSpy = vi.fn();
    Object.defineProperty(frame, "contentWindow", {
      configurable: true,
      value: { postMessage: postSpy, closed: false },
    });

    // Before 'load' fires, zero host-init messages should be sent!
    vi.advanceTimersByTime(1000);
    expect(postSpy).not.toHaveBeenCalled();

    // Once 'load' fires, handshake begins deterministically.
    frame.dispatchEvent(new Event("load"));
    expect(postSpy).toHaveBeenCalledWith(
      {
        type: "sokoban:cocreation-host-init",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: "sess-1",
      },
      "https://sokobanaidemo.top",
    );

    // If lab-ready never arrives within HANDSHAKE_TIMEOUT_MS, error card appears.
    vi.advanceTimersByTime(HANDSHAKE_TIMEOUT_MS);
    const errorEl = document.querySelector<HTMLElement>("#cocreation-host-error")!;
    expect(errorEl.hidden).toBe(false);

    // Escape parks host when error card is visible
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", cancelable: true }));
    expect(
      document.querySelector<HTMLElement>("#cocreation-host")!.classList.contains("parked"),
    ).toBe(true);

    host.destroy();
  });

  it("positions lobby overlay inputs with ResizeObserver using exact 1920x1080 Unity coordinates", () => {
    renderTemplateDom();
    const overlayEl = document.querySelector<HTMLElement>(
      "#sokoban-lobby-native-overlay",
    )!;
    const generatedInput = document.querySelector<HTMLInputElement>(
      "#sokoban-lobby-generated-room-code",
    )!;
    const joinInput = document.querySelector<HTMLInputElement>(
      "#sokoban-lobby-join-room-code",
    )!;
    const canvas = document.querySelector<HTMLCanvasElement>("#unity-canvas")!;

    const observerCallbacks: ResizeObserverCallback[] = [];
    const observeSpy = vi.fn();
    const disconnectSpy = vi.fn();
    class MockResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        observerCallbacks.push(cb);
      }
      observe = observeSpy;
      unobserve = vi.fn();
      disconnect = disconnectSpy;
    }
    vi.stubGlobal("ResizeObserver", MockResizeObserver);

    vi.spyOn(overlayEl, "getBoundingClientRect").mockReturnValue(
      new DOMRect(10, 20, 960, 540),
    );
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      new DOMRect(10, 20, 960, 540),
    );

    const lobby = createLobbyOverlay({
      elements: { overlay: overlayEl, generatedInput, joinInput, canvas },
      sendToUnity: vi.fn(() => true),
    });

    expect(observeSpy).toHaveBeenCalledWith(canvas);
    lobby.setVisible(true);

    // At 960x540 (0.5x scale of 1920x1080):
    // generated rect (285, 574, 570, 82, 18) -> (142.5px, 287px, 285px, 41px, font 20px, padding 0 9px)
    expect(generatedInput.style.left).toBe("142.5px");
    expect(generatedInput.style.top).toBe("287px");
    expect(generatedInput.style.width).toBe("285px");
    expect(generatedInput.style.height).toBe("41px");
    expect(generatedInput.style.fontSize).toBe("20px");
    expect(generatedInput.style.padding).toBe("0px 9px");

    // Simulate resize via ResizeObserver callback to 1920x1080 (1x scale)
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      new DOMRect(10, 20, 1920, 1080),
    );
    observerCallbacks.forEach((cb) => cb([], {} as ResizeObserver));
    expect(joinInput.style.left).toBe("1065px");
    expect(joinInput.style.top).toBe("574px");
    expect(joinInput.style.width).toBe("570px");
    expect(joinInput.style.height).toBe("82px");

    lobby.destroy();
    expect(disconnectSpy).toHaveBeenCalledTimes(1);
  });

  it("pre-opens blank popup synchronously, closes on 401, handles blocked popup, and preserves dialog-internal key handlers", async () => {
    renderTemplateDom();
    let failAuth = true;
    const fetchMock = vi.fn(async () =>
      failAuth
        ? new Response(JSON.stringify({ detail: "Unauthorized" }), { status: 401 })
        : new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );
    const createdPopups: Array<{
      opener: unknown;
      closed: boolean;
      location: { href: string };
      close: ReturnType<typeof vi.fn>;
    }> = [];
    let blockPopups = false;
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => {
      if (blockPopups) return null;
      const winObj = {
        opener: window as unknown,
        closed: false,
        location: { href: "about:blank" },
        close: vi.fn(() => {
          winObj.closed = true;
        }),
      };
      createdPopups.push(winObj);
      return winObj as unknown as Window;
    });

    const gate = createDashboardGate({
      elements: {
        dialog: document.querySelector("#dashboard-access-modal")!,
        form: document.querySelector("#dashboard-access-form")!,
        passwordInput: document.querySelector("#dashboard-access-password")!,
        statusText: document.querySelector("#dashboard-access-status")!,
        cancelButton: document.querySelector("#dashboard-access-cancel")!,
        submitButton: document.querySelector("#dashboard-access-submit")!,
        canvas: document.querySelector("#unity-canvas")!,
        triggerLinks: document.querySelectorAll("[data-dashboard-link]"),
      },
      fetchImpl: fetchMock,
    });

    gate.open("/frontend/");
    const dialog = document.querySelector<HTMLDialogElement>(
      "#dashboard-access-modal",
    )!;
    expect(dialog.open || dialog.hasAttribute("open")).toBe(true);

    // Key events inside dialog reach dialog-internal handlers while still stopping before window listeners
    const windowListener = vi.fn();
    const dialogInternalListener = vi.fn();
    window.addEventListener("keydown", windowListener);
    dialog.addEventListener("keydown", dialogInternalListener);
    const keyEvent = new KeyboardEvent("keydown", {
      key: "a",
      bubbles: true,
      cancelable: true,
    });
    document.querySelector("#dashboard-access-password")!.dispatchEvent(keyEvent);
    expect(keyEvent.defaultPrevented).toBe(false);
    expect(dialogInternalListener).toHaveBeenCalledTimes(1);
    expect(windowListener).not.toHaveBeenCalled();
    window.removeEventListener("keydown", windowListener);
    dialog.removeEventListener("keydown", dialogInternalListener);

    // 401 closes pre-opened blank tab and keeps modal open
    document.querySelector<HTMLInputElement>("#dashboard-access-password")!.value =
      "wrong";
    document
      .querySelector<HTMLFormElement>("#dashboard-access-form")!
      .dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await vi.waitFor(() => {
      expect(
        document.querySelector("#dashboard-access-status")!.textContent,
      ).toBe("Incorrect password");
    });
    expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
    expect(createdPopups[0].opener).toBeNull();
    expect(createdPopups[0].close).toHaveBeenCalledTimes(1);
    expect(dialog.open || dialog.hasAttribute("open")).toBe(true);

    // Blocked popup (!popup) on valid auth keeps modal open with clickable fallback link
    failAuth = false;
    blockPopups = true;
    document.querySelector<HTMLInputElement>("#dashboard-access-password")!.value =
      "secret";
    document
      .querySelector<HTMLFormElement>("#dashboard-access-form")!
      .dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await vi.waitFor(() => {
      const link = document.querySelector<HTMLAnchorElement>(
        "#dashboard-access-status a",
      );
      expect(link).toBeTruthy();
      expect(link?.getAttribute("href")).toMatch(/\/frontend\/$/);
      expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    });
    expect(dialog.open || dialog.hasAttribute("open")).toBe(true);

    // Valid password with unblocked pre-opened popup severs opener, navigates location.href, and closes modal
    blockPopups = false;
    document
      .querySelector<HTMLFormElement>("#dashboard-access-form")!
      .dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(createdPopups).toHaveLength(2);
      expect(createdPopups[1].opener).toBeNull();
      expect(createdPopups[1].location.href).toMatch(/\/frontend\/$/);
      expect(dialog.open || dialog.hasAttribute("open")).toBe(false);
    });

    gate.destroy();
  });

  it("renders dismissible unity banners with close button", () => {
    renderTemplateDom();
    const shell = createUnityShell({
      elements: {
        container: document.querySelector("#unity-container")!,
        canvas: document.querySelector("#unity-canvas")!,
        loadingBar: document.querySelector("#unity-loading-bar")!,
        progressBarEmpty: document.querySelector("#unity-progress-bar-empty")!,
        progressBarFull: document.querySelector("#unity-progress-bar-full")!,
        loadingPercent: document.querySelector("#unity-loading-percent")!,
        fullscreenButton: document.querySelector("#unity-fullscreen-button")!,
        warningBanner: document.querySelector("#unity-warning")!,
      },
      state: createInitialBridgeState(),
    });

    shell.showBanner("Loader error", "error");
    const warningEl = document.querySelector<HTMLElement>("#unity-warning")!;
    expect(warningEl.style.display).toBe("grid");
    const closeBtn = warningEl.querySelector<HTMLButtonElement>(".unity-banner-close")!;
    expect(closeBtn).toBeTruthy();
    closeBtn.click();
    expect(warningEl.children.length).toBe(0);
    expect(warningEl.style.display).toBe("none");

    shell.destroy();
  });
});
