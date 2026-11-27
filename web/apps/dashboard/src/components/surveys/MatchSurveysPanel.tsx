import type { JSX } from "preact";
import { useState } from "preact/hooks";
import {
  clean,
  compactId,
  formatBilingualText,
  formatLegacyPayloadEntries,
  formatSeconds,
  formatSurveyLabel,
  formatSurveySectionLabel,
  formatTimestamp,
  parseSurveyAnswersList,
} from "../../format";
import type {
  MatchRecord,
  MatchSurveyRecord,
  ParsedSurveyAnswer,
} from "../../types";

export interface MatchSurveysPanelProps {
  match: MatchRecord;
}

interface SurveyGroup {
  key: string;
  title: string | null;
  records: MatchSurveyRecord[];
}

interface MergedQuestionRow {
  questionId: string;
  sectionId: string;
  questionIndex: number | null;
  questionTextZh: string | null;
  questionTextEn: string | null;
  p1Answer: ParsedSurveyAnswer | null;
  p2Answer: ParsedSurveyAnswer | null;
}

function surveyKey(record: MatchSurveyRecord): string {
  return (
    clean(record.surveyId) ||
    clean(record.payload?.surveyId) ||
    "legacy"
  );
}

function surveyOrderWeight(key: string): number {
  if (key === "online_pre_match_survey") return 0;
  if (key === "online_post_match_survey") return 1;
  if (key === "legacy") return 3;
  return 2;
}

function groupMatchSurveys(
  surveys: readonly MatchSurveyRecord[],
): SurveyGroup[] {
  const byKey = new Map<string, SurveyGroup>();
  for (const record of surveys) {
    const key = surveyKey(record);
    const title = clean(record.payload?.surveyTitle) || null;
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, { key, title, records: [record] });
    } else {
      existing.records.push(record);
      if (!existing.title && title) {
        existing.title = title;
      }
    }
  }
  return Array.from(byKey.values()).sort((a, b) => {
    const weightDiff = surveyOrderWeight(a.key) - surveyOrderWeight(b.key);
    return weightDiff !== 0 ? weightDiff : a.key.localeCompare(b.key);
  });
}

function getRecordSubmittedAt(record: MatchSurveyRecord): string {
  return clean(record.payload?.submittedAt) || record.createdAt;
}

function getRecordDurationSeconds(record: MatchSurveyRecord): number | null {
  const raw = record.payload?.durationSeconds;
  if (typeof raw === "number" && Number.isFinite(raw)) {
    return raw;
  }
  return null;
}

function renderAnswerCell(answer: ParsedSurveyAnswer | null): JSX.Element {
  if (!answer) {
    return <span class="survey-missing-cell">No response</span>;
  }

  if (answer.questionType === "likert_5" && answer.score !== null) {
    const fillPct = Math.max(0, Math.min(100, (answer.score / 5) * 100));
    const label = formatBilingualText(
      answer.optionTextZh,
      answer.optionTextEn,
      "",
    );
    return (
      <div class="survey-likert-cell">
        <div class="likert-mini-row">
          <div class="likert-mini-bar" aria-hidden="true">
            <div class="likert-mini-fill" style={{ width: `${fillPct}%` }} />
          </div>
          <span class="survey-score-pill">{answer.score}/5</span>
        </div>
        {label ? <div class="survey-answer-label">{label}</div> : null}
      </div>
    );
  }

  if (answer.questionType === "textarea") {
    return (
      <blockquote class="survey-quote">
        {answer.textValue || "-"}
      </blockquote>
    );
  }

  const choiceLabel = formatBilingualText(
    answer.optionTextZh,
    answer.optionTextEn,
    answer.optionId || "-",
  );
  return <span class="survey-option-badge">{choiceLabel}</span>;
}

function buildSectionedQuestions(
  p1Answers: readonly ParsedSurveyAnswer[],
  p2Answers: readonly ParsedSurveyAnswer[],
): Array<{ sectionId: string; rows: MergedQuestionRow[] }> {
  const rowsByKey = new Map<string, MergedQuestionRow>();
  const sectionOrder: string[] = [];

  const absorb = (
    answers: readonly ParsedSurveyAnswer[],
    slot: 1 | 2,
  ): void => {
    for (const ans of answers) {
      if (!sectionOrder.includes(ans.sectionId)) {
        sectionOrder.push(ans.sectionId);
      }
      const key = `${ans.sectionId}::${ans.questionId}`;
      let row = rowsByKey.get(key);
      if (!row) {
        row = {
          questionId: ans.questionId,
          sectionId: ans.sectionId,
          questionIndex: ans.questionIndex,
          questionTextZh: ans.questionTextZh,
          questionTextEn: ans.questionTextEn,
          p1Answer: null,
          p2Answer: null,
        };
        rowsByKey.set(key, row);
      } else {
        if (row.questionIndex === null && ans.questionIndex !== null) {
          row.questionIndex = ans.questionIndex;
        }
        if (!row.questionTextZh && ans.questionTextZh) {
          row.questionTextZh = ans.questionTextZh;
        }
        if (!row.questionTextEn && ans.questionTextEn) {
          row.questionTextEn = ans.questionTextEn;
        }
      }
      if (slot === 1 && row.p1Answer === null) {
        row.p1Answer = ans;
      } else if (slot === 2 && row.p2Answer === null) {
        row.p2Answer = ans;
      }
    }
  };

  absorb(p1Answers, 1);
  absorb(p2Answers, 2);

  return sectionOrder.map((sectionId) => {
    const rows = Array.from(rowsByKey.values())
      .filter((row) => row.sectionId === sectionId)
      .sort((a, b) => {
        const aIdx = a.questionIndex ?? Number.MAX_SAFE_INTEGER;
        const bIdx = b.questionIndex ?? Number.MAX_SAFE_INTEGER;
        return aIdx - bIdx;
      });
    return { sectionId, rows };
  });
}

function LegacySurveyPreview({
  records,
}: {
  records: readonly MatchSurveyRecord[];
}): JSX.Element {
  return (
    <div class="survey-legacy-grid">
      {records.map((rec) => {
        const entries = formatLegacyPayloadEntries(rec.payload);
        const slotLabel = rec.playerSlot ? `Player ${rec.playerSlot}` : "Unassigned slot";
        return (
          <article class="survey-legacy-card" key={rec.recordId}>
            <div class="survey-legacy-card-head">
              <strong>
                {slotLabel} · Record #{rec.recordId}
              </strong>
              <span class="survey-badge survey-badge--legacy">Legacy payload</span>
            </div>
            <p class="survey-legacy-note">
              Structured <code>answers[]</code> were not recorded for this legacy submission.
            </p>
            <table class="delta-table">
              <tbody>
                {entries.map(([key, val]) => (
                  <tr key={key}>
                    <td>{key}</td>
                    <td>{val}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </article>
        );
      })}
    </div>
  );
}

export function MatchSurveysPanel({
  match,
}: MatchSurveysPanelProps): JSX.Element {
  const surveys = match.surveys ?? [];
  const groups = groupMatchSurveys(surveys);
  const [selectedSurveyKey, setSelectedSurveyKey] = useState("");

  const activeGroup =
    groups.find((group) => group.key === selectedSurveyKey) ?? groups[0] ?? null;

  const preCount = surveys.filter(
    (s) => surveyKey(s) === "online_pre_match_survey",
  ).length;
  const postCount = surveys.filter(
    (s) => surveyKey(s) === "online_post_match_survey",
  ).length;

  const renderPlayerCard = (playerNumber: 1 | 2): JSX.Element => {
    const player = (match.players ?? []).find(
      (p) => p.playerNumber === playerNumber,
    );
    const playerSurveys = surveys.filter((s) => s.playerSlot === playerNumber);
    const preRec = playerSurveys.find(
      (s) => surveyKey(s) === "online_pre_match_survey",
    );
    const postRec = playerSurveys.find(
      (s) => surveyKey(s) === "online_post_match_survey",
    );
    const extraRecs = playerSurveys.filter(
      (s) =>
        surveyKey(s) !== "online_pre_match_survey" &&
        surveyKey(s) !== "online_post_match_survey",
    );

    const statusItems: Array<{
      label: string;
      record: MatchSurveyRecord | undefined;
      key: string;
    }> = [
      { key: "pre", label: "Pre-Match · Online1", record: preRec },
      { key: "post", label: "Post-Match · Online2", record: postRec },
      ...extraRecs.map((rec) => ({
        key: `extra-${rec.recordId}`,
        label: formatSurveyLabel(
          clean(rec.surveyId) || clean(rec.payload?.surveyId),
          clean(rec.payload?.surveyTitle),
        ),
        record: rec,
      })),
    ];

    return (
      <article class="survey-player-card" key={playerNumber}>
        <div class="survey-player-card-head">
          <h3>Player {playerNumber}</h3>
          <span class="survey-session-chip">
            Session {compactId(player?.studySessionId)}
          </span>
        </div>
        <div class="survey-player-status-list">
          {statusItems.map((item) => (
            <div class="survey-player-status-row" key={item.key}>
              <div class="survey-player-status-top">
                <span class="survey-status-label">{item.label}</span>
                {item.record ? (
                  <span class="survey-badge survey-badge--ok">Submitted</span>
                ) : (
                  <span class="survey-badge survey-badge--missing">Missing</span>
                )}
              </div>
              {item.record ? (
                <div class="survey-player-status-meta">
                  <span>
                    Duration {formatSeconds(getRecordDurationSeconds(item.record))}
                  </span>
                  <span>
                    Submitted {formatTimestamp(getRecordSubmittedAt(item.record))}
                  </span>
                  <span>Matched via {item.record.matchedBy}</span>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </article>
    );
  };

  let comparisonBody: JSX.Element | null = null;
  if (activeGroup) {
    const p1Rec =
      activeGroup.records.find((rec) => rec.playerSlot === 1) ?? null;
    const p2Rec =
      activeGroup.records.find((rec) => rec.playerSlot === 2) ?? null;
    const p1Answers = p1Rec
      ? parseSurveyAnswersList(p1Rec.payload?.answers)
      : [];
    const p2Answers = p2Rec
      ? parseSurveyAnswersList(p2Rec.payload?.answers)
      : [];

    if (p1Answers.length === 0 && p2Answers.length === 0) {
      comparisonBody = <LegacySurveyPreview records={activeGroup.records} />;
    } else {
      const sections = buildSectionedQuestions(p1Answers, p2Answers);
      comparisonBody = (
        <div class="survey-sections-compare">
          {sections.map((section) => (
            <div class="survey-section-block" key={section.sectionId}>
              <div class="survey-section-header">
                <h4>{formatSurveySectionLabel(section.sectionId)}</h4>
                <span class="survey-section-count">
                  {section.rows.length}{" "}
                  {section.rows.length === 1 ? "question" : "questions"}
                </span>
              </div>

              <div class="survey-table-scroll">
                <table class="survey-compare-table">
                  <thead>
                    <tr>
                      <th>Question</th>
                      <th>Player 1</th>
                      <th>Player 2</th>
                    </tr>
                  </thead>
                  <tbody>
                    {section.rows.map((row) => (
                      <tr key={row.questionId}>
                        <td class="survey-question-cell">
                          <div class="survey-question-id">
                            {row.questionIndex !== null
                              ? `Q${row.questionIndex} · `
                              : ""}
                            <code>{row.questionId}</code>
                          </div>
                          {row.questionTextZh ? (
                            <div class="survey-question-zh">
                              {row.questionTextZh}
                            </div>
                          ) : null}
                          {row.questionTextEn ? (
                            <div class="survey-question-en">
                              {row.questionTextEn}
                            </div>
                          ) : null}
                        </td>
                        <td>{renderAnswerCell(row.p1Answer)}</td>
                        <td>{renderAnswerCell(row.p2Answer)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      );
    }
  }

  return (
    <section class="stone-panel match-surveys-panel" aria-label="Match surveys">
      <div class="stone-panel-header stone-panel-header--purple compact-header">
        <div>
          <p class="eyebrow">Questionnaires</p>
          <h2>Player Surveys</h2>
        </div>
        <div class="survey-header-pills">
          <span class="count-pill">{surveys.length} linked</span>
          <span class="count-pill">Pre {preCount}/2</span>
          <span class="count-pill">Post {postCount}/2</span>
        </div>
      </div>

      <div class="match-surveys-body">
        <div class="survey-player-status-grid">
          {renderPlayerCard(1)}
          {renderPlayerCard(2)}
        </div>

        {groups.length === 0 ? (
          <div class="compare-placeholder">
            No linked surveys recorded for this match yet.
          </div>
        ) : (
          <>
            <div
              class="survey-type-tabs"
              role="tablist"
              aria-label="Linked match survey types"
            >
              {groups.map((group) => {
                const isSelected = activeGroup?.key === group.key;
                return (
                  <button
                    type="button"
                    role="tab"
                    key={group.key}
                    aria-selected={isSelected ? "true" : "false"}
                    class={`inspector-tab${isSelected ? " selected" : ""}`}
                    onClick={() => setSelectedSurveyKey(group.key)}
                  >
                    <span>{formatSurveyLabel(group.key, group.title)}</span>
                    <span class="level-tab-status">
                      {group.records.length}/2
                    </span>
                  </button>
                );
              })}
            </div>

            {comparisonBody}
          </>
        )}
      </div>
    </section>
  );
}
