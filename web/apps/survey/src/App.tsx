import { ApiError, NoticeBanner } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { submitSurveyResponse, type SurveySubmissionPayload } from "./api";
import { BilingualCopy, QuestionCard } from "./components/QuestionCard";
import { SurveyReceipt } from "./components/SurveyReceipt";
import {
  generateResponseId,
  parseSurveyLaunchContext,
  type SurveyLocationLike,
} from "./surveyContext";
import {
  SURVEY_DEFINITIONS,
  buildSurveyAnswers,
  findMissingRequiredQuestionIds,
  getVisibleQuestions,
  getVisibleSurveySections,
  isAnswerProvided,
  resolveSurveyDefinition,
  type AnswerValue,
  type DisplayLanguage,
  type SurveyDefinition,
} from "./surveyDefinitions";
import {
  readSubmissionReceipt,
  writeSubmissionReceipt,
  type SubmissionReceipt,
} from "./surveyStorage";

export interface SurveyWindowLike {
  history?: Pick<History, "pushState" | "replaceState">;
  close?: () => void;
  addEventListener?: Window["addEventListener"];
  removeEventListener?: Window["removeEventListener"];
}

export interface AppProps {
  definitions?: readonly SurveyDefinition[];
  surveyOverride?: SurveyDefinition;
  locationObj?: SurveyLocationLike;
  windowObj?: SurveyWindowLike;
  fetchImpl?: typeof fetch;
  storage?: Storage | null;
  nowMs?: () => number;
  idGenerator?: () => string;
}

const DEFAULT_NOW_MS = () =>
  typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();

export function buildSurveyUrlWithType(
  routeKey: string | null,
  locationObj?: SurveyLocationLike,
): string {
  const loc: SurveyLocationLike | undefined =
    locationObj ??
    (typeof globalThis.location !== "undefined"
      ? globalThis.location
      : undefined);
  const pathname = loc?.pathname || "/survey/";
  let search = "";
  if (typeof loc?.search === "string") {
    search = loc.search;
  } else if (typeof loc?.href === "string") {
    try {
      search = new URL(loc.href).search;
    } catch {
      search = "";
    }
  }
  const params = new URLSearchParams(search);
  params.delete("survey");
  params.delete("surveyId");
  if (routeKey) {
    params.set("type", routeKey);
  } else {
    params.delete("type");
  }
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
}

function getStandaloneNoticeText(language: DisplayLanguage): string {
  if (language === "zh") {
    return "独立测试模式：当前未携带 Unity 对战参数，提交记录将标记为 standalone，不会混入正式对战研究导出。";
  }
  if (language === "en") {
    return "Standalone mode: opened without Unity matchmaking parameters. Submissions are tagged as standalone and excluded from formal research exports.";
  }
  return "独立测试模式：未携带 Unity 对战参数，提交将标记为 standalone 且不混入正式研究导出。 / Standalone mode: submissions are tagged standalone and excluded from formal exports.";
}

function getValidationBannerText(
  missingCount: number,
  language: DisplayLanguage,
): string {
  if (language === "zh") {
    return `还有 ${missingCount} 道必填题尚未作答，请补全高亮题目后再提交。`;
  }
  if (language === "en") {
    return `${missingCount} required question${missingCount === 1 ? "" : "s"} still need an answer. Please complete the highlighted items.`;
  }
  return `还有 ${missingCount} 道必填题尚未作答，请补全高亮题目。 / ${missingCount} required question${missingCount === 1 ? "" : "s"} remaining — please complete the highlighted items.`;
}

function formatSubmitError(error: unknown, language: DisplayLanguage): string {
  const detail =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : "Unknown error";
  if (language === "zh") {
    return `提交失败（${detail}）。您的作答已保留，请检查网络后重试提交。`;
  }
  if (language === "en") {
    return `Submission failed (${detail}). Your answers are preserved—please check your connection and retry.`;
  }
  return `提交失败（${detail}），请重试。 / Submission failed (${detail}). Your answers are preserved—please retry.`;
}

export function App({
  definitions = SURVEY_DEFINITIONS,
  surveyOverride,
  locationObj,
  windowObj,
  fetchImpl,
  storage,
  nowMs = DEFAULT_NOW_MS,
  idGenerator = generateResponseId,
}: AppProps): JSX.Element {
  const resolvedWindow: SurveyWindowLike | undefined =
    windowObj ?? (typeof globalThis.window !== "undefined" ? globalThis.window : undefined);

  const launchContext = useMemo(
    () => parseSurveyLaunchContext(locationObj, definitions),
    [locationObj, definitions],
  );

  const [selectedSurveyId, setSelectedSurveyId] = useState<string | null>(() =>
    surveyOverride ? surveyOverride.id : launchContext.surveyId,
  );
  const [language, setLanguage] = useState<DisplayLanguage>(
    launchContext.initialLanguage,
  );
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>({});
  const [missingIds, setMissingIds] = useState<string[]>([]);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const activeSurvey = useMemo<SurveyDefinition | null>(() => {
    if (surveyOverride) {
      return surveyOverride;
    }
    return resolveSurveyDefinition(selectedSurveyId, definitions);
  }, [surveyOverride, selectedSurveyId, definitions]);

  const [receiptState, setReceiptState] = useState<{
    receipt: SubmissionReceipt;
    alreadySubmitted: boolean;
  } | null>(() => {
    const initialSurvey = surveyOverride
      ? surveyOverride
      : resolveSurveyDefinition(launchContext.surveyId, definitions);
    if (!initialSurvey) {
      return null;
    }
    const existing = readSubmissionReceipt(
      initialSurvey.id,
      launchContext.entrySource,
      launchContext.studySessionId,
      launchContext.matchId,
      storage,
    );
    return existing ? { receipt: existing, alreadySubmitted: true } : null;
  });

  const responseIdRef = useRef<string>(idGenerator());
  const openedAtMsRef = useRef<number>(nowMs());

  // Sync browser back/forward navigation when mounted in a real browser window
  useEffect(() => {
    if (surveyOverride || !resolvedWindow?.addEventListener) {
      return undefined;
    }
    const onPopState = () => {
      const nextCtx = parseSurveyLaunchContext(locationObj, definitions);
      setSelectedSurveyId(nextCtx.surveyId);
      setAnswers({});
      setMissingIds([]);
      setErrorBanner(null);
      responseIdRef.current = idGenerator();
      openedAtMsRef.current = nowMs();
      if (nextCtx.surveyId) {
        const existing = readSubmissionReceipt(
          nextCtx.surveyId,
          nextCtx.entrySource,
          nextCtx.studySessionId,
          nextCtx.matchId,
          storage,
        );
        setReceiptState(
          existing ? { receipt: existing, alreadySubmitted: true } : null,
        );
      } else {
        setReceiptState(null);
      }
    };
    resolvedWindow.addEventListener("popstate", onPopState);
    return () => {
      resolvedWindow.removeEventListener?.("popstate", onPopState);
    };
  }, [surveyOverride, resolvedWindow, locationObj, definitions, storage, idGenerator, nowMs]);

  const visibleSections = useMemo(
    () => (activeSurvey ? getVisibleSurveySections(activeSurvey, answers) : []),
    [activeSurvey, answers],
  );

  const visibleQuestions = useMemo(
    () => (activeSurvey ? getVisibleQuestions(activeSurvey, answers) : []),
    [activeSurvey, answers],
  );

  const answeredCount = useMemo(
    () =>
      visibleQuestions.filter((item) => isAnswerProvided(answers[item.question.id]))
        .length,
    [visibleQuestions, answers],
  );

  const missingSet = useMemo(() => new Set(missingIds), [missingIds]);

  const handleSelectSurvey = (survey: SurveyDefinition) => {
    setSelectedSurveyId(survey.id);
    setAnswers({});
    setMissingIds([]);
    setErrorBanner(null);
    responseIdRef.current = idGenerator();
    openedAtMsRef.current = nowMs();

    const existing = readSubmissionReceipt(
      survey.id,
      launchContext.entrySource,
      launchContext.studySessionId,
      launchContext.matchId,
      storage,
    );
    setReceiptState(
      existing ? { receipt: existing, alreadySubmitted: true } : null,
    );

    try {
      const nextUrl = buildSurveyUrlWithType(survey.routeKey, locationObj);
      resolvedWindow?.history?.pushState?.({ surveyId: survey.id }, "", nextUrl);
    } catch {
      // Ignore restricted history SecurityError in sandboxed contexts.
    }
  };

  const handleBackToLanding = () => {
    setSelectedSurveyId(null);
    setAnswers({});
    setMissingIds([]);
    setErrorBanner(null);
    setReceiptState(null);
    responseIdRef.current = idGenerator();
    openedAtMsRef.current = nowMs();

    try {
      const nextUrl = buildSurveyUrlWithType(null, locationObj);
      resolvedWindow?.history?.pushState?.({}, "", nextUrl);
    } catch {
      // Ignore restricted history SecurityError.
    }
  };

  const handleAnswerChange = (questionId: string, nextValue: AnswerValue) => {
    setAnswers((prev) => ({ ...prev, [questionId]: nextValue }));
    if (missingSet.has(questionId) && isAnswerProvided(nextValue)) {
      setMissingIds((prev) => prev.filter((id) => id !== questionId));
    }
    if (errorBanner) {
      setErrorBanner(null);
    }
  };

  const handleStandaloneReset = () => {
    setAnswers({});
    setMissingIds([]);
    setErrorBanner(null);
    setReceiptState(null);
    responseIdRef.current = idGenerator();
    openedAtMsRef.current = nowMs();
  };

  const handleCloseTab = () => {
    try {
      resolvedWindow?.close?.();
    } catch {
      // Browsers may block script-initiated tab closure; fallback hint is displayed.
    }
  };

  const handleSubmit = async (event: Event) => {
    event.preventDefault();
    if (!activeSurvey || submitting || receiptState) {
      return;
    }

    const missing = findMissingRequiredQuestionIds(activeSurvey, answers);
    if (missing.length > 0) {
      setMissingIds(missing);
      setErrorBanner(getValidationBannerText(missing.length, language));
      const firstMissingEl = document.getElementById(`question-${missing[0]}`);
      if (firstMissingEl) {
        if (typeof firstMissingEl.scrollIntoView === "function") {
          firstMissingEl.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        const focusTarget = firstMissingEl.querySelector<HTMLElement>(
          "input, textarea, button",
        );
        focusTarget?.focus();
      }
      return;
    }

    setMissingIds([]);
    setErrorBanner(null);
    setSubmitting(true);

    const submittedAt = new Date().toISOString();
    const elapsedSeconds = Math.max(
      0,
      Math.round(((nowMs() - openedAtMsRef.current) / 1000) * 10) / 10,
    );

    const rawParticipantCode = answers.participant_code;
    const formParticipantId =
      typeof rawParticipantCode === "string" ? rawParticipantCode.trim() : "";
    const resolvedParticipantId =
      launchContext.participantId || formParticipantId;

    const payload: SurveySubmissionPayload = {
      eventType: "survey-response",
      surveyId: activeSurvey.id,
      surveyVersion: activeSurvey.version,
      surveyTitle: `${activeSurvey.title.zh} / ${activeSurvey.title.en}`,
      entrySource: launchContext.entrySource,
      responseId: responseIdRef.current,
      studySessionId: launchContext.studySessionId,
      sessionId: launchContext.studySessionId,
      matchId: launchContext.matchId,
      roomCode: launchContext.roomCode,
      playerNumber: launchContext.playerNumber,
      ...(resolvedParticipantId
        ? { participantId: resolvedParticipantId }
        : {}),
      ...(launchContext.experimentCondition
        ? { experimentCondition: launchContext.experimentCondition }
        : {}),
      language,
      submittedAt,
      durationSeconds: elapsedSeconds,
      answers: buildSurveyAnswers(activeSurvey, answers),
    };

    try {
      const response = await submitSurveyResponse(payload, { fetchImpl });
      const receipt: SubmissionReceipt = {
        responseId: payload.responseId,
        submittedAt,
        recordId: typeof response.id === "number" ? response.id : null,
        deduplicated: response.deduplicated,
      };
      writeSubmissionReceipt(
        activeSurvey.id,
        launchContext.entrySource,
        launchContext.studySessionId,
        launchContext.matchId,
        receipt,
        storage,
      );
      setReceiptState({ receipt, alreadySubmitted: false });
    } catch (error) {
      setErrorBanner(formatSubmitError(error, language));
    } finally {
      setSubmitting(false);
    }
  };

  const headerTitle = activeSurvey
    ? activeSurvey.title
    : { zh: "推箱子研究问卷中心", en: "Sokoban Study Questionnaires" };

  return (
    <div class="survey-app">
      <div class="survey-shell">
        <header class="wood-sign survey-header">
          <span class="wood-bolt" aria-hidden="true" />
          <div class="survey-sign-body">
            <p class="survey-sign-eyebrow">SOKOBAN STUDY QUESTIONNAIRE</p>
            <h1 class="survey-sign-title">
              <BilingualCopy text={headerTitle} language={language} />
            </h1>
            <div class="survey-header-bar">
              {activeSurvey && !receiptState ? (
                <div class="progress-badge" aria-live="polite">
                  <span class="progress-label">
                    {language === "zh"
                      ? "作答进度"
                      : language === "en"
                        ? "Progress"
                        : "进度 / Progress"}
                  </span>
                  <strong class="progress-count">
                    {answeredCount} / {visibleQuestions.length}
                  </strong>
                </div>
              ) : (
                <div class="progress-badge">
                  <span>
                    {launchContext.entrySource === "unity_matchmaking"
                      ? "STUDY FLOW"
                      : "STANDALONE"}
                  </span>
                </div>
              )}

              <div
                class="lang-toggle"
                role="group"
                aria-label="Display language / 显示语言"
              >
                <button
                  type="button"
                  class={
                    language === "bilingual"
                      ? "lang-btn lang-btn--active"
                      : "lang-btn"
                  }
                  aria-pressed={language === "bilingual"}
                  onClick={() => setLanguage("bilingual")}
                >
                  双语
                </button>
                <button
                  type="button"
                  class={
                    language === "zh" ? "lang-btn lang-btn--active" : "lang-btn"
                  }
                  aria-pressed={language === "zh"}
                  onClick={() => setLanguage("zh")}
                >
                  中文
                </button>
                <button
                  type="button"
                  class={
                    language === "en" ? "lang-btn lang-btn--active" : "lang-btn"
                  }
                  aria-pressed={language === "en"}
                  onClick={() => setLanguage("en")}
                >
                  EN
                </button>
              </div>
            </div>
          </div>
          <span class="wood-bolt" aria-hidden="true" />
        </header>

        <main class="survey-main">
          {!activeSurvey ? (
            <section class="stone-panel landing-panel">
              <div class="stone-panel-header">
                <div>
                  <p class="panel-eyebrow">SELECT QUESTIONNAIRE</p>
                  <h2>
                    <BilingualCopy
                      text={{ zh: "选择问卷", en: "Choose a Questionnaire" }}
                      language={language}
                    />
                  </h2>
                </div>
                <span class="header-pill">{definitions.length} SURVEYS</span>
              </div>

              <div class="landing-body">
                <p class="landing-intro">
                  <BilingualCopy
                    text={{
                      zh: "请选择需要填写或预览的在线研究问卷。正式实验参与者将由游戏自动跳转到对应问卷并自动关联会话编号。",
                      en: "Select a study questionnaire to complete or preview. Formal participants are routed here automatically from the game with session identifiers attached.",
                    }}
                    language={language}
                  />
                </p>

                <div class="landing-cards">
                  {definitions.map((def) => {
                    const totalCount = def.sections.reduce(
                      (sum, sec) => sum + sec.questions.length,
                      0,
                    );
                    return (
                      <article key={def.id} class="landing-card">
                        <div class="landing-card-head">
                          <span class="landing-route-pill">
                            ?type={def.routeKey}
                          </span>
                          <span class="landing-count-pill">
                            {totalCount}{" "}
                            {language === "zh"
                              ? "题"
                              : language === "en"
                                ? "items"
                                : "题 / items"}
                          </span>
                        </div>
                        <h3 class="landing-card-title">
                          <BilingualCopy text={def.title} language={language} />
                        </h3>
                        <p class="landing-card-desc">
                          <BilingualCopy
                            text={def.description}
                            language={language}
                          />
                        </p>
                        <button
                          type="button"
                          class="pixel-btn pixel-btn--play landing-card-btn"
                          aria-label={`${def.title.zh} / ${def.title.en}`}
                          onClick={() => handleSelectSurvey(def)}
                        >
                          <BilingualCopy
                            text={{ zh: "开始填写", en: "Open Questionnaire" }}
                            language={language}
                            className="btn-bilingual"
                          />
                        </button>
                      </article>
                    );
                  })}
                </div>
              </div>
            </section>
          ) : receiptState ? (
            <SurveyReceipt
              survey={activeSurvey}
              receipt={receiptState.receipt}
              alreadySubmitted={receiptState.alreadySubmitted}
              entrySource={launchContext.entrySource}
              language={language}
              onCloseTab={handleCloseTab}
              onFillAnother={
                launchContext.entrySource === "standalone"
                  ? handleStandaloneReset
                  : undefined
              }
              onBackToLanding={
                !surveyOverride && launchContext.entrySource === "standalone"
                  ? handleBackToLanding
                  : undefined
              }
            />
          ) : (
            <form
              class="survey-form"
              novalidate
              onSubmit={(event) => void handleSubmit(event)}
            >
              {launchContext.entrySource === "standalone" ? (
                <NoticeBanner
                  message={getStandaloneNoticeText(language)}
                  variant="info"
                  durationMs={null}
                  className="survey-notice"
                />
              ) : null}

              <section class="stone-panel intro-panel">
                <div class="stone-panel-header">
                  <div>
                    <p class="panel-eyebrow">INSTRUCTIONS</p>
                    <h2>
                      <BilingualCopy
                        text={activeSurvey.title}
                        language={language}
                      />
                    </h2>
                  </div>
                </div>
                <div class="intro-body">
                  <p class="intro-description">
                    <BilingualCopy
                      text={activeSurvey.description}
                      language={language}
                    />
                  </p>
                  <p class="intro-required-note">
                    <span class="required-mark" aria-hidden="true">
                      *
                    </span>
                    <BilingualCopy
                      text={{
                        zh: "标有星号（*）的题目为必填项，其余为选填项。",
                        en: "Questions marked with an asterisk (*) are required; all others are optional.",
                      }}
                      language={language}
                    />
                  </p>
                </div>
              </section>

              {(() => {
                let runningDisplayNumber = 0;
                return visibleSections.map(({ section, questions }) => {
                  const sectionStart = runningDisplayNumber + 1;
                  const sectionAnswered = questions.filter((item) =>
                    isAnswerProvided(answers[item.question.id]),
                  ).length;
                  const sectionCards = questions.map((item) => {
                    runningDisplayNumber += 1;
                    return (
                      <QuestionCard
                        key={item.question.id}
                        question={item.question}
                        displayNumber={runningDisplayNumber}
                        language={language}
                        value={answers[item.question.id]}
                        hasError={missingSet.has(item.question.id)}
                        disabled={submitting}
                        onChange={handleAnswerChange}
                      />
                    );
                  });
                  const sectionEnd = runningDisplayNumber;

                  return (
                    <section
                      key={section.id}
                      class="stone-panel survey-section"
                      data-section-id={section.id}
                    >
                      {section.title ? (
                        <div class="stone-panel-header">
                          <div>
                            <h2>
                              <BilingualCopy
                                text={section.title}
                                language={language}
                              />
                            </h2>
                          </div>
                          <span class="header-pill">
                            Q{sectionStart}–Q{sectionEnd} · {sectionAnswered}/
                            {questions.length}
                          </span>
                        </div>
                      ) : null}

                      {section.description ? (
                        <p class="section-description">
                          <BilingualCopy
                            text={section.description}
                            language={language}
                          />
                        </p>
                      ) : null}

                      <div class="section-questions">{sectionCards}</div>
                    </section>
                  );
                });
              })()}

              {errorBanner ? (
                <NoticeBanner
                  message={errorBanner}
                  variant="error"
                  durationMs={null}
                  onDismiss={() => setErrorBanner(null)}
                  className="survey-notice"
                />
              ) : null}

              <section class="stone-panel submit-panel">
                <div class="submit-summary">
                  <BilingualCopy
                    text={{
                      zh: `已作答 ${answeredCount} / ${visibleQuestions.length} 题`,
                      en: `Answered ${answeredCount} / ${visibleQuestions.length} questions`,
                    }}
                    language={language}
                  />
                </div>
                <div class="submit-actions">
                  {!surveyOverride && launchContext.entrySource === "standalone" ? (
                    <button
                      type="button"
                      class="pixel-btn pixel-btn--secondary"
                      disabled={submitting}
                      onClick={handleBackToLanding}
                    >
                      <BilingualCopy
                        text={{ zh: "返回问卷列表", en: "Switch Survey" }}
                        language={language}
                        className="btn-bilingual"
                      />
                    </button>
                  ) : null}

                  <button
                    type="submit"
                    class="pixel-btn pixel-btn--play submit-btn"
                    disabled={submitting}
                  >
                    <BilingualCopy
                      text={
                        submitting
                          ? { zh: "正在提交…", en: "Submitting…" }
                          : { zh: "提交问卷", en: "Submit Survey" }
                      }
                      language={language}
                      className="btn-bilingual"
                    />
                  </button>
                </div>
              </section>
            </form>
          )}
        </main>
      </div>
    </div>
  );
}
