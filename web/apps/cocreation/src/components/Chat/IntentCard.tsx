import type { JSX } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { t } from "../../i18n";
import { intentFeedbackBusyKeys } from "../../store/chatStore";
import { busy, canEditSelected, language, unityPlayActive } from "../../store/sessionStore";
import { submitIntentFeedback } from "../../store/workflows";
import type { IntentHypothesis, Turn } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

export interface IntentCardProps {
  text: string;
  intentState: IntentHypothesis | null | undefined;
  turn: Turn;
}

function resolvedStatusLabel(
  lang: "en" | "zh-CN",
  cardState: IntentHypothesis,
  originalText: string,
): string {
  if (cardState.status === "confirmed") {
    return cardState.resolvedStatement &&
      String(cardState.resolvedStatement).trim() !== String(originalText).trim()
      ? t(lang, "intentRevised")
      : t(lang, "intentConfirmed");
  }
  if (cardState.status === "rejected") return t(lang, "intentRejected");
  if (cardState.status === "superseded") return t(lang, "intentSuperseded");
  return t(lang, "intentStale");
}

function StandardIntentCard({ text, intentState, turn }: IntentCardProps): JSX.Element {
  const lang = language.value;
  const cardState: IntentHypothesis =
    intentState && typeof intentState === "object"
      ? intentState
      : { hypothesisId: "", status: "tentative", interactionMode: "resolved", actionable: false };

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
      textareaRef.current.select();
    }
  }, [editing]);

  const busyKey = `${turn.turnId}:${cardState.hypothesisId}`;
  const cardBusy = intentFeedbackBusyKeys.value.has(busyKey);
  const interactionDisabled = busy.value || unityPlayActive.value || cardBusy || !canEditSelected.value;
  const isActionable = Boolean(cardState.actionable && canEditSelected.value);

  const openEditor = () => {
    if (interactionDisabled) return;
    setDraft(text);
    setEditing(true);
  };

  const handleReviseSubmit = async (event: Event) => {
    event.preventDefault();
    const candidate = draft.trim();
    if (candidate.length < 4) {
      textareaRef.current?.focus();
      return;
    }
    const ok = await submitIntentFeedback({
      action: "revise",
      turn,
      intentState: cardState,
      candidateText: candidate,
    });
    if (ok) setEditing(false);
  };

  return (
    <section
      class="guidance-cue guidance-cue-intent intent-feedback-card"
      aria-busy={cardBusy ? "true" : "false"}
    >
      <span class="guidance-cue-label">{t(lang, "cue.intent")}</span>
      <strong>{text}</strong>

      {!isActionable ? (
        <small class="guidance-cue-stale-note intent-resolution">
          {resolvedStatusLabel(lang, cardState, text)}
        </small>
      ) : editing ? (
        <form class="intent-edit-form" onSubmit={(e) => void handleReviseSubmit(e)}>
          <label class="sr-only" for={`intent-edit-${turn.turnId}`}>
            {t(lang, "intentModify")}
          </label>
          <textarea
            ref={textareaRef}
            id={`intent-edit-${turn.turnId}`}
            rows={3}
            maxLength={1200}
            disabled={interactionDisabled}
            value={draft}
            onInput={(event) => {
              setDraft((event.currentTarget as HTMLTextAreaElement).value);
            }}
          />
          <small class="intent-character-count">
            {t(lang, "intentCharacters", { count: draft.length })}
          </small>
          <div class="intent-edit-actions">
            <button
              type="submit"
              class="secondary-button guidance-cue-button"
              disabled={interactionDisabled}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "intentSave",
                titleKey: "intentSave",
              })}
            >
              {t(lang, "intentSave")}
            </button>
            <button
              type="button"
              class="secondary-button guidance-cue-button"
              disabled={interactionDisabled}
              onClick={() => setEditing(false)}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "intentCancel",
                titleKey: "intentCancel",
              })}
            >
              {t(lang, "intentCancel")}
            </button>
          </div>
        </form>
      ) : (
        <div class="intent-card-actions">
          {cardState.interactionMode === "full" ? (
            <>
              <button
                type="button"
                class="secondary-button guidance-cue-button"
                disabled={interactionDisabled}
                onClick={() =>
                  void submitIntentFeedback({ action: "confirm", turn, intentState: cardState })
                }
                {...tooltipHandlers({
                  kind: "action",
                  helpKey: "intentMatches",
                  titleKey: "intentMatches",
                })}
              >
                {t(lang, "intentMatches")}
              </button>
              <button
                type="button"
                class="secondary-button guidance-cue-button"
                disabled={interactionDisabled}
                onClick={() =>
                  void submitIntentFeedback({ action: "reject", turn, intentState: cardState })
                }
                {...tooltipHandlers({
                  kind: "action",
                  helpKey: "intentReject",
                  titleKey: "intentReject",
                })}
              >
                {t(lang, "intentReject")}
              </button>
              <button
                type="button"
                class="secondary-button guidance-cue-button"
                disabled={interactionDisabled}
                onClick={openEditor}
                {...tooltipHandlers({
                  kind: "action",
                  helpKey: "intentAdjust",
                  titleKey: "intentAdjust",
                })}
              >
                {t(lang, "intentAdjust")}
              </button>
            </>
          ) : (
            <button
              type="button"
              class="secondary-button guidance-cue-button"
              disabled={interactionDisabled}
              onClick={openEditor}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "intentModify",
                titleKey: "intentModify",
              })}
            >
              {t(lang, "intentModify")}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

export function IntentCard({ text, intentState, turn }: IntentCardProps): JSX.Element {
  const lang = language.value;
  const choice = intentState?.conflictChoice;

  if (
    intentState?.interactionMode !== "conflict_choice" ||
    !choice ||
    !Array.isArray(choice.options) ||
    choice.options.length !== 2
  ) {
    return <StandardIntentCard text={text} intentState={intentState} turn={turn} />;
  }

  const busyKey = `${turn.turnId}:${intentState.hypothesisId}`;
  const cardBusy = intentFeedbackBusyKeys.value.has(busyKey);
  const interactionDisabled = busy.value || unityPlayActive.value || cardBusy || !canEditSelected.value;
  const isActionable = Boolean(intentState.actionable && canEditSelected.value);

  return (
    <section class="intent-conflict-choice">
      {choice.options.map((option) => {
        const statement = String(
          option.role === "new" ? text || option.statement || "" : option.statement || "",
        ).trim();
        const evidenceText = String(option.evidenceText || "").trim();

        let statusText = t(lang, "intentStale");
        if (choice.selectedHypothesisId === option.hypothesisId) {
          statusText = t(lang, "intentKept");
        } else if (option.status === "superseded") {
          statusText = t(lang, "intentSuperseded");
        } else if (option.status === "rejected") {
          statusText = t(lang, "intentRejected");
        }

        return (
          <section
            key={option.hypothesisId}
            class="guidance-cue guidance-cue-intent intent-feedback-card intent-conflict-option"
            aria-busy={cardBusy ? "true" : "false"}
          >
            <span class="guidance-cue-label">{t(lang, "cue.intent")}</span>
            <small class="intent-conflict-option-role">
              {t(lang, option.role === "new" ? "intentNewOption" : "intentExistingOption")}
            </small>
            <strong>{statement}</strong>
            {evidenceText ? (
              <small class="intent-conflict-evidence">
                {t(lang, "intentEvidence", { text: evidenceText })}
              </small>
            ) : null}
            {isActionable ? (
              <div class="intent-card-actions">
                <button
                  type="button"
                  class="secondary-button guidance-cue-button"
                  disabled={interactionDisabled}
                  onClick={() =>
                    void submitIntentFeedback({
                      action: "keep",
                      turn,
                      intentState,
                      selectedHypothesisId: option.hypothesisId,
                    })
                  }
                  {...tooltipHandlers({
                    kind: "action",
                    helpKey: "intentKeepThis",
                    titleKey: "intentKeepThis",
                  })}
                >
                  {t(lang, "intentKeepThis")}
                </button>
              </div>
            ) : (
              <small class="guidance-cue-stale-note intent-resolution">{statusText}</small>
            )}
          </section>
        );
      })}
    </section>
  );
}
