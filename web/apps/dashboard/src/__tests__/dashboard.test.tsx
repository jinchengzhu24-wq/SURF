import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
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
import type { MatchmakingRecordsResponse } from "../types";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function createSamplePayload(): MatchmakingRecordsResponse {
  return {
    generatedAt: "2026-11-18T10:00:00Z",
    summary: { malformedCount: 0 },
    matches: [
      {
        matchId: "match-alpha-12345678",
        roomCode: "AB12CD",
        status: "completed",
        playerCount: 2,
        createdAt: "2026-11-18T09:00:00Z",
        updatedAt: "2026-11-18T09:10:00Z",
        players: [
          {
            playerNumber: 1,
            ready: true,
            studySessionId: "study-session-p1-abcdef",
            designerIntention: "Tight corridor test",
            coCreationFlow: [
              {
                eventId: "fs-1",
                eventType: "first_stage",
                playerNumber: 1,
                stageNumber: 1,
                initialDraftMethod: "description_generation",
                rows: [
                  "############",
                  "#..........#",
                  "#..p.s.t...#",
                  "#..........#",
                  "#..........#",
                  "#..........#",
                  "#..........#",
                  "#..........#",
                  "#..........#",
                  "############",
                ],
                serverReceivedAt: "2026-11-18T09:01:00Z",
              },
              {
                eventId: "st-4",
                eventType: "stage",
                playerNumber: 1,
                source: "restored",
                stageNumber: 4,
                restoredFromStageNumber: 1,
                replacedStageNumber: 3,
                rows: ["####", "#pt#", "####"],
                serverReceivedAt: "2026-11-18T09:02:00Z",
              },
              {
                eventId: "node-1",
                eventType: "node",
                playerNumber: 1,
                nodeType: "player_challenge",
                nodeStatus: "replacement_selected",
                stageNumber: 2,
                nodeParentId: "proposal:original",
                serverReceivedAt: "2026-11-18T09:03:00Z",
                nodeEntries: [
                  {
                    kind: "player_message",
                    text: "No; follow my reason.",
                    occurredAt: "2026-11-18T09:03:30Z",
                  },
                  {
                    kind: "card",
                    text: "Keep the original proposal?",
                    occurredAt: "2026-11-18T09:03:20Z",
                  },
                  {
                    kind: "llm_message",
                    text: "I compare your reason with the proposal.",
                    occurredAt: "2026-11-18T09:03:10Z",
                  },
                ],
              },
              {
                eventId: "legacy-turn-ignored",
                eventType: "turn",
                playerNumber: 1,
                serverReceivedAt: "2026-11-18T09:04:00Z",
              },
            ],
            challenge: {
              createdByPlayerNumber: 1,
              playedByPlayerNumber: 2,
              rows: [
                "############",
                "#..p.s.t...#",
                "############",
              ],
              runResult: {
                durationSeconds: 12,
                moveCount: 5,
                minimumMoves: 4,
                restartCount: 0,
                outcome: "completed",
              },
            },
            result: { durationSeconds: 18 },
          },
          {
            playerNumber: 2,
            ready: true,
            studySessionId: "study-session-p2-ghijkl",
            designerIntention: "Symmetric boxes",
            coCreationFlow: [],
            challenge: {
              createdByPlayerNumber: 2,
              playedByPlayerNumber: 1,
              rows: [
                "############",
                "#..t.s.p...#",
                "############",
              ],
              runResult: {
                durationSeconds: 30,
                moveCount: 2,
                minimumMoves: 10,
                restartCount: 1,
                outcome: "timed_out",
              },
            },
            result: { durationSeconds: 12 },
          },
        ],
        events: [],
      },
      {
        matchId: "match-beta-87654321",
        roomCode: "XY99ZZ",
        status: "in_progress",
        playerCount: 1,
        createdAt: "2026-11-18T08:00:00Z",
        players: [
          {
            playerNumber: 1,
            ready: false,
            studySessionId: "study-session-beta-1",
            coCreationFlow: [
              {
                eventId: "beta-fs",
                eventType: "first_stage",
                playerNumber: 1,
                stageNumber: 1,
                rows: ["####", "#pt#", "####"],
                serverReceivedAt: "2026-11-18T08:01:00Z",
              },
            ],
          },
        ],
        events: [],
      },
      {
        matchId: "match-hidden-no-first-stage",
        roomCode: "ZZZZZZ",
        status: "in_progress",
        playerCount: 1,
        createdAt: "2026-11-18T09:05:00Z",
        players: [{ playerNumber: 1, ready: false, coCreationFlow: [] }],
        events: [],
      },
    ],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("dashboard formatters and API utilities", () => {
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

  it("formats node statuses, restored stage labels, clock duration, and timed-out metrics", () => {
    expect(dashboardNodeStatus("acknowledged")).toBe(
      "Responded · proposal block released",
    );
    expect(dashboardNodeStatus("replacement_selected")).toBe(
      "New plan from the player's reason selected",
    );
    expect(
      flowStageLabel({
        eventType: "stage",
        source: "restored",
        stageNumber: 4,
      }),
    ).toBe("Stage 4 · Restored");
    expect(formatClockDuration(1500)).toBe("20:00");
    expect(formatClockDuration(-1)).toBe("-");
    expect(formatChallengeOutcome("timed_out")).toBe("Timed out");

    const metrics = new Map(
      formatChallengeMetrics({
        createdByPlayerNumber: 2,
        playedByPlayerNumber: 1,
        rows: ["###", "#p#", "###"],
        runResult: {
          durationSeconds: 30,
          moveCount: 2,
          minimumMoves: 10,
          restartCount: 1,
          outcome: "timed_out",
        },
      }),
    );
    expect(metrics.get("Outcome")).toBe("Timed out");
    expect(metrics.get("Move overhead")).toBe("-");
  });

  it("filters out legacy turn/opening flow events and evaluates First Stage / completion predicates", () => {
    const payload = createSamplePayload();
    expect(hasFirstStage(payload.matches[0])).toBe(true);
    expect(hasFirstStage(payload.matches[2])).toBe(false);
    expect(isCompletedMatch(payload.matches[0])).toBe(true);
    expect(isCompletedMatch(payload.matches[1])).toBe(false);

    const stages = buildTimelineStages(payload.matches[0]);
    expect(stages.map((s) => s.record.eventType)).toEqual([
      "first_stage",
      "stage",
      "node",
    ]);

    const openingMatch = createSamplePayload().matches[0];
    openingMatch.players[0].coCreationFlow.push(
      {
        eventId: "open-1",
        eventType: "opening",
        playerNumber: 1,
        versionId: "v1",
        assistantTurnId: "turn-open-1",
        assistantText: "Opening evaluation text",
      },
      {
        eventId: "turn-consuming-open-1",
        eventType: "turn",
        playerNumber: 1,
        openingAssistantTurnId: "turn-open-1",
      },
    );
    expect(
      openingIsConsumed(openingMatch, {
        eventType: "opening",
        playerNumber: 1,
        assistantTurnId: "turn-open-1",
      }),
    ).toBe(true);
    expect(
      openingIsConsumed(openingMatch, {
        eventType: "opening",
        playerNumber: 1,
        assistantTurnId: "turn-open-unconsumed",
      }),
    ).toBe(false);
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

    resolveSecond(jsonResponse(createSamplePayload()));
    await Promise.all([firstLoad, secondLoad]);

    expect(store.loadError.value).toBeNull();
    expect(store.firstStageMatches.value).toHaveLength(2);
    store.dispose();
  });
});

describe("dashboard App UI interactions", () => {
  it("loads openly without read-gating, renders summary, tabs, SokobanMap, and chronological node entries", async () => {
    const payload = createSamplePayload();
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
      findByText,
      getByLabelText,
      getByRole,
      getByText,
      getAllByRole,
      getAllByText,
      queryByRole,
    } = render(<App store={store} />);

    await findByText("1 rooms without First Stage hidden");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Summary stats & direct export link
    expect(getByText("50% completion rate")).toBeTruthy();
    const exportLink = getByRole("link", { name: "Export JSONL" });
    expect(exportLink.getAttribute("href")).toBe(
      "https://sokobanaidemo.top/export/study-logs.jsonl",
    );

    // Match list aria-current="true" on active match
    const activeMatchButton = getByRole("button", { name: /ROOM AB12CD/i });
    expect(activeMatchButton.getAttribute("aria-current")).toBe("true");

    // Selecting another match does not throw even when replaceState fails
    const secondMatchButton = getByRole("button", { name: /ROOM XY99ZZ/i });
    fireEvent.click(secondMatchButton);
    expect(secondMatchButton.getAttribute("aria-current")).toBe("true");
    fireEvent.click(activeMatchButton);

    // Challenge maps use SokobanMap (role="img") and accessible tablist
    const mapImages = getAllByRole("img");
    expect(mapImages.length).toBeGreaterThan(0);

    const p2ChallengeTab = getByRole("tab", { name: /P2 challenge/i });
    expect(p2ChallengeTab.getAttribute("aria-selected")).toBe("false");
    fireEvent.click(p2ChallengeTab);
    expect(p2ChallengeTab.getAttribute("aria-selected")).toBe("true");
    expect(getAllByText("Timed out").length).toBeGreaterThanOrEqual(2);

    // StageInspector tablist switches between Stage details and Record & IDs
    const detailsTab = getByRole("tab", { name: "Stage details" });
    const recordTab = getByRole("tab", { name: "Record & IDs" });
    expect(detailsTab.getAttribute("aria-selected")).toBe("true");
    expect(recordTab.getAttribute("aria-selected")).toBe("false");

    // Node entries appear in chronological order
    const panel = getByRole("tabpanel", { name: "Stage details" });
    const text = panel.textContent ?? "";
    expect(text).toContain("New plan from the player's reason selected");
    expect(text.indexOf("I compare your reason")).toBeLessThan(
      text.indexOf("Keep the original proposal"),
    );
    expect(text.indexOf("Keep the original proposal")).toBeLessThan(
      text.indexOf("No; follow my reason"),
    );

    fireEvent.click(recordTab);
    expect(recordTab.getAttribute("aria-selected")).toBe("true");
    expect(getByText("Player 1 Study session ID")).toBeTruthy();

    // Live search filters by Study session ID
    const searchInput = getByLabelText(
      "Search room code, Match ID, or Study session ID",
    ) as HTMLInputElement;
    fireEvent.input(searchInput, { target: { value: "study-session-beta-1" } });
    await waitFor(() => {
      expect(getByText("1 shown")).toBeTruthy();
    });
    expect(getByRole("button", { name: /ROOM XY99ZZ/i })).toBeTruthy();

    // Developer tools menu opens, closes on Escape and outside click
    const toolsButton = getByRole("button", { name: "Developer tools" });
    fireEvent.click(toolsButton);
    expect(getByRole("menu")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(queryByRole("menu")).toBeNull();

    fireEvent.click(toolsButton);
    expect(getByRole("menu")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(queryByRole("menu")).toBeNull();

    store.dispose();
  });

  it("handles destructive delete dialog with X-Delete-Password without numeric inputmode", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const payload = createSamplePayload();

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

    // Wrong password shows 401 error
    fireEvent.input(passwordInput, { target: { value: "wrong" } });
    fireEvent.click(getDialogConfirmButton());

    await waitFor(() => {
      expect(
        getByText("Incorrect password. Please try again."),
      ).toBeTruthy();
    });

    // Correct password succeeds, sends X-Delete-Password, shows notice, and reloads
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
