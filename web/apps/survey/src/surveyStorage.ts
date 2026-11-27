import type { EntrySource } from "./surveyContext";

export interface SubmissionReceipt {
  readonly responseId: string;
  readonly submittedAt: string;
  readonly recordId: number | null;
  readonly deduplicated?: boolean;
}

export function buildSubmittedStorageKey(
  surveyId: string,
  studySessionId: string,
  matchId: string,
): string {
  return `sokoban_survey_submitted::${surveyId}::${studySessionId}::${matchId}`;
}

export function shouldLockFormalSubmission(
  entrySource: EntrySource,
  studySessionId: string,
  matchId: string,
): boolean {
  return (
    entrySource === "unity_matchmaking" &&
    (studySessionId.trim().length > 0 || matchId.trim().length > 0)
  );
}

function resolveStorage(explicit?: Storage | null): Storage | null {
  if (explicit !== undefined) {
    return explicit;
  }
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readSubmissionReceipt(
  surveyId: string,
  entrySource: EntrySource,
  studySessionId: string,
  matchId: string,
  storageOverride?: Storage | null,
): SubmissionReceipt | null {
  if (!shouldLockFormalSubmission(entrySource, studySessionId, matchId)) {
    return null;
  }
  const storage = resolveStorage(storageOverride);
  if (!storage) {
    return null;
  }
  const key = buildSubmittedStorageKey(surveyId, studySessionId, matchId);
  try {
    const raw = storage.getItem(key);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<SubmissionReceipt>;
    if (
      !parsed ||
      typeof parsed !== "object" ||
      typeof parsed.responseId !== "string" ||
      typeof parsed.submittedAt !== "string"
    ) {
      return null;
    }
    return {
      responseId: parsed.responseId,
      submittedAt: parsed.submittedAt,
      recordId: typeof parsed.recordId === "number" ? parsed.recordId : null,
      deduplicated:
        typeof parsed.deduplicated === "boolean"
          ? parsed.deduplicated
          : undefined,
    };
  } catch {
    return null;
  }
}

export function writeSubmissionReceipt(
  surveyId: string,
  entrySource: EntrySource,
  studySessionId: string,
  matchId: string,
  receipt: SubmissionReceipt,
  storageOverride?: Storage | null,
): void {
  if (!shouldLockFormalSubmission(entrySource, studySessionId, matchId)) {
    return;
  }
  const storage = resolveStorage(storageOverride);
  if (!storage) {
    return;
  }
  const key = buildSubmittedStorageKey(surveyId, studySessionId, matchId);
  try {
    storage.setItem(
      key,
      JSON.stringify({
        responseId: receipt.responseId,
        submittedAt: receipt.submittedAt,
        recordId: receipt.recordId,
      }),
    );
  } catch {
    // Ignore QuotaExceededError / SecurityError in restricted browser environments.
  }
}
