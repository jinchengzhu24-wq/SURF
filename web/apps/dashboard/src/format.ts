import { ApiError } from "@sokoban/ui-core";
import type {
  Challenge,
  CoCreationFlowEvent,
  InspectorRecordRow,
  MatchEvent,
  MatchRecord,
  StageRecord,
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
  const totalSeconds = Math.min(1200, Math.round(input));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
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
