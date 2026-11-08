"use client";

import { AlertTriangle } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { hypervisorCli } from "@/lib/utils";

export interface LeftoverTrust {
  clusterId: string;
  commands: string[];
  // The removed cluster's hypervisor type, so the fallback hint names the CLI it has.
  clusterType?: string | null;
}

/**
 * After a deregistration the dashboard could not finish revoking its own access: the cluster was
 * unreachable, or refused a removal. The registration is gone either way, so these commands are
 * the only remaining record of what still trusts this dashboard on that host.
 */
export function LeftoverTrustDialog({
  leftover,
  onClose,
}: {
  leftover: LeftoverTrust | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={!!leftover} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Trust Entries Left on the Cluster</DialogTitle>
          <DialogDescription>
            {leftover?.clusterId} was removed from this dashboard, but the cluster may still trust
            its certificates.
          </DialogDescription>
        </DialogHeader>
        <Alert>
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>
            Run these on the hypervisor to revoke the dashboard&apos;s access. Nothing else on the
            cluster is affected.
          </AlertDescription>
        </Alert>
        {leftover && leftover.commands.length > 0 && (
          <CodeBlock value={leftover.commands.join("\n")} label="Commands" copyLabel="commands" />
        )}
        {leftover && leftover.commands.length === 0 && (
          // The dashboard could not even read which certificate it was using, so it cannot name
          // the entry. The trust list can: its entries are named after the cluster.
          <p className="text-sm text-muted-foreground">
            The dashboard could not confirm which of its certificates the cluster still trusts. On the
            hypervisor, run <span className="font-mono">{hypervisorCli(leftover.clusterType)} config trust list</span> and look for entries
            named <span className="font-mono">orca-{leftover.clusterId}-</span> followed by a date. Another
            dashboard that manages this host under the same name creates entries of the same shape, so
            confirm an entry is this dashboard&apos;s before removing it.
          </p>
        )}
        <DialogFooter>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
