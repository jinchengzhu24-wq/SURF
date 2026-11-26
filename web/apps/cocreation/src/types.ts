import type { SokobanTile } from "@sokoban/ui-core";

export type Language = "en" | "zh-CN";

export type SessionStatus =
  | "active"
  | "awaiting_intention"
  | "completed"
  | "finalized"
  | "locked";

export type InitialDraftMethod =
  | "partial_completion"
  | "description_generation"
  | "algorithm_demo";

export type StageSource =
  | "initial"
  | "human_edit"
  | "llm_accepted"
  | "ai_proposal"
  | "restored";

export interface TileDiff {
  x: number;
  y: number;
  before: SokobanTile;
  after: SokobanTile;
}

export interface ValidationSummary {
  solvable?: boolean;
  solutionSteps: number;
  solutionPushes: number;
  searchedStates: number;
}

export type PlayAttemptStatus =
  | "issued"
  | "bootstrapped"
  | "playing"
  | "started"
  | "completed"
  | "abandoned"
  | "interrupted"
  | "expired";

export interface PlayAttempt {
  attemptId: string;
  sessionId?: string | null;
  versionId?: string | null;
  status: PlayAttemptStatus;
  issuedAt?: string;
  loadedAt?: string | null;
  firstMoveAt?: string | null;
  finishedAt?: string | null;
  durationSeconds?: number;
  moveCount: number;
  pushCount: number;
  restartCount: number;
  minimumMoves?: number;
  minimumPushes?: number;
}

export interface StageVersion {
  versionId: string;
  sessionId?: string;
  stageNumber: number;
  parentVersionId?: string | null;
  source: StageSource;
  rows: string[];
  summary?: string;
  diff: TileDiff[];
  validation: ValidationSummary;
  playAttempts: PlayAttempt[];
  openingTurnId?: string | null;
  createdAt?: string;
}

export interface CoordinatePoint {
  row: number;
  column: number;
}

export interface CoordinateLink {
  text: string;
  from: CoordinatePoint;
  to: CoordinatePoint;
}

export type GuidanceCueType =
  | "manual_edit"
  | "question"
  | "revision"
  | "intent"
  | "warning"
  | "tradeoff"
  | "clarification";

export interface UiCue {
  type: GuidanceCueType;
  text: string;
}

export interface ProposalChangeItem {
  row: number;
  column: number;
  before: SokobanTile;
  after: SokobanTile;
}

export interface ProposalSolutionInfo {
  minimumMoves: number;
  pushesOnMinimumMoveSolution: number;
}

export interface ProposalPresentationV2 {
  schemaVersion: 2;
  summary?: string;
  rationale?: string;
  binaryCompact?: boolean;
  requestText?: string | string[];
  answerTexts?: string | string[];
  changes?: ProposalChangeItem[];
  verifiedActions?: string[];
  reason?: string;
  solution?: ProposalSolutionInfo;
}

export type RevisionCardAction =
  | "execute_revision"
  | "challenge_revision"
  | "alternative_revision";

export interface ProposalOffer {
  summary?: string;
  rationale?: string;
  availableActions?: RevisionCardAction[];
  proposalPresentation?: ProposalPresentationV2;
}

export type ProposalStateStatus =
  | "active"
  | "stale"
  | "already_satisfied"
  | "unbound"
  | "precondition_failed"
  | "disagreement_active"
  | "challenged";

export interface ProposalState {
  status: ProposalStateStatus;
  actionable?: boolean;
}

export interface BinaryChoice {
  choiceId: string;
  letter: string;
  label: string;
}

export interface BinaryQuestion {
  questionId: string;
  topicId: string;
  baseVersionId: string;
  question: string;
  note?: string;
  status: "pending" | "answered" | "cancelled" | "expired" | string;
  selectedChoiceId?: string | null;
  sourceLanguage?: Language;
  choices: BinaryChoice[];
}

export interface BinaryRetryAction {
  kind: "start" | "choice" | "cancel";
  baseVersionId: string;
  idempotencyKey: string;
  topicId?: string;
  questionId?: string;
  choiceId?: string;
  displayLanguage?: Language;
  content?: string;
  requestProposal?: boolean;
}

export interface BinaryDiscovery {
  active: boolean;
  status:
    | "inactive"
    | "clarifying"
    | "planning"
    | "retry_pending"
    | "revision_needed"
    | "proposal_ready"
    | "cancelled"
    | string;
  interactionMode?: "binary_choice" | string;
  topicId?: string;
  clarificationQuestionCount?: number;
  pendingQuestion?: BinaryQuestion | null;
  recordedUserTurnIds?: string[];
  retryable?: boolean;
  retryAction?: BinaryRetryAction | null;
}

export interface IntentConflictOption {
  hypothesisId: string;
  role: "new" | "existing";
  statement: string;
  evidenceText?: string;
  status?: "tentative" | "confirmed" | "rejected" | "superseded" | string;
}

export interface IntentConflictChoice {
  options: IntentConflictOption[];
  selectedHypothesisId?: string | null;
}

export interface IntentHypothesis {
  hypothesisId: string;
  status: "tentative" | "confirmed" | "rejected" | "superseded" | string;
  interactionMode: "full" | "modify_only" | "conflict_choice" | "resolved" | string;
  actionable: boolean;
  resolvedStatement?: string | null;
  conflictChoice?: IntentConflictChoice | null;
}

export interface Disagreement {
  status: "active" | "resolved" | string;
  displayCard?: boolean;
  subject?: string;
  phase?: "choice_pending" | string;
  challengeId?: string;
  userPosition?: string;
  aiPosition?: string;
  coreDisagreement?: string;
  nextQuestion?: string;
}

export interface ChallengeState {
  challengeId: string;
  status: "active" | "choice_pending" | "resolved" | string;
}

export interface TurnGuidance {
  move?: string | null;
  intentHypothesis?: string | null;
  intentConfidence?: "low" | "medium" | "high" | null;
  followUpQuestion?: string | null;
  proposalOffer?: ProposalOffer | null;
  proposalQuestion?: BinaryQuestion | null;
  disagreement?: Disagreement | null;
  discussionCardMode?: string | null;
  uiCues?: UiCue[];
  coordinateLinks?: CoordinateLink[];
  intentState?: IntentHypothesis | null;
  challengeState?: ChallengeState | null;
}

export interface TurnTranslation {
  body: string;
  guidance?: TurnGuidance;
}

export interface Turn {
  turnId: string;
  versionId: string;
  sequence: number;
  role: "user" | "assistant";
  content: string;
  language?: Language;
  requestId?: string | null;
  requestProposal?: boolean;
  guidance?: TurnGuidance | null;
  proposalState?: ProposalState | null;
  translations?: Partial<Record<Language, TurnTranslation>>;
  createdAt?: string;
}

export interface Proposal {
  proposalId: string;
  baseVersionId: string;
  assistantTurnId?: string | null;
  status: "pending" | "accepted" | "rejected" | "superseded" | string;
  summary?: string;
  proposedRows: string[];
  diff: TileDiff[];
  createdAt?: string;
}

export type DraftRegenerationStatus =
  | "pending"
  | "claimed"
  | "completed"
  | "failed"
  | "cancelled"
  | "timed_out";

export type DraftRegenerationFailureCode =
  | "generation_failed"
  | "llm_failed"
  | "validation_failed"
  | "unity_unavailable"
  | "blueprint_unavailable"
  | "generation_timed_out";

export interface DraftRegeneration {
  rows?: string[];
  generation?: number;
  mutable?: boolean;
  regenerationStatus?: DraftRegenerationStatus | null;
  regenerationRequestId?: string | null;
  regenerationRequestedAt?: string | null;
  regenerationUpdatedAt?: string | null;
  failureCode?: DraftRegenerationFailureCode | null;
}

export interface ComponentRecovery {
  recoveryId: string;
  baseVersionId: string;
  status: "pending" | "retry_pending" | "claimed" | "waiting_message" | "completed" | "failed" | string;
  nextAttemptKey: string;
  attemptsUsed: number;
  leaseUntil?: string | null;
}

export interface InteractionState {
  versionId?: string;
  revision?: number;
  proposalBlocked?: boolean;
  recoveries?: ComponentRecovery[];
}

export interface QuestionRecord {
  questionId: string;
  question: string;
  status: "unanswered" | "answered" | "ignored" | string;
  askedAtStageNumber?: number;
  answeredAtStageNumber?: number;
  ignoredAtStageNumber?: number;
}

export interface InclinationEvidence {
  stageNumber: number;
  text: string;
  kind?: string;
  detailedText?: string;
}

export interface DesignInclination {
  inclinationId?: string;
  statement: string;
  confirmedAtStageNumber?: number;
  evidenceTrail?: InclinationEvidence[];
}

export interface ProgressContext {
  versionId: string;
  questionRecords: QuestionRecord[];
  designInclinations: DesignInclination[];
}

export interface Assessment {
  versionId: string;
  assistantTurnId: string;
}

export interface SessionIntention {
  content: string;
  synced?: boolean;
}

export interface LastMessageFailure {
  messageKey: string;
  baseVersionId: string;
  code: string;
  committed: boolean;
  retryable: boolean;
  requestId?: string;
  details?: Record<string, unknown>;
}

export interface Session {
  sessionId: string;
  status: SessionStatus;
  language: Language;
  languageLocked: boolean;
  demoMode: boolean;
  initialDraftMethod: InitialDraftMethod;
  currentVersionId: string;
  finalVersionId?: string | null;
  matchId?: string | null;
  playerNumber?: number | null;
  deadlineStartedAt?: string | null;
  deadlineAt?: string | null;
  remainingSeconds?: number | null;
  deadlineExpired?: boolean;
  createdAt?: string;
  updatedAt?: string;
  versions: StageVersion[];
  turns: Turn[];
  proposals: Proposal[];
  assessments: Assessment[];
  progressContexts?: ProgressContext[];
  proposalFlowState?: BinaryDiscovery | null;
  interactionState?: InteractionState | null;
  draftPreview?: DraftRegeneration | null;
  intention?: SessionIntention | null;
  lastMessageFailure?: LastMessageFailure | null;
}

export type MessageAction =
  | "none"
  | "execute_revision"
  | "challenge_revision"
  | "alternative_revision"
  | "continue_challenge";

export interface PendingMessage {
  content: string;
  baseVersionId: string;
  idempotencyKey: string;
  requestProposal?: boolean;
  action?: MessageAction;
  sourceTurnId?: string;
  challengeId?: string;
  challengeChoice?: "ai" | "user" | "retain_current";
}

export interface PendingBinaryAction {
  kind: "choice" | "cancel";
  baseVersionId: string;
  topicId: string;
  idempotencyKey: string;
  questionId?: string;
  choiceId?: string;
  displayLanguage?: Language;
}

export interface PendingFinalize {
  sessionId: string;
  targetVersionId: string;
  rowsSignature: string | null;
  idempotencyKey: string;
}

export interface PendingIntention {
  sessionId: string;
  content: string;
  idempotencyKey: string;
}

export interface CoordinateLinkSelection {
  versionId: string;
  turnId: string;
  linkIndex: number;
  from: CoordinatePoint;
  to: CoordinatePoint;
}

export interface FeedbackResponse {
  outcome: string;
  session: Session;
}

export interface PlayAttemptResponse {
  attemptId: string;
  playUrl: string;
}

export interface DemoSessionResponse {
  sessionId: string;
  launchUrl: string;
}

export interface BrowserAccessResponse {
  sessionId: string;
}

export interface StructuredErrorLike {
  code?: string;
  message?: string;
  retryable?: boolean;
  status?: number;
  details?: Record<string, unknown> | null;
}

export interface ReplyFeedback {
  message: string;
  error: StructuredErrorLike | null;
  retryAction: (() => void | Promise<void>) | null;
  versionId: string;
}

export type LandingMode = "demo" | "demo-loading" | "demo-preview" | "demo-error" | "formal";
