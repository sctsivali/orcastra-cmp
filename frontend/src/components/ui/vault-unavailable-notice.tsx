"use client";

import { ShieldAlert } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/**
 * The secret store is not configured, so a credential surface has nothing to show.
 *
 * Extracted from the copy that was inline in integrations-settings, which painted itself with
 * six raw amber palette steps. That is the shape the house rules ban: a semantic state written
 * as a colour rather than as a token, in one file, waiting to be copied into a second one with
 * slightly different steps. Both call sites now render this.
 *
 * note: not `variant="destructive"`. Nothing is broken and nothing was lost; a capability is
 * unavailable until an operator configures it, and painting that as an error sends people
 * looking for a fault.
 */
export function VaultUnavailableNotice({ surface }: { surface: string }) {
  return (
    <Alert>
      <ShieldAlert className="h-5 w-5" />
      <AlertTitle>Vault is not configured</AlertTitle>
      <AlertDescription className="mt-1 space-y-2 text-muted-foreground">
        <p>
          {surface} needs HashiCorp Vault, which is where credentials are kept. Nothing has been
          lost: this surface stays unavailable until Vault is reachable.
        </p>
        <p className="text-xs">
          Set <span className="font-mono">VAULT_ENABLED=true</span>,{" "}
          <span className="font-mono">VAULT_ADDR</span> and{" "}
          <span className="font-mono">VAULT_TOKEN</span> in the backend environment, then restart
          the backend.
        </p>
      </AlertDescription>
    </Alert>
  );
}
