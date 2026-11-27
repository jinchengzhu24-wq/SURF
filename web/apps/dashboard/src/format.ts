import { ApiError } from "@sokoban/ui-core";
import type {
  Challenge,
  CoCreationFlowEvent,
  InspectorRecordRow,
  MapDiffCell,
  MatchEvent,
  MatchRecord,
  ParsedSurveyAnswer,
  SolverMetrics,
  StageRecord,
  SurveyScope,
  TimelineStage,
} from "./types";

export function clean(input: unknown): string {
  if (input === null || input === undefined) {
    return "";
  }
  const text = String(input).trim();
  return text === "-" ? "" : text;
}

export function value(input: unknown): string {
  if (input === null || input === undefined || input === "") {
    return "-";
  }
  return String(input);
}

export function numeric(input: unknown): number {
  return typeof input === "number" && Number.isFinite(input) ? input : 0;
}

export function compactId(input: unknown): string {
  const text = clean(input);
  return text ? text.slice(0, 8) : "-";
}

export function titleCase(input: unknown): string {
  return (
    clean(input)
      .replace(/[-_]+/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase()) || "-"
  );
}

export function formatSeconds(input: unknown): string {
  if (typeof input !== "number" || !Number.isFinite(input)) {
    return "-";
  }
  return `${input.toFixed(1)}s`;
}

export function formatClockDuration(input: unknown): string {
  if (typeof input !== "number" || !Number.isFinite(input) || input < 0) {
    return "-";
  }
  const totalSeconds = Math.round(input);
  if (totalSeconds >= 3600) {
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(
      2,
      "0",
    );
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    return `${hours}:${minutes}:${seconds}`;
  }
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export function formatMapDiffCell(cell: MapDiffCell): string {
  return `(${cell.x}, ${cell.y}): ${cell.before} -> ${cell.after}`;
}

export function formatSolverSummary(
  solver: SolverMetrics | null | undefined,
): string {
  if (!solver) return "-";
  if (!solver.solvable) return "Unsolvable";
  const parts = ["Solvable"];
  if (solver.minSteps !== null && solver.minSteps !== undefined) {
    parts.push(`${solver.minSteps} min steps`);
  }
  if (solver.minPushes !== null && solver.minPushes !== undefined) {
    parts.push(`${solver.minPushes} min pushes`);
  }
  return parts.join(" · ");
}

function isMapDiffCellLike(item: unknown): item is MapDiffCell {
  if (!item || typeof item !== "object") return false;
  const candidate = item as Record<string, unknown>;
  return (
    typeof candidate.x === "number" &&
    typeof candidate.y === "number" &&
    typeof candidate.before === "string" &&
    typeof candidate.after === "string"
  );
}

export function formatMapDiffEntryText(
  rawText: string | null | undefined,
): string {
  const raw = clean(rawText);
  if (!raw) return "-";
  if (!raw.startsWith("[") && !raw.startsWith("{")) {
    return raw;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every(isMapDiffCellLike)) {
      if (parsed.length === 0) return "No cell changes";
      return parsed.map(formatMapDiffCell).join("\n");
    }
    if (parsed && typeof parsed === "object") {
      const record = parsed as Record<string, unknown>;
      const lines: string[] = [];
      if (Array.isArray(record.diff) && record.diff.every(isMapDiffCellLike)) {
        lines.push(
          record.diff.length > 0
            ? record.diff.map(formatMapDiffCell).join(", ")
            : "No cell changes",
        );
      }
      if (record.validation && typeof record.validation === "object") {
        const validation = record.validation as Record<string, unknown>;
        if (typeof validation.solvable === "boolean") {
          const steps =
            typeof validation.solutionSteps === "number"
              ? validation.solutionSteps
              : typeof validation.minSteps === "number"
                ? validation.minSteps
                : null;
          const pushes =
            typeof validation.solutionPushes === "number"
              ? validation.solutionPushes
              : typeof validation.minPushes === "number"
                ? validation.minPushes
                : null;
          lines.push(
            formatSolverSummary({
              solvable: validation.solvable,
              minSteps: steps,
              minPushes: pushes,
            }),
          );
        }
      }
      if (lines.length > 0) {
        return lines.join("\n");
      }
    }
  } catch {
    // Fall back to raw text when not JSON.
  }
  return raw;
}

export function formatTimestamp(input: unknown): string {
  const raw = clean(input);
  if (!raw) {
    return "-";
  }
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString();
}

export function formatShortDate(input: unknown): string {
  const raw = clean(input);
  if (!raw) {
    return "No time";
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return raw.slice(0, 10);
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function getTimestamp(
  record: { serverReceivedAt?: string | null; timestamp?: string | null } | null | undefined,
): string {
  return clean(record?.serverReceivedAt || record?.timestamp);
}

export function getMatchDisplayTimestamp(match: MatchRecord | null | undefined): string {
  return clean(match?.createdAt || match?.updatedAt);
}

export function hasFirstStage(match: MatchRecord | null | undefined): boolean {
  return (match?.players ?? []).some((player) =>
    (player.coCreationFlow ?? []).some(
      (event) => event.eventType === "first_stage",
    ),
  );
}

export function isCompletedMatch(match: MatchRecord | null | undefined): boolean {
  if (!match || match.status !== "completed") {
    return false;
  }
  return [1, 2].every((playerNumber) =>
    (match.players ?? []).some(
      (player) => player.playerNumber === playerNumber && Boolean(player.result),
    ),
  );
}

export function statusLabel(status: string | null | undefined): string {
  const labels: Record<string, string> = {
    completed: "Completed",
    in_progress: "In progress",
    cancelled: "Cancelled",
    expired: "Expired",
  };
  const key = clean(status);
  return labels[key] || titleCase(key);
}

export function statusClass(
  status: string | null | undefined,
): "completed" | "anomaly" | "progress" {
  if (status === "completed") return "completed";
  if (status === "cancelled" || status === "expired") return "anomaly";
  return "progress";
}

export function formatMode(input: unknown): string {
  const key = clean(input);
  if (key === "description_generation") {
    return "Description Generation";
  }
  return titleCase(key);
}

export function dashboardNodeLabel(nodeType: string | null | undefined): string {
  const labels: Record<string, string> = {
    discussion: "General Discussion",
    intent: "Intent",
    intent_conflict: "Intent Conflict",
    proposal: "Proposal",
    player_challenge: "Player Challenge",
    manual_edit_review: "Manual Edit Review",
    llm_challenge: "LLM Challenge",
  };
  return (nodeType && labels[nodeType]) || "Co-creation Node";
}

export function dashboardNodeStatus(status: string | null | undefined): string {
  const labels: Record<string, string> = {
    in_progress: "In progress",
    awaiting_player_choice: "Awaiting player choice",
    clarifying_1: "Proposal clarification 1/3",
    clarifying_2: "Proposal clarification 2/3",
    clarifying_3: "Proposal clarification 3/3",
    proposal_ready: "Proposal ready",
    pending: "Pending",
    accepted: "Accepted",
    rejected: "Rejected",
    replaced: "Replaced",
    superseded: "Superseded",
    retry_pending: "Retry pending",
    revision_needed: "Revision needed",
    exited: "Exited",
    resolved: "Resolved",
    confirmed: "Confirmed",
    cancelled: "Cancelled",
    reviewed: "Reviewed",
    acknowledged: "Responded · proposal block released",
    manual_reedit: "Ended through a new manual edit",
    original_selected: "Original proposal selected",
    replacement_selected: "New plan from the player's reason selected",
    awaiting_reason: "Awaiting challenge reason",
    review_pending: "Reason review pending retry",
    revised: "Revised intention",
  };
  const normalized = clean(status);
  return (
    labels[normalized] || titleCase(normalized.replace(/_/g, " ") || "In progress")
  );
}

export function flowStageLabel(event: CoCreationFlowEvent): string {
  if (event.eventType === "stage") {
    const sourceLabel =
      event.source === "restored"
        ? "Restored"
        : event.source === "ai"
          ? "AI"
          : "Manual";
    return `Stage ${value(event.stageNumber)} · ${sourceLabel}`;
  }
  if (event.eventType === "node") {
    return dashboardNodeLabel(event.nodeType);
  }
  const labels: Record<string, string> = {
    draft: "Draft",
    first_stage: "First Stage",
    final: "Final Stage",
    message: "Message",
  };
  return labels[event.eventType] || titleCase(event.eventType || "Flow event");
}

export function eventStageLabel(event: MatchEvent): string {
  const player = numeric(event.playerNumber) ? `P${event.playerNumber} ` : "";
  const labels: Record<string, string> = {
    room_created: "Room created",
    player_joined: `${player}joined`,
    ready_changed: `${player}${event.ready ? "ready" : "not ready"}`,
    challenge_submitted: `${player}challenge submitted`,
    designer_intention_synchronized: `${player}message confirmed`,
    result_submitted: `${player}result submitted`,
    player_left: `${player}left`,
    room_expired: "Room expired",
  };
  return labels[event.eventType] || titleCase(event.eventType || "Match event");
}

export function formatNodeMetrics(
  record: CoCreationFlowEvent,
): Array<[string, string]> {
  const rows: Array<[string, string]> = [
    ["Status", dashboardNodeStatus(record.nodeStatus)],
    ["Started", formatTimestamp(record.serverReceivedAt)],
    [
      "Last updated",
      formatTimestamp(record.nodeUpdatedAt || record.serverReceivedAt),
    ],
  ];
  if (record.stageNumber !== undefined && record.stageNumber !== null) {
    rows.push(["Stage", `Stage ${value(record.stageNumber)}`]);
  }
  if (record.nodeParentId) {
    rows.push(["Related node", record.nodeParentId]);
  }
  return rows;
}

const ACTIVE_FLOW_EVENT_TYPES = new Set([
  "draft",
  "first_stage",
  "stage",
  "final",
  "message",
  "node",
]);

export function buildTimelineStages(match: MatchRecord): TimelineStage[] {
  const flowStages: TimelineStage[] = (match.players ?? []).flatMap((player) =>
    (player.coCreationFlow ?? [])
      .filter((event) => ACTIVE_FLOW_EVENT_TYPES.has(event.eventType))
      .map((event, index) => ({
        key: `flow:${player.playerNumber}:${clean(event.eventId) || index}`,
        type: "flow" as const,
        label: flowStageLabel(event),
        timestamp: getTimestamp(event),
        warning: false,
        playerNumber: player.playerNumber,
        record: event,
      })),
  );

  if (flowStages.length > 0) {
    return flowStages.sort((left, right) =>
      left.timestamp.localeCompare(right.timestamp),
    );
  }

  return (match.events ?? [])
    .map((event, index) => ({
      key: `event:${clean(event.eventId) || index}`,
      type: "event" as const,
      label: eventStageLabel(event),
      timestamp: getTimestamp(event),
      warning:
        event.eventType === "player_left" || event.eventType === "room_expired",
      playerNumber: numeric(event.playerNumber),
      record: event,
    }))
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

export function findOpeningForStage(
  match: MatchRecord,
  record: CoCreationFlowEvent,
): CoCreationFlowEvent | null {
  if (!record.versionId) return null;
  const player = (match.players ?? []).find(
    (item) => item.playerNumber === record.playerNumber,
  );
  if (!player) return null;
  return (
    (player.coCreationFlow ?? []).find(
      (event) =>
        event.eventType === "opening" && event.versionId === record.versionId,
    ) ?? null
  );
}

export function openingIsConsumed(
  match: MatchRecord,
  opening: CoCreationFlowEvent | null | undefined,
): boolean {
  if (!opening) return false;
  if (opening.consumed === true) return true;
  if (!opening.assistantTurnId) return false;
  const player = (match.players ?? []).find(
    (item) => item.playerNumber === opening.playerNumber,
  );
  return (player?.coCreationFlow ?? []).some(
    (event) =>
      (event.eventType === "node" || event.eventType === "turn") &&
      event.openingAssistantTurnId === opening.assistantTurnId,
  );
}

export function formatChallengeOutcome(outcome: string | null | undefined): string {
  if (outcome === "timed_out") return "Timed out";
  if (outcome === "completed") return "Completed";
  return outcome ? titleCase(outcome) : "-";
}

export function formatChallengeMetrics(
  challenge: Challenge,
): Array<[string, string]> {
  const result = challenge.runResult;
  const hasResult = Boolean(result);
  const outcome = hasResult ? result?.outcome || "completed" : "";
  const isCompleted = hasResult && outcome !== "timed_out";
  const overhead =
    isCompleted &&
    typeof result?.moveCount === "number" &&
    typeof result?.minimumMoves === "number"
      ? result.moveCount - result.minimumMoves
      : null;

  return [
    ["Created by", `Player ${value(challenge.createdByPlayerNumber)}`],
    ["Played by", `Player ${value(challenge.playedByPlayerNumber)}`],
    ["Outcome", hasResult ? formatChallengeOutcome(outcome) : "-"],
    ["Play time", formatSeconds(result?.durationSeconds)],
    ["Restarts", value(result?.restartCount)],
    ["Moves", value(result?.moveCount)],
    ["Minimum moves", value(result?.minimumMoves)],
    [
      "Move overhead",
      overhead === null ? "-" : `${overhead >= 0 ? "+" : ""}${overhead}`,
    ],
  ];
}

export function buildInspectorRecordRows(
  match: MatchRecord,
  record: StageRecord,
): InspectorRecordRow[] {
  const players = match.players ?? [];
  const playerOne = players.find((player) => player.playerNumber === 1);
  const playerTwo = players.find((player) => player.playerNumber === 2);

  const rows: InspectorRecordRow[] = [
    { label: "Event", value: titleCase(record.eventType) },
    {
      label: "Player",
      value: record.playerNumber ? `Player ${record.playerNumber}` : "Room",
    },
    { label: "Recorded", value: formatTimestamp(getTimestamp(record)) },
    {
      label: "Player 1 Study session ID",
      value: clean(playerOne?.studySessionId),
      copyable: true,
    },
    {
      label: "Player 2 Study session ID",
      value: clean(playerTwo?.studySessionId),
      copyable: true,
    },
  ];

  if ("statusAfter" in record && record.statusAfter) {
    rows.splice(2, 0, {
      label: "Status after",
      value: titleCase(record.statusAfter),
    });
  }
  if (record.eventType === "ready_changed" && "ready" in record) {
    rows.push({ label: "Ready", value: record.ready ? "Yes" : "No" });
  }
  if (record.eventType === "draft") {
    const draft = record as CoCreationFlowEvent;
    rows.push({ label: "Difficulty", value: value(draft.finalDifficulty) });
    rows.push({ label: "Layout", value: value(draft.finalLayout) });
  }
  if (
    record.eventType === "designer_intention_synchronized" &&
    "designerIntention" in record
  ) {
    rows.push({ label: "Message", value: value(record.designerIntention) });
  }
  if (record.eventType === "result_submitted") {
    const resultEvent = record as MatchEvent;
    rows.push({
      label: "Outcome",
      value: formatChallengeOutcome(resultEvent.outcome || "completed"),
    });
    rows.push({
      label: "Play time",
      value: formatSeconds(resultEvent.durationSeconds),
    });
    rows.push({ label: "Restarts", value: value(resultEvent.restartCount) });
    rows.push({ label: "Moves", value: value(resultEvent.moveCount) });
    rows.push({
      label: "Minimum moves",
      value: value(resultEvent.minimumMoves),
    });
  }
  if (record.eventType === "final") {
    const finalEvent = record as CoCreationFlowEvent;
    rows.push({
      label: "Co-creation time",
      value: formatClockDuration(finalEvent.coCreationDurationSeconds),
    });
    rows.push({
      label: "Opponent restarts",
      value: value(finalEvent.opponentRestartCount),
    });
  }

  return rows;
}

export function getDeleteErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return "Incorrect password. Please try again.";
    }
    if (error.status === 503) {
      return "Deletion password is not configured on the server.";
    }
    return error.message || "Deletion failed. Please try again.";
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Deletion failed. Please try again.";
}

export async function copyTextToClipboard(text: string): Promise<boolean> {
  const valueToCopy = clean(text);
  if (!valueToCopy) return false;

  try {
    if (
      typeof navigator !== "undefined" &&
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === "function"
    ) {
      await navigator.clipboard.writeText(valueToCopy);
      return true;
    }
  } catch {
    // Fall back to execCommand below.
  }

  if (
    typeof document === "undefined" ||
    !document.body ||
    typeof document.execCommand !== "function"
  ) {
    return false;
  }

  const textarea = document.createElement("textarea");
  textarea.value = valueToCopy;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  textarea.remove();
  return copied;
}

export function formatSurveyLabel(
  surveyId: string | null | undefined,
  surveyTitle?: string | null,
): string {
  const id = clean(surveyId);
  if (id === "online_pre_match_survey") {
    return "Pre-Match · Online1";
  }
  if (id === "online_post_match_survey") {
    return "Post-Match · Online2";
  }
  if (!id || id === "legacy") {
    return "Legacy Survey";
  }
  return clean(surveyTitle) || titleCase(id);
}

export function formatSurveySectionLabel(
  sectionId: string | null | undefined,
): string {
  const raw = clean(sectionId);
  if (!raw) {
    return "General";
  }
  const partMatch = /^part_([a-z0-9]+)$/i.exec(raw);
  if (partMatch) {
    return `Part ${partMatch[1].toUpperCase()}`;
  }
  if (raw === "pre_match_background") {
    return "Pre-Match Background";
  }
  return titleCase(raw);
}

export function formatSurveyScopeLabel(scope: SurveyScope): string {
  const labels: Record<SurveyScope, string> = {
    active_matches: "Active Formal Matches",
    formal: "All Formal",
    standalone: "Standalone Tests",
    all: "All Records",
  };
  return labels[scope];
}

export function formatStatNumber(
  input: number | null | undefined,
  digits = 2,
): string {
  if (typeof input !== "number" || !Number.isFinite(input)) {
    return "—";
  }
  return Number.isInteger(input) && digits === 0
    ? String(input)
    : input.toFixed(digits);
}

export function formatBilingualText(
  zh: string | null | undefined,
  en: string | null | undefined,
  fallback = "-",
): string {
  const cleanZh = clean(zh);
  const cleanEn = clean(en);
  if (cleanZh && cleanEn) {
    return cleanZh === cleanEn ? cleanZh : `${cleanZh} / ${cleanEn}`;
  }
  return cleanZh || cleanEn || fallback;
}

function asNullableString(input: unknown): string | null {
  if (input === null || input === undefined) {
    return null;
  }
  const text = String(input).trim();
  return text ? text : null;
}

function asNullableNumber(input: unknown): number | null {
  if (typeof input === "number" && Number.isFinite(input)) {
    return input;
  }
  if (typeof input === "string" && input.trim()) {
    const parsed = Number(input);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function parseSurveyAnswersList(
  rawAnswers: unknown,
): ParsedSurveyAnswer[] {
  if (!Array.isArray(rawAnswers)) {
    return [];
  }
  const parsed: ParsedSurveyAnswer[] = [];
  rawAnswers.forEach((entry, idx) => {
    if (!entry || typeof entry !== "object") {
      return;
    }
    const item = entry as Record<string, unknown>;
    const questionIndex = asNullableNumber(item.questionIndex);
    const questionId =
      asNullableString(item.questionId) ||
      (questionIndex !== null ? `q_${questionIndex}` : `q_${idx + 1}`);
    const score = asNullableNumber(item.score);
    const textValue = asNullableString(item.textValue);
    const rawType = asNullableString(item.questionType);
    const questionType: ParsedSurveyAnswer["questionType"] =
      rawType === "likert_5" || rawType === "textarea" || rawType === "single_choice"
        ? rawType
        : score !== null
          ? "likert_5"
          : textValue !== null
            ? "textarea"
            : "single_choice";

    parsed.push({
      sectionId: asNullableString(item.sectionId) || "default",
      questionIndex,
      questionId,
      questionType,
      questionTextZh: asNullableString(item.questionTextZh),
      questionTextEn: asNullableString(item.questionTextEn),
      optionId: asNullableString(item.optionId),
      optionIndex: asNullableNumber(item.optionIndex),
      optionTextZh: asNullableString(item.optionTextZh),
      optionTextEn: asNullableString(item.optionTextEn),
      score,
      textValue,
    });
  });
  return parsed;
}

export function formatLegacyPayloadEntries(
  payload: Record<string, unknown> | null | undefined,
): Array<[string, string]> {
  if (!payload || typeof payload !== "object") {
    return [];
  }
  return Object.entries(payload)
    .filter(([key]) => key !== "answers")
    .map(([key, val]) => {
      if (val === null || val === undefined || val === "") {
        return [key, "-"];
      }
      if (typeof val === "object") {
        try {
          return [key, JSON.stringify(val)];
        } catch {
          return [key, String(val)];
        }
      }
      return [key, String(val)];
    });
}
