import { computed, signal } from "@preact/signals";
import { t, type TranslationKey } from "../i18n";
import type {
  ChallengeState,
  LandingMode,
  Language,
  ProgressContext,
  Proposal,
  Session,
  StageVersion,
  StructuredErrorLike,
  Turn,
} from "../types";
import { deadlineClock } from "./deadlineClock";

export interface GlobalNotice {
  key: number;
  message: string;
  error: StructuredErrorLike | null;
  retryAction: (() => void | Promise<void>) | null;
}

export interface HashParams {
  session: string;
  stage: string;
  bootstrap: string;
  playReturn: string;
}

let noticeSequence = 0;

export const session = signal<Session | null>(null);
export const sessionId = signal<string>("");
export const selectedVersionId = signal<string>("");
export const language = signal<Language>("zh-CN");
export const landingMode = signal<LandingMode>("demo");

export const busy = signal<boolean>(false);
export const demoCreationPending = signal<boolean>(false);
export const demoCreationFailed = signal<boolean>(false);
export const draftRegenerationPending = signal<boolean>(false);
export const unityPlayActive = signal<boolean>(false);
export const embeddedHostReady = signal<boolean>(false);
export const formalStandaloneBlocked = signal<boolean>(false);

export const notice = signal<GlobalNotice | null>(null);
export const finalizeModalOpen = signal<boolean>(false);
export const intentionDraft = signal<string>("");
export const headerStatus = signal<{ labelKey: TranslationKey; state: "pending" | "ready" | "error" }>({
  labelKey: "loading",
  state: "pending",
});

export const isWorkspaceVisible = computed<boolean>(() => {
  const s = session.value;
  return Boolean(s && s.languageLocked && !formalStandaloneBlocked.value);
});

export const isDeadlineExpired = computed<boolean>(
  () => Boolean(session.value?.deadlineExpired) || deadlineClock.expired.value,
);

export const currentVersion = computed<StageVersion | null>(() => {
  const s = session.value;
  if (!s) return null;
  return s.versions.find((item) => item.versionId === s.currentVersionId) ?? null;
});

export const selectedVersion = computed<StageVersion | null>(() => {
  const s = session.value;
  const targetId = selectedVersionId.value;
  if (!s || !targetId) return null;
  return s.versions.find((item) => item.versionId === targetId) ?? null;
});

export const isHistoricalStage = computed<boolean>(() => {
  const s = session.value;
  if (!s) return false;
  return Boolean(selectedVersionId.value) && selectedVersionId.value !== s.currentVersionId;
});

export const canEditSelected = computed<boolean>(() => {
  const s = session.value;
  return Boolean(
    s &&
      s.status === "active" &&
      !unityPlayActive.value &&
      !isDeadlineExpired.value &&
      selectedVersionId.value === s.currentVersionId,
  );
});

export const currentPendingProposal = computed<Proposal | null>(() => {
  const s = session.value;
  if (!s) return null;
  for (let i = s.proposals.length - 1; i >= 0; i -= 1) {
    const proposal = s.proposals[i];
    if (proposal.status === "pending" && proposal.baseVersionId === s.currentVersionId) {
      return proposal;
    }
  }
  return null;
});

export const selectedStageTurns = computed<Turn[]>(() => {
  const s = session.value;
  const versionId = selectedVersionId.value;
  if (!s || !versionId) return [];

  const version = s.versions.find((item) => item.versionId === versionId);
  const openingTurnId = version?.openingTurnId ?? null;
  const supersededAssessmentTurnIds = new Set<string>(
    openingTurnId
      ? (s.assessments || [])
          .filter((item) => item.versionId === versionId)
          .map((item) => item.assistantTurnId)
      : [],
  );

  const directTurns = (s.turns || []).filter(
    (turn) => turn.versionId === versionId && !supersededAssessmentTurnIds.has(turn.turnId),
  );
  const openingTurn = openingTurnId
    ? (s.turns || []).find((turn) => turn.turnId === openingTurnId) ?? null
    : null;

  if (!openingTurn || directTurns.some((turn) => turn.turnId === openingTurn.turnId)) {
    return directTurns;
  }
  return [openingTurn, ...directTurns];
});

export const selectedProgressContext = computed<ProgressContext | null>(() => {
  const s = session.value;
  if (!s || !Array.isArray(s.progressContexts)) return null;
  return (
    s.progressContexts.find((item) => item.versionId === selectedVersionId.value) ?? {
      versionId: selectedVersionId.value,
      questionRecords: [],
      designInclinations: [],
    }
  );
});

export const proposalFlowActive = computed<boolean>(() =>
  Boolean(session.value?.proposalFlowState?.active),
);

export const binaryFlowActive = computed<boolean>(() =>
  Boolean(
    session.value?.proposalFlowState?.active &&
      session.value.proposalFlowState.interactionMode === "binary_choice",
  ),
);

export const selectedStageHasActiveDisagreement = computed<boolean>(() => {
  const s = session.value;
  const canonical = s?.interactionState;
  if (
    s &&
    selectedVersionId.value === s.currentVersionId &&
    canonical?.versionId === selectedVersionId.value
  ) {
    return Boolean(canonical.proposalBlocked);
  }
  let latestStatus: string | null = null;
  for (const turn of selectedStageTurns.value) {
    if (turn.role !== "assistant") continue;
    const disagreement = turn.guidance?.disagreement || turn.guidance?.challengeCard;
    if (disagreement && typeof disagreement === "object") {
      latestStatus = disagreement.status || null;
    }
  }
  return latestStatus === "active" || latestStatus === "choice_pending";
});

export const selectedStageHasPendingIntentConflict = computed<boolean>(() =>
  selectedStageTurns.value.some(
    (turn) =>
      turn.role === "assistant" &&
      turn.guidance?.intentState?.interactionMode === "conflict_choice" &&
      turn.guidance.intentState.actionable === true,
  ),
);

export const activeChallengeState = computed<ChallengeState | null>(() => {
  const turns = selectedStageTurns.value;
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const guidance = turns[i]?.guidance;
    const card = guidance?.challengeCard;
    const challenge: ChallengeState | null =
      guidance?.challengeState ??
      (card?.challengeId
        ? {
            challengeId: card.challengeId,
            status: card.phase === "choice_pending" ? "choice_pending" : card.status || "active",
          }
        : null);
    if (!challenge?.challengeId) continue;
    if (challenge.status === "resolved") return null;
    return challenge;
  }
  return null;
});

export function isRevisionOfferTurn(turn: Turn | null | undefined): boolean {
  const offer = turn?.guidance?.proposalOffer;
  return Boolean(
    turn?.role === "assistant" &&
      offer &&
      typeof offer === "object" &&
      (String(offer.summary || "").trim() ||
        (offer.proposalPresentation && typeof offer.proposalPresentation === "object")),
  );
}

export const latestRevisionOfferTurnId = computed<string | null>(() => {
  const turns = selectedStageTurns.value;
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (isRevisionOfferTurn(turns[i])) {
      return turns[i].turnId;
    }
  }
  return null;
});

export function findVersion(versionId: string): StageVersion | null {
  return session.value?.versions.find((item) => item.versionId === versionId) ?? null;
}

export function proposalForTurn(turnId: string): Proposal | null {
  return session.value?.proposals.find((item) => item.assistantTurnId === turnId) ?? null;
}

export function retainNewerInteractionState(next: Session): Session {
  const current = session.value;
  if (
    !next.interactionState ||
    next.sessionId !== current?.sessionId ||
    next.currentVersionId !== current.currentVersionId ||
    (next.interactionState.revision ?? 0) >= (current.interactionState?.revision ?? 0)
  ) {
    return next;
  }
  const liveTurns = new Map((current.turns || []).map((turn) => [turn.turnId, turn]));
  return {
    ...next,
    interactionState: current.interactionState,
    turns: (next.turns || []).map((turn) => {
      const live = liveTurns.get(turn.turnId);
      return live ? { ...turn, proposalState: live.proposalState } : turn;
    }),
  };
}

export function applySession(next: Session | null): void {
  if (!next) {
    session.value = null;
    deadlineClock.reset();
    return;
  }
  const merged = retainNewerInteractionState(next);
  session.value = merged;
  if (merged.languageLocked && merged.language) {
    language.value = merged.language;
  }
  if (merged.status === "awaiting_intention" && merged.intention?.content && !intentionDraft.value.trim()) {
    intentionDraft.value = merged.intention.content;
  }
  deadlineClock.syncFromSession(merged);
}

export function showGlobalNotice(
  message: string,
  options: { error?: StructuredErrorLike | null; retryAction?: (() => void | Promise<void>) | null } = {},
): void {
  noticeSequence += 1;
  notice.value = {
    key: noticeSequence,
    message,
    error: options.error ?? null,
    retryAction: options.retryAction ?? null,
  };
}

export function hideGlobalNotice(): void {
  notice.value = null;
}

export function setHeaderStatus(labelKey: TranslationKey, state: "pending" | "ready" | "error"): void {
  headerStatus.value = { labelKey, state };
}

export function showPlayReturnNotice(status: string | undefined): void {
  if (status === "sync_failed") {
    showGlobalNotice(t(language.value, "playSyncFailed"));
  } else if (status === "load_failed") {
    showGlobalNotice(t(language.value, "playLoadFailed"));
  }
}

export function readUrlHash(loc: Location | undefined = globalThis.location): HashParams {
  const params = new URLSearchParams((loc?.hash || "").replace(/^#/, ""));
  return {
    session: params.get("session") || "",
    stage: params.get("stage") || "",
    bootstrap: params.get("bootstrap") || "",
    playReturn: params.get("playReturn") || "",
  };
}

export function stripBootstrapFromHash(
  win: Window | undefined = typeof window !== "undefined" ? window : undefined,
): void {
  if (!win?.location || !win.history) return;
  const cleanHash = new URLSearchParams((win.location.hash || "").replace(/^#/, ""));
  if (!cleanHash.has("bootstrap")) return;
  cleanHash.delete("bootstrap");
  win.history.replaceState(
    null,
    "",
    `${win.location.pathname}${win.location.search}#${cleanHash.toString()}`,
  );
}

export function syncUrlHash(
  win: Window | undefined = typeof window !== "undefined" ? window : undefined,
): void {
  const sid = sessionId.value;
  if (!sid || !win?.location || !win.history) return;
  const hash = new URLSearchParams({ session: sid });
  if (selectedVersionId.value) {
    hash.set("stage", selectedVersionId.value);
  }
  win.history.replaceState(
    null,
    "",
    `${win.location.pathname}${win.location.search}#${hash.toString()}`,
  );
}
