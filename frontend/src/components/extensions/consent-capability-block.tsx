"use client";

import { Check, ShieldAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { capabilityNote } from "@/lib/extension-display";
import type { ExtensionCapabilityInfo } from "@/types/api";

/**
 * What an add-on asks to be allowed to call. Capabilities that reach inside a guest get their
 * own block above the rest, split on the server's `reaches_inside_a_guest`, so running a command
 * inside an instance is never buried among inventory reads. Each marked capability shows the
 * server's own reason.
 */
function CapabilityRow({ capability }: { capability: ExtensionCapabilityInfo }) {
  const note = capabilityNote(capability.elevated, capability.elevated_reason);
  return (
    <li className="flex items-start gap-2 py-1.5">
      <Check className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 space-y-0.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs">{capability.name}</span>
          {note ? (
            <Badge variant="caution" className="shrink-0">
              {note}
            </Badge>
          ) : null}
        </div>
        <p className="text-sm text-muted-foreground">{capability.description}</p>
      </div>
    </li>
  );
}

export function ConsentCapabilityBlock({
  capabilities,
}: {
  capabilities: ExtensionCapabilityInfo[];
}) {
  const guest = capabilities.filter((c) => c.reaches_inside_a_guest);
  const rest = capabilities.filter((c) => !c.reaches_inside_a_guest);

  return (
    <div className="space-y-4">
      {guest.length > 0 ? (
        <section className="rounded-lg border border-destructive/40 p-3">
          <div className="mb-1 flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-destructive" aria-hidden="true" />
            <h4 className="text-sm font-semibold">Reaches Inside Your Guests</h4>
          </div>
          <p className="mb-2 text-sm text-muted-foreground">
            These let the add-on act inside a running instance rather than manage it from
            outside. Your organization must already permit guest access, and desktop control is
            permitted separately from viewing.
          </p>
          <ul className="divide-y">
            {guest.map((capability) => (
              <CapabilityRow key={capability.name} capability={capability} />
            ))}
          </ul>
        </section>
      ) : null}

      {rest.length > 0 ? (
        <section>
          <h4 className="mb-1 text-sm font-semibold">What It May Call</h4>
          <ul className="divide-y">
            {rest.map((capability) => (
              <CapabilityRow key={capability.name} capability={capability} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
