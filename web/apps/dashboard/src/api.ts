import {
  ApiError,
  fetchJson,
  isLoopbackHost,
} from "@sokoban/ui-core";
import { clean } from "./format";
import {
  MatchmakingRecordsResponseSchema,
  MutationResponseSchema,
  type MatchmakingRecordsResponse,
  type MutationResponse,
} from "./types";

export const DEFAULT_API_BASE = "https://sokobanaidemo.top";

export interface LocationLike {
  href?: string;
  origin?: string;
  protocol?: string;
  hostname?: string;
  pathname?: string;
  search?: string;
}

function tryParseUrl(raw: string | undefined): URL | null {
  if (!raw || raw === "null") {
    return null;
  }
  try {
    return new URL(raw);
  } catch {
    return null;
  }
}

/**
 * Resolves the API origin safely, allowing `?api=` overrides only for
 * same-origin URLs or loopback hosts during local development.
 */
export function resolveApiBase(
  locationObj: LocationLike | undefined = typeof globalThis.location !== "undefined"
    ? globalThis.location
    : undefined,
): string {
  const loc = locationObj ?? {};
  const parsedHref = tryParseUrl(loc.href);
  const parsedOrigin = tryParseUrl(loc.origin);
  const protocol =
    loc.protocol || parsedHref?.protocol || parsedOrigin?.protocol || "";
  const origin =
    loc.origin && loc.origin !== "null"
      ? loc.origin
      : parsedHref?.origin || parsedOrigin?.origin || "";
  const hostname =
    loc.hostname !== undefined
      ? String(loc.hostname || "")
      : parsedHref?.hostname || parsedOrigin?.hostname || "";
  const search =
    loc.search !== undefined
      ? String(loc.search || "")
      : parsedHref?.search || "";

  const queryApi = new URLSearchParams(search).get("api");
  if (queryApi) {
    try {
      const baseHref =
        (protocol === "http:" || protocol === "https:") && origin
          ? origin
          : DEFAULT_API_BASE;
      const parsed = new URL(queryApi, baseHref);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        const sameOrigin =
          (protocol === "http:" || protocol === "https:") &&
          Boolean(origin) &&
          parsed.origin === origin;
        const localContext = protocol === "file:" || isLoopbackHost(hostname);
        if (sameOrigin || (localContext && isLoopbackHost(parsed.hostname))) {
          return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
        }
      }
    } catch {
      // Ignore malformed ?api= overrides and fall back safely.
    }
  }

  if ((protocol === "http:" || protocol === "https:") && origin) {
    return origin.replace(/\/+$/, "");
  }

  return DEFAULT_API_BASE;
}

export interface DashboardApiOptions {
  apiBase?: string;
  fetchImpl?: typeof fetch;
  locationObj?: LocationLike;
  timeoutMs?: number;
}

export interface DashboardApi {
  readonly apiBase: string;
  readonly exportStudyLogsUrl: string;
  readonly matchmakingDataUrl: string;
  readonly docsUrl: string;
  apiUrl(path: string): string;
  fetchMatchmakingRecords(signal?: AbortSignal): Promise<MatchmakingRecordsResponse>;
  deleteOnlineMatch(matchId: string, password: string): Promise<MutationResponse>;
  clearMatchmakingRecords(password: string): Promise<MutationResponse>;
  abortPendingLoad(): void;
}

export function createDashboardApi(
  options: DashboardApiOptions = {},
): DashboardApi {
  const apiBase = clean(options.apiBase)
    ? clean(options.apiBase).replace(/\/+$/, "")
    : resolveApiBase(options.locationObj);

  let activeLoadController: AbortController | null = null;

  function apiUrl(path: string): string {
    const normalizedPath = path.startsWith("/") ? path : `/${path}`;
    return `${apiBase}${normalizedPath}`;
  }

  function abortPendingLoad(): void {
    if (activeLoadController) {
      activeLoadController.abort();
      activeLoadController = null;
    }
  }

  async function fetchMatchmakingRecords(
    externalSignal?: AbortSignal,
  ): Promise<MatchmakingRecordsResponse> {
    abortPendingLoad();
    const controller = new AbortController();
    activeLoadController = controller;

    const onExternalAbort = () => controller.abort(externalSignal?.reason);
    if (externalSignal) {
      if (externalSignal.aborted) {
        controller.abort(externalSignal.reason);
      } else {
        externalSignal.addEventListener("abort", onExternalAbort, { once: true });
      }
    }

    try {
      return await fetchJson(apiUrl("/matchmaking-records-data"), {
        method: "GET",
        cache: "no-store",
        signal: controller.signal,
        timeoutMs: options.timeoutMs,
        fetchImpl: options.fetchImpl,
        schema: MatchmakingRecordsResponseSchema,
      });
    } finally {
      if (externalSignal) {
        externalSignal.removeEventListener("abort", onExternalAbort);
      }
      if (activeLoadController === controller) {
        activeLoadController = null;
      }
    }
  }

  async function deleteOnlineMatch(
    matchId: string,
    password: string,
  ): Promise<MutationResponse> {
    return fetchJson(apiUrl("/delete-online-match"), {
      method: "POST",
      headers: {
        "X-Delete-Password": password,
      },
      body: { matchId },
      timeoutMs: options.timeoutMs,
      fetchImpl: options.fetchImpl,
      schema: MutationResponseSchema,
    });
  }

  async function clearMatchmakingRecords(
    password: string,
  ): Promise<MutationResponse> {
    return fetchJson(apiUrl("/clear-matchmaking-records"), {
      method: "POST",
      headers: {
        "X-Delete-Password": password,
      },
      body: {},
      timeoutMs: options.timeoutMs,
      fetchImpl: options.fetchImpl,
      schema: MutationResponseSchema,
    });
  }

  return {
    apiBase,
    exportStudyLogsUrl: apiUrl("/export/study-logs.jsonl"),
    matchmakingDataUrl: apiUrl("/matchmaking-records-data"),
    docsUrl: apiUrl("/docs"),
    apiUrl,
    fetchMatchmakingRecords,
    deleteOnlineMatch,
    clearMatchmakingRecords,
    abortPendingLoad,
  };
}

export function isAbortError(error: unknown): boolean {
  return error instanceof ApiError && error.code === "ABORTED";
}
