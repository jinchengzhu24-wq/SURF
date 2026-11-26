import type { JSX } from "preact";
import { useEffect, useRef } from "preact/hooks";
import { t } from "../../i18n";
import {
  busy,
  intentionDraft,
  language,
  session,
} from "../../store/sessionStore";
import { returnToUnity, submitIntention } from "../../store/workflows";
import { tooltipHandlers } from "../Common/Tooltip";

export function IntentionForm(): JSX.Element | null {
  const s = session.value;
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const lang = language.value;

  useEffect(() => {
    if (s?.status === "awaiting_intention") {
      inputRef.current?.focus();
    }
  }, [s?.status]);

  if (!s) return null;

  if (s.status === "awaiting_intention") {
    const unsyncedIntention = s.intention?.content || "";
    const readOnly = Boolean(unsyncedIntention);

    return (
      <form
        class="intention-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submitIntention();
        }}
      >
        <p class="eyebrow">{t(lang, "intentionTitle")}</p>
        <h3>{t(lang, "intentionQuestion")}</h3>
        <textarea
          ref={inputRef}
          id="intentionInput"
          rows={4}
          maxLength={4000}
          required
          readOnly={readOnly}
          value={intentionDraft.value}
          onInput={(event) => {
            if (!readOnly) {
              intentionDraft.value = (event.currentTarget as HTMLTextAreaElement).value;
            }
          }}
        />
        <button
          class="primary-button"
          type="submit"
          disabled={busy.value}
          {...tooltipHandlers({
            kind: "action",
            helpKey: "completeSession",
            titleKey: "completeSession",
          })}
        >
          {t(lang, "completeSession")}
        </button>
      </form>
    );
  }

  if (s.status === "completed") {
    return (
      <div class="complete-card">
        <strong>{t(lang, "sessionComplete")}</strong>
        <p>{t(lang, s.demoMode ? "demoCompleteBody" : "sessionCompleteBody")}</p>
        {!s.demoMode ? (
          <button
            class="primary-button"
            type="button"
            onClick={returnToUnity}
            {...tooltipHandlers({
              kind: "action",
              helpKey: "returnUnity",
              titleKey: "returnUnity",
            })}
          >
            {t(lang, "returnUnity")}
          </button>
        ) : null}
      </div>
    );
  }

  return null;
}
