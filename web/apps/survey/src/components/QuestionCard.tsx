import type { JSX } from "preact";
import type {
  AnswerValue,
  BilingualText,
  DisplayLanguage,
  QuestionDefinition,
} from "../surveyDefinitions";

export function BilingualCopy({
  text,
  language,
  className,
}: {
  text: BilingualText;
  language: DisplayLanguage;
  className?: string;
}): JSX.Element {
  return (
    <span class={className ? `bilingual-copy ${className}` : "bilingual-copy"}>
      {language !== "en" ? (
        <span class="bilingual-zh" lang="zh-CN">
          {text.zh}
        </span>
      ) : null}
      {language !== "zh" ? (
        <span class="bilingual-en" lang="en">
          {text.en}
        </span>
      ) : null}
    </span>
  );
}

export interface QuestionCardProps {
  question: QuestionDefinition;
  displayNumber: number;
  language: DisplayLanguage;
  value: AnswerValue | undefined;
  hasError: boolean;
  disabled: boolean;
  onChange: (questionId: string, nextValue: AnswerValue) => void;
}

function formatBilingualInline(
  text: BilingualText,
  language: DisplayLanguage,
): string {
  if (language === "zh") {
    return text.zh;
  }
  if (language === "en") {
    return text.en;
  }
  return `${text.zh} / ${text.en}`;
}

function getPlaceholderText(
  placeholder: BilingualText | undefined,
  language: DisplayLanguage,
): string {
  if (!placeholder) {
    return "";
  }
  return formatBilingualInline(placeholder, language);
}

export function QuestionCard({
  question,
  displayNumber,
  language,
  value,
  hasError,
  disabled,
  onChange,
}: QuestionCardProps): JSX.Element {
  const fieldId = `question-${question.id}`;
  const errorId = `${fieldId}-error`;
  const cardClass = hasError
    ? "question-card question-card--error"
    : "question-card";

  const textareaValue = typeof value === "string" ? value : "";
  const charCount = textareaValue.length;

  return (
    <div class={cardClass} id={fieldId} data-question-id={question.id}>
      <fieldset
        class="question-fieldset"
        aria-invalid={hasError ? "true" : undefined}
        aria-describedby={hasError ? errorId : undefined}
      >
        <legend class="question-legend">
          <span class="question-badge">Q{displayNumber}</span>
          <span class="question-prompt-wrap">
            <BilingualCopy
              text={question.prompt}
              language={language}
              className="question-prompt"
            />
            {question.required ? (
              <span class="required-mark" aria-hidden="true">
                *
              </span>
            ) : null}
          </span>
        </legend>

        {question.helpText ? (
          <p class="question-help">
            <BilingualCopy text={question.helpText} language={language} />
          </p>
        ) : null}

        {question.type === "single_choice" ? (
          <div class="choice-grid">
            {(question.options ?? []).map((option) => {
              const selected =
                value !== undefined && String(value) === option.id;
              return (
                <label
                  key={option.id}
                  class={
                    selected
                      ? "choice-option choice-option--selected"
                      : "choice-option"
                  }
                >
                  <input
                    type="radio"
                    class="sr-only"
                    name={`q_${question.id}`}
                    value={option.id}
                    checked={selected}
                    disabled={disabled}
                    onChange={() => onChange(question.id, option.id)}
                  />
                  <span class="choice-indicator" aria-hidden="true">
                    {selected ? "■" : ""}
                  </span>
                  <BilingualCopy
                    text={option.label}
                    language={language}
                    className="choice-label"
                  />
                </label>
              );
            })}
          </div>
        ) : null}

        {question.type === "likert_5"
          ? (() => {
              const likertOptions = question.options ?? [];
              const minScore = likertOptions[0]?.score ?? 1;
              const maxScore =
                likertOptions[likertOptions.length - 1]?.score ?? 5;
              const neutralScore = Math.round((minScore + maxScore) / 2);
              const matchedOption =
                value !== undefined
                  ? likertOptions.find(
                      (option) =>
                        (option.score !== undefined &&
                          value === option.score) ||
                        String(value) === option.id,
                    )
                  : undefined;
              const selectedScore =
                matchedOption?.score ??
                (typeof value === "number" && Number.isFinite(value)
                  ? value
                  : undefined);
              const hasAnswer = selectedScore !== undefined;
              const fillPercent =
                hasAnswer && maxScore > minScore
                  ? ((selectedScore - minScore) / (maxScore - minScore)) * 100
                  : 0;

              const clampScore = (raw: number): number => {
                if (!Number.isFinite(raw)) {
                  return neutralScore;
                }
                return Math.min(maxScore, Math.max(minScore, Math.round(raw)));
              };

              const commitScore = (raw: number) => {
                if (disabled) {
                  return;
                }
                const clamped = clampScore(raw);
                const targetOpt = likertOptions.find(
                  (option) => (option.score ?? option.index) === clamped,
                );
                onChange(question.id, targetOpt?.score ?? clamped);
              };

              const activeBadgeText =
                hasAnswer && matchedOption
                  ? `${selectedScore} · ${formatBilingualInline(matchedOption.label, language)}`
                  : hasAnswer
                    ? String(selectedScore)
                    : formatBilingualInline(
                        {
                          zh: "请拖动滑块或点击刻度评分",
                          en: "Drag slider or click a tick to rate",
                        },
                        language,
                      );

              const ariaValueText =
                hasAnswer && matchedOption
                  ? `${selectedScore} · ${formatBilingualInline(matchedOption.label, language)}`
                  : formatBilingualInline(
                      { zh: "未作答", en: "Not answered" },
                      language,
                    );

              return (
                <div
                  class="likert-slider"
                  data-answered={hasAnswer ? "true" : "false"}
                >
                  <div class="likert-slider-header">
                    <span
                      class={
                        hasAnswer
                          ? "likert-status-badge likert-status-badge--active"
                          : "likert-status-badge likert-status-badge--inactive"
                      }
                      data-score={hasAnswer ? String(selectedScore) : undefined}
                      aria-live="polite"
                    >
                      {activeBadgeText}
                    </span>
                    <span class="likert-scale-hint" aria-hidden="true">
                      {minScore} – {maxScore}
                    </span>
                  </div>

                  <div class="likert-track-stage">
                    <div class="likert-track-wrap">
                      <div class="likert-track-rail" aria-hidden="true">
                        <div
                          class="likert-track-fill"
                          style={{ width: `${fillPercent}%` }}
                          data-fill-percent={String(fillPercent)}
                        />
                      </div>
                      <div class="likert-track-pips" aria-hidden="true">
                        {likertOptions.map((option) => {
                          const optionScore = option.score ?? option.index;
                          const isFilled =
                            hasAnswer && optionScore <= selectedScore;
                          return (
                            <span
                              key={option.id}
                              class={
                                isFilled
                                  ? "likert-track-pip likert-track-pip--filled"
                                  : "likert-track-pip"
                              }
                            />
                          );
                        })}
                      </div>
                      <input
                        id={`${fieldId}-slider`}
                        name={`q_${question.id}`}
                        type="range"
                        class="likert-range-input"
                        min={minScore}
                        max={maxScore}
                        step={1}
                        value={hasAnswer ? selectedScore : neutralScore}
                        disabled={disabled}
                        aria-label={formatBilingualInline(
                          question.prompt,
                          language,
                        )}
                        aria-valuemin={minScore}
                        aria-valuemax={maxScore}
                        aria-valuenow={hasAnswer ? selectedScore : undefined}
                        aria-valuetext={ariaValueText}
                        onInput={(event) =>
                          commitScore(
                            Number(
                              (event.currentTarget as HTMLInputElement).value,
                            ),
                          )
                        }
                        onChange={(event) =>
                          commitScore(
                            Number(
                              (event.currentTarget as HTMLInputElement).value,
                            ),
                          )
                        }
                        onClick={(event) => {
                          if (!hasAnswer) {
                            commitScore(
                              Number(
                                (event.currentTarget as HTMLInputElement).value,
                              ),
                            );
                          }
                        }}
                        onPointerUp={(event) => {
                          if (!hasAnswer) {
                            commitScore(
                              Number(
                                (event.currentTarget as HTMLInputElement).value,
                              ),
                            );
                          }
                        }}
                        onKeyDown={(event) => {
                          if (disabled || hasAnswer) {
                            return;
                          }
                          const key = event.key;
                          if (key === "ArrowLeft" || key === "ArrowDown") {
                            event.preventDefault();
                            commitScore(neutralScore - 1);
                          } else if (
                            key === "ArrowRight" ||
                            key === "ArrowUp"
                          ) {
                            event.preventDefault();
                            commitScore(neutralScore + 1);
                          } else if (key === "Home") {
                            event.preventDefault();
                            commitScore(minScore);
                          } else if (key === "End") {
                            event.preventDefault();
                            commitScore(maxScore);
                          } else if (key === "Enter" || key === " ") {
                            event.preventDefault();
                            commitScore(neutralScore);
                          }
                        }}
                      />
                    </div>
                  </div>

                  <div
                    class="likert-ticks"
                    role="group"
                    aria-label={formatBilingualInline(
                      { zh: "评分刻度", en: "Rating ticks" },
                      language,
                    )}
                  >
                    {likertOptions.map((option) => {
                      const optionScore = option.score ?? option.index;
                      const selected =
                        hasAnswer && selectedScore === optionScore;
                      return (
                        <button
                          key={option.id}
                          type="button"
                          class={
                            selected
                              ? "likert-tick likert-tick--selected"
                              : "likert-tick"
                          }
                          aria-pressed={selected}
                          data-score={String(optionScore)}
                          disabled={disabled}
                          onClick={() => commitScore(optionScore)}
                        >
                          <span class="likert-tick-score" aria-hidden="true">
                            {optionScore}
                          </span>
                          <BilingualCopy
                            text={option.label}
                            language={language}
                            className="likert-tick-label"
                          />
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })()
          : null}

        {question.type === "textarea" ? (
          <div class="textarea-wrap">
            {question.singleLine ? (
              <input
                type="text"
                class="survey-text-input"
                id={`${fieldId}-input`}
                name={`q_${question.id}`}
                maxLength={question.maxLength}
                disabled={disabled}
                autocomplete="off"
                placeholder={getPlaceholderText(question.placeholder, language)}
                value={textareaValue}
                aria-label={
                  language === "en" ? question.prompt.en : question.prompt.zh
                }
                onInput={(event) =>
                  onChange(
                    question.id,
                    (event.currentTarget as HTMLInputElement).value,
                  )
                }
              />
            ) : (
              <textarea
                class="survey-textarea"
                id={`${fieldId}-input`}
                name={`q_${question.id}`}
                rows={question.rows ?? 4}
                maxLength={question.maxLength}
                disabled={disabled}
                placeholder={getPlaceholderText(question.placeholder, language)}
                value={textareaValue}
                aria-label={
                  language === "en" ? question.prompt.en : question.prompt.zh
                }
                onInput={(event) =>
                  onChange(
                    question.id,
                    (event.currentTarget as HTMLTextAreaElement).value,
                  )
                }
              />
            )}
            {question.maxLength !== undefined ? (
              <div class="char-counter" aria-live="polite">
                {charCount} / {question.maxLength}
              </div>
            ) : null}
          </div>
        ) : null}

        {hasError ? (
          <p class="question-error-msg" id={errorId} role="alert">
            <BilingualCopy
              text={{
                zh: "此题为必填项，请完成作答后再提交。",
                en: "This question is required. Please provide an answer before submitting.",
              }}
              language={language}
            />
          </p>
        ) : null}
      </fieldset>
    </div>
  );
}
