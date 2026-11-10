"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";

/**
 * A reading that came back incomplete, said out loud.
 *
 * important: render this instead of a zero. A cluster read that lost one part answers with
 * the parts that survived, so without a marker "0 B" would mean both "no storage" and "could
 * not find out". Every surface that can serve a partial reading says so with this notice.
 */
export function PartialReadNotice({
  children,
  "data-testid": testId,
}: {
  children: React.ReactNode;
  /** A hook for browser tests that assert the notice on a degraded reading. */
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
