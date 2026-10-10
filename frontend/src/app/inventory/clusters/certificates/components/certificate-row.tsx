"use client";

import { CheckCircle, KeyRound, RefreshCw, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NodeLink } from "@/components/ui/node-link";
import { OverflowText } from "@/components/ui/overflow-text";
import { TableCell, TableRow } from "@/components/ui/table";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { cn, formatDate } from "@/lib/utils";
import { CertificateStatus, type CertificateInfo } from "@/types/api";

import { certificateStatusMeta } from "./certificate-status";
import { FingerprintCell } from "./certificate-fingerprint";
import { daysTone, getDaysDisplay, openSuperseded } from "./certificate-helpers";

interface CertificateRowProps {
  cert: CertificateInfo;
  isReadOnly: boolean;
  /** Another certificate request is in flight; the row's own actions wait for it. */
  busy: boolean;
  onRenew: (cert: CertificateInfo) => void;
  onVerify: (cert: CertificateInfo) => void;
  onCancel: (cert: CertificateInfo) => void;
  onShowSuperseded: (cert: CertificateInfo) => void;
}

function supersededSummary(cert: CertificateInfo): { text: string; failed: boolean } | null {
  const open = openSuperseded(cert);
  const failed = open.filter((entry) => entry.state === "failed").length;
  if (failed) {
    return { text: `${failed} old trust ${failed === 1 ? "entry" : "entries"} not removed`, failed: true };
  }
  if (open.length) {
    return { text: `${open.length} old trust ${open.length === 1 ? "entry" : "entries"} pending removal`, failed: false };
  }
  if ((cert.superseded ?? []).some((entry) => entry.state === "removed")) {
    return { text: "Old trust entry removed", failed: false };
  }
  return null;
}

export function CertificateRow({
  cert,
  isReadOnly,
  busy,
  onRenew,
  onVerify,
  onCancel,
  onShowSuperseded,
}: CertificateRowProps) {
  const config = certificateStatusMeta(cert.status);
  const StatusIcon = config.icon;
  const superseded = supersededSummary(cert);

  return (
    <TableRow>
      <TableCell className="max-w-[16rem]">
        <div>
          <div className="font-medium">
            {/* wrap: the whole id stays readable inside the 16rem cell without a
                second reveal control, and the link is the way to the cluster. */}
            <NodeLink clusterId={cert.cluster_id} wrap />
          </div>
          {(cert.endpoint || cert.common_name) && (
            <TableSubText wrap>
              <OverflowText
                value={cert.endpoint || cert.common_name || ""}
                label={`endpoint for cluster ${cert.cluster_id}`}
              />
            </TableSubText>
          )}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-1">
          <Badge variant={config.tone} className="gap-1 w-fit">
            <StatusIcon className="h-3 w-3" />
            {config.label}
          </Badge>
          {cert.has_pending_renewal && (
            <Badge variant="outline" className="gap-1 w-fit">
              <RefreshCw className="h-3 w-3" />
              Pending
            </Badge>
          )}
        </div>
      </TableCell>
      <TableCell className="min-w-[9rem]">
        <FingerprintCell value={cert.fingerprint} label={`certificate fingerprint for ${cert.cluster_id}`} />
        {superseded && (
          <TableSubText>
            <TableLink
              onClick={() => onShowSuperseded(cert)}
              className={cn(superseded.failed && "text-status-offline")}
              aria-label={`${superseded.text} on ${cert.cluster_id}`}
            >
              {superseded.text}
            </TableLink>
          </TableSubText>
        )}
      </TableCell>
      <TableCell className="hidden md:table-cell">
        <span className="text-sm text-muted-foreground">
          {cert.issuer || <TableEmptyValue label="No issuer" />}
        </span>
      </TableCell>
      <TableCell className="hidden lg:table-cell">
        <span className="text-sm whitespace-nowrap">{formatDate(cert.issued_at)}</span>
      </TableCell>
      <TableCell>
        <span className="text-sm whitespace-nowrap">{formatDate(cert.expires_at)}</span>
      </TableCell>
      <TableCell>
        <span className={cn("text-xs md:text-sm font-medium whitespace-nowrap", daysTone(cert.days_until_expiry))}>
          {getDaysDisplay(cert.days_until_expiry)}
        </span>
      </TableCell>
      <TableCell className="text-right">
        <div className="flex justify-end gap-1 md:gap-2">
          {cert.has_pending_renewal ? (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onVerify(cert)}
                disabled={isReadOnly || busy}
                className="h-8 px-2 md:px-3"
                aria-label={`Verify the pending certificate for ${cert.cluster_id}`}
              >
                <CheckCircle className="h-4 w-4 md:mr-1" />
                <span className="hidden md:inline">Verify</span>
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onCancel(cert)}
                disabled={isReadOnly || busy}
                className="text-status-offline h-8 px-2"
                aria-label={`Cancel the pending renewal for ${cert.cluster_id}`}
              >
                <XCircle className="h-4 w-4" />
              </Button>
            </>
          ) : (
            <Button
              variant={
                cert.status === CertificateStatus.EXPIRED
                  ? "destructive"
                  : cert.status === CertificateStatus.EXPIRING
                  ? "secondary"
                  : "outline"
              }
              size="sm"
              onClick={() => onRenew(cert)}
              disabled={isReadOnly || busy}
              className="h-8 px-2 md:px-3"
              aria-label={`Renew the certificate for ${cert.cluster_id}`}
            >
              <KeyRound className="h-4 w-4 md:mr-1" />
              <span className="hidden md:inline">Renew</span>
            </Button>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}
