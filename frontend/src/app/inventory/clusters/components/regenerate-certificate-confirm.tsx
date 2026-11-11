"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";

import { CertificateLifetimeSelect } from "@/components/certificate-lifetime-select";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCertificate } from "@/hooks/use-lxd";
import type { CertificateLifetime, ClusterInfo } from "@/types/api";

interface RegenerateCertificateConfirmProps {
  /** The cluster to regenerate for; the dialog is open while this is set. */
  cluster: ClusterInfo | null;
  pending: boolean;
  onCancel: () => void;
  /** Undefined lifetime: nothing was picked, so the cluster keeps its last one. */
  onConfirm: (lifetime: CertificateLifetime | undefined) => void;
}

/** Before Regenerate issues anything: how long the new certificate should last. */
export function RegenerateCertificateConfirm({ cluster, pending, onCancel, onConfirm }: RegenerateCertificateConfirmProps) {
  const open = !!cluster;
  const clusterId = cluster?.cluster_id ?? "";
  // A cluster still waiting for its first trust has no certificate row; it then gets the default.
  const current = useCertificate(open ? clusterId : "");
  const [lifetime, setLifetime] = React.useState<CertificateLifetime | undefined>(undefined);

  React.useEffect(() => {
    if (open) setLifetime(undefined);
  }, [open, clusterId]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !pending && onCancel()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Regenerate Certificate</DialogTitle>
          <DialogDescription>
            A new certificate for {cluster?.display_name ?? clusterId} is issued as pending. The current one
            stays active until the cluster trusts the new one and you verify it.
          </DialogDescription>
        </DialogHeader>
        <CertificateLifetimeSelect
          id="regenerate-lifetime"
          value={lifetime}
          onChange={setLifetime}
          fallback={current.data?.lifetime ?? "default"}
          disabled={pending}
          enabled={open}
        />
        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={() => onConfirm(lifetime)} disabled={pending}>
            {pending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Regenerating…
              </>
            ) : (
              "Regenerate"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
