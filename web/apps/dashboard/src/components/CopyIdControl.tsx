import type { JSX } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { clean, compactId, copyTextToClipboard } from "../format";

export interface CopyIdControlProps {
  label: string;
  rawValue: string | null | undefined;
  prefix?: string;
}

export function CopyIdControl({
  label,
  rawValue,
  prefix = "",
}: CopyIdControlProps): JSX.Element {
  const fullValue = clean(rawValue);
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setCopied(false);
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [fullValue]);

  if (!fullValue) {
    return <strong>-</strong>;
  }

  const handleCopy = async () => {
    const ok = await copyTextToClipboard(fullValue);
    if (!ok) return;
    setCopied(true);
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      setCopied(false);
    }, 1200);
  };

  return (
    <span class="id-control">
      <strong class="id-value" title={`Full ${label}: ${fullValue}`}>
        {prefix}
        {compactId(fullValue)}
      </strong>
      <button
        type="button"
        class="copy-id-button"
        disabled={copied}
        title={`Copy full ${label}`}
        aria-label={`Copy full ${label}`}
        onClick={() => void handleCopy()}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}
