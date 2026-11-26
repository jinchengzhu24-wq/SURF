import type { JSX } from "preact";
import { useEffect, useRef } from "preact/hooks";
import { t } from "../../i18n";
import {
  isHistoricalStage,
  language,
  selectedStageTurns,
  selectedVersionId,
} from "../../store/sessionStore";
import { AssistantBubble } from "./AssistantBubble";

export function MessageList(): JSX.Element {
  const lang = language.value;
  const turns = selectedStageTurns.value;
  const vid = selectedVersionId.value;
  const historical = isHistoricalStage.value;

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const lastStageRef = useRef<string | null>(null);
  const lastCountRef = useRef<number>(0);
  const wasNearBottomRef = useRef<boolean>(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const stageChanged = lastStageRef.current !== null && lastStageRef.current !== vid;
    const initialRender = lastStageRef.current === null;
    const hasNewTurns = turns.length > lastCountRef.current;

    lastStageRef.current = vid;
    lastCountRef.current = turns.length;

    if (initialRender || stageChanged || (hasNewTurns && wasNearBottomRef.current)) {
      window.requestAnimationFrame(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
      });
    }
  }, [vid, turns.length]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    wasNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
  };

  const showEmpty = turns.length === 0 && historical;

  return (
    <div
      ref={scrollRef}
      class="chat-scroll"
      role="log"
      tabIndex={0}
      aria-label={t(lang, "chatAriaLabel")}
      onScroll={handleScroll}
    >
      {showEmpty ? (
        <div class="empty-chat">
          <div class="empty-mark" aria-hidden="true">
            AI
          </div>
          <h3>{t(lang, "noStageConversation")}</h3>
          <p>{t(lang, "noStageConversationBody")}</p>
        </div>
      ) : null}

      <div class="message-list" aria-live="polite">
        {turns.map((turn) => (
          <div key={turn.turnId} class={`message-row ${turn.role}`}>
            {turn.role === "assistant" ? (
              <div class="message-avatar" aria-hidden="true">
                AI
              </div>
            ) : null}
            <div class="message-content">
              <div class="message-bubble">
                {turn.role === "assistant" ? <AssistantBubble turn={turn} /> : turn.content}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
