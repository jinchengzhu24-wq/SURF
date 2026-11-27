import {
  SURVEY_DEFINITIONS,
  resolveSurveyDefinition,
  type DisplayLanguage,
  type SurveyDefinition,
} from "./surveyDefinitions";

export type EntrySource = "unity_matchmaking" | "standalone";

export interface SurveyLocationLike {
  href?: string;
  origin?: string;
  protocol?: string;
  hostname?: string;
  pathname?: string;
  search?: string;
}

export interface SurveyLaunchContext {
  readonly surveyId: string | null;
  readonly entrySource: EntrySource;
  readonly studySessionId: string;
  readonly matchId: string;
  readonly roomCode: string;
  readonly playerNumber: number;
  readonly participantId?: string;
  readonly experimentCondition?: string;
  readonly initialLanguage: DisplayLanguage;
}

function cleanParam(params: URLSearchParams, ...keys: string[]): string {
  for (const key of keys) {
    const raw = params.get(key);
    if (raw !== null) {
      const trimmed = raw.trim();
      if (trimmed.length > 0) {
        return trimmed;
      }
    }
  }
  return "";
}

function parsePlayerNumber(raw: string): number {
  const parsed = Number.parseInt(raw, 10);
  return parsed === 1 || parsed === 2 ? parsed : 0;
}

function parseInitialLanguage(raw: string): DisplayLanguage {
  const normalized = raw.trim().toLowerCase();
  if (normalized === "zh" || normalized === "zh-cn" || normalized === "cn") {
    return "zh";
  }
  if (normalized === "en" || normalized === "en-us") {
    return "en";
  }
  return "bilingual";
}

function resolveSearchString(locationObj?: SurveyLocationLike): string {
  const loc =
    locationObj ??
    (typeof globalThis.location !== "undefined" ? globalThis.location : {});
  if (typeof loc.search === "string") {
    return loc.search;
  }
  if (typeof loc.href === "string") {
    try {
      return new URL(loc.href).search;
    } catch {
      return "";
    }
  }
  return "";
}

/**
 * Parses URL query parameters into a normalized survey launch context.
 * Hidden tracking identifiers are kept strictly off-form and carried directly in payload.
 */
export function parseSurveyLaunchContext(
  locationObj?: SurveyLocationLike,
  catalog: readonly SurveyDefinition[] = SURVEY_DEFINITIONS,
): SurveyLaunchContext {
  const params = new URLSearchParams(resolveSearchString(locationObj));
  const rawSurveySelector = cleanParam(params, "type", "survey", "surveyId");
  const matchedSurvey = resolveSurveyDefinition(rawSurveySelector, catalog);

  const studySessionId = cleanParam(params, "studySessionId", "sessionId");
  const matchId = cleanParam(params, "matchId");
  const roomCode = cleanParam(params, "roomCode");
  const playerNumber = parsePlayerNumber(cleanParam(params, "playerNumber"));
  const participantId = cleanParam(params, "participantId") || undefined;
  const experimentCondition =
    cleanParam(params, "experimentCondition", "condition") || undefined;
  const source = cleanParam(params, "source").toLowerCase();

  const entrySource: EntrySource =
    source === "unity" || Boolean(studySessionId) || Boolean(matchId)
      ? "unity_matchmaking"
      : "standalone";

  const initialLanguage = parseInitialLanguage(cleanParam(params, "lang"));

  return {
    surveyId: matchedSurvey?.id ?? null,
    entrySource,
    studySessionId,
    matchId,
    roomCode,
    playerNumber,
    participantId,
    experimentCondition,
    initialLanguage,
  };
}

/** Generates a stable UUIDv4 response identifier for idempotent survey submission. */
export function generateResponseId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID();
  }
  if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    cryptoObj.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  const ts = Date.now().toString(16);
  const rand = Math.random().toString(16).slice(2, 14);
  return `survey-${ts}-${rand}`;
}
