import type { JSX } from "preact";
import { useState } from "preact/hooks";
import {
  clean,
  compactId,
  formatBilingualText,
  formatLegacyPayloadEntries,
  formatSeconds,
  formatSurveySectionLabel,
  formatTimestamp,
  parseSurveyAnswersList,
} from "../../format";
import type { SurveyResponseRecord } from "../../types";

export interface SurveyResponsesTableProps {
  responses: readonly SurveyResponseRecord[];
  onJumpToMatch: (matchId: string) => void;
}

export function SurveyResponsesTable({
  responses,
  onJumpToMatch,
}: SurveyResponsesTableProps): JSX.Element {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});

  const toggleRow = (recordId: number) => {
    setExpanded((prev) => ({ ...prev, [recordId]: !prev[recordId] }));
  };

  return (
    <section class="survey-responses-section">
      <div class="survey-section-header">
        <h4>Individual Responses</h4>
        <span class="survey-section-count">
          {responses.length} {responses.length === 1 ? "row" : "rows"} · newest first
        </span>
      </div>

      {responses.length === 0 ? (
        <div class="compare-placeholder">No individual responses in this group.</div>
      ) : (
        <div class="survey-table-scroll">
          <table class="survey-responses-table">
            <thead>
              <tr>
                <th>Submitted</th>
                <th>Room / Source</th>
                <th>Player</th>
                <th>Version</th>
                <th>Duration</th>
                <th>Answers</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {responses.map((resp) => {
                const isExpanded = Boolean(expanded[resp.recordId]);
                const parsedAnswers = parseSurveyAnswersList(resp.answers);
                const legacyEntries =
                  parsedAnswers.length === 0
                    ? formatLegacyPayloadEntries(resp as unknown as Record<string, unknown>)
                    : [];

                return [
                  <tr
                    key={`row-${resp.recordId}`}
                    class={`survey-response-row${isExpanded ? " is-expanded" : ""}`}
                    onClick={() => toggleRow(resp.recordId)}
                  >
                    <td>
                      {formatTimestamp(resp.submittedAt || resp.createdAt)}
                    </td>
                    <td>
                      {resp.linkedActiveMatchId ? (
                        <button
                          type="button"
                          class="survey-room-jump"
                          onClick={(event) => {
                            event.stopPropagation();
                            onJumpToMatch(resp.linkedActiveMatchId!);
                          }}
                        >
                          ROOM {clean(resp.roomCode) || compactId(resp.linkedActiveMatchId)}
                        </button>
                      ) : resp.roomCode ? (
                        <code class="survey-unlinked-room">
                          ROOM {resp.roomCode}
                        </code>
                      ) : (
                        <span class="survey-source-badge">
                          {resp.entrySource === "standalone"
                            ? "Standalone"
                            : clean(resp.entrySource) || "Unlinked"}
                        </span>
                      )}
                    </td>
                    <td>{resp.playerSlot ? `P${resp.playerSlot}` : "-"}</td>
                    <td>{resp.surveyVersion || "-"}</td>
                    <td>{formatSeconds(resp.durationSeconds)}</td>
                    <td>{resp.answerCount}</td>
                    <td>
                      <button
                        type="button"
                        class="copy-id-button"
                        aria-expanded={isExpanded ? "true" : "false"}
                        onClick={(event) => {
                          event.stopPropagation();
                          toggleRow(resp.recordId);
                        }}
                      >
                        {isExpanded ? "Hide" : "Inspect"}
                      </button>
                    </td>
                  </tr>,
                  isExpanded ? (
                    <tr
                      key={`detail-${resp.recordId}`}
                      class="survey-response-detail-row"
                    >
                      <td colSpan={7}>
                        <div class="survey-response-detail-panel">
                          <div class="survey-response-meta-chips">
                            <span>Record #{resp.recordId}</span>
                            {resp.responseId ? (
                              <span>Response {compactId(resp.responseId)}</span>
                            ) : null}
                            {resp.studySessionId ? (
                              <span>
                                Session {compactId(resp.studySessionId)}
                              </span>
                            ) : null}
                            {resp.experimentCondition ? (
                              <span>Condition: {resp.experimentCondition}</span>
                            ) : null}
                            {resp.language ? (
                              <span>Lang: {resp.language}</span>
                            ) : null}
                          </div>

                          {parsedAnswers.length > 0 ? (
                            <div class="survey-response-answer-list">
                              {parsedAnswers.map((ans) => (
                                <div
                                  class="survey-response-answer-item"
                                  key={`${resp.recordId}-${ans.questionId}`}
                                >
                                  <div class="survey-response-answer-q">
                                    <span class="survey-q-code">
                                      {formatSurveySectionLabel(ans.sectionId)} ·{" "}
                                      {ans.questionIndex !== null
                                        ? `Q${ans.questionIndex} · `
                                        : ""}
                                      {ans.questionId}
                                    </span>
                                    <span>
                                      {formatBilingualText(
                                        ans.questionTextZh,
                                        ans.questionTextEn,
                                        ans.questionId,
                                      )}
                                    </span>
                                  </div>
                                  <div class="survey-response-answer-val">
                                    {ans.questionType === "likert_5" &&
                                    ans.score !== null ? (
                                      <span class="survey-score-pill">
                                        {ans.score}/5
                                        {formatBilingualText(
                                          ans.optionTextZh,
                                          ans.optionTextEn,
                                          "",
                                        )
                                          ? ` · ${formatBilingualText(
                                              ans.optionTextZh,
                                              ans.optionTextEn,
                                              "",
                                            )}`
                                          : ""}
                                      </span>
                                    ) : ans.questionType === "textarea" ? (
                                      <blockquote class="survey-quote">
                                        {ans.textValue || "-"}
                                      </blockquote>
                                    ) : (
                                      <span class="survey-option-badge">
                                        {formatBilingualText(
                                          ans.optionTextZh,
                                          ans.optionTextEn,
                                          ans.optionId || "-",
                                        )}
                                      </span>
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <table class="delta-table">
                              <tbody>
                                {legacyEntries.map(([key, val]) => (
                                  <tr key={key}>
                                    <td>{key}</td>
                                    <td>{val}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </div>
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
