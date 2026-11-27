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

export const MapDiffCellSchema = z
  .object({
    x: z.number(),
    y: z.number(),
    before: z.string(),
    after: z.string(),
  })
  .passthrough();

export const SolverMetricsSchema = z
  .object({
    solvable: z.boolean(),
    minSteps: z.number().nullable().optional(),
    minPushes: z.number().nullable().optional(),
  })
  .passthrough();

export const PlayAttemptSchema = z
  .object({
    attemptId: z.string(),
    versionId: z.string().nullable().optional(),
    stageNumber: z.number().nullable().optional(),
    mode: z.string().nullable().optional(),
    language: z.string().nullable().optional(),
    status: z.string(),
    issuedAt: z.string().nullable().optional(),
    loadedAt: z.string().nullable().optional(),
    firstMoveAt: z.string().nullable().optional(),
    finishedAt: z.string().nullable().optional(),
    durationSeconds: z.number().nullable().optional(),
    moveCount: z.number().nullable().optional(),
    pushCount: z.number().nullable().optional(),
    restartCount: z.number().nullable().optional(),
    minimumMoves: z.number().nullable().optional(),
    minimumPushes: z.number().nullable().optional(),
  })
  .passthrough();

export const DraftRegenerationSchema = z
  .object({
    requestId: z.string(),
    status: z.string(),
    generation: z.number().nullable().optional(),
    failureCode: z.string().nullable().optional(),
    rows: z.array(z.string()).nullable().optional(),
    solver: SolverMetricsSchema.nullable().optional(),
    requestedAt: z.string().nullable().optional(),
    updatedAt: z.string().nullable().optional(),
  })
  .passthrough();

export const NodeEntrySchema = z
  .object({
    entryId: z.string().nullable().optional(),
    kind: z.string().optional(),
    label: z.string().nullable().optional(),
    text: z.string().nullable().optional(),
    occurredAt: z.string().nullable().optional(),
    language: z.string().nullable().optional(),
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
    diff: z.array(MapDiffCellSchema).nullable().optional(),
    solver: SolverMetricsSchema.nullable().optional(),
    playAttempts: z.array(PlayAttemptSchema).nullable().optional(),
    draftRegenerations: z.array(DraftRegenerationSchema).nullable().optional(),
    message: z.string().nullable().optional(),
    assistantText: z.string().nullable().optional(),
    language: z.string().nullable().optional(),
    cards: z.array(FlowCardSchema).nullable().optional(),
    consumed: z.boolean().nullable().optional(),
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
    playAttempts: z.array(PlayAttemptSchema).optional().default([]),
    draftRegenerations: z.array(DraftRegenerationSchema).optional().default([]),
  })
  .passthrough();

export const MatchSurveyRecordSchema = z
  .object({
    recordId: z.number(),
    surveyId: z.string().nullable().optional(),
    surveyVersion: z.string().nullable().optional(),
    entrySource: z.string().nullable().optional(),
    playerSlot: z.union([z.literal(1), z.literal(2)]).nullable().optional(),
    matchedBy: z.enum(["matchId", "studySessionId"]),
    createdAt: z.string(),
    payload: z.record(z.string(), z.unknown()),
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
    surveys: z.array(MatchSurveyRecordSchema).optional().default([]),
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

export const MatchmakingRecordsPayloadSchema = MatchmakingRecordsResponseSchema;

export const MutationResponseSchema = z
  .object({
    status: z.string().optional(),
    ok: z.boolean().optional(),
  })
  .passthrough();

export const SurveyScopeSchema = z.enum([
  "active_matches",
  "formal",
  "standalone",
  "all",
]);

export const PlayerSlotSchema = z.union([z.literal(1), z.literal(2)]);

export const SurveyAnalyticsFiltersSchema = z
  .object({
    scope: z.string(),
    surveyId: z.string().nullable(),
    surveyVersion: z.string().nullable(),
  })
  .passthrough();

export const SurveyScopeCountsSchema = z
  .object({
    active_matches: z.number(),
    formal: z.number(),
    standalone: z.number(),
    all: z.number(),
  })
  .passthrough();

export const AvailableSurveySchema = z
  .object({
    surveyId: z.string(),
    surveyTitle: z.string().nullable(),
    responseCount: z.number(),
    versions: z.array(z.string()),
  })
  .passthrough();

export const SurveyKpiSchema = z
  .object({
    responseCount: z.number(),
    linkedMatchCount: z.number(),
    avgDurationSeconds: z.number().nullable(),
    medianDurationSeconds: z.number().nullable(),
    latestSubmittedAt: z.string().nullable(),
    versions: z.array(z.string()),
  })
  .passthrough();

export const SurveyLikertSummarySchema = z
  .object({
    questionCount: z.number(),
    scoreCount: z.number(),
    mean: z.number(),
    sd: z.number().nullable(),
    median: z.number(),
    rawScoresNote: z.literal("Raw scores (unreversed)"),
  })
  .passthrough();

export const SingleChoiceOptionAnalyticsSchema = z
  .object({
    optionId: z.string(),
    optionIndex: z.number().nullable(),
    optionTextZh: z.string().nullable(),
    optionTextEn: z.string().nullable(),
    count: z.number(),
    percentage: z.number(),
  })
  .passthrough();

export const SingleChoiceQuestionAnalyticsSchema = z
  .object({
    sectionId: z.string(),
    questionIndex: z.number().nullable(),
    questionId: z.string(),
    questionType: z.literal("single_choice"),
    questionTextZh: z.string().nullable(),
    questionTextEn: z.string().nullable(),
    answeredCount: z.number(),
    skippedCount: z.number(),
    totalResponses: z.number(),
    responseRate: z.number(),
    options: z.array(SingleChoiceOptionAnalyticsSchema),
  })
  .passthrough();

export const LikertBucketAnalyticsSchema = z
  .object({
    score: z.number(),
    optionId: z.string(),
    optionTextZh: z.string().nullable(),
    optionTextEn: z.string().nullable(),
    count: z.number(),
    percentage: z.number(),
  })
  .passthrough();

export const LikertStatsSchema = z
  .object({
    mean: z.number().nullable(),
    sd: z.number().nullable(),
    median: z.number().nullable(),
    min: z.number().nullable(),
    max: z.number().nullable(),
  })
  .passthrough();

export const Likert5QuestionAnalyticsSchema = z
  .object({
    sectionId: z.string(),
    questionIndex: z.number().nullable(),
    questionId: z.string(),
    questionType: z.literal("likert_5"),
    questionTextZh: z.string().nullable(),
    questionTextEn: z.string().nullable(),
    answeredCount: z.number(),
    skippedCount: z.number(),
    totalResponses: z.number(),
    responseRate: z.number(),
    buckets: z.array(LikertBucketAnalyticsSchema),
    stats: LikertStatsSchema,
  })
  .passthrough();

export const TextareaEntrySchema = z
  .object({
    recordId: z.number(),
    text: z.string(),
    matchId: z.string().nullable(),
    roomCode: z.string().nullable(),
    playerSlot: PlayerSlotSchema.nullable(),
    linkedActiveMatchId: z.string().nullable(),
    studySessionId: z.string().nullable(),
    entrySource: z.string().nullable(),
    submittedAt: z.string().nullable(),
  })
  .passthrough();

export const TextareaQuestionAnalyticsSchema = z
  .object({
    sectionId: z.string(),
    questionIndex: z.number().nullable(),
    questionId: z.string(),
    questionType: z.literal("textarea"),
    questionTextZh: z.string().nullable(),
    questionTextEn: z.string().nullable(),
    answeredCount: z.number(),
    skippedCount: z.number(),
    totalResponses: z.number(),
    responseRate: z.number(),
    nonEmptyCount: z.number(),
    entries: z.array(TextareaEntrySchema),
  })
  .passthrough();

export const SurveyQuestionAnalyticsSchema = z.discriminatedUnion(
  "questionType",
  [
    SingleChoiceQuestionAnalyticsSchema,
    Likert5QuestionAnalyticsSchema,
    TextareaQuestionAnalyticsSchema,
  ],
);

export const SurveySectionAnalyticsSchema = z
  .object({
    sectionId: z.string(),
    questionCount: z.number(),
    questionIds: z.array(z.string()),
    questions: z.array(SurveyQuestionAnalyticsSchema),
    likertSummary: SurveyLikertSummarySchema.nullable(),
  })
  .passthrough();

export const SurveyResponseRecordSchema = z
  .object({
    recordId: z.number(),
    responseId: z.string().nullable(),
    surveyId: z.string(),
    surveyVersion: z.string().nullable(),
    surveyTitle: z.string().nullable(),
    entrySource: z.string().nullable(),
    matchId: z.string().nullable(),
    roomCode: z.string().nullable(),
    playerSlot: PlayerSlotSchema.nullable(),
    linkedActiveMatchId: z.string().nullable(),
    studySessionId: z.string().nullable(),
    participantId: z.string().nullable(),
    experimentCondition: z.string().nullable(),
    language: z.string().nullable(),
    submittedAt: z.string().nullable(),
    createdAt: z.string(),
    durationSeconds: z.number().nullable(),
    answerCount: z.number(),
    answers: z.array(z.record(z.string(), z.unknown())),
  })
  .passthrough();

export const SurveyGroupAnalyticsSchema = z
  .object({
    surveyId: z.string(),
    surveyTitle: z.string().nullable(),
    kpi: SurveyKpiSchema,
    sections: z.array(SurveySectionAnalyticsSchema),
    questions: z.array(SurveyQuestionAnalyticsSchema),
    responses: z.array(SurveyResponseRecordSchema),
  })
  .passthrough();

export const SurveyAnalyticsResponseSchema = z
  .object({
    generatedAt: z.string(),
    scope: SurveyScopeSchema,
    filters: SurveyAnalyticsFiltersSchema,
    scopeCounts: SurveyScopeCountsSchema,
    availableSurveys: z.array(AvailableSurveySchema),
    availableVersions: z.array(z.string()),
    totalResponses: z.number(),
    surveys: z.array(SurveyGroupAnalyticsSchema),
  })
  .passthrough();

export type RunResult = z.infer<typeof RunResultSchema>;
export type Challenge = z.infer<typeof ChallengeSchema>;
export type MapDiffCell = z.infer<typeof MapDiffCellSchema>;
export type SolverMetrics = z.infer<typeof SolverMetricsSchema>;
export type PlayAttempt = z.infer<typeof PlayAttemptSchema>;
export type DraftRegeneration = z.infer<typeof DraftRegenerationSchema>;
export type NodeEntry = z.infer<typeof NodeEntrySchema>;
export type FlowCard = z.infer<typeof FlowCardSchema>;
export type CoCreationFlowEvent = z.infer<typeof CoCreationFlowEventSchema>;
export type MatchEvent = z.infer<typeof MatchEventSchema>;
export type MatchPlayer = z.infer<typeof MatchPlayerSchema>;
export type MatchSurveyRecord = z.infer<typeof MatchSurveyRecordSchema>;
export type MatchRecord = z.infer<typeof MatchRecordSchema>;
export type MatchmakingSummary = z.infer<typeof MatchmakingSummarySchema>;
export type MatchmakingRecordsResponse = z.infer<
  typeof MatchmakingRecordsResponseSchema
>;
export type MatchmakingRecordsPayload = MatchmakingRecordsResponse;
export type MutationResponse = z.infer<typeof MutationResponseSchema>;

export type SurveyScope = z.infer<typeof SurveyScopeSchema>;
export type AvailableSurvey = z.infer<typeof AvailableSurveySchema>;
export type SurveyKpi = z.infer<typeof SurveyKpiSchema>;
export type SurveyLikertSummary = z.infer<typeof SurveyLikertSummarySchema>;
export type SingleChoiceOptionAnalytics = z.infer<
  typeof SingleChoiceOptionAnalyticsSchema
>;
export type SingleChoiceQuestionAnalytics = z.infer<
  typeof SingleChoiceQuestionAnalyticsSchema
>;
export type LikertBucketAnalytics = z.infer<typeof LikertBucketAnalyticsSchema>;
export type LikertStats = z.infer<typeof LikertStatsSchema>;
export type Likert5QuestionAnalytics = z.infer<
  typeof Likert5QuestionAnalyticsSchema
>;
export type TextareaEntry = z.infer<typeof TextareaEntrySchema>;
export type TextareaQuestionAnalytics = z.infer<
  typeof TextareaQuestionAnalyticsSchema
>;
export type SurveyQuestionAnalytics = z.infer<
  typeof SurveyQuestionAnalyticsSchema
>;
export type SurveySectionAnalytics = z.infer<
  typeof SurveySectionAnalyticsSchema
>;
export type SurveyResponseRecord = z.infer<typeof SurveyResponseRecordSchema>;
export type SurveyGroupAnalytics = z.infer<typeof SurveyGroupAnalyticsSchema>;
export type SurveyAnalyticsResponse = z.infer<
  typeof SurveyAnalyticsResponseSchema
>;

export type DashboardTab = "matches" | "surveys";

export interface SurveyQueryParams {
  scope?: string;
  surveyId?: string;
  surveyVersion?: string;
}

export interface ParsedSurveyAnswer {
  sectionId: string;
  questionIndex: number | null;
  questionId: string;
  questionType: "single_choice" | "likert_5" | "textarea";
  questionTextZh: string | null;
  questionTextEn: string | null;
  optionId: string | null;
  optionIndex: number | null;
  optionTextZh: string | null;
  optionTextEn: string | null;
  score: number | null;
  textValue: string | null;
}

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
