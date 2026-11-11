"use client";

import { ShieldAlert } from "lucide-react";
import { TableLink } from "@/components/ui/table-link";
import { useHostHealth } from "@/hooks/use-host-health";

/**
 * Tells an Administrator, on every page, that the deployment still runs with a template placeholder
 * or too short a secret (Host Health's configuration check, critical). It cannot be dismissed: the
 * fix is a new value in .env. Other roles never read Host Health, so they see nothing.
 */
export function SecretsNotice() {
  const { data } = useHostHealth({ poll: "badge" });
  const check = data?.checks.find((item) => item.id === "configuration");
  if (!check || check.severity !== "critical") return null;
  return (
    <div className="border-b border-destructive/40 bg-destructive/10" data-testid="secrets-notice">
      <div
        role="status"
        aria-live="polite"
        className="container flex gap-3 px-4 py-3 text-sm md:px-6 lg:px-8"
      >
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium text-foreground">Template secrets in use</p>
          <p className="text-muted-foreground">
            {check.summary} Anyone who has read the public template knows these values.{" "}
            <TableLink href="/inventory/warnings/host-health">Host Health</TableLink> says how to replace them.
          </p>
        </div>
      </div>
    </div>
  );
}
