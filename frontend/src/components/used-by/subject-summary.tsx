import * as React from "react";
import type { SubjectFact } from "./types";

// The row's own facts, kept on screen in every panel rather than parked behind a tab.
//
// important: this is what turns "who is using something" into "who is using THIS thing". The
// dialog it replaces showed a bare list, so an operator reading it had to remember which row
// they had clicked and what its driver, status and node were.
export function SubjectSummary({ facts }: { facts?: readonly SubjectFact[] }) {
  if (!facts?.length) return null;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-b pb-3 sm:grid-cols-3 lg:grid-cols-4">
      {/* note: no cap. This used to render the first eight facts and drop the rest with no
          count and no indicator, so a ninth fact simply did not exist. The grid wraps. */}
      {facts.map((fact) => (
        <div key={fact.label} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{fact.label}</dt>
          {/* note: wraps rather than clipping. A fact value is a ReactNode by design, so it
              cannot go through OverflowText, and the grid cell has room to grow. */}
          <dd className="break-words text-sm font-medium">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
