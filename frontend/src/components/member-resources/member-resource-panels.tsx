"use client";

import * as React from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { TableLink } from "@/components/ui/table-link";
import { AlertTriangle, Cpu, HardDrive, Layers, MemoryStick, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { cpuAllocation, cpuAllocationText, formatCpuThreads } from "@/lib/cpu-allocation";
import { CpuAllocationCaption, CpuAllocationInfo, CpuAllocationMeter } from "@/components/cpu/cpu-allocation-info";
import { formatLabel, formatVcpuCount } from "@/lib/utils";
import { usageLevel, usagePercent } from "@/lib/usage-tone";
import type { MemberResources } from "@/types/api";
import { GpuPanel } from "../gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";
import { formatBytes, formatBytesCompact } from "./format";
import { useByteFormat } from "@/hooks/use-byte-format";

interface MemberResourcePanelsProps {
  resources: MemberResources;
  memberName: string | null;
  onShowInstances: (filter: "running" | "stopped") => void;
}

/**
 * The populated body of the member resource dialog.
 *
 * important: honour `degraded_parts`. Without it a failed storage or instance read renders as
 * zeroes, which reads as "this member has no storage" instead of "could not be read". Anything
 * the backend marks lost is named here, next to the number it affects.
 */
export function MemberResourcePanels({
  resources,
  memberName,
  onShowInstances,
}: MemberResourcePanelsProps) {
  const { units } = useByteFormat();
  const lostParts = resources.degraded_parts ?? [];
  const storageLost = lostParts.some((part) => part.startsWith("storage")) || Boolean(resources.storage_partial);
  const instancesLost = lostParts.includes("instances") || Boolean(resources.instances_partial);

  // Allocation, not utilisation: the sum of limits.cpu of the running instances placed here
  // (one core for an instance with no limit). LXD's resources API carries no host CPU
  // utilisation, and KVM lets the sum exceed the thread count, so it is a ratio.
  const cpu = cpuAllocation(resources);
  // The API's own percentage, held to the range a bar can draw.
  const memoryPercent = usagePercent(resources.memory_usage_percent, 100);

  // What the storage card headlines. A cluster-wide pool (ceph) is seen from every member, so
  // adding it to each would inflate every member's total. With the local/shared split the card
  // headlines the member's own disks and lists the shared store once, on its own line; a
  // payload without the split falls back to the sum of its rows.
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
      percent: usagePercent(used, total),
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

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Cpu className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              vCPU Allocation
              <CpuAllocationInfo cpu={instancesLost ? undefined : cpu} scope="cluster member" />
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2" data-partial={instancesLost ? "true" : undefined}>
              <div className="flex items-baseline justify-center gap-1">
                <span className="text-2xl font-bold">{instancesLost ? "?" : cpu.allocated}</span>{" "}
                <span className="text-sm text-muted-foreground">
                  {cpu.allocated === 1 ? "vCPU" : "vCPUs"} / {formatCpuThreads(cpu.threads)}
                </span>
              </div>
              {/* An unknown allocation draws an empty track: zero would read as free capacity. */}
              {instancesLost ? (
                <UsageBar percent={0} label="vCPU allocation" valueText="unknown" className="h-2" />
              ) : (
                <CpuAllocationMeter cpu={cpu} className="h-2" />
              )}
              <p className="flex items-center justify-center gap-1 text-sm font-medium">
                {instancesLost ? (
                  <span className="text-muted-foreground">allocation unknown</span>
                ) : (
                  <>
                    {cpu.level === "Critical" && (
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-status-offline" aria-hidden />
                    )}
                    <CpuAllocationCaption cpu={cpu} />
                  </>
                )}
              </p>
            </div>
          </CardContent>
        </Card>

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
                <span className="text-2xl font-bold">{formatBytesCompact(resources.memory_used, units).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.memory_used, units).unit}</span>
                <span className="text-xs text-muted-foreground"> / </span>
                <span className="text-base font-semibold">{formatBytesCompact(resources.memory_total, units).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.memory_total, units).unit}</span>
              </div>
              <UsageBar
                percent={memoryPercent}
                label="Memory"
                valueText={`${formatBytes(resources.memory_used, units)} / ${formatBytes(resources.memory_total, units)} (${memoryPercent.toFixed(1)}%)`}
                className="h-2"
              />
              <p className="text-sm font-medium text-center">
                <UsageCaption level={usageLevel(memoryPercent)}>{`${memoryPercent.toFixed(1)}% used`}</UsageCaption>
              </p>
            </div>
          </CardContent>
        </Card>

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
                <span className="text-2xl font-bold">{formatBytesCompact(storage.used, units).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(storage.used, units).unit}</span>
                <span className="text-xs text-muted-foreground"> / </span>
                <span className="text-base font-semibold">{formatBytesCompact(storage.total, units).value}</span>
                <span className="text-xs text-muted-foreground"> {formatBytesCompact(storage.total, units).unit}</span>
              </div>
              <UsageBar
                percent={storage.percent}
                label={storage.headline}
                valueText={`${formatBytes(storage.used, units)} / ${formatBytes(storage.total, units)} (${storage.percent.toFixed(1)}%)${storageLost ? ", incomplete" : ""}`}
                className="h-2"
              />
              <p className="text-sm font-medium text-center">
                <UsageCaption level={usageLevel(storage.percent)}>
                  {`${storage.percent.toFixed(1)}% used${storageLost ? " (incomplete)" : ""}`}
                </UsageCaption>
              </p>
              {storage.sharedTotal > 0 && (
                <p className="text-xs text-muted-foreground text-center">
                  + {formatBytes(storage.sharedUsed, units)} / {formatBytes(storage.sharedTotal, units)} shared, cluster-wide
                </p>
              )}
            </div>
          </CardContent>
        </Card>

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

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Sparkles className="h-4 w-4" />
            GPU
          </CardTitle>
          <CardDescription>{GPU_UTILIZATION_NOTE}</CardDescription>
        </CardHeader>
        <CardContent>
          <GpuPanel src={resources} dense allocationUnknown={instancesLost} scope="member" />
        </CardContent>
      </Card>

      {/* A pool list lost to a failed read says so instead of the card disappearing. */}
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
                const poolPercent = usagePercent(pool.space_used, pool.space_total);

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
                        {formatBytes(pool.space_used, units)} / {formatBytes(pool.space_total, units)}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <UsageBar
                        percent={poolPercent}
                        label={`${pool.name} usage`}
                        valueText={`${formatBytes(pool.space_used, units)} / ${formatBytes(pool.space_total, units)} (${poolPercent.toFixed(1)}%)`}
                        className="h-2"
                      />
                      <p className="text-xs">
                        <UsageCaption level={usageLevel(poolPercent)}>{`${poolPercent.toFixed(1)}% used`}</UsageCaption>
                        <span className="text-muted-foreground"> · {formatBytes(pool.space_available, units)} available</span>
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Resource Summary</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground mb-1">{cpu.over > 0 ? "Beyond Capacity" : "Unallocated CPU Threads"}</p>
              <p className="font-medium">
                {instancesLost
                  ? "unknown"
                  : cpu.over > 0
                    ? `${formatVcpuCount(cpu.over)} (${cpuAllocationText(cpu)})`
                    : formatCpuThreads(cpu.unallocated)}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground mb-1">Available Memory</p>
              <p className="font-medium">{formatBytes(resources.memory_available, units)}</p>
            </div>
            <div>
              <p className="text-muted-foreground mb-1">{storage.summaryLabel}</p>
              <p className="font-medium">{formatBytes(storage.total - storage.used, units)}</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
