import { NoticeBanner } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useMemo } from "preact/hooks";
import { createDashboardApi, type DashboardApi } from "./api";
import { DeleteMatchDialog } from "./components/DeleteMatchDialog";
import { MatchDetail } from "./components/MatchDetail";
import { MatchList } from "./components/MatchList";
import { SummaryCards } from "./components/SummaryCards";
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

        <SummaryCards store={store} />

        <section class="workspace" aria-label="Match explorer">
          <MatchList store={store} />
          <MatchDetail store={store} />
        </section>
      </main>

      <DeleteMatchDialog store={store} />
    </div>
  );
}
