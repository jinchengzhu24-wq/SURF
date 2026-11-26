import { computed, signal } from "@preact/signals";
import { t } from "../i18n";
import {
  localStore,
  readPendingBinaryAction,
  readValidatedPendingMessage,
  storageKeys,
} from "../storage/safeStorage";
import type {
  PendingBinaryAction,
  PendingMessage,
  ReplyFeedback,
  Session,
  StructuredErrorLike,
} from "../types";
import { isDirty } from "./editorStore";
import {
  activeChallengeState,
  binaryFlowActive,
  busy,
  canEditSelected,
  isDeadlineExpired,
  language,
  selectedStageHasActiveDisagreement,
  selectedVersionId,
  session,
  sessionId,
  unityPlayActive,
} from "./sessionStore";

export const MAX_MESSAGE_LENGTH = 2000;

export const composerText = signal<string>("");
export const proposalMode = signal<boolean>(false);

export const chatStatus = signal<"idle" | "waiting" | "error">("idle");
export const chatBusy = signal<boolean>(false);
export const chatBusyVersionId = signal<string>("");
export const chatStartedAt = signal<number>(0);
export const chatElapsedSeconds = signal<number>(0);
export const chatError = signal<StructuredErrorLike | null>(null);
export const replyFeedback = signal<ReplyFeedback | null>(null);

export const pendingMessage = signal<PendingMessage | null>(null);
export const pendingBinary = signal<PendingBinaryAction | null>(null);

export const assessingVersionIds = signal<ReadonlySet<string>>(new Set());
export const assessmentRetryingVersionIds = signal<ReadonlySet<string>>(new Set());
export const questionFeedbackBusyKeys = signal<ReadonlySet<string>>(new Set());
export const intentFeedbackBusyKeys = signal<ReadonlySet<string>>(new Set());

export const translationInProgress = signal<boolean>(false);
export const translationRemainingCount = signal<number>(0);
export const translationFailures = signal<ReadonlySet<string>>(new Set());

export const isGenerationPending = computed<boolean>(
  () =>
    chatStatus.value === "waiting" ||
    assessingVersionIds.value.size > 0 ||
    assessmentRetryingVersionIds.value.size > 0,
);

let chatTimerId: ReturnType<typeof setInterval> | null = null;

export function startChatTimer(): void {
  stopChatTimer();
  chatStartedAt.value = Date.now();
  chatElapsedSeconds.value = 0;
  chatTimerId = globalThis.setInterval(() => {
    chatElapsedSeconds.value = Math.max(0, Math.floor((Date.now() - chatStartedAt.value) / 1000));
  }, 250);
}

export function stopChatTimer(): void {
  if (chatTimerId !== null) {
    clearInterval(chatTimerId);
    chatTimerId = null;
  }
}

export function isCardActionPending(pending: PendingMessage | null | undefined): boolean {
  if (!pending) return false;
  return (
    pending.action === "execute_revision" ||
    pending.action === "challenge_revision" ||
    pending.action === "alternative_revision" ||
    (pending.action === "continue_challenge" && Boolean(pending.challengeChoice))
  );
}

export function recoverMessageFailure(
  error: StructuredErrorLike,
  latestSession: Session | null | undefined,
  pending: { idempotencyKey?: string; baseVersionId?: string } | null | undefined,
): StructuredErrorLike {
  if (
    !error.code ||
    !["RETRYABLE_REQUEST_FAILED", "INVALID_RESPONSE", "CLIENT_TIMEOUT"].includes(error.code)
  ) {
    return error;
  }
  const failure = latestSession?.lastMessageFailure;
  if (
    !failure ||
    failure.messageKey !== pending?.idempotencyKey ||
    failure.baseVersionId !== pending?.baseVersionId ||
    failure.committed !== false ||
    !failure.code
  ) {
    return error;
  }
  return {
    code: failure.code,
    message: error.message,
    retryable: Boolean(failure.retryable),
    details: {
      ...(error.details ?? {}),
      ...(failure.details ?? {}),
      requestId: failure.requestId || error.details?.requestId,
      committed: false,
    },
  };
}

export function setComposerText(value: string): void {
  composerText.value = value;
  const sid = sessionId.value;
  const vid = session.value?.currentVersionId;
  if (sid && vid) {
    localStore.set(storageKeys.composer(sid, vid), value);
  }
}

export function clearComposerText(): void {
  composerText.value = "";
  const sid = sessionId.value;
  const vid = session.value?.currentVersionId;
  if (sid && vid) {
    localStore.remove(storageKeys.composer(sid, vid));
  }
}

export function setProposalMode(enabled: boolean): void {
  const next = Boolean(enabled);
  proposalMode.value = next;
  const sid = sessionId.value;
  const vid = selectedVersionId.value;
  if (!sid || !vid) return;
  if (next) {
    localStore.set(storageKeys.proposalMode(sid, vid), "on");
  } else {
    localStore.remove(storageKeys.proposalMode(sid, vid));
  }
}

export function persistPendingMessage(): void {
  const sid = sessionId.value;
  const vid = session.value?.currentVersionId;
  if (sid && vid && pendingMessage.value) {
    localStore.set(storageKeys.pendingMessage(sid, vid), pendingMessage.value);
  }
}

export function clearPendingMessage(): void {
  pendingMessage.value = null;
  const sid = sessionId.value;
  const vid = session.value?.currentVersionId;
  if (sid && vid) {
    localStore.remove(storageKeys.pendingMessage(sid, vid));
  }
}

export function persistPendingBinary(action: PendingBinaryAction | null): void {
  pendingBinary.value = action;
  const sid = sessionId.value;
  if (!sid) return;
  if (action) {
    localStore.set(storageKeys.pendingBinary(sid), action);
  } else {
    localStore.remove(storageKeys.pendingBinary(sid));
  }
}

export function getStoredBinaryAction(): PendingBinaryAction | null {
  const sid = sessionId.value;
  return sid ? readPendingBinaryAction(sid) : null;
}

export function restoreComposerDraft(): void {
  const sid = sessionId.value;
  const vid = session.value?.currentVersionId || "";
  if (canEditSelected.value && sid && vid) {
    composerText.value = localStore.get<string>(storageKeys.composer(sid, vid)) ?? "";
  } else {
    composerText.value = "";
  }

  const allowed = canEditSelected.value && !selectedStageHasActiveDisagreement.value;
  const savedMode =
    allowed && sid && selectedVersionId.value
      ? localStore.get<string>(storageKeys.proposalMode(sid, selectedVersionId.value)) === "on"
      : false;
  proposalMode.value = savedMode;
  if (!allowed && sid && selectedVersionId.value) {
    localStore.remove(storageKeys.proposalMode(sid, selectedVersionId.value));
  }
}

export function recoverPendingMessageFromSession(): void {
  const s = session.value;
  const sid = sessionId.value;
  if (!s || !sid) return;
  if (binaryFlowActive.value) {
    pendingMessage.value = null;
    return;
  }

  const stored = readValidatedPendingMessage(sid, s.currentVersionId);
  const turns = (s.turns || []).filter((turn) => turn.versionId === s.currentVersionId);
  const finalTurn = turns[turns.length - 1];

  if (
    s.proposalFlowState?.interactionMode === "binary_choice" &&
    finalTurn?.turnId &&
    s.proposalFlowState.recordedUserTurnIds?.includes(finalTurn.turnId)
  ) {
    clearPendingMessage();
    return;
  }

  const unmatched: PendingMessage | null =
    finalTurn?.role === "user" &&
    finalTurn.requestId &&
    !turns.some((turn) => turn.role === "assistant" && turn.requestId === finalTurn.requestId)
      ? {
          content: finalTurn.content,
          baseVersionId: finalTurn.versionId,
          idempotencyKey: finalTurn.requestId,
          requestProposal: finalTurn.requestProposal === true,
        }
      : null;

  const pending = stored || unmatched;
  if (!pending) return;

  const completed = turns.some(
    (turn) => turn.role === "assistant" && turn.requestId === pending.idempotencyKey,
  );
  const cardAction = isCardActionPending(pending);

  if (completed) {
    if (!cardAction && composerText.value.trim() === pending.content) {
      clearComposerText();
    }
    clearPendingMessage();
    return;
  }

  const lastFailure = s.lastMessageFailure;
  if (
    lastFailure &&
    lastFailure.messageKey === pending.idempotencyKey &&
    lastFailure.baseVersionId === pending.baseVersionId &&
    lastFailure.retryable === false
  ) {
    clearPendingMessage();
    return;
  }

  const draft = composerText.value.trim();
  if (!cardAction && draft && draft !== pending.content) {
    clearPendingMessage();
    return;
  }

  pendingMessage.value = pending;
  if (!cardAction) {
    setProposalMode(pending.requestProposal === true);
    setComposerText(pending.content);
  }
  persistPendingMessage();

  chatStatus.value = "error";
  chatError.value = {
    code: "PENDING_MESSAGE",
    message: t(language.value, "chatRetryPending"),
    retryable: true,
  };
}

export function canChooseBinary(questionId: string): boolean {
  const s = session.value;
  const flow = s?.proposalFlowState;
  return Boolean(
    binaryFlowActive.value &&
      flow?.status === "clarifying" &&
      !busy.value &&
      !unityPlayActive.value &&
      !isDirty.value &&
      !isDeadlineExpired.value &&
      canEditSelected.value &&
      selectedVersionId.value === s?.currentVersionId &&
      flow.pendingQuestion?.questionId === questionId &&
      !flow.pendingQuestion.selectedChoiceId,
  );
}

export function canUseChallengeChoice(challengeId: string | undefined): boolean {
  const active = activeChallengeState.value;
  return Boolean(
    challengeId &&
      canEditSelected.value &&
      !busy.value &&
      !unityPlayActive.value &&
      active?.status === "choice_pending" &&
      active.challengeId === challengeId,
  );
}

export function showReplyNotice(
  message: string,
  error: StructuredErrorLike | null = null,
  retryAction: (() => void | Promise<void>) | null = null,
  versionId: string = selectedVersionId.value,
): void {
  replyFeedback.value = { message, error, retryAction, versionId };
}

export function clearReplyNotice(): void {
  replyFeedback.value = null;
}

export function updateSetSignal(
  sig: { value: ReadonlySet<string> },
  mutate: (copy: Set<string>) => void,
): void {
  const next = new Set(sig.value);
  mutate(next);
  sig.value = next;
}
