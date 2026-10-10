"use client";

import * as React from "react";
import { RefreshCw } from "lucide-react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
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
import { toast } from "@/hooks/use-toast";
import { useRemoveSupersededTrust } from "@/hooks/use-lxd";
import { formatDateTime, formatLabel } from "@/lib/utils";
import type { CertificateInfo, SupersededTrust, SupersededTrustState } from "@/types/api";

import { FingerprintField } from "./certificate-fingerprint";

const STATE: Record<SupersededTrustState, { label: string; tone: BadgeTone }> = {
  pending: { label: "Pending Removal", tone: "caution" },
  failed: { label: "Not Removed", tone: "negative" },
  removed: { label: "Removed", tone: "positive" },
  cancelled: { label: "Still in Use", tone: "neutral" },
};

const REASON: Record<string, string> = {
  renew: "Replaced by a renewal",
  verify: "Replaced by a verified renewal",
  regenerate: "Replaced by a regenerated certificate",
  register: "Replaced when the cluster was registered again",
  cancel: "Pending renewal cancelled",
  pending_replaced: "Pending renewal replaced by a newer one",
  pending_expired: "Pending renewal expired after 24 hours",
  renew_aborted: "Renewal could not be activated",
};

function entryTiming(entry: SupersededTrust): string {
  if (entry.state === "removed") {
    const when = formatDateTime(entry.removed_at);
    return entry.already_gone ? `Already gone from the cluster (checked ${when})` : `Removed ${when}`;
  }
  if (entry.state === "pending" && entry.due_at) return `Next attempt after ${formatDateTime(entry.due_at)}`;
  return `Superseded ${formatDateTime(entry.superseded_at)}`;
}

function SupersededEntry({
  entry,
  clusterId,
  busy,
  readOnly,
  onRemove,
}: {
  entry: SupersededTrust;
  clusterId: string;
  busy: boolean;
  readOnly: boolean;
  onRemove: (fingerprint: string) => void;
}) {
  const state = STATE[entry.state] ?? STATE.pending;
  const open = entry.state === "pending" || entry.state === "failed";
  return (
    <li className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={state.tone}>{state.label}</Badge>
          <span className="text-sm">{REASON[entry.reason] ?? formatLabel(entry.reason)}</span>
        </div>
        {open && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => onRemove(entry.fingerprint)}
            disabled={busy || readOnly}
            aria-label={`Remove trust entry ${entry.fingerprint} from ${clusterId} now`}
          >
            Remove now
          </Button>
        )}
      </div>
      <FingerprintField
        label={entry.identity_name ? `Fingerprint (${entry.identity_name})` : "Fingerprint"}
        value={entry.fingerprint}
        copyLabel={`superseded certificate fingerprint for ${clusterId}`}
      />
      <p className="text-xs text-muted-foreground">{entryTiming(entry)}</p>
      {entry.last_error && open && (
        <p className="text-xs text-status-offline">Last attempt: {entry.last_error}</p>
      )}
      {entry.manual_command && entry.state === "failed" && (
        <CodeBlock value={entry.manual_command} label="Remove it by hand" copyLabel="remove command" />
      )}
    </li>
  );
}

interface TrustCleanupDialogProps {
  certificate: CertificateInfo | null;
  isReadOnly: boolean;
  onClose: () => void;
}

export function TrustCleanupDialog({ certificate, isReadOnly, onClose }: TrustCleanupDialogProps) {
  const removal = useRemoveSupersededTrust();
  const [entries, setEntries] = React.useState<SupersededTrust[]>([]);
  const clusterId = certificate?.cluster_id ?? "";

  React.useEffect(() => {
    setEntries(certificate?.superseded ?? []);
  }, [certificate]);

  // isPending only flips on the next render, so a double click in one tick would pass it twice.
  const inFlight = React.useRef(false);

  const remove = async (fingerprint?: string) => {
    if (!certificate || removal.isPending || inFlight.current) return;
    inFlight.current = true;
    try {
      const result = await removal.mutateAsync({ clusterId, fingerprint });
      setEntries(result.superseded);
      const failed = Object.values(result.outcomes).filter((o) => o === "failed" || o === "forbidden").length;
      toast({
        title: failed ? "Some entries were not removed" : "Trust cleanup finished",
        description: result.message,
        variant: failed ? "destructive" : undefined,
      });
    } catch (error) {
      toast({
        title: "Trust cleanup failed",
        description: error instanceof Error ? error.message : "The cluster could not be reached.",
        variant: "destructive",
      });
    } finally {
      inFlight.current = false;
    }
  };

  const openCount = entries.filter((e) => e.state === "pending" || e.state === "failed").length;

  return (
    <Dialog open={!!certificate} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Old Trust Entries</DialogTitle>
          <DialogDescription>
            Certificates this dashboard replaced on {clusterId}. Each one stays trusted by the
            cluster until its entry is removed, which happens automatically once the cluster trusts
            the current certificate and the grace period has passed.
          </DialogDescription>
        </DialogHeader>
        {entries.length === 0 ? (
          <p className="text-sm text-muted-foreground">No replaced certificates are tracked for this cluster.</p>
        ) : (
          <ul className="max-h-[60vh] space-y-3 overflow-y-auto pr-1">
            {entries.map((entry) => (
              <SupersededEntry
                key={entry.fingerprint}
                entry={entry}
                clusterId={clusterId}
                busy={removal.isPending}
                readOnly={isReadOnly}
                onRemove={(fp) => void remove(fp)}
              />
            ))}
          </ul>
        )}
        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          {openCount > 1 && (
            <Button onClick={() => void remove()} disabled={removal.isPending || isReadOnly}>
              {removal.isPending ? (
                <>
                  <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                  Removing...
                </>
              ) : (
                `Remove all ${openCount}`
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
