import type { JSX } from "preact";
import { t, tileDisplayKey, type TranslationKey } from "../../i18n";
import {
  binaryFlowActive,
  busy,
  canEditSelected,
  language,
  latestRevisionOfferTurnId,
  selectedStageHasActiveDisagreement,
  unityPlayActive,
} from "../../store/sessionStore";
import { proposalStateLabel, sendRevisionCardAction } from "../../store/workflows";
import type {
  ProposalOffer,
  ProposalPresentationV2,
  RevisionCardAction,
  Turn,
} from "../../types";
import { tooltipHandlers, type ActionHelpKey } from "../Common/Tooltip";

export interface RevisionOfferCardProps {
  offer: ProposalOffer;
  turn: Turn;
}

function tilePairLabel(lang: "en" | "zh-CN", tile: string): string {
  return `${t(lang, tileDisplayKey(tile))} (${tile})`;
}

function PresentationV2Full({
  presentation,
}: {
  presentation: ProposalPresentationV2;
}): JSX.Element {
  const lang = language.value;
  const colon = lang === "zh-CN" ? "：" : ":";

  const renderSection = (labelKey: TranslationKey, rawValues: string | string[] | undefined) => {
    const items = (Array.isArray(rawValues) ? rawValues : rawValues ? [rawValues] : [])
      .map((value) => String(value ?? "").trim())
      .filter(Boolean);
    if (items.length === 0) return null;
    return (
      <section class="proposal-v2-section">
        <strong class="proposal-v2-title">{`${t(lang, labelKey)}${colon}`}</strong>
        <ul class="proposal-v2-list">
          {items.map((item, idx) => (
            <li key={idx} class="proposal-v2-item">
              {item}
            </li>
          ))}
        </ul>
      </section>
    );
  };

  const changeLines = (Array.isArray(presentation.changes) ? presentation.changes : [])
    .filter((change) => Number.isInteger(change?.row) && Number.isInteger(change?.column))
    .map(
      (change) =>
        `(${change.row}, ${change.column})${colon} ${tilePairLabel(lang, change.before)} → ${tilePairLabel(lang, change.after)}`,
    );

  const sol = presentation.solution;
  const solutionText =
    sol && Number.isInteger(sol.minimumMoves) && Number.isInteger(sol.pushesOnMinimumMoveSolution)
      ? t(lang, "proposalSolutionStats", {
          moves: sol.minimumMoves,
          pushes: sol.pushesOnMinimumMoveSolution,
        })
      : undefined;

  return (
    <div class="proposal-plan-details proposal-plan-details-v2">
      {renderSection("proposalRequestText", presentation.requestText)}
      {renderSection("proposalAnswerTexts", presentation.answerTexts)}
      {renderSection("proposalChangeList", changeLines)}
      {renderSection("proposalReason", presentation.reason)}
      {renderSection("proposalSolution", solutionText)}
    </div>
  );
}

function PresentationV2({ presentation }: { presentation: ProposalPresentationV2 }): JSX.Element {
  const lang = language.value;
  if (!presentation.binaryCompact) {
    return <PresentationV2Full presentation={presentation} />;
  }

  const groups = new Map<string, number>();
  for (const change of presentation.changes || []) {
    const label = `${tilePairLabel(lang, change.before)} → ${tilePairLabel(lang, change.after)}`;
    groups.set(label, (groups.get(label) || 0) + 1);
  }
  const summaryLine = Array.from(groups.entries())
    .map(([label, count]) => t(lang, "proposalTileChangeCount", { label, count }))
    .join("; ");

  const verifiedActions = presentation.verifiedActions || [];

  return (
    <div class="proposal-plan-details proposal-plan-details-v2">
      <strong>{t(lang, "proposalLocalPlanTitle")}</strong>
      {summaryLine ? <p>{summaryLine}</p> : null}
      {verifiedActions.length > 0 ? (
        <p>{t(lang, "proposalSelectedActions", { actions: verifiedActions.join(" / ") })}</p>
      ) : null}
      {presentation.reason ? <p>{presentation.reason}</p> : null}
      <p>{t(lang, "proposalBinaryVerified")}</p>
      <details>
        <summary>{t(lang, "proposalTileChangeDetailsSummary")}</summary>
        <PresentationV2Full presentation={{ ...presentation, binaryCompact: false }} />
      </details>
    </div>
  );
}

const REVISION_ACTIONS: ReadonlyArray<{
  action: RevisionCardAction;
  labelKey: TranslationKey;
  helpKey: ActionHelpKey;
}> = [
  {
    action: "execute_revision",
    labelKey: "draftSuggestedRevision",
    helpKey: "draftSuggestedRevision",
  },
  {
    action: "challenge_revision",
    labelKey: "challengeRevision",
    helpKey: "challengeRevision",
  },
  {
    action: "alternative_revision",
    labelKey: "alternativeRevision",
    helpKey: "alternativeRevision",
  },
];

/**
 * Revision offer card supporting v2 structured presentation only.
 */
export function RevisionOfferCard({ offer, turn }: RevisionOfferCardProps): JSX.Element | null {
  const lang = language.value;
  const presentation = offer.proposalPresentation?.schemaVersion === 2
    ? offer.proposalPresentation
    : null;
  const summary = presentation?.summary || offer.summary || "";
  const rationale = presentation?.rationale || offer.rationale || "";

  if (!presentation && !summary) return null;

  const isLatest = latestRevisionOfferTurnId.value === turn.turnId;
  const proposalStatus = turn.proposalState?.status || (isLatest ? "active" : "stale");
  const actionable =
    proposalStatus === "active" && isLatest && turn.proposalState?.actionable !== false;

  const canShowButtons =
    canEditSelected.value &&
    !selectedStageHasActiveDisagreement.value &&
    proposalStatus !== "already_satisfied" &&
    proposalStatus !== "unbound";

  const available = REVISION_ACTIONS.filter(
    (entry) =>
      !Array.isArray(offer.availableActions) || offer.availableActions.includes(entry.action),
  );

  const interactionDisabled =
    !actionable || busy.value || unityPlayActive.value || binaryFlowActive.value;
  const statusNote = proposalStateLabel(proposalStatus);

  return (
    <section class="guidance-cue guidance-cue-revision">
      <span class="guidance-cue-label">{t(lang, "cue.revision")}</span>
      {!presentation && summary ? <strong>{summary}</strong> : null}
      {!presentation && rationale ? <p>{rationale}</p> : null}
      {presentation ? <PresentationV2 presentation={presentation} /> : null}

      {canShowButtons
        ? available.map(({ action, labelKey, helpKey }) => (
            <button
              key={action}
              type="button"
              class={`secondary-button guidance-cue-button revision-action-button${!actionable ? " guidance-cue-button-stale" : ""}`}
              disabled={interactionDisabled}
              aria-disabled={interactionDisabled ? "true" : undefined}
              onClick={() => {
                if (!interactionDisabled) sendRevisionCardAction(action, turn);
              }}
              {...tooltipHandlers({
                kind: "action",
                helpKey,
                titleKey: labelKey,
                status: !actionable ? statusNote : undefined,
              })}
            >
              {t(lang, labelKey)}
            </button>
          ))
        : null}

      {canShowButtons && !actionable ? (
        <small class="guidance-cue-stale-note">{statusNote}</small>
      ) : null}

      {proposalStatus === "already_satisfied" || proposalStatus === "unbound" ? (
        <small class="guidance-cue-stale-note">{statusNote}</small>
      ) : null}
    </section>
  );
}
