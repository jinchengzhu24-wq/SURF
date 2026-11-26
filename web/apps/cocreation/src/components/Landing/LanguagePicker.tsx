import type { JSX } from "preact";
import { t } from "../../i18n";
import {
  busy,
  demoCreationPending,
  landingMode,
  language,
  session,
} from "../../store/sessionStore";
import { selectSetupLanguage } from "../../store/workflows";
import { tooltipHandlers } from "../Common/Tooltip";

export function LanguagePicker(): JSX.Element {
  const lang = language.value;
  const disabled = busy.value || demoCreationPending.value || Boolean(session.value?.languageLocked);
  const lockKey = landingMode.value === "formal" ? "formalLanguageLock" : "demoLanguageLock";
  const tip = tooltipHandlers({
    kind: "action",
    helpKey: "setupLanguage",
    titleKey: "sessionLanguage",
  });

  return (
    <div class="language-setup" aria-labelledby="languageSetupLabel">
      <p class="language-setup-label" id="languageSetupLabel">
        {t(lang, "sessionLanguage")}
      </p>
      <div class="language-choice" role="group" aria-labelledby="languageSetupLabel">
        <button
          class="setup-language-button"
          type="button"
          lang="zh-CN"
          aria-pressed={lang === "zh-CN" ? "true" : "false"}
          disabled={disabled}
          onClick={() => selectSetupLanguage("zh-CN")}
          {...tip}
        >
          中文
        </button>
        <button
          class="setup-language-button"
          type="button"
          lang="en"
          aria-pressed={lang === "en" ? "true" : "false"}
          disabled={disabled}
          onClick={() => selectSetupLanguage("en")}
          {...tip}
        >
          English
        </button>
      </div>
      <p class="language-lock-notice">{t(lang, lockKey)}</p>
    </div>
  );
}
