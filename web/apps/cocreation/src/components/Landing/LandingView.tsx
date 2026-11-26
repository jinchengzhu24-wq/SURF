import type { JSX } from "preact";
import { t, type TranslationKey } from "../../i18n";
import {
  busy,
  demoCreationFailed,
  demoCreationPending,
  draftRegenerationPending,
  formalStandaloneBlocked,
  landingMode,
  language,
  session,
  sessionId,
} from "../../store/sessionStore";
import { confirmSessionLanguage, regenerateDraft } from "../../store/workflows";
import { tooltipHandlers } from "../Common/Tooltip";
import { DraftPreview } from "./DraftPreview";
import { LanguagePicker } from "./LanguagePicker";

export function LandingView(): JSX.Element {
  const lang = language.value;

  if (formalStandaloneBlocked.value) {
    return (
      <section class="landing panel" id="formalStandaloneBlock">
        <div class="landing-mark">S</div>
        <p class="eyebrow">{t(lang, "neutralBrief")}</p>
        <h2>{t(lang, "embeddedOnlyTitle")}</h2>
        <p>{t(lang, "embeddedOnlyBody")}</p>
      </section>
    );
  }

  const s = session.value;
  const isFormal = landingMode.value === "formal";

  const titleKey: TranslationKey = demoCreationPending.value
    ? "draftLoadingTitle"
    : demoCreationFailed.value
      ? "draftInitialFailed"
      : isFormal
        ? "formalLandingTitle"
        : "demoLandingTitle";

  const bodyKey: TranslationKey = demoCreationPending.value
    ? "draftLoadingBody"
    : isFormal
      ? "formalLandingBody"
      : "demoLandingBody";

  const regenStatus = s?.draftPreview?.regenerationStatus;
  const regenerating =
    draftRegenerationPending.value || regenStatus === "pending" || regenStatus === "claimed";

  const regenerateDisabled = !s
    ? busy.value || demoCreationPending.value || Boolean(sessionId.value)
    : busy.value || regenerating || !s.draftPreview?.mutable;

  const enterDisabled = !s || busy.value || regenerating;

  return (
    <section class="landing panel" id="landing">
      <div class="landing-mark">S</div>
      <p class="eyebrow">{t(lang, "sessionSetup")}</p>
      <h2>{t(lang, titleKey)}</h2>
      <p class="landing-body">{t(lang, bodyKey)}</p>
      <LanguagePicker />
      <DraftPreview />
      <div class="draft-actions">
        <button
          class="secondary-button"
          type="button"
          disabled={regenerateDisabled}
          onClick={() => void regenerateDraft()}
          {...tooltipHandlers({
            kind: "action",
            helpKey: "regenerateDraft",
            titleKey: "regenerateDraft",
          })}
        >
          {t(lang, "regenerateDraft")}
        </button>
        <button
          class="primary-button"
          type="button"
          disabled={enterDisabled}
          onClick={() => void confirmSessionLanguage()}
          {...tooltipHandlers({
            kind: "action",
            helpKey: "enterSession",
            titleKey: "enterSession",
          })}
        >
          {t(lang, "enterSession")}
        </button>
      </div>
    </section>
  );
}
