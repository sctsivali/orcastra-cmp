"use client";

import * as React from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Progress } from "@/components/ui/progress";
import { TableLink } from "@/components/ui/table-link";
import { AlertTriangle, Cpu, HardDrive, Layers, MemoryStick, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { cpuAllocation, cpuAllocationText } from "@/lib/cpu-allocation";
import { formatLabel, formatMemory } from "@/lib/utils";
import { usageTone } from "@/lib/usage-tone";
import type { MemberResources } from "@/types/api";
import { GpuPanel } from "../gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";
import { formatBytes, formatBytesCompact, formatMemoryCompact } from "./format";

interface MemberResourcePanelsProps {
  resources: MemberResources;
  memberName: string | null;
  onShowInstances: (filter: "running" | "stopped") => void;
}

/**
 * The populated body of the member resource dialog.
 *
 * important: `degraded_parts` is not decoration. A storage or instance read that failed used to
 * be swallowed and the panel rendered zeroes, so "0 B / 0 B" and a missing Storage Pools card
 * read as "this member has no storage" rather than as "we could not find out". Anything the
 * backend marks lost is named here, next to the number it affects.
 */
export function MemberResourcePanels({
  resources,
  memberName,
  onShowInstances,
}: MemberResourcePanelsProps) {
  const lostParts = resources.degraded_parts ?? [];
  const storageLost = lostParts.some((part) => part.startsWith("storage")) || Boolean(resources.storage_partial);
  const instancesLost = lostParts.includes("instances") || Boolean(resources.instances_partial);

  // note: allocation, not utilisation. The figure is the sum of limits.cpu of the running
  // instances placed here (one core for an instance with no limit); LXD's resources API carries
  // no host CPU utilisation, and KVM lets the sum exceed the thread count, so it is a ratio.
  const cpu = cpuAllocation(resources);

  // What the storage card headlines. A cluster-wide pool (ceph) is seen from every member, and
  // adding it to each one made a 1.7 TB member read as 12.4 TB. A build that reports the
  // local/shared split headlines the member's own disks and lists the shared store once, on its
  // own line; an older payload (no split) falls back to the sum of its rows.
  const storage = React.useMemo(() => {
    const local = resources.storage_local_total;
    const hasSplit = typeof local === "number" && typeof resources.storage_shared_total === "number";
    const total = hasSplit ? local : resources.storage_total;
    const used = hasSplit ? resources.storage_local_used ?? 0 : resources.storage_used;
    return {
      headline: hasSplit ? "Storage (Local)" : "Storage",
      summaryLabel: hasSplit ? "Available Storage (Local)" : "Available Storage",
      total,
      used,
      percent: total > 0 ? (used / total) * 100 : 0,
      sharedTotal: hasSplit ? resources.storage_shared_total ?? 0 : 0,
      sharedUsed: hasSplit ? resources.storage_shared_used ?? 0 : 0,
    };
  }, [resources]);

  return (
    <div className="space-y-6">
      {(resources.degraded || storageLost || instancesLost) && (
        <PartialReadNotice data-testid="member-partial-notice">
          <>
            Part of this reading did not come back from the cluster
            {storageLost && instancesLost
              ? ", so the storage and instance figures below are floors rather than measurements."
              : storageLost
                ? ", so the storage figures below are floors rather than measurements."
                : instancesLost
                  ? ", so the instance and allocation figures below are floors rather than measurements."
                  : "."}{" "}
            Refreshing usually clears it.
          </>
        </PartialReadNotice>
      )}

      {/* Overview Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* CPU */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Cpu className="h-4 w-4 text-muted-foreground" />
              vCPU Allocation
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2" data-partial={instancesLost ? "true" : undefined}>
              <div className="flex items-baseline justify-center gap-1">
                <span className="text-2xl font-bold">{instancesLost ? "?" : cpu.allocated}</span>
                <span className="text-sm text-muted-foreground">vCPU on {cpu.threads} hardware threads</span>
              </div>
              <Progress
                value={instancesLost ? 0 : cpu.percentClamped}
                className={`h-2 ${cpu.meterClassName}`}
              />
              <p className={`flex items-center justify-center gap-1 text-sm font-medium ${instancesLost ? "text-muted-foreground" : cpu.textClassName}`}>
                {cpu.tone === "warning" && !instancesLost && (
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
                )}
                {instancesLost ? "allocation unknown" : cpuAllocationText(cpu)}
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Memory */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <MemoryStick className="h-4 w-4 text-muted-foreground" />
              Memory
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <div className="text-center text-sm">
                <span className="text-2xl font-bold">{formatMemoryCompact(resources.memory_used).value}</span>
                <span className="text-xs text-muted-foreground"> {formatMemoryCompact(resources.memory_used).unit}</span>
                <span className="text-xs text-muted-foreground"> / </span>
                <span className="text-base font-semibold">{formatMemoryCompact(resources.memory_total).value}</span>
                <span className="text-xs text-muted-foreground"> {formatMemoryCompact(resources.memory_total).unit}</span>
              </div>
              <Progress 
                value={resources.memory_usage_percent} 
                className="h-2"
              />
              <p className={`text-sm font-medium text-center ${usageTone(resources.memory_usage_percent).text}`}>
                {resources.memory_usage_percent.toFixed(1)}% used
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Storage */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <HardDrive className="h-4 w-4 text-muted-foreground" />
              {storage.headline}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2" data-partial={storageLost ? "true" : undefined}>
              <div className="text-center text-sm">
                <span className="text-2xl font-bold">{formatBytesCompact(storage.used).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(storage.used).unit}</span>
                <span className="text-xs text-muted-foreground"> / </span>
                <span className="text-base font-semibold">{formatBytesCompact(storage.total).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(storage.total).unit}</span>
              </div>
              <Progress 
                value={storage.percent} 
                className="h-2"
              />
              <p className={`text-sm font-medium text-center ${usageTone(storage.percent).text}`}>
                {storage.percent.toFixed(1)}% used{storageLost ? " (incomplete)" : ""}
              </p>
              {storage.sharedTotal > 0 && (
                <p className="text-xs text-muted-foreground text-center">
                  + {formatBytes(storage.sharedUsed)} / {formatBytes(storage.sharedTotal)} shared, cluster-wide
                </p>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Instances */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Layers className="h-4 w-4 text-muted-foreground" />
              Instances
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2" data-partial={instancesLost ? "true" : undefined}>
              {instancesLost ? (
                <p className="text-center text-sm text-muted-foreground">
                  <span className="text-2xl font-bold text-foreground">?</span> unavailable
                </p>
              ) : (
                <div className="flex items-baseline justify-center gap-1">
                  <span className="text-2xl font-bold">{resources.instances_running}</span>
                  <span className="text-sm text-muted-foreground">/ {resources.instances_count} total</span>
                </div>
              )}
              <div className="flex flex-col items-center justify-center gap-2">
                {!instancesLost && resources.instances_running > 0 && (
                  <TableLink
                    onClick={() => onShowInstances("running")}
                    aria-label={`Show the running instances on ${memberName}`}
                  >
                    {`${resources.instances_running} Running`}
                  </TableLink>
                )}
                {!instancesLost && resources.instances_count - resources.instances_running > 0 && (
                  <TableLink
                    onClick={() => onShowInstances("stopped")}
                    aria-label={`Show the stopped instances on ${memberName}`}
                  >
                    {`${resources.instances_count - resources.instances_running} Stopped`}
                  </TableLink>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* GPU */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Sparkles className="h-4 w-4" />
            GPU
          </CardTitle>
          <CardDescription>{GPU_UTILIZATION_NOTE}</CardDescription>
        </CardHeader>
        <CardContent>
          <GpuPanel src={resources} dense allocationUnknown={instancesLost} />
        </CardContent>
      </Card>

      {/* Storage Pools */}
      {/* important: an empty list used to make this card disappear with no explanation, which
          is the same silence the degraded marker exists to break. A failed read says so. */}
      {storageLost && !(resources.storage_pools ?? []).length ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Storage Pools</CardTitle>
          </CardHeader>
          <CardContent>
            <EmptyState
              icon={HardDrive}
              title="Storage could not be read"
              description="The cluster did not answer for this member's storage pools, so no pool is listed and the totals above exclude them."
            />
          </CardContent>
        </Card>
      ) : null}
      {resources.storage_pools && resources.storage_pools.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Storage Pools</CardTitle>
            <CardDescription>
              Individual storage pool utilization. A shared pool is the cluster&apos;s store as this member sees it, counted once at cluster level.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {resources.storage_pools.map((pool, index) => {
                const usagePercent = pool.space_total > 0
                  ? ((pool.space_used / pool.space_total) * 100)
                  : 0;
            
                return (
                  <div key={index} className="space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <HardDrive className="h-4 w-4 text-muted-foreground" />
                        <span className="font-medium">{pool.name}</span>
                        <span className="text-muted-foreground">{formatLabel(pool.driver)}</span>
                        {pool.scope === "shared" && (
                          <Badge variant="muted">shared, cluster-wide</Badge>
                        )}
                      </div>
                      <span className="text-sm text-muted-foreground">
                        {formatBytes(pool.space_used)} / {formatBytes(pool.space_total)}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <Progress value={usagePercent} className="h-2" />
                      <p className={`text-xs ${usageTone(usagePercent).text}`}>
                        {usagePercent.toFixed(1)}% used • {formatBytes(pool.space_available)} available
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Resource Summary */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Resource Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground mb-1">{cpu.over > 0 ? "vCPU Over-Commit" : "Unallocated Hardware Threads"}</p>
              <p className="font-medium">
                {instancesLost
                  ? "unknown"
                  : cpu.over > 0
                    ? `${cpu.over} vCPU beyond capacity (${cpu.ratioLabel})`
                    : `${cpu.unallocated} hardware threads`}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground mb-1">Available Memory</p>
              <p className="font-medium">{formatMemory(resources.memory_available)}</p>
            </div>
            <div>
              <p className="text-muted-foreground mb-1">{storage.summaryLabel}</p>
              <p className="font-medium">{formatBytes(storage.total - storage.used)}</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
