import type { JSX } from "preact";
import { formatTechnicalError, t } from "../../i18n";
import { language } from "../../store/sessionStore";
import type { StructuredErrorLike } from "../../types";

export interface TechnicalDetailsProps {
  error?: StructuredErrorLike | null;
  plainText?: string;
}

export function TechnicalDetails({ error, plainText }: TechnicalDetailsProps): JSX.Element | null {
  const lang = language.value;

  if (plainText) {
    return (
      <details class="technical-details">
        <summary>{t(lang, "technicalDetails")}</summary>
        <p>{plainText}</p>
      </details>
    );
  }

  const formatted = formatTechnicalError(lang, error);
  if (!formatted) return null;

  return (
    <details class="technical-details">
      <summary>{t(lang, "technicalDetails")}</summary>
      <p>{formatted.code ? `${formatted.code} ${formatted.message}` : formatted.message}</p>
    </details>
  );
}
