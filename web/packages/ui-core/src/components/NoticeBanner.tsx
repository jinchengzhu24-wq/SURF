import type { JSX } from "preact";
import { useEffect } from "preact/hooks";

export type NoticeVariant = "info" | "success" | "warning" | "error";

export interface NoticeBannerProps {
  message: string | null | undefined;
  noticeKey?: string | number;
  variant?: NoticeVariant;
  durationMs?: number | null;
  actionLabel?: string;
  onAction?: () => void;
  onDismiss?: () => void;
  dismissLabel?: string;
  className?: string;
}

/**
 * Dismissible notice banner with automatic `clearTimeout` cleanup so rapid
 * consecutive notices never cancel each other's display window prematurely.
 */
export function NoticeBanner({
  message,
  noticeKey,
  variant = "info",
  durationMs = 4500,
  actionLabel,
  onAction,
  onDismiss,
  dismissLabel = "Dismiss notice",
  className,
}: NoticeBannerProps): JSX.Element | null {
  useEffect(() => {
    if (!message || !durationMs || durationMs <= 0 || !onDismiss) {
      return undefined;
    }
    const timerId = window.setTimeout(() => {
      onDismiss();
    }, durationMs);
    return () => {
      window.clearTimeout(timerId);
    };
  }, [message, noticeKey, durationMs, onDismiss]);

  if (!message) {
    return null;
  }

  const isError = variant === "error";
  const bannerClass = [
    "sokoban-notice",
    `sokoban-notice--${variant}`,
    className || "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      class={bannerClass}
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
    >
      <span class="sokoban-notice__message">{message}</span>
      <div class="sokoban-notice__actions">
        {actionLabel && onAction ? (
          <button
            type="button"
            class="pixel-btn pixel-btn--secondary"
            onClick={onAction}
          >
            {actionLabel}
          </button>
        ) : null}
        {onDismiss ? (
          <button
            type="button"
            class="sokoban-notice__dismiss"
            aria-label={dismissLabel}
            onClick={onDismiss}
          >
            ×
          </button>
        ) : null}
      </div>
    </div>
  );
}
