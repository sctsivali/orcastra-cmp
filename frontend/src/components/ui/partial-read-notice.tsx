"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";

/**
 * A reading that came back incomplete, said out loud.
 *
 * important: this is the alternative to rendering a zero. A cluster read that lost one part
 * used to answer with the parts that survived and no marker, so "0 B" meant both "no storage"
 * and "we could not find out". Every surface that can serve a partial reading says so with
 * this, so the two never look the same again.
 */
export function PartialReadNotice({
  children,
  "data-testid": testId,
}: {
  children: React.ReactNode;
  /** A hook for the browser harness, which asserts the notice on a degraded reading. */
  "data-testid"?: string;
}) {
  return (
    <div
      className="flex items-start gap-3 rounded-md border border-status-pending/40 bg-muted/50 p-3"
      data-testid={testId}
      role="status"
    >
      <AlertTriangle
        className="mt-0.5 h-4 w-4 shrink-0 text-status-pending"
        aria-hidden="true"
      />
      <p className="text-sm text-muted-foreground">{children}</p>
    </div>
  );
}
