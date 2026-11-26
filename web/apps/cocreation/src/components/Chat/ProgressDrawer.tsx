import type { JSX } from "preact";
import { useEffect, useState } from "preact/hooks";
import { t } from "../../i18n";
import { localStore, storageKeys } from "../../storage/safeStorage";
import { questionFeedbackBusyKeys } from "../../store/chatStore";
import {
  busy,
  canEditSelected,
  language,
  selectedProgressContext,
  selectedVersionId,
  sessionId,
  unityPlayActive,
} from "../../store/sessionStore";
import { submitQuestionFeedback } from "../../store/workflows";
import type { DesignInclination, QuestionRecord } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

function QuestionList({
  items,
  processed,
}: {
  items: QuestionRecord[];
  processed: boolean;
}): JSX.Element {
  const lang = language.value;
  const editable = canEditSelected.value;
  const interactionBusy = busy.value || unityPlayActive.value;
  const busyKeys = questionFeedbackBusyKeys.value;

  if (items.length === 0) {
    return (
      <div class="progress-items" aria-live="polite">
        <p class="progress-empty">
          {t(lang, processed ? "noAnsweredQuestions" : "noUnresolvedQuestions")}
        </p>
      </div>
    );
  }

  return (
    <div class="progress-items" aria-live="polite">
      {items.map((item) => {
        const ignored = item.status === "ignored";
        const statusClass = ignored ? "ignored" : processed ? "answered" : "unanswered";
        const metaParts = [
          t(lang, ignored ? "ignoredLabel" : processed ? "answeredLabel" : "unresolvedLabel"),
        ];
        if (Number.isInteger(item.askedAtStageNumber)) {
          metaParts.push(t(lang, "askedAtStage", { stage: item.askedAtStageNumber! }));
        }
        if (processed && Number.isInteger(item.answeredAtStageNumber)) {
          metaParts.push(t(lang, "answeredAtStage", { stage: item.answeredAtStageNumber! }));
        }
        if (ignored && Number.isInteger(item.ignoredAtStageNumber)) {
          metaParts.push(t(lang, "ignoredAtStage", { stage: item.ignoredAtStageNumber! }));
        }

        const action: "ignore" | "restore" | null =
          item.questionId && (!processed || ignored) ? (ignored ? "restore" : "ignore") : null;
        const feedbackKey = action ? `${item.questionId}:${action}` : "";
        const itemBusy = Boolean(feedbackKey && busyKeys.has(feedbackKey));
        const disabled = interactionBusy || !editable || itemBusy;

        return (
          <article
            key={item.questionId || item.question}
            class={`progress-item progress-item-question ${statusClass}`}
            aria-busy={itemBusy ? "true" : "false"}
          >
            <p class="progress-item-title progress-item-text">{String(item.question || "").trim()}</p>
            <footer class="progress-item-footer">
              <small class="progress-item-meta">{metaParts.join(" · ")}</small>
              {action ? (
                <button
                  type="button"
                  class="secondary-button guidance-cue-button question-feedback-button"
                  disabled={disabled}
                  aria-disabled={disabled ? "true" : undefined}
                  onClick={() => void submitQuestionFeedback(action, item)}
                  {...tooltipHandlers({
                    kind: "action",
                    helpKey: ignored ? "restoreQuestion" : "ignoreQuestion",
                    titleKey: ignored ? "restoreQuestion" : "ignoreQuestion",
                  })}
                >
                  {t(lang, ignored ? "restoreQuestion" : "ignoreQuestion")}
                </button>
              ) : null}
            </footer>
          </article>
        );
      })}
    </div>
  );
}

function InclinationsList({ items }: { items: DesignInclination[] }): JSX.Element {
  const lang = language.value;

  if (items.length === 0) {
    return (
      <div class="progress-items" aria-live="polite">
        <p class="progress-empty">{t(lang, "noDesignInclinations")}</p>
      </div>
    );
  }

  return (
    <div class="progress-items" aria-live="polite">
      {items.map((item, idx) => (
        <article
          key={item.inclinationId || `${item.statement}:${idx}`}
          class="progress-item progress-item-inclination"
        >
          <p class="progress-item-title progress-item-text">{String(item.statement || "").trim()}</p>
          <div class="progress-evidence">
            <strong class="inclination-why">{t(lang, "whyUnderstand")}</strong>
            <ol class="inclination-evidence">
              {(Array.isArray(item.evidenceTrail) ? item.evidenceTrail : []).map((ev, evIdx) => (
                <li key={evIdx}>
                  <span class="progress-evidence-stage">
                    {t(lang, "stage")} {ev.stageNumber}
                  </span>
                  <span class="progress-evidence-text">{String(ev.text || "")}</span>
                  {ev.kind === "manual_edit" && ev.detailedText ? (
                    <details class="manual-observation-detail">
                      <summary>{t(lang, "viewDetailedObservation")}</summary>
                      <p>{String(ev.detailedText)}</p>
                    </details>
                  ) : null}
                </li>
              ))}
            </ol>
          </div>
          <footer class="progress-item-footer progress-item-footer-confirmed">
            <small class="progress-item-meta">
              {t(lang, "confirmedAtStage", { stage: item.confirmedAtStageNumber ?? "" })}
            </small>
          </footer>
        </article>
      ))}
    </div>
  );
}

export function ProgressDrawer(): JSX.Element | null {
  const context = selectedProgressContext.value;
  const sid = sessionId.value;
  const vid = selectedVersionId.value;
  const lang = language.value;

  const storageKey = sid && vid ? storageKeys.progressPanel(sid, vid) : "";
  const [open, setOpen] = useState<boolean>(() =>
    storageKey ? localStore.get<string>(storageKey) === "open" : false,
  );
  const [processedOpen, setProcessedOpen] = useState<boolean>(false);

  useEffect(() => {
    if (!storageKey) return;
    setOpen(localStore.get<string>(storageKey) === "open");
  }, [storageKey]);

  if (!context) return null;

  const questions = Array.isArray(context.questionRecords) ? context.questionRecords : [];
  const unresolved = questions.filter((item) => item?.status === "unanswered");
  const processed = questions.filter(
    (item) => item?.status === "answered" || item?.status === "ignored",
  );
  const inclinations = Array.isArray(context.designInclinations) ? context.designInclinations : [];

  return (
    <details
      class="progress-panel"
      open={open}
      onToggle={(event) => {
        const nextOpen = (event.currentTarget as HTMLDetailsElement).open;
        setOpen(nextOpen);
        if (storageKey) {
          localStore.set(storageKey, nextOpen ? "open" : "closed");
        }
      }}
    >
      <summary aria-controls="progressPanelBody" aria-expanded={open ? "true" : "false"}>
        <span>{t(lang, "progressTitle")}</span>
      </summary>
      <div class="progress-panel-body" id="progressPanelBody">
        <p class="progress-introduction">{t(lang, "progressHelpBody")}</p>

        <section class="progress-section progress-section-questions" aria-labelledby="progressQuestionsTitle">
          <h3 id="progressQuestionsTitle">{t(lang, "unresolvedQuestions")}</h3>
          <QuestionList items={unresolved} processed={false} />
          {processed.length > 0 ? (
            <details
              class="answered-questions"
              open={processedOpen}
              onToggle={(event) => {
                setProcessedOpen((event.currentTarget as HTMLDetailsElement).open);
              }}
            >
              <summary
                aria-controls="answeredQuestionsList"
                aria-expanded={processedOpen ? "true" : "false"}
              >
                {t(lang, "answeredQuestions")}
              </summary>
              <div id="answeredQuestionsList">
                <QuestionList items={processed} processed={true} />
              </div>
            </details>
          ) : null}
        </section>

        <section
          class="progress-section progress-section-inclinations"
          aria-labelledby="designInclinationsTitle"
        >
          <h3 id="designInclinationsTitle">{t(lang, "designInclinations")}</h3>
          <InclinationsList items={inclinations} />
        </section>
      </div>
    </details>
  );
}
