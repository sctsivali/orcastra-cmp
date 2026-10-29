"use client";

import * as React from "react";
import { ActionChip } from "@/components/ui/action-chip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tooltip, TooltipContent, TooltipPortal, TooltipTrigger } from "@/components/ui/tooltip";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { IntentLink } from "@/components/navigation/route-intent";
import { useKernelAdvisories } from "@/hooks/use-kernel-advisories";
import { inventoryLinks } from "@/lib/inventory-links";
import { KernelAdvisoryMembers, KernelAdvisorySummary, UnknownAdvisorySummary } from "./kernel-advisory-detail";
import { KERNEL_STATE_DISPLAY, clusterVerdict, cveCountText, hostsForCluster } from "./kernel-advisory-display";
import { KernelCveList } from "./kernel-cve-list";

// note: Radix opens a tooltip on focus, and two focuses here are not a reader asking for the
// preview. Closing the dialog hands focus back to the chip (ui/dialog.tsx restores the opener),
// and a tap focuses it after its pointerup, so Radix takes it for keyboard focus and flashes the
// preview over the card just before the dialog covers it. Each arms this window, and the first
// focus inside it skips the preview. A window rather than a one-shot flag: Safari does not focus a
// clicked button, so the focus may never come, and a flag would then swallow the next Tab.
const QUIET_FOCUS_MS = 1000;

/**
 * A cluster's open kernel CVEs as a count chip: hover or focus previews the list, a click, tap or
 * Enter opens every member's detail.
 *
 * Partner and Admin only: the caller renders it inside its isPartner gate and the query is
 * disabled for anyone else. Patched clusters show nothing on the card (`showPatched` for the
 * system panel), so the card only grows when there is something to do.
 */
export function KernelAdvisoryBadge({
  clusterId,
  showPatched = false,
  fallback = null,
  unreadFallback,
}: {
  clusterId: string;
  showPatched?: boolean;
  /** Shown while nothing has been read for this cluster yet. */
  fallback?: React.ReactNode;
  /** Shown instead of `fallback` once the read failed or the cluster did not answer. */
  unreadFallback?: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const quietFocusUntil = React.useRef(0);
  const query = useKernelAdvisories();
  const rows = React.useMemo(() => hostsForCluster(query.data, clusterId), [query.data, clusterId]);
  const advisories = query.data?.advisories;
  const verdict = React.useMemo(() => clusterVerdict(rows, advisories ?? []), [rows, advisories]);
  const unreachable = (query.data?.unreachable_nodes ?? []).filter((c) => c === clusterId);

  if (!verdict.state) {
    const unread = query.isError || unreachable.length > 0;
    return <>{unread && unreadFallback !== undefined ? unreadFallback : fallback}</>;
  }
  if (verdict.state === "patched" && !showPatched) return null;

  const display = KERNEL_STATE_DISPLAY[verdict.state];
  const displayName = rows[0]?.display_name || clusterId;
  const text = verdict.open.length > 0 ? cveCountText(verdict.open.length) : display.label;
  // important: the name starts with the visible text (WCAG 2.5.3); an aria-label replaces it.
  const spoken = verdict.open.length > 0 ? `${text}, ${display.label.toLowerCase()}` : text;

  const chip = (
    <ActionChip
      icon={display.icon}
      iconClassName={display.iconClassName}
      aria-label={`${spoken}: show kernel CVE details for ${displayName}`}
      onClick={() => setOpen(true)}
      onPointerDown={(event) => {
        if (event.pointerType !== "mouse") quietFocusUntil.current = performance.now() + QUIET_FOCUS_MS;
      }}
      onFocus={(event) => {
        if (performance.now() < quietFocusUntil.current) {
          quietFocusUntil.current = 0;
          event.preventDefault();
        }
      }}
    >
      {text}
    </ActionChip>
  );

  return (
    <>
      {verdict.open.length > 0 ? (
        <Tooltip>
          <TooltipTrigger asChild>{chip}</TooltipTrigger>
          <TooltipPortal>
            <TooltipContent side="bottom" align="start" data-slot="kernel-cve-tooltip" className="w-72 p-3">
              <KernelCveList items={verdict.open} />
            </TooltipContent>
          </TooltipPortal>
        </Tooltip>
      ) : (
        chip
      )}
      <RecordDetailDialog
        open={open}
        onOpenChange={(next) => {
          if (!next) quietFocusUntil.current = performance.now() + QUIET_FOCUS_MS;
          setOpen(next);
        }}
        title="Kernel CVEs"
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
          {(advisories ?? []).map((advisory) => (
            <KernelAdvisorySummary
              key={advisory.id}
              advisory={advisory}
              ubuntuCheckEnabled={query.data?.ubuntu_check_enabled ?? true}
            />
          ))}
          {verdict.open
            .filter((item) => item.advisory === null)
            .map((item) => (
              <UnknownAdvisorySummary key={item.id} id={item.id} />
            ))}
          <KernelAdvisoryMembers rows={rows} unreachable={unreachable} reasons={query.data?.node_errors} />
        </div>
      </RecordDetailDialog>
    </>
  );
}
