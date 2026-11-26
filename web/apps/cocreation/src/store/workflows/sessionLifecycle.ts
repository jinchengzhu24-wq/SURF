import { isLevelValidationError, t } from "../../i18n";
import { localStore, storageKeys } from "../../storage/safeStorage";
import type { Language, PendingFinalize, PendingIntention } from "../../types";
import {
  recoverPendingMessageFromSession,
  restoreComposerDraft,
  stopChatTimer,
} from "../chatStore";
import { deadlineClock } from "../deadlineClock";
import {
  activeCoordinateLink,
  clearUnsavedMapDraft,
  draftRows,
  isDirty,
  resetDraftFromVersion,
  restoreUnsavedMapDraft,
  validationError,
} from "../editorStore";
import {
  applySession,
  busy,
  demoCreationFailed,
  demoCreationPending,
  finalizeModalOpen,
  findVersion,
  formalStandaloneBlocked,
  hideGlobalNotice,
  intentionDraft,
  isDeadlineExpired,
  language,
  readUrlHash,
  selectedVersionId,
  session,
  sessionId,
  setHeaderStatus,
  showGlobalNotice,
  showPlayReturnNotice,
  stripBootstrapFromHash,
  syncUrlHash,
  unityPlayActive,
} from "../sessionStore";
import { cancelBinaryRefreshTimer, scheduleBinaryRefresh } from "./binaryDiscovery";
import { resumeDraftRegenerationPolling } from "./draftRegeneration";
import {
  cancelComponentRecoveryTimer,
  clearPausedComponentRecoveries,
  scheduleComponentRecovery,
} from "./sendMessage";
import {
  api,
  asStructuredError,
  destroyBridge,
  enterLanding,
  getBridge,
  showError,
  showFormalStandaloneBlock,
  uniqueId,
  withBusy,
  workflowHooks,
} from "./shared";
import { clearTranslationRetryTimer, ensureVisibleTranslations } from "./translationQueue";
import { clearAssessmentTimers, ensureAssessment } from "./versionActions";

let pendingFinalize: PendingFinalize | null = null;
let pendingIntention: PendingIntention | null = null;
let lifecycleBound = false;

export async function enterWorkspaceFromSession(): Promise<void> {
  const s = session.value;
  if (!s) return;
  formalStandaloneBlocked.value = false;
  language.value = s.language || language.value;
  if (!findVersion(selectedVersionId.value)) {
    selectedVersionId.value = s.currentVersionId;
  }
  localStore.set(storageKeys.selectedStage(s.sessionId), selectedVersionId.value);
  syncUrlHash();
  activeCoordinateLink.value = null;
  resetDraftFromVersion();
  restoreUnsavedMapDraft();
  setHeaderStatus("ready", "ready");
  scheduleBinaryRefresh();
  scheduleComponentRecovery();
  void ensureVisibleTranslations();
  await ensureAssessment(s.currentVersionId);
}

export async function refreshSession(): Promise<boolean> {
  const sid = sessionId.value;
  if (!sid) return false;
  const latest = await api.getSession(sid);
  applySession(latest);

  const b = getBridge();
  b.announceLabReady(sid);

  if (!latest.demoMode && !b.hasEmbeddedFormalHost(sid)) {
    showFormalStandaloneBlock();
    return false;
  }
  formalStandaloneBlocked.value = false;

  if (!latest.languageLocked) {
    language.value = latest.language || language.value;
    enterLanding(latest.demoMode ? "demo-preview" : "formal");
    resumeDraftRegenerationPolling();
    return false;
  }

  await enterWorkspaceFromSession();
  return true;
}

export async function initializeSession(): Promise<void> {
  bindLifecycleListeners();
  const hash = readUrlHash();
  sessionId.value = hash.session;
  getBridge();

  if (!hash.session) {
    applySession(null);
    language.value = "zh-CN";
    void createDemoSession();
    return;
  }

  try {
    if (hash.bootstrap) {
      try {
        await api.exchangeBrowserAccess(hash.session, hash.bootstrap);
      } catch (error) {
        const err = asStructuredError(error);
        if (err.code !== "BOOTSTRAP_TOKEN_USED") throw error;
      }
      stripBootstrapFromHash();
    }

    localStore.set(storageKeys.lastSession(), hash.session);
    selectedVersionId.value =
      hash.stage || localStore.get<string>(storageKeys.selectedStage(hash.session)) || "";

    const enteredWorkspace = await refreshSession();
    if (!enteredWorkspace) return;
    restoreComposerDraft();
    recoverPendingMessageFromSession();
    showPlayReturnNotice(hash.playReturn);
  } catch (error) {
    enterLanding("demo-error");
    showError(error, () => void initializeSession());
  }
}

async function openCreatedSession(launchUrl: string): Promise<void> {
  const destination = new URL(launchUrl, window.location.href);
  if (destination.origin !== window.location.origin) {
    getBridge().navigateToPlayUrl(destination.href);
    return;
  }
  window.history.replaceState(
    null,
    "",
    `${destination.pathname}${destination.search}${destination.hash}`,
  );
  await initializeSession();
}

export async function createDemoSession(): Promise<void> {
  if (busy.value || demoCreationPending.value) return;
  demoCreationPending.value = true;
  demoCreationFailed.value = false;
  busy.value = true;
  hideGlobalNotice();
  enterLanding("demo-loading");

  try {
    const created = await api.createDemoSession(language.value, uniqueId("demo"));
    await openCreatedSession(created.launchUrl);
  } catch (error) {
    demoCreationFailed.value = true;
    showError(error, () => void createDemoSession());
  } finally {
    busy.value = false;
    demoCreationPending.value = false;
    if (session.value?.demoMode && !session.value.languageLocked) {
      enterLanding("demo-preview");
    } else if (!session.value) {
      enterLanding("demo-error");
    }
  }
}

export function selectSetupLanguage(nextLang: Language): void {
  if (busy.value || demoCreationPending.value || session.value?.languageLocked) return;
  language.value = nextLang;
}

export async function confirmSessionLanguage(): Promise<void> {
  const s = session.value;
  if (!s || s.languageLocked) return;
  if (!s.demoMode && !getBridge().hasEmbeddedFormalHost(s.sessionId)) {
    showFormalStandaloneBlock();
    return;
  }
  await withBusy(async () => {
    await api.setSessionLanguage(s.sessionId, language.value);
    const entered = await refreshSession();
    if (entered) {
      restoreComposerDraft();
      recoverPendingMessageFromSession();
    }
  });
}

export function requestFinalize(): void {
  if (isDeadlineExpired.value) {
    void finalizeSession();
  } else {
    finalizeModalOpen.value = true;
  }
}

export async function finalizeSession(): Promise<void> {
  finalizeModalOpen.value = false;
  const s = session.value;
  if (!s) return;

  const targetVersionId = selectedVersionId.value;
  const targetIsCurrent = targetVersionId === s.currentVersionId;
  const rows =
    isDeadlineExpired.value && targetIsCurrent && isDirty.value ? draftRows.value.slice() : null;
  const rowsSignature = rows ? rows.join("\n") : null;

  if (
    !pendingFinalize ||
    pendingFinalize.sessionId !== s.sessionId ||
    pendingFinalize.targetVersionId !== targetVersionId ||
    pendingFinalize.rowsSignature !== rowsSignature
  ) {
    pendingFinalize = {
      sessionId: s.sessionId,
      targetVersionId,
      rowsSignature,
      idempotencyKey: uniqueId("finalize"),
    };
  }

  const key = pendingFinalize.idempotencyKey;
  await withBusy(async () => {
    validationError.value = null;
    try {
      const updated = await api.finalizeSession(s.sessionId, {
        baseVersionId: targetVersionId,
        idempotencyKey: key,
        rows,
      });
      applySession(updated);
      pendingFinalize = null;
      clearUnsavedMapDraft();
    } catch (error) {
      const err = asStructuredError(error);
      if (isLevelValidationError(err)) {
        validationError.value = err;
      }
      throw error;
    }
  });
}

export async function submitIntention(): Promise<void> {
  const s = session.value;
  if (!s) return;
  const content = intentionDraft.value.trim();
  if (!content) {
    showGlobalNotice(t(language.value, "errors.INTENT_REQUIRED"));
    return;
  }

  if (
    !pendingIntention ||
    pendingIntention.sessionId !== s.sessionId ||
    pendingIntention.content !== content
  ) {
    pendingIntention = {
      sessionId: s.sessionId,
      content,
      idempotencyKey: uniqueId("intention"),
    };
  }

  const key = pendingIntention.idempotencyKey;
  await withBusy(async () => {
    const updated = await api.submitIntention(s.sessionId, {
      content,
      idempotencyKey: key,
    });
    applySession(updated);
    pendingIntention = null;
    clearUnsavedMapDraft();
  });
}

export function returnToUnity(): void {
  hideGlobalNotice();
  const sid = sessionId.value;
  if (sid && getBridge().requestHostView("sokoban:cocreation-show-unity", sid)) {
    return;
  }
  showGlobalNotice(t(language.value, "returnUnityUnavailable"));
}

const handleOnline = () => {
  resumeDraftRegenerationPolling();
};

const handleVisibilityChange = () => {
  if (typeof document !== "undefined" && !document.hidden) {
    resumeDraftRegenerationPolling();
    clearPausedComponentRecoveries();
  }
  scheduleComponentRecovery();
  scheduleBinaryRefresh();
};

const handlePageHide = () => {
  cancelComponentRecoveryTimer();
  cancelBinaryRefreshTimer();
};

function bindLifecycleListeners(): void {
  if (lifecycleBound || typeof window === "undefined") return;
  lifecycleBound = true;

  deadlineClock.setOnExpired(() => {
    clearAssessmentTimers();
    if (unityPlayActive.value && sessionId.value) {
      unityPlayActive.value = false;
      getBridge().requestHostView("sokoban:cocreation-show-lab", sessionId.value);
    }
  });

  window.addEventListener("online", handleOnline);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("pagehide", handlePageHide);
}

export function destroyWorkflows(): void {
  if (lifecycleBound && typeof window !== "undefined") {
    window.removeEventListener("online", handleOnline);
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    window.removeEventListener("pagehide", handlePageHide);
    lifecycleBound = false;
  }
  stopChatTimer();
  clearAssessmentTimers();
  cancelBinaryRefreshTimer();
  cancelComponentRecoveryTimer();
  clearTranslationRetryTimer();
  deadlineClock.destroy();
  destroyBridge();
}

workflowHooks.refreshSession = refreshSession;
workflowHooks.initializeSession = initializeSession;
workflowHooks.enterWorkspaceFromSession = enterWorkspaceFromSession;
workflowHooks.createDemoSession = createDemoSession;
