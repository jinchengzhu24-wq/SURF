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
    "errors.BINARY_STEP_FAILED",
    [
      "BINARY_PROTECTED_STRATEGY",
      "BINARY_METRIC_GOALS_FORBIDDEN",
      "BINARY_STRATEGY_INVALID",
      "BINARY_PLAN_STRATEGY_INVALID",
      "BINARY_UNUSED_FIELDS",
      "BINARY_ENVELOPE_INVALID",
      "BINARY_ACTION_INVALID",
      "BINARY_TEXT_INVALID",
      "BINARY_PROTECTED_ENTITIES_REQUIRED",
      "BINARY_QUESTION_LIMIT",
      "BINARY_TWO_BRANCHES_REQUIRED",
      "BINARY_BRANCH_INVALID",
      "BINARY_LABEL_INVALID",
      "BINARY_DUPLICATE_BRANCHES",
      "BINARY_CORRIDOR_INVALID",
      "BINARY_CORRIDOR_UNEXPECTED",
      "BINARY_METRIC_UNVERIFIED",
      "BINARY_ALREADY_SELECTED_DIRECTION",
      "BINARY_INITIAL_REQUIREMENTS_REQUIRED",
      "BINARY_INITIAL_REQUIREMENTS_INVALID",
      "BINARY_INITIAL_REQUIREMENTS_LOCKED",
      "BINARY_PREFLIGHT_NOT_FOUND",
    ],
  ],
  [
    "errors.ACTION_IN_PROGRESS",
    [
      "MESSAGE_IN_PROGRESS",
      "BINARY_CHOICE_IN_PROGRESS",
      "CLARIFICATION_IN_PROGRESS",
      "PROPOSAL_DECISION_IN_PROGRESS",
      "QUESTION_FEEDBACK_IN_PROGRESS",
      "INTENT_FEEDBACK_IN_PROGRESS",
      "SAVE_IN_PROGRESS",
      "RESTORE_IN_PROGRESS",
      "ASSESSMENT_IN_PROGRESS",
      "FINALIZE_IN_PROGRESS",
      "RECOVERY_IN_PROGRESS",
    ],
  ],
  [
    "errors.REPLY_FAILED",
    [
      "UPSTREAM_TIMEOUT",
      "UPSTREAM_CONNECTION_ERROR",
      "UPSTREAM_SERVER_ERROR",
      "UPSTREAM_RATE_LIMIT",
      "UPSTREAM_ERROR",
      "DEADLINE_EXCEEDED",
      "MODEL_EMPTY_RESPONSE",
      "MODEL_OUTPUT_TRUNCATED",
      "MODEL_JSON_INVALID",
      "MODEL_SCHEMA_INVALID",
      "MODEL_VALIDATION_FAILED",
      "MODEL_RESPONSE_INVALID",
      "MODEL_LOW_QUALITY_RESPONSE",
      "CLIENT_TIMEOUT",
      "RETRYABLE_REQUEST_FAILED",
      "INVALID_RESPONSE",
      "CHALLENGE_REVIEW_FAILED",
      "NETWORK_ERROR",
      "INVALID_JSON",
      "EMPTY_BODY",
      "GROUNDING_MISMATCH",
      "INTERNAL_TOKEN_LEAK",
      "EVIDENCE_SPAN_MISMATCH",
      "CHANGE_EVIDENCE_MISMATCH",
      "ENGAGEMENT_EVIDENCE_MISMATCH",
      "DIRECTION_SLOTS_MISMATCH",
      "CONFLICT_CARD_REQUIRED",
      "CONFLICT_CARD_FORBIDDEN",
      "REVIEW_MUST_BE_DECLARATIVE",
      "CONFLICT_QUESTION_COUNT",
      "REQUIREMENT_PREFLIGHT_CONFLICT",
      "MAP_OPERATIONS_INVALID",
      "LEVEL_PLAN_INVALID",
      "DG_GUIDE_INVALID",
      "DG_DIFFICULTY_RULE_VIOLATION",
      "DG_LAYOUT_RULE_VIOLATION",
      "DG_SUMMARY_INVALID",
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
  [
    "errors.OLD_PROPOSAL",
    [
      "INVALID_CARD_SOURCE",
      "PROPOSAL_STALE",
      "STALE_PROPOSAL",
      "PROPOSAL_NOT_FOUND",
      "TOPIC_NOT_FOUND",
      "STALE_TOPIC",
      "CHALLENGE_NOT_FOUND",
      "CHALLENGE_NOT_READY",
      "TURN_NOT_FOUND",
      "STALE_REVISION_BINDING",
      "INVALID_REVISION_BINDING",
      "UNBOUND_REVISION",
    ],
  ],
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

function lookupGroupedErrorKey(code: string | undefined): TranslationKey | null {
  if (!code) return null;
  for (const [groupKey, codes] of GROUPED_ERROR_KEYS) {
    if (codes.includes(code)) {
      return groupKey;
    }
  }
  return null;
}

function lookupPatternErrorKey(code: string | undefined): TranslationKey | null {
  if (!code) return null;
  if (code.startsWith("BINARY_")) return "errors.BINARY_STEP_FAILED";
  if (code.startsWith("TRANSLATION_")) return "errors.REPLY_FAILED";
  if (code.endsWith("_IN_PROGRESS")) return "errors.ACTION_IN_PROGRESS";
  return null;
}

function resolveBaseErrorKey(
  code: string | undefined,
  preferGrouped = false,
): TranslationKey {
  if (!code) return "errors.GENERIC";
  if (preferGrouped) {
    return (
      lookupGroupedErrorKey(code) ??
      lookupExactErrorKey(code) ??
      lookupPatternErrorKey(code) ??
      "errors.GENERIC"
    );
  }
  return (
    lookupExactErrorKey(code) ??
    lookupGroupedErrorKey(code) ??
    lookupPatternErrorKey(code) ??
    "errors.GENERIC"
  );
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

  const baseMessage = t(language, resolveBaseErrorKey(error.code, true));
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
    base = t(language, resolveBaseErrorKey(error.code, false));
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
