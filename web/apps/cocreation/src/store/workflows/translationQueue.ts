import type { Language, Turn } from "../../types";
import {
  translationFailures,
  translationInProgress,
  translationRemainingCount,
  updateSetSignal,
} from "../chatStore";
import { language, selectedStageTurns, session, sessionId } from "../sessionStore";
import { api, asStructuredError } from "./shared";

let translationRetryTimer: ReturnType<typeof setTimeout> | null = null;
let translationPendingRerun = false;
const translatingBatchKeys = new Set<string>();
const translationRetryAttempts = new Map<string, number>();

export function clearTranslationRetryTimer(): void {
  if (translationRetryTimer !== null) {
    clearTimeout(translationRetryTimer);
    translationRetryTimer = null;
  }
}

function recordBatchTranslationOutcomes(
  capturedSessionId: string,
  turnIds: readonly string[],
  targetLanguage: Language,
  missingIds: ReadonlySet<string>,
): void {
  let shouldScheduleRetry = false;
  updateSetSignal(translationFailures, (set) => {
    for (const id of turnIds) {
      const key = `${id}:${targetLanguage}`;
      if (missingIds.has(id)) {
        set.add(key);
        const nextAttempt = (translationRetryAttempts.get(key) ?? 0) + 1;
        translationRetryAttempts.set(key, nextAttempt);
        if (nextAttempt < 3) shouldScheduleRetry = true;
      } else {
        set.delete(key);
        translationRetryAttempts.delete(key);
      }
    }
  });

  if (shouldScheduleRetry && translationRetryTimer === null) {
    translationRetryTimer = globalThis.setTimeout(() => {
      translationRetryTimer = null;
      if (sessionId.value === capturedSessionId) {
        void ensureVisibleTranslations();
      }
    }, 5_000);
  }
}

async function translateVisibleBatch(
  capturedSessionId: string,
  batch: Turn[],
  targetLanguage: Language,
): Promise<void> {
  const turnIds = batch.map((turn) => turn.turnId);
  const requestKey = `${capturedSessionId}:${targetLanguage}:${turnIds.join(",")}`;
  if (translatingBatchKeys.has(requestKey)) return;
  translatingBatchKeys.add(requestKey);

  try {
    const translatedSession = await api.translateTurns(capturedSessionId, targetLanguage, turnIds);
    if (sessionId.value !== capturedSessionId || !session.value) return;

    const translatedById = new Map(translatedSession.turns.map((turn) => [turn.turnId, turn]));
    session.value = {
      ...session.value,
      turns: session.value.turns.map((turn) => {
        const translated = translatedById.get(turn.turnId);
        return translated ? { ...turn, translations: translated.translations } : turn;
      }),
    };

    const stillMissing = new Set(
      turnIds.filter((id) => !translatedById.get(id)?.translations?.[targetLanguage]),
    );
    recordBatchTranslationOutcomes(capturedSessionId, turnIds, targetLanguage, stillMissing);
  } catch (error) {
    if (sessionId.value !== capturedSessionId || !session.value) return;
    const err = asStructuredError(error);
    let remaining: string[] = Array.isArray(err.details?.pendingTurnIds)
      ? (err.details.pendingTurnIds as string[])
      : turnIds;

    try {
      const latest = await api.getSession(capturedSessionId);
      if (sessionId.value !== capturedSessionId || !session.value) return;
      const byId = new Map(latest.turns.map((turn) => [turn.turnId, turn]));
      session.value = {
        ...session.value,
        turns: session.value.turns.map((turn) => {
          const updated = byId.get(turn.turnId);
          return updated ? { ...turn, translations: updated.translations } : turn;
        }),
      };
      remaining = remaining.filter((id) => !byId.get(id)?.translations?.[targetLanguage]);
    } catch {
      // Retain source text and original retry target.
    }

    recordBatchTranslationOutcomes(
      capturedSessionId,
      turnIds,
      targetLanguage,
      new Set(remaining),
    );
  } finally {
    translatingBatchKeys.delete(requestKey);
    translationRemainingCount.value = Math.max(0, translationRemainingCount.value - batch.length);
  }
}

export async function ensureVisibleTranslations(): Promise<void> {
  const s = session.value;
  const capturedSessionId = sessionId.value;
  if (!s || !capturedSessionId) return;

  if (translationInProgress.value) {
    translationPendingRerun = true;
    return;
  }

  const targetLanguage = language.value;
  const missing = selectedStageTurns.value.filter(
    (turn) =>
      turn.role === "assistant" &&
      turn.language &&
      turn.language !== targetLanguage &&
      !turn.translations?.[targetLanguage],
  );

  if (missing.length === 0) {
    translationRemainingCount.value = 0;
    return;
  }

  const batches: Turn[][] = [];
  for (let i = 0; i < missing.length; i += 8) {
    batches.push(missing.slice(i, i + 8));
  }

  translationInProgress.value = true;
  translationRemainingCount.value = missing.length;

  try {
    let nextIndex = 0;
    const worker = async () => {
      while (nextIndex < batches.length) {
        if (sessionId.value !== capturedSessionId) return;
        const batch = batches[nextIndex++];
        await translateVisibleBatch(capturedSessionId, batch, targetLanguage);
      }
    };
    const workerCount = Math.min(2, batches.length);
    await Promise.all(Array.from({ length: workerCount }, worker));
  } finally {
    translationInProgress.value = false;
    translationRemainingCount.value = 0;
    const rerun = translationPendingRerun;
    translationPendingRerun = false;
    if (rerun && sessionId.value === capturedSessionId) {
      void ensureVisibleTranslations();
    }
  }
}
