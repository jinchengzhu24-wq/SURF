import type { BinaryRetryAction, Language, PendingBinaryAction, Session } from "../../types";
import {
  canChooseBinary,
  chatBusy,
  chatBusyVersionId,
  chatError,
  chatStatus,
  clearComposerText,
  clearPendingMessage,
  clearReplyNotice,
  getStoredBinaryAction,
  isCardActionPending,
  pendingBinary,
  pendingMessage,
  persistPendingBinary,
  recoverMessageFailure,
  setProposalMode,
  startChatTimer,
  stopChatTimer,
} from "../chatStore";
import { isDirty, resetDraftFromVersion } from "../editorStore";
import {
  applySession,
  binaryFlowActive,
  busy,
  canEditSelected,
  findVersion,
  isDeadlineExpired,
  language,
  selectedVersionId,
  session,
  sessionId,
  unityPlayActive,
} from "../sessionStore";
import {
  api,
  asStructuredError,
  isInteractionBusy,
  uniqueId,
  workflowHooks,
} from "./shared";
import { ensureVisibleTranslations } from "./translationQueue";

let binaryRefreshTimer: ReturnType<typeof setTimeout> | null = null;

const BINARY_BUSY_ERROR_CODES = new Set([
  "BINARY_BUSY",
  "BINARY_CHOICE_IN_PROGRESS",
  "CLARIFICATION_IN_PROGRESS",
]);

const BINARY_STALE_ERROR_CODES = new Set([
  "STALE_TOPIC",
  "TOPIC_NOT_FOUND",
]);

function isBinaryActionCommitted(
  latest: Session | null,
  action: { idempotencyKey?: string; baseVersionId?: string } | null,
): boolean {
  if (!latest) return false;
  const flow = latest.proposalFlowState;
  const status = flow?.status;
  if (status === "cancelled" || status === "proposal_ready") return true;
  if (!(flow?.interactionMode === "binary_choice" && flow.active)) return true;
  if (
    action?.idempotencyKey &&
    latest.turns.some(
      (turn) =>
        turn.role === "assistant" &&
        (!action.baseVersionId || turn.versionId === action.baseVersionId) &&
        turn.requestId === action.idempotencyKey,
    )
  ) {
    return true;
  }
  return false;
}

function clearBinaryPendingState(): void {
  persistPendingBinary(null);
  clearPendingMessage();
  setProposalMode(false);
  chatStatus.value = "idle";
  chatError.value = null;
}

export function cancelBinaryRefreshTimer(): void {
  if (binaryRefreshTimer !== null) {
    clearTimeout(binaryRefreshTimer);
    binaryRefreshTimer = null;
  }
}

export async function submitBinaryAction(action: PendingBinaryAction): Promise<void> {
  const sid = sessionId.value;
  if (!sid || busy.value || unityPlayActive.value || !canEditSelected.value) return;
  if (isDirty.value && (action.kind === "choice" || Boolean(action.choiceId))) return;

  cancelBinaryRefreshTimer();
  persistPendingBinary(action);
  busy.value = true;
  chatBusy.value = true;
  chatBusyVersionId.value = action.baseVersionId;
  chatStatus.value = "waiting";
  chatError.value = null;
  clearReplyNotice();
  startChatTimer();

  try {
    const updated =
      action.kind === "cancel"
        ? await api.cancelBinaryProposal(sid, {
            baseVersionId: action.baseVersionId,
            topicId: action.topicId,
            idempotencyKey: action.idempotencyKey,
          })
        : await api.submitBinaryChoice(sid, {
            baseVersionId: action.baseVersionId,
            topicId: action.topicId,
            questionId: action.questionId,
            choiceId: action.choiceId,
            displayLanguage: action.displayLanguage || language.value || "zh-CN",
            idempotencyKey: action.idempotencyKey,
          });
    applySession(updated);
    clearBinaryPendingState();
    void ensureVisibleTranslations();
  } catch (error) {
    let err = asStructuredError(error);
    chatError.value = err;
    chatStatus.value = "error";
    const isBusy = Boolean(err.code && BINARY_BUSY_ERROR_CODES.has(err.code));
    const isStale = Boolean(err.code && BINARY_STALE_ERROR_CODES.has(err.code));
    try {
      const latest = await api.getSession(sid);
      applySession(latest);
      err = recoverMessageFailure(err, latest, action);
      chatError.value = err;
      if (isBinaryActionCommitted(latest, action)) {
        clearBinaryPendingState();
      } else if (isStale || (!err.retryable && !isBusy)) {
        persistPendingBinary(null);
      }
    } catch {
      if (isStale || (!err.retryable && !isBusy)) {
        persistPendingBinary(null);
      }
    }
  } finally {
    stopChatTimer();
    busy.value = false;
    chatBusy.value = false;
    chatBusyVersionId.value = "";
    scheduleBinaryRefresh();
    workflowHooks.scheduleComponentRecovery?.();
  }
}

export function submitBinaryChoice(params: {
  baseVersionId: string;
  topicId: string;
  questionId: string;
  choiceId: string;
  displayLanguage: Language;
}): void {
  if (!canChooseBinary(params.questionId)) return;
  void submitBinaryAction({
    kind: "choice",
    ...params,
    idempotencyKey: uniqueId("binary-choice"),
  });
}

export function cancelBinaryProposal(): void {
  const s = session.value;
  if (
    !s ||
    !binaryFlowActive.value ||
    !canEditSelected.value ||
    busy.value ||
    unityPlayActive.value ||
    s.proposalFlowState?.status === "planning"
  ) {
    return;
  }
  void submitBinaryAction({
    kind: "cancel",
    baseVersionId: s.currentVersionId,
    topicId: s.proposalFlowState?.topicId || "",
    idempotencyKey: uniqueId("binary-cancel"),
  });
}

export async function retryBinaryProposal(): Promise<void> {
  const s = session.value;
  if (!s || busy.value || unityPlayActive.value || !canEditSelected.value) return;

  const action: BinaryRetryAction | PendingBinaryAction | null =
    s.proposalFlowState?.retryAction || pendingBinary.value || getStoredBinaryAction();
  if (!action || action.baseVersionId !== s.currentVersionId) return;
  if (isDirty.value && (action.kind === "choice" || Boolean(action.choiceId))) return;

  if (action.kind === "start") {
    pendingMessage.value = {
      content: action.content || "",
      baseVersionId: action.baseVersionId,
      idempotencyKey: action.idempotencyKey,
      requestProposal: action.requestProposal,
    };
    await workflowHooks.submitPendingMessage?.();
    return;
  }
  await submitBinaryAction({
    kind: action.kind,
    baseVersionId: action.baseVersionId,
    topicId: action.topicId || s.proposalFlowState?.topicId || "",
    questionId: action.questionId,
    choiceId: action.choiceId,
    displayLanguage: action.displayLanguage || language.value || "zh-CN",
    idempotencyKey: action.idempotencyKey,
  });
}

async function refreshBinarySessionLightweight(): Promise<void> {
  const capturedSessionId = sessionId.value;
  if (!capturedSessionId) return;
  const latest = await api.getSession(capturedSessionId);
  if (sessionId.value !== capturedSessionId) return;
  if (isInteractionBusy()) {
    scheduleBinaryRefresh();
    return;
  }

  const previousVersionId = session.value?.currentVersionId;
  applySession(latest);

  if (pendingBinary.value && isBinaryActionCommitted(latest, pendingBinary.value)) {
    clearBinaryPendingState();
  } else if (
    !binaryFlowActive.value ||
    latest.proposalFlowState?.status === "cancelled" ||
    latest.proposalFlowState?.status === "proposal_ready"
  ) {
    persistPendingBinary(null);
    if (chatStatus.value === "error") {
      chatStatus.value = "idle";
      chatError.value = null;
    }
  } else if (pendingBinary.value && chatError.value) {
    chatError.value = recoverMessageFailure(chatError.value, latest, pendingBinary.value);
  }

  const pm = pendingMessage.value;
  if (
    pm &&
    (latest.proposalFlowState?.status === "cancelled" ||
      latest.proposalFlowState?.status === "proposal_ready" ||
      latest.turns.some(
        (turn) =>
          turn.role === "assistant" &&
          turn.versionId === pm.baseVersionId &&
          turn.requestId === pm.idempotencyKey,
      ))
  ) {
    if (!isCardActionPending(pm)) {
      setProposalMode(false);
      clearComposerText();
    }
    clearPendingMessage();
    chatStatus.value = "idle";
    chatError.value = null;
  }

  if (
    previousVersionId &&
    latest.currentVersionId &&
    previousVersionId !== latest.currentVersionId &&
    !findVersion(selectedVersionId.value)
  ) {
    selectedVersionId.value = latest.currentVersionId;
  }
  if (
    !isDirty.value &&
    selectedVersionId.value === latest.currentVersionId &&
    previousVersionId !== latest.currentVersionId
  ) {
    resetDraftFromVersion();
  }
  void ensureVisibleTranslations();
  scheduleBinaryRefresh();
}

export function scheduleBinaryRefresh(): void {
  const docHidden = typeof document !== "undefined" && document.hidden;
  const eligible = Boolean(
    binaryFlowActive.value &&
      session.value?.proposalFlowState?.status === "planning" &&
      session.value.status === "active" &&
      !isDeadlineExpired.value &&
      !docHidden &&
      !busy.value,
  );
  if (!eligible) {
    cancelBinaryRefreshTimer();
    return;
  }
  if (binaryRefreshTimer !== null) return;
  binaryRefreshTimer = globalThis.setTimeout(() => {
    binaryRefreshTimer = null;
    const stillHidden = typeof document !== "undefined" && document.hidden;
    if (
      !binaryFlowActive.value ||
      session.value?.proposalFlowState?.status !== "planning" ||
      session.value.status !== "active" ||
      isDeadlineExpired.value ||
      stillHidden ||
      busy.value
    ) {
      return;
    }
    void refreshBinarySessionLightweight().catch(() => scheduleBinaryRefresh());
  }, 4_000);
}

workflowHooks.scheduleBinaryRefresh = scheduleBinaryRefresh;
