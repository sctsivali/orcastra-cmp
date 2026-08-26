"use client";

import * as React from "react";
import { OverflowText } from "@/components/ui/overflow-text";
import { cn } from "@/lib/utils";

// A value the operator needs to take somewhere else: a full resource id, an LXD reference, an
// IP address. Shown in full, with the copy affordance beside it.
//
// It is a preset of components/ui/overflow-text.tsx rather than its own implementation, and the
// direction of that composition is the point. This component used to render its value in a
// `truncate` span with the whole string on a `title` attribute, which meant that on a narrow
// viewport the reveal at the centre of every detail dialog was itself clipped, and reachable
// only by hovering a mouse over it. RecordDetailDialog renders its required `recordId` through
// here, DetailField renders `copyable` through here, and DetailListDialog renders every row
// through here, so that one clip was the last line of defence failing on every surface at once:
// the dialog opened to show what a table had hidden was hiding it again.
//
// note: `lines="none"` is what fixes it. A detail surface has the room, so the value wraps and
// is readable with no interaction at all, rather than asking for a second click to see
// something that would have fitted.
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
  return (
    <OverflowText
      value={value}
      label={label}
      lines="none"
      mono
      copy="always"
      onCopied={onCopied}
      className={cn("text-sm", className)}
    >
      {children}
    </OverflowText>
  );
}
