"use client";

import { useState, type ReactNode } from "react";
import { Loader2, Unlock, RefreshCw, ShieldAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TableLink } from "@/components/ui/table-link";
import { UnsealVaultDialog } from "@/components/host-health/unseal-vault-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { useVaultStatus } from "@/hooks/use-vault-status";

// Issue #15: a sealed Vault made the dashboard look like a deployment with no clusters. This says
// what is actually wrong, on every page, and clears on its own when Vault answers again. A notice
// rather than a lock: whatever still works keeps working, and nothing is hidden from an operator.

const ADMIN_COPY: Record<NonNullable<ReturnType<typeof useVaultStatus>["state"]>, { title: string; body: ReactNode }> = {
  sealed: {
    title: "Vault is sealed",
    body: (
      <>
        Cluster credentials are locked, so cluster data cannot be read and lists may be empty or out
        of date. Unseal it here with the key shares, or run{" "}
        <span className="font-mono text-foreground">vault operator unseal</span> on the Vault host up to its key
        threshold.
      </>
    ),
  },
  uninitialized: {
    title: "Vault is not initialized",
    body: "Initialize and unseal Vault before cluster data can be read.",
  },
  unreachable: {
    title: "Vault is unreachable",
    body: "The backend cannot reach Vault, so cluster data cannot be read and lists may be empty or out of date.",
  },
  unavailable: {
    title: "Cluster registry unreadable",
    body: "Vault answered, but the cluster registry could not be read. Check that the backend token may list the clusters path.",
  },
};

const OPERATOR_COPY = {
  title: "Secret store unavailable",
  body: "Cluster data may be missing or out of date. Contact your administrator.",
};

export function VaultNotice() {
  const { state, status, checking, checkNow } = useVaultStatus();
  const { isAdmin } = usePermissions();
  const [unsealOpen, setUnsealOpen] = useState(false);
  if (!state) return null;

  const { title, body } = isAdmin ? ADMIN_COPY[state] : OPERATOR_COPY;
  const reason = isAdmin ? status?.detail?.reason ?? status?.detail?.registry_error : null;

  return (
    <div className="border-b border-warning/40 bg-warning/10">
      <div
        role="status"
        aria-live="polite"
        className="container flex flex-col gap-3 px-4 py-3 text-sm sm:flex-row sm:items-start sm:justify-between md:px-6 lg:px-8"
      >
        <div className="flex min-w-0 gap-3">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
          <div className="min-w-0 space-y-1">
            <p className="font-medium text-foreground">{title}</p>
            <p className="text-muted-foreground">
              {body}
              {status?.registry_source === "last_known" ? " Showing the last known cluster list." : ""}{" "}
              This notice clears on its own when Vault recovers.
            </p>
            {reason && <p className="font-mono text-xs text-muted-foreground break-all">{reason}</p>}
            {isAdmin && (
              <p className="text-muted-foreground">
                <TableLink href="/inventory/warnings/host-health">Host Health</TableLink> shows every service the
                dashboard depends on.
              </p>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 self-start">
          {isAdmin && state === "sealed" && (
            <Button size="sm" onClick={() => setUnsealOpen(true)}>
              <Unlock className="mr-2 h-4 w-4" aria-hidden="true" />
              Unseal Vault
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={checkNow} disabled={checking}>
            {checking ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
            Check now
          </Button>
        </div>
      </div>
      {isAdmin && <UnsealVaultDialog open={unsealOpen} onOpenChange={setUnsealOpen} />}
    </div>
  );
}
