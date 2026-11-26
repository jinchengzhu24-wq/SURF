import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { PROTOCOL_VERSION } from "@sokoban/ui-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createCocreationApi, resolveApiPrefix } from "../api/cocreationApi";
import { createUnityBridge } from "../bridge/unityBridge";
import { AssistantBubble } from "../components/Chat/AssistantBubble";
import { Composer } from "../components/Chat/Composer";
import { REQUEST_COPY, en, localizeError, t, zhCN } from "../i18n";
import { createSafeStorage } from "../storage/safeStorage";
import { composerText } from "../store/chatStore";
import { createDeadlineClock } from "../store/deadlineClock";
import { draftRows, findCoordinateRoute } from "../store/editorStore";
import { applySession, language, selectedVersionId, session, sessionId } from "../store/sessionStore";
import * as workflows from "../store/workflows";
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
});
