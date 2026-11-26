import { SokobanMap } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useId, useState } from "preact/hooks";
import {
  buildInspectorRecordRows,
  clean,
  dashboardNodeLabel,
  findOpeningForStage,
  formatMode,
  formatNodeMetrics,
  formatTimestamp,
  openingIsConsumed,
  value,
} from "../format";
import type { DashboardStore } from "../store";
import type { CoCreationFlowEvent, FlowCard, MatchRecord } from "../types";
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

function DraftDetails({ record }: { record: CoCreationFlowEvent }): JSX.Element {
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
  const visibleOpening = opening && !openingIsConsumed(match, opening) ? opening : null;

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
  record,
}: {
  record: CoCreationFlowEvent;
}): JSX.Element {
  const metrics = formatNodeMetrics(record);
  const entries = (record.nodeEntries ?? [])
    .slice()
    .sort((left, right) =>
      clean(left.occurredAt).localeCompare(clean(right.occurredAt)),
    );

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

      {entries.length === 0 ? (
        <div class="empty-state compact-empty">
          No public node entries recorded.
        </div>
      ) : (
        entries.map((entry, index) => (
          <article class="node-entry" key={`${entry.occurredAt ?? ""}-${index}`}>
            <h4>
              {entry.label ||
                (entry.kind && NODE_ENTRY_LABELS[entry.kind]) ||
                "Entry"}
            </h4>
            <p>{entry.text || "-"}</p>
            <small>{formatTimestamp(entry.occurredAt)}</small>
          </article>
        ))
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
      return <DraftDetails record={record} />;
    }
    if (
      record.eventType === "first_stage" ||
      record.eventType === "stage" ||
      record.eventType === "final"
    ) {
      return <FlowStageDetails match={match} record={record} />;
    }
    if (record.eventType === "node") {
      return <DashboardNodeDetails record={record} />;
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
