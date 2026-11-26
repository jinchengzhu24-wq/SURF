import { cocreationApi, type CocreationApiClient } from "../../api/cocreationApi";
import { createUnityBridge, type UnityBridgeClient } from "../../bridge/unityBridge";
import { localizeError } from "../../i18n";
import type { Session, StructuredErrorLike } from "../../types";
import {
  chatBusy,
  clearReplyNotice,
  questionFeedbackBusyKeys,
  recoverPendingMessageFromSession,
  restoreComposerDraft,
  showReplyNotice,
} from "../chatStore";
import { deadlineClock } from "../deadlineClock";
import {
  applySession,
  busy,
  canEditSelected,
  demoCreationPending,
  embeddedHostReady,
  formalStandaloneBlocked,
  hideGlobalNotice,
  isWorkspaceVisible,
  landingMode,
  language,
  selectedVersionId,
  session,
  sessionId,
  setHeaderStatus,
  showGlobalNotice,
  showPlayReturnNotice,
  unityPlayActive,
} from "../sessionStore";

export const api: CocreationApiClient = cocreationApi;

export interface WorkflowHooks {
  refreshSession?: () => Promise<boolean>;
  initializeSession?: () => Promise<void>;
  enterWorkspaceFromSession?: () => Promise<void>;
  createDemoSession?: () => Promise<void>;
  ensureDraftRegenerationPolling?: (requestId: string) => Promise<void>;
  scheduleBinaryRefresh?: () => void;
  scheduleComponentRecovery?: () => void;
  submitPendingMessage?: () => Promise<void>;
}

/**
 * Late-bound cross-module workflow callbacks so feature modules depend only
 * on `shared.ts` without forming circular imports.
 */
export const workflowHooks: WorkflowHooks = {};

let bridge: UnityBridgeClient | null = null;

export function retryDelay(attempt: number): number {
  return Math.min(60_000, 2_000 * 2 ** Math.min(Math.max(0, attempt), 5));
}

export function uniqueId(prefix: string): string {
  const value =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID().replaceAll("-", "")
      : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${value}`.slice(0, 64);
}

export function asStructuredError(error: unknown): StructuredErrorLike {
  if (error && typeof error === "object") {
    const e = error as StructuredErrorLike & { message?: string };
    return {
      code: e.code,
      message: e.message,
      retryable: Boolean(e.retryable),
      status: e.status,
      details: e.details ?? null,
    };
  }
  return { code: "REQUEST_FAILED", message: String(error ?? ""), retryable: false };
}

export function isInteractionBusy(): boolean {
  return busy.value || chatBusy.value || questionFeedbackBusyKeys.value.size > 0;
}

export function maxTurnSequence(value: Session | null | undefined): number {
  return Math.max(0, ...(value?.turns || []).map((turn) => turn.sequence || 0));
}

export async function tryRefreshSession(): Promise<void> {
  try {
    await workflowHooks.refreshSession?.();
  } catch {
    // Keep original operation error visible.
  }
}

export function getBridge(): UnityBridgeClient {
  if (!bridge) {
    bridge = createUnityBridge({
      getSessionId: () => sessionId.value,
      onHostReady: () => {
        embeddedHostReady.value = true;
        const refresh = workflowHooks.refreshSession;
        if (session.value && !session.value.demoMode && formalStandaloneBlocked.value && refresh) {
          void refresh()
            .then((entered) => {
              if (entered) {
                restoreComposerDraft();
                recoverPendingMessageFromSession();
              }
            })
            .catch((error) => showError(error, () => void workflowHooks.initializeSession?.()));
        }
      },
      onPlayReturn: (event) => {
        unityPlayActive.value = false;
        const refresh = workflowHooks.refreshSession;
        if (refresh) {
          void refresh()
            .then((entered) => {
              if (entered) {
                restoreComposerDraft();
                recoverPendingMessageFromSession();
                showPlayReturnNotice(event.status);
              }
            })
            .catch((error) => showError(error, () => void refresh()));
        }
      },
      onDraftRegenerateReturn: (event) => {
        const currentRequestId =
          session.value?.draftPreview?.regenerationRequestId || bridge?.activePollRequestId || "";
        if (
          !currentRequestId ||
          event.requestId !== currentRequestId ||
          (event.status !== "completed" && event.status !== "failed")
        ) {
          return;
        }
        const poll = workflowHooks.ensureDraftRegenerationPolling;
        if (poll) {
          void poll(currentRequestId).catch((error) =>
            showError(error, () => void poll(currentRequestId)),
          );
        }
      },
    });
  }
  return bridge;
}

export function destroyBridge(): void {
  bridge?.destroy();
  bridge = null;
}

export function showError(
  error: unknown,
  retryAction: (() => void | Promise<void>) | null = null,
  options: { versionId?: string } = {},
): void {
  const err = asStructuredError(error);
  if (session.value && isWorkspaceVisible.value && canEditSelected.value) {
    showReplyNotice(
      "",
      err,
      err.retryable ? retryAction : null,
      options.versionId ?? selectedVersionId.value,
    );
    return;
  }
  showGlobalNotice(localizeError(language.value, err), {
    error: err,
    retryAction: err.retryable ? retryAction : null,
  });
}

export function showFormalStandaloneBlock(): void {
  deadlineClock.reset();
  formalStandaloneBlocked.value = true;
  setHeaderStatus("ready", "ready");
}

export function enterLanding(
  mode: "demo" | "demo-loading" | "demo-preview" | "demo-error" | "formal",
): void {
  formalStandaloneBlocked.value = false;
  landingMode.value = mode;
  if (demoCreationPending.value) {
    setHeaderStatus("draftInitialGenerating", "pending");
  } else if (session.value && !session.value.languageLocked) {
    setHeaderStatus("ready", "ready");
  } else {
    setHeaderStatus("errors.NO_SESSION", "pending");
  }
}

export async function withBusy(
  action: () => Promise<void>,
  onError?: () => void,
): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  hideGlobalNotice();
  clearReplyNotice();
  try {
    await action();
  } catch (error) {
    const err = asStructuredError(error);
    onError?.();
    let recoveredWorkspaceEntry = false;
    if (err.retryable && sessionId.value) {
      try {
        const wasHidden = !isWorkspaceVisible.value;
        const latest = await api.getSession(sessionId.value);
        applySession(latest);
        if (!latest.languageLocked) {
          enterLanding(latest.demoMode ? "demo-preview" : "formal");
        } else if (wasHidden) {
          await workflowHooks.enterWorkspaceFromSession?.();
          restoreComposerDraft();
          recoverPendingMessageFromSession();
          recoveredWorkspaceEntry =
            err.code !== "ONLINE_FLOW_SYNC_UNAVAILABLE" &&
            err.code !== "ONLINE_INTENTION_SYNC_UNAVAILABLE";
        }
      } catch {
        // Preserve original operation error.
      }
    }
    if (!recoveredWorkspaceEntry) {
      showError(err, () => void withBusy(action, onError));
    }
  } finally {
    busy.value = false;
    workflowHooks.scheduleBinaryRefresh?.();
    workflowHooks.scheduleComponentRecovery?.();
  }
}
