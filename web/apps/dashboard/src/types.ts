import type { NoticeVariant } from "@sokoban/ui-core";
import { z } from "zod";

export const RunResultSchema = z
  .object({
    submittedAt: z.string().nullable().optional(),
    durationSeconds: z.number().nullable().optional(),
    moveCount: z.number().nullable().optional(),
    minimumMoves: z.number().nullable().optional(),
    restartCount: z.number().nullable().optional(),
    outcome: z.string().nullable().optional(),
  })
  .passthrough();

export const ChallengeSchema = z
  .object({
    submittedAt: z.string().nullable().optional(),
    aiAssistantMode: z.string().nullable().optional(),
    rows: z.array(z.string()).optional().default([]),
    createdByPlayerNumber: z.number().optional(),
    playedByPlayerNumber: z.number().optional(),
    runResult: RunResultSchema.nullable().optional(),
  })
  .passthrough();

export const NodeEntrySchema = z
  .object({
    kind: z.string().optional(),
    label: z.string().nullable().optional(),
    text: z.string().nullable().optional(),
    occurredAt: z.string().nullable().optional(),
  })
  .passthrough();

export const FlowCardSchema = z
  .object({
    type: z.string().nullable().optional(),
    text: z.string().nullable().optional(),
  })
  .passthrough();

export const CoCreationFlowEventSchema = z
  .object({
    eventId: z.string().nullable().optional(),
    eventType: z.string(),
    matchId: z.string().nullable().optional(),
    roomCode: z.string().nullable().optional(),
    playerNumber: z.number().nullable().optional(),
    studySessionId: z.string().nullable().optional(),
    sessionId: z.string().nullable().optional(),
    serverReceivedAt: z.string().nullable().optional(),
    timestamp: z.string().nullable().optional(),
    versionId: z.string().nullable().optional(),
    stageNumber: z.number().nullable().optional(),
    source: z.string().nullable().optional(),
    initialDraftMethod: z.string().nullable().optional(),
    rows: z.array(z.string()).nullable().optional(),
    message: z.string().nullable().optional(),
    assistantText: z.string().nullable().optional(),
    cards: z.array(FlowCardSchema).nullable().optional(),
    assistantTurnId: z.string().nullable().optional(),
    openingAssistantTurnId: z.string().nullable().optional(),
    finalDifficulty: z.string().nullable().optional(),
    finalLayout: z.string().nullable().optional(),
    q1Answer: z.string().nullable().optional(),
    q2Answer: z.string().nullable().optional(),
    q3Answer: z.string().nullable().optional(),
    q4Answer: z.string().nullable().optional(),
    aiReflection: z.string().nullable().optional(),
    aiDifficultyRationale: z.string().nullable().optional(),
    aiLayoutRationale: z.string().nullable().optional(),
    aiRecommendedDifficulty: z.string().nullable().optional(),
    aiRecommendedLayout: z.string().nullable().optional(),
    aiRecommendationSource: z.string().nullable().optional(),
    draftMetadataComplete: z.boolean().nullable().optional(),
    coCreationDurationSeconds: z.number().nullable().optional(),
    opponentRestartCount: z.number().nullable().optional(),
    nodeId: z.string().nullable().optional(),
    nodeType: z.string().nullable().optional(),
    nodeStatus: z.string().nullable().optional(),
    nodeParentId: z.string().nullable().optional(),
    nodeEntries: z.array(NodeEntrySchema).nullable().optional(),
    nodeUpdatedAt: z.string().nullable().optional(),
    restoredFromVersionId: z.string().nullable().optional(),
    restoredFromStageNumber: z.number().nullable().optional(),
    replacedVersionId: z.string().nullable().optional(),
    replacedStageNumber: z.number().nullable().optional(),
  })
  .passthrough();

export const MatchEventSchema = z
  .object({
    eventId: z.string().nullable().optional(),
    eventType: z.string(),
    matchId: z.string().nullable().optional(),
    roomCode: z.string().nullable().optional(),
    playerNumber: z.number().nullable().optional(),
    statusAfter: z.string().nullable().optional(),
    serverReceivedAt: z.string().nullable().optional(),
    timestamp: z.string().nullable().optional(),
    ready: z.boolean().nullable().optional(),
    aiAssistantMode: z.string().nullable().optional(),
    designerIntention: z.string().nullable().optional(),
    rows: z.array(z.string()).nullable().optional(),
    durationSeconds: z.number().nullable().optional(),
    moveCount: z.number().nullable().optional(),
    minimumMoves: z.number().nullable().optional(),
    restartCount: z.number().nullable().optional(),
    outcome: z.string().nullable().optional(),
    studySessionId: z.string().nullable().optional(),
  })
  .passthrough();

export const MatchPlayerSchema = z
  .object({
    playerNumber: z.number(),
    joinedAt: z.string().nullable().optional(),
    ready: z.boolean().optional().default(false),
    readyAt: z.string().nullable().optional(),
    leftAt: z.string().nullable().optional(),
    challenge: ChallengeSchema.nullable().optional(),
    result: RunResultSchema.nullable().optional(),
    designerIntention: z.string().nullable().optional(),
    studySessionId: z.string().nullable().optional(),
    coCreationFlow: z.array(CoCreationFlowEventSchema).optional().default([]),
  })
  .passthrough();

export const MatchRecordSchema = z
  .object({
    matchId: z.string(),
    roomCode: z.string().nullable().optional().default(""),
    status: z.string().optional().default("in_progress"),
    createdAt: z.string().nullable().optional(),
    updatedAt: z.string().nullable().optional(),
    completedAt: z.string().nullable().optional(),
    endedAt: z.string().nullable().optional(),
    playerCount: z.number().optional().default(0),
    players: z.array(MatchPlayerSchema).optional().default([]),
    events: z.array(MatchEventSchema).optional().default([]),
  })
  .passthrough();

export const MatchmakingSummarySchema = z
  .object({
    matchCount: z.number().optional(),
    completedCount: z.number().optional(),
    inProgressCount: z.number().optional(),
    cancelledCount: z.number().optional(),
    expiredCount: z.number().optional(),
    averageRunDurationSeconds: z.number().optional(),
    malformedCount: z.number().optional(),
  })
  .passthrough();

export const MatchmakingRecordsResponseSchema = z
  .object({
    summary: MatchmakingSummarySchema.optional(),
    matches: z.array(MatchRecordSchema).optional().default([]),
    malformedCount: z.number().optional(),
    logFile: z.string().nullable().optional(),
    generatedAt: z.string().nullable().optional(),
  })
  .passthrough();

export const MutationResponseSchema = z
  .object({
    status: z.string().optional(),
    ok: z.boolean().optional(),
  })
  .passthrough();

export type RunResult = z.infer<typeof RunResultSchema>;
export type Challenge = z.infer<typeof ChallengeSchema>;
export type NodeEntry = z.infer<typeof NodeEntrySchema>;
export type FlowCard = z.infer<typeof FlowCardSchema>;
export type CoCreationFlowEvent = z.infer<typeof CoCreationFlowEventSchema>;
export type MatchEvent = z.infer<typeof MatchEventSchema>;
export type MatchPlayer = z.infer<typeof MatchPlayerSchema>;
export type MatchRecord = z.infer<typeof MatchRecordSchema>;
export type MatchmakingSummary = z.infer<typeof MatchmakingSummarySchema>;
export type MatchmakingRecordsResponse = z.infer<
  typeof MatchmakingRecordsResponseSchema
>;
export type MutationResponse = z.infer<typeof MutationResponseSchema>;

export type StageRecord = CoCreationFlowEvent | MatchEvent;

export interface TimelineStage {
  key: string;
  type: "flow" | "event";
  label: string;
  timestamp: string;
  warning: boolean;
  playerNumber: number;
  record: StageRecord;
}

export interface InspectorRecordRow {
  label: string;
  value: string;
  copyable?: boolean;
}

export interface SummaryStats {
  total: number;
  completed: number;
  completionRate: number;
  averageDuration: number | null;
  attention: number;
  malformed: number;
  hidden: number;
  dataHealthText: string;
}

export type DeleteActionConfig =
  | {
      kind: "match";
      matchId: string;
      roomCode: string;
    }
  | {
      kind: "clear_all";
    };

export interface NoticeState {
  key: number;
  message: string;
  variant: NoticeVariant;
}
