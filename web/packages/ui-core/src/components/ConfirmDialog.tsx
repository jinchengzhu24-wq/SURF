import type { ComponentChildren, JSX, RefObject } from "preact";
import { useEffect, useId, useRef } from "preact/hooks";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: string;
  eyebrow?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "default" | "danger";
  busy?: boolean;
  closeOnBackdropClick?: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
  children?: ComponentChildren;
}

/**
 * Accessible native `<dialog>` wrapper supporting Escape cancellation,
 * backdrop click dismissal, initial focus placement, and focus restoration on close.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  eyebrow,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "default",
  busy = false,
  closeOnBackdropClick = true,
  initialFocusRef,
  onConfirm,
  onCancel,
  children,
}: ConfirmDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const cancelButtonRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open) {
      if (!dialog.open) {
        const active = document.activeElement;
        previousFocusRef.current = active instanceof HTMLElement ? active : null;
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      queueMicrotask(() => {
        if (!dialogRef.current?.open) return;
        const target = initialFocusRef?.current ?? cancelButtonRef.current;
        target?.focus();
      });
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
      const previous = previousFocusRef.current;
      previousFocusRef.current = null;
      if (previous && previous.isConnected) {
        previous.focus();
      }
    }
  }, [open, initialFocusRef]);

  useEffect(() => {
    return () => {
      const dialog = dialogRef.current;
      if (dialog && (dialog.open || dialog.hasAttribute("open"))) {
        if (typeof dialog.close === "function") {
          dialog.close();
        } else {
          dialog.removeAttribute("open");
        }
      }
      const previous = previousFocusRef.current;
      previousFocusRef.current = null;
      if (previous && previous.isConnected) {
        previous.focus();
      }
    };
  }, []);

  const handleCancel = (event: Event) => {
    event.preventDefault();
    if (!busy) {
      onCancel();
    }
  };

  const handleBackdropClick = (event: MouseEvent) => {
    if (!closeOnBackdropClick || busy) return;
    if (event.target === dialogRef.current) {
      onCancel();
    }
  };

  return (
    <dialog
      ref={dialogRef}
      class="sokoban-dialog"
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={handleCancel}
      onClick={handleBackdropClick}
    >
      <div class="sokoban-dialog__surface" aria-busy={busy ? "true" : "false"}>
        <div class="sokoban-dialog__header">
          {eyebrow ? <p class="sokoban-dialog__eyebrow">{eyebrow}</p> : null}
          <h2 id={titleId} class="sokoban-dialog__title">
            {title}
          </h2>
        </div>
        <div class="sokoban-dialog__body">
          {description ? (
            <p id={descriptionId} class="sokoban-dialog__description">
              {description}
            </p>
          ) : null}
          {children}
        </div>
        <div class="sokoban-dialog__actions">
          <button
            ref={cancelButtonRef}
            type="button"
            class="pixel-btn pixel-btn--secondary"
            disabled={busy}
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            class={`pixel-btn ${variant === "danger" ? "pixel-btn--danger" : ""}`}
            disabled={busy}
            onClick={() => void onConfirm()}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
