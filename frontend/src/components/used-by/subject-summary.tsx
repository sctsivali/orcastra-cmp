import * as React from "react";
import type { SubjectFact } from "./types";

// The row's own facts, kept on screen in every panel rather than behind a tab, so the operator
// can see which row the usage belongs to and its driver, status and node.
export function SubjectSummary({ facts }: { facts?: readonly SubjectFact[] }) {
  if (!facts?.length) return null;
  return (
    // The rule separates this pinned block from the body that scrolls under it. The spacing is
    // asymmetric: pb-3 binds the rule to the facts it closes, mb-4 matches the dialog's 16px
    // rhythm. mb-4 lives here so it also holds when a bare UsagePanel follows instead of a tab
    // strip.
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-b pb-3 mb-4 sm:grid-cols-3 lg:grid-cols-4">
      {/* No cap: every fact renders and the grid wraps. */}
      {facts.map((fact) => (
        <div key={fact.label} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{fact.label}</dt>
          {/* Wraps rather than clipping. A fact value is a ReactNode, so it cannot go through
              OverflowText, and the grid cell has room to grow. */}
          <dd className="break-words text-sm font-medium">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
