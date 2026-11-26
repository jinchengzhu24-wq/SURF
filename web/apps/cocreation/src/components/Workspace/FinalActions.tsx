import type { JSX } from "preact";
import { t } from "../../i18n";
import { isGenerationPending } from "../../store/chatStore";
import { isDirty } from "../../store/editorStore";
import {
  busy,
  currentPendingProposal,
  isDeadlineExpired,
  isHistoricalStage,
  language,
  selectedVersion,
  session,
  unityPlayActive,
} from "../../store/sessionStore";
import { requestFinalize } from "../../store/workflows";
import { tooltipHandlers } from "../Common/Tooltip";

export function FinalActions(): JSX.Element | null {
  const s = session.value;
  if (!s || s.status !== "active") return null;

  const lang = language.value;
  const expired = isDeadlineExpired.value;
  const interactionBusy = busy.value || unityPlayActive.value;
  const generationPending = isGenerationPending.value;
  const pendingProposal = Boolean(currentPendingProposal.value);
  const historical = isHistoricalStage.value;
  const dirty = isDirty.value;
  const hasVersion = Boolean(selectedVersion.value);

  const disabled =
    interactionBusy ||
    (!expired && generationPending) ||
    !hasVersion ||
    (!historical && !expired && (dirty || pendingProposal));

  let hintText = "";
  if (disabled) {
    if (unityPlayActive.value) hintText = t(lang, "hint.playActive");
    else if (busy.value) hintText = t(lang, "hint.busy");
    else if (!generationPending) {
      if (!hasVersion) hintText = t(lang, "hint.noVersion");
      else if (!historical && dirty) hintText = t(lang, "hint.finalDirty");
      else if (!historical && pendingProposal) hintText = t(lang, "hint.pending");
    }
  }

  return (
    <div class="final-actions">
      <h3>
        <svg class="map-section-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M5 21V4c5-4 9 4 14 0v11c-5 4-9-4-14 0" />
        </svg>
        <span>{t(lang, "finishTitle")}</span>
      </h3>
      <p>{t(lang, "finalizeHint")}</p>
      <button
        class="finalize-button"
        type="button"
        aria-describedby="finalizeControlHint"
        disabled={disabled}
        aria-disabled={disabled ? "true" : undefined}
        onClick={requestFinalize}
        {...tooltipHandlers({
          kind: "action",
          helpKey: "finalize",
          titleKey: "finalizeStage",
          status: disabled ? hintText || undefined : undefined,
        })}
      >
        {t(lang, "finalizeStage")}
      </button>
      {hintText ? (
        <p class="control-hint" id="finalizeControlHint" role="status">
          {hintText}
        </p>
      ) : null}
    </div>
  );
}
