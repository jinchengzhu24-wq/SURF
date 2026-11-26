import { exitCanvasFullscreenIfNeeded } from "./state";
import type { UnitySendTarget } from "./unity-exports";

export const LOBBY_REFERENCE_WIDTH = 1920;
export const LOBBY_REFERENCE_HEIGHT = 1080;

// Fixed reference-space rectangles from Online_Lobby.unity.
export const GENERATED_ROOM_CODE_RECT = {
  left: 285,
  top: 574,
  width: 570,
  height: 82,
  paddingX: 18,
} as const;

export const JOIN_ROOM_CODE_RECT = {
  left: 1065,
  top: 574,
  width: 570,
  height: 82,
  paddingX: 24,
} as const;

export function normalizeLobbyRoomCode(value: string): string {
  return String(value || "")
    .toUpperCase()
    .replace(/^\s*ROOM[:\s-]+/, "")
    .replace(/[^A-Z0-9]/g, "")
    .slice(0, 6);
}

export interface LobbyOverlayElements {
  overlay: HTMLElement;
  generatedInput: HTMLInputElement;
  joinInput: HTMLInputElement;
  canvas: HTMLCanvasElement;
}

export interface LobbyOverlayOptions {
  elements: LobbyOverlayElements;
  sendToUnity: (target: UnitySendTarget) => boolean;
  win?: Window;
  doc?: Document;
}

export interface LobbyOverlayController {
  setVisible(visible: boolean): void;
  setRoomCode(value: string): void;
  setJoinCode(value: string): void;
  setJoinInteractable(interactable: boolean): void;
  reposition(): void;
  destroy(): void;
}

export function createLobbyOverlay({
  elements,
  sendToUnity,
  win = window,
  doc = document,
}: LobbyOverlayOptions): LobbyOverlayController {
  let visible = false;
  const abortController = new AbortController();

  function positionInputs(): void {
    if (!visible) return;
    const overlayRect = elements.overlay.getBoundingClientRect();
    const canvasRect = elements.canvas.getBoundingClientRect();
    if (!canvasRect.width || !canvasRect.height) return;

    const scaleX = canvasRect.width / LOBBY_REFERENCE_WIDTH;
    const scaleY = canvasRect.height / LOBBY_REFERENCE_HEIGHT;
    const offsetX = canvasRect.left - overlayRect.left;
    const offsetY = canvasRect.top - overlayRect.top;

    const placeInput = (
      input: HTMLInputElement,
      rect: { left: number; top: number; width: number; height: number; paddingX: number },
    ) => {
      input.style.left = `${offsetX + rect.left * scaleX}px`;
      input.style.top = `${offsetY + rect.top * scaleY}px`;
      input.style.width = `${rect.width * scaleX}px`;
      input.style.height = `${rect.height * scaleY}px`;
      input.style.fontSize = `${40 * scaleY}px`;
      input.style.padding = `0 ${rect.paddingX * scaleX}px`;
    };

    placeInput(elements.generatedInput, GENERATED_ROOM_CODE_RECT);
    placeInput(elements.joinInput, JOIN_ROOM_CODE_RECT);
  }

  let resizeObserver: ResizeObserver | null = null;
  const ResizeObserverCtor =
    (win as Window & { ResizeObserver?: typeof ResizeObserver })
      .ResizeObserver ?? globalThis.ResizeObserver;
  if (typeof ResizeObserverCtor === "function") {
    const observer = new ResizeObserverCtor(() => {
      positionInputs();
    });
    observer.observe(elements.canvas);
    observer.observe(elements.overlay);
    resizeObserver = observer;
  }

  const { signal } = abortController;

  elements.joinInput.addEventListener(
    "input",
    () => {
      if (elements.joinInput.disabled) return;
      const normalized = normalizeLobbyRoomCode(elements.joinInput.value);
      if (elements.joinInput.value !== normalized) {
        elements.joinInput.value = normalized;
      }
      sendToUnity({
        gameObject: "OnlineLobbyController",
        methodName: "SetNativeJoinRoomCode",
        value: normalized,
      });
    },
    { signal },
  );

  elements.joinInput.addEventListener(
    "keydown",
    (event) => {
      if (elements.joinInput.disabled || event.key !== "Enter") return;
      event.preventDefault();
      event.stopPropagation();
      sendToUnity({
        gameObject: "OnlineLobbyController",
        methodName: "SubmitNativeJoinRoomCode",
      });
    },
    { signal },
  );

  win.addEventListener("resize", positionInputs, { signal });
  doc.addEventListener("fullscreenchange", positionInputs, { signal });

  return {
    setVisible(nextVisible: boolean): void {
      visible = Boolean(nextVisible);
      if (visible) {
        exitCanvasFullscreenIfNeeded(elements.canvas, doc);
      }
      elements.overlay.hidden = !visible;
      if (!visible) {
        const active = doc.activeElement;
        if (
          active === elements.generatedInput ||
          active === elements.joinInput
        ) {
          (active as HTMLElement).blur();
        }
        return;
      }
      positionInputs();
    },

    setRoomCode(value: string): void {
      const roomCode =
        value === "ROOM CODE APPEARS HERE" ? "" : value || "";
      elements.generatedInput.value = roomCode;
      elements.generatedInput.disabled = !roomCode;
      elements.generatedInput.setAttribute(
        "aria-label",
        roomCode ? "Generated room code" : "Room code will appear here",
      );
    },

    setJoinCode(value: string): void {
      const normalized = normalizeLobbyRoomCode(value);
      if (elements.joinInput.value !== normalized) {
        elements.joinInput.value = normalized;
      }
    },

    setJoinInteractable(interactable: boolean): void {
      elements.joinInput.disabled = !interactable;
    },

    reposition: positionInputs,

    destroy(): void {
      resizeObserver?.disconnect();
      abortController.abort();
    },
  };
}
