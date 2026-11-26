import type { BridgeState, UnityRuntimeConfig } from "./state";

export type UnityBannerType = "info" | "warning" | "error";

export interface UnityShellElements {
  container: HTMLElement;
  canvas: HTMLCanvasElement;
  loadingBar: HTMLElement;
  progressBarEmpty: HTMLElement;
  progressBarFull: HTMLElement;
  loadingPercent: HTMLElement;
  fullscreenButton: HTMLButtonElement;
  warningBanner: HTMLElement;
}

export interface UnityShellOptions {
  elements: UnityShellElements;
  state: BridgeState;
  config?: UnityRuntimeConfig;
  win?: Window;
  doc?: Document;
}

export interface UnityShellController {
  showBanner(message: string, type?: UnityBannerType): void;
  destroy(): void;
}

export function createUnityShell({
  elements,
  state,
  config,
  win = window,
  doc = document,
}: UnityShellOptions): UnityShellController {
  const bannerTimers = new Map<HTMLElement, number>();
  let loaderScript: HTMLScriptElement | null = null;
  let destroyed = false;

  function updateBannerContainerVisibility(): void {
    elements.warningBanner.style.display = elements.warningBanner.children.length
      ? "grid"
      : "none";
  }

  function dismissBanner(banner: HTMLElement): void {
    const timerId = bannerTimers.get(banner);
    if (timerId !== undefined) {
      win.clearTimeout(timerId);
      bannerTimers.delete(banner);
    }
    banner.remove();
    updateBannerContainerVisibility();
  }

  function showBanner(message: string, type: UnityBannerType = "info"): void {
    const banner = doc.createElement("div");
    banner.className = `unity-banner ${type}`;
    banner.setAttribute("role", type === "error" ? "alert" : "status");

    const textSpan = doc.createElement("span");
    textSpan.className = "unity-banner-text";
    textSpan.textContent = message;

    const closeButton = doc.createElement("button");
    closeButton.type = "button";
    closeButton.className = "unity-banner-close";
    closeButton.setAttribute("aria-label", "Dismiss banner");
    closeButton.textContent = "×";
    closeButton.addEventListener("click", () => dismissBanner(banner));

    banner.append(textSpan, closeButton);
    elements.warningBanner.appendChild(banner);

    if (type !== "error") {
      const timerId = win.setTimeout(() => {
        dismissBanner(banner);
      }, 5000);
      bannerTimers.set(banner, timerId);
    }

    updateBannerContainerVisibility();
  }

  if (config) {
    const unityConfig: Record<string, unknown> = {
      dataUrl: config.dataUrl,
      frameworkUrl: config.frameworkUrl,
      streamingAssetsUrl: config.streamingAssetsUrl,
      companyName: config.companyName,
      productName: config.productName,
      productVersion: config.productVersion,
      showBanner,
    };
    if (config.workerUrl) unityConfig.workerUrl = config.workerUrl;
    if (config.codeUrl) unityConfig.codeUrl = config.codeUrl;
    if (config.memoryUrl) unityConfig.memoryUrl = config.memoryUrl;
    if (config.symbolsUrl) unityConfig.symbolsUrl = config.symbolsUrl;

    if (/iPhone|iPad|iPod|Android/i.test(win.navigator.userAgent)) {
      doc.body.classList.add("mobile-game");
      elements.container.className = "unity-mobile";
      elements.canvas.className = "unity-mobile";
      unityConfig.devicePixelRatio = 1;
    }

    if (config.backgroundUrl) {
      elements.canvas.style.background = `url('${config.backgroundUrl}') center / cover`;
    }

    elements.loadingBar.style.display = "grid";

    loaderScript = doc.createElement("script");
    loaderScript.src = config.loaderUrl;
    loaderScript.onload = () => {
      if (destroyed) return;
      if (typeof win.createUnityInstance !== "function") {
        elements.loadingBar.style.display = "none";
        showBanner("Unity WebGL runtime did not initialize.", "error");
        return;
      }
      win
        .createUnityInstance(elements.canvas, unityConfig, (progress) => {
          const percentage = Math.round(progress * 100);
          elements.progressBarFull.style.width = `${percentage}%`;
          elements.progressBarEmpty.setAttribute(
            "aria-valuenow",
            String(percentage),
          );
          elements.loadingPercent.textContent = `Loading ${percentage}%`;
        })
        .then((unityInstance) => {
          if (destroyed) return;
          state.unityInstance = unityInstance;
          win.SokobanUnityInstance = unityInstance;
          elements.loadingBar.style.display = "none";
          elements.fullscreenButton.onclick = () => {
            unityInstance.SetFullscreen(1);
          };
        })
        .catch((error: unknown) => {
          if (destroyed) return;
          elements.loadingBar.style.display = "none";
          const msg =
            error instanceof Error ? error.message : String(error || "Load failed.");
          showBanner(msg, "error");
        });
    };
    loaderScript.onerror = () => {
      if (destroyed) return;
      elements.loadingBar.style.display = "none";
      showBanner("Game loader could not be loaded.", "error");
    };

    doc.body.appendChild(loaderScript);
  }

  return {
    showBanner,
    destroy(): void {
      destroyed = true;
      for (const timerId of bannerTimers.values()) {
        win.clearTimeout(timerId);
      }
      bannerTimers.clear();
      if (loaderScript) {
        loaderScript.onload = null;
        loaderScript.onerror = null;
      }
    },
  };
}
