import { signal } from "@preact/signals";
import type { JSX } from "preact";
import { useEffect, useRef } from "preact/hooks";
import { t, tileDisplayKey, type TranslationKey } from "../../i18n";
import { isDirty, selectedTile } from "../../store/editorStore";
import { language } from "../../store/sessionStore";

export type ActionHelpKey =
  | "setupLanguage"
  | "regenerateDraft"
  | "enterSession"
  | "play"
  | "saveStage"
  | "discardDraft"
  | "restoreStage"
  | "finalize"
  | "confirmFinalize"
  | "cancelFinalize"
  | "send"
  | "proposalRequest"
  | "returnCurrent"
  | "returnUnity"
  | "retry"
  | "chatRetry"
  | "completeSession"
  | "draftSuggestedRevision"
  | "challengeRevision"
  | "alternativeRevision"
  | "accept"
  | "reject"
  | "intentMatches"
  | "intentReject"
  | "intentAdjust"
  | "intentModify"
  | "intentSave"
  | "intentCancel"
  | "intentKeepThis"
  | "ignoreQuestion"
  | "restoreQuestion"
  | "challengeChoice"
  | "cancelProposal";

export type TooltipDescriptor =
  | {
      kind: "action";
      helpKey: ActionHelpKey;
      titleKey: TranslationKey;
      status?: string;
    }
  | {
      kind: "stage";
      stageNumber: number;
      status?: string;
    }
  | {
      kind: "tile";
      tile: string;
      mode: "palette" | "map" | "preview" | "stagePreview";
      row?: number;
      column?: number;
      entityLabel?: string;
      disabled?: boolean;
      status?: string;
    }
  | {
      kind: "route";
    };

interface ActiveTooltip {
  descriptor: TooltipDescriptor;
  anchor: HTMLElement;
}

const activeTooltip = signal<ActiveTooltip | null>(null);

let hoverTimer: ReturnType<typeof setTimeout> | null = null;
let pendingAnchor: HTMLElement | null = null;

export function hideTooltip(): void {
  if (hoverTimer !== null) {
    clearTimeout(hoverTimer);
    hoverTimer = null;
  }
  pendingAnchor = null;
  activeTooltip.value = null;
}

function queueTooltip(anchor: HTMLElement, descriptor: TooltipDescriptor, immediate: boolean): void {
  if (activeTooltip.value?.anchor === anchor || pendingAnchor === anchor) return;
  hideTooltip();
  pendingAnchor = anchor;
  if (immediate) {
    activeTooltip.value = { descriptor, anchor };
    pendingAnchor = null;
  } else {
    hoverTimer = globalThis.setTimeout(() => {
      hoverTimer = null;
      pendingAnchor = null;
      if (anchor.isConnected) {
        activeTooltip.value = { descriptor, anchor };
      }
    }, 360);
  }
}

export function showTooltip(
  anchor: HTMLElement,
  descriptor: TooltipDescriptor,
  immediate = false,
): void {
  queueTooltip(anchor, descriptor, immediate);
}

/**
 * Container-delegated pointer hover tooltip handlers for `.mini-map` previews.
 */
export function miniMapTooltipHandlers(mode: "preview" | "stagePreview") {
  return {
    onPointerOver: (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const target = event.target;
      const cell =
        target instanceof Element ? target.closest<HTMLElement>("i[data-x][data-y]") : null;
      if (!cell) {
        hideTooltip();
        return;
      }
      if (event.relatedTarget instanceof Node && cell.contains(event.relatedTarget)) return;
      const x = Number(cell.dataset.x);
      const y = Number(cell.dataset.y);
      const tile = cell.dataset.tile;
      if (!Number.isInteger(x) || !Number.isInteger(y) || typeof tile !== "string") return;
      showTooltip(
        cell,
        {
          kind: "tile",
          tile,
          mode,
          row: y + 1,
          column: x + 1,
          entityLabel: cell.dataset.entityId || undefined,
        },
        false,
      );
    },
    onPointerLeave: () => {
      hideTooltip();
    },
  };
}

/**
 * Returns event handlers that explicitly bind a typed tooltip descriptor to a control.
 * Never performs reverse text lookup against rendered labels.
 */
export function tooltipHandlers(descriptor: TooltipDescriptor) {
  return {
    onPointerEnter: (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const el = event.currentTarget;
      if (el instanceof HTMLElement) {
        queueTooltip(el, descriptor, false);
      }
    },
    onPointerLeave: () => {
      hideTooltip();
    },
    onFocus: (event: FocusEvent) => {
      const el = event.currentTarget;
      if (el instanceof HTMLElement) {
        queueTooltip(el, descriptor, true);
      }
    },
    onBlur: () => {
      hideTooltip();
    },
  };
}

function resolveTooltipContent(descriptor: TooltipDescriptor): {
  title: string;
  body: string;
  status: string;
} {
  const lang = language.value;
  if (descriptor.kind === "action") {
    return {
      title: t(lang, descriptor.titleKey),
      body: t(lang, `help.${descriptor.helpKey}` as TranslationKey),
      status: descriptor.status || "",
    };
  }
  if (descriptor.kind === "stage") {
    const dirtyWarning = isDirty.value ? t(lang, "help.stageDirtyWarning") : "";
    return {
      title: `${t(lang, "stage")} ${descriptor.stageNumber}`,
      body: `${t(lang, "help.stageBody")}${dirtyWarning}`,
      status: descriptor.status || "",
    };
  }
  if (descriptor.kind === "route") {
    return {
      title: t(lang, "help.routeTitle"),
      body: t(lang, "help.routeBody"),
      status: "",
    };
  }

  const displayKey = tileDisplayKey(descriptor.tile);
  const title = [descriptor.entityLabel, t(lang, displayKey)].filter(Boolean).join(" · ");
  const coords =
    descriptor.mode === "palette" || descriptor.row == null || descriptor.column == null
      ? ""
      : t(lang, "help.tileCoords", { row: descriptor.row, column: descriptor.column });
  const tileHelpKey = `tileHelp.${displayKey}` as TranslationKey;
  const baseHelp = t(lang, tileHelpKey);

  let instruction: string;
  if (descriptor.mode === "palette") {
    instruction = t(lang, "help.tilePalette");
  } else if (descriptor.mode === "stagePreview") {
    instruction =
      t(lang, "help.tileStagePreview") +
      (isDirty.value ? t(lang, "help.stageDirtyWarning") : "");
  } else if (descriptor.mode === "preview") {
    instruction = t(lang, "help.tilePreview");
  } else if (descriptor.disabled) {
    instruction = t(lang, "help.tileDisabled");
  } else {
    instruction = t(lang, "help.tileEditable", {
      tool: t(lang, tileDisplayKey(selectedTile.value)),
    });
  }

  return {
    title,
    body: `${coords}${baseHelp} ${instruction}`.trim(),
    status: descriptor.disabled ? descriptor.status || "" : "",
  };
}

export function TooltipOverlay(): JSX.Element | null {
  const tipRef = useRef<HTMLDivElement | null>(null);
  const current = activeTooltip.value;

  useEffect(() => {
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") hideTooltip();
    };
    const onDismiss = () => hideTooltip();
    window.addEventListener("keydown", onEscape);
    window.addEventListener("scroll", onDismiss, true);
    window.addEventListener("resize", onDismiss);
    window.addEventListener("pointerdown", onDismiss);
    return () => {
      window.removeEventListener("keydown", onEscape);
      window.removeEventListener("scroll", onDismiss, true);
      window.removeEventListener("resize", onDismiss);
      window.removeEventListener("pointerdown", onDismiss);
    };
  }, []);

  useEffect(() => {
    const node = tipRef.current;
    if (!current || !node || !current.anchor.isConnected) return;

    const anchor = current.anchor;
    const previousDescribedBy = anchor.getAttribute("aria-describedby");
    const tokens = (previousDescribedBy || "").split(/\s+/).filter(Boolean);
    const addedDescribedBy = !tokens.includes("uiTooltip");
    if (addedDescribedBy) {
      anchor.setAttribute("aria-describedby", [...tokens, "uiTooltip"].join(" "));
    }

    const rect = anchor.getBoundingClientRect();
    const box = node.getBoundingClientRect();
    const left = Math.max(
      8,
      Math.min(rect.left + rect.width / 2 - box.width / 2, window.innerWidth - box.width - 8),
    );
    const above = rect.top >= box.height + 16;
    const top = above
      ? rect.top - box.height - 10
      : Math.min(rect.bottom + 10, window.innerHeight - box.height - 8);

    node.style.left = `${left}px`;
    node.style.top = `${Math.max(8, top)}px`;
    node.dataset.placement = above ? "above" : "below";
    node.style.setProperty(
      "--help-anchor-x",
      `${Math.max(14, Math.min(rect.left + rect.width / 2 - left, box.width - 14))}px`,
    );

    return () => {
      if (!addedDescribedBy) return;
      if (previousDescribedBy === null) {
        anchor.removeAttribute("aria-describedby");
      } else {
        anchor.setAttribute("aria-describedby", previousDescribedBy);
      }
    };
  }, [current]);

  if (!current || !current.anchor.isConnected) return null;
  const content = resolveTooltipContent(current.descriptor);

  return (
    <div ref={tipRef} class="ui-tooltip" id="uiTooltip" role="tooltip">
      <strong>{content.title}</strong>
      <p>{content.body}</p>
      {content.status ? <p class="ui-tooltip-status">{content.status}</p> : null}
    </div>
  );
}
