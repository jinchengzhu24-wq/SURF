import type { Language, StructuredErrorLike } from "../types";
import en from "./en.json";
import zhCN from "./zh-CN.json";

export { REQUEST_COPY } from "./requestCopy";

export type TranslationKey = keyof typeof en;
export type TranslationParams = Record<string, string | number>;

const BUNDLES: Record<Language, Record<TranslationKey, string>> = {
  en,
  "zh-CN": zhCN,
};

export const LEVEL_VALIDATION_ERROR_CODES = new Set<string>([
  "INVALID_LEVEL",
  "INVALID_HEIGHT",
  "INVALID_WIDTH",
  "UNKNOWN_TILE",
  "INVALID_PLAYER_COUNT",
  "INVALID_BOX_COUNT",
  "MISMATCHED_TARGET_COUNT",
  "UNSOLVABLE_LEVEL",
  "SEARCH_BUDGET_EXCEEDED",
  "OPEN_OUTER_WALL",
]);

export function isLevelValidationError(error: StructuredErrorLike | null | undefined): boolean {
  return Boolean(error?.code && LEVEL_VALIDATION_ERROR_CODES.has(error.code));
}

export function t(
  language: Language,
  key: TranslationKey,
  params?: TranslationParams,
): string {
  const bundle = BUNDLES[language] ?? BUNDLES.en;
  const raw = bundle[key] ?? BUNDLES.en[key] ?? key;
  if (!params) return raw;
  return raw.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match,
  );
}

export function tileSymbolName(language: Language, tile: string): string {
  const map: Record<string, TranslationKey> = {
    " ": "tileSymbolVoid",
    "#": "tileSymbolWall",
    ".": "tileSymbolFloor",
    "@": "tileSymbolWater",
    p: "tileSymbolPlayer",
    s: "tileSymbolBox",
    t: "tileSymbolTarget",
  };
  const key = map[tile];
  return key ? t(language, key) : String(tile || "?");
}

export function tileDisplayKey(tile: string): TranslationKey {
  switch (tile) {
    case "#":
      return "wall";
    case "@":
      return "water";
    case "p":
      return "player";
    case "s":
      return "box";
    case "t":
      return "target";
    case ".":
      return "floor";
    default:
      return "erase";
  }
}

export function formatValidationDetails(
  language: Language,
  error: StructuredErrorLike | null | undefined,
): string {
  if (!isLevelValidationError(error) || !error?.details || typeof error.details !== "object") {
    return "";
  }
  const details = error.details;
  const row = Number(details.row);
  const column = Number(details.column);
  const hasRow = Number.isInteger(row);
  const hasColumn = Number.isInteger(column);
  const displayRow = row + (error.code === "OPEN_OUTER_WALL" ? 0 : 1);
  const displayColumn = column + (error.code === "OPEN_OUTER_WALL" ? 0 : 1);

  if (error.code === "OPEN_OUTER_WALL" && hasRow && hasColumn) {
    return t(language, "validation.openOuterWall", {
      row: displayRow,
      column: displayColumn,
      tile: tileSymbolName(language, String(details.tile ?? "?")),
    });
  }

  if (error.code === "INVALID_WIDTH" && hasRow) {
    return t(language, "validation.invalidWidth", { row: displayRow });
  }

  if (error.code === "UNKNOWN_TILE") {
    const tiles = Array.isArray(details.tiles)
      ? details.tiles.join(", ")
      : String(details.tiles ?? "?");
    return hasRow
      ? t(language, "validation.unknownTile", { row: displayRow, tiles })
      : t(language, "validation.unknownTileGeneral", { tiles });
  }

  if (error.code === "INVALID_PLAYER_COUNT" || error.code === "INVALID_BOX_COUNT") {
    const count = Number(details.count);
    if (Number.isFinite(count)) {
      return t(language, "validation.currentCount", { count });
    }
  }

  if (error.code === "MISMATCHED_TARGET_COUNT") {
    const boxes = Number(details.boxes);
    const targets = Number(details.targets);
    if (Number.isFinite(boxes) && Number.isFinite(targets)) {
      return t(language, "validation.mismatchedTargets", { boxes, targets });
    }
  }

  if (error.code === "UNSOLVABLE_LEVEL" || error.code === "SEARCH_BUDGET_EXCEEDED") {
    const searchedStates = Number(details.searchedStates);
    if (Number.isFinite(searchedStates)) {
      return t(language, "validation.searchedStates", { count: searchedStates });
    }
  }

  return "";
}

const GROUPED_ERROR_KEYS: ReadonlyArray<readonly [TranslationKey, readonly string[]]> = [
  [
    "errors.REPLY_FAILED",
    [
      "UPSTREAM_TIMEOUT",
      "UPSTREAM_CONNECTION_ERROR",
      "UPSTREAM_SERVER_ERROR",
      "UPSTREAM_RATE_LIMIT",
      "UPSTREAM_ERROR",
      "MODEL_EMPTY_RESPONSE",
      "MODEL_RESPONSE_INVALID",
      "MODEL_LOW_QUALITY_RESPONSE",
      "CLIENT_TIMEOUT",
      "RETRYABLE_REQUEST_FAILED",
      "INVALID_RESPONSE",
      "CHALLENGE_REVIEW_FAILED",
      "NETWORK_ERROR",
      "INVALID_JSON",
    ],
  ],
  [
    "errors.MODIFICATION_FAILED",
    [
      "REVISION_EXECUTION_INVALID",
      "REVISION_EXECUTION_MISMATCH",
      "SEMANTIC_POSTCONDITION_FAILED",
      "EXECUTION_REPLAY_MISMATCH",
      "CANDIDATE_UNSOLVABLE",
    ],
  ],
  ["errors.OLD_PROPOSAL", ["INVALID_CARD_SOURCE", "PROPOSAL_STALE"]],
  [
    "errors.NO_PROPOSAL",
    ["PROPOSAL_SEARCH_EXHAUSTED", "DETERMINISTIC_SEARCH_EXHAUSTED", "SEMANTIC_CONSTRAINT_NOT_MET"],
  ],
];

function lookupExactErrorKey(code: string | undefined): TranslationKey | null {
  if (!code) return null;
  const candidate = `errors.${code}` as TranslationKey;
  return Object.prototype.hasOwnProperty.call(en, candidate) ? candidate : null;
}

/**
 * User-friendly localized error copy suitable for notices and reply status bars.
 */
export function localizeError(
  language: Language,
  error: StructuredErrorLike | null | undefined,
): string {
  if (!error) return t(language, "errors.GENERIC");
  if (error.details?.failureStage === "context_binding") {
    return t(language, "errors.CONTEXT_CHANGED");
  }
  if (error.code === "PENDING_MESSAGE") {
    return t(language, "chatRetryPending");
  }
  if (error.code === "PROPOSAL_PRECONDITION_FAILED") {
    const reason = error.details?.reason;
    if (reason === "already_satisfied") return t(language, "proposalAlreadySatisfied");
    if (reason === "unbound") return t(language, "proposalUnbound");
    if (reason === "already_executed") return t(language, "proposalAlreadyExecuted");
    return t(language, "proposalPreconditionFailed");
  }

  for (const [groupKey, codes] of GROUPED_ERROR_KEYS) {
    if (error.code && codes.includes(error.code)) {
      return t(language, groupKey);
    }
  }

  const exactKey = lookupExactErrorKey(error.code);
  const baseMessage = exactKey ? t(language, exactKey) : t(language, "errors.GENERIC");
  const details = formatValidationDetails(language, error);
  return details ? `${baseMessage} ${details}` : baseMessage;
}

/**
 * Detailed technical error copy displayed inside collapsible `<details class="technical-details">`.
 */
export function formatTechnicalError(
  language: Language,
  error: StructuredErrorLike | null | undefined,
): { code: string; message: string } | null {
  if (!error) return null;
  const code = error.code && /^[A-Z0-9_]{1,100}$/.test(error.code) ? error.code : "";

  let base: string;
  if (error.details?.failureStage === "context_binding") {
    base = t(language, "errors.CONTEXT_CHANGED");
  } else if (error.details?.failureStage === "clarification_review") {
    base = t(language, "errors.CLARIFICATION_REVIEW_FAILED");
  } else if (error.code === "PENDING_MESSAGE") {
    base = t(language, "chatRetryPending");
  } else if (error.code === "PROPOSAL_PRECONDITION_FAILED") {
    const reason = error.details?.reason;
    base =
      reason === "already_satisfied"
        ? t(language, "proposalAlreadySatisfied")
        : reason === "unbound"
          ? t(language, "proposalUnbound")
          : reason === "already_executed"
            ? t(language, "proposalAlreadyExecuted")
            : t(language, "proposalPreconditionFailed");
  } else {
    const exactKey = lookupExactErrorKey(error.code);
    base = exactKey ? t(language, exactKey) : t(language, "errors.GENERIC");
  }

  const attemptsUsed = Number(error.details?.attemptsUsed);
  const maximumAttempts = Number(error.details?.maximumAttempts);
  const attemptSuffix =
    error.details?.attemptsUsed != null &&
    error.details?.maximumAttempts != null &&
    Number.isInteger(attemptsUsed) &&
    Number.isInteger(maximumAttempts) &&
    maximumAttempts > 0 &&
    attemptsUsed >= 0 &&
    attemptsUsed <= maximumAttempts
      ? ` (${attemptsUsed}/${maximumAttempts})`
      : "";

  const repairUsed = Number(error.details?.repairAttemptsUsed);
  const repairMaximum = Number(error.details?.maximumRepairAttempts);
  const repairSuffix =
    error.details?.repairAttemptsUsed != null &&
    error.details?.maximumRepairAttempts != null &&
    Number.isInteger(repairUsed) &&
    Number.isInteger(repairMaximum) &&
    repairMaximum > 0 &&
    repairUsed >= 0 &&
    repairUsed <= repairMaximum
      ? ` · ${t(language, "componentRepair", { used: repairUsed, max: repairMaximum })}`
      : "";

  const details = formatValidationDetails(language, error);
  const message = details
    ? `${base}${attemptSuffix}${repairSuffix} ${details}`
    : `${base}${attemptSuffix}${repairSuffix}`;

  return { code, message };
}

export { en, zhCN };
