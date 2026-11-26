import {
  buildEntityLabels,
  formatEntityCoordinate,
  getTileClass,
} from "@sokoban/ui-core";
import type { JSX } from "preact";
import { t, tileDisplayKey } from "../../i18n";
import {
  busy,
  canEditSelected,
  currentPendingProposal,
  language,
  unityPlayActive,
} from "../../store/sessionStore";
import { decideProposal } from "../../store/workflows";
import type { TileDiff } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

export function formatVerifiedProposalSummary(
  lang: "en" | "zh-CN",
  diff: readonly TileDiff[],
): string {
  const changes = Array.isArray(diff) ? diff : [];
  if (changes.length === 0) {
    return t(lang, "verifiedProposalNoChanges");
  }

  const nameOf = (tile: string) => t(lang, tileDisplayKey(tile));

  if (changes.length <= 8) {
    const sep = lang === "zh-CN" ? "；" : "; ";
    const details = changes
      .map((change) =>
        t(lang, "verifiedProposalCellDetail", {
          row: change.y + 1,
          column: change.x + 1,
          before: nameOf(change.before),
          after: nameOf(change.after),
        }),
      )
      .join(sep);
    return t(lang, "verifiedProposalSummaryShort", { count: changes.length, details });
  }

  const counts = new Map<string, number>();
  for (const change of changes) {
    const key = `${change.before}\u0000${change.after}`;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const sep = lang === "zh-CN" ? "；" : "; ";
  const details = Array.from(counts.entries())
    .map(([key, count]) => {
      const [before, after] = key.split("\u0000");
      return t(lang, "verifiedProposalTransitionCount", {
        before: nameOf(before),
        after: nameOf(after),
        count,
      });
    })
    .join(sep);
  return t(lang, "verifiedProposalSummaryLong", { count: changes.length, details });
}

export function ProposalCard(): JSX.Element | null {
  if (!canEditSelected.value) return null;
  const proposal = currentPendingProposal.value;
  if (!proposal) return null;

  const lang = language.value;
  const disabled = busy.value || unityPlayActive.value;
  const summary = formatVerifiedProposalSummary(lang, proposal.diff);
  const colon = lang === "zh-CN" ? "：" : ": ";
  const entityLabels = buildEntityLabels(proposal.proposedRows);
  const changedSet = new Set((proposal.diff || []).map((item) => `${item.x},${item.y}`));

  return (
    <div class="proposal-area">
      <section class="proposal-card">
        <div class="proposal-heading">
          <div>
            <p class="eyebrow">{t(lang, "proposal")}</p>
            <h3>{summary || t(lang, "proposal")}</h3>
          </div>
          <span>
            {proposal.diff.length} {t(lang, "changedTiles")}
          </span>
        </div>
        <p>{t(lang, "proposalValid")}</p>
        <ul class="proposal-plan-detail-list">
          {(proposal.diff || []).map((change, idx) => (
            <li key={`${change.x},${change.y},${idx}`} class="proposal-plan-detail-item">
              {`(${change.y + 1}, ${change.x + 1})${colon}${t(lang, tileDisplayKey(change.before))} (${change.before}) → ${t(lang, tileDisplayKey(change.after))} (${change.after})`}
            </li>
          ))}
        </ul>
        <div class="mini-map proposal-map" style={{ gridTemplateColumns: "repeat(12, 1fr)" }}>
          {proposal.proposedRows.map((row, y) =>
            [...row].map((tile, x) => {
              const coordKey = `${x},${y}`;
              const entity = entityLabels.get(coordKey);
              return (
                <i
                  key={coordKey}
                  class={`${getTileClass(tile)}${changedSet.has(coordKey) ? " changed" : ""}`}
                  data-entity-id={entity}
                  role={entity ? "img" : undefined}
                  aria-label={entity ? formatEntityCoordinate(entity, x, y) : undefined}
                />
              );
            }),
          )}
        </div>
        <div class="proposal-actions">
          <button
            type="button"
            class="secondary-button"
            disabled={disabled}
            aria-disabled={disabled ? "true" : undefined}
            onClick={() => void decideProposal(proposal, "reject")}
            {...tooltipHandlers({
              kind: "action",
              helpKey: "reject",
              titleKey: "reject",
            })}
          >
            {t(lang, "reject")}
          </button>
          <button
            type="button"
            class="primary-button"
            disabled={disabled}
            aria-disabled={disabled ? "true" : undefined}
            onClick={() => void decideProposal(proposal, "accept")}
            {...tooltipHandlers({
              kind: "action",
              helpKey: "accept",
              titleKey: "accept",
            })}
          >
            {t(lang, "accept")}
          </button>
        </div>
      </section>
    </div>
  );
}
