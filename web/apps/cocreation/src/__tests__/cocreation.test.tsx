import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { PROTOCOL_VERSION } from "@sokoban/ui-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createCocreationApi, resolveApiPrefix } from "../api/cocreationApi";
import { createUnityBridge } from "../bridge/unityBridge";
import { AssistantBubble } from "../components/Chat/AssistantBubble";
import { Composer } from "../components/Chat/Composer";
import { ProgressDrawer } from "../components/Chat/ProgressDrawer";
import { ProposalCard } from "../components/Chat/ProposalCard";
import { TooltipOverlay, hideTooltip } from "../components/Common/Tooltip";
import { DraftPreview } from "../components/Landing/DraftPreview";
import { MapPanel } from "../components/Workspace/MapPanel";
import { StageSidebar } from "../components/Workspace/StageSidebar";
import { TilePalette } from "../components/Workspace/TilePalette";
import { REQUEST_COPY, en, formatTechnicalError, localizeError, t, zhCN } from "../i18n";
import { createSafeStorage } from "../storage/safeStorage";
import {
  clearPendingMessage,
  composerText,
  translationFailures,
} from "../store/chatStore";
import { createDeadlineClock } from "../store/deadlineClock";
import { draftRows, editTile, findCoordinateRoute } from "../store/editorStore";
import {
  activeChallengeState,
  applySession,
  busy,
  landingMode,
  language,
  selectedStageHasActiveDisagreement,
  selectedVersionId,
  session,
  sessionId,
} from "../store/sessionStore";
import * as workflows from "../store/workflows";
import { api } from "../store/workflows/shared";
import { clearTranslationRetryTimer } from "../store/workflows/translationQueue";
import type { Session, Turn } from "../types";

const SAMPLE_ROWS = [
  "############",
  "#..........#",
  "#.p..s...t.#",
  "#..........#",
  "#....##....#",
  "#..........#",
  "#....@@....#",
  "#..........#",
  "#..........#",
  "############",
];

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    sessionId: "sess_1",
    status: "active",
    language: "en",
    languageLocked: true,
    demoMode: true,
    initialDraftMethod: "algorithm_demo",
    currentVersionId: "v1",
    versions: [
      {
        versionId: "v1",
        stageNumber: 1,
        source: "initial",
        rows: SAMPLE_ROWS.slice(),
        diff: [],
        validation: { solutionSteps: 5, solutionPushes: 1, searchedStates: 42 },
        playAttempts: [],
      },
    ],
    turns: [],
    proposals: [],
    assessments: [],
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  clearTranslationRetryTimer();
  clearPendingMessage();
  busy.value = false;
  translationFailures.value = new Set();
  hideTooltip();
  cleanup();
  applySession(null);
  sessionId.value = "";
  selectedVersionId.value = "";
  composerText.value = "";
  draftRows.value = [];
  language.value = "en";
});

describe("i18n parity and error normalization", () => {
  it("enforces 100% key parity between en.json and zh-CN.json", () => {
    const enKeys = Object.keys(en).sort();
    const zhKeys = Object.keys(zhCN).sort();
    expect(zhKeys).toEqual(enKeys);
    for (const key of enKeys) {
      expect((en as Record<string, string>)[key].trim().length).toBeGreaterThan(0);
      expect((zhCN as Record<string, string>)[key].trim().length).toBeGreaterThan(0);
    }
  });

  it("keeps REQUEST_COPY decoupled from presentation button labels", () => {
    expect(REQUEST_COPY.en.executeBoundProposal).toBe("Execute the bound proposal.");
    expect(REQUEST_COPY["zh-CN"].executeBoundProposal).toBe("执行已绑定的方案。");
    expect(t("en", "draftSuggestedRevision")).not.toBe(REQUEST_COPY.en.executeBoundProposal);
  });

  it("localizes error codes and validation details via unified errors.* keys", () => {
    expect(
      localizeError("en", {
        code: "OPEN_OUTER_WALL",
        details: { row: 1, column: 2, tile: "@" },
      }),
    ).toContain("row 1, column 2");
    expect(
      localizeError("zh-CN", {
        code: "PROPOSAL_PRECONDITION_FAILED",
        details: { reason: "already_satisfied" },
      }),
    ).toBe(t("zh-CN", "proposalAlreadySatisfied"));
  });
});

describe("safeStorage resilience and pruning", () => {
  it("never throws when underlying storage rejects reads and writes", () => {
    const brokenStorage = {
      length: 0,
      clear() {
        throw new Error("blocked");
      },
      key() {
        throw new Error("blocked");
      },
      getItem() {
        throw new Error("SecurityError");
      },
      setItem() {
        throw new Error("QuotaExceededError");
      },
      removeItem() {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;

    const store = createSafeStorage(brokenStorage);
    expect(() => store.set("draft", { text: "hello" })).not.toThrow();
    expect(store.get<{ text: string }>("draft")).toEqual({ text: "hello" });
    store.remove("draft");
    expect(store.get("draft")).toBeNull();
  });

  it("evicts expired and oldest entries according to TTL and maxEntries", () => {
    let now = 1_000;
    const backing = new Map<string, string>();
    const fakeStorage = {
      get length() {
        return backing.size;
      },
      key: (i: number) => Array.from(backing.keys())[i] ?? null,
      getItem: (k: string) => backing.get(k) ?? null,
      setItem: (k: string, v: string) => {
        backing.set(k, v);
      },
      removeItem: (k: string) => {
        backing.delete(k);
      },
      clear: () => backing.clear(),
    } as unknown as Storage;

    const store = createSafeStorage(fakeStorage, {
      maxEntries: 2,
      defaultTtlMs: 500,
      now: () => now,
    });

    store.set("a", 1);
    now += 100;
    store.set("b", 2);
    now += 100;
    store.set("c", 3);

    expect(store.get("a")).toBeNull();
    expect(store.get("b")).toBe(2);
    expect(store.get("c")).toBe(3);

    now += 600;
    expect(store.get("b")).toBeNull();
  });
});

describe("deadlineClock monotonic server anchoring", () => {
  it("tracks normal -> warning -> critical -> expired without re-render drift", () => {
    let mono = 10_000;
    const onExpired = vi.fn();
    const clock = createDeadlineClock({
      nowMono: () => mono,
      onExpired,
    });

    const baseSession = makeSession({
      deadlineAt: "2026-11-26T10:00:00Z",
      remainingSeconds: 305,
    });
    clock.syncFromSession(baseSession);
    expect(clock.presentationState.value).toBe("normal");
    expect(clock.formattedRemaining.value).toBe("05:05");

    // Re-syncing the same session with slightly stale remainingSeconds must not freeze countdown
    mono += 10_000;
    clock.tick();
    expect(clock.remainingSeconds.value).toBe(295);
    expect(clock.presentationState.value).toBe("warning");

    mono += 240_000;
    clock.tick();
    expect(clock.presentationState.value).toBe("critical");

    mono += 60_000;
    clock.tick();
    expect(clock.presentationState.value).toBe("expired");
    expect(clock.expired.value).toBe(true);
    expect(onExpired).toHaveBeenCalledTimes(1);
    clock.destroy();
  });
});

describe("cocreationApi prefixing and encoding", () => {
  it("prefixes /cocreation when mounted under /cocreation and encodes path IDs", async () => {
    expect(resolveApiPrefix("/cocreation/index.html")).toBe("/cocreation");
    expect(resolveApiPrefix("/index.html")).toBe("");

    const fetched: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      fetched.push(String(input));
      return new Response(JSON.stringify(makeSession()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const client = createCocreationApi({
      apiPrefix: "/cocreation",
      fetchImpl: fetchMock as unknown as typeof fetch,
    });

    await client.getSession("sess/with space");
    expect(fetched[0]).toBe("/cocreation/api/sessions/sess%2Fwith%20space");
  });

  it("preserves top-level requestId and X-Request-ID inside error details and marks cancelDraftRegeneration safe to retry", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/cancel")) {
        return new Response("upstream error", {
          status: 502,
          headers: { "X-Request-ID": "req-cancel-502" },
        });
      }
      return new Response(
        JSON.stringify({
          detail: { code: "UPSTREAM_ERROR", message: "Model failed", details: { attemptsUsed: 1 } },
          requestId: "req-body-top",
        }),
        {
          status: 500,
          headers: {
            "Content-Type": "application/json",
            "X-Request-ID": "req-header-500",
          },
        },
      );
    });

    const client = createCocreationApi({
      apiPrefix: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
    });

    await expect(client.getSession("s1")).rejects.toMatchObject({
      code: "UPSTREAM_ERROR",
      details: expect.objectContaining({
        attemptsUsed: 1,
        httpStatus: 500,
        requestId: "req-header-500",
      }),
    });

    // Without X-Request-ID header, top-level requestId from JSON body is still preserved
    fetchMock.mockImplementationOnce(async () =>
      new Response(
        JSON.stringify({
          detail: { code: "UPSTREAM_ERROR", message: "Model failed" },
          requestId: "req-only-body",
        }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(client.getSession("s1")).rejects.toMatchObject({
      details: expect.objectContaining({ requestId: "req-only-body" }),
    });

    await expect(client.cancelDraftRegeneration("s1", "req-1")).rejects.toMatchObject({
      retryable: true,
      details: expect.objectContaining({ requestId: "req-cancel-502", httpStatus: 502 }),
    });

    // Unified backend error envelope (top-level string detail + error/details objects)
    fetchMock.mockImplementationOnce(async () =>
      new Response(
        JSON.stringify({
          detail: "Outer wall breach",
          error: {
            code: "OPEN_OUTER_WALL",
            message: "Outer wall breach",
            details: { row: 0, column: 4, tile: "@" },
            retryable: false,
          },
          code: "OPEN_OUTER_WALL",
          message: "Outer wall breach",
          details: { row: 0, column: 4, tile: "@" },
          retryable: false,
          requestId: "req-unified-env",
        }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(client.getSession("s1")).rejects.toMatchObject({
      code: "OPEN_OUTER_WALL",
      retryable: false,
      details: expect.objectContaining({
        row: 0,
        column: 4,
        tile: "@",
        httpStatus: 400,
        requestId: "req-unified-env",
      }),
    });
  });
});

describe("unityBridge protocol v5 & safe URL validation", () => {
  it("validates event.source === window.parent, rejects unsafe play URLs, and cleans up on destroy", async () => {
    const parentPost = vi.fn();
    const parentWin = { postMessage: parentPost } as unknown as Window;
    const listeners = new Map<string, Set<EventListener>>();
    const assignMock = vi.fn();

    const fakeWin = {
      parent: parentWin,
      location: {
        href: "https://sokobanaidemo.top/cocreation/",
        origin: "https://sokobanaidemo.top",
        hostname: "sokobanaidemo.top",
        assign: assignMock,
      },
      addEventListener(type: string, fn: EventListener) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type)!.add(fn);
      },
      removeEventListener(type: string, fn: EventListener) {
        listeners.get(type)?.delete(fn);
      },
    } as unknown as Window;

    const onHostReady = vi.fn();
    const bridge = createUnityBridge(
      {
        getSessionId: () => "sess_1",
        onHostReady,
        onPlayReturn: vi.fn(),
        onDraftRegenerateReturn: vi.fn(),
      },
      fakeWin,
    );

    const dispatch = (event: Partial<MessageEvent>) => {
      listeners.get("message")?.forEach((fn) => fn(event as MessageEvent));
    };

    // Wrong source must be ignored
    dispatch({
      source: {} as MessageEventSource,
      origin: "https://sokobanaidemo.top",
      data: { type: "sokoban:cocreation-host-init", protocolVersion: PROTOCOL_VERSION, sessionId: "sess_1" },
    });
    expect(onHostReady).not.toHaveBeenCalled();

    // Valid host-init completes handshake
    dispatch({
      source: parentWin,
      origin: "https://sokobanaidemo.top",
      data: { type: "sokoban:cocreation-host-init", protocolVersion: PROTOCOL_VERSION, sessionId: "sess_1" },
    });
    expect(onHostReady).toHaveBeenCalledWith("sess_1");
    expect(bridge.hasEmbeddedFormalHost("sess_1")).toBe(true);
    expect(parentPost).toHaveBeenCalledWith(
      {
        type: "sokoban:cocreation-lab-ready",
        protocolVersion: PROTOCOL_VERSION,
        sessionId: "sess_1",
      },
      "https://sokobanaidemo.top",
    );

    // Unsafe javascript: playUrl is rejected
    expect(() => bridge.navigateToPlayUrl("javascript:alert(1)")).toThrow();
    expect(assignMock).not.toHaveBeenCalled();

    // Safe relative URL succeeds
    bridge.navigateToPlayUrl("/play?ticket=abc");
    expect(assignMock).toHaveBeenCalledWith("https://sokobanaidemo.top/play?ticket=abc");

    bridge.destroy();
    expect(listeners.get("message")?.size ?? 0).toBe(0);
    expect(bridge.hasEmbeddedFormalHost("sess_1")).toBe(false);
  });
});

describe("Coordinate route BFS & Chat components", () => {
  it("finds walkable path on 12x10 grid and blocks walls/water", () => {
    const path = findCoordinateRoute(SAMPLE_ROWS, { row: 3, column: 3 }, { row: 3, column: 6 });
    expect(path).not.toBeNull();
    expect(path?.[0]).toEqual({ row: 3, column: 3 });
    expect(path?.[path.length - 1]).toEqual({ row: 3, column: 6 });

    const blocked = findCoordinateRoute(SAMPLE_ROWS, { row: 1, column: 1 }, { row: 3, column: 3 });
    expect(blocked).toBeNull();
  });

  it("renders clarification cue, v2 revision card, binary choice card, and preserves composer input", () => {
    const s = makeSession({
      proposalFlowState: {
        active: true,
        status: "clarifying",
        interactionMode: "binary_choice",
        topicId: "topic_1",
        pendingQuestion: {
          questionId: "q_1",
          topicId: "topic_1",
          baseVersionId: "v1",
          question: "Prefer tighter walls or wider water?",
          status: "pending",
          choices: [
            { choiceId: "c_a", letter: "A", label: "Tighter walls" },
            { choiceId: "c_b", letter: "B", label: "Wider water" },
          ],
        },
      },
    });
    sessionId.value = s.sessionId;
    selectedVersionId.value = "v1";
    applySession(s);
    draftRows.value = SAMPLE_ROWS.slice();

    const binaryTurn: Turn = {
      turnId: "t_bin",
      versionId: "v1",
      sequence: 1,
      role: "assistant",
      content: "",
      language: "en",
      guidance: {
        proposalQuestion: s.proposalFlowState!.pendingQuestion!,
      },
    };
    const { container: binContainer } = render(<AssistantBubble turn={binaryTurn} />);
    expect(binContainer.textContent).toContain("A: Tighter walls");
    expect(binContainer.textContent).toContain("B: Wider water");

    const clarificationTurn: Turn = {
      turnId: "t_clar",
      versionId: "v1",
      sequence: 2,
      role: "assistant",
      content: "Could you clarify which corner you want changed?",
      language: "en",
      guidance: {
        uiCues: [{ type: "clarification", text: "Top-left or bottom-right?" }],
      },
    };
    const { container: clarContainer } = render(<AssistantBubble turn={clarificationTurn} />);
    expect(clarContainer.querySelector(".guidance-cue-clarification")).not.toBeNull();

    // Test composer typing updates signal cleanly when not in binary mode
    applySession(makeSession());
    render(<Composer />);
    const textarea = screen.getByLabelText(t("en", "messageLabel")) as HTMLTextAreaElement;
    fireEvent.input(textarea, { target: { value: "Add a detour around water" } });
    expect(composerText.value).toBe("Add a detour around water");
  });

  it("re-exports modular workflow functions and computes exponential retryDelay", () => {
    expect(typeof workflows.initializeSession).toBe("function");
    expect(typeof workflows.sendMessage).toBe("function");
    expect(typeof workflows.submitBinaryChoice).toBe("function");
    expect(typeof workflows.decideProposal).toBe("function");
    expect(typeof workflows.saveManualStage).toBe("function");
    expect(typeof workflows.regenerateDraft).toBe("function");
    expect(typeof workflows.ensureVisibleTranslations).toBe("function");
    expect(typeof workflows.destroyWorkflows).toBe("function");
    expect(workflows.retryDelay(0)).toBe(2_000);
    expect(workflows.retryDelay(2)).toBe(8_000);
    expect(workflows.retryDelay(10)).toBe(60_000);
  });

  it("wires map/mini-map tile tooltips with aria-describedby restoration, challengeCard fallback, hypothesisId, and DraftPreview title", async () => {
    vi.useFakeTimers();
    const s = makeSession({
      demoMode: false,
      initialDraftMethod: "description_generation",
      turns: [
        {
          turnId: "t_challenge",
          versionId: "v1",
          sequence: 1,
          role: "assistant",
          content: "Let us compare directions.",
          language: "en",
          guidance: {
            challengeCard: {
              status: "active",
              subject: "ai_revision_challenge",
              phase: "choice_pending",
              challengeId: "ch_fallback_1",
              userPosition: "Keep corridor tight",
              coreDisagreement: "Box placement",
              nextQuestion: "Use original plan?",
            },
          },
        },
      ],
      proposals: [
        {
          proposalId: "prop_1",
          baseVersionId: "v1",
          status: "pending",
          summary: "Move box",
          proposedRows: SAMPLE_ROWS.slice(),
          diff: [{ x: 4, y: 2, before: ".", after: "#" }],
        },
      ],
      progressContexts: [
        {
          versionId: "v1",
          questionRecords: [],
          designInclinations: [
            {
              hypothesisId: "hyp_1",
              statement: "Keep tight corridor",
              confirmedAtStageNumber: 1,
              evidenceTrail: [],
            },
          ],
        },
      ],
    });
    sessionId.value = s.sessionId;
    selectedVersionId.value = "v1";
    applySession(s);
    draftRows.value = SAMPLE_ROWS.slice();
    landingMode.value = "formal";

    expect(selectedStageHasActiveDisagreement.value).toBe(true);
    expect(activeChallengeState.value).toEqual({
      challengeId: "ch_fallback_1",
      status: "choice_pending",
    });

    const { container } = render(
      <div>
        <StageSidebar />
        <AssistantBubble turn={s.turns[0]} />
        <ProposalCard />
        <ProgressDrawer />
        <MapPanel />
        <DraftPreview />
        <TooltipOverlay />
      </div>,
    );

    expect(container.querySelector(".disagreement-card")?.textContent).toContain("Use original plan?");
    expect(container.querySelector(".progress-item-inclination")?.textContent).toContain("Keep tight corridor");

    // Focus a tile in MapPanel -> TooltipOverlay opens immediately and sets aria-describedby="uiTooltip"
    const tileBtn = container.querySelector<HTMLButtonElement>('.map-grid button[data-x="2"][data-y="2"]')!;
    fireEvent.focusIn(tileBtn);
    await Promise.resolve();
    expect(tileBtn.getAttribute("aria-describedby")).toContain("uiTooltip");
    expect(document.querySelector("#uiTooltip")?.textContent).toContain("P");

    fireEvent.focusOut(tileBtn, { relatedTarget: document.body });
    await Promise.resolve();
    expect(tileBtn.getAttribute("aria-describedby")).toBeNull();

    // Hover a tile in StageSidebar .mini-map -> stagePreview tooltip
    const stageMiniCell = container.querySelector<HTMLElement>('.stage-card .mini-map i[data-x="2"][data-y="2"]')!;
    const hoverEv1 = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(hoverEv1, "pointerType", { value: "mouse" });
    stageMiniCell.dispatchEvent(hoverEv1);
    await vi.advanceTimersByTimeAsync(400);
    expect(document.querySelector("#uiTooltip")?.textContent).toContain(t("en", "help.tileStagePreview"));

    // Hover a tile in ProposalCard .mini-map.proposal-map -> preview tooltip
    const proposalMiniCell = container.querySelector<HTMLElement>('.proposal-map i[data-x="5"][data-y="2"]')!;
    const hoverEv2 = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(hoverEv2, "pointerType", { value: "mouse" });
    proposalMiniCell.dispatchEvent(hoverEv2);
    await vi.advanceTimersByTimeAsync(400);
    expect(document.querySelector("#uiTooltip")?.textContent).toContain(t("en", "help.tilePreview"));

    // DraftPreview distinguishes formal vs demo preview titles on landing
    applySession(makeSession({ languageLocked: false, demoMode: false, initialDraftMethod: "description_generation" }));
    landingMode.value = "formal";
    const { container: formalDraftContainer } = render(<DraftPreview />);
    const formalTitle = formalDraftContainer.querySelector("#draftPreviewTitle")?.textContent;
    expect(formalTitle).toContain(t("en", "description_generation"));

    applySession(makeSession({ languageLocked: false, demoMode: true, initialDraftMethod: "algorithm_demo" }));
    landingMode.value = "demo-preview";
    const { container: demoDraftContainer } = render(<DraftPreview />);
    const demoTitle = demoDraftContainer.querySelector("#draftPreviewTitle")?.textContent;
    expect(demoTitle).toContain(t("en", "algorithm_demo"));
    expect(formalTitle).not.toBe(demoTitle);

    vi.useRealTimers();
  });

  it("gates Composer on choice_pending challenge, ProposalCard on active disagreement, and MapPanel/TilePalette during binary flow", async () => {
    vi.useFakeTimers();

    // 1. Challenge choice_pending disables Composer free-text input and ProposalCard accept/reject
    const challengeSession = makeSession({
      turns: [
        {
          turnId: "t_ch",
          versionId: "v1",
          sequence: 1,
          role: "assistant",
          content: "Compare both directions.",
          language: "en",
          guidance: {
            challengeCard: {
              status: "active",
              subject: "ai_revision_challenge",
              phase: "choice_pending",
              challengeId: "ch_gate_1",
              nextQuestion: "Use original plan?",
            },
          },
        },
      ],
      proposals: [
        {
          proposalId: "prop_gate",
          baseVersionId: "v1",
          status: "pending",
          summary: "Add wall",
          proposedRows: SAMPLE_ROWS.slice(),
          diff: [{ x: 4, y: 2, before: ".", after: "#" }],
        },
      ],
      interactionState: {
        versionId: "v1",
        revision: 2,
        proposalBlocked: true,
        recoveries: [],
      },
    });
    sessionId.value = challengeSession.sessionId;
    selectedVersionId.value = "v1";
    applySession(challengeSession);
    draftRows.value = SAMPLE_ROWS.slice();

    const { container: gateContainer } = render(
      <div>
        <Composer />
        <ProposalCard />
      </div>,
    );
    const textarea = gateContainer.querySelector<HTMLTextAreaElement>("#messageInput")!;
    expect(textarea.disabled).toBe(true);
    expect(textarea.getAttribute("placeholder")).toBe(t("en", "challengeChoicePlaceholder"));

    const proposalBtns = gateContainer.querySelectorAll<HTMLButtonElement>(".proposal-actions button");
    expect(proposalBtns).toHaveLength(2);
    expect(proposalBtns[0].disabled).toBe(true);
    expect(proposalBtns[1].disabled).toBe(true);
    expect(gateContainer.querySelector(".proposal-card")?.textContent).toContain(
      t("en", "proposalDisagreementActive"),
    );

    // 2. Binary discovery active + dirty draft blocks MapPanel/TilePalette/editTile and shows explicit reset hint
    const binarySession = makeSession({
      proposalFlowState: {
        active: true,
        status: "clarifying",
        interactionMode: "binary_choice",
        topicId: "topic_gate",
      },
    });
    applySession(binarySession);
    const dirtyRows = SAMPLE_ROWS.slice();
    dirtyRows[1] = "#.#........#";
    draftRows.value = dirtyRows;

    // editTile must be ignored during active binary flow
    editTile(4, 1);
    expect(draftRows.value[1]).toBe("#.#........#");

    const { container: mapContainer } = render(
      <div>
        <MapPanel />
        <TooltipOverlay />
      </div>,
    );
    expect(mapContainer.querySelector("#mapControlHint")?.textContent).toContain(
      t("en", "hint.binaryDraftReset"),
    );
    const saveBtn = mapContainer.querySelector<HTMLButtonElement>(".map-actions .primary-button")!;
    const discardBtn = mapContainer.querySelector<HTMLButtonElement>(".map-actions .secondary-button")!;
    expect(saveBtn.disabled).toBe(true);
    expect(discardBtn.disabled).toBe(false);

    // TilePalette uses aria-disabled="true" so hover/focus still triggers blocked reason tooltip
    const paletteBtn = mapContainer.querySelector<HTMLButtonElement>(".palette-button")!;
    expect(paletteBtn.disabled).toBe(false);
    expect(paletteBtn.getAttribute("aria-disabled")).toBe("true");
    fireEvent.focus(paletteBtn);
    await Promise.resolve();
    expect(document.querySelector("#uiTooltip")?.textContent).toContain(
      t("en", "hint.binaryDraftReset"),
    );
    fireEvent.blur(paletteBtn);

    vi.useRealTimers();
  });

  it("delegates DraftPreview tooltips with aria-hidden cells and restores StageSidebar card tooltip on mini-map exit", async () => {
    vi.useFakeTimers();
    const s = makeSession({ languageLocked: false, demoMode: true });
    sessionId.value = s.sessionId;
    selectedVersionId.value = "v1";
    applySession(s);
    landingMode.value = "demo-preview";

    const { container } = render(
      <div>
        <DraftPreview />
        <StageSidebar />
        <TooltipOverlay />
      </div>,
    );

    const previewCells = container.querySelectorAll<HTMLElement>(".draft-preview-cell");
    expect(previewCells).toHaveLength(120);
    for (const cell of previewCells) {
      expect(cell.getAttribute("aria-hidden")).toBe("true");
    }

    const playerPreviewCell = container.querySelector<HTMLElement>(
      '.draft-preview-cell[data-x="2"][data-y="2"]',
    )!;
    const cellHover = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(cellHover, "pointerType", { value: "mouse" });
    playerPreviewCell.dispatchEvent(cellHover);
    await vi.advanceTimersByTimeAsync(400);
    expect(document.querySelector("#uiTooltip")?.textContent).toContain("P");

    // StageSidebar: hover mini-map -> leave mini-map onto stage-card -> outer stage tooltip is restored
    const stageCard = container.querySelector<HTMLButtonElement>(".stage-card")!;
    const miniMap = stageCard.querySelector<HTMLElement>(".mini-map")!;
    const miniCell = miniMap.querySelector<HTMLElement>('i[data-x="2"][data-y="2"]')!;
    const miniHover = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(miniHover, "pointerType", { value: "mouse" });
    miniCell.dispatchEvent(miniHover);
    await vi.advanceTimersByTimeAsync(400);
    expect(document.querySelector("#uiTooltip")?.textContent).toContain(
      t("en", "help.tileStagePreview"),
    );

    fireEvent.pointerLeave(miniMap);
    expect(document.querySelector("#uiTooltip")).toBeNull();

    const cardTop = stageCard.querySelector<HTMLElement>(".stage-card-top")!;
    const backToCard = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(backToCard, "pointerType", { value: "mouse" });
    cardTop.dispatchEvent(backToCard);
    await vi.advanceTimersByTimeAsync(400);
    expect(document.querySelector("#uiTooltip")?.textContent).toContain(t("en", "help.stageBody"));

    vi.useRealTimers();
  });

  it("maps newly added backend error codes, auto-refreshes stale actions, and retries missing translations on partial 200 OK", async () => {
    const newCodes = [
      "PENDING_PROPOSAL",
      "TURN_NOT_FOUND",
      "STALE_REVISION_BINDING",
      "UNBOUND_REVISION",
      "STALE_PROPOSAL",
      "QUESTION_FEEDBACK_STALE",
      "INTENT_HYPOTHESIS_STALE",
      "DEADLINE_EXCEEDED",
      "MODEL_OUTPUT_TRUNCATED",
      "MODEL_JSON_INVALID",
      "MODEL_SCHEMA_INVALID",
      "MODEL_VALIDATION_FAILED",
      "UPSTREAM_AUTHENTICATION_FAILED",
      "UPSTREAM_REQUEST_REJECTED",
    ];
    for (const code of newCodes) {
      expect(localizeError("en", { code })).not.toBe(t("en", "errors.GENERIC"));
      expect(localizeError("zh-CN", { code })).not.toBe(t("zh-CN", "errors.GENERIC"));
      expect(formatTechnicalError("en", { code })?.message).not.toBe(t("en", "errors.GENERIC"));
      expect(formatTechnicalError("zh-CN", { code })?.message).not.toBe(
        t("zh-CN", "errors.GENERIC"),
      );
    }

    // Auto-refresh on STALE_PROPOSAL in decideProposal
    const s = makeSession({
      proposals: [
        {
          proposalId: "prop_stale",
          baseVersionId: "v1",
          status: "pending",
          summary: "Move box",
          proposedRows: SAMPLE_ROWS.slice(),
          diff: [],
        },
      ],
    });
    sessionId.value = s.sessionId;
    selectedVersionId.value = "v1";
    applySession(s);

    const refreshedSession = makeSession({
      proposals: [{ ...s.proposals[0], status: "superseded" }],
    });
    vi.spyOn(api, "decideProposal").mockRejectedValueOnce({
      code: "STALE_PROPOSAL",
      message: "Stale",
      retryable: false,
    });
    const getSessionSpy = vi.spyOn(api, "getSession").mockResolvedValue(refreshedSession);
    await workflows.decideProposal(s.proposals[0], "accept");
    expect(getSessionSpy).toHaveBeenCalledWith("sess_1");
    expect(session.value?.proposals[0].status).toBe("superseded");

    // Partial 200 OK from translateTurns only clears turns with targetLanguage and schedules bounded retry for missing ones
    vi.useFakeTimers();
    const transSession = makeSession({
      language: "zh-CN",
      turns: [
        {
          turnId: "t1",
          versionId: "v1",
          sequence: 1,
          role: "assistant",
          content: "First",
          language: "en",
        },
        {
          turnId: "t2",
          versionId: "v1",
          sequence: 2,
          role: "assistant",
          content: "Second",
          language: "en",
        },
      ],
    });
    language.value = "zh-CN";
    applySession(transSession);

    const translateSpy = vi
      .spyOn(api, "translateTurns")
      .mockResolvedValueOnce({
        ...transSession,
        turns: [
          {
            ...transSession.turns[0],
            translations: { "zh-CN": { body: "第一条" } },
          },
          transSession.turns[1],
        ],
      })
      .mockResolvedValue({
        ...transSession,
        turns: [
          {
            ...transSession.turns[0],
            translations: { "zh-CN": { body: "第一条" } },
          },
          {
            ...transSession.turns[1],
            translations: { "zh-CN": { body: "第二条" } },
          },
        ],
      });

    await workflows.ensureVisibleTranslations();
    expect(translateSpy).toHaveBeenCalledTimes(1);
    expect(translationFailures.value.has("t1:zh-CN")).toBe(false);
    expect(translationFailures.value.has("t2:zh-CN")).toBe(true);

    await vi.advanceTimersByTimeAsync(5_000);
    expect(translateSpy).toHaveBeenCalledTimes(2);
    expect(translationFailures.value.has("t2:zh-CN")).toBe(false);

    vi.useRealTimers();
  });
});
