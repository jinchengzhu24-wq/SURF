import type { JSX } from "preact";
import { t, type TranslationKey } from "../../i18n";
import { canUseChallengeChoice } from "../../store/chatStore";
import { language } from "../../store/sessionStore";
import { sendChallengeChoice } from "../../store/workflows";
import type { Disagreement } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

export interface DisagreementCardProps {
  disagreement: Disagreement;
}

export function DisagreementCard({ disagreement }: DisagreementCardProps): JSX.Element {
  const lang = language.value;
  const fields: Array<[TranslationKey, string | undefined]> = [
    ["discussionUser", disagreement.userPosition],
    ["discussionCore", disagreement.coreDisagreement],
    ["discussionNext", disagreement.nextQuestion],
  ];

  const showChoiceButtons =
    disagreement.subject === "ai_revision_challenge" &&
    disagreement.phase === "choice_pending" &&
    Boolean(disagreement.challengeId);
  const canChoose = canUseChallengeChoice(disagreement.challengeId);

  return (
    <section class="discussion-focus disagreement-card">
      <span class="discussion-focus-label">{t(lang, "cue.question")}</span>
      {fields.map(([labelKey, value]) => {
        const text = String(value || "").trim();
        if (!text) return null;
        return (
          <div key={labelKey} class="disagreement-field">
            <small>{t(lang, labelKey)}</small>
            <strong>{text}</strong>
          </div>
        );
      })}
      {showChoiceButtons && disagreement.challengeId ? (
        <div class="proposal-actions">
          {(
            [
              ["ai", "challengeChoiceYes"],
              ["user", "challengeChoiceNo"],
            ] as const
          ).map(([choice, labelKey]) => (
            <button
              key={choice}
              type="button"
              class="secondary-button guidance-cue-button challenge-choice-button"
              disabled={!canChoose}
              aria-disabled={!canChoose ? "true" : undefined}
              onClick={() => sendChallengeChoice(disagreement.challengeId!, choice)}
              {...tooltipHandlers({
                kind: "action",
                helpKey: "challengeChoice",
                titleKey: labelKey,
              })}
            >
              {t(lang, labelKey)}
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}
