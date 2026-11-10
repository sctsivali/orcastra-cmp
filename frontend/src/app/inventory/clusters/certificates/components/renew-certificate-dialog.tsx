"use client";

import * as React from "react";
import { AlertTriangle, CheckCircle, Loader2, RefreshCw, Terminal } from "lucide-react";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "@/hooks/use-toast";
import { useInitiateCertRenewal, usePendingCertificate, useVerifyCertRenewal } from "@/hooks/use-lxd";
import { hypervisorCli, isIncusType } from "@/lib/utils";
import {
  CertificateStatus,
  CertificateTTLOption,
  TTL_LABELS,
  type CertificateInfo,
} from "@/types/api";

import { FingerprintField, TRUST_LIST_CHARS } from "./certificate-fingerprint";

export type RenewDialogMode = "renew" | "verify";

interface ManualTrust {
  cli_command?: string;
  certificate?: string;
  pending_cert_id?: string;
  fingerprint?: string | null;
}

interface Done {
  fingerprint?: string | null;
  previous_fingerprint?: string | null;
}

interface RenewCertificateDialogProps {
  certificate: CertificateInfo | null;
  /** "verify" opens straight on the trust step and loads the pending certificate. */
  mode: RenewDialogMode | null;
  onClose: () => void;
}

const errorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

export function RenewCertificateDialog({ certificate, mode, onClose }: RenewCertificateDialogProps) {
  const open = !!certificate && !!mode;
  const clusterId = certificate?.cluster_id ?? "";
  const cli = hypervisorCli(certificate?.cluster_type);
  // Incus has no core.trust_password, so on Incus the only way back in is the CLI command.
  const offersTrustPassword = !isIncusType(certificate?.cluster_type);
  const [ttl, setTtl] = React.useState<CertificateTTLOption>(CertificateTTLOption.SIX_MONTHS);
  const [trustPassword, setTrustPassword] = React.useState("");
  const [manual, setManual] = React.useState<ManualTrust | null>(null);
  const [done, setDone] = React.useState<Done | null>(null);
  const [step, setStep] = React.useState<"form" | "trust" | "done">("form");

  const renewal = useInitiateCertRenewal();
  const verification = useVerifyCertRenewal();
  // Resuming from a row: the command lived only in the original renew response, so read it back.
  const pending = usePendingCertificate(clusterId, open && step === "trust" && !manual);

  React.useEffect(() => {
    if (!open) return;
    setTtl(CertificateTTLOption.SIX_MONTHS);
    setTrustPassword("");
    setManual(null);
    setDone(null);
    setStep(mode === "verify" ? "trust" : "form");
  }, [open, mode, clusterId]);

  const trust: ManualTrust | null = manual ?? (pending.data
    ? {
        cli_command: pending.data.cli_command,
        certificate: pending.data.certificate,
        pending_cert_id: pending.data.pending_cert_id ?? undefined,
        fingerprint: pending.data.fingerprint,
      }
    : null);

  // isPending only flips on the next render, so a double click in one tick would pass it twice.
  const inFlight = React.useRef(false);

  const handleRenew = async () => {
    if (!certificate || renewal.isPending || inFlight.current) return;
    inFlight.current = true;
    try {
      const result = await renewal.mutateAsync({
        cluster_id: certificate.cluster_id,
        ttl,
        trust_password: (offersTrustPassword && trustPassword) || undefined,
      });
      if (result.auto_renewed) {
        toast({ title: "Certificate renewed", description: "The new certificate is active." });
        setDone({ fingerprint: result.fingerprint, previous_fingerprint: result.previous_fingerprint });
        setStep("done");
      } else if (result.requires_manual_trust) {
        setManual({
          cli_command: result.cli_command,
          certificate: result.certificate,
          pending_cert_id: result.pending_cert_id,
          fingerprint: result.fingerprint,
        });
        setStep("trust");
        toast({
          title: "Manual trust required",
          description: "Run the trust command on the cluster (any one member is enough).",
        });
      }
    } catch (error) {
      toast({
        title: "Could not renew certificate",
        description: errorMessage(error, "Try again."),
        variant: "destructive",
      });
    } finally {
      inFlight.current = false;
    }
  };

  const handleVerify = async () => {
    if (!certificate || verification.isPending || inFlight.current) return;
    inFlight.current = true;
    try {
      const result = await verification.mutateAsync({
        cluster_id: certificate.cluster_id,
        pending_cert_id: trust?.pending_cert_id,
      });
      if (result.verified && result.activated) {
        toast({ title: "Certificate activated", description: "New certificate is now active." });
        setDone({
          fingerprint: result.fingerprint,
          previous_fingerprint: result.previous_fingerprint ?? certificate.fingerprint,
        });
        setStep("done");
      } else {
        toast({
          title: "Certificate not yet verified",
          description: "Certificate not yet trusted. Run the CLI command first.",
          variant: "destructive",
        });
      }
    } catch (error) {
      toast({
        title: "Could not verify certificate",
        description: errorMessage(error, "Try again."),
        variant: "destructive",
      });
      void pending.refetch();
    } finally {
      inFlight.current = false;
    }
  };

  const title =
    step === "done" ? "Certificate Renewed" : step === "trust" ? "Complete Certificate Renewal" : "Renew Certificate";

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader className="pb-4">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{clusterId}</DialogDescription>
        </DialogHeader>

        {step === "form" && certificate && (
          <div className="space-y-4">
            {certificate.status === CertificateStatus.EXPIRED && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>
                  {offersTrustPassword
                    ? "This certificate is expired. You may need to provide a trust password or add trust manually via CLI."
                    : `This certificate is expired. Renewing it gives a ${cli} command to add trust on the host.`}
                </AlertDescription>
              </Alert>
            )}

            <FingerprintField
              label="Current certificate fingerprint"
              value={certificate.fingerprint}
              copyLabel={`current certificate fingerprint for ${clusterId}`}
              hint={`The first ${TRUST_LIST_CHARS} characters are what ${cli} config trust list shows for it.`}
            />

            <div className="space-y-2">
              <Label htmlFor="renew-ttl">Certificate Validity Period</Label>
              <Select value={ttl} onValueChange={(v) => setTtl(v as CertificateTTLOption)}>
                <SelectTrigger id="renew-ttl">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(TTL_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Offered for every renewal. A valid certificate is still rejected once its trust
                entry is replaced or dropped on the LXD side; the backend then cannot
                authenticate with it, and the trust password is the only way to re-add trust
                without shell access to the host. Not on Incus, which has no trust password. */}
            {offersTrustPassword && <div className="space-y-2">
              <Label htmlFor="renew-trust-password">Trust Password (Optional)</Label>
              <Input
                id="renew-trust-password"
                type="password"
                placeholder="Enter cluster trust password"
                value={trustPassword}
                onChange={(e) => setTrustPassword(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Used when the cluster no longer trusts the current certificate. If not
                provided, you will need to add trust manually via CLI.
              </p>
            </div>}

            <DialogFooter>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button onClick={handleRenew} disabled={renewal.isPending}>
                {renewal.isPending ? (
                  <>
                    <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                    Renewing…
                  </>
                ) : (
                  "Renew Certificate"
                )}
              </Button>
            </DialogFooter>
          </div>
        )}

        {step === "trust" && (
          <div className="space-y-4">
            <Alert>
              <Terminal className="h-4 w-4" />
              <AlertDescription>
                Run the following command on the cluster (any one member is enough) to add trust for the new certificate.
              </AlertDescription>
            </Alert>

            {!trust && pending.isLoading && (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Loading the pending certificate
              </p>
            )}
            {!trust && pending.isError && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>
                  {errorMessage(pending.error, "The pending certificate could not be read.")} It may
                  already have been verified or cancelled; close this dialog to see the current state.
                </AlertDescription>
              </Alert>
            )}

            {trust && (
              <FingerprintField
                label="New certificate fingerprint"
                value={trust.fingerprint}
                copyLabel={`new certificate fingerprint for ${clusterId}`}
                hint={`After running the command, ${cli} config trust list shows an entry starting with these ${TRUST_LIST_CHARS} characters.`}
              />
            )}
            {trust?.cli_command && (
              <CodeBlock value={trust.cli_command} label="CLI Command" copyLabel="CLI command" />
            )}
            {trust?.certificate && (
              <CodeBlock value={trust.certificate} label="Certificate (PEM)" copyLabel="Certificate" />
            )}

            <DialogFooter className="gap-2">
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
              <Button onClick={handleVerify} disabled={verification.isPending || (!trust && pending.isLoading)}>
                {verification.isPending ? (
                  <>
                    <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                    Verifying…
                  </>
                ) : (
                  "Verify & Activate"
                )}
              </Button>
            </DialogFooter>
          </div>
        )}

        {step === "done" && (
          <div className="space-y-4">
            <Alert>
              <CheckCircle className="h-4 w-4 text-success" />
              <AlertDescription>
                The new certificate is active. The cluster still trusts the previous one for now:
                its trust entry is removed automatically after a short grace period, or right away
                from the old trust entries link on this cluster&apos;s row.
              </AlertDescription>
            </Alert>
            <FingerprintField
              label="New certificate fingerprint"
              value={done?.fingerprint}
              copyLabel={`new certificate fingerprint for ${clusterId}`}
            />
            <FingerprintField
              label="Previous certificate fingerprint"
              value={done?.previous_fingerprint}
              copyLabel={`previous certificate fingerprint for ${clusterId}`}
            />
            <DialogFooter>
              <Button onClick={onClose}>Close</Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
