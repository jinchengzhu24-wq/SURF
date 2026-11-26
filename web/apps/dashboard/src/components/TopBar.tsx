import type { JSX } from "preact";
import { useEffect, useId, useRef, useState } from "preact/hooks";
import type { DashboardStore } from "../store";

export interface TopBarProps {
  store: DashboardStore;
}

export function TopBar({ store }: TopBarProps): JSX.Element {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuContainerRef = useRef<HTMLDivElement | null>(null);
  const toggleButtonRef = useRef<HTMLButtonElement | null>(null);
  const menuId = useId();

  const isLoading = store.loading.value;
  const hasPayload = store.payload.value !== null;

  useEffect(() => {
    if (!menuOpen) return undefined;

    const handlePointerDown = (event: MouseEvent) => {
      const container = menuContainerRef.current;
      if (container && event.target instanceof Node && !container.contains(event.target)) {
        setMenuOpen(false);
      }
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuOpen(false);
        toggleButtonRef.current?.focus();
      }
    };

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  const handleClearClick = () => {
    setMenuOpen(false);
    store.openClearAll();
  };

  return (
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">
          S
        </div>
        <div class="brand-copy">
          <h1>Sokoban MatchMaking Dashboard</h1>
          <div class="subtitle">
            {hasPayload
              ? `API: ${store.api.apiBase}`
              : "Loading MatchMaking records..."}
          </div>
        </div>
      </div>

      <div class="actions">
        <a
          class="pixel-btn pixel-btn--secondary topbar-export-link"
          href={store.api.exportStudyLogsUrl}
          download="study-logs.jsonl"
        >
          Export JSONL
        </a>

        <button
          type="button"
          class={`pixel-btn pixel-btn--secondary icon-button${isLoading ? " is-loading" : ""}`}
          title="Refresh data"
          aria-label="Refresh data"
          aria-busy={isLoading ? "true" : "false"}
          disabled={isLoading}
          onClick={() => void store.loadData(true)}
        >
          <svg
            class={`icon${isLoading ? " icon-spin" : ""}`}
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path d="M21 12a9 9 0 0 1-15 6.7" />
            <path d="M3 12a9 9 0 0 1 15-6.7" />
            <path d="M3 18v-5h5" />
            <path d="M21 6v5h-5" />
          </svg>
        </button>

        <div class="tools-menu" ref={menuContainerRef}>
          <button
            ref={toggleButtonRef}
            type="button"
            class="pixel-btn pixel-btn--secondary tools-trigger"
            aria-expanded={menuOpen ? "true" : "false"}
            aria-haspopup="menu"
            aria-controls={menuId}
            onClick={() => setMenuOpen((prev) => !prev)}
          >
            Developer tools
          </button>

          {menuOpen ? (
            <div class="tools-popover" id={menuId} role="menu">
              <a
                role="menuitem"
                href={store.api.exportStudyLogsUrl}
                download="study-logs.jsonl"
                onClick={() => setMenuOpen(false)}
              >
                Export Study Logs (JSONL)
              </a>
              <a
                role="menuitem"
                href={store.api.matchmakingDataUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => setMenuOpen(false)}
              >
                MatchMaking data API
              </a>
              <a
                role="menuitem"
                href={store.api.docsUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => setMenuOpen(false)}
              >
                API documentation
              </a>
              <div class="menu-divider" role="separator" />
              <button
                type="button"
                role="menuitem"
                class="menu-danger"
                onClick={handleClearClick}
              >
                Clear MatchMaking data
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
