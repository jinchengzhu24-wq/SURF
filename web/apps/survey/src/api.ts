import { fetchJson } from "@sokoban/ui-core";
import { z } from "zod";
import type { EntrySource } from "./surveyContext";
import type { DisplayLanguage, SurveyAnswerRecord } from "./surveyDefinitions";

export const SURVEY_SUBMIT_PATH = "/record-survey-response";

export interface SurveySubmissionPayload {
  eventType: "survey-response";
  surveyId: string;
  surveyVersion: string;
  surveyTitle: string;
  entrySource: EntrySource;
  responseId: string;
  studySessionId: string;
  sessionId: string;
  matchId: string;
  roomCode: string;
  playerNumber: number;
  participantId?: string;
  experimentCondition?: string;
  language: DisplayLanguage;
  submittedAt: string;
  durationSeconds: number;
  answers: SurveyAnswerRecord[];
}

export const SurveySubmitResponseSchema = z.object({
  status: z.string(),
  id: z.number().optional(),
  recordType: z.string().optional(),
  eventType: z.string().optional(),
  deduplicated: z.boolean().optional(),
});

export type SurveySubmitResponse = z.infer<typeof SurveySubmitResponseSchema>;

export interface SubmitSurveyOptions {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export async function submitSurveyResponse(
  payload: SurveySubmissionPayload,
  options: SubmitSurveyOptions = {},
): Promise<SurveySubmitResponse> {
  return fetchJson<SurveySubmitResponse>(SURVEY_SUBMIT_PATH, {
    method: "POST",
    body: payload,
    fetchImpl: options.fetchImpl,
    timeoutMs: options.timeoutMs,
    schema: SurveySubmitResponseSchema,
  });
}
