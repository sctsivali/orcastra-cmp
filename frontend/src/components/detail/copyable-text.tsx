"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

// A value the operator needs to take somewhere else: a full resource id, an LXD reference, an
// IP address. Shown in full rather than truncated, with the copy affordance beside it.
//
// important: the copy button is icon-only, so `label` is required and becomes its accessible
// name. check-icon-button-names.mjs enforces that, and an unnamed button in a list of twenty
// identical rows is unusable by anything but a mouse.
//
// note: it reports the result through `onCopied` instead of announcing it itself. A list can
// render fifty of these, and fifty live regions is fifty announcements competing to be read.
// The container owns one region and speaks for all of them.
interface CopyableTextProps {
  value: string;
  /** What is being copied, ex; "reference for web-01". Names the button. */
  label: string;
  onCopied?: (ok: boolean) => void;
  className?: string;
  /** Render the value itself. Defaults to the raw string. */
  children?: React.ReactNode;
}

export function CopyableText({ value, label, onCopied, className, children }: CopyableTextProps) {
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const handleCopy = async () => {
    const ok = await copyText(value);
    setCopied(ok);
    onCopied?.(ok);
  };

  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5", className)}>
      <span className="min-w-0 truncate font-mono text-sm" title={value}>
        {children ?? value}
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-6 w-6 shrink-0"
        onClick={handleCopy}
        aria-label={`Copy ${label}`}
      >
        {copied ? (
          <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
        ) : (
          <Copy className="h-3.5 w-3.5" aria-hidden="true" />
        )}
      </Button>
    </span>
  );
}
