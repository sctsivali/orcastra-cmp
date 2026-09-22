"use client";

import { AlertTriangle, Info, RefreshCw } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";
import { ClusterStatus } from "@/types/api";
import type { ClusterStatusMeta } from "@/lib/cluster-status";

interface NodeStatusNoticeProps {
  /** Resolved status of the currently selected node, if one is selected. */
  meta?: ClusterStatusMeta;
  /** The backend's short explanation, ex; "Cluster did not answer." */
  reason?: string;
  /** Label the operator recognises, so the line names the cluster they picked. */
  nodeLabel?: string;
  /** Reachability could not be established at all. Takes priority over `meta`. */
  unavailable?: boolean;
  /**
   * Nothing in the list can be picked, because every cluster in the operator's scope is
   * still waiting on its trust handshake. A dropdown where every row is dimmed and nothing
   * says why is a dead end, and on this fleet it is the ordinary state for a partner.
   */
  noSelectableNodes?: boolean;
  /** Epoch ms of the reachability data, so the line can say how old the claim is. */
  updatedAt?: number | null;
  onRecheck?: () => Promise<unknown> | void;
  className?: string;
}

function ageLabel(updatedAt?: number | null): string {
  if (!updatedAt) return "";
  const seconds = Math.max(0, Math.round((Date.now() - updatedAt) / 1000));
  if (seconds < 60) return "moments ago";
  const minutes = Math.round(seconds / 60);
  return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
}

/**
 * The consequence of picking a cluster that will not answer, said where the operator is
 * actually looking.
 *
 * A twelve-pixel word inside a closed popover is not a warning. This sits under the field,
 * next to the submit button, and only for a selected node that is not online, so it stays
 * rare enough to keep meaning something.
 */
export function NodeStatusNotice({
  meta,
  reason,
  nodeLabel,
  unavailable,
  noSelectableNodes,
  updatedAt,
  onRecheck,
  className,
}: NodeStatusNoticeProps) {
  const [rechecking, setRechecking] = useState(false);

  if (unavailable) {
    return (
      <p
        role="status"
        className={cn(
          "flex items-start gap-2 text-xs text-muted-foreground",
          className,
        )}
      >
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>Cluster reachability is unavailable. Clusters are listed without status.</span>
      </p>
    );
  }

  if (noSelectableNodes && !meta) {
    return (
      <p
        role="status"
        className={cn("flex items-start gap-2 text-xs", className)}
      >
        <AlertTriangle
          className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-pending"
          aria-hidden
        />
        <span className="text-muted-foreground">
          No cluster in your scope has completed its trust handshake yet, so none can be
          selected. Register or re-authorize a cluster before creating here.
        </span>
      </p>
    );
  }

  if (!meta || meta.status === ClusterStatus.ONLINE) return null;

  const name = nodeLabel ? `${nodeLabel} ` : "";
  const headline =
    meta.status === ClusterStatus.NOT_AUTHORIZED || meta.status === ClusterStatus.PENDING
      ? `${name}has not completed the trust handshake, so creating here will fail.`
      : `${name}did not answer the last reachability check. Creating here will fail until it responds.`;
  // note: the age is its own sentence. Folding it into the headline produced "did not answer
  // when last checked just now", which reads as a contradiction.
  const checked = updatedAt ? ` Checked ${ageLabel(updatedAt)}.` : "";
  // The curated reason repeats the headline for offline and untrusted clusters, so it only
  // earns its place when the status is one the headline cannot explain.
  const detail = meta.status === ClusterStatus.ERROR && reason ? ` ${reason}` : "";

  const handleRecheck = async () => {
    if (!onRecheck || rechecking) return;
    setRechecking(true);
    try {
      await onRecheck();
    } finally {
      setRechecking(false);
    }
  };

  return (
    <div
      role="status"
      className={cn("flex items-start gap-2 text-xs", className)}
    >
      <AlertTriangle
        className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", meta.iconClassName)}
        aria-hidden
      />
      <p className="text-muted-foreground">
        <span className="sr-only">{meta.srLabel}. </span>
        {headline}
        {detail}
        {checked}
        {onRecheck && (
          <button
            type="button"
            onClick={handleRecheck}
            disabled={rechecking}
            className="ml-1.5 inline-flex items-center gap-1 underline decoration-dotted underline-offset-2 hover:decoration-solid disabled:opacity-50"
          >
            <RefreshCw
              className={cn("h-3 w-3", rechecking && "animate-spin")}
              aria-hidden
            />
            {rechecking ? "Rechecking" : "Recheck"}
          </button>
        )}
      </p>
    </div>
  );
}
