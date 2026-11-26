import { t } from "../../i18n";
import type { IntentHypothesis, Proposal, QuestionRecord, Turn } from "../../types";
import {
  intentFeedbackBusyKeys,
  questionFeedbackBusyKeys,
  updateSetSignal,
} from "../chatStore";
import { clearUnsavedMapDraft } from "../editorStore";
import { applySession, busy, canEditSelected, language, session } from "../sessionStore";
import { api, uniqueId, withBusy } from "./shared";
import { ensureAssessment, selectVersion } from "./versionActions";

export async function decideProposal(
  proposal: Proposal,
  decision: "accept" | "reject",
): Promise<void> {
  const s = session.value;
  if (!s) return;
  await withBusy(async () => {
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
      const result = await api.submitQuestionFeedback(s.sessionId, item.questionId, {
        action,
        baseVersionId: s.currentVersionId,
        idempotencyKey: uniqueId(`question-${action}`),
      });
      applySession(result.session);
      selectVersion(result.session.currentVersionId, { preserveSameStageDraft: true });
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
