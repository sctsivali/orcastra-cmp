"use client";

import * as React from "react";
import { ShieldAlert } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { cn, plural } from "@/lib/utils";
import type { CapabilityOption } from "@/types";

interface ApiKeyCapabilitySelectorProps {
  capabilities: CapabilityOption[];
  selected: string[];
  onToggle: (capability: string) => void;
  disabled?: boolean;
}

// Which endpoint families a key may use, as opposed to which clusters it may reach. The two
// compose: a key needs lxd.proxy to use the proxy at all, and still needs the right per-cluster
// level once it is there.
//
// Selection is tracked by name and mirrors the cluster picker's idiom next to it, including the
// label-wraps-input pattern, so the whole row is a hit target for a pointer and the checkbox is
// reachable by keyboard.
export function ApiKeyCapabilitySelector({
  capabilities,
  selected,
  onToggle,
  disabled = false,
}: ApiKeyCapabilitySelectorProps) {
  const safeList = Array.isArray(capabilities) ? capabilities : [];

  // The server already marks the capabilities that reach inside a running guest, so this reads
  // that flag rather than keeping a second copy of the list. Ticking one here is only half of
  // what they need: without it, the first call meets a 403 nothing on this form warned about.
  const needsOrgConsent = safeList.some(
    (capability) => capability?.never_inherited && selected.includes(capability.name),
  );

  return (
    <div className="space-y-2">
      <Label>Capabilities</Label>
      <div className="space-y-2 rounded-md border p-3">
        {safeList.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">
            No capabilities available.
          </p>
        ) : (
          safeList.map((capability) => {
            if (!capability || typeof capability.name !== "string") return null;
            const name = capability.name;
            const isSelected = selected.includes(name);

            return (
              <label
                key={name}
                htmlFor={`capability-${name}`}
                className={cn(
                  "flex items-start gap-3 rounded-md p-2 transition-colors",
                  disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
                  isSelected
                    ? "border border-primary bg-primary/10"
                    : "bg-muted/50 hover:bg-muted"
                )}
              >
                <input
                  type="checkbox"
                  id={`capability-${name}`}
                  checked={isSelected}
                  onChange={() => onToggle(name)}
                  disabled={disabled}
                  className="mt-0.5 h-4 w-4 rounded border-primary text-primary focus:ring-primary"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-medium">{name}</span>
                    {capability.elevated && (
                      <Badge variant="caution" className="gap-1 px-2">
                        <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                        {capability.elevated_reason ?? "Needs a deliberate decision"}
                      </Badge>
                    )}
                    {capability.never_inherited && (
                      <Badge variant="muted" className="px-2">
                        Grant explicitly
                      </Badge>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {capability.description}
                  </p>
                </div>
              </label>
            );
          })
        )}
      </div>
      {needsOrgConsent && (
        <Alert>
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            Granting this on the key is not enough on its own. Reaching inside a running guest also
            needs the organization&apos;s own consent, set on the Desktop Control card in this tab,
            and a cluster shared with another organization needs every one of them to agree.
          </AlertDescription>
        </Alert>
      )}
      <p className="text-xs text-muted-foreground">
        Selected: {selected.length} of {plural(safeList.length, "capability", "capabilities")}
      </p>
    </div>
  );
}
