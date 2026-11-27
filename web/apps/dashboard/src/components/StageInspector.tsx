import { SokobanMap } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useId, useState } from "preact/hooks";
import {
  buildInspectorRecordRows,
  clean,
  dashboardNodeLabel,
  dashboardNodeStatus,
  findOpeningForStage,
  formatClockDuration,
  formatMapDiffCell,
  formatMapDiffEntryText,
  formatMode,
  formatNodeMetrics,
  formatSeconds,
  formatSolverSummary,
  formatTimestamp,
  openingIsConsumed,
  titleCase,
  value,
} from "../format";
import type { DashboardStore } from "../store";
import type {
  CoCreationFlowEvent,
  DraftRegeneration,
  FlowCard,
  MapDiffCell,
  MatchRecord,
  PlayAttempt,
  SolverMetrics,
  TimelineStage,
} from "../types";
import { CopyIdControl } from "./CopyIdControl";

export interface StageInspectorProps {
  store: DashboardStore;
}

type InspectorTab = "details" | "record";

const NODE_ENTRY_LABELS: Record<string, string> = {
  player_message: "Player",
  llm_message: "LLM",
  card: "Card",
  player_action: "Player action",
  map_diff: "Verified map change",
  status: "Status",
};

function renderCards(
  cards: readonly FlowCard[] | null | undefined,
  title = "Cards",
): JSX.Element | null {
  const safeCards = cards ?? [];
  if (safeCards.length === 0) return null;
  return (
    <div class="inspector-subsection">
      <h4>{title}</h4>
      {safeCards.map((card, index) => (
        <p key={index}>
          [{value(card.type)}] {value(card.text)}
        </p>
      ))}
    </div>
  );
}

function renderSolverMetrics(
  solver: SolverMetrics | null | undefined,
): JSX.Element | null {
  if (!solver) return null;
  const rows: Array<[string, string]> = [
    ["Solvable", solver.solvable ? "Yes" : "No"],
    ["Min steps", value(solver.minSteps)],
    ["Min pushes", value(solver.minPushes)],
  ];
  return (
    <div class="inspector-subsection">
      <h4>Solver metrics</h4>
      <div class="record-grid">
        {rows.map(([label, rowValue]) => (
          <div class="record-row" key={label}>
            <span>{label}</span>
            <strong>{rowValue}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

function renderMapDiff(
  diff: readonly MapDiffCell[] | null | undefined,
  eventType: string,
): JSX.Element | null {
  if (!Array.isArray(diff)) return null;
  if (diff.length === 0 && eventType === "first_stage") return null;

  return (
    <div class="inspector-subsection">
      <h4>
        Map diff ({diff.length} {diff.length === 1 ? "cell" : "cells"})
      </h4>
      {diff.length === 0 ? (
        <p class="stage-meta-note">No cell changes from previous stage.</p>
      ) : (
        <ul class="diff-list">
          {diff.map((cell, index) => (
            <li class="diff-chip" key={`${cell.x}-${cell.y}-${index}`}>
              {formatMapDiffCell(cell)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function renderPlayAttempts(
  attempts: readonly PlayAttempt[] | null | undefined,
): JSX.Element | null {
  const list = attempts ?? [];
  if (list.length === 0) return null;

  return (
    <div class="inspector-subsection">
      <h4>Play attempts ({list.length})</h4>
      {list.map((attempt, index) => {
        const rows: Array<[string, string]> = [
          ["Status", titleCase(attempt.status)],
          ["Duration", formatSeconds(attempt.durationSeconds)],
          ["Moves", value(attempt.moveCount)],
          ["Pushes", value(attempt.pushCount)],
          ["Restarts", value(attempt.restartCount)],
        ];
        if (
          attempt.minimumMoves !== null &&
          attempt.minimumMoves !== undefined
        ) {
          rows.push([
            "Minimum",
            `${value(attempt.minimumMoves)} moves / ${value(attempt.minimumPushes)} pushes`,
          ]);
        }

        return (
          <article class="node-entry" key={attempt.attemptId || index}>
            <h4>
              Attempt {index + 1} · {titleCase(attempt.status)}
            </h4>
            <div class="record-grid">
              {rows.map(([label, rowValue]) => (
                <div class="record-row" key={label}>
                  <span>{label}</span>
                  <strong>{rowValue}</strong>
                </div>
              ))}
            </div>
            <small>{formatTimestamp(attempt.finishedAt || attempt.issuedAt)}</small>
          </article>
        );
      })}
    </div>
  );
}

function renderDraftRegenerations(
  regenerations: readonly DraftRegeneration[] | null | undefined,
): JSX.Element | null {
  const list = regenerations ?? [];
  if (list.length === 0) return null;

  return (
    <div class="inspector-subsection">
      <h4>Draft regenerations ({list.length})</h4>
      {list.map((regen, index) => {
        const rows: Array<[string, string]> = [
          ["Request ID", value(regen.requestId)],
          ["Status", titleCase(regen.status)],
        ];
        if (regen.failureCode) {
          rows.push(["Failure code", regen.failureCode]);
        }
        if (regen.solver) {
          rows.push(["Solver", formatSolverSummary(regen.solver)]);
        }

        return (
          <article class="node-entry" key={regen.requestId || index}>
            <h4>
              Generation {value(regen.generation)} · {titleCase(regen.status)}
            </h4>
            <div class="record-grid">
              {rows.map(([label, rowValue]) => (
                <div class="record-row" key={label}>
                  <span>{label}</span>
                  <strong>{rowValue}</strong>
                </div>
              ))}
            </div>
            <small>{formatTimestamp(regen.updatedAt || regen.requestedAt)}</small>
          </article>
        );
      })}
    </div>
  );
}

function DraftDetails({
  match,
  record,
}: {
  match: MatchRecord;
  record: CoCreationFlowEvent;
}): JSX.Element {
  const player = (match.players ?? []).find(
    (item) => item.playerNumber === record.playerNumber,
  );
  const regenerations =
    record.draftRegenerations ?? player?.draftRegenerations ?? [];

  const rows: Array<[string, string]> = [
    ["Metadata complete", record.draftMetadataComplete ? "Yes" : "No"],
    ["Q1 answer", value(record.q1Answer)],
    ["Q2 answer", value(record.q2Answer)],
    ["Q3 answer", value(record.q3Answer)],
    ["Q4 answer", value(record.q4Answer)],
    ["AI recommended difficulty", value(record.aiRecommendedDifficulty)],
    ["AI recommended layout", value(record.aiRecommendedLayout)],
    ["AI recommendation source", value(record.aiRecommendationSource)],
    ["User final difficulty", value(record.finalDifficulty)],
    ["User final layout", value(record.finalLayout)],
  ];

  return (
    <section class="record-section">
      <h3>Draft research record</h3>
      <div class="record-grid">
        {rows.map(([label, rowValue]) => (
          <div class="record-row" key={label}>
            <span>{label}</span>
            <strong>{rowValue}</strong>
          </div>
        ))}
      </div>
      <div class="inspector-subsection">
        <h4>AI Reflection</h4>
        <p>{value(record.aiReflection)}</p>
        <h4>AI difficulty rationale</h4>
        <p>{value(record.aiDifficultyRationale)}</p>
        <h4>AI layout rationale</h4>
        <p>{value(record.aiLayoutRationale)}</p>
      </div>
      {renderDraftRegenerations(regenerations)}
    </section>
  );
}

function FlowStageDetails({
  match,
  record,
}: {
  match: MatchRecord;
  record: CoCreationFlowEvent;
}): JSX.Element {
  const titles: Record<string, string> = {
    first_stage: "First Stage map",
    final: "Final map",
    stage: "Stage change",
  };
  const sourceLabel =
    record.source === "restored"
      ? "Restored"
      : record.source === "ai"
        ? "AI-assisted"
        : "Manual";
  const rows = record.rows ?? [];
  const opening = findOpeningForStage(match, record);
  const visibleOpening =
    opening && !openingIsConsumed(match, opening) ? opening : null;

  return (
    <section class="record-section">
      <h3>{titles[record.eventType] || "Stage map"}</h3>
      {record.source ? <p class="stage-meta-note">Source: {sourceLabel}</p> : null}
      {record.source === "restored" ? (
        <p class="stage-meta-note">
          Restored from Stage {value(record.restoredFromStageNumber)} · replaced
          Stage {value(record.replacedStageNumber)}
        </p>
      ) : null}
      {record.initialDraftMethod ? (
        <p class="stage-meta-note">
          Initial draft: {formatMode(record.initialDraftMethod)}
        </p>
      ) : null}
      {record.eventType === "final" ? (
        <p class="stage-meta-note">
          Co-creation time: {formatClockDuration(record.coCreationDurationSeconds)}
          {" · "}Opponent restarts: {value(record.opponentRestartCount)}
        </p>
      ) : null}

      <div class="sokoban-map-frame mini-map-frame">
        {rows.length > 0 ? (
          <SokobanMap
            rows={rows}
            mini
            mode="readonly"
            label={`${titles[record.eventType] || "Stage"} preview map`}
          />
        ) : (
          <div class="map-empty">No map rows recorded.</div>
        )}
      </div>

      {renderSolverMetrics(record.solver)}
      {renderMapDiff(record.diff, record.eventType)}
      {renderPlayAttempts(record.playAttempts)}

      {visibleOpening ? (
        <div class="inspector-subsection">
          <h4>AI opening evaluation</h4>
          <p>{visibleOpening.assistantText || "-"}</p>
          {renderCards(visibleOpening.cards, "Opening cards")}
        </div>
      ) : null}
    </section>
  );
}

function DashboardNodeDetails({
  store,
  record,
}: {
  store: DashboardStore;
  record: CoCreationFlowEvent;
}): JSX.Element {
  const metrics = formatNodeMetrics(record);
  const entries = (record.nodeEntries ?? [])
    .slice()
    .sort((left, right) =>
      clean(left.occurredAt).localeCompare(clean(right.occurredAt)),
    );

  const playerNodeStages: TimelineStage[] = store.timelineStages.value.filter(
    (item) =>
      item.type === "flow" &&
      item.playerNumber === record.playerNumber &&
      item.record.eventType === "node",
  );

  const parentStage = record.nodeParentId
    ? playerNodeStages.find(
        (item) =>
          (item.record as CoCreationFlowEvent).nodeId === record.nodeParentId,
      ) ?? null
    : null;

  const childStages = record.nodeId
    ? playerNodeStages.filter(
        (item) =>
          (item.record as CoCreationFlowEvent).nodeParentId === record.nodeId,
      )
    : [];

  return (
    <section class="record-section">
      <h3>{dashboardNodeLabel(record.nodeType)}</h3>
      <div class="record-grid">
        {metrics.map(([label, rowValue]) => (
          <div class="record-row" key={label}>
            <span>{label}</span>
            <strong>{value(rowValue)}</strong>
          </div>
        ))}
      </div>

      {parentStage ? (
        <div class="inspector-subsection">
          <h4>Parent node</h4>
          <div class="node-relation-list">
            <button
              type="button"
              class="node-relation-btn"
              onClick={() => store.selectStage(parentStage)}
            >
              <strong>{parentStage.label}</strong>
              <span>
                {dashboardNodeStatus(
                  (parentStage.record as CoCreationFlowEvent).nodeStatus,
                )}
              </span>
              <code>{(parentStage.record as CoCreationFlowEvent).nodeId}</code>
            </button>
          </div>
        </div>
      ) : null}

      {childStages.length > 0 ? (
        <div class="inspector-subsection">
          <h4>Follow-up / Child nodes ({childStages.length})</h4>
          <div class="node-relation-list">
            {childStages.map((childStage) => {
              const childNode = childStage.record as CoCreationFlowEvent;
              return (
                <button
                  key={childStage.key}
                  type="button"
                  class="node-relation-btn"
                  onClick={() => store.selectStage(childStage)}
                >
                  <strong>{childStage.label}</strong>
                  <span>{dashboardNodeStatus(childNode.nodeStatus)}</span>
                  <code>{childNode.nodeId}</code>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      {entries.length === 0 ? (
        <div class="empty-state compact-empty">
          No public node entries recorded.
        </div>
      ) : (
        entries.map((entry, index) => {
          const entryText =
            entry.kind === "map_diff"
              ? formatMapDiffEntryText(entry.text)
              : entry.text || "-";
          return (
            <article
              class="node-entry"
              key={`${entry.entryId ?? entry.occurredAt ?? ""}-${index}`}
            >
              <h4>
                {entry.label ||
                  (entry.kind && NODE_ENTRY_LABELS[entry.kind]) ||
                  "Entry"}
              </h4>
              <p>{entryText}</p>
              <small>{formatTimestamp(entry.occurredAt)}</small>
            </article>
          );
        })
      )}
    </section>
  );
}

function MessageDetails({ record }: { record: CoCreationFlowEvent }): JSX.Element {
  return (
    <section class="record-section">
      <h3>Message to opponent</h3>
      <p>{record.message || "-"}</p>
    </section>
  );
}

export function StageInspector({ store }: StageInspectorProps): JSX.Element {
  const match = store.selectedMatch.value;
  const stage = store.activeStage.value;
  const [activeTab, setActiveTab] = useState<InspectorTab>("details");
  const idPrefix = useId();

  useEffect(() => {
    setActiveTab("details");
  }, [stage?.key]);

  const tabs: ReadonlyArray<{ id: InspectorTab; label: string }> = [
    { id: "details", label: "Stage details" },
    { id: "record", label: "Record & IDs" },
  ];

  const handleTabKeyDown = (event: KeyboardEvent, index: number) => {
    let nextIndex = index;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    setActiveTab(tabs[nextIndex].id);
    const list = (event.currentTarget as HTMLElement).parentElement;
    const nextTab = list?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[
      nextIndex
    ];
    nextTab?.focus();
  };

  const renderRecordGrid = () => {
    if (!match || !stage) return null;
    const rows = buildInspectorRecordRows(match, stage.record);
    return (
      <section class="record-section">
        <h3>Record details</h3>
        <div class="record-grid">
          {rows.map((row) => (
            <div class="record-row" key={row.label}>
              <span>{row.label}</span>
              {row.copyable ? (
                <CopyIdControl label={row.label} rawValue={row.value} />
              ) : (
                <strong>{value(row.value)}</strong>
              )}
            </div>
          ))}
        </div>
      </section>
    );
  };

  const renderStageSpecificContent = () => {
    if (!match || !stage) return null;
    const record = stage.record as CoCreationFlowEvent;

    if (record.eventType === "draft") {
      return <DraftDetails match={match} record={record} />;
    }
    if (
      record.eventType === "first_stage" ||
      record.eventType === "stage" ||
      record.eventType === "final"
    ) {
      return <FlowStageDetails match={match} record={record} />;
    }
    if (record.eventType === "node") {
      return <DashboardNodeDetails store={store} record={record} />;
    }
    if (record.eventType === "message") {
      return <MessageDetails record={record} />;
    }
    return renderRecordGrid();
  };

  return (
    <aside class="stone-panel inspector-panel">
      <div class="stone-panel-header compact-header">
        <div>
          <p class="eyebrow">Selected stage</p>
          <h2>{stage ? stage.label : "Match details"}</h2>
        </div>
      </div>

      {stage && match ? (
        <>
          <div
            class="inspector-tabs"
            role="tablist"
            aria-label="Stage inspector views"
          >
            {tabs.map((tab, index) => {
              const isSelected = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  id={`${idPrefix}-tab-${tab.id}`}
                  type="button"
                  role="tab"
                  class={`inspector-tab${isSelected ? " selected" : ""}`}
                  aria-selected={isSelected ? "true" : "false"}
                  aria-controls={`${idPrefix}-panel-${tab.id}`}
                  tabIndex={isSelected ? 0 : -1}
                  onClick={() => setActiveTab(tab.id)}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>

          <div
            id={`${idPrefix}-panel-${activeTab}`}
            class="inspector-body"
            role="tabpanel"
            aria-labelledby={`${idPrefix}-tab-${activeTab}`}
          >
            {activeTab === "details"
              ? renderStageSpecificContent()
              : renderRecordGrid()}
          </div>
        </>
      ) : (
        <div class="inspector-body">
          <div class="empty-state inspector-empty">
            Select a timeline stage.
          </div>
        </div>
      )}
    </aside>
  );
}
