import type { JSX } from "preact";
import { t, type TranslationKey } from "../../i18n";
import { canUseChallengeChoice } from "../../store/chatStore";
import { language } from "../../store/sessionStore";
import { sendChallengeChoice } from "../../store/workflows";
import type { ChallengeCard, ChallengeState, Disagreement } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

export interface DisagreementCardProps {
  disagreement: Disagreement | ChallengeCard;
  challengeState?: ChallengeState | null;
}

export function DisagreementCard({
  disagreement,
  challengeState,
}: DisagreementCardProps): JSX.Element {
  const lang = language.value;
  const cardQuestion =
    disagreement.nextQuestion ||
    ("question" in disagreement ? disagreement.question : undefined);
  const cardSummary =
    disagreement.coreDisagreement ||
    ("summary" in disagreement ? disagreement.summary : undefined);
  const fields: Array<[TranslationKey, string | undefined]> = [
    ["discussionUser", disagreement.userPosition],
    ["discussionCore", cardSummary],
    ["discussionNext", cardQuestion],
  ];

  const resolvedChallengeId = disagreement.challengeId || challengeState?.challengeId;
  const effectivePhase =
    disagreement.phase ||
    (challengeState?.status === "choice_pending" || disagreement.status === "choice_pending"
      ? "choice_pending"
      : undefined);

  const showChoiceButtons =
    (disagreement.subject === "ai_revision_challenge" ||
      Boolean("choices" in disagreement && disagreement.choices?.length)) &&
    effectivePhase === "choice_pending" &&
    Boolean(resolvedChallengeId);
  const canChoose = canUseChallengeChoice(resolvedChallengeId);

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
      {showChoiceButtons && resolvedChallengeId ? (
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
              onClick={() => sendChallengeChoice(resolvedChallengeId, choice)}
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
