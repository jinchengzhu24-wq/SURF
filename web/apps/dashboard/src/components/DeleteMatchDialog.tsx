import { ConfirmDialog } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useEffect, useId, useRef, useState } from "preact/hooks";
import type { DashboardStore } from "../store";

export interface DeleteMatchDialogProps {
  store: DashboardStore;
}

export function DeleteMatchDialog({
  store,
}: DeleteMatchDialogProps): JSX.Element {
  const config = store.deleteDialog.value;
  const busy = store.deleteBusy.value;
  const error = store.deleteError.value;
  const [password, setPassword] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const inputId = useId();
  const errorId = useId();

  const isOpen = config !== null;

  useEffect(() => {
    setPassword("");
  }, [isOpen, config?.kind, config?.kind === "match" ? config.matchId : ""]);

  const title =
    config?.kind === "match"
      ? `Delete ROOM ${config.roomCode}?`
      : "Clear all MatchMaking data?";

  const description =
    config?.kind === "match"
      ? "This removes the complete MatchMaking record."
      : "This removes all recorded matches. Survey records are preserved.";

  const scope =
    config?.kind === "match"
      ? `Match ${config.matchId}`
      : "Match events, challenge maps, and results";

  const confirmLabel = busy
    ? config?.kind === "match"
      ? "Deleting..."
      : "Clearing..."
    : config?.kind === "match"
      ? "Delete match"
      : "Clear MatchMaking data";

  const handleConfirm = async () => {
    const ok = await store.confirmDelete(password);
    if (!ok) {
      inputRef.current?.focus();
    }
  };

  const handleFormSubmit = (event: Event) => {
    event.preventDefault();
    void handleConfirm();
  };

  return (
    <ConfirmDialog
      open={isOpen}
      eyebrow="Protected action"
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      cancelLabel="Cancel"
      variant="danger"
      busy={busy}
      initialFocusRef={inputRef}
      onConfirm={handleConfirm}
      onCancel={() => store.closeDeleteDialog()}
    >
      <form class="delete-dialog-form" onSubmit={handleFormSubmit}>
        <div class="delete-dialog-scope">{scope}</div>
        <label class="delete-password-field" for={inputId}>
          <span>Deletion password</span>
          <input
            ref={inputRef}
            id={inputId}
            type="password"
            autocomplete="off"
            required
            disabled={busy}
            value={password}
            aria-describedby={error ? errorId : undefined}
            aria-invalid={error ? "true" : "false"}
            onInput={(event) =>
              setPassword((event.currentTarget as HTMLInputElement).value)
            }
          />
        </label>
        <p
          id={errorId}
          class="delete-dialog-error"
          role="alert"
          aria-live="assertive"
        >
          {error}
        </p>
      </form>
    </ConfirmDialog>
  );
}
