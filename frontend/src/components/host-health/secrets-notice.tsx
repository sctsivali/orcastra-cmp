"use client";

import { ShieldAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
    <div className="border-b" data-testid="secrets-notice">
      <div className="container px-4 py-3 md:px-6 lg:px-8">
        <Alert variant="destructive" role="status" aria-live="polite">
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          <AlertTitle>Template secrets in use</AlertTitle>
          <AlertDescription className="text-muted-foreground">
            {check.summary} Anyone who has read the public template knows these values.{" "}
            <TableLink href="/inventory/warnings/host-health">Host Health</TableLink> says how to replace them.
          </AlertDescription>
        </Alert>
      </div>
    </div>
  );
}
