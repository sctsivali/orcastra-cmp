"use client";

import * as React from "react";
import { ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { IntentLink } from "@/components/navigation/route-intent";
import { useKernelAdvisories } from "@/hooks/use-kernel-advisories";
import { inventoryLinks } from "@/lib/inventory-links";
import { KernelAdvisoryMembers, KernelAdvisorySummary } from "./kernel-advisory-detail";
import { KERNEL_STATE_DISPLAY, hostsForCluster, rowState, worstOf } from "./kernel-advisory-display";

const ICONS = { exposed: ShieldAlert, unverified: ShieldQuestion, patched: ShieldCheck } as const;

/**
 * A cluster's host kernel verdict, opening every member's detail.
 *
 * Partner and Admin only: the caller renders it inside its isPartner gate and the query is
 * disabled for anyone else. Patched clusters show nothing on the card (`showPatched` for the
 * system panel), so the card only grows when there is something to do.
 */
export function KernelAdvisoryBadge({
  clusterId,
  showPatched = false,
  fallback = null,
}: {
  clusterId: string;
  showPatched?: boolean;
  /** Shown while nothing has been read for this cluster (loading, or it did not answer). */
  fallback?: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const query = useKernelAdvisories();
  const rows = React.useMemo(() => hostsForCluster(query.data, clusterId), [query.data, clusterId]);
  const unreachable = (query.data?.unreachable_nodes ?? []).filter((c) => c === clusterId);
  const state = worstOf(rows.map(rowState));

  if (!state) return <>{fallback}</>;
  if (state === "patched" && !showPatched) return null;

  const display = KERNEL_STATE_DISPLAY[state];
  const Icon = ICONS[state];
  const displayName = rows[0]?.display_name || clusterId;

  return (
    <>
      <Badge asChild variant={display.tone} className="gap-1 cursor-pointer hover:opacity-90">
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label={`${display.cardLabel}: show host kernel advisory for ${displayName}`}
        >
          <Icon className="h-3 w-3" aria-hidden="true" />
          {display.cardLabel}
        </button>
      </Badge>
      <RecordDetailDialog
        open={open}
        onOpenChange={setOpen}
        title="Host Kernel Advisory"
        subtitle={displayName}
        recordId={clusterId}
        recordIdLabel="Cluster"
        status={<Badge variant={display.tone}>{display.label}</Badge>}
        size="xl"
        footer={
          <div className="flex flex-wrap items-center gap-2">
            <RefreshButton
              label="Read the kernels again"
              lastUpdatedAt={query.dataUpdatedAt}
              onRefresh={query.refreshNow}
            />
            <Button asChild size="sm" variant="outline">
              <IntentLink href={inventoryLinks.warnings({ tab: "kernel", cluster: displayName })}>
                Open in Warnings
              </IntentLink>
            </Button>
          </div>
        }
      >
        <div className="space-y-6">
          {(query.data?.advisories ?? []).map((advisory) => (
            <KernelAdvisorySummary key={advisory.id} advisory={advisory} />
          ))}
          <KernelAdvisoryMembers rows={rows} unreachable={unreachable} reasons={query.data?.node_errors} />
        </div>
      </RecordDetailDialog>
    </>
  );
}
