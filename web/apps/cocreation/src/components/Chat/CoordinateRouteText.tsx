import type { ComponentChildren, JSX } from "preact";
import { t } from "../../i18n";
import {
  draftRows,
  findCoordinateRoute,
  toggleCoordinateLink,
} from "../../store/editorStore";
import { language } from "../../store/sessionStore";
import type { CoordinateLink } from "../../types";
import { tooltipHandlers } from "../Common/Tooltip";

export interface CoordinateRouteTextProps {
  turnId: string;
  body: string;
  links?: CoordinateLink[];
}

interface MatchCandidate {
  link: CoordinateLink;
  linkIndex: number;
  start: number;
  end: number;
  text: string;
}

export function CoordinateRouteText({
  turnId,
  body,
  links,
}: CoordinateRouteTextProps): JSX.Element {
  const lang = language.value;
  const rows = draftRows.value;
  const text = String(body || "");

  const candidates: MatchCandidate[] = [];
  const occupied: Array<{ start: number; end: number }> = [];

  (Array.isArray(links) ? links : []).forEach((link, linkIndex) => {
    const linkText = String(link?.text || "").trim();
    if (!linkText) return;
    if (!findCoordinateRoute(rows, link.from, link.to)) return;

    let start = text.indexOf(linkText);
    while (
      start >= 0 &&
      occupied.some((range) => start < range.end && start + linkText.length > range.start)
    ) {
      start = text.indexOf(linkText, start + 1);
    }
    if (start < 0) return;

    const end = start + linkText.length;
    occupied.push({ start, end });
    candidates.push({ link, linkIndex, start, end, text: linkText });
  });

  if (candidates.length === 0) {
    return <div class="message-body">{text}</div>;
  }

  candidates.sort((a, b) => a.start - b.start);

  const nodes: ComponentChildren[] = [];
  let cursor = 0;
  for (const candidate of candidates) {
    if (candidate.start > cursor) {
      nodes.push(text.slice(cursor, candidate.start));
    }
    const ariaLabel = t(lang, "routeAriaLabel", {
      fromRow: candidate.link.from.row,
      fromColumn: candidate.link.from.column,
      toRow: candidate.link.to.row,
      toColumn: candidate.link.to.column,
    });
    nodes.push(
      <span
        key={`${turnId}:${candidate.linkIndex}:${candidate.start}`}
        class="coordinate-link"
        role="button"
        tabIndex={0}
        aria-label={ariaLabel}
        onClick={() => toggleCoordinateLink(turnId, candidate.linkIndex, candidate.link)}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          toggleCoordinateLink(turnId, candidate.linkIndex, candidate.link);
        }}
        {...tooltipHandlers({ kind: "route" })}
      >
        {candidate.text}
      </span>,
    );
    cursor = candidate.end;
  }
  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }

  return <div class="message-body">{nodes}</div>;
}
