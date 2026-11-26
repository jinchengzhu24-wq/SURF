import type { ZodType } from "zod";

export const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;

export interface ApiErrorOptions {
  status: number;
  code: string;
  message: string;
  url: string;
  detail?: unknown;
  retryable?: boolean;
  cause?: unknown;
}

/** Structured error thrown by `fetchJson` and `validateSafeNavigationUrl`. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly url: string;
  readonly detail?: unknown;
  readonly retryable: boolean;

  constructor(options: ApiErrorOptions) {
    super(options.message, options.cause !== undefined ? { cause: options.cause } : undefined);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.url = options.url;
    this.detail = options.detail;
    this.retryable = Boolean(options.retryable);
  }
}

export function isLoopbackHost(hostname: string): boolean {
  const normalized = String(hostname || "").trim().toLowerCase();
  return (
    normalized === "localhost" ||
    normalized === "127.0.0.1" ||
    normalized === "::1" ||
    normalized === "[::1]"
  );
}

/**
 * Joins a base URL and path segments using `encodeURIComponent`,
 * optionally appending query parameters.
 */
export function buildUrl(
  base: string,
  pathSegments: readonly (string | number)[],
  query?: Record<string, string | number | boolean | null | undefined>,
): string {
  const normalizedBase = base.replace(/\/+$/, "");
  const encodedPath = pathSegments
    .map((segment) => encodeURIComponent(String(segment)))
    .join("/");
  const pathPart = encodedPath ? `/${encodedPath}` : "";
  const urlBase = `${normalizedBase}${pathPart}` || "/";

  if (!query) {
    return urlBase;
  }

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) {
      params.set(key, String(value));
    }
  }
  const search = params.toString();
  return search ? `${urlBase}?${search}` : urlBase;
}

/**
 * Replaces `{param}` placeholders in a path template using `encodeURIComponent`.
 */
export function interpolatePath(
  template: string,
  params: Record<string, string | number>,
): string {
  return template.replace(/\{([^}]+)\}/g, (_match, key: string) => {
    if (!(key in params)) {
      throw new Error(`Missing URL path parameter: ${key}`);
    }
    return encodeURIComponent(String(params[key]));
  });
}

export interface ValidateNavigationUrlOptions {
  baseUrl?: string;
  allowedOrigins?: readonly string[];
  allowRelative?: boolean;
}

const CONTROL_CHAR_PATTERN = /[\u0000-\u001F\u007F]/;
const SCHEME_PREFIX_PATTERN = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

/**
 * Validates that a navigation URL uses `http:` or `https:`, has a non-null origin,
 * does not smuggle relative-host escapes (`//evil.com`, `/\evil.com`, `\evil.com`),
 * and optionally matches an explicit `allowedOrigins` allowlist.
 */
export function validateSafeNavigationUrl(
  rawUrl: string,
  options: ValidateNavigationUrlOptions = {},
): string {
  const trimmed = typeof rawUrl === "string" ? rawUrl.trim() : "";
  if (!trimmed || CONTROL_CHAR_PATTERN.test(trimmed)) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Navigation URL is empty or contains invalid characters.",
      url: String(rawUrl ?? ""),
    });
  }

  // Block protocol-relative or backslash-normalized host escapes before URL resolution.
  if (
    trimmed.startsWith("//") ||
    trimmed.startsWith("\\") ||
    trimmed.startsWith("/\\") ||
    trimmed.includes("\\")
  ) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Protocol-relative or backslash navigation URLs are not allowed.",
      url: trimmed,
    });
  }

  const hasExplicitScheme = SCHEME_PREFIX_PATTERN.test(trimmed);
  if (hasExplicitScheme && !/^https?:/i.test(trimmed)) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Only http and https navigation URLs are allowed.",
      url: trimmed,
    });
  }

  const allowRelative = options.allowRelative ?? true;
  if (!hasExplicitScheme && !allowRelative) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Relative navigation URLs are not allowed.",
      url: trimmed,
    });
  }

  const fallbackBase =
    options.baseUrl ??
    (typeof globalThis.location?.href === "string" &&
    /^https?:/i.test(globalThis.location.href)
      ? globalThis.location.href
      : "https://sokobanaidemo.top");

  let parsed: URL;
  let baseParsed: URL;
  try {
    baseParsed = new URL(fallbackBase);
    parsed = hasExplicitScheme ? new URL(trimmed) : new URL(trimmed, baseParsed);
  } catch (error) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Navigation URL could not be parsed.",
      url: trimmed,
      cause: error,
    });
  }

  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    !parsed.origin ||
    parsed.origin === "null"
  ) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Navigation URL origin is not allowed.",
      url: trimmed,
    });
  }

  if (!hasExplicitScheme && parsed.origin !== baseParsed.origin) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Relative URL resolved to a different origin.",
      url: trimmed,
    });
  }

  if (options.allowedOrigins && !options.allowedOrigins.includes(parsed.origin)) {
    throw new ApiError({
      status: 0,
      code: "UNSAFE_NAVIGATION_URL",
      message: "Navigation URL origin is not in the allowlist.",
      url: trimmed,
    });
  }

  return parsed.href;
}

export function isSafeNavigationUrl(
  rawUrl: string,
  options?: ValidateNavigationUrlOptions,
): boolean {
  try {
    validateSafeNavigationUrl(rawUrl, options);
    return true;
  } catch {
    return false;
  }
}

export function formatErrorDetail(detail: unknown): string {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => formatErrorDetail(item)).filter(Boolean).join("; ");
  }
  if (typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string" && record.message) return record.message;
    if (typeof record.msg === "string" && record.msg) return record.msg;
    if (typeof record.code === "string" && record.code) return record.code;
    try {
      return JSON.stringify(detail);
    } catch {
      return String(detail);
    }
  }
  return String(detail);
}

function extractErrorMeta(payload: unknown, fallbackStatus: number): {
  code: string;
  message: string;
  detail: unknown;
  retryable?: boolean;
} {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    const detail = "detail" in record ? record.detail : payload;
    const detailRecord =
      detail && typeof detail === "object" && !Array.isArray(detail)
        ? (detail as Record<string, unknown>)
        : null;
    const code =
      (typeof detailRecord?.code === "string" && detailRecord.code) ||
      (typeof record.code === "string" && record.code) ||
      `HTTP_${fallbackStatus}`;
    const message =
      formatErrorDetail(detail) ||
      (typeof record.message === "string" && record.message) ||
      `HTTP ${fallbackStatus}`;
    const retryable =
      typeof detailRecord?.retryable === "boolean"
        ? detailRecord.retryable
        : typeof record.retryable === "boolean"
          ? record.retryable
          : undefined;
    return { code, message, detail, retryable };
  }
  return {
    code: `HTTP_${fallbackStatus}`,
    message: `HTTP ${fallbackStatus}`,
    detail: payload,
  };
}

export interface FetchJsonOptions<T> {
  method?: string;
  headers?: HeadersInit;
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
  schema?: ZodType<T>;
  fetchImpl?: typeof fetch;
  cache?: RequestCache;
  credentials?: RequestCredentials;
}

/**
 * Fetches JSON with `AbortController` timeout enforcement, structured `ApiError`
 * normalization, and optional `zod` runtime response validation.
 */
export async function fetchJson<T>(
  url: string,
  options: FetchJsonOptions<T> = {},
): Promise<T> {
  const {
    method = "GET",
    headers: rawHeaders,
    body,
    timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    signal: externalSignal,
    schema,
    fetchImpl = globalThis.fetch,
    cache,
    credentials,
  } = options;

  if (typeof fetchImpl !== "function") {
    throw new ApiError({
      status: 0,
      code: "FETCH_UNAVAILABLE",
      message: "No fetch implementation is available.",
      url,
    });
  }

  const headers = new Headers(rawHeaders);
  let serializedBody: BodyInit | undefined;
  if (body !== undefined) {
    if (
      typeof body === "string" ||
      body instanceof FormData ||
      body instanceof URLSearchParams ||
      body instanceof Blob ||
      body instanceof ArrayBuffer
    ) {
      serializedBody = body;
    } else {
      if (!headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      serializedBody = JSON.stringify(body);
    }
  }

  const controller = new AbortController();
  let timedOut = false;
  let timerId: ReturnType<typeof setTimeout> | undefined;

  const onExternalAbort = () => {
    controller.abort(externalSignal?.reason);
  };

  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort(externalSignal.reason);
    } else {
      externalSignal.addEventListener("abort", onExternalAbort, { once: true });
    }
  }

  if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
    timerId = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  }

  let response: Response;
  try {
    response = await fetchImpl(url, {
      method,
      headers,
      body: serializedBody,
      signal: controller.signal,
      cache,
      credentials,
    });
  } catch (error) {
    if (timedOut) {
      throw new ApiError({
        status: 0,
        code: "CLIENT_TIMEOUT",
        message: `Request timed out after ${timeoutMs}ms.`,
        url,
        retryable: true,
        cause: error,
      });
    }
    const isAbort =
      error instanceof DOMException
        ? error.name === "AbortError"
        : Boolean((error as { name?: string })?.name === "AbortError");
    throw new ApiError({
      status: 0,
      code: isAbort ? "ABORTED" : "NETWORK_ERROR",
      message: isAbort
        ? "Request was aborted."
        : error instanceof Error
          ? error.message
          : "Network request failed.",
      url,
      retryable: !isAbort,
      cause: error,
    });
  } finally {
    if (timerId !== undefined) clearTimeout(timerId);
    if (externalSignal) {
      externalSignal.removeEventListener("abort", onExternalAbort);
    }
  }

  const rawText = await response.text();
  let parsedBody: unknown = undefined;
  if (rawText.trim().length > 0) {
    try {
      parsedBody = JSON.parse(rawText);
    } catch (error) {
      if (!response.ok) {
        throw new ApiError({
          status: response.status,
          code: `HTTP_${response.status}`,
          message: `HTTP ${response.status}`,
          url,
          detail: rawText,
          retryable: response.status >= 500 || response.status === 429,
          cause: error,
        });
      }
      throw new ApiError({
        status: response.status,
        code: "INVALID_JSON",
        message: "Server returned invalid JSON.",
        url,
        detail: rawText,
        retryable: true,
        cause: error,
      });
    }
  }

  if (!response.ok) {
    const meta = extractErrorMeta(parsedBody, response.status);
    throw new ApiError({
      status: response.status,
      code: meta.code,
      message: meta.message,
      url,
      detail: meta.detail,
      retryable:
        meta.retryable ?? (response.status >= 500 || response.status === 429),
    });
  }

  if (schema) {
    const result = schema.safeParse(parsedBody);
    if (!result.success) {
      throw new ApiError({
        status: response.status,
        code: "SCHEMA_VALIDATION_ERROR",
        message: "Response payload failed schema validation.",
        url,
        detail: result.error.issues,
        retryable: false,
      });
    }
    return result.data;
  }

  return parsedBody as T;
}

export interface ApiClientConfig {
  baseUrl?: string;
  defaultHeaders?: HeadersInit;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

export function createApiClient(config: ApiClientConfig = {}) {
  const baseUrl = (config.baseUrl ?? "").replace(/\/+$/, "");

  return {
    baseUrl,
    url(
      pathSegments: readonly (string | number)[],
      query?: Record<string, string | number | boolean | null | undefined>,
    ): string {
      return buildUrl(baseUrl, pathSegments, query);
    },
    fetchJson<T>(pathOrUrl: string, options: FetchJsonOptions<T> = {}): Promise<T> {
      const target = /^https?:/i.test(pathOrUrl)
        ? pathOrUrl
        : `${baseUrl}${pathOrUrl.startsWith("/") ? pathOrUrl : `/${pathOrUrl}`}`;
      const mergedHeaders = new Headers(config.defaultHeaders);
      if (options.headers) {
        new Headers(options.headers).forEach((value, key) => {
          mergedHeaders.set(key, value);
        });
      }
      return fetchJson<T>(target, {
        fetchImpl: config.fetchImpl,
        timeoutMs: config.timeoutMs,
        ...options,
        headers: mergedHeaders,
      });
    },
  };
}
