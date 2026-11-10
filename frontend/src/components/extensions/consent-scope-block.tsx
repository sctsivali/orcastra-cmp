"use client";

import { EMPTY_VALUE } from "@/components/ui/table-text";

/**
 * Which clusters the add-on will reach, and which it asked for and will not get.
 *
 * important: the granted set is the server's answer, passed in and never recomputed here; an
 * intersection worked out in the browser can disagree with the server at the moment of approval.
 * Refused clusters are listed so a shorter list does not read as a mistake.
 *
 * A plain list rather than a Table: a fixed summary of a few rows, never filtered or sorted.
 */
export function ConsentScopeBlock({
  requested,
  granted,
  refused,
}: {
  requested: string;
  granted: string[];
  refused: string[];
}) {
  const nothing = granted.length === 0 && refused.length === 0;

  return (
    <section className="space-y-2">
      <h4 className="text-sm font-semibold">Where It May Reach</h4>
      <p className="text-sm text-muted-foreground">
        This add-on asks for {requested} access. It never receives more than you hold yourself,
        and the reach is recomputed on every call rather than fixed at install.
      </p>

      {nothing ? (
        <p className="text-sm text-muted-foreground">
          This organization has no clusters bound to it yet, so there is nothing to grant.
        </p>
      ) : (
        <dl className="divide-y rounded-lg border">
          {granted.map((id) => (
            <div key={id} className="flex items-baseline justify-between gap-4 px-3 py-2">
              <dt className="font-mono text-sm">{id}</dt>
              <dd className="text-sm text-muted-foreground">Granted</dd>
            </div>
          ))}
          {refused.map((id) => (
            <div key={id} className="flex items-baseline justify-between gap-4 px-3 py-2">
              <dt className="font-mono text-sm text-muted-foreground">{id}</dt>
              <dd className="text-right text-sm text-muted-foreground">
                {EMPTY_VALUE}
                <span className="block text-xs">outside your own access</span>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
