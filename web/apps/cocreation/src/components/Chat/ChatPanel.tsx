import type { JSX } from "preact";
import { t, type TranslationKey } from "../../i18n";
import { isHistoricalStage, language, session } from "../../store/sessionStore";
import { selectCurrentVersion } from "../../store/workflows";
import { tooltipHandlers } from "../Common/Tooltip";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { ProgressDrawer } from "./ProgressDrawer";
import { ProposalCard } from "./ProposalCard";

export function ChatPanel(): JSX.Element | null {
  const s = session.value;
  if (!s) return null;
  const lang = language.value;
  const methodKey: TranslationKey =
    s.initialDraftMethod === "partial_completion" ||
    s.initialDraftMethod === "description_generation" ||
    s.initialDraftMethod === "algorithm_demo"
      ? s.initialDraftMethod
      : "algorithm_demo";

  return (
    <section class="panel chat-panel" aria-labelledby="chatTitle">
      <header class="panel-header">
        <div>
          <p class="eyebrow">{t(lang, "conversation")}</p>
          <h2 id="chatTitle">{t(lang, "discuss")}</h2>
        </div>
        <span class="method-pill">{t(lang, methodKey)}</span>
      </header>

      {isHistoricalStage.value ? (
        <div class="history-banner">
          <span>{t(lang, "viewingHistory")}</span>
          <button
            type="button"
            onClick={selectCurrentVersion}
            {...tooltipHandlers({
              kind: "action",
              helpKey: "returnCurrent",
              titleKey: "returnCurrent",
            })}
          >
            {t(lang, "returnCurrent")}
          </button>
        </div>
      ) : null}

      <ProgressDrawer />
      <MessageList />
      <ProposalCard />
      <Composer />
    </section>
  );
}
