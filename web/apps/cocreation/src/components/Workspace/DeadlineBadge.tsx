import type { JSX } from "preact";
import { t } from "../../i18n";
import { deadlineClock } from "../../store/deadlineClock";
import { language } from "../../store/sessionStore";

export function DeadlineBadge(): JSX.Element | null {
  if (!deadlineClock.visible.value) return null;

  const lang = language.value;
  const expired = deadlineClock.expired.value;
  const presentation = deadlineClock.presentationState.value;
  const formatted = deadlineClock.formattedRemaining.value;
  const annState = deadlineClock.announcementState.value;

  const label = expired ? t(lang, "deadlineExpired") : t(lang, "deadlineRemaining");
  const value = expired ? `· ${t(lang, "deadlineExpiredAction")}` : formatted;
  const ariaLabel = expired
    ? `${t(lang, "deadlineExpired")}. ${t(lang, "deadlineExpiredAction")}`
    : `${t(lang, "deadlineRemaining")} ${formatted}`;

  const announcement =
    annState === "warning"
      ? t(lang, "deadlineWarningAnnouncement")
      : annState === "critical"
        ? t(lang, "deadlineCriticalAnnouncement")
        : annState === "expired"
          ? t(lang, "deadlineExpiredAnnouncement")
          : "";

  return (
    <div class={`deadline-status ${presentation}`} role="timer" aria-label={ariaLabel}>
      <span class="deadline-label">{label}</span>
      <span class="deadline-value">{value}</span>
      <span class="sr-only" aria-live="polite">
        {announcement}
      </span>
    </div>
  );
}
