import type { JSX } from "preact";
import { t, type TranslationKey } from "../../i18n";
import { language, proposalForTurn } from "../../store/sessionStore";
import type { GuidanceCueType, Proposal, Turn, TurnGuidance, UiCue } from "../../types";
import { BinaryChoiceCard } from "./BinaryChoiceCard";
import { CoordinateRouteText } from "./CoordinateRouteText";
import { DisagreementCard } from "./DisagreementCard";
import { IntentCard } from "./IntentCard";
import { formatVerifiedProposalSummary } from "./ProposalCard";
import { RevisionOfferCard } from "./RevisionOfferCard";

export function isInternalProposalFailureCue(cue: UiCue | null | undefined): boolean {
  if (!cue || cue.type !== "clarification") return false;
  const text = String(cue.text || "").toLowerCase();
  return (
    text.includes("无法形成") ||
    text.includes("可验证的方案") ||
    text.includes("could not form a proposal") ||
    text.includes("fully consistent with the saved map")
  );
}

export function assistantBodyWithoutCues(
  content: string,
  uiCues: readonly UiCue[],
  question: string,
): string {
  let body = String(content || "").trimEnd();
  const suffixes = [
    ...uiCues.map((cue) => String(cue?.text || "").trim()).filter(Boolean),
    question,
  ].filter(Boolean);

  for (let i = suffixes.length - 1; i >= 0; i -= 1) {
    const suffix = suffixes[i];
    if (body.endsWith(suffix)) {
      body = body.slice(0, -suffix.length).trimEnd();
    }
  }
  return body;
}

function guidanceForDisplay(
  source: TurnGuidance,
  proposal: Proposal | null,
  lang: "en" | "zh-CN",
): TurnGuidance {
  const guidance: TurnGuidance = { ...source };
  const cues = (Array.isArray(source.uiCues) ? source.uiCues : [])
    .filter((cue): cue is UiCue => Boolean(cue && cue.text))
    .filter((cue) => !isInternalProposalFailureCue(cue));

  const warning = cues.find((cue) => cue.type === "warning" || cue.type === "tradeoff");
  let manual = cues.find((cue) => cue.type === "manual_edit");
  const clarification = cues.find((cue) => cue.type === "clarification");

  if (guidance.proposalOffer || proposal) {
    manual = manual ?? {
      type: "manual_edit",
      text: t(lang, proposal ? "proposalCompanionPending" : "proposalCompanionOffer"),
    };
    guidance.intentHypothesis = null;
    guidance.followUpQuestion = null;
    guidance.uiCues = [manual, warning].filter((c): c is UiCue => Boolean(c));
    return guidance;
  }

  if (manual) {
    guidance.intentHypothesis = null;
    guidance.followUpQuestion = null;
    guidance.uiCues = [manual];
    return guidance;
  }

  if (clarification) {
    guidance.intentHypothesis = null;
    guidance.followUpQuestion = null;
    guidance.uiCues = [clarification, warning].filter((c): c is UiCue => Boolean(c));
    return guidance;
  }

  guidance.uiCues = warning ? [warning] : [];
  return guidance;
}

function cueLabelKey(type: GuidanceCueType): TranslationKey {
  return `cue.${type}` as TranslationKey;
}

export interface AssistantBubbleProps {
  turn: Turn;
}

export function AssistantBubble({ turn }: AssistantBubbleProps): JSX.Element {
  const lang = language.value;
  const proposal = proposalForTurn(turn.turnId);

  const translation =
    turn.role === "assistant" && turn.language && turn.language !== lang
      ? turn.translations?.[lang]
      : undefined;

  const localizedContent = proposal
    ? t(lang, "verifiedProposalMessage")
    : translation
      ? translation.body
      : turn.content;

  const localizedGuidance: TurnGuidance =
    !proposal && translation ? translation.guidance || {} : turn.guidance || {};

  if (localizedGuidance.proposalQuestion) {
    return <BinaryChoiceCard question={localizedGuidance.proposalQuestion} turn={turn} />;
  }

  const guidance = guidanceForDisplay(localizedGuidance, proposal, lang);
  const question = String(guidance.followUpQuestion || "").trim();
  const uiCues = Array.isArray(guidance.uiCues) ? guidance.uiCues : [];
  const rawDisagreement = guidance.disagreement || guidance.challengeCard;
  const activeDisagreement =
    rawDisagreement &&
    (rawDisagreement.status === "active" ||
      rawDisagreement.status === "choice_pending" ||
      (!rawDisagreement.status && Boolean(rawDisagreement.challengeId))) &&
    rawDisagreement.displayCard !== false
      ? rawDisagreement
      : null;

  const body = assistantBodyWithoutCues(localizedContent, uiCues, question);
  const isTranslatedDisplay = Boolean(translation && !proposal);

  const offer = guidance.proposalOffer;
  const hasOffer = Boolean(
    offer && (offer.proposalPresentation?.schemaVersion === 2 || offer.summary),
  );
  const secondaryCues = uiCues.filter(
    (cue) =>
      cue &&
      (cue.type === "manual_edit" ||
        cue.type === "warning" ||
        cue.type === "tradeoff" ||
        cue.type === "clarification") &&
      cue.text,
  );
  const hasPrimaryCue =
    hasOffer || Boolean(proposal && proposal.summary) || Boolean(guidance.intentHypothesis);

  return (
    <>
      <CoordinateRouteText turnId={turn.turnId} body={body} links={guidance.coordinateLinks} />

      {isTranslatedDisplay ? (
        <small class="translation-label">{t(lang, "translatedDisplay")}</small>
      ) : null}

      {hasPrimaryCue || secondaryCues.length > 0 ? (
        <div class="guidance-cues">
          {hasOffer && offer ? (
            <RevisionOfferCard offer={offer} turn={turn} />
          ) : proposal && proposal.summary ? (
            <section class="guidance-cue guidance-cue-revision">
              <span class="guidance-cue-label">{t(lang, "cue.revision")}</span>
              <strong>{formatVerifiedProposalSummary(lang, proposal.diff)}</strong>
            </section>
          ) : guidance.intentHypothesis ? (
            <IntentCard
              text={guidance.intentHypothesis}
              intentState={guidance.intentState}
              turn={turn}
            />
          ) : null}

          {secondaryCues.map((cue, idx) => {
            const displayType: GuidanceCueType = cue.type === "tradeoff" ? "warning" : cue.type;
            return (
              <section
                key={`${displayType}:${idx}`}
                class={`guidance-cue guidance-cue-${displayType.replace("_", "-")}`}
              >
                <span class="guidance-cue-label">{t(lang, cueLabelKey(displayType))}</span>
                <strong>{cue.text}</strong>
              </section>
            );
          })}
        </div>
      ) : null}

      {activeDisagreement ? (
        <DisagreementCard
          disagreement={activeDisagreement}
          challengeState={guidance.challengeState}
        />
      ) : question && !guidance.discussionCardMode ? (
        <section class="discussion-focus">
          <span class="discussion-focus-label">{t(lang, "cue.question")}</span>
          <strong>{question}</strong>
        </section>
      ) : null}
    </>
  );
}
