import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { createRef } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import {
  ApiError,
  COCREATION_BRIDGE_EVENT_TYPES,
  ConfirmDialog,
  NoticeBanner,
  PROTOCOL_VERSION,
  SokobanMap,
  bridgeStateChanged,
  buildEntityLabels,
  buildUrl,
  createApiClient,
  fetchJson,
  interpolatePath,
  isDraftRegenerateAbortEvent,
  isDraftRegenerateAckEvent,
  isDraftRegeneratePrepareAckEvent,
  isDraftRegeneratePrepareEvent,
  isDraftRegenerateRequestEvent,
  isDraftRegenerateReturnEvent,
  isHostInitEvent,
  isLabReadyEvent,
  isPlayAckEvent,
  isPlayRequestEvent,
  isPlayReturnEvent,
  isSafeNavigationUrl,
  isShowLabEvent,
  isShowUnityEvent,
  parseBridgeEvent,
  prepareRejectionReason,
  requestRejectionReason,
  shouldHandleSource,
  validateSafeNavigationUrl,
} from "../index";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const SAMPLE_ROWS = [
  "############",
  "#..........#",
  "#..p..s..t.#",
  "#.....s..t.#",
  "#..@.......#",
  "#..........#",
  "#..........#",
  "#..........#",
  "#..........#",
  "############",
];

describe("SokobanMap", () => {
  it("renders readonly mode with role=img, accessible summary, entity labels, and diff markers", () => {
    const labels = buildEntityLabels(SAMPLE_ROWS);
    expect(labels.get("3,2")).toBe("P");
    expect(labels.get("6,2")).toBe("B1");
    expect(labels.get("6,3")).toBe("B2");
    expect(labels.get("9,2")).toBe("T1");
    expect(labels.get("9,3")).toBe("T2");

    render(
      <SokobanMap
        rows={SAMPLE_ROWS}
        diff={[{ x: 6, y: 2 }]}
        mini={true}
      />,
    );
    const img = screen.getByRole("img");
    expect(img.className).toContain("sokoban-map--mini");
    expect(img.getAttribute("aria-label")).toContain("12 by 10 Sokoban map");
    expect(img.getAttribute("aria-label")).toContain("1 player, 2 boxes, 2 targets");

    const changedCell = document.querySelector('[data-x="6"][data-y="2"]')!;
    expect(changedCell.className).toContain("changed");
    expect(changedCell.textContent).toBe("B1");
    expect(changedCell.getAttribute("title")).toBe("B1 (row 3, column 7)");
    const wallCell = document.querySelector('[data-x="0"][data-y="0"]')!;
    expect(wallCell.getAttribute("title")).toBe("wall (1, 1)");
  });

  it("delegates onTileInspect for pointer hover (ignoring touch) and keyboard focus", () => {
    const onTileInspect = vi.fn();
    render(
      <SokobanMap
        rows={SAMPLE_ROWS}
        mode="editor"
        onTileInspect={onTileInspect}
      />,
    );

    const grid = screen.getByRole("group");
    const playerBtn = document.querySelector<HTMLButtonElement>(
      'button[data-x="3"][data-y="2"]',
    )!;

    // Touch pointerover is ignored
    const touchEv = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(touchEv, "pointerType", { value: "touch" });
    playerBtn.dispatchEvent(touchEv);
    expect(onTileInspect).not.toHaveBeenCalled();

    // Mouse pointerover inspects tile with immediate: false and entityLabel
    const mouseEv = new Event("pointerover", { bubbles: true }) as PointerEvent;
    Object.defineProperty(mouseEv, "pointerType", { value: "mouse" });
    playerBtn.dispatchEvent(mouseEv);
    expect(onTileInspect).toHaveBeenLastCalledWith({
      anchor: playerBtn,
      coord: { x: 3, y: 2 },
      tile: "p",
      entityLabel: "P",
      immediate: false,
    });

    // Pointer leave emits null
    fireEvent.pointerLeave(grid);
    expect(onTileInspect).toHaveBeenLastCalledWith(null);

    // Focus-in inspects tile with immediate: true; focus-out outside grid emits null
    fireEvent.focusIn(playerBtn);
    expect(onTileInspect).toHaveBeenLastCalledWith({
      anchor: playerBtn,
      coord: { x: 3, y: 2 },
      tile: "p",
      entityLabel: "P",
      immediate: true,
    });
    fireEvent.focusOut(playerBtn, { relatedTarget: document.body });
    expect(onTileInspect).toHaveBeenLastCalledWith(null);
  });

  it("uses event delegation and arrow-key navigation in editor mode without destroying button identity/focus", () => {
    let currentRows = [...SAMPLE_ROWS];
    const onTileClick = vi.fn(({ x, y }: { x: number; y: number }) => {
      const chars = [...currentRows[y]];
      chars[x] = "#";
      currentRows = currentRows.map((row, idx) =>
        idx === y ? chars.join("") : row,
      );
    });

    const { rerender } = render(
      <SokobanMap rows={currentRows} mode="editor" onTileClick={onTileClick} />,
    );

    const targetButton = document.querySelector<HTMLButtonElement>(
      'button[data-x="4"][data-y="2"]',
    )!;
    targetButton.focus();
    expect(document.activeElement).toBe(targetButton);

    // Arrow key navigation moves focus across grid cells
    fireEvent.keyDown(targetButton, { key: "ArrowRight" });
    expect(document.activeElement).toBe(
      document.querySelector('button[data-x="5"][data-y="2"]'),
    );
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(
      document.querySelector('button[data-x="5"][data-y="3"]'),
    );
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(document.activeElement).toBe(targetButton);

    // Click triggers delegated handler and preserves exact DOM node + focus after rerender
    fireEvent.click(targetButton);
    expect(onTileClick).toHaveBeenCalledWith({ x: 4, y: 2 }, ".");

    rerender(
      <SokobanMap rows={currentRows} mode="editor" onTileClick={onTileClick} />,
    );
    const sameButton = document.querySelector<HTMLButtonElement>(
      'button[data-x="4"][data-y="2"]',
    )!;
    expect(sameButton).toBe(targetButton);
    expect(document.activeElement).toBe(sameButton);
    expect(sameButton.dataset.tile).toBe("#");
  });

  it("ignores clicks when disabled in editor mode", () => {
    const onTileClick = vi.fn();
    render(
      <SokobanMap
        rows={SAMPLE_ROWS}
        mode="editor"
        disabled={true}
        onTileClick={onTileClick}
      />,
    );
    const btn = document.querySelector<HTMLButtonElement>('button[data-x="1"][data-y="1"]')!;
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(btn);
    expect(onTileClick).not.toHaveBeenCalled();
  });

  it("keeps disabled tiles inspectable via aria-disabled when onTileInspect is provided", () => {
    const onTileClick = vi.fn();
    const onTileInspect = vi.fn();
    render(
      <SokobanMap
        rows={SAMPLE_ROWS}
        mode="editor"
        disabled={true}
        onTileClick={onTileClick}
        onTileInspect={onTileInspect}
      />,
    );
    const btn = document.querySelector<HTMLButtonElement>('button[data-x="3"][data-y="2"]')!;
    expect(btn.disabled).toBe(false);
    expect(btn.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(btn);
    expect(onTileClick).not.toHaveBeenCalled();
    fireEvent.focusIn(btn);
    expect(onTileInspect).toHaveBeenCalledWith(
      expect.objectContaining({
        anchor: btn,
        coord: { x: 3, y: 2 },
        immediate: true,
      }),
    );
  });
});

describe("ConfirmDialog", () => {
  it("supports Escape cancel, backdrop click, initialFocusRef, confirm, and focus restoration", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "Trigger";
    document.body.appendChild(trigger);
    trigger.focus();

    const inputRef = createRef<HTMLInputElement>();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();

    const { rerender } = render(
      <ConfirmDialog
        open={true}
        title="Confirm Stage"
        description="Lock editing?"
        confirmLabel="Lock"
        cancelLabel="Back"
        initialFocusRef={inputRef}
        onConfirm={onConfirm}
        onCancel={onCancel}
      >
        <input ref={inputRef} aria-label="Note" />
      </ConfirmDialog>,
    );

    await Promise.resolve();
    const dialog = document.querySelector("dialog")!;
    expect(dialog.open || dialog.hasAttribute("open")).toBe(true);
    expect(document.activeElement).toBe(inputRef.current);

    fireEvent.click(screen.getByRole("button", { name: "Lock" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    // Clicking backdrop (dialog itself) triggers onCancel
    fireEvent.click(dialog);
    expect(onCancel).toHaveBeenCalledTimes(1);

    // Firing cancel event (Escape) triggers onCancel
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onCancel).toHaveBeenCalledTimes(2);

    // While busy, Escape and backdrop clicks do not cancel
    rerender(
      <ConfirmDialog
        open={true}
        busy={true}
        title="Confirm Stage"
        description="Lock editing?"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(dialog);
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onCancel).toHaveBeenCalledTimes(2);

    rerender(
      <ConfirmDialog
        open={false}
        title="Confirm Stage"
        description="Lock editing?"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });
});

describe("NoticeBanner", () => {
  it("clears previous timeout when a new notice replaces an existing one and supports dismiss/action", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    const onAction = vi.fn();

    const { rerender } = render(
      <NoticeBanner
        message="First"
        noticeKey={1}
        durationMs={4000}
        onDismiss={onDismiss}
      />,
    );

    vi.advanceTimersByTime(3000);
    expect(onDismiss).not.toHaveBeenCalled();

    rerender(
      <NoticeBanner
        message="Second"
        noticeKey={2}
        variant="error"
        durationMs={4000}
        actionLabel="Retry"
        onAction={onAction}
        onDismiss={onDismiss}
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("Second");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onAction).toHaveBeenCalledTimes(1);

    // At t=4500ms, the old timer from "First" would have fired if not cleared!
    vi.advanceTimersByTime(1500);
    expect(onDismiss).not.toHaveBeenCalled();

    // At t=7000ms (4000ms after "Second"), the new timer fires once.
    vi.advanceTimersByTime(2500);
    expect(onDismiss).toHaveBeenCalledTimes(1);

    // Manual dismiss works cleanly and clears timer on unmount
    fireEvent.click(screen.getByRole("button", { name: "Dismiss notice" }));
    expect(onDismiss).toHaveBeenCalledTimes(2);
  });
});

describe("API client and URL safety", () => {
  it("encodes path parameters and builds safe URLs", () => {
    expect(
      buildUrl("https://sokobanaidemo.top/", ["api", "sessions", "a/b c"], {
        lang: "zh-CN",
        empty: undefined,
      }),
    ).toBe("https://sokobanaidemo.top/api/sessions/a%2Fb%20c?lang=zh-CN");

    expect(
      interpolatePath("/api/sessions/{sessionId}/stages/{stageId}", {
        sessionId: "s/1?x=2",
        stageId: "st 3",
      }),
    ).toBe("/api/sessions/s%2F1%3Fx%3D2/stages/st%203");
  });

  it("blocks javascript:, data:, vbscript:, and protocol-relative/backslash host escapes", () => {
    expect(() => validateSafeNavigationUrl("javascript:alert(1)")).toThrow(
      ApiError,
    );
    expect(() =>
      validateSafeNavigationUrl("  JaVaScRiPt:alert(1)"),
    ).toThrow(ApiError);
    expect(() =>
      validateSafeNavigationUrl("data:text/html,<script>alert(1)</script>"),
    ).toThrow(ApiError);
    expect(() => validateSafeNavigationUrl("vbscript:msgbox(1)")).toThrow(
      ApiError,
    );
    expect(() => validateSafeNavigationUrl("//evil.com/phish")).toThrow(
      ApiError,
    );
    expect(() => validateSafeNavigationUrl("/\\evil.com")).toThrow(ApiError);
    expect(() => validateSafeNavigationUrl("\\evil.com")).toThrow(ApiError);
    expect(
      isSafeNavigationUrl("https://evil.com", {
        allowedOrigins: ["https://sokobanaidemo.top"],
      }),
    ).toBe(false);
    expect(
      validateSafeNavigationUrl("/frontend/", {
        baseUrl: "https://sokobanaidemo.top/webgl/",
      }),
    ).toBe("https://sokobanaidemo.top/frontend/");
  });

  it("validates responses with Zod, structures HTTP ApiError, and enforces AbortController timeouts", async () => {
    const schema = z.object({ ok: z.boolean() });
    const goodFetch = vi.fn(
      async () => new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );
    const client = createApiClient({
      baseUrl: "https://sokobanaidemo.top",
      fetchImpl: goodFetch,
    });
    await expect(client.fetchJson("/api/health", { schema })).resolves.toEqual({
      ok: true,
    });

    const badSchemaFetch = vi.fn(
      async () => new Response(JSON.stringify({ ok: "nope" }), { status: 200 }),
    );
    await expect(
      fetchJson("/api/health", { fetchImpl: badSchemaFetch, schema }),
    ).rejects.toMatchObject({ code: "SCHEMA_VALIDATION_ERROR", retryable: false });

    const httpErrFetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ detail: { code: "RATE_LIMIT", message: "Slow down" } }),
          { status: 429, headers: { "X-Request-ID": "req-hdr-429" } },
        ),
    );
    await expect(
      fetchJson("/api/chat", { fetchImpl: httpErrFetch }),
    ).rejects.toMatchObject({
      status: 429,
      code: "RATE_LIMIT",
      message: "Slow down",
      requestId: "req-hdr-429",
      retryable: true,
    });

    // Unified backend error envelope with string detail + structured error/details
    const unifiedErrFetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            detail: "Model failed",
            error: {
              code: "UPSTREAM_ERROR",
              message: "Model failed",
              details: { attemptsUsed: 2 },
              retryable: true,
            },
            code: "UPSTREAM_ERROR",
            message: "Model failed",
            details: { attemptsUsed: 2, row: 1 },
            retryable: true,
            requestId: "req-unified-1",
          }),
          { status: 502, headers: { "Content-Type": "application/json" } },
        ),
    );
    await expect(
      fetchJson("/api/chat", { fetchImpl: unifiedErrFetch }),
    ).rejects.toMatchObject({
      status: 502,
      code: "UPSTREAM_ERROR",
      message: "Model failed",
      requestId: "req-unified-1",
      retryable: true,
      detail: {
        code: "UPSTREAM_ERROR",
        message: "Model failed",
        retryable: true,
        details: { attemptsUsed: 2, row: 1 },
      },
    });

    const detailsOnlyFetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            detail: "Outer wall breach",
            code: "OPEN_OUTER_WALL",
            details: { row: 0, column: 2, tile: "@" },
            retryable: false,
          }),
          { status: 400, headers: { "Content-Type": "application/json" } },
        ),
    );
    await expect(
      fetchJson("/api/versions", { fetchImpl: detailsOnlyFetch }),
    ).rejects.toMatchObject({
      status: 400,
      code: "OPEN_OUTER_WALL",
      message: "Outer wall breach",
      retryable: false,
      detail: {
        details: { row: 0, column: 2, tile: "@" },
      },
    });

    vi.useFakeTimers();
    const slowFetch = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );
    const pending = fetchJson("/api/slow", {
      fetchImpl: slowFetch,
      timeoutMs: 500,
    });
    vi.advanceTimersByTime(500);
    await expect(pending).rejects.toMatchObject({
      code: "CLIENT_TIMEOUT",
      retryable: true,
    });
  });
});

describe("Bridge protocol", () => {
  it("validates all 13 sokoban:cocreation-* schemas and individual type guards", () => {
    expect(PROTOCOL_VERSION).toBe(5);
    expect(COCREATION_BRIDGE_EVENT_TYPES).toHaveLength(13);

    const events = [
      [{ type: "sokoban:cocreation-host-init", protocolVersion: 5, sessionId: "s1" }, isHostInitEvent],
      [{ type: "sokoban:cocreation-lab-ready", protocolVersion: 5, sessionId: "s1" }, isLabReadyEvent],
      [{ type: "sokoban:cocreation-show-unity", protocolVersion: 5, sessionId: "s1" }, isShowUnityEvent],
      [{ type: "sokoban:cocreation-show-lab", protocolVersion: 5, sessionId: "s1", requestId: "r1" }, isShowLabEvent],
      [{ type: "sokoban:cocreation-play-request", requestId: "req1", sessionId: "s1", playUrl: "https://sokobanaidemo.top/play", protocolVersion: 5 }, isPlayRequestEvent],
      [{ type: "sokoban:cocreation-play-ack", requestId: "req1", accepted: true, protocolVersion: 5 }, isPlayAckEvent],
      [{ type: "sokoban:cocreation-play-return", protocolVersion: 5, sessionId: "s1", status: "completed" }, isPlayReturnEvent],
      [{ type: "sokoban:cocreation-draft-regenerate-prepare", requestId: "p1", sessionId: "s1", protocolVersion: 5 }, isDraftRegeneratePrepareEvent],
      [{ type: "sokoban:cocreation-draft-regenerate-prepare-ack", requestId: "p1", sessionId: "s1", accepted: true, reason: "", protocolVersion: 5 }, isDraftRegeneratePrepareAckEvent],
      [{ type: "sokoban:cocreation-draft-regenerate-request", requestId: "r1", prepareRequestId: "p1", protocolVersion: 5, sessionId: "s1", regenerationRequestId: "regen1" }, isDraftRegenerateRequestEvent],
      [{ type: "sokoban:cocreation-draft-regenerate-ack", requestId: "r1", sessionId: "s1", regenerationRequestId: "regen1", accepted: true, reason: "", protocolVersion: 5 }, isDraftRegenerateAckEvent],
      [{ type: "sokoban:cocreation-draft-regenerate-abort", protocolVersion: 5, sessionId: "s1", prepareRequestId: "p1" }, isDraftRegenerateAbortEvent],
      [{ type: "sokoban:cocreation-draft-regenerate-return", protocolVersion: 5, sessionId: "s1", requestId: "regen1", status: "completed" }, isDraftRegenerateReturnEvent],
    ] as const;

    for (const [sample, guard] of events) {
      expect(parseBridgeEvent(sample)).toEqual(sample);
      expect(guard(sample)).toBe(true);
    }
    expect(parseBridgeEvent({ type: "sokoban:cocreation-unknown" })).toBeNull();
    expect(isHostInitEvent({ type: "sokoban:cocreation-host-init", protocolVersion: 5, sessionId: "" })).toBe(false);
  });

  it("evaluates all bridge state-machine helpers accurately", () => {
    const baseState = {
      protocolVersion: PROTOCOL_VERSION,
      activeProtocolVersion: PROTOCOL_VERSION,
      activeSessionId: "s1",
      unityReady: true,
      receiverReady: true,
    };
    expect(prepareRejectionReason(baseState, { protocolVersion: 5, sessionId: "s1" })).toBe("");
    expect(prepareRejectionReason(baseState, { protocolVersion: 4, sessionId: "s1" })).toBe("protocol_mismatch");
    expect(prepareRejectionReason({ ...baseState, unityReady: false }, { protocolVersion: 5, sessionId: "s1" })).toBe("unity_not_ready");
    expect(prepareRejectionReason({ ...baseState, receiverReady: false }, { protocolVersion: 5, sessionId: "s1" })).toBe("receiver_not_ready");
    expect(prepareRejectionReason(baseState, { protocolVersion: 5, sessionId: "other" })).toBe("session_mismatch");

    const source = {} as MessageEventSource;
    const prepared = {
      requestId: "p1",
      sessionId: "s1",
      source,
      origin: "https://sokobanaidemo.top",
      expiresAt: 1000,
    };
    expect(
      requestRejectionReason(
        { ...baseState, source, origin: "https://sokobanaidemo.top" },
        { protocolVersion: 5, sessionId: "s1", prepareRequestId: "p1", regenerationRequestId: "rg1" },
        null,
        500,
      ),
    ).toBe("prepare_missing");
    expect(
      requestRejectionReason(
        { ...baseState, source, origin: "https://sokobanaidemo.top" },
        { protocolVersion: 5, sessionId: "s1", prepareRequestId: "p1", regenerationRequestId: "rg1" },
        prepared,
        2000,
      ),
    ).toBe("prepare_expired");
    expect(
      requestRejectionReason(
        { ...baseState, source, origin: "https://evil.example" },
        { protocolVersion: 5, sessionId: "s1", prepareRequestId: "p1", regenerationRequestId: "rg1" },
        prepared,
        500,
      ),
    ).toBe("request_mismatch");
    expect(
      requestRejectionReason(
        { ...baseState, source, origin: "https://sokobanaidemo.top" },
        { protocolVersion: 5, sessionId: "s1", prepareRequestId: "p1", regenerationRequestId: "rg1" },
        prepared,
        500,
      ),
    ).toBe("");

    expect(bridgeStateChanged("s1", 5, "s1", 5)).toBe(false);
    expect(bridgeStateChanged("s1", 5, "s2", 5)).toBe(true);
    expect(bridgeStateChanged("s1", 5, "s1", 4)).toBe(true);
    expect(shouldHandleSource(true)).toBe(true);
    expect(shouldHandleSource(false)).toBe(false);
    expect(shouldHandleSource(1)).toBe(false);
  });
});
