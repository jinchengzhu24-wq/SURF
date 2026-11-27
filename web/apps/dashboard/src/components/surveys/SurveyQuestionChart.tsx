import type { JSX } from "preact";
import {
  compactId,
  formatBilingualText,
  formatStatNumber,
  formatTimestamp,
} from "../../format";
import type { SurveyQuestionAnalytics } from "../../types";

export interface SurveyQuestionChartProps {
  question: SurveyQuestionAnalytics;
  onJumpToMatch: (matchId: string) => void;
}

const QUESTION_TYPE_LABELS: Record<
  SurveyQuestionAnalytics["questionType"],
  string
> = {
  single_choice: "Single Choice",
  likert_5: "Likert 1–5",
  textarea: "Open-Ended",
};

export function SurveyQuestionChart({
  question,
  onJumpToMatch,
}: SurveyQuestionChartProps): JSX.Element {
  return (
    <article class="survey-question-card">
      <header class="survey-question-card-head">
        <div class="survey-question-meta">
          <span class="survey-q-code">
            {question.questionIndex !== null ? `Q${question.questionIndex} · ` : ""}
            {question.questionId}
          </span>
          <span class="survey-q-type-badge">
            {QUESTION_TYPE_LABELS[question.questionType]}
          </span>
        </div>

        <span class="survey-rate-pill">
          {question.answeredCount}/{question.totalResponses} ({question.responseRate}%)
        </span>
      </header>

      <div class="survey-question-prompts">
        {question.questionTextZh ? (
          <div class="survey-question-zh">{question.questionTextZh}</div>
        ) : null}
        {question.questionTextEn ? (
          <div class="survey-question-en">{question.questionTextEn}</div>
        ) : null}
        {!question.questionTextZh && !question.questionTextEn ? (
          <div class="survey-question-en">{question.questionId}</div>
        ) : null}
      </div>

      {question.questionType === "single_choice" ? (
        <div class="survey-table-scroll">
          <table class="survey-dist-table">
            <thead>
              <tr>
                <th>Option</th>
                <th>Count</th>
                <th>%</th>
                <th>Distribution</th>
              </tr>
            </thead>
            <tbody>
              {question.options.length === 0 ? (
                <tr>
                  <td colSpan={4} class="survey-missing-cell">
                    No option selections recorded.
                  </td>
                </tr>
              ) : (
                question.options.map((opt) => (
                  <tr key={opt.optionId}>
                    <td>
                      {formatBilingualText(
                        opt.optionTextZh,
                        opt.optionTextEn,
                        opt.optionId,
                      )}
                    </td>
                    <td class="num-col">{opt.count}</td>
                    <td class="num-col">{opt.percentage}%</td>
                    <td class="bar-col">
                      <div class="survey-bar-track" aria-hidden="true">
                        <div
                          class="survey-bar-fill"
                          style={{
                            width: `${Math.max(0, Math.min(100, opt.percentage))}%`,
                          }}
                        />
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      ) : null}

      {question.questionType === "likert_5" ? (
        <div class="survey-likert-analytics">
          <div class="survey-stats-chips">
            <span class="survey-stat-chip">
              Mean: <strong>{formatStatNumber(question.stats.mean)}</strong>
            </span>
            <span class="survey-stat-chip">
              SD: <strong>{formatStatNumber(question.stats.sd)}</strong>
            </span>
            <span class="survey-stat-chip">
              Median:{" "}
              <strong>
                {question.stats.median === null
                  ? "—"
                  : String(question.stats.median)}
              </strong>
            </span>
            <span class="survey-stat-chip">
              Answered: <strong>{question.answeredCount}</strong>
            </span>
          </div>

          <div class="likert-buckets">
            {question.buckets.map((bucket) => {
              const anchor = formatBilingualText(
                bucket.optionTextZh,
                bucket.optionTextEn,
                "",
              );
              return (
                <div class="likert-bucket-row" key={bucket.score}>
                  <span class="likert-bucket-score">{bucket.score}</span>
                  <span class="likert-bucket-label" title={anchor || undefined}>
                    {anchor || `Score ${bucket.score}`}
                  </span>
                  <div class="survey-bar-track" aria-hidden="true">
                    <div
                      class="survey-bar-fill survey-bar-fill--likert"
                      style={{
                        width: `${Math.max(0, Math.min(100, bucket.percentage))}%`,
                      }}
                    />
                  </div>
                  <span class="likert-bucket-count">
                    {bucket.count} ({bucket.percentage}%)
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {question.questionType === "textarea" ? (
        <div class="survey-textarea-analytics">
          <div class="survey-textarea-header">
            <span class="survey-stat-chip">
              Responses: <strong>{question.nonEmptyCount}</strong>
            </span>
          </div>
          {question.entries.length === 0 ? (
            <div class="survey-missing-cell">No open-ended responses recorded.</div>
          ) : (
            <div class="survey-textarea-stream">
              {question.entries.map((entry) => (
                <article
                  class="survey-textarea-entry"
                  key={`${entry.recordId}-${entry.submittedAt ?? ""}`}
                >
                  <div class="survey-textarea-entry-meta">
                    {entry.linkedActiveMatchId && entry.roomCode ? (
                      <button
                        type="button"
                        class="survey-room-jump"
                        onClick={() => onJumpToMatch(entry.linkedActiveMatchId!)}
                      >
                        ROOM {entry.roomCode}
                        {entry.playerSlot ? ` · P${entry.playerSlot}` : ""}
                      </button>
                    ) : entry.roomCode ? (
                      <span class="survey-source-badge">
                        ROOM {entry.roomCode}
                        {entry.playerSlot ? ` · P${entry.playerSlot}` : ""}
                      </span>
                    ) : entry.entrySource === "standalone" ? (
                      <span class="survey-source-badge">Standalone</span>
                    ) : (
                      <span class="survey-source-badge">
                        {entry.studySessionId
                          ? `Session ${compactId(entry.studySessionId)}`
                          : `Record #${entry.recordId}`}
                      </span>
                    )}
                    <span class="survey-entry-time">
                      {formatTimestamp(entry.submittedAt)}
                    </span>
                  </div>
                  <blockquote class="survey-quote">{entry.text}</blockquote>
                </article>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </article>
  );
}
