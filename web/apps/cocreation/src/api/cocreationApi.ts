import { ApiError, fetchJson } from "@sokoban/ui-core";
import { isLevelValidationError } from "../i18n";
import type {
  BrowserAccessResponse,
  DemoSessionResponse,
  FeedbackResponse,
  Language,
  PendingBinaryAction,
  PendingMessage,
  PlayAttemptResponse,
  Session,
  StructuredErrorLike,
} from "../types";

export const DEFAULT_API_TIMEOUT_MS = 15_000;
export const LLM_REQUEST_TIMEOUT_MS = 120_000;
export const MESSAGE_REQUEST_TIMEOUT_MS = 125_000;
export const DRAFT_REGENERATION_API_TIMEOUT_MS = 8_000;
export const DRAFT_REGENERATION_POLL_TIMEOUT_MS = 5_000;
export const COMPONENT_RECOVERY_TIMEOUT_MS = 12_000;

export interface RequestOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export class CocreationApiError extends ApiError implements StructuredErrorLike {
  readonly details: Record<string, unknown> | null;

  constructor(options: {
    status: number;
    code: string;
    message: string;
    url: string;
    retryable: boolean;
    details?: Record<string, unknown> | null;
    cause?: unknown;
  }) {
    super({
      status: options.status,
      code: options.code,
      message: options.message,
      url: options.url,
      detail: options.details ?? undefined,
      retryable: options.retryable,
      cause: options.cause,
    });
    this.name = "CocreationApiError";
    this.details = options.details ?? null;
  }
}

export function resolveApiPrefix(pathname?: string): string {
  const path = pathname ?? globalThis.location?.pathname ?? "";
  return path.startsWith("/cocreation") ? "/cocreation" : "";
}

function isValidSessionPayload(payload: unknown): payload is Session {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  return (
    typeof candidate.sessionId === "string" &&
    candidate.sessionId.length > 0 &&
    typeof candidate.status === "string" &&
    Array.isArray(candidate.versions) &&
    Array.isArray(candidate.turns) &&
    Object.prototype.hasOwnProperty.call(candidate, "currentVersionId")
  );
}

function isValidFeedbackPayload(payload: unknown): payload is FeedbackResponse {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  return typeof candidate.outcome === "string" && isValidSessionPayload(candidate.session);
}

function isValidPlayAttemptPayload(payload: unknown): payload is PlayAttemptResponse {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  return typeof candidate.attemptId === "string" && typeof candidate.playUrl === "string";
}

function isValidDemoPayload(payload: unknown): payload is DemoSessionResponse {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  return (
    typeof candidate.sessionId === "string" &&
    candidate.sessionId.length > 0 &&
    typeof candidate.launchUrl === "string" &&
    candidate.launchUrl.length > 0
  );
}

function isValidBrowserAccessPayload(payload: unknown): payload is BrowserAccessResponse {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  return typeof candidate.sessionId === "string" && candidate.sessionId.length > 0;
}

function normalizeErrorDetails(detail: unknown): Record<string, unknown> | null {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return null;
  }
  const record = detail as Record<string, unknown>;
  if (record.details && typeof record.details === "object" && !Array.isArray(record.details)) {
    const nested = record.details as Record<string, unknown>;
    if (typeof record.requestId === "string" && record.requestId && !nested.requestId) {
      return { ...nested, requestId: record.requestId };
    }
    return nested;
  }
  return record;
}

function isSafeRetryRequest(method: string, path: string, body: unknown): boolean {
  if (method === "GET") return true;
  if (
    /\/translations\/(?:en|zh-CN)$/.test(path) ||
    /\/language$/.test(path) ||
    /\/draft-regenerations\/[^/]+\/cancel$/.test(path)
  ) {
    return true;
  }
  if (body && typeof body === "object" && !Array.isArray(body)) {
    const key = (body as Record<string, unknown>).idempotencyKey;
    return typeof key === "string" && key.length > 0;
  }
  return false;
}

export function createCocreationApi(config: {
  apiPrefix?: string;
  fetchImpl?: typeof fetch;
} = {}) {
  const getPrefix = () => config.apiPrefix ?? resolveApiPrefix();

  async function request<T>(
    path: string,
    options: {
      method?: string;
      body?: unknown;
      timeoutMs?: number;
      signal?: AbortSignal;
      fetchImpl?: typeof fetch;
      validate?: (payload: unknown) => payload is T;
    } = {},
  ): Promise<T> {
    const method = options.method ?? "GET";
    const targetUrl = path.startsWith("/api/") ? `${getPrefix()}${path}` : path;
    const safeRetry = isSafeRetryRequest(method, path, options.body);
    const timeoutMs = options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS;

    try {
      const payload = await fetchJson<unknown>(targetUrl, {
        method,
        body: options.body,
        timeoutMs,
        signal: options.signal,
        credentials: "include",
        fetchImpl: options.fetchImpl ?? config.fetchImpl,
      });

      if (options.validate && !options.validate(payload)) {
        throw new CocreationApiError({
          status: 200,
          code: "INVALID_RESPONSE",
          message: "Server response payload did not match expected contract.",
          url: targetUrl,
          retryable: safeRetry,
        });
      }

      return payload as T;
    } catch (error) {
      if (error instanceof CocreationApiError) {
        throw error;
      }

      if (error instanceof ApiError) {
        const details = normalizeErrorDetails(error.detail);
        let code = error.code;
        if (code === "INVALID_JSON") {
          code = error.status >= 400 ? "RETRYABLE_REQUEST_FAILED" : "INVALID_RESPONSE";
        } else if (code === "NETWORK_ERROR") {
          code = "RETRYABLE_REQUEST_FAILED";
        } else if (/^HTTP_\d+$/.test(code)) {
          code = safeRetry && error.status >= 500 ? "RETRYABLE_REQUEST_FAILED" : "REQUEST_FAILED";
        }

        const validationError = isLevelValidationError({ code });
        const retryable = validationError
          ? false
          : code === "CLIENT_TIMEOUT" || code === "RETRYABLE_REQUEST_FAILED" || code === "INVALID_RESPONSE"
            ? safeRetry
            : error.retryable;

        const detailRequestId =
          typeof details?.requestId === "string" && details.requestId ? details.requestId : undefined;
        const resolvedRequestId = error.requestId || detailRequestId;
        const mergedDetails: Record<string, unknown> = {
          ...(details ?? {}),
          httpStatus: error.status,
          ...(resolvedRequestId ? { requestId: resolvedRequestId } : {}),
        };

        throw new CocreationApiError({
          status: error.status,
          code,
          message: error.message,
          url: error.url,
          retryable,
          details: mergedDetails,
          cause: error,
        });
      }

      throw new CocreationApiError({
        status: 0,
        code: safeRetry ? "RETRYABLE_REQUEST_FAILED" : "REQUEST_FAILED",
        message: error instanceof Error ? error.message : "Request failed.",
        url: targetUrl,
        retryable: safeRetry,
        cause: error,
      });
    }
  }

  return {
    createDemoSession(language: Language, idempotencyKey: string, opts?: RequestOptions) {
      return request<DemoSessionResponse>("/api/demo-sessions", {
        method: "POST",
        body: { language, idempotencyKey },
        timeoutMs: opts?.timeoutMs ?? LLM_REQUEST_TIMEOUT_MS,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidDemoPayload,
      });
    },

    exchangeBrowserAccess(sessionId: string, bootstrapToken: string, opts?: RequestOptions) {
      return request<BrowserAccessResponse>(
        `/api/sessions/${encodeURIComponent(sessionId)}/browser-access`,
        {
          method: "POST",
          body: { bootstrapToken },
          timeoutMs: opts?.timeoutMs,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidBrowserAccessPayload,
        },
      );
    },

    getSession(sessionId: string, opts?: RequestOptions) {
      return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: "GET",
        timeoutMs: opts?.timeoutMs,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidSessionPayload,
      });
    },

    setSessionLanguage(sessionId: string, language: Language, opts?: RequestOptions) {
      return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}/language`, {
        method: "PATCH",
        body: { language },
        timeoutMs: opts?.timeoutMs,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidSessionPayload,
      });
    },

    createDraftRegeneration(sessionId: string, idempotencyKey: string, opts?: RequestOptions) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/draft-regenerations`,
        {
          method: "POST",
          body: { idempotencyKey },
          timeoutMs: opts?.timeoutMs ?? DRAFT_REGENERATION_API_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    cancelDraftRegeneration(sessionId: string, requestId: string, opts?: RequestOptions) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/draft-regenerations/${encodeURIComponent(requestId)}/cancel`,
        {
          method: "POST",
          timeoutMs: opts?.timeoutMs ?? DRAFT_REGENERATION_API_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    createAssessment(
      sessionId: string,
      versionId: string,
      idempotencyKey: string,
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/versions/${encodeURIComponent(versionId)}/assessments`,
        {
          method: "POST",
          body: { idempotencyKey },
          timeoutMs: opts?.timeoutMs ?? LLM_REQUEST_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    sendMessage(sessionId: string, payload: PendingMessage, opts?: RequestOptions) {
      return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        body: payload,
        timeoutMs: opts?.timeoutMs ?? MESSAGE_REQUEST_TIMEOUT_MS,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidSessionPayload,
      });
    },

    submitBinaryChoice(
      sessionId: string,
      payload: Omit<PendingBinaryAction, "kind">,
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/proposal-discovery/choices`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs ?? MESSAGE_REQUEST_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    cancelBinaryProposal(
      sessionId: string,
      payload: { baseVersionId: string; topicId: string; idempotencyKey: string },
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/proposal-discovery/cancel`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs ?? MESSAGE_REQUEST_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    saveManualVersion(
      sessionId: string,
      payload: { rows: string[]; baseVersionId: string; idempotencyKey: string; summary: string },
      opts?: RequestOptions,
    ) {
      return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}/versions`, {
        method: "POST",
        body: payload,
        timeoutMs: opts?.timeoutMs,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidSessionPayload,
      });
    },

    restoreVersion(
      sessionId: string,
      versionId: string,
      payload: { baseVersionId: string; idempotencyKey: string },
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/versions/${encodeURIComponent(versionId)}/restore`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    decideProposal(
      sessionId: string,
      proposalId: string,
      payload: { decision: "accept" | "reject"; baseVersionId: string; idempotencyKey: string; reason: string },
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/proposals/${encodeURIComponent(proposalId)}/decision`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    createPlayAttempt(
      sessionId: string,
      versionId: string,
      idempotencyKey: string,
      opts?: RequestOptions,
    ) {
      return request<PlayAttemptResponse>(
        `/api/sessions/${encodeURIComponent(sessionId)}/versions/${encodeURIComponent(versionId)}/play-attempts`,
        {
          method: "POST",
          body: { idempotencyKey },
          timeoutMs: opts?.timeoutMs,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidPlayAttemptPayload,
        },
      );
    },

    finalizeSession(
      sessionId: string,
      payload: { baseVersionId: string; idempotencyKey: string; rows: string[] | null },
      opts?: RequestOptions,
    ) {
      return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}/finalize`, {
        method: "POST",
        body: payload,
        timeoutMs: opts?.timeoutMs,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidSessionPayload,
      });
    },

    submitIntention(
      sessionId: string,
      payload: { content: string; idempotencyKey: string },
      opts?: RequestOptions,
    ) {
      return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}/intention`, {
        method: "POST",
        body: payload,
        timeoutMs: opts?.timeoutMs,
        signal: opts?.signal,
        fetchImpl: opts?.fetchImpl,
        validate: isValidSessionPayload,
      });
    },

    submitQuestionFeedback(
      sessionId: string,
      questionId: string,
      payload: { action: "ignore" | "restore"; baseVersionId: string; idempotencyKey: string },
      opts?: RequestOptions,
    ) {
      return request<FeedbackResponse>(
        `/api/sessions/${encodeURIComponent(sessionId)}/questions/${encodeURIComponent(questionId)}/feedback`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidFeedbackPayload,
        },
      );
    },

    submitIntentFeedback(
      sessionId: string,
      hypothesisId: string,
      payload: {
        action: "confirm" | "reject" | "revise" | "keep";
        candidateText: string | null;
        selectedHypothesisId: string | null;
        sourceTurnId: string;
        baseVersionId: string;
        idempotencyKey: string;
      },
      opts?: RequestOptions,
    ) {
      return request<FeedbackResponse>(
        `/api/sessions/${encodeURIComponent(sessionId)}/intent-hypotheses/${encodeURIComponent(hypothesisId)}/feedback`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs ?? LLM_REQUEST_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidFeedbackPayload,
        },
      );
    },

    translateTurns(
      sessionId: string,
      targetLanguage: Language,
      turnIds: string[],
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/translations/${encodeURIComponent(targetLanguage)}`,
        {
          method: "POST",
          body: { turnIds },
          timeoutMs: opts?.timeoutMs ?? LLM_REQUEST_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },

    retryComponentRecovery(
      sessionId: string,
      recoveryId: string,
      payload: { baseVersionId: string; idempotencyKey: string },
      opts?: RequestOptions,
    ) {
      return request<Session>(
        `/api/sessions/${encodeURIComponent(sessionId)}/component-recoveries/${encodeURIComponent(recoveryId)}/retry`,
        {
          method: "POST",
          body: payload,
          timeoutMs: opts?.timeoutMs ?? COMPONENT_RECOVERY_TIMEOUT_MS,
          signal: opts?.signal,
          fetchImpl: opts?.fetchImpl,
          validate: isValidSessionPayload,
        },
      );
    },
  };
}

export type CocreationApiClient = ReturnType<typeof createCocreationApi>;
export const cocreationApi = createCocreationApi();
