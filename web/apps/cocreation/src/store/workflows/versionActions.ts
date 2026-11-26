import { REQUEST_COPY, isLevelValidationError, t } from "../../i18n";
import { localStore, storageKeys } from "../../storage/safeStorage";
import {
  assessingVersionIds,
  assessmentRetryingVersionIds,
  clearReplyNotice,
  composerText,
  proposalMode,
  restoreComposerDraft,
  setProposalMode,
  updateSetSignal,
} from "../chatStore";
import {
  activeCoordinateLink,
  clearUnsavedMapDraft,
  draftRows,
  isDirty,
  resetDraftFromVersion,
  validationError,
} from "../editorStore";
import {
  applySession,
  canEditSelected,
  currentPendingProposal,
  findVersion,
  hideGlobalNotice,
  isDeadlineExpired,
  language,
  selectedVersion,
  selectedVersionId,
  session,
  sessionId,
  showGlobalNotice,
  syncUrlHash,
  unityPlayActive,
} from "../sessionStore";
import {
  api,
  asStructuredError,
  getBridge,
  maxTurnSequence,
  retryDelay,
  showError,
  showFormalStandaloneBlock,
  uniqueId,
  withBusy,
  workflowHooks,
} from "./shared";
import { ensureVisibleTranslations } from "./translationQueue";

const assessmentIdempotencyKeys = new Map<string, string>();
const assessmentRetryCounts = new Map<string, number>();
const assessmentRetryTimers = new Map<string, ReturnType<typeof setTimeout>>();

export function selectVersion(
  versionId: string,
  options: { forceReset?: boolean; preserveSameStageDraft?: boolean } = {},
): void {
  const s = session.value;
  if (!s || !findVersion(versionId)) return;
  if (selectedVersionId.value === versionId && !options.forceReset) return;

  const preserveSameStageDraft = Boolean(
    options.preserveSameStageDraft && selectedVersionId.value === versionId && isDirty.value,
  );
  if (selectedVersionId.value && (selectedVersionId.value !== versionId || options.forceReset)) {
    setProposalMode(false);
    clearUnsavedMapDraft();
  }

  selectedVersionId.value = versionId;
  localStore.remove(storageKeys.proposalMode(s.sessionId, versionId));
  proposalMode.value = false;

  if (!preserveSameStageDraft) {
    activeCoordinateLink.value = null;
    resetDraftFromVersion();
  }

  localStore.set(storageKeys.selectedStage(s.sessionId), versionId);
  syncUrlHash();
  restoreComposerDraft();
  workflowHooks.scheduleComponentRecovery?.();
  void ensureVisibleTranslations();
}

export function selectCurrentVersion(): void {
  if (session.value?.currentVersionId) {
    selectVersion(session.value.currentVersionId);
  }
}

export function clearAssessmentTimers(): void {
  for (const timer of assessmentRetryTimers.values()) {
    clearTimeout(timer);
  }
  assessmentRetryTimers.clear();
  assessmentRetryingVersionIds.value = new Set();
}

function scheduleAssessmentRetry(versionId: string): void {
  if (isDeadlineExpired.value || assessmentRetryTimers.has(versionId)) return;
  const attempt = assessmentRetryCounts.get(versionId) ?? 0;
  assessmentRetryCounts.set(versionId, attempt + 1);

  const timerId = globalThis.setTimeout(() => {
    assessmentRetryTimers.delete(versionId);
    updateSetSignal(assessmentRetryingVersionIds, (set) => set.delete(versionId));
    void ensureAssessment(versionId);
  }, retryDelay(attempt));

  assessmentRetryTimers.set(versionId, timerId);
  updateSetSignal(assessmentRetryingVersionIds, (set) => set.add(versionId));
}

export async function ensureAssessment(versionId: string): Promise<void> {
  if (isDeadlineExpired.value) return;
  const s = session.value;
  const capturedSessionId = sessionId.value;
  const version = findVersion(versionId);
  if (
    !s ||
    !capturedSessionId ||
    version?.openingTurnId ||
    s.assessments.some((item) => item.versionId === versionId) ||
    assessingVersionIds.value.has(versionId)
  ) {
    return;
  }

  updateSetSignal(assessingVersionIds, (set) => set.add(versionId));
  let idempotencyKey = assessmentIdempotencyKeys.get(versionId);
  if (!idempotencyKey) {
    idempotencyKey = uniqueId("assessment");
    assessmentIdempotencyKeys.set(versionId, idempotencyKey);
  }

  try {
    const updatedSession = await api.createAssessment(capturedSessionId, versionId, idempotencyKey);
    if (sessionId.value !== capturedSessionId || !session.value) return;

    const currentMaxSeq = maxTurnSequence(session.value);
    const updatedMaxSeq = maxTurnSequence(updatedSession);
    const versionsBehind = (updatedSession.versions?.length || 0) < (session.value.versions?.length || 0);

    if (versionsBehind || updatedMaxSeq < currentMaxSeq) {
      const mergedAssessments = (updatedSession.assessments || []).filter(
        (item) => !session.value!.assessments.some((existing) => existing.versionId === item.versionId),
      );
      if (mergedAssessments.length > 0) {
        session.value = {
          ...session.value,
          assessments: [...session.value.assessments, ...mergedAssessments],
        };
      }
    } else {
      applySession(updatedSession);
    }

    assessmentRetryCounts.delete(versionId);
    assessmentIdempotencyKeys.delete(versionId);
    hideGlobalNotice();
    clearReplyNotice();
    void ensureVisibleTranslations();
  } catch (error) {
    if (sessionId.value !== capturedSessionId) return;
    const err = asStructuredError(error);
    if (err.retryable && !isDeadlineExpired.value) {
      scheduleAssessmentRetry(versionId);
      return;
    }
    if (!isDeadlineExpired.value) {
      showError(err, () => void ensureAssessment(versionId), { versionId });
    }
  } finally {
    updateSetSignal(assessingVersionIds, (set) => set.delete(versionId));
    workflowHooks.scheduleComponentRecovery?.();
  }
}

export async function saveManualStage(): Promise<void> {
  const s = session.value;
  if (!s || !isDirty.value) return;
  const payload = {
    rows: draftRows.value.slice(),
    baseVersionId: s.currentVersionId,
    idempotencyKey: uniqueId("manual"),
    summary: REQUEST_COPY[language.value].manualEditSummary,
  };
  await withBusy(async () => {
    validationError.value = null;
    try {
      const updated = await api.saveManualVersion(s.sessionId, payload);
      applySession(updated);
      clearUnsavedMapDraft();
      selectVersion(updated.currentVersionId, { forceReset: true });
      await ensureAssessment(updated.currentVersionId);
    } catch (error) {
      const err = asStructuredError(error);
      if (isLevelValidationError(err)) {
        validationError.value = err;
      }
      throw error;
    }
  });
}

export async function restoreSelectedStage(): Promise<void> {
  const s = session.value;
  const version = selectedVersion.value;
  if (!s || !version) return;
  await withBusy(async () => {
    const updated = await api.restoreVersion(s.sessionId, version.versionId, {
      baseVersionId: s.currentVersionId,
      idempotencyKey: uniqueId("restore"),
    });
    applySession(updated);
    clearUnsavedMapDraft();
    selectVersion(updated.currentVersionId, { forceReset: true });
    await ensureAssessment(updated.currentVersionId);
  });
}

export async function playSelectedStage(): Promise<void> {
  const s = session.value;
  if (!s) return;
  const b = getBridge();
  if (!s.demoMode && !b.hasEmbeddedFormalHost(s.sessionId)) {
    showFormalStandaloneBlock();
    return;
  }
  if (isDirty.value) {
    showGlobalNotice(t(language.value, "errors.DIRTY_PLAY"));
    return;
  }
  if (currentPendingProposal.value) {
    showGlobalNotice(t(language.value, "errors.PENDING_PLAY"));
    return;
  }
  const version = selectedVersion.value;
  if (!version) return;

  const playKey = uniqueId("play");
  await withBusy(async () => {
    localStore.set(storageKeys.selectedStage(s.sessionId), version.versionId);
    if (canEditSelected.value) {
      localStore.set(storageKeys.composer(s.sessionId, s.currentVersionId), composerText.value);
    }
    const payload = await api.createPlayAttempt(s.sessionId, version.versionId, playKey);

    if (!s.demoMode) {
      if (await b.requestPlayInUnity(s.sessionId, payload.playUrl)) {
        unityPlayActive.value = true;
        showGlobalNotice(t(language.value, "playOpenedInUnity"));
        return;
      }
      if (b.isEmbeddedParent()) {
        showGlobalNotice(t(language.value, "playUnityUnavailable"));
        return;
      }
    }

    b.navigateToPlayUrl(payload.playUrl);
  });
}
