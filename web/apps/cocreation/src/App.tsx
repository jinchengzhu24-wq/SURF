import { NoticeBanner } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect } from "preact/hooks";
import { t } from "./i18n";
import {
  formalStandaloneBlocked,
  hideGlobalNotice,
  isWorkspaceVisible,
  language,
  notice,
} from "./store/sessionStore";
import { destroyWorkflows, initializeSession } from "./store/workflows";
import { ChatPanel } from "./components/Chat/ChatPanel";
import { FinalizeModal } from "./components/Common/FinalizeModal";
import { TechnicalDetails } from "./components/Common/TechnicalDetails";
import { TooltipOverlay } from "./components/Common/Tooltip";
import { LandingView } from "./components/Landing/LandingView";
import { MapPanel } from "./components/Workspace/MapPanel";
import { StageSidebar } from "./components/Workspace/StageSidebar";
import { Topbar } from "./components/Workspace/Topbar";

export function App(): JSX.Element {
  const lang = language.value;
  const workspaceActive = isWorkspaceVisible.value;
  const showLandingScreen = !workspaceActive && !formalStandaloneBlocked.value;
  const activeNotice = notice.value;

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  useEffect(() => {
    void initializeSession();

    const closeHelpOnOutsideClick = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      document.querySelectorAll<HTMLDetailsElement>(".contextual-help[open]").forEach((details) => {
        if (!details.contains(target)) details.open = false;
      });
    };
    const closeHelpOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      document.querySelectorAll<HTMLDetailsElement>(".contextual-help[open]").forEach((details) => {
        details.open = false;
      });
    };

    document.addEventListener("pointerdown", closeHelpOnOutsideClick);
    document.addEventListener("keydown", closeHelpOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeHelpOnOutsideClick);
      document.removeEventListener("keydown", closeHelpOnEscape);
      destroyWorkflows();
    };
  }, []);

  return (
    <div class={`app${showLandingScreen ? " is-landing" : ""}`}>
      {!showLandingScreen ? <Topbar /> : null}
      <main>
        {activeNotice ? (
          <div class="global-notice-stack">
            <NoticeBanner
              message={activeNotice.message}
              noticeKey={activeNotice.key}
              variant={activeNotice.error ? "error" : "info"}
              durationMs={activeNotice.error || activeNotice.retryAction ? null : 6000}
              actionLabel={activeNotice.retryAction ? t(lang, "retry") : undefined}
              onAction={
                activeNotice.retryAction
                  ? () => {
                      const action = activeNotice.retryAction;
                      hideGlobalNotice();
                      void action?.();
                    }
                  : undefined
              }
              onDismiss={hideGlobalNotice}
              dismissLabel={t(lang, "dismissNotice")}
            />
            {activeNotice.error ? <TechnicalDetails error={activeNotice.error} /> : null}
          </div>
        ) : null}

        {workspaceActive ? (
          <section class="workspace" aria-label={t(lang, "workspaceAriaLabel")}>
            <StageSidebar />
            <ChatPanel />
            <MapPanel />
          </section>
        ) : (
          <LandingView />
        )}
      </main>

      <FinalizeModal />
      <TooltipOverlay />
    </div>
  );
}
