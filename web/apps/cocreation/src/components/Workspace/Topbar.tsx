import type { JSX } from "preact";
import { t } from "../../i18n";
import { headerStatus, language } from "../../store/sessionStore";
import { DeadlineBadge } from "./DeadlineBadge";

export function Topbar(): JSX.Element {
  const lang = language.value;
  const status = headerStatus.value;

  return (
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">
          S
        </div>
        <div>
          <h1>{t(lang, "title")}</h1>
          <p class="subtitle">{t(lang, "subtitle")}</p>
        </div>
      </div>
      <div class="topbar-actions">
        <DeadlineBadge />
        <div class={`prototype-status ${status.state}`}>
          <span class="status-dot" aria-hidden="true" />
          <span>{t(lang, status.labelKey)}</span>
        </div>
      </div>
    </header>
  );
}
