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
      {facts.slice(0, 8).map((fact) => (
        <div key={fact.label} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{fact.label}</dt>
          <dd className="truncate text-sm font-medium">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
