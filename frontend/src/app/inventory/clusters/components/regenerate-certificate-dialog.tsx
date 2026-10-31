"use client";

import * as React from "react";
import { Loader2, Terminal } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/hooks/use-toast";
import { useCancelPendingCert, useCheckClusterAuth, useVerifyCertRenewal } from "@/hooks/use-lxd";
import { formatHypervisorType, trustPickupHint } from "@/lib/utils";
import type { ClusterInfo, ClusterRegisterResponse } from "@/types/api";

import { FingerprintField, TRUST_LIST_CHARS } from "../certificates/components/certificate-fingerprint";

interface RegenerateCertificateDialogProps {
  cluster: ClusterInfo | null;
  /** What regenerate answered; the dialog is open while this is set. */
  issued: ClusterRegisterResponse | null;
  onClose: () => void;
  /** The new certificate is active: the page refetches. */
  onActivated: () => void;
}

const errorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

/**
 * After Regenerate: the new certificate is PENDING and the current one is still active, so
 * nothing on the cluster is cut off while the operator adds trust. Verify & Activate switches the
 * dashboard to it once the cluster trusts it; Discard drops it. Closing keeps it pending, and the
 * Certificates page can finish it later.
 *
 * note: when the cluster had no certificate at all there is nothing pending (no
 * `pending_cert_id`), the new one is already active, and the old Check Authentication applies.
 */
export function RegenerateCertificateDialog({
  cluster,
  issued,
  onClose,
  onActivated,
}: RegenerateCertificateDialogProps) {
  const verify = useVerifyCertRenewal();
  const discard = useCancelPendingCert();
  const checkAuth = useCheckClusterAuth();
  const [confirmDiscard, setConfirmDiscard] = React.useState(false);
  const inFlight = React.useRef(false);

  const clusterId = cluster?.cluster_id ?? issued?.cluster_id ?? "";
  const pendingId = issued?.pending_cert_id ?? null;
  const isIncus = issued?.cluster_type === "Incus";
  const busy = verify.isPending || discard.isPending || checkAuth.isPending;

  const run = async (action: () => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await action();
    } finally {
      inFlight.current = false;
    }
  };

  const handleVerify = () =>
    run(async () => {
      try {
        if (pendingId) {
          const result = await verify.mutateAsync({ cluster_id: clusterId, pending_cert_id: pendingId });
          if (result.verified && result.activated) {
            toast({ title: "Certificate Activated", description: `Cluster ${clusterId} now uses the new certificate.` });
            onActivated();
            return;
          }
        } else {
          const result = await checkAuth.mutateAsync(clusterId);
          if (result.authenticated) {
            toast({ title: "Authentication Successful", description: `Cluster ${clusterId} is now online` });
            onActivated();
            return;
          }
        }
        toast({
          title: "Not Trusted Yet",
          description: "The cluster does not trust the new certificate yet. Run the command on the cluster first (any one member is enough).",
          variant: "destructive",
        });
      } catch (error) {
        toast({ title: "Verification Failed", description: errorMessage(error, "Failed to verify the certificate"), variant: "destructive" });
      }
    });

  const handleDiscard = () =>
    run(async () => {
      try {
        await discard.mutateAsync(clusterId);
        toast({ title: "New Certificate Discarded", description: "The current certificate stays active." });
        setConfirmDiscard(false);
        onClose();
      } catch (error) {
        toast({ title: "Discard Failed", description: errorMessage(error, "Failed to discard the certificate"), variant: "destructive" });
      }
    });

  return (
    <>
      <Dialog open={!!issued} onOpenChange={(open) => !open && onClose()}>
        <DialogContent className="max-w-3xl">
          <DialogHeader className="pb-4">
            <DialogTitle>Certificate Regenerated</DialogTitle>
            <DialogDescription>
              {pendingId
                ? `Add trust for the new certificate on ${cluster?.display_name ?? clusterId}, then verify it. The current certificate stays active until then.`
                : `Add trust to complete the authentication for ${cluster?.display_name ?? clusterId}.`}
            </DialogDescription>
          </DialogHeader>

          {issued && (
            <div className="space-y-4">
              <Alert>
                <Terminal className="h-4 w-4" />
                <AlertDescription className="space-y-3">
                  <p className="text-sm font-medium">Run this command on the cluster (any one member is enough):</p>
                  {issued.cli_command && (
                    <CodeBlock
                      value={issued.cli_command}
                      label={
                        <>
                          <Badge variant={isIncus ? "secondary" : "outline"} className="mr-2">
                            {formatHypervisorType(issued.cluster_type)}
                          </Badge>
                          Identity: <span className="font-mono">{issued.identity_name}</span>
                        </>
                      }
                      copyLabel="CLI command"
                    />
                  )}
                </AlertDescription>
              </Alert>

              <FingerprintField
                label="New certificate fingerprint"
                value={issued.fingerprint}
                copyLabel={`new certificate fingerprint for ${clusterId}`}
                hint={`After running the command, lxc config trust list shows an entry starting with these ${TRUST_LIST_CHARS} characters.`}
              />

              <div className="flex flex-wrap gap-3">
                <Button onClick={() => void handleVerify()} disabled={busy} className="flex-1">
                  {verify.isPending || checkAuth.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      Verifying…
                    </>
                  ) : pendingId ? (
                    "Verify & Activate"
                  ) : (
                    "Verify Connection"
                  )}
                </Button>
                {pendingId && (
                  <Button variant="outline" onClick={() => setConfirmDiscard(true)} disabled={busy}>
                    Discard
                  </Button>
                )}
                <Button variant="outline" onClick={onClose} disabled={busy}>
                  Close
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                {pendingId
                  ? "Closing keeps the new certificate pending; finish or cancel it later from the Certificates page."
                  : trustPickupHint("Verify Connection")}
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <DestructiveConfirmDialog
        open={confirmDiscard}
        onOpenChange={setConfirmDiscard}
        title="Discard the new certificate?"
        description="The current certificate stays active. If the new one was already added to the cluster's trust, that entry is removed automatically later."
        confirmLabel="Discard certificate"
        pendingLabel="Discarding…"
        pending={discard.isPending}
        onConfirm={() => void handleDiscard()}
      />
    </>
  );
}
