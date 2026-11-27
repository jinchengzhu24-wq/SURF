import { NoticeBanner } from "@sokoban/ui-core";
import type { JSX } from "preact";
import {
  formatSeconds,
  formatStatNumber,
  formatSurveyLabel,
  formatSurveyScopeLabel,
  formatSurveySectionLabel,
  formatTimestamp,
} from "../../format";
import type { DashboardStore } from "../../store";
import type { SurveyGroupAnalytics, SurveyScope } from "../../types";
import { SurveyQuestionChart } from "./SurveyQuestionChart";
import { SurveyResponsesTable } from "./SurveyResponsesTable";

export interface SurveyAnalyticsViewProps {
  store: DashboardStore;
}

const SCOPES: readonly SurveyScope[] = [
  "active_matches",
  "formal",
  "standalone",
  "all",
];

function SurveyGroupPanel({
  survey,
  onJumpToMatch,
}: {
  survey: SurveyGroupAnalytics;
  onJumpToMatch: (matchId: string) => void;
}): JSX.Element {
  const likertSections = survey.sections.filter(
    (section) => section.likertSummary !== null,
  );

  return (
    <section class="survey-group-block" aria-label={survey.surveyId}>
      <div class="stone-panel survey-group-header-panel">
        <div class="stone-panel-header">
          <div>
            <p class="eyebrow">{survey.surveyId}</p>
            <h2>{formatSurveyLabel(survey.surveyId, survey.surveyTitle)}</h2>
          </div>
          <div class="survey-header-pills">
            {survey.kpi.versions.map((ver) => (
              <span class="count-pill" key={ver}>
                {ver}
              </span>
            ))}
            <span class="count-pill">{survey.kpi.responseCount} responses</span>
          </div>
        </div>
      </div>

      <section class="summary" aria-label="Survey summary KPIs">
        <div class="stat">
          <span>Valid Responses</span>
          <strong>{survey.kpi.responseCount}</strong>
          <small>matching current filter</small>
        </div>

        <div class="stat">
          <span>Linked Matches</span>
          <strong>{survey.kpi.linkedMatchCount}</strong>
          <small>active formal rooms</small>
        </div>

        <div class="stat">
          <span>Avg Duration</span>
          <strong>{formatSeconds(survey.kpi.avgDurationSeconds)}</strong>
          <small>Median {formatSeconds(survey.kpi.medianDurationSeconds)}</small>
        </div>

        <div class="stat">
          <span>Latest Submission</span>
          <strong class="stat-timestamp">
            {formatTimestamp(survey.kpi.latestSubmittedAt)}
          </strong>
          <small>most recent submission</small>
        </div>
      </section>

      {likertSections.length > 0 ? (
        <section class="stone-panel survey-likert-strip-panel">
          <div class="stone-panel-header stone-panel-header--purple compact-header">
            <div>
              <p class="eyebrow">Section summary</p>
              <h2>Section Likert Averages</h2>
            </div>
            <span class="count-pill">Raw scores (unreversed)</span>
          </div>

          <div class="survey-likert-strip-grid">
            {likertSections.map((section) => {
              const summary = section.likertSummary!;
              const barWidth = Math.max(
                0,
                Math.min(100, (summary.mean / 5) * 100),
              );
              return (
                <article class="survey-likert-strip-card" key={section.sectionId}>
                  <div class="survey-likert-strip-top">
                    <h3>{formatSurveySectionLabel(section.sectionId)}</h3>
                    <span class="survey-raw-note">{summary.rawScoresNote}</span>
                  </div>

                  <div class="likert-mini-row">
                    <div class="likert-mini-bar" aria-hidden="true">
                      <div
                        class="likert-mini-fill"
                        style={{ width: `${barWidth}%` }}
                      />
                    </div>
                    <span class="survey-score-pill">
                      {formatStatNumber(summary.mean)} / 5
                    </span>
                  </div>

                  <div class="survey-likert-strip-stats">
                    <span>
                      Mean <strong>{formatStatNumber(summary.mean)}</strong>
                    </span>
                    <span>
                      SD <strong>{formatStatNumber(summary.sd)}</strong>
                    </span>
                    <span>
                      Median <strong>{summary.median}</strong>
                    </span>
                    <span>
                      n=<strong>{summary.scoreCount}</strong>
                    </span>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      {survey.sections.map((section) => (
        <section class="stone-panel survey-section-panel" key={section.sectionId}>
          <div class="stone-panel-header compact-header">
            <div>
              <p class="eyebrow">Question breakdown</p>
              <h2>{formatSurveySectionLabel(section.sectionId)}</h2>
            </div>
            <span class="count-pill">
              {section.questionCount}{" "}
              {section.questionCount === 1 ? "question" : "questions"}
            </span>
          </div>

          <div class="survey-questions-grid">
            {section.questions.map((question) => (
              <SurveyQuestionChart
                key={question.questionId}
                question={question}
                onJumpToMatch={onJumpToMatch}
              />
            ))}
          </div>
        </section>
      ))}

      <section class="stone-panel survey-responses-panel">
        <div class="stone-panel-header compact-header">
          <div>
            <p class="eyebrow">Response log</p>
            <h2>Submitted Responses</h2>
          </div>
          <span class="count-pill">{survey.responses.length} total</span>
        </div>

        <div class="survey-responses-body">
          <SurveyResponsesTable
            responses={survey.responses}
            onJumpToMatch={onJumpToMatch}
          />
        </div>
      </section>
    </section>
  );
}

export function SurveyAnalyticsView({
  store,
}: SurveyAnalyticsViewProps): JSX.Element {
  const analytics = store.surveyAnalytics.value;
  const isLoading = store.surveyLoading.value;
  const error = store.surveyError.value;
  const activeScope = store.surveyScope.value;
  const surveyIdFilter = store.surveyIdFilter.value;
  const surveyVersionFilter = store.surveyVersionFilter.value;

  const csvHref = store.api.exportSurveysCsvUrl({
    scope: activeScope,
    surveyId: surveyIdFilter,
    surveyVersion: surveyVersionFilter,
  });

  const availableSurveys = analytics?.availableSurveys ?? [];
  const availableVersions = analytics?.availableVersions ?? [];
  const scopeCounts = analytics?.scopeCounts;

  return (
    <div class="survey-analytics-view">
      <section class="stone-panel survey-filter-bar" aria-label="Survey filters">
        <div class="survey-filter-row">
          <div class="survey-filter-group">
            <span class="survey-filter-label">Survey</span>
            <div class="survey-pill-group" role="group" aria-label="Survey filter">
              <button
                type="button"
                class={`inspector-tab${surveyIdFilter === "" ? " selected" : ""}`}
                aria-pressed={surveyIdFilter === "" ? "true" : "false"}
                onClick={() => store.setSurveyIdFilter("")}
              >
                <span>All Surveys</span>
                {analytics ? (
                  <span class="level-tab-status">
                    {availableSurveys.reduce((acc, s) => acc + s.responseCount, 0)}
                  </span>
                ) : null}
              </button>

              {availableSurveys.map((avail) => {
                const isSelected = surveyIdFilter === avail.surveyId;
                return (
                  <button
                    type="button"
                    key={avail.surveyId}
                    class={`inspector-tab${isSelected ? " selected" : ""}`}
                    aria-pressed={isSelected ? "true" : "false"}
                    onClick={() => store.setSurveyIdFilter(avail.surveyId)}
                  >
                    <span>
                      {formatSurveyLabel(avail.surveyId, avail.surveyTitle)}
                    </span>
                    <span class="level-tab-status">{avail.responseCount}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div class="survey-filter-controls">
            <label class="survey-version-field">
              <span>Version</span>
              <select
                aria-label="Survey version filter"
                value={surveyVersionFilter}
                onChange={(event) =>
                  store.setSurveyVersionFilter(
                    (event.currentTarget as HTMLSelectElement).value,
                  )
                }
              >
                <option value="">All versions</option>
                {availableVersions.map((ver) => (
                  <option key={ver} value={ver}>
                    {ver}
                  </option>
                ))}
              </select>
            </label>

            <a
              class="pixel-btn pixel-btn--gold survey-csv-export"
              href={csvHref}
              download=""
            >
              Export Surveys (CSV)
            </a>
          </div>
        </div>

        <div class="survey-filter-row">
          <div class="survey-filter-group">
            <span class="survey-filter-label">Scope</span>
            <div class="survey-pill-group" role="group" aria-label="Scope filter">
              {SCOPES.map((scope) => {
                const isSelected = activeScope === scope;
                return (
                  <button
                    type="button"
                    key={scope}
                    class={`level-tab${isSelected ? " selected" : ""}`}
                    aria-pressed={isSelected ? "true" : "false"}
                    onClick={() => store.setSurveyScope(scope)}
                  >
                    <span>{formatSurveyScopeLabel(scope)}</span>
                    {scopeCounts ? (
                      <span class="level-tab-status">{scopeCounts[scope]}</span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </section>

      {error ? (
        <NoticeBanner
          message={`Failed to load survey analytics: ${error}`}
          variant="error"
          className="dashboard-notice"
          actionLabel="Retry"
          onAction={() => void store.loadSurveyAnalytics(true)}
        />
      ) : null}

      {isLoading && !analytics ? (
        <section class="stone-panel empty-state">
          Loading survey analytics...
        </section>
      ) : analytics && analytics.surveys.length === 0 ? (
        <section class="stone-panel empty-state">
          <p>
            No survey responses found for scope{" "}
            <strong>{formatSurveyScopeLabel(activeScope)}</strong>
            {surveyIdFilter ? ` · survey ${surveyIdFilter}` : ""}
            {surveyVersionFilter ? ` · version ${surveyVersionFilter}` : ""}.
          </p>
          {surveyIdFilter || surveyVersionFilter ? (
            <button
              type="button"
              class="pixel-btn pixel-btn--secondary"
              onClick={() => store.resetSurveyFilters()}
            >
              Clear survey filters
            </button>
          ) : null}
        </section>
      ) : analytics ? (
        <div class="survey-groups-stack">
          {analytics.surveys.map((survey) => (
            <SurveyGroupPanel
              key={survey.surveyId}
              survey={survey}
              onJumpToMatch={(matchId) => store.jumpToMatch(matchId)}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
