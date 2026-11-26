import { COMPONENT_RECOVERY_TIMEOUT_MS } from "../../api/cocreationApi";
import { REQUEST_COPY, t } from "../../i18n";
import { recordReplyDiagnostic } from "../../storage/safeStorage";
import type { ComponentRecovery, RevisionCardAction, Session, Turn } from "../../types";
import {
  canUseChallengeChoice, chatBusy, chatBusyVersionId, chatError, chatStatus,
  clearComposerText, clearPendingMessage, clearReplyNotice, composerText,
  isCardActionPending, pendingMessage, persistPendingMessage, proposalMode,
  recoverMessageFailure, setComposerText, setProposalMode, showReplyNotice,
  startChatTimer, stopChatTimer,
} from "../chatStore";
import {
  activeChallengeState, applySession, binaryFlowActive, busy, canEditSelected,
  hideGlobalNotice, isDeadlineExpired, language, latestRevisionOfferTurnId,
  proposalFlowActive, selectedStageHasActiveDisagreement, selectedVersionId,
  session, sessionId, unityPlayActive,
} from "../sessionStore";
import { proposalStateLabel } from "./proposalActions";
import {
  api, asStructuredError, isInteractionBusy, maxTurnSequence, tryRefreshSession,
  uniqueId, workflowHooks,
} from "./shared";
import { ensureVisibleTranslations } from "./translationQueue";

const REFRESH_BEFORE_ERROR_CODES = new Set([
  "REQUIREMENT_PERMISSION_CONFLICT",
  "PROPOSAL_CHOICE_REQUIRED",
  "BINARY_BUSY",
  "DISAGREEMENT_ACTIVE",
  "INTENT_CONFLICT_PENDING",
  "SESSION_DEADLINE_EXPIRED",
  "PENDING_PROPOSAL",
  "PROPOSAL_PENDING",
  "VERSION_CONFLICT",
  "TURN_NOT_FOUND",
  "STALE_REVISION_BINDING",
  "UNBOUND_REVISION",
  "STALE_PROPOSAL",
  "QUESTION_FEEDBACK_STALE",
  "INTENT_HYPOTHESIS_STALE",
]);

const CARD_STALE_ERROR_CODES = new Set([
  "INVALID_CARD_SOURCE",
  "PROPOSAL_STALE",
  "STALE_PROPOSAL",
  "PROPOSAL_PRECONDITION_FAILED",
  "CHALLENGE_STALE",
  "TURN_NOT_FOUND",
  "STALE_REVISION_BINDING",
  "UNBOUND_REVISION",
]);

const componentRecoveryState = {
  timer: null as ReturnType<typeof setTimeout> | null,
  busy: false,
  scheduled: "",
  dispatched: new Set<string>(),
  paused: new Set<string>(),
};

export function clearPausedComponentRecoveries(): void {
  componentRecoveryState.paused.clear();
}

export function toggleProposalMode(): void {
  if (
    busy.value || !canEditSelected.value ||
    selectedStageHasActiveDisagreement.value || proposalFlowActive.value
  ) {
    return;
  }
  setProposalMode(!proposalMode.value);
}

export async function sendMessage(): Promise<void> {
  if (binaryFlowActive.value) return;
  const challenge = activeChallengeState.value;
  if (challenge && (challenge.status === "choice_pending" || challenge.status === "awaiting_choice")) {
    return;
  }
  const content = composerText.value.trim();
  const s = session.value;
  if (!content || busy.value || !s || !canEditSelected.value) return;

  const existing = pendingMessage.value;
  if (
    !existing || isCardActionPending(existing) ||
    existing.content !== content || existing.baseVersionId !== s.currentVersionId ||
    Boolean(existing.requestProposal) !== proposalMode.value
  ) {
    pendingMessage.value = {
      content,
      baseVersionId: s.currentVersionId,
      idempotencyKey: uniqueId(proposalMode.value ? "proposal-request" : "message"),
      requestProposal: proposalMode.value,
      ...(challenge ? { action: "continue_challenge" as const, challengeId: challenge.challengeId } : {}),
    };
  }
  persistPendingMessage();
  await submitPendingMessage();
}

export function sendRevisionCardAction(action: RevisionCardAction, turn: Turn): void {
  const s = session.value;
  if (
    !s || !canEditSelected.value || busy.value ||
    binaryFlowActive.value || selectedStageHasActiveDisagreement.value
  ) {
    return;
  }
  if (turn.proposalState && turn.proposalState.actionable === false) {
    showReplyNotice(proposalStateLabel(turn.proposalState.status));
    return;
  }
  if (latestRevisionOfferTurnId.value !== turn.turnId) {
    showReplyNotice(t(language.value, "staleRevisionCard"));
    return;
  }
  const copy = REQUEST_COPY[language.value];
  const content = action === "execute_revision"
    ? copy.executeBoundProposal
    : action === "challenge_revision"
      ? copy.challengeRevision
      : copy.alternativeRevision;

  pendingMessage.value = {
    content,
    baseVersionId: s.currentVersionId,
    idempotencyKey: uniqueId(`card-${action}`),
    requestProposal: false,
    action,
    sourceTurnId: turn.turnId,
  };
  persistPendingMessage();
  void submitPendingMessage();
}

export function sendChallengeChoice(challengeId: string, choice: "ai" | "user"): void {
  const s = session.value;
  if (!s || !canUseChallengeChoice(challengeId)) return;
  const copy = REQUEST_COPY[language.value];
  pendingMessage.value = {
    content: choice === "ai" ? copy.challengeChoiceAi : copy.challengeChoiceUser,
    baseVersionId: s.currentVersionId,
    idempotencyKey: uniqueId("challenge-choice"),
    requestProposal: false,
    action: "continue_challenge",
    challengeId,
    challengeChoice: choice,
  };
  persistPendingMessage();
  void submitPendingMessage();
}

export async function retryPendingMessage(): Promise<void> {
  const pending = pendingMessage.value;
  if (!pending || busy.value) return;
  if (!isCardActionPending(pending)) {
    setComposerText(pending.content);
  }
  await submitPendingMessage();
}

function finishPendingMessageSuccess(cardAction: boolean): void {
  if (!cardAction) {
    setProposalMode(false);
    clearComposerText();
  }
  clearPendingMessage();
  chatStatus.value = "idle";
  chatError.value = null;
}

export async function submitPendingMessage(): Promise<void> {
  const pending = pendingMessage.value;
  const sid = sessionId.value;
  if (!pending || !sid || busy.value) return;
  const cardAction = isCardActionPending(pending);

  busy.value = true;
  chatBusy.value = true;
  chatBusyVersionId.value = pending.baseVersionId;
  chatStatus.value = "waiting";
  chatError.value = null;
  hideGlobalNotice();
  clearReplyNotice();
  startChatTimer();

  recordReplyDiagnostic("transport", "started", {
    messageKey: pending.idempotencyKey,
    versionId: pending.baseVersionId,
  });

  try {
    const updated = await api.sendMessage(sid, pending);
    const assistantTurn = (updated.turns || []).find(
      (item) => item.role === "assistant" && item.requestId === pending.idempotencyKey,
    );
    recordReplyDiagnostic("response_body", "passed", {
      messageKey: pending.idempotencyKey,
      versionId: pending.baseVersionId,
      assistantTurnId: assistantTurn?.turnId,
      turnPresent: Boolean(assistantTurn),
      receivedCard: Boolean(assistantTurn?.guidance?.intentHypothesis),
      code: assistantTurn ? "" : "ASSISTANT_TURN_MISSING",
    });

    applySession(updated);
    finishPendingMessageSuccess(cardAction);
    void ensureVisibleTranslations();
  } catch (error) {
    let err = asStructuredError(error);
    recordReplyDiagnostic("transport", "failed", {
      messageKey: pending.idempotencyKey,
      versionId: pending.baseVersionId,
      code: err.code || "REQUEST_FAILED",
    });

    const isCard = Boolean(pending.action && pending.action !== "none");
    if ((err.code === "PROPOSAL_STALE" || err.code === "STALE_PROPOSAL") && !isCard) {
      clearPendingMessage();
      setComposerText(pending.content);
      await tryRefreshSession();
    } else if (err.code && CARD_STALE_ERROR_CODES.has(err.code) && isCard) {
      clearPendingMessage();
      await tryRefreshSession();
    } else if (err.code && REFRESH_BEFORE_ERROR_CODES.has(err.code)) {
      await tryRefreshSession();
    } else if (err.retryable) {
      try {
        const latest = await api.getSession(sid);
        applySession(latest);
        err = recoverMessageFailure(err, latest, pending);
        const committed = latest.turns.some(
          (turn) =>
            turn.role === "assistant" &&
            turn.versionId === pending.baseVersionId &&
            turn.requestId === pending.idempotencyKey,
        );
        if (committed) {
          finishPendingMessageSuccess(cardAction);
          return;
        }
      } catch {
        // Keep original retryable error.
      }
    }

    if (!(err.retryable && pendingMessage.value)) {
      clearPendingMessage();
    }
    chatStatus.value = "error";
    chatError.value = err;
  } finally {
    stopChatTimer();
    busy.value = false;
    chatBusy.value = false;
    chatBusyVersionId.value = "";
    workflowHooks.scheduleBinaryRefresh?.();
    scheduleComponentRecovery();
  }
}

export function cancelComponentRecoveryTimer(): void {
  if (componentRecoveryState.timer !== null) {
    clearTimeout(componentRecoveryState.timer);
    componentRecoveryState.timer = null;
  }
  componentRecoveryState.scheduled = "";
}

function isRecoveryEligible(): boolean {
  const docHidden = typeof document !== "undefined" && document.hidden;
  return Boolean(
    !docHidden && !unityPlayActive.value &&
    session.value?.status === "active" && !isDeadlineExpired.value &&
    selectedVersionId.value === session.value.currentVersionId,
  );
}

function mergeRecoverySession(
  latest: Session,
  captured: { sessionId: string; selectedVersionId: string; versionId: string },
): boolean {
  if (
    isInteractionBusy() || !isRecoveryEligible() ||
    sessionId.value !== captured.sessionId ||
    selectedVersionId.value !== captured.selectedVersionId ||
    session.value?.currentVersionId !== captured.versionId ||
    latest.sessionId !== captured.sessionId || latest.currentVersionId !== captured.versionId
  ) {
    return false;
  }
  if (
    maxTurnSequence(latest) < maxTurnSequence(session.value) ||
    (latest.interactionState?.revision ?? 0) < (session.value.interactionState?.revision ?? 0) ||
    (latest.updatedAt && session.value.updatedAt && latest.updatedAt < session.value.updatedAt)
  ) {
    return false;
  }
  applySession(latest);
  return true;
}

async function recoverComponent(task: ComponentRecovery): Promise<void> {
  const s = session.value;
  if (
    componentRecoveryState.busy || isInteractionBusy() ||
    !isRecoveryEligible() || !s || task.baseVersionId !== s.currentVersionId
  ) {
    return;
  }

  componentRecoveryState.busy = true;
  const captured = {
    sessionId: sessionId.value,
    selectedVersionId: selectedVersionId.value,
    versionId: s.currentVersionId,
  };
  const canReplayKey = () => Boolean(
    task.nextAttemptKey && sessionId.value === captured.sessionId &&
    session.value?.currentVersionId === captured.versionId &&
    (!isRecoveryEligible() || isInteractionBusy()),
  );

  try {
    let latest: Session;
    if (task.status === "claimed" || task.status === "waiting_message") {
      latest = await api.getSession(captured.sessionId, { timeoutMs: COMPONENT_RECOVERY_TIMEOUT_MS });
    } else {
      if (componentRecoveryState.dispatched.has(task.nextAttemptKey)) return;
      componentRecoveryState.dispatched.add(task.nextAttemptKey);
      latest = await api.retryComponentRecovery(
        captured.sessionId,
        task.recoveryId,
        { baseVersionId: task.baseVersionId, idempotencyKey: task.nextAttemptKey },
        { timeoutMs: COMPONENT_RECOVERY_TIMEOUT_MS },
      );
    }

    const merged = mergeRecoverySession(latest, captured);
    if (!merged && canReplayKey()) {
      componentRecoveryState.dispatched.delete(task.nextAttemptKey);
    }
  } catch {
    try {
      const latest = await api.getSession(captured.sessionId, { timeoutMs: COMPONENT_RECOVERY_TIMEOUT_MS });
      const latestTask = (latest.interactionState?.recoveries || []).find(
        (item) => item.recoveryId === task.recoveryId,
      );
      const unclaimed = Boolean(
        task.nextAttemptKey && latestTask && latestTask.nextAttemptKey === task.nextAttemptKey &&
        ["pending", "retry_pending", "waiting_message"].includes(latestTask.status),
      );
      const merged = mergeRecoverySession(latest, captured);
      if (unclaimed || (!merged && canReplayKey())) {
        componentRecoveryState.dispatched.delete(task.nextAttemptKey);
      }
    } catch {
      componentRecoveryState.paused.add(task.recoveryId);
    }
  } finally {
    componentRecoveryState.busy = false;
    scheduleComponentRecovery();
  }
}

export function scheduleComponentRecovery(): void {
  if (!isRecoveryEligible() || isInteractionBusy()) {
    cancelComponentRecoveryTimer();
    return;
  }
  if (componentRecoveryState.busy) return;

  const s = session.value;
  const task = (s?.interactionState?.recoveries || []).find(
    (item) =>
      ["pending", "retry_pending", "claimed", "waiting_message"].includes(item.status) &&
      !componentRecoveryState.paused.has(item.recoveryId) &&
      item.baseVersionId === s?.currentVersionId &&
      (["claimed", "waiting_message"].includes(item.status) ||
        !componentRecoveryState.dispatched.has(item.nextAttemptKey)),
  );
  if (!task) {
    cancelComponentRecoveryTimer();
    return;
  }

  const scheduledKey = `${sessionId.value}:${task.recoveryId}:${task.status}:${task.nextAttemptKey}`;
  if (componentRecoveryState.scheduled === scheduledKey) return;

  cancelComponentRecoveryTimer();
  componentRecoveryState.scheduled = scheduledKey;
  const delay = task.status === "claimed"
    ? Math.max(1_000, Date.parse(task.leaseUntil || "") - Date.now() + 250)
    : task.status === "waiting_message"
      ? 1_000
      : task.attemptsUsed > 0 ? 3_000 : 1_000;

  componentRecoveryState.timer = globalThis.setTimeout(
    () => {
      componentRecoveryState.timer = null;
      componentRecoveryState.scheduled = "";
      void recoverComponent(task);
    },
    Math.min(15_500, Number.isFinite(delay) ? delay : 15_000),
  );
}

workflowHooks.scheduleComponentRecovery = scheduleComponentRecovery;
workflowHooks.submitPendingMessage = submitPendingMessage;
