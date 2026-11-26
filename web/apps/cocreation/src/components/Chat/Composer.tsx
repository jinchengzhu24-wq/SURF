import type { JSX } from "preact";
import { useEffect } from "preact/hooks";
import { localizeError, t } from "../../i18n";
import {
  MAX_MESSAGE_LENGTH,
  assessingVersionIds,
  assessmentRetryingVersionIds,
  chatBusy,
  chatBusyVersionId,
  chatElapsedSeconds,
  chatError,
  chatStatus,
  clearReplyNotice,
  composerText,
  getStoredBinaryAction,
  pendingBinary,
  pendingMessage,
  proposalMode,
  replyFeedback,
  setComposerText,
  setProposalMode,
  translationFailures,
  translationInProgress,
  translationRemainingCount,
} from "../../store/chatStore";
import { isDirty } from "../../store/editorStore";
import {
  activeChallengeState,
  binaryFlowActive,
  busy,
  canEditSelected,
  isDeadlineExpired,
  isHistoricalStage,
  language,
  proposalFlowActive,
  selectedStageHasActiveDisagreement,
  selectedStageHasPendingIntentConflict,
  selectedStageTurns,
  selectedVersionId,
  session,
  unityPlayActive,
} from "../../store/sessionStore";
import {
  cancelBinaryProposal,
  retryBinaryProposal,
  retryPendingMessage,
  sendMessage,
  toggleProposalMode,
} from "../../store/workflows";
import type { StructuredErrorLike } from "../../types";
import { TechnicalDetails } from "../Common/TechnicalDetails";
import { tooltipHandlers } from "../Common/Tooltip";

interface EffectiveFailure {
  error: StructuredErrorLike | null;
  message: string;
  retryAction: (() => void | Promise<void>) | null;
  isBinaryRetry?: boolean;
}

export function Composer(): JSX.Element {
  const lang = language.value;
  const s = session.value;
  const vid = selectedVersionId.value;
  const text = composerText.value;
  const editable = canEditSelected.value;
  const interactionBusy = busy.value || unityPlayActive.value;
  const expired = isDeadlineExpired.value;
  const historical = isHistoricalStage.value;
  const intentConflictPending = selectedStageHasPendingIntentConflict.value;
  const disagreementActive = selectedStageHasActiveDisagreement.value;
  const challengeStatus = activeChallengeState.value?.status;
  const challengeChoicePending =
    challengeStatus === "choice_pending" || challengeStatus === "awaiting_choice";
  const proposalLocked = proposalFlowActive.value;
  const binaryActive = binaryFlowActive.value;

  const flowStatus = s?.proposalFlowState?.status || "inactive";
  const proposalPlanning = flowStatus === "planning";
  const proposalRetryPending = flowStatus === "retry_pending" && Boolean(pendingMessage.value);
  const binaryRetryPending = binaryActive && flowStatus === "retry_pending";

  useEffect(() => {
    if ((!editable || disagreementActive || intentConflictPending) && proposalMode.value) {
      setProposalMode(false);
    }
  }, [editable, disagreementActive, intentConflictPending]);

  const inputDisabled =
    interactionBusy ||
    !editable ||
    intentConflictPending ||
    challengeChoicePending ||
    proposalPlanning ||
    proposalRetryPending ||
    binaryActive;

  const placeholder = intentConflictPending
    ? t(lang, "intentConflictPlaceholder")
    : challengeChoicePending
      ? t(lang, "challengeChoicePlaceholder")
      : proposalPlanning
        ? t(lang, "proposalPlanningPlaceholder")
        : proposalRetryPending || binaryRetryPending
          ? t(lang, "proposalRetryPlaceholder")
          : binaryActive && flowStatus === "revision_needed"
            ? t(lang, "binaryRevisionNeededPlaceholder")
            : binaryActive
              ? t(lang, "binaryChoicePlaceholder")
              : flowStatus === "revision_needed"
                ? t(lang, "proposalSupplementPlaceholder")
                : t(lang, "messagePlaceholder");

  const proposalActive = proposalMode.value || proposalLocked;
  const proposalToggleDisabled =
    interactionBusy || !editable || disagreementActive || proposalLocked || intentConflictPending;
  const sendDisabled = inputDisabled || !text.trim();
  const cancelDisabled = interactionBusy || !editable || proposalPlanning;

  const proposalCaption = t(
    lang,
    proposalLocked
      ? "proposalTopicActive"
      : proposalMode.value
        ? "proposalModeOn"
        : "proposalModeOff",
  );

  // Compute unified reply feedback bar
  const waiting =
    chatStatus.value === "waiting" &&
    chatBusy.value &&
    chatBusyVersionId.value === vid &&
    editable;

  let failure: EffectiveFailure | null = null;
  if (!waiting && editable) {
    if (
      binaryActive &&
      (s?.proposalFlowState?.retryable || (pendingBinary.value && chatStatus.value === "error"))
    ) {
      const hasChoices =
        (s?.proposalFlowState?.clarificationQuestionCount || 0) > 0 ||
        Boolean(s?.proposalFlowState?.pendingQuestion?.selectedChoiceId) ||
        pendingBinary.value?.kind === "choice";
      failure = {
        error: chatError.value,
        message: t(lang, hasChoices ? "binarySavedChoicesRetry" : "binarySavedRequestRetry"),
        retryAction: retryBinaryProposal,
        isBinaryRetry: true,
      };
    } else if (
      chatStatus.value === "error" &&
      (!pendingMessage.value || pendingMessage.value.baseVersionId === vid)
    ) {
      failure = {
        error: chatError.value,
        message: "",
        retryAction: chatError.value?.retryable && pendingMessage.value ? retryPendingMessage : null,
      };
    } else if (replyFeedback.value?.versionId === vid) {
      failure = {
        error: replyFeedback.value.error,
        message: replyFeedback.value.message,
        retryAction: replyFeedback.value.retryAction,
      };
    }
  }

  const opening =
    assessingVersionIds.value.has(vid) || assessmentRetryingVersionIds.value.has(vid);

  const turns = selectedStageTurns.value;
  const missingTranslation = turns.some(
    (turn) =>
      turn.role === "assistant" &&
      turn.language &&
      turn.language !== lang &&
      !turn.translations?.[lang],
  );
  const translationFailed = turns.some(
    (turn) =>
      translationFailures.value.has(`${turn.turnId}:${lang}`) &&
      turn.language !== lang &&
      !turn.translations?.[lang],
  );

  const commonBlock = expired
    ? t(lang, "deadlineExpiredAnnouncement")
    : s && s.status !== "active"
      ? t(lang, "hint.closed")
      : unityPlayActive.value
        ? t(lang, "hint.playActive")
        : "";

  const chatBlockReason =
    inputDisabled || sendDisabled
      ? commonBlock ||
        (historical
          ? t(lang, "hint.history")
          : intentConflictPending
            ? t(lang, "hint.conflict")
            : challengeChoicePending
              ? t(lang, "challengeChoicePlaceholder")
              : proposalPlanning
                ? t(lang, "proposalPlanningPlaceholder")
                : proposalRetryPending
                  ? t(lang, "hint.retry")
                  : "")
      : "";

  const isProposalWaiting =
    pendingMessage.value?.requestProposal === true ||
    pendingMessage.value?.action === "execute_revision" ||
    pendingMessage.value?.action === "alternative_revision" ||
    proposalLocked;

  const waitingText = `${t(lang, isProposalWaiting ? "chatWaitingProposal" : "chatWaitingPrimary")} · ${t(lang, "chatElapsed", { seconds: chatElapsedSeconds.value })}`;

  const feedbackMessage = failure
    ? failure.message || localizeError(lang, failure.error)
    : waiting
      ? waitingText
      : opening
        ? t(lang, "assessmentPending")
        : busy.value && !chatBusy.value
          ? t(lang, "hint.busy")
          : translationInProgress.value && missingTranslation
            ? t(lang, "translationInProgress", { count: translationRemainingCount.value })
            : translationFailed
              ? t(lang, "translationUnavailable")
              : chatBlockReason;

  const iconState: "error" | "thinking" | "translation" | "info" = failure
    ? failure.error
      ? "error"
      : "info"
    : waiting || opening || (busy.value && !chatBusy.value)
      ? "thinking"
      : translationInProgress.value && missingTranslation
        ? "translation"
        : translationFailed
          ? "error"
          : "info";

  const effectiveBinaryAction =
    s?.proposalFlowState?.retryAction || pendingBinary.value || getStoredBinaryAction();
  const binaryChoiceDirtyBlocked = Boolean(
    failure?.isBinaryRetry &&
      isDirty.value &&
      (effectiveBinaryAction?.kind === "choice" || Boolean(effectiveBinaryAction?.choiceId)),
  );
  const retryDisabled =
    busy.value || unityPlayActive.value || !failure?.retryAction || binaryChoiceDirtyBlocked;

  return (
    <div class="composer-dock">
      <form
        class={`composer${binaryActive ? " is-binary" : ""}`}
        onSubmit={(event) => {
          event.preventDefault();
          void sendMessage();
        }}
      >
        <label class="sr-only" for="messageInput">
          {t(lang, "messageLabel")}
        </label>
        <textarea
          id="messageInput"
          aria-describedby={feedbackMessage ? "chatControlHint" : undefined}
          rows={2}
          maxLength={MAX_MESSAGE_LENGTH}
          disabled={inputDisabled}
          placeholder={placeholder}
          value={text}
          onInput={(event) => {
            setComposerText((event.currentTarget as HTMLTextAreaElement).value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void sendMessage();
            }
          }}
        />
        <div class="composer-actions">
          <div class="proposal-control">
            <button
              class={`secondary-button proposal-mode-toggle${proposalActive ? " is-active" : ""}`}
              type="button"
              aria-pressed={proposalActive ? "true" : "false"}
              aria-describedby="proposalModeCaption proposalModeHelp"
              aria-label={t(lang, "requestProposalHint")}
              disabled={proposalToggleDisabled}
              aria-disabled={proposalToggleDisabled ? "true" : undefined}
              onClick={toggleProposalMode}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "proposalRequest",
                titleKey: "requestProposal",
                status: proposalToggleDisabled
                  ? disagreementActive
                    ? t(lang, "errors.DISAGREEMENT_ACTIVE")
                    : proposalLocked
                      ? t(lang, "proposalTopicActive")
                      : undefined
                  : undefined,
              })}
            >
              <span class="proposal-toggle-label">{t(lang, "requestProposal")}</span>
              <span class="proposal-toggle-track" aria-hidden="true">
                <span class="proposal-toggle-thumb" />
              </span>
            </button>
            <div class="proposal-caption-row">
              <span id="proposalModeCaption" class="proposal-mode-caption">
                {proposalCaption}
              </span>
              <details class="contextual-help proposal-help">
                <summary>{t(lang, "proposalHelp")}</summary>
                <p id="proposalModeHelp">{t(lang, "requestProposalHint")}</p>
              </details>
            </div>
          </div>

          {binaryActive ? (
            <button
              class="secondary-button cancel-proposal-button"
              type="button"
              disabled={cancelDisabled}
              aria-disabled={cancelDisabled ? "true" : undefined}
              onClick={cancelBinaryProposal}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "cancelProposal",
                titleKey: "cancelProposal",
              })}
            >
              {t(lang, "cancelProposal")}
            </button>
          ) : null}

          <button
            class="primary-button"
            type="submit"
            disabled={sendDisabled}
            aria-disabled={sendDisabled ? "true" : undefined}
            {...tooltipHandlers({
              kind: "action",
              helpKey: "send",
              titleKey: "send",
              status: sendDisabled
                ? feedbackMessage || (!text.trim() ? t(lang, "hint.emptyMessage") : undefined)
                : undefined,
            })}
          >
            {t(lang, "send")}
          </button>
        </div>
      </form>

      {feedbackMessage ? (
        <div
          class={`chat-request-status composer-feedback${failure?.error ? " error" : ""}`}
          role="status"
          aria-live="polite"
        >
          <span class="reply-feedback-icon" aria-hidden="true" data-state={iconState}>
            <svg class="feedback-thinking" viewBox="0 0 20 20" focusable="false">
              <path d="M3 3h14v10H9l-4 4v-4H3zM6 7h1m3 0h1m3 0h1" />
            </svg>
            <svg class="feedback-translation" viewBox="0 0 20 20" focusable="false">
              <path d="M2 4h9M6 2v2m-2 1c0 4 3 7 6 8M9 5c0 4-3 7-7 9m9 3 3-9 4 9m-6-3h5" />
            </svg>
            <svg class="feedback-error" viewBox="0 0 20 20" focusable="false">
              <path d="M3 3h14v14H3zM10 6v5m0 2v1" />
            </svg>
            <svg class="feedback-info" viewBox="0 0 20 20" focusable="false">
              <path d="M3 3h14v14H3zM10 6v1m0 2v5" />
            </svg>
          </span>
          <p class="interface-hint control-hint" id="chatControlHint">
            {feedbackMessage}
          </p>
          {failure?.error ? <TechnicalDetails error={failure.error} /> : null}
          {failure?.retryAction ? (
            <button
              class="chat-retry-button"
              type="button"
              disabled={retryDisabled}
              aria-disabled={retryDisabled ? "true" : undefined}
              onClick={() => {
                const action = failure?.retryAction;
                if (action) {
                  clearReplyNotice();
                  void action();
                }
              }}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "chatRetry",
                titleKey: "retry",
                status: binaryChoiceDirtyBlocked ? t(lang, "hint.binaryDraftReset") : undefined,
              })}
            >
              {t(lang, "retry")}
            </button>
          ) : null}
        </div>
      ) : null}

      <div class="composer-meta">
        <span>{t(lang, "sendHint")}</span>
        <span>
          {t(lang, "characterCount", { count: text.length, max: MAX_MESSAGE_LENGTH })}
        </span>
      </div>
    </div>
  );
}
