import { t } from "../../i18n";
import type { IntentHypothesis, Proposal, QuestionRecord, Turn } from "../../types";
import {
  intentFeedbackBusyKeys,
  questionFeedbackBusyKeys,
  updateSetSignal,
} from "../chatStore";
import { clearUnsavedMapDraft } from "../editorStore";
import { applySession, busy, canEditSelected, language, selectedStageHasActiveDisagreement, session } from "../sessionStore";
import { api, asStructuredError, uniqueId, withBusy } from "./shared";
import { ensureAssessment, selectVersion } from "./versionActions";

const PROPOSAL_STALE_ERROR_CODES = new Set([
  "STALE_PROPOSAL",
  "PROPOSAL_STALE",
  "PROPOSAL_NOT_FOUND",
  "VERSION_CONFLICT",
  "PENDING_PROPOSAL",
  "PROPOSAL_PENDING",
  "DISAGREEMENT_ACTIVE",
  "SESSION_DEADLINE_EXPIRED",
]);

const QUESTION_STALE_ERROR_CODES = new Set([
  "QUESTION_FEEDBACK_STALE",
  "STALE_QUESTION",
  "VERSION_CONFLICT",
  "SESSION_DEADLINE_EXPIRED",
]);

const INTENT_STALE_ERROR_CODES = new Set([
  "INTENT_HYPOTHESIS_STALE",
  "STALE_INTENT_CARD",
  "INTENT_CONFLICT_PENDING",
  "VERSION_CONFLICT",
  "SESSION_DEADLINE_EXPIRED",
]);

async function refreshSessionAfterConflict(sid: string): Promise<void> {
  try {
    const latest = await api.getSession(sid);
    applySession(latest);
    selectVersion(latest.currentVersionId, { preserveSameStageDraft: true });
  } catch {
    // Keep original conflict error visible.
  }
}

export async function decideProposal(
  proposal: Proposal,
  decision: "accept" | "reject",
): Promise<void> {
  const s = session.value;
  if (!s || selectedStageHasActiveDisagreement.value) return;
  await withBusy(async () => {
    try {
      const updated = await api.decideProposal(s.sessionId, proposal.proposalId, {
        decision,
        baseVersionId: s.currentVersionId,
        idempotencyKey: uniqueId(decision),
        reason: "",
      });
      applySession(updated);
      if (decision === "accept") {
        clearUnsavedMapDraft();
        selectVersion(updated.currentVersionId, { forceReset: true });
        await ensureAssessment(updated.currentVersionId);
      } else {
        selectVersion(updated.currentVersionId, { preserveSameStageDraft: true });
      }
    } catch (error) {
      const err = asStructuredError(error);
      if (err.code && PROPOSAL_STALE_ERROR_CODES.has(err.code)) {
        await refreshSessionAfterConflict(s.sessionId);
      }
      throw error;
    }
  });
}

export async function submitQuestionFeedback(
  action: "ignore" | "restore",
  item: QuestionRecord,
): Promise<void> {
  const s = session.value;
  if (!s || busy.value || !canEditSelected.value || !item.questionId) return;
  const feedbackKey = `${item.questionId}:${action}`;
  if (questionFeedbackBusyKeys.value.has(feedbackKey)) return;

  updateSetSignal(questionFeedbackBusyKeys, (set) => set.add(feedbackKey));
  try {
    await withBusy(async () => {
      try {
        const result = await api.submitQuestionFeedback(s.sessionId, item.questionId, {
          action,
          baseVersionId: s.currentVersionId,
          idempotencyKey: uniqueId(`question-${action}`),
        });
        applySession(result.session);
        selectVersion(result.session.currentVersionId, { preserveSameStageDraft: true });
      } catch (error) {
        const err = asStructuredError(error);
        if (err.code && QUESTION_STALE_ERROR_CODES.has(err.code)) {
          await refreshSessionAfterConflict(s.sessionId);
        }
        throw error;
      }
    });
  } finally {
    updateSetSignal(questionFeedbackBusyKeys, (set) => set.delete(feedbackKey));
  }
}

export async function submitIntentFeedback(params: {
  action: "confirm" | "reject" | "revise" | "keep";
  turn: Turn;
  intentState: IntentHypothesis;
  candidateText?: string | null;
  selectedHypothesisId?: string | null;
}): Promise<boolean> {
  const s = session.value;
  if (!s || busy.value || !canEditSelected.value || !params.intentState.actionable) return false;

  const busyKey = `${params.turn.turnId}:${params.intentState.hypothesisId}`;
  updateSetSignal(intentFeedbackBusyKeys, (set) => set.add(busyKey));
  let succeeded = false;
  try {
    await withBusy(async () => {
      try {
        const result = await api.submitIntentFeedback(s.sessionId, params.intentState.hypothesisId, {
          action: params.action,
          candidateText: params.action === "revise" ? (params.candidateText ?? null) : null,
          selectedHypothesisId: params.action === "keep" ? (params.selectedHypothesisId ?? null) : null,
          sourceTurnId: params.turn.turnId,
          baseVersionId: s.currentVersionId,
          idempotencyKey: uniqueId(`intent-${params.action}`),
        });
        applySession(result.session);
        selectVersion(result.session.currentVersionId, { preserveSameStageDraft: true });
        succeeded = true;
      } catch (error) {
        const err = asStructuredError(error);
        if (err.code && INTENT_STALE_ERROR_CODES.has(err.code)) {
          await refreshSessionAfterConflict(s.sessionId);
        }
        throw error;
      }
    });
  } finally {
    updateSetSignal(intentFeedbackBusyKeys, (set) => set.delete(busyKey));
  }
  return succeeded;
}

export function proposalStateLabel(status: string | undefined): string {
  const lang = language.value;
  if (status === "already_satisfied") return t(lang, "proposalAlreadySatisfied");
  if (status === "unbound") return t(lang, "proposalUnbound");
  if (status === "stale") return t(lang, "proposalStale");
  if (status === "precondition_failed") return t(lang, "proposalPreconditionFailed");
  if (status === "disagreement_active") return t(lang, "proposalDisagreementActive");
  if (status === "challenged") return t(lang, "proposalChallenged");
  return t(lang, "staleRevisionCard");
}
