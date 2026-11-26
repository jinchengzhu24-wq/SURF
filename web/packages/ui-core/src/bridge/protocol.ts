import { z } from "zod";

export const PROTOCOL_VERSION = 5 as const;

const NonEmptyString = z.string().min(1);
const ProtocolVersionSchema = z.number().int().nonnegative();

/** 1. Host -> Lab: initialize embedded workspace handshake. */
export const HostInitEventSchema = z.object({
  type: z.literal("sokoban:cocreation-host-init"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
});

/** 2. Lab -> Host: confirm lab iframe is ready for the session. */
export const LabReadyEventSchema = z.object({
  type: z.literal("sokoban:cocreation-lab-ready"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
});

/** 3. Lab -> Host: switch visible view back to Unity canvas. */
export const ShowUnityEventSchema = z.object({
  type: z.literal("sokoban:cocreation-show-unity"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
  requestId: z.string().optional(),
});

/** 4. Lab -> Host: reveal the embedded Co-Creation Lab iframe. */
export const ShowLabEventSchema = z.object({
  type: z.literal("sokoban:cocreation-show-lab"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
  requestId: z.string().optional(),
});

/** 5. Lab -> Host: request launching a Stage in the Unity player. */
export const PlayRequestEventSchema = z.object({
  type: z.literal("sokoban:cocreation-play-request"),
  requestId: NonEmptyString,
  sessionId: NonEmptyString,
  playUrl: NonEmptyString,
  protocolVersion: ProtocolVersionSchema,
});

/** 6. Host -> Lab: acknowledge whether Unity accepted the play request. */
export const PlayAckEventSchema = z.object({
  type: z.literal("sokoban:cocreation-play-ack"),
  requestId: NonEmptyString,
  accepted: z.boolean(),
  protocolVersion: ProtocolVersionSchema,
});

/** 7. Host -> Lab: notify lab that Unity play finished or exited. */
export const PlayReturnEventSchema = z.object({
  type: z.literal("sokoban:cocreation-play-return"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
  status: z.string(),
});

/** 8. Lab -> Host: prepare Unity bridge for draft regeneration. */
export const DraftRegeneratePrepareEventSchema = z.object({
  type: z.literal("sokoban:cocreation-draft-regenerate-prepare"),
  requestId: NonEmptyString,
  sessionId: NonEmptyString,
  protocolVersion: ProtocolVersionSchema,
});

/** 9. Host -> Lab: acknowledge draft regeneration preparation. */
export const DraftRegeneratePrepareAckEventSchema = z.object({
  type: z.literal("sokoban:cocreation-draft-regenerate-prepare-ack"),
  requestId: NonEmptyString,
  sessionId: NonEmptyString,
  accepted: z.boolean(),
  reason: z.string(),
  protocolVersion: ProtocolVersionSchema,
});

/** 10. Lab -> Host: execute draft regeneration for a prepared request. */
export const DraftRegenerateRequestEventSchema = z.object({
  type: z.literal("sokoban:cocreation-draft-regenerate-request"),
  requestId: NonEmptyString,
  prepareRequestId: NonEmptyString,
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
  regenerationRequestId: NonEmptyString,
});

/** 11. Host -> Lab: acknowledge draft regeneration dispatch. */
export const DraftRegenerateAckEventSchema = z.object({
  type: z.literal("sokoban:cocreation-draft-regenerate-ack"),
  requestId: NonEmptyString,
  sessionId: NonEmptyString,
  regenerationRequestId: z.string(),
  accepted: z.boolean(),
  reason: z.string(),
  protocolVersion: ProtocolVersionSchema,
});

/** 12. Lab -> Host: abort a previously prepared draft regeneration request. */
export const DraftRegenerateAbortEventSchema = z.object({
  type: z.literal("sokoban:cocreation-draft-regenerate-abort"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
  prepareRequestId: NonEmptyString,
});

/** 13. Host -> Lab: report draft regeneration completion or failure from Unity. */
export const DraftRegenerateReturnEventSchema = z.object({
  type: z.literal("sokoban:cocreation-draft-regenerate-return"),
  protocolVersion: ProtocolVersionSchema,
  sessionId: NonEmptyString,
  requestId: z.string(),
  status: z.string(),
});

export const CoCreationBridgeEventSchema = z.discriminatedUnion("type", [
  HostInitEventSchema,
  LabReadyEventSchema,
  ShowUnityEventSchema,
  ShowLabEventSchema,
  PlayRequestEventSchema,
  PlayAckEventSchema,
  PlayReturnEventSchema,
  DraftRegeneratePrepareEventSchema,
  DraftRegeneratePrepareAckEventSchema,
  DraftRegenerateRequestEventSchema,
  DraftRegenerateAckEventSchema,
  DraftRegenerateAbortEventSchema,
  DraftRegenerateReturnEventSchema,
]);

export type HostInitEvent = z.infer<typeof HostInitEventSchema>;
export type LabReadyEvent = z.infer<typeof LabReadyEventSchema>;
export type ShowUnityEvent = z.infer<typeof ShowUnityEventSchema>;
export type ShowLabEvent = z.infer<typeof ShowLabEventSchema>;
export type PlayRequestEvent = z.infer<typeof PlayRequestEventSchema>;
export type PlayAckEvent = z.infer<typeof PlayAckEventSchema>;
export type PlayReturnEvent = z.infer<typeof PlayReturnEventSchema>;
export type DraftRegeneratePrepareEvent = z.infer<typeof DraftRegeneratePrepareEventSchema>;
export type DraftRegeneratePrepareAckEvent = z.infer<typeof DraftRegeneratePrepareAckEventSchema>;
export type DraftRegenerateRequestEvent = z.infer<typeof DraftRegenerateRequestEventSchema>;
export type DraftRegenerateAckEvent = z.infer<typeof DraftRegenerateAckEventSchema>;
export type DraftRegenerateAbortEvent = z.infer<typeof DraftRegenerateAbortEventSchema>;
export type DraftRegenerateReturnEvent = z.infer<typeof DraftRegenerateReturnEventSchema>;
export type CoCreationBridgeEvent = z.infer<typeof CoCreationBridgeEventSchema>;
export type CoCreationBridgeEventType = CoCreationBridgeEvent["type"];

export const COCREATION_BRIDGE_EVENT_TYPES = [
  "sokoban:cocreation-host-init",
  "sokoban:cocreation-lab-ready",
  "sokoban:cocreation-show-unity",
  "sokoban:cocreation-show-lab",
  "sokoban:cocreation-play-request",
  "sokoban:cocreation-play-ack",
  "sokoban:cocreation-play-return",
  "sokoban:cocreation-draft-regenerate-prepare",
  "sokoban:cocreation-draft-regenerate-prepare-ack",
  "sokoban:cocreation-draft-regenerate-request",
  "sokoban:cocreation-draft-regenerate-ack",
  "sokoban:cocreation-draft-regenerate-abort",
  "sokoban:cocreation-draft-regenerate-return",
] as const;

export function parseBridgeEvent(data: unknown): CoCreationBridgeEvent | null {
  const result = CoCreationBridgeEventSchema.safeParse(data);
  return result.success ? result.data : null;
}

export function isCoCreationBridgeEvent(data: unknown): data is CoCreationBridgeEvent {
  return CoCreationBridgeEventSchema.safeParse(data).success;
}

export function isHostInitEvent(data: unknown): data is HostInitEvent {
  return HostInitEventSchema.safeParse(data).success;
}

export function isLabReadyEvent(data: unknown): data is LabReadyEvent {
  return LabReadyEventSchema.safeParse(data).success;
}

export function isShowUnityEvent(data: unknown): data is ShowUnityEvent {
  return ShowUnityEventSchema.safeParse(data).success;
}

export function isShowLabEvent(data: unknown): data is ShowLabEvent {
  return ShowLabEventSchema.safeParse(data).success;
}

export function isPlayRequestEvent(data: unknown): data is PlayRequestEvent {
  return PlayRequestEventSchema.safeParse(data).success;
}

export function isPlayAckEvent(data: unknown): data is PlayAckEvent {
  return PlayAckEventSchema.safeParse(data).success;
}

export function isPlayReturnEvent(data: unknown): data is PlayReturnEvent {
  return PlayReturnEventSchema.safeParse(data).success;
}

export function isDraftRegeneratePrepareEvent(
  data: unknown,
): data is DraftRegeneratePrepareEvent {
  return DraftRegeneratePrepareEventSchema.safeParse(data).success;
}

export function isDraftRegeneratePrepareAckEvent(
  data: unknown,
): data is DraftRegeneratePrepareAckEvent {
  return DraftRegeneratePrepareAckEventSchema.safeParse(data).success;
}

export function isDraftRegenerateRequestEvent(
  data: unknown,
): data is DraftRegenerateRequestEvent {
  return DraftRegenerateRequestEventSchema.safeParse(data).success;
}

export function isDraftRegenerateAckEvent(
  data: unknown,
): data is DraftRegenerateAckEvent {
  return DraftRegenerateAckEventSchema.safeParse(data).success;
}

export function isDraftRegenerateAbortEvent(
  data: unknown,
): data is DraftRegenerateAbortEvent {
  return DraftRegenerateAbortEventSchema.safeParse(data).success;
}

export function isDraftRegenerateReturnEvent(
  data: unknown,
): data is DraftRegenerateReturnEvent {
  return DraftRegenerateReturnEventSchema.safeParse(data).success;
}

export type BridgeRejectionReason =
  | ""
  | "protocol_mismatch"
  | "unity_not_ready"
  | "receiver_not_ready"
  | "session_mismatch"
  | "prepare_missing"
  | "prepare_expired"
  | "request_mismatch";

export interface PrepareRejectionState {
  protocolVersion: number;
  activeProtocolVersion: number;
  activeSessionId: string;
  unityReady: boolean;
  receiverReady: boolean;
}

export interface RequestRejectionState extends PrepareRejectionState {
  source: MessageEventSource | null;
  origin: string;
}

export interface PreparedDraftRegeneration {
  requestId: string;
  sessionId: string;
  source: MessageEventSource | null;
  origin: string;
  expiresAt: number;
}

export function prepareRejectionReason(
  state: PrepareRejectionState,
  message: { protocolVersion?: number; sessionId?: string } | null | undefined,
): BridgeRejectionReason {
  if (
    !message ||
    message.protocolVersion !== state.protocolVersion ||
    (state.activeProtocolVersion > 0 &&
      state.activeProtocolVersion !== state.protocolVersion)
  ) {
    return "protocol_mismatch";
  }
  if (!state.unityReady) return "unity_not_ready";
  if (!state.receiverReady) return "receiver_not_ready";
  if (!message.sessionId || message.sessionId !== state.activeSessionId) {
    return "session_mismatch";
  }
  return "";
}

export function requestRejectionReason(
  state: RequestRejectionState,
  message: {
    protocolVersion?: number;
    sessionId?: string;
    prepareRequestId?: string;
    regenerationRequestId?: string;
  } | null | undefined,
  preparedRequest: PreparedDraftRegeneration | null | undefined,
  now: number,
): BridgeRejectionReason {
  const prepareReason = prepareRejectionReason(state, message);
  if (prepareReason) return prepareReason;
  if (!preparedRequest) return "prepare_missing";
  if (preparedRequest.expiresAt < now) return "prepare_expired";
  if (
    !message ||
    preparedRequest.requestId !== message.prepareRequestId ||
    preparedRequest.sessionId !== message.sessionId ||
    preparedRequest.source !== state.source ||
    preparedRequest.origin !== state.origin
  ) {
    return "request_mismatch";
  }
  if (!message.regenerationRequestId) return "request_mismatch";
  return "";
}

export function bridgeStateChanged(
  previousSessionId: string | null | undefined,
  previousProtocolVersion: number | null | undefined,
  sessionId: string | null | undefined,
  protocolVersion: number | null | undefined,
): boolean {
  return (
    (previousSessionId || "") !== (sessionId || "") ||
    Number(previousProtocolVersion || 0) !== Number(protocolVersion || 0)
  );
}

export function shouldHandleSource(isKnownSource: unknown): boolean {
  return isKnownSource === true;
}
