import type { PendingBinaryAction, PendingMessage } from "../types";

export const STORAGE_NAMESPACE = "sokoban.cocreation.v1:";
export const DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
export const MAX_NAMESPACED_ENTRIES = 60;
export const REPLY_DIAGNOSTICS_KEY = "replyDiagnosticsV2";
export const REPLY_PIPELINE_VERSION = "ordinary-harness-2026-10-13-v2";

interface StoredEnvelope<T> {
  v: T;
  ts: number;
  ttl: number;
}

export interface SafeStorageAdapter {
  get<T>(key: string): T | null;
  set<T>(key: string, value: T, ttlMs?: number): void;
  remove(key: string): void;
  prune(now?: number): void;
  clearNamespace(): void;
}

function resolveBrowserStorage(kind: "local" | "session"): Storage | null {
  try {
    const store = kind === "local" ? globalThis.localStorage : globalThis.sessionStorage;
    if (!store) return null;
    return store;
  } catch {
    return null;
  }
}

/**
 * Creates a `try/catch`-wrapped storage adapter with namespaced keys, TTL expiration,
 * LRU eviction, and an in-memory fallback when browser storage is blocked (private mode / iframe).
 */
export function createSafeStorage(
  backing: Storage | null | "local" | "session",
  options: {
    namespace?: string;
    maxEntries?: number;
    defaultTtlMs?: number;
    now?: () => number;
  } = {},
): SafeStorageAdapter {
  const namespace = options.namespace ?? STORAGE_NAMESPACE;
  const maxEntries = options.maxEntries ?? MAX_NAMESPACED_ENTRIES;
  const defaultTtlMs = options.defaultTtlMs ?? DEFAULT_TTL_MS;
  const nowFn = options.now ?? (() => Date.now());
  const memoryFallback = new Map<string, string>();

  const getStore = (): Storage | null => {
    if (backing === "local" || backing === "session") {
      return resolveBrowserStorage(backing);
    }
    return backing;
  };

  const fullKey = (key: string) => `${namespace}${key}`;

  const rawGet = (k: string): string | null => {
    const store = getStore();
    if (store) {
      try {
        const value = store.getItem(k);
        if (value !== null) return value;
      } catch {
        // Fall back to in-memory cache.
      }
    }
    return memoryFallback.get(k) ?? null;
  };

  const rawRemove = (k: string): void => {
    memoryFallback.delete(k);
    const store = getStore();
    if (!store) return;
    try {
      store.removeItem(k);
    } catch {
      // Ignore restricted storage removal errors.
    }
  };

  const listNamespacedEntries = (store: Storage | null): Array<{ key: string; ts: number; expired: boolean }> => {
    const now = nowFn();
    const entries: Array<{ key: string; ts: number; expired: boolean }> = [];
    const seen = new Set<string>();

    const inspectKey = (k: string, raw: string | null) => {
      if (!k.startsWith(namespace) || seen.has(k)) return;
      seen.add(k);
      if (!raw) {
        entries.push({ key: k, ts: 0, expired: true });
        return;
      }
      try {
        const parsed = JSON.parse(raw) as Partial<StoredEnvelope<unknown>>;
        const ts = typeof parsed.ts === "number" ? parsed.ts : 0;
        const ttl = typeof parsed.ttl === "number" ? parsed.ttl : defaultTtlMs;
        entries.push({ key: k, ts, expired: ttl > 0 && now - ts > ttl });
      } catch {
        entries.push({ key: k, ts: 0, expired: true });
      }
    };

    if (store) {
      try {
        for (let i = 0; i < store.length; i += 1) {
          const k = store.key(i);
          if (k && k.startsWith(namespace)) {
            inspectKey(k, store.getItem(k));
          }
        }
      } catch {
        // Ignore enumeration failure.
      }
    }

    for (const [k, raw] of memoryFallback.entries()) {
      inspectKey(k, raw);
    }
    return entries;
  };

  const prune = (nowOverride?: number): void => {
    const store = getStore();
    const now = nowOverride ?? nowFn();
    const entries = listNamespacedEntries(store);
    const active: Array<{ key: string; ts: number }> = [];

    for (const entry of entries) {
      if (entry.expired || (entry.ts > 0 && now - entry.ts > defaultTtlMs)) {
        rawRemove(entry.key);
      } else {
        active.push({ key: entry.key, ts: entry.ts });
      }
    }

    if (active.length > maxEntries) {
      active.sort((a, b) => a.ts - b.ts);
      const excess = active.length - maxEntries;
      for (let i = 0; i < excess; i += 1) {
        rawRemove(active[i].key);
      }
    }
  };

  const rawSet = (k: string, serialized: string): void => {
    memoryFallback.set(k, serialized);
    const store = getStore();
    if (!store) return;
    try {
      store.setItem(k, serialized);
    } catch {
      // Retry once after pruning expired/oldest namespaced entries (handles QuotaExceededError).
      prune();
      try {
        store.setItem(k, serialized);
      } catch {
        // Remain safely in memory fallback without throwing.
      }
    }
  };

  return {
    get<T>(key: string): T | null {
      const k = fullKey(key);
      const raw = rawGet(k);
      if (raw === null) return null;
      try {
        const parsed = JSON.parse(raw) as Partial<StoredEnvelope<T>>;
        if (!parsed || typeof parsed !== "object" || !("v" in parsed)) {
          rawRemove(k);
          return null;
        }
        const ts = typeof parsed.ts === "number" ? parsed.ts : 0;
        const ttl = typeof parsed.ttl === "number" ? parsed.ttl : defaultTtlMs;
        if (ttl > 0 && nowFn() - ts > ttl) {
          rawRemove(k);
          return null;
        }
        return parsed.v as T;
      } catch {
        rawRemove(k);
        return null;
      }
    },

    set<T>(key: string, value: T, ttlMs = defaultTtlMs): void {
      const k = fullKey(key);
      const envelope: StoredEnvelope<T> = {
        v: value,
        ts: nowFn(),
        ttl: ttlMs,
      };
      let serialized: string;
      try {
        serialized = JSON.stringify(envelope);
      } catch {
        return;
      }
      rawSet(k, serialized);
      prune();
    },

    remove(key: string): void {
      rawRemove(fullKey(key));
    },

    prune,

    clearNamespace(): void {
      const store = getStore();
      const entries = listNamespacedEntries(store);
      for (const entry of entries) {
        rawRemove(entry.key);
      }
    },
  };
}

export const localStore = createSafeStorage("local");
export const sessionStore = createSafeStorage("session", { maxEntries: 30 });

// Domain-specific typed storage helpers
export interface UnsavedMapDraft {
  sessionId: string;
  baseVersionId: string;
  rows: string[];
  dirty: true;
  savedAt: string;
}

export const storageKeys = {
  lastSession: () => "lastSessionId",
  selectedStage: (sessionId: string) => `stage:${sessionId}`,
  composer: (sessionId: string, versionId: string) => `composer:${sessionId}:${versionId || "none"}`,
  pendingMessage: (sessionId: string, versionId: string) =>
    `pendingMessage:${sessionId}:${versionId || "none"}`,
  pendingBinary: (sessionId: string) => `binaryAction:${sessionId}`,
  proposalMode: (sessionId: string, versionId: string) =>
    `proposalMode:${sessionId}:${versionId || "none"}`,
  unsavedMap: (sessionId: string) => `unsavedMap:${sessionId}`,
  progressPanel: (sessionId: string, versionId: string) =>
    `progress:${sessionId}:${versionId || "none"}`,
};

export function readValidatedPendingMessage(
  sessionId: string,
  currentVersionId: string,
): PendingMessage | null {
  const key = storageKeys.pendingMessage(sessionId, currentVersionId);
  const pending = localStore.get<PendingMessage>(key);
  if (
    !pending ||
    typeof pending.content !== "string" ||
    typeof pending.baseVersionId !== "string" ||
    typeof pending.idempotencyKey !== "string"
  ) {
    return null;
  }
  if (pending.requestProposal !== undefined && typeof pending.requestProposal !== "boolean") {
    return null;
  }
  if (
    pending.action !== undefined &&
    !["none", "execute_revision", "challenge_revision", "alternative_revision", "continue_challenge"].includes(
      pending.action,
    )
  ) {
    return null;
  }
  if (
    pending.action &&
    ["execute_revision", "challenge_revision", "alternative_revision"].includes(pending.action) &&
    typeof pending.sourceTurnId !== "string"
  ) {
    return null;
  }
  if (pending.action === "continue_challenge" && typeof pending.challengeId !== "string") {
    return null;
  }
  if (
    pending.challengeChoice !== undefined &&
    !["ai", "user", "retain_current"].includes(pending.challengeChoice)
  ) {
    return null;
  }
  if (pending.baseVersionId !== currentVersionId) {
    return null;
  }
  return pending;
}

export function readPendingBinaryAction(sessionId: string): PendingBinaryAction | null {
  const action = localStore.get<PendingBinaryAction>(storageKeys.pendingBinary(sessionId));
  if (
    !action ||
    (action.kind !== "choice" && action.kind !== "cancel") ||
    typeof action.baseVersionId !== "string" ||
    typeof action.idempotencyKey !== "string"
  ) {
    return null;
  }
  return action;
}

const DIAGNOSTIC_ALLOWED_FIELDS = [
  "stage",
  "status",
  "code",
  "requestId",
  "messageKey",
  "versionId",
  "assistantTurnId",
  "pipelineVersion",
  "frontendVersion",
  "receivedCard",
  "displayCard",
  "renderedCard",
  "turnPresent",
  "httpStatus",
  "at",
] as const;

export function recordReplyDiagnostic(
  stage: string,
  status: string,
  fields: Record<string, unknown> = {},
): void {
  const raw = {
    stage,
    status,
    frontendVersion: REPLY_PIPELINE_VERSION,
    at: Date.now(),
    ...fields,
  };
  const sanitized: Record<string, string | number | boolean> = {};
  for (const key of DIAGNOSTIC_ALLOWED_FIELDS) {
    const val = (raw as Record<string, unknown>)[key];
    if (typeof val === "boolean" || (typeof val === "number" && Number.isFinite(val))) {
      sanitized[key] = val;
    } else if (typeof val === "string" && val.length <= 128 && /^[a-zA-Z0-9_:. /-]*$/.test(val)) {
      sanitized[key] = val;
    }
  }
  const existing = sessionStore.get<Record<string, string | number | boolean>[]>(REPLY_DIAGNOSTICS_KEY) ?? [];
  const next = [...existing.slice(-199), sanitized];
  sessionStore.set(REPLY_DIAGNOSTICS_KEY, next, 24 * 60 * 60 * 1000);
}
