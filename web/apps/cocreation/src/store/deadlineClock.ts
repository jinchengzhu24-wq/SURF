import { computed, signal } from "@preact/signals";
import type { Session } from "../types";

export type DeadlinePresentationState = "normal" | "warning" | "critical" | "expired";

export interface DeadlineClockOptions {
  nowMono?: () => number;
  nowWall?: () => number;
  onExpired?: () => void;
}

/**
 * Monotonic server-anchored deadline countdown immune to re-render drift or timer resets.
 */
export function createDeadlineClock(options: DeadlineClockOptions = {}) {
  const nowMono = options.nowMono ?? (() => globalThis.performance?.now?.() ?? Date.now());
  const nowWall = options.nowWall ?? (() => Date.now());

  const visible = signal(false);
  const remainingSeconds = signal<number>(0);
  const expired = signal(false);
  const announcementState = signal<DeadlinePresentationState | "">("");

  let anchorIdentity = "";
  let anchorRemainingSec = 0;
  let anchorMonoMs = 0;
  let lastPresentationState: DeadlinePresentationState | "" = "";
  let intervalId: ReturnType<typeof setInterval> | null = null;
  let onExpiredCallback = options.onExpired;

  const presentationState = computed<DeadlinePresentationState>(() => {
    if (expired.value || remainingSeconds.value <= 0) return "expired";
    if (remainingSeconds.value <= 60) return "critical";
    if (remainingSeconds.value <= 300) return "warning";
    return "normal";
  });

  const formattedRemaining = computed(() => {
    const sec = Math.max(0, remainingSeconds.value);
    const minutes = String(Math.floor(sec / 60)).padStart(2, "0");
    const remainder = String(sec % 60).padStart(2, "0");
    return `${minutes}:${remainder}`;
  });

  function stopTimer(): void {
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
  }

  function tick(): void {
    if (!visible.value) return;
    const elapsedSec = Math.max(0, Math.floor((nowMono() - anchorMonoMs) / 1000));
    const nextRemaining = Math.max(0, anchorRemainingSec - elapsedSec);
    remainingSeconds.value = nextRemaining;

    if (nextRemaining <= 0) {
      expired.value = true;
      stopTimer();
    }

    const nextPresentation = presentationState.value;
    if (nextPresentation !== lastPresentationState) {
      lastPresentationState = nextPresentation;
      if (nextPresentation !== "normal") {
        announcementState.value = nextPresentation;
      }
      if (nextPresentation === "expired") {
        onExpiredCallback?.();
      }
    }
  }

  function syncFromSession(session: Session | null): void {
    if (!session?.deadlineAt || session.status !== "active") {
      reset();
      return;
    }

    const nextIdentity = `${session.sessionId}:${session.deadlineAt}`;
    const serverRemaining =
      typeof session.remainingSeconds === "number" && Number.isFinite(session.remainingSeconds)
        ? Math.max(0, Math.ceil(session.remainingSeconds))
        : Math.max(0, Math.ceil((Date.parse(session.deadlineAt) - nowWall()) / 1000));

    const serverExpired = Boolean(session.deadlineExpired) || serverRemaining <= 0;

    if (nextIdentity !== anchorIdentity) {
      anchorIdentity = nextIdentity;
      lastPresentationState = "";
      announcementState.value = "";
      anchorRemainingSec = serverExpired ? 0 : serverRemaining;
      anchorMonoMs = nowMono();
      expired.value = serverExpired;
    } else if (serverExpired && !expired.value) {
      anchorRemainingSec = 0;
      anchorMonoMs = nowMono();
      expired.value = true;
    } else if (!expired.value) {
      // Only tighten anchor if server reports a meaningfully different value (> 2s drift),
      // so routine session re-renders never stall or jitter the 1s countdown.
      const currentElapsed = Math.max(0, Math.floor((nowMono() - anchorMonoMs) / 1000));
      const localRemaining = Math.max(0, anchorRemainingSec - currentElapsed);
      if (Math.abs(localRemaining - serverRemaining) > 2) {
        anchorRemainingSec = serverRemaining;
        anchorMonoMs = nowMono();
      }
    }

    visible.value = true;
    tick();

    if (!expired.value && intervalId === null) {
      intervalId = globalThis.setInterval(tick, 1000);
    } else if (expired.value) {
      stopTimer();
    }
  }

  function reset(): void {
    stopTimer();
    visible.value = false;
    remainingSeconds.value = 0;
    expired.value = false;
    announcementState.value = "";
    anchorIdentity = "";
    anchorRemainingSec = 0;
    anchorMonoMs = 0;
    lastPresentationState = "";
  }

  function setOnExpired(callback: (() => void) | undefined): void {
    onExpiredCallback = callback;
  }

  return {
    visible,
    remainingSeconds,
    expired,
    presentationState,
    formattedRemaining,
    announcementState,
    syncFromSession,
    tick,
    reset,
    setOnExpired,
    destroy: reset,
  };
}

export const deadlineClock = createDeadlineClock();
