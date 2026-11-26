import {
  DRAFT_REGENERATION_API_TIMEOUT_MS,
  DRAFT_REGENERATION_POLL_TIMEOUT_MS,
} from "../../api/cocreationApi";
import type { PreparedDraftRegenerationHandle } from "../../bridge/unityBridge";
import { t, type TranslationKey } from "../../i18n";
import type { Session } from "../../types";
import { recoverPendingMessageFromSession, restoreComposerDraft } from "../chatStore";
import {
  applySession,
  busy,
  draftRegenerationPending,
  hideGlobalNotice,
  language,
  session,
  sessionId,
  showGlobalNotice,
} from "../sessionStore";
import {
  api,
  asStructuredError,
  getBridge,
  showError,
  showFormalStandaloneBlock,
  uniqueId,
  workflowHooks,
} from "./shared";

let draftRegenerationKey = "";

function draftBridgeFailureKey(reason: string | undefined): TranslationKey {
  const map: Record<string, TranslationKey> = {
    protocol_mismatch: "draftProtocolMismatch",
    session_mismatch: "draftSessionMismatch",
    unity_not_ready: "draftUnityNotReady",
    receiver_not_ready: "draftReceiverNotReady",
    prepare_missing: "draftPrepareExpired",
    prepare_expired: "draftPrepareExpired",
    request_mismatch: "draftPrepareExpired",
  };
  return (reason && map[reason]) || "draftProtocolUnavailable";
}

function draftRegenerationFailureMessage(failureCode: string | null | undefined): string {
  const map: Record<string, TranslationKey> = {
    blueprint_unavailable: "draftBlueprintUnavailable",
    generation_timed_out: "draftGenerationTimedOut",
    generation_failed: "draftGenerationFailed",
    llm_failed: "draftLlmFailed",
    validation_failed: "draftValidationFailed",
    unity_unavailable: "draftUnityUnavailable",
  };
  const detailKey = (failureCode && map[failureCode]) || "draftUnityUnavailable";
  return `${t(language.value, "draftRegenerationFailed")} ${t(language.value, detailKey)}`;
}

async function createOrRecoverDraftRegeneration(
  sid: string,
  idempotencyKey: string,
): Promise<Session> {
  const create = () =>
    api.createDraftRegeneration(sid, idempotencyKey, {
      timeoutMs: DRAFT_REGENERATION_API_TIMEOUT_MS,
    });
  try {
    return await create();
  } catch (error) {
    const err = asStructuredError(error);
    const shouldRecover =
      Boolean(err.retryable) ||
      err.code === "CLIENT_TIMEOUT" ||
      err.code === "RETRYABLE_REQUEST_FAILED" ||
      err.code === "INVALID_RESPONSE";
    if (!shouldRecover) throw error;

    try {
      const latest = await api.getSession(sid, { timeoutMs: DRAFT_REGENERATION_POLL_TIMEOUT_MS });
      const status = latest.draftPreview?.regenerationStatus;
      if (status === "pending" || status === "claimed") {
        return latest;
      }
    } catch {
      // Retry idempotent create below.
    }
    return create();
  }
}

export async function ensureDraftRegenerationPolling(requestId: string): Promise<void> {
  const sid = sessionId.value;
  if (!sid || !requestId) return;
  return getBridge().pollDraftRegeneration({
    api,
    sessionId: sid,
    requestId,
    getCurrentSessionId: () => sessionId.value,
    isLanguageLocked: () => Boolean(session.value?.languageLocked),
    onSessionUpdate: (latest) => {
      applySession(latest);
    },
    onEnteredWorkspace: async () => {
      await workflowHooks.enterWorkspaceFromSession?.();
      restoreComposerDraft();
      recoverPendingMessageFromSession();
    },
    onReconnecting: () => {
      showGlobalNotice(t(language.value, "draftReconnecting"));
    },
    onTerminalFailure: (failureCode) => {
      draftRegenerationKey = "";
      showGlobalNotice(draftRegenerationFailureMessage(failureCode));
    },
    onComplete: () => {
      draftRegenerationKey = "";
      hideGlobalNotice();
    },
  });
}

export function resumeDraftRegenerationPolling(): void {
  const preview = session.value?.draftPreview;
  if (
    !preview?.regenerationRequestId ||
    (preview.regenerationStatus !== "pending" && preview.regenerationStatus !== "claimed")
  ) {
    return;
  }
  void ensureDraftRegenerationPolling(preview.regenerationRequestId).catch((error) =>
    showError(error, () => resumeDraftRegenerationPolling()),
  );
}

export async function regenerateDraft(): Promise<void> {
  if (busy.value || draftRegenerationPending.value) return;
  const s = session.value;
  if (!s) {
    if (!sessionId.value) void workflowHooks.createDemoSession?.();
    return;
  }
  if (!s.draftPreview?.mutable || s.languageLocked) return;

  const b = getBridge();
  if (!s.demoMode && !b.hasEmbeddedFormalHost(s.sessionId)) {
    showFormalStandaloneBlock();
    return;
  }

  const key = draftRegenerationKey || uniqueId("draft_regeneration");
  draftRegenerationKey = key;
  let preparedHandle: PreparedDraftRegenerationHandle | null = null;

  draftRegenerationPending.value = true;
  busy.value = true;
  hideGlobalNotice();

  try {
    if (!s.demoMode) {
      const prepared = await b.prepareDraftRegeneration(s.sessionId);
      if (!prepared.accepted) {
        showGlobalNotice(t(language.value, draftBridgeFailureKey(prepared.reason)));
        return;
      }
      preparedHandle = prepared;
    }

    const updated = await createOrRecoverDraftRegeneration(s.sessionId, key);
    applySession(updated);

    const requestId = updated.draftPreview?.regenerationRequestId;
    if (!updated.demoMode && requestId && preparedHandle) {
      const delivery = await b.sendDraftRegenerationRequest(preparedHandle, s.sessionId, requestId);
      if (delivery.received && !delivery.accepted) {
        const latest = await api.getSession(s.sessionId, {
          timeoutMs: DRAFT_REGENERATION_POLL_TIMEOUT_MS,
        });
        applySession(latest);
        if (
          latest.draftPreview?.regenerationRequestId === requestId &&
          latest.draftPreview.regenerationStatus === "pending"
        ) {
          const cancelled = await api.cancelDraftRegeneration(s.sessionId, requestId);
          applySession(cancelled);
          draftRegenerationKey = "";
          showGlobalNotice(t(language.value, draftBridgeFailureKey(delivery.reason)));
          return;
        }
      }
      await ensureDraftRegenerationPolling(requestId);
    }
    draftRegenerationKey = "";
  } catch (error) {
    if (preparedHandle) {
      b.abortDraftRegeneration(preparedHandle, s.sessionId);
    }
    const err = asStructuredError(error);
    if (!err.retryable) draftRegenerationKey = "";
    showError(err, () => void regenerateDraft());
  } finally {
    if (!session.value?.demoMode) {
      b.requestHostView(
        "sokoban:cocreation-show-lab",
        s.sessionId,
        session.value?.draftPreview?.regenerationRequestId || "",
      );
    }
    busy.value = false;
    draftRegenerationPending.value = false;
  }
}

workflowHooks.ensureDraftRegenerationPolling = ensureDraftRegenerationPolling;
