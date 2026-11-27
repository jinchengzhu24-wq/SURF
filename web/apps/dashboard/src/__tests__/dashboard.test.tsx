import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { createDashboardApi, resolveApiBase } from "../api";
import {
  buildTimelineStages,
  dashboardNodeStatus,
  flowStageLabel,
  formatChallengeMetrics,
  formatChallengeOutcome,
  formatClockDuration,
  hasFirstStage,
  isCompletedMatch,
  openingIsConsumed,
} from "../format";
import { createDashboardStore } from "../store";
import {
  DraftRegenerationSchema,
  MapDiffCellSchema,
  MatchmakingRecordsPayloadSchema,
  PlayAttemptSchema,
  SolverMetricsSchema,
  SurveyAnalyticsResponseSchema,
  type MatchmakingRecordsResponse,
  type SurveyAnalyticsResponse,
} from "../types";
import contractFixture from "./fixtures/matchmaking_projection.contract.json";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function cloneContractPayload(): MatchmakingRecordsResponse {
  return MatchmakingRecordsPayloadSchema.parse(
    structuredClone(contractFixture),
  );
}

function createMultiMatchPayloadFromContract(): MatchmakingRecordsResponse {
  const base = cloneContractPayload();
  const primary = base.matches[0];
  const inProgressClone = structuredClone(primary);
  inProgressClone.matchId = "00000002202640008000000000000002";
  inProgressClone.roomCode = "BETA99";
  inProgressClone.status = "in_progress";
  inProgressClone.completedAt = null;
  inProgressClone.endedAt = null;
  inProgressClone.players[0].studySessionId = "study-beta-p1-uuid";
  inProgressClone.players[0].result = null;
  inProgressClone.players[1].studySessionId = "study-beta-p2-uuid";
  inProgressClone.players[1].result = null;

  const hiddenClone = structuredClone(primary);
  hiddenClone.matchId = "00000003202640008000000000000003";
  hiddenClone.roomCode = "HIDE00";
  hiddenClone.status = "in_progress";
  hiddenClone.players = [
    {
      ...hiddenClone.players[0],
      studySessionId: "study-hidden-p1",
      coCreationFlow: [],
      result: null,
    },
  ];

  return {
    ...base,
    matches: [primary, inProgressClone, hiddenClone],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("backend projection contract schema & formatters", () => {
  it("strictly parses the real backend-generated contract fixture with explicit schemas", () => {
    const parsed = MatchmakingRecordsPayloadSchema.parse(contractFixture);
    expect(parsed.matches).toHaveLength(1);

    const match = parsed.matches[0];
    expect(match.roomCode).toBe("CONT2A");
    const [p1, p2] = match.players;

    // Zero raw turn events in authoritative projection
    expect(
      [...p1.coCreationFlow, ...p2.coCreationFlow].some(
        (ev) => ev.eventType === "turn",
      ),
    ).toBe(false);

    // Player-level playAttempts & draftRegenerations
    expect(p1.playAttempts).toHaveLength(2);
    expect(p1.draftRegenerations).toHaveLength(2);
    expect(p2.playAttempts).toHaveLength(0);
    expect(p2.draftRegenerations).toHaveLength(0);
    for (const attempt of p1.playAttempts) {
      expect(() => PlayAttemptSchema.parse(attempt)).not.toThrow();
    }
    for (const regen of p1.draftRegenerations) {
      expect(() => DraftRegenerationSchema.parse(regen)).not.toThrow();
    }

    // Flow-level typed fields: draftRegenerations, solver, diff, playAttempts, consumed, cards
    const p1Draft = p1.coCreationFlow.find((ev) => ev.eventType === "draft");
    expect(p1Draft?.draftRegenerations).toHaveLength(2);

    const p1Stage5 = p1.coCreationFlow.find(
      (ev) => ev.eventType === "stage" && ev.stageNumber === 5,
    );
    expect(SolverMetricsSchema.parse(p1Stage5?.solver)).toEqual({
      solvable: true,
      minSteps: 5,
      minPushes: 3,
    });
    expect(p1Stage5?.diff).toHaveLength(1);
    expect(MapDiffCellSchema.parse(p1Stage5?.diff?.[0])).toEqual({
      x: 5,
      y: 4,
      before: "@",
      after: ".",
    });
    expect(p1Stage5?.playAttempts).toHaveLength(1);

    const p1Opening = p1.coCreationFlow.find((ev) => ev.eventType === "opening");
    const p2Opening = p2.coCreationFlow.find((ev) => ev.eventType === "opening");
    expect(p1Opening?.consumed).toBe(true);
    expect(p1Opening?.cards).toHaveLength(2);
    expect(p2Opening?.consumed).toBe(false);
    expect(p2Opening?.cards).toHaveLength(2);
  });

  it("resolves API base safely while blocking cross-origin overrides", () => {
    expect(
      resolveApiBase({
        search: "?api=https://evil.example/api",
        protocol: "https:",
        origin: "https://sokobanaidemo.top",
        hostname: "sokobanaidemo.top",
      }),
    ).toBe("https://sokobanaidemo.top");

    expect(
      resolveApiBase({
        search: "?api=https://sokobanaidemo.top/v2/",
        protocol: "https:",
        origin: "https://sokobanaidemo.top",
        hostname: "sokobanaidemo.top",
      }),
    ).toBe("https://sokobanaidemo.top/v2");

    expect(
      resolveApiBase({
        search: "?api=http://127.0.0.1:8000/",
        protocol: "file:",
        origin: "null",
        hostname: "",
      }),
    ).toBe("http://127.0.0.1:8000");
  });

  it("formats durations >1200s without clamping and maps all authoritative node statuses", () => {
    // >1200s durations must NOT clamp to 20:00
    expect(formatClockDuration(1470)).toBe("24:30");
    expect(formatClockDuration(1850)).toBe("30:50");
    expect(formatClockDuration(1500)).toBe("25:00");
    expect(formatClockDuration(3661)).toBe("1:01:01");
    expect(formatClockDuration(425)).toBe("07:05");
    expect(formatClockDuration(0)).toBe("00:00");
    expect(formatClockDuration(-1)).toBe("-");

    // All authoritative node statuses
    const expectedStatuses: Record<string, string> = {
      exited: "Exited",
      clarifying_1: "Proposal clarification 1/3",
      clarifying_2: "Proposal clarification 2/3",
      clarifying_3: "Proposal clarification 3/3",
      proposal_ready: "Proposal ready",
      cancelled: "Cancelled",
      retry_pending: "Retry pending",
      revision_needed: "Revision needed",
      accepted: "Accepted",
      rejected: "Rejected",
      replaced: "Replaced",
      superseded: "Superseded",
      awaiting_reason: "Awaiting challenge reason",
      review_pending: "Reason review pending retry",
      awaiting_player_choice: "Awaiting player choice",
      original_selected: "Original proposal selected",
      replacement_selected: "New plan from the player's reason selected",
      in_progress: "In progress",
      acknowledged: "Responded · proposal block released",
      manual_reedit: "Ended through a new manual edit",
      confirmed: "Confirmed",
      revised: "Revised intention",
      reviewed: "Reviewed",
      resolved: "Resolved",
    };
    for (const [raw, expected] of Object.entries(expectedStatuses)) {
      expect(dashboardNodeStatus(raw)).toBe(expected);
    }

    expect(
      flowStageLabel({
        eventType: "stage",
        source: "restored",
        stageNumber: 6,
      }),
    ).toBe("Stage 6 · Restored");
    expect(formatChallengeOutcome("timed_out")).toBe("Timed out");

    const parsed = cloneContractPayload();
    const p1Challenge = parsed.matches[0].players[0].challenge!;
    const metrics = new Map(formatChallengeMetrics(p1Challenge));
    expect(metrics.get("Outcome")).toBe("Timed out");
    expect(metrics.get("Move overhead")).toBe("-");
  });

  it("evaluates openingIsConsumed using consumed flag and node openingAssistantTurnId", () => {
    const parsed = cloneContractPayload();
    const match = parsed.matches[0];
    const p1Opening = match.players[0].coCreationFlow.find(
      (ev) => ev.eventType === "opening",
    )!;
    const p2Opening = match.players[1].coCreationFlow.find(
      (ev) => ev.eventType === "opening",
    )!;

    // P1 opening is consumed (both consumed=true and referenced by discussion node)
    expect(openingIsConsumed(match, p1Opening)).toBe(true);

    // Even if consumed flag is omitted, node.openingAssistantTurnId still marks it consumed
    expect(
      openingIsConsumed(match, { ...p1Opening, consumed: undefined }),
    ).toBe(true);

    // P2 opening is NOT consumed
    expect(openingIsConsumed(match, p2Opening)).toBe(false);

    // Predicates on multi-match payload derived from contract fixture
    const multi = createMultiMatchPayloadFromContract();
    expect(hasFirstStage(multi.matches[0])).toBe(true);
    expect(hasFirstStage(multi.matches[2])).toBe(false);
    expect(isCompletedMatch(multi.matches[0])).toBe(true);
    expect(isCompletedMatch(multi.matches[1])).toBe(false);

    const stages = buildTimelineStages(match);
    expect(stages.some((s) => s.record.eventType === "opening")).toBe(false);
    expect(stages.some((s) => s.record.eventType === "turn")).toBe(false);
  });

  it("aborts an in-flight refresh when a newer refresh starts", async () => {
    const signals: AbortSignal[] = [];
    let resolveSecond!: (response: Response) => void;

    const fetchMock: typeof fetch = vi.fn((_input, init) => {
      const signal = init?.signal as AbortSignal;
      signals.push(signal);
      if (signals.length === 1) {
        return new Promise<Response>((_resolve, reject) => {
          signal.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        });
      }
      return new Promise<Response>((resolve) => {
        resolveSecond = resolve;
      });
    });

    const api = createDashboardApi({
      apiBase: "http://127.0.0.1:8000",
      fetchImpl: fetchMock,
    });
    const store = createDashboardStore(api, { debounceMs: 0 });

    const firstLoad = store.loadData(false);
    const secondLoad = store.loadData(true);

    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    resolveSecond(jsonResponse(createMultiMatchPayloadFromContract()));
    await Promise.all([firstLoad, secondLoad]);

    expect(store.loadError.value).toBeNull();
    expect(store.firstStageMatches.value).toHaveLength(2);
    store.dispose();
  });
});

describe("dashboard App UI against real backend contract fixture", () => {
  it("renders authoritative nodes, diff/solver/playAttempts/draftRegenerations, opening cards, >1200s duration, exports, and self-hosted survey empty copy", async () => {
    const payload = createMultiMatchPayloadFromContract();
    const fetchMock = vi.fn(async () => jsonResponse(payload));
    const api = createDashboardApi({
      apiBase: "https://sokobanaidemo.top",
      fetchImpl: fetchMock,
    });

    const fakeWindow = {
      location: {
        href: "https://sokobanaidemo.top/frontend/?access=1",
        origin: "https://sokobanaidemo.top",
        protocol: "https:",
        hostname: "sokobanaidemo.top",
        pathname: "/frontend/",
        search: "?access=1",
      },
      history: {
        replaceState() {
          throw new Error("Simulated SecurityError");
        },
      },
    } as unknown as Window;

    const store = createDashboardStore(api, {
      debounceMs: 0,
      windowObj: fakeWindow,
    });

    const {
      container,
      findByText,
      getByLabelText,
      getByRole,
      getByText,
      getAllByRole,
      queryByRole,
    } = render(<App store={store} />);

    await findByText("1 rooms without First Stage hidden");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // TopBar primary research export link
    const primaryExportLink = getByRole("link", {
      name: "Export Research (JSONL)",
    });
    expect(primaryExportLink.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/matchmaking-research.jsonl",
    );
    expect(primaryExportLink.getAttribute("download")).toBe(
      "matchmaking-research.jsonl",
    );

    // Developer tools menu has all three distinct export/raw links
    const toolsButton = getByRole("button", { name: "Developer tools" });
    fireEvent.click(toolsButton);
    const menu = getByRole("menu");
    const menuResearch = within(menu).getByRole("menuitem", {
      name: "Export Research (JSONL)",
    });
    const menuRaw = within(menu).getByRole("menuitem", {
      name: "Raw Room Events",
    });
    const menuTelemetry = within(menu).getByRole("menuitem", {
      name: "Study Telemetry (JSONL)",
    });
    const menuSurveysCsv = within(menu).getByRole("menuitem", {
      name: "Export Surveys (CSV)",
    });
    expect(menuResearch.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/matchmaking-research.jsonl",
    );
    expect(menuSurveysCsv.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/surveys.csv",
    );
    expect(menuSurveysCsv.getAttribute("download")).toBe("surveys.csv");
    expect(menuRaw.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/matchmaking-records",
    );
    expect(menuTelemetry.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/study-logs.jsonl",
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(queryByRole("menu")).toBeNull();

    // Active match is CONT2A; check timeline lanes for Player 1 & Player 2
    const activeMatchButton = getByRole("button", { name: /ROOM CONT2A/i });
    expect(activeMatchButton.getAttribute("aria-current")).toBe("true");

    const playerFlows = container.querySelectorAll(".player-flow");
    expect(playerFlows.length).toBe(2);
    const p1Lane = playerFlows[0] as HTMLElement;
    const p2Lane = playerFlows[1] as HTMLElement;

    const getInspectorPanel = () =>
      getByRole("tabpanel", { name: "Stage details" });

    // 1. P1 Draft: renders DG metadata + draftRegenerations (failed + completed)
    fireEvent.click(within(p1Lane).getByRole("button", { name: /Draft/i }));
    let inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Draft regenerations");
    expect(inspectorText).toContain("regen-p1-1");
    expect(inspectorText).toContain("GENERATOR_TIMEOUT");
    expect(inspectorText).toContain("regen-p1-2");

    // 2. P1 First Stage: consumed opening is NOT duplicated; solver metrics ARE rendered
    fireEvent.click(within(p1Lane).getByRole("button", { name: /First Stage/i }));
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Solver metrics");
    expect(inspectorText).toContain("Min steps");
    expect(inspectorText).toContain("Min pushes");
    expect(inspectorText).not.toContain("AI opening evaluation");

    // 3. P1 General Discussion: includes absorbed opening evaluation & cards + chat turns
    fireEvent.click(
      within(p1Lane).getByRole("button", { name: /General Discussion/i }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("AI opening evaluation");
    expect(inspectorText).toContain("Would you like to test or tweak this layout?");
    expect(inspectorText).toContain("How does the center corridor feel right now?");

    // 4. P1 Intent: revised status + original hypothesis + revised action
    fireEvent.click(within(p1Lane).getByRole("button", { name: /Intent/i }));
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Revised intention");
    expect(inspectorText).toContain("Keep the center open for readable pushing.");
    expect(inspectorText).toContain("Keep the center open with clear visual lanes.");

    // 5. P1 Manual Edit Review (Stage 2) -> linked child LLM Challenge (acknowledged) & jump navigation
    const p1ManualReviews = within(p1Lane).getAllByRole("button", {
      name: /Manual Edit Review/i,
    });
    expect(p1ManualReviews).toHaveLength(3);
    fireEvent.click(p1ManualReviews[0]);
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Reviewed");
    expect(inspectorText).toContain("Added right flank wall");
    expect(inspectorText).toContain("(8, 4): . -> #");
    expect(inspectorText).toContain("llm-challenge:dis_0000001220264000");

    // Clicking linked child node button navigates directly to LLM Challenge
    fireEvent.click(
      within(getInspectorPanel()).getByRole("button", {
        name: /LLM Challenge/i,
      }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Responded · proposal block released");
    expect(inspectorText).toContain("manual-review:0000000e20264000800000000000000e");
    expect(inspectorText).toContain("LET'S DISCUSS");

    // 6. P1 second LLM Challenge: manual_reedit + parent linkage
    const p1LlmChallenges = within(p1Lane).getAllByRole("button", {
      name: /LLM Challenge/i,
    });
    expect(p1LlmChallenges).toHaveLength(2);
    fireEvent.click(p1LlmChallenges[1]);
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Ended through a new manual edit");
    expect(inspectorText).toContain("manual-review:00000015202640008000000000000015");

    // 7. P1 Proposal (Q1+Q2 binary discovery, replaced) -> child Player Challenge -> child Proposal (accepted)
    const p1Proposals = within(p1Lane).getAllByRole("button", {
      name: /Proposal/i,
    });
    expect(p1Proposals).toHaveLength(2);
    fireEvent.click(p1Proposals[0]);
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Replaced");
    expect(inspectorText).toContain("Remove internal wall");
    expect(inspectorText).toContain("Also clear lower flank wall");
    expect(inspectorText).toContain("player-challenge:chg_0000002c20264000");

    fireEvent.click(
      within(p1Lane).getByRole("button", { name: /Player Challenge/i }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("New plan from the player's reason selected");
    expect(inspectorText).toContain("proposal:top_0000001e20264000");
    expect(inspectorText).toContain(
      "I prefer to remove the water tile instead of the wall.",
    );
    expect(inspectorText).toContain("proposal:challenge:chg_0000002c20264000");

    fireEvent.click(p1Proposals[1]);
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Accepted");
    expect(inspectorText).toContain("player-challenge:chg_0000002c20264000");
    expect(inspectorText).toContain("(5, 4): @ -> .");
    expect(inspectorText).toContain("Accepted candidate map · saved as Stage 5");

    // 8. P1 Stage 5 · AI: diff + solver + playAttempts (abandoned)
    fireEvent.click(
      within(p1Lane).getByRole("button", { name: /Stage 5 · AI/i }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("(5, 4): @ -> .");
    expect(inspectorText).toContain("Play attempts");
    expect(inspectorText).toContain("Abandoned");
    expect(inspectorText).toContain("4.0s");

    // 9. P1 Stage 6 · Restored: diff + solver + playAttempts (completed) + restore metadata
    fireEvent.click(
      within(p1Lane).getByRole("button", { name: /Stage 6 · Restored/i }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Restored from Stage 4");
    expect(inspectorText).toContain("(5, 4): . -> @");
    expect(inspectorText).toContain("Completed");
    expect(inspectorText).toContain("8.0s");

    // 10. P1 Final Stage: >1200s co-creation duration (1470s => 24:30, NOT 20:00)
    fireEvent.click(
      within(p1Lane).getByRole("button", { name: /Final Stage/i }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("24:30");
    expect(inspectorText).not.toContain("20:00");
    const recordTab = getByRole("tab", { name: "Record & IDs" });
    fireEvent.click(recordTab);
    const recordPanel = getByRole("tabpanel", { name: "Record & IDs" });
    expect(recordPanel.textContent).toContain("24:30");
    expect(recordPanel.textContent).not.toContain("20:00");
    expect(recordPanel.textContent).toContain("study-p1");
    expect(
      recordPanel.querySelector('[title*="study-p1-full-uuid"]'),
    ).toBeTruthy();

    // 11. P2 First Stage: UNCONSUMED opening renders evaluation text AND both opening cards
    fireEvent.click(within(p2Lane).getByRole("button", { name: /First Stage/i }));
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("AI opening evaluation");
    expect(inspectorText).toContain(
      "Player 2 Stage 1 starts with an open baseline route.",
    );
    expect(inspectorText).toContain("Opening cards");
    expect(inspectorText).toContain(
      "[discussion] Would you like to test or tweak this layout?",
    );
    expect(inspectorText).toContain(
      "[clarification] Playtest to verify box pacing.",
    );

    // 12. P2 Player Challenge (original_selected) & child Proposal (rejected)
    fireEvent.click(
      within(p2Lane).getByRole("button", { name: /Player Challenge/i }),
    );
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Original proposal selected");
    expect(inspectorText).toContain("proposal:top_0000004120264000");

    const p2Proposals = within(p2Lane).getAllByRole("button", {
      name: /Proposal/i,
    });
    expect(p2Proposals).toHaveLength(2);
    fireEvent.click(p2Proposals[1]);
    inspectorText = getInspectorPanel().textContent ?? "";
    expect(inspectorText).toContain("Rejected");
    expect(inspectorText).toContain("Keeping my Stage 1 layout as-is.");

    // Challenge maps use SokobanMap
    expect(getAllByRole("img").length).toBeGreaterThan(0);

    // Live search by full/short studySessionId & non-matching query reveals accurate self-hosted survey copy
    const searchInput = getByLabelText(
      "Search room code, Match ID, or Study session ID",
    ) as HTMLInputElement;
    fireEvent.input(searchInput, { target: { value: "study-beta-p1" } });
    await waitFor(() => {
      expect(getByText("1 shown")).toBeTruthy();
    });
    expect(getByRole("button", { name: /ROOM BETA99/i })).toBeTruthy();

    fireEvent.input(searchInput, { target: { value: "no-match-anywhere" } });
    await waitFor(() => {
      expect(getByText("0 shown")).toBeTruthy();
    });
    const emptyCopy = container.querySelector(".empty-detail")?.textContent ?? "";
    expect(emptyCopy).toContain("/survey/");
    expect(emptyCopy).toContain("Study Session ID");
    expect(emptyCopy).toContain("/export/matchmaking-research.jsonl");
    expect(emptyCopy).not.toContain("WJX");
    expect(emptyCopy).not.toContain("results, and questionnaires.");

    store.dispose();
  });

  it("handles destructive delete dialog with X-Delete-Password without numeric inputmode", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const payload = cloneContractPayload();

    const fetchMock: typeof fetch = vi.fn(async (input, init) => {
      const url = String(input);
      requests.push({ url, init });
      if (url.endsWith("/delete-online-match")) {
        const headers = new Headers(init?.headers);
        if (headers.get("X-Delete-Password") !== "alpha-pass") {
          return jsonResponse({ detail: "Unauthorized" }, 401);
        }
        return jsonResponse({ status: "ok" }, 200);
      }
      return jsonResponse(payload, 200);
    });

    const api = createDashboardApi({
      apiBase: "https://sokobanaidemo.top",
      fetchImpl: fetchMock,
    });
    const store = createDashboardStore(api, { debounceMs: 0 });

    const { findByRole, getByLabelText, getByText } = render(
      <App store={store} />,
    );

    const deleteMatchButton = await findByRole("button", {
      name: "Delete match",
    });
    expect(requests).toHaveLength(1);

    fireEvent.click(deleteMatchButton);

    const passwordInput = getByLabelText(
      "Deletion password",
    ) as HTMLInputElement;
    expect(passwordInput.getAttribute("inputmode")).toBeNull();
    expect(passwordInput.type).toBe("password");

    const getDialogConfirmButton = () => {
      const btn = Array.from(document.querySelectorAll("dialog button")).find(
        (el) => el.textContent === "Delete match",
      );
      if (!btn) {
        throw new Error("Dialog confirm button not found");
      }
      return btn as HTMLButtonElement;
    };

    fireEvent.input(passwordInput, { target: { value: "wrong" } });
    fireEvent.click(getDialogConfirmButton());

    await waitFor(() => {
      expect(
        getByText("Incorrect password. Please try again."),
      ).toBeTruthy();
    });

    fireEvent.input(passwordInput, { target: { value: "alpha-pass" } });
    fireEvent.click(getDialogConfirmButton());

    await waitFor(() => {
      expect(requests).toHaveLength(4);
    });
    expect(requests[2].url).toBe(
      "https://sokobanaidemo.top/delete-online-match",
    );
    expect(requests[2].init?.method).toBe("POST");
    expect(new Headers(requests[2].init?.headers).get("X-Delete-Password")).toBe(
      "alpha-pass",
    );
    expect(getByText("Match deleted successfully.")).toBeTruthy();

    store.dispose();
  });
});

function createSurveyAnalyticsFixture(
  overrides: Partial<SurveyAnalyticsResponse> = {},
): SurveyAnalyticsResponse {
  return SurveyAnalyticsResponseSchema.parse({
    generatedAt: "2026-11-27T12:00:00Z",
    scope: "active_matches",
    filters: {
      scope: "active_matches",
      surveyId: null,
      surveyVersion: null,
    },
    scopeCounts: {
      active_matches: 2,
      formal: 3,
      standalone: 1,
      all: 4,
    },
    availableSurveys: [
      {
        surveyId: "online_post_match_survey",
        surveyTitle: "赛后问卷 / Post-Game Questionnaire",
        responseCount: 1,
        versions: ["v1.0-wjx-migrated"],
      },
      {
        surveyId: "online_pre_match_survey",
        surveyTitle: "匹配前简短问卷 / Pre-Match Short Questionnaire",
        responseCount: 1,
        versions: ["v1.0-wjx-migrated", "v2.0"],
      },
    ],
    availableVersions: ["v1.0-wjx-migrated", "v2.0"],
    totalResponses: 2,
    surveys: [
      {
        surveyId: "online_post_match_survey",
        surveyTitle: "赛后问卷 / Post-Game Questionnaire",
        kpi: {
          responseCount: 1,
          linkedMatchCount: 1,
          avgDurationSeconds: 64.5,
          medianDurationSeconds: 64.5,
          latestSubmittedAt: "2026-11-27T09:00:00Z",
          versions: ["v1.0-wjx-migrated"],
        },
        sections: [
          {
            sectionId: "part_a",
            questionCount: 1,
            questionIds: ["rel_comfort"],
            likertSummary: {
              questionCount: 1,
              scoreCount: 1,
              mean: 4.0,
              sd: null,
              median: 4.0,
              rawScoresNote: "Raw scores (unreversed)",
            },
            questions: [
              {
                sectionId: "part_a",
                questionIndex: 2,
                questionId: "rel_comfort",
                questionType: "likert_5",
                questionTextZh: "相处自在",
                questionTextEn: "I feel comfortable",
                answeredCount: 1,
                skippedCount: 0,
                totalResponses: 1,
                responseRate: 100.0,
                buckets: [
                  {
                    score: 1,
                    optionId: "1",
                    optionTextZh: "非常不同意",
                    optionTextEn: "Strongly disagree",
                    count: 0,
                    percentage: 0,
                  },
                  {
                    score: 2,
                    optionId: "2",
                    optionTextZh: "不同意",
                    optionTextEn: "Disagree",
                    count: 0,
                    percentage: 0,
                  },
                  {
                    score: 3,
                    optionId: "3",
                    optionTextZh: "中立",
                    optionTextEn: "Neutral",
                    count: 0,
                    percentage: 0,
                  },
                  {
                    score: 4,
                    optionId: "4",
                    optionTextZh: "同意",
                    optionTextEn: "Agree",
                    count: 1,
                    percentage: 100,
                  },
                  {
                    score: 5,
                    optionId: "5",
                    optionTextZh: "非常同意",
                    optionTextEn: "Strongly agree",
                    count: 0,
                    percentage: 0,
                  },
                ],
                stats: {
                  mean: 4.0,
                  sd: null,
                  median: 4.0,
                  min: 4,
                  max: 4,
                },
              },
            ],
          },
          {
            sectionId: "part_b",
            questionCount: 1,
            questionIds: ["partner_choice"],
            likertSummary: null,
            questions: [
              {
                sectionId: "part_b",
                questionIndex: 3,
                questionId: "partner_choice",
                questionType: "single_choice",
                questionTextZh: "合作偏好",
                questionTextEn: "Preferred partner style",
                answeredCount: 1,
                skippedCount: 0,
                totalResponses: 1,
                responseRate: 100.0,
                options: [
                  {
                    optionId: "collaborative",
                    optionIndex: 1,
                    optionTextZh: "协作型",
                    optionTextEn: "Collaborative",
                    count: 1,
                    percentage: 100.0,
                  },
                ],
              },
            ],
          },
          {
            sectionId: "part_c",
            questionCount: 1,
            questionIds: ["open_feedback"],
            likertSummary: null,
            questions: [
              {
                sectionId: "part_c",
                questionIndex: 22,
                questionId: "open_feedback",
                questionType: "textarea",
                questionTextZh: "补充意见",
                questionTextEn: "Additional feedback",
                answeredCount: 1,
                skippedCount: 0,
                totalResponses: 1,
                responseRate: 100.0,
                nonEmptyCount: 1,
                entries: [
                  {
                    recordId: 101,
                    text: "Co-creating obstacles together felt smooth.",
                    matchId: "00000002202640008000000000000002",
                    roomCode: "BETA99",
                    playerSlot: 1,
                    linkedActiveMatchId: "00000002202640008000000000000002",
                    studySessionId: "study-beta-p1-uuid",
                    entrySource: "unity_matchmaking",
                    submittedAt: "2026-11-27T09:00:00Z",
                  },
                ],
              },
            ],
          },
        ],
        questions: [],
        responses: [
          {
            recordId: 101,
            responseId: "resp-beta-post-p1",
            surveyId: "online_post_match_survey",
            surveyVersion: "v1.0-wjx-migrated",
            surveyTitle: "赛后问卷 / Post-Game Questionnaire",
            entrySource: "unity_matchmaking",
            matchId: "00000002202640008000000000000002",
            roomCode: "BETA99",
            playerSlot: 1,
            linkedActiveMatchId: "00000002202640008000000000000002",
            studySessionId: "study-beta-p1-uuid",
            participantId: "P-BETA-1",
            experimentCondition: "A",
            language: "bilingual",
            submittedAt: "2026-11-27T09:00:00Z",
            createdAt: "2026-11-27T09:00:01Z",
            durationSeconds: 64.5,
            answerCount: 3,
            answers: [
              {
                sectionId: "part_a",
                questionIndex: 2,
                questionId: "rel_comfort",
                questionType: "likert_5",
                questionTextZh: "相处自在",
                questionTextEn: "I feel comfortable",
                optionId: "4",
                optionIndex: 4,
                optionTextZh: "同意",
                optionTextEn: "Agree",
                score: 4,
                textValue: null,
              },
              {
                sectionId: "part_b",
                questionIndex: 3,
                questionId: "partner_choice",
                questionType: "single_choice",
                questionTextZh: "合作偏好",
                questionTextEn: "Preferred partner style",
                optionId: "collaborative",
                optionIndex: 1,
                optionTextZh: "协作型",
                optionTextEn: "Collaborative",
                score: null,
                textValue: null,
              },
              {
                sectionId: "part_c",
                questionIndex: 22,
                questionId: "open_feedback",
                questionType: "textarea",
                questionTextZh: "补充意见",
                questionTextEn: "Additional feedback",
                optionId: null,
                optionIndex: null,
                optionTextZh: null,
                optionTextEn: null,
                score: null,
                textValue: "Co-creating obstacles together felt smooth.",
              },
            ],
          },
          {
            recordId: 102,
            responseId: "resp-orphan-post",
            surveyId: "online_post_match_survey",
            surveyVersion: "v1.0-wjx-migrated",
            surveyTitle: "赛后问卷 / Post-Game Questionnaire",
            entrySource: "unity_matchmaking",
            matchId: "00000099202640008000000000000099",
            roomCode: "OLD999",
            playerSlot: 2,
            linkedActiveMatchId: null,
            studySessionId: "study-orphan-p2",
            participantId: null,
            experimentCondition: null,
            language: "zh",
            submittedAt: "2026-11-27T07:00:00Z",
            createdAt: "2026-11-27T07:00:01Z",
            durationSeconds: 30,
            answerCount: 0,
            answers: [],
          },
        ],
      },
    ],
    ...overrides,
  });
}

describe("dashboard survey integration (Tab 1 & Tab 2)", () => {
  it("renders Tab 1 MatchSurveysPanel with P1/P2 status, Part A/B/C comparison, and legacy fallback", async () => {
    const payload = createMultiMatchPayloadFromContract();
    // CONT2A already has legacy survey (no answers[]) from contract fixture.
    // Give BETA99 rich structured P1 & P2 surveys across Part A / B / C.
    payload.matches[1].surveys = [
      {
        recordId: 11,
        surveyId: "online_post_match_survey",
        surveyVersion: "v1.0-wjx-migrated",
        entrySource: "unity_matchmaking",
        playerSlot: 1,
        matchedBy: "matchId",
        createdAt: "2026-11-27T09:00:00Z",
        payload: {
          surveyId: "online_post_match_survey",
          surveyTitle: "赛后问卷 / Post-Game Questionnaire",
          durationSeconds: 55.2,
          submittedAt: "2026-11-27T09:00:00Z",
          answers: [
            {
              sectionId: "part_a",
              questionIndex: 1,
              questionId: "rel_comfort",
              questionType: "likert_5",
              questionTextZh: "相处自在",
              questionTextEn: "I feel comfortable",
              optionId: "4",
              optionIndex: 4,
              optionTextZh: "同意",
              optionTextEn: "Agree",
              score: 4,
              textValue: null,
            },
            {
              sectionId: "part_b",
              questionIndex: 2,
              questionId: "role_style",
              questionType: "single_choice",
              questionTextZh: "分工角色",
              questionTextEn: "Role preference",
              optionId: "architect",
              optionIndex: 1,
              optionTextZh: "规划者",
              optionTextEn: "Architect",
              score: null,
              textValue: null,
            },
            {
              sectionId: "part_c",
              questionIndex: 3,
              questionId: "open_feedback",
              questionType: "textarea",
              questionTextZh: "补充意见",
              questionTextEn: "Additional feedback",
              optionId: null,
              optionIndex: null,
              optionTextZh: null,
              optionTextEn: null,
              score: null,
              textValue: "Player 1 loved the puzzle rhythm.",
            },
          ],
        },
      },
      {
        recordId: 12,
        surveyId: "online_post_match_survey",
        surveyVersion: "v1.0-wjx-migrated",
        entrySource: "unity_matchmaking",
        playerSlot: 2,
        matchedBy: "studySessionId",
        createdAt: "2026-11-27T09:02:00Z",
        payload: {
          surveyId: "online_post_match_survey",
          surveyTitle: "赛后问卷 / Post-Game Questionnaire",
          durationSeconds: 40.0,
          submittedAt: "2026-11-27T09:02:00Z",
          answers: [
            {
              sectionId: "part_a",
              questionIndex: 1,
              questionId: "rel_comfort",
              questionType: "likert_5",
              questionTextZh: "相处自在",
              questionTextEn: "I feel comfortable",
              optionId: "5",
              optionIndex: 5,
              optionTextZh: "非常同意",
              optionTextEn: "Strongly agree",
              score: 5,
              textValue: null,
            },
          ],
        },
      },
    ];

    const fetchMock: typeof fetch = vi.fn(async () => jsonResponse(payload));
    const api = createDashboardApi({
      apiBase: "https://sokobanaidemo.top",
      fetchImpl: fetchMock,
    });
    const store = createDashboardStore(api, { debounceMs: 0 });
    const { findByRole, getByRole } = render(<App store={store} />);

    // Default CONT2A renders legacy survey fallback cleanly without crashing
    const surveysRegion = await findByRole("region", {
      name: "Match surveys",
    });
    expect(surveysRegion.textContent).toContain("Legacy payload");
    expect(surveysRegion.textContent).toContain("study-p1-full-uuid");
    expect(surveysRegion.textContent).toContain("Matched via studySessionId");

    // Switch to BETA99 to inspect rich Part A / B / C side-by-side comparison
    fireEvent.click(getByRole("button", { name: /ROOM BETA99/i }));
    await waitFor(() => {
      expect(surveysRegion.textContent).toContain("Post-Match · Online2");
    });
    expect(surveysRegion.textContent).toContain("Submitted");
    expect(surveysRegion.textContent).toContain("Missing");
    expect(surveysRegion.textContent).toContain("Part A");
    expect(surveysRegion.textContent).toContain("Part B");
    expect(surveysRegion.textContent).toContain("Part C");
    expect(surveysRegion.textContent).toContain("4/5");
    expect(surveysRegion.textContent).toContain("5/5");
    expect(surveysRegion.textContent).toContain("规划者 / Architect");
    expect(surveysRegion.textContent).toContain(
      "Player 1 loved the puzzle rhythm.",
    );

    const miniFills = surveysRegion.querySelectorAll(".likert-mini-fill");
    expect(miniFills).toHaveLength(2);
    expect((miniFills[0] as HTMLElement).style.width).toBe("80%");
    expect((miniFills[1] as HTMLElement).style.width).toBe("100%");

    store.dispose();
  });

  it("supports Tab 2 Survey Analytics filtering, SD null rendering, raw unreversed note, row expand, and jumpToMatch", async () => {
    const matchPayload = createMultiMatchPayloadFromContract();
    const requestedUrls: string[] = [];

    const fetchMock: typeof fetch = vi.fn(async (input) => {
      const url = String(input);
      requestedUrls.push(url);
      if (url.includes("/survey-analytics-data")) {
        const parsedUrl = new URL(url);
        const scope = parsedUrl.searchParams.get("scope") || "active_matches";
        const surveyId = parsedUrl.searchParams.get("survey_id") || null;
        const surveyVersion = parsedUrl.searchParams.get("survey_version") || null;
        return jsonResponse(
          createSurveyAnalyticsFixture({
            scope: scope as SurveyAnalyticsResponse["scope"],
            filters: {
              scope,
              surveyId,
              surveyVersion,
            },
          }),
        );
      }
      if (url.endsWith("/delete-online-match")) {
        return jsonResponse({ status: "ok" }, 200);
      }
      return jsonResponse(matchPayload, 200);
    });

    const api = createDashboardApi({
      apiBase: "https://sokobanaidemo.top",
      fetchImpl: fetchMock,
    });
    const store = createDashboardStore(api, { debounceMs: 0 });
    const {
      findByText,
      getByLabelText,
      getByRole,
      getByText,
      getAllByRole,
      queryByRole,
    } = render(<App store={store} />);

    await findByText("1 rooms without First Stage hidden");
    // Initial load on Tab 1 does not fetch /survey-analytics-data yet
    expect(
      requestedUrls.some((u) => u.includes("/survey-analytics-data")),
    ).toBe(false);

    // Switch to Survey Analytics tab -> triggers default scope=active_matches fetch
    fireEvent.click(getByRole("tab", { name: /Survey Analytics/i }));
    await waitFor(() => {
      expect(
        requestedUrls.some((u) =>
          u.endsWith("/survey-analytics-data?scope=active_matches"),
        ),
      ).toBe(true);
    });

    await findByText("Section Likert Averages");
    // Explicit raw scores note & em-dash for sd === null
    expect(document.body.textContent).toContain("Raw scores (unreversed)");
    expect(document.body.textContent).toContain("SD —");
    expect(document.body.textContent).toContain("SD: —");

    // single_choice, likert_5, textarea rendered
    expect(document.body.textContent).toContain("协作型 / Collaborative");
    expect(document.body.textContent).toContain(
      "Co-creating obstacles together felt smooth.",
    );

    // Dynamic CSV link reflects default scope=active_matches with valueless download attribute
    const csvLink = getByRole("link", { name: "Export Surveys (CSV)" });
    expect(csvLink.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/surveys.csv?scope=active_matches",
    );
    expect(csvLink.hasAttribute("download")).toBe(true);
    expect(csvLink.getAttribute("download")).toBe("");

    // Change scope, survey filter, and version filter -> re-fetches and updates CSV href
    fireEvent.click(getByRole("button", { name: /Standalone Tests/i }));
    await waitFor(() => {
      expect(
        requestedUrls.some((u) =>
          u.includes("/survey-analytics-data?scope=standalone"),
        ),
      ).toBe(true);
    });

    fireEvent.click(getByRole("button", { name: /Pre-Match · Online1/i }));
    await waitFor(() => {
      expect(
        requestedUrls.some((u) =>
          u.includes("survey_id=online_pre_match_survey"),
        ),
      ).toBe(true);
    });

    const versionSelect = getByLabelText(
      "Survey version filter",
    ) as HTMLSelectElement;
    fireEvent.change(versionSelect, { target: { value: "v2.0" } });
    await waitFor(() => {
      expect(
        requestedUrls.some((u) => u.includes("survey_version=v2.0")),
      ).toBe(true);
    });

    expect(csvLink.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/surveys.csv?scope=standalone&survey_id=online_pre_match_survey&survey_version=v2.0",
    );

    // Row expand toggle in SurveyResponsesTable
    const inspectButtons = getAllByRole("button", { name: "Inspect" });
    fireEvent.click(inspectButtons[0]);
    expect(getByText(/Response resp-bet/)).toBeTruthy();
    expect(getByText("Condition: A")).toBeTruthy();

    // Unlinked room renders as non-clickable text/code, while linked active room jumps back to Tab 1
    expect(queryByRole("button", { name: "ROOM OLD999" })).toBeNull();
    expect(getByText("ROOM OLD999")).toBeTruthy();

    const jumpButtons = getAllByRole("button", { name: /ROOM BETA99/i });
    fireEvent.click(jumpButtons[jumpButtons.length - 1]);

    await waitFor(() => {
      expect(store.activeTab.value).toBe("matches");
    });
    expect(store.selectedMatch.value?.matchId).toBe(
      "00000002202640008000000000000002",
    );
    const betaItem = getByRole("button", { name: /ROOM BETA99/i });
    expect(betaItem.getAttribute("aria-current")).toBe("true");

    // Jumping to a match without First Stage switches to Tab 1 and shows a NoticeBanner
    store.jumpToMatch("00000003202640008000000000000003");
    await waitFor(() => {
      expect(
        getByText(/has not reached First Stage and is hidden/i),
      ).toBeTruthy();
    });

    store.dispose();
  });

  it("aborts stale survey analytics requests independently without cancelling matchmaking loads, and refreshes on delete", async () => {
    const matchPayload = createMultiMatchPayloadFromContract();
    const matchSignals: AbortSignal[] = [];
    const surveySignals: AbortSignal[] = [];
    let surveyFetchCount = 0;

    const fetchMock: typeof fetch = vi.fn((input, init) => {
      const url = String(input);
      const signal = init?.signal as AbortSignal;
      if (url.includes("/survey-analytics-data")) {
        surveySignals.push(signal);
        surveyFetchCount += 1;
        if (surveyFetchCount === 1) {
          return new Promise<Response>((_resolve, reject) => {
            signal.addEventListener("abort", () => {
              reject(new DOMException("Aborted", "AbortError"));
            });
          });
        }
        return Promise.resolve(jsonResponse(createSurveyAnalyticsFixture()));
      }
      if (url.endsWith("/delete-online-match")) {
        return Promise.resolve(jsonResponse({ status: "ok" }, 200));
      }
      matchSignals.push(signal);
      return Promise.resolve(jsonResponse(matchPayload, 200));
    });

    const api = createDashboardApi({
      apiBase: "https://sokobanaidemo.top",
      fetchImpl: fetchMock,
    });
    const store = createDashboardStore(api, { debounceMs: 0 });

    const matchLoad = store.loadData(false);
    const surveyLoad1 = store.loadSurveyAnalytics(false);
    const surveyLoad2 = store.loadSurveyAnalytics(false);

    // Second survey load aborts first survey load, NOT the concurrent match load
    expect(surveySignals).toHaveLength(2);
    expect(surveySignals[0].aborted).toBe(true);
    expect(surveySignals[1].aborted).toBe(false);
    expect(matchSignals[0].aborted).toBe(false);

    await Promise.all([matchLoad, surveyLoad1, surveyLoad2]);
    expect(store.surveyAnalytics.value?.totalResponses).toBe(2);

    // Deleting a match refreshes surveyAnalytics too since it was previously loaded
    const beforeDeleteSurveyFetches = surveyFetchCount;
    store.openDeleteMatch(matchPayload.matches[0]);
    const ok = await store.confirmDelete("pw");
    expect(ok).toBe(true);
    expect(surveyFetchCount).toBe(beforeDeleteSurveyFetches + 1);

    store.dispose();
  });

  it("resets surveyIdFilter and surveyVersionFilter in a single analytics request when Clear survey filters is clicked", async () => {
    const matchPayload = createMultiMatchPayloadFromContract();
    const surveyUrls: string[] = [];

    const fetchMock: typeof fetch = vi.fn(async (input) => {
      const url = String(input);
      if (url.includes("/survey-analytics-data")) {
        surveyUrls.push(url);
        const parsedUrl = new URL(url);
        const surveyId = parsedUrl.searchParams.get("survey_id") || null;
        const surveyVersion = parsedUrl.searchParams.get("survey_version") || null;
        if (surveyId && surveyVersion) {
          return jsonResponse(
            createSurveyAnalyticsFixture({
              filters: {
                scope: "active_matches",
                surveyId,
                surveyVersion,
              },
              totalResponses: 0,
              surveys: [],
            }),
          );
        }
        return jsonResponse(
          createSurveyAnalyticsFixture({
            filters: {
              scope: "active_matches",
              surveyId,
              surveyVersion,
            },
          }),
        );
      }
      return jsonResponse(matchPayload, 200);
    });

    const api = createDashboardApi({
      apiBase: "https://sokobanaidemo.top",
      fetchImpl: fetchMock,
    });
    const store = createDashboardStore(api, { debounceMs: 0 });
    const { findByRole, getByLabelText } = render(<App store={store} />);

    fireEvent.click(await findByRole("tab", { name: /Survey Analytics/i }));
    const preMatchFilterBtn = await findByRole("button", {
      name: /Pre-Match · Online1/i,
    });
    expect(surveyUrls).toHaveLength(1);

    fireEvent.click(preMatchFilterBtn);
    await waitFor(() => {
      expect(surveyUrls).toHaveLength(2);
    });

    fireEvent.change(getByLabelText("Survey version filter"), {
      target: { value: "v2.0" },
    });
    const clearButton = await findByRole("button", {
      name: "Clear survey filters",
    });
    expect(surveyUrls).toHaveLength(3);

    fireEvent.click(clearButton);
    await waitFor(() => {
      expect(store.surveyIdFilter.value).toBe("");
      expect(store.surveyVersionFilter.value).toBe("");
    });
    expect(surveyUrls).toHaveLength(4);
    expect(surveyUrls[3]).toBe(
      "https://sokobanaidemo.top/survey-analytics-data?scope=active_matches",
    );

    store.dispose();
  });
});
