import { ConfirmDialog } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { t } from "../../i18n";
import { busy, finalizeModalOpen, language } from "../../store/sessionStore";
import { finalizeSession } from "../../store/workflows";

export function FinalizeModal(): JSX.Element {
  const lang = language.value;
  return (
    <ConfirmDialog
      open={finalizeModalOpen.value}
      eyebrow={t(lang, "finalConfirmation")}
      title={t(lang, "confirmQuestion")}
      description={t(lang, "confirmBody")}
      cancelLabel={t(lang, "cancel")}
      confirmLabel={t(lang, "confirmFinal")}
      busy={busy.value}
      onCancel={() => {
        finalizeModalOpen.value = false;
      }}
      onConfirm={finalizeSession}
    />
  );
}
