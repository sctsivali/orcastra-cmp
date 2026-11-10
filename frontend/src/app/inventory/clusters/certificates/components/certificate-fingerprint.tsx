"use client";

import type * as React from "react";

import { CopyableText } from "@/components/detail/copyable-text";
import { OverflowText } from "@/components/ui/overflow-text";
import { TableEmptyValue } from "@/components/ui/table-text";

// What `lxc config trust list` prints in its FINGERPRINT column: the first 12 characters.
export const TRUST_LIST_CHARS = 12;

/**
 * A certificate fingerprint in a table cell: the trust list's 12 characters at rest, the whole
 * value and a copy button behind the reveal.
 *
 * An overlay instead of inline expansion: 64 monospace characters expanded in place would
 * wrap a narrow column into five lines and push every row below it down.
 */
export function FingerprintCell({ value, label }: { value?: string | null; label: string }) {
  if (!value) return <TableEmptyValue label={`No ${label}`} />;
  return (
    <OverflowText
      value={value}
      label={label}
      mode="overlay"
      mono
      reveal="always"
      visibleChars={TRUST_LIST_CHARS}
    />
  );
}

/** A fingerprint in a dialog: labelled, whole, wrapping, with a copy button. */
export function FingerprintField({
  label,
  value,
  copyLabel,
  hint,
}: {
  label: string;
  value?: string | null;
  /** Names the copy button, ex; "new certificate fingerprint for edge-01". */
  copyLabel: string;
  hint?: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground">{label}</div>
      {value ? (
        <CopyableText value={value} label={copyLabel} />
      ) : (
        <TableEmptyValue label={`No ${label.toLowerCase()}`} />
      )}
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
