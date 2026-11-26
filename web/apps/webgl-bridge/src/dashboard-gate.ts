import {
  ApiError,
  fetchJson,
  validateSafeNavigationUrl,
} from "@sokoban/ui-core";
import { exitCanvasFullscreenIfNeeded } from "./state";

export interface DashboardGateElements {
  dialog: HTMLDialogElement;
  form: HTMLFormElement;
  passwordInput: HTMLInputElement;
  statusText: HTMLElement;
  cancelButton: HTMLButtonElement;
  submitButton: HTMLButtonElement;
  canvas: HTMLCanvasElement;
  triggerLinks: Iterable<HTMLAnchorElement>;
}

export interface DashboardGateOptions {
  elements: DashboardGateElements;
  win?: Window;
  doc?: Document;
  fetchImpl?: typeof fetch;
  defaultTargetUrl?: string;
}

export interface DashboardGateController {
  open(targetUrl?: string): void;
  close(): void;
  destroy(): void;
}

/**
 * Native `<dialog>` password gate for the 8000 study dashboard.
 * Isolates Unity's global WebGL keyboard listeners cleanly via `canvas.blur()`
 * and capture-phase propagation isolation without synthetic keystroke splicing.
 */
export function createDashboardGate({
  elements,
  win = window,
  doc = document,
  fetchImpl = globalThis.fetch,
  defaultTargetUrl = "/frontend/",
}: DashboardGateOptions): DashboardGateController {
  let targetUrl = defaultTargetUrl;
  let busy = false;
  let previousFocus: HTMLElement | null = null;
  const abortController = new AbortController();
  const { signal } = abortController;

  function setBusy(nextBusy: boolean): void {
    busy = nextBusy;
    elements.submitButton.disabled = nextBusy;
    elements.cancelButton.disabled = nextBusy;
    elements.form.setAttribute("aria-busy", nextBusy ? "true" : "false");
  }

  function isOpen(): boolean {
    return elements.dialog.open || elements.dialog.hasAttribute("open");
  }

  function open(nextUrl?: string): void {
    exitCanvasFullscreenIfNeeded(elements.canvas, doc);
    if (nextUrl) {
      targetUrl = nextUrl;
    }
    const active = doc.activeElement;
    previousFocus = active instanceof HTMLElement ? active : null;
    elements.canvas.blur();

    setBusy(false);
    elements.statusText.textContent = "";
    elements.passwordInput.value = "";

    if (!isOpen()) {
      if (typeof elements.dialog.showModal === "function") {
        elements.dialog.showModal();
      } else {
        elements.dialog.setAttribute("open", "");
      }
    }

    queueMicrotask(() => {
      if (isOpen()) {
        elements.passwordInput.focus();
      }
    });
  }

  function close(): void {
    if (busy) return;
    if (isOpen()) {
      if (typeof elements.dialog.close === "function") {
        elements.dialog.close();
      } else {
        elements.dialog.removeAttribute("open");
      }
    }
    elements.passwordInput.value = "";
    elements.statusText.textContent = "";
    const restoreTarget =
      previousFocus && previousFocus.isConnected
        ? previousFocus
        : elements.canvas;
    previousFocus = null;
    restoreTarget.focus();
  }

  // Prevent Unity WebGL's window/document keyboard listeners from intercepting
  // keys typed inside the native <dialog> without swallowing dialog-internal handlers.
  const isolateOutsideDialogKeyboard = (event: KeyboardEvent): void => {
    if (!isOpen()) return;
    if (event.target instanceof Node && elements.dialog.contains(event.target)) {
      return;
    }
    event.stopImmediatePropagation();
  };

  const isolateDialogKeyboardBubble = (event: KeyboardEvent): void => {
    if (!isOpen()) return;
    event.stopPropagation();
  };

  for (const eventName of ["keydown", "keypress", "keyup"] as const) {
    win.addEventListener(eventName, isolateOutsideDialogKeyboard, {
      capture: true,
      signal,
    });
    elements.dialog.addEventListener(eventName, isolateDialogKeyboardBubble, {
      signal,
    });
  }

  elements.dialog.addEventListener(
    "cancel",
    (event) => {
      event.preventDefault();
      close();
    },
    { signal },
  );

  elements.dialog.addEventListener(
    "click",
    (event) => {
      if (!busy && event.target === elements.dialog) {
        close();
      }
    },
    { signal },
  );

  elements.cancelButton.addEventListener("click", close, { signal });

  elements.form.addEventListener(
    "submit",
    async (event) => {
      event.preventDefault();
      if (busy) return;

      const password = elements.passwordInput.value;
      if (!password) {
        elements.statusText.textContent = "Enter the dashboard password.";
        elements.passwordInput.focus();
        return;
      }

      let safeUrl: string;
      try {
        safeUrl = validateSafeNavigationUrl(targetUrl, {
          baseUrl: win.location.href,
        });
      } catch {
        elements.statusText.textContent = "Dashboard target URL is invalid.";
        return;
      }

      setBusy(true);
      elements.statusText.textContent = "Checking password...";

      // Pre-open synchronously inside the submit gesture without noopener so we retain
      // the Window handle to navigate (or close on failure), then sever opener immediately.
      const popup = win.open("about:blank", "_blank");
      if (popup) {
        try {
          popup.opener = null;
        } catch {
          // Ignore readonly opener in restricted environments.
        }
      }

      try {
        await fetchJson<unknown>("/verify-dashboard-password", {
          method: "POST",
          headers: { "X-Delete-Password": password },
          timeoutMs: 10_000,
          fetchImpl,
        });
        setBusy(false);
        if (popup && !popup.closed) {
          try {
            popup.opener = null;
          } catch {
            // Ignore readonly opener in restricted environments.
          }
          popup.location.href = safeUrl;
          close();
          return;
        }
        const fallbackLink = doc.createElement("a");
        fallbackLink.href = safeUrl;
        fallbackLink.target = "_blank";
        fallbackLink.rel = "noopener noreferrer";
        fallbackLink.textContent = "Open dashboard";
        elements.statusText.replaceChildren(
          "Popup blocked. Open the dashboard directly: ",
          fallbackLink,
        );
      } catch (error) {
        if (popup && !popup.closed) {
          try {
            popup.close();
          } catch {
            // Ignore close failures on mocked or restricted windows.
          }
        }
        setBusy(false);
        if (error instanceof ApiError && error.status === 401) {
          elements.statusText.textContent = "Incorrect password";
        } else if (error instanceof Error && error.message) {
          elements.statusText.textContent = error.message;
        } else {
          elements.statusText.textContent = "Could not verify password.";
        }
        elements.passwordInput.focus();
      }
    },
    { signal },
  );

  for (const link of elements.triggerLinks) {
    link.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        open(link.getAttribute("href") || link.href);
      },
      { signal },
    );
  }

  return {
    open,
    close,
    destroy(): void {
      abortController.abort();
    },
  };
}
