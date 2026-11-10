"use client";

import * as React from "react";
import { OverflowText } from "@/components/ui/overflow-text";
import { cn } from "@/lib/utils";

// A value the operator needs to take somewhere else: a full resource id, an LXD reference, an
// IP address. Shown in full, with the copy affordance beside it.
//
// A preset of components/ui/overflow-text.tsx. RecordDetailDialog's `recordId`, DetailField's
// `copyable` and every DetailListDialog row render here, so this is the reveal for what a table
// cut and must never clip again: `lines="none"` lets the value wrap, since a detail surface has
// the room.
//
// The result goes through `onCopied` rather than a live region of its own: a list can render
// fifty of these, and the container owns one region that speaks for all of them.
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
