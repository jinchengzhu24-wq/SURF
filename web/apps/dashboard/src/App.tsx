import { NoticeBanner } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useMemo } from "preact/hooks";
import { createDashboardApi, type DashboardApi } from "./api";
import { DeleteMatchDialog } from "./components/DeleteMatchDialog";
import { MatchDetail } from "./components/MatchDetail";
import { MatchList } from "./components/MatchList";
import { SummaryCards } from "./components/SummaryCards";
import { SurveyAnalyticsView } from "./components/surveys/SurveyAnalyticsView";
import { TopBar } from "./components/TopBar";
import { createDashboardStore, type DashboardStore } from "./store";

export interface AppProps {
  store?: DashboardStore;
  api?: DashboardApi;
  autoLoad?: boolean;
}

export function App({
  store: injectedStore,
  api: injectedApi,
  autoLoad = true,
}: AppProps): JSX.Element {
  const store = useMemo(
    () => injectedStore ?? createDashboardStore(injectedApi ?? createDashboardApi()),
    [injectedStore, injectedApi],
  );

  useEffect(() => {
    if (autoLoad) {
      void store.loadData(false);
    }
    return () => {
      if (!injectedStore) {
        store.dispose();
      }
    };
  }, [store, autoLoad, injectedStore]);

  const activeNotice = store.notice.value;
  const activeTab = store.activeTab.value;
  const surveyBadgeCount = store.surveyAnalytics.value?.totalResponses ?? null;

  return (
    <div class="app">
      <TopBar store={store} />

      <main class="dashboard-main">
        {activeNotice ? (
          <NoticeBanner
            message={activeNotice.message}
            noticeKey={activeNotice.key}
            variant={activeNotice.variant}
            onDismiss={() => store.dismissNotice()}
            className="dashboard-notice"
          />
        ) : null}

        <div class="status-line" role="status" aria-live="polite">
          {store.statusLine.value}
        </div>

        <nav class="dashboard-tabs" aria-label="Dashboard views">
          <div class="dashboard-tablist" role="tablist" aria-label="Dashboard sections">
            <button
              type="button"
              role="tab"
              id="dashboard-tab-matches"
              aria-selected={activeTab === "matches" ? "true" : "false"}
              aria-controls="dashboard-panel-matches"
              class={`dashboard-tab${activeTab === "matches" ? " selected" : ""}`}
              onClick={() => store.setActiveTab("matches")}
            >
              Matches &amp; Sessions
            </button>

            <button
              type="button"
              role="tab"
              id="dashboard-tab-surveys"
              aria-selected={activeTab === "surveys" ? "true" : "false"}
              aria-controls="dashboard-panel-surveys"
              class={`dashboard-tab${activeTab === "surveys" ? " selected" : ""}`}
              onClick={() => store.setActiveTab("surveys")}
            >
              <span>Survey Analytics</span>
              {surveyBadgeCount !== null ? (
                <span class="tab-badge">{surveyBadgeCount}</span>
              ) : null}
            </button>
          </div>
        </nav>

        {activeTab === "matches" ? (
          <div
            role="tabpanel"
            id="dashboard-panel-matches"
            aria-labelledby="dashboard-tab-matches"
          >
            <SummaryCards store={store} />

            <section class="workspace" aria-label="Match explorer">
              <MatchList store={store} />
              <MatchDetail store={store} />
            </section>
          </div>
        ) : (
          <div
            role="tabpanel"
            id="dashboard-panel-surveys"
            aria-labelledby="dashboard-tab-surveys"
          >
            <SurveyAnalyticsView store={store} />
          </div>
        )}
      </main>

      <DeleteMatchDialog store={store} />
    </div>
  );
}
