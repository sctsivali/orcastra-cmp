"use client";

import { Cpu, HardDrive, MemoryStick, Sparkles } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { GpuPanel } from "@/components/gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";
import { usePermissions } from "@/hooks/use-permissions";
import { cn, formatBytes, formatLabel, formatVcpuCount, EMPTY_VALUE } from "@/lib/utils";
import type { NodeResources } from "@/types";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { UsageMeter } from "@/components/ui/usage-meter";
import { Progress } from "@/components/ui/progress";
import { usagePercent } from "@/lib/usage-tone";
import { cpuAllocation, cpuAllocationFigure, cpuAllocationText, formatCpuThreads } from "@/lib/cpu-allocation";
import { CpuAllocationInfo } from "@/components/cpu/cpu-allocation-info";

/**
 * The four tabs that read NodeResources: the freshest copy the modal has, from the list row or
 * from the live per-cluster read. The System tab reads no resources and lives in its own file.
 */

const UNAVAILABLE = "did not report its hardware";

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  );
}

function Unavailable({ what }: { what: string }) {
  return <p className="text-muted-foreground">No {what} information available</p>;
}

export function NodeCpuPanel({
  resources,
  architecture,
}: {
  resources: NodeResources | null;
  architecture: string;
}) {
  const cpu = cpuAllocation(resources ?? { cpu_total: 0, cpu_used: 0 });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Cpu className="h-5 w-5" />
          CPU Information
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {resources ? (
          <>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Architecture" value={architecture} />
              <Field label="CPU Threads" value={String(resources.cpu_total)} />
              <Field label="CPU Sockets" value={String(resources.cpu_sockets)} />
              <Field
                label="Threads per Socket"
                value={
                  resources.cpu_sockets > 0
                    ? String(Math.floor(resources.cpu_total / resources.cpu_sockets))
                    : EMPTY_VALUE
                }
              />
            </div>

            <div className="border-t pt-4">
              <h4 className="mb-3 flex items-center gap-1.5 text-sm font-semibold">
                Resource Usage
                <CpuAllocationInfo cpu={cpu} />
              </h4>
              <div className="space-y-3">
                {/* The card's allocation meter instead of UsageMeter, which turns red past 90%.
                    An over-commit is a ratio to read and no fault (lib/cpu-allocation.ts). */}
                <div className="space-y-1">
                  <div className="flex justify-between gap-4 text-sm">
                    <span className="text-muted-foreground">Allocated</span>
                    <span className="text-right font-semibold tabular-nums">
                      {`${cpuAllocationFigure(cpu)} (${cpu.ratioLabel})`}
                    </span>
                  </div>
                  {cpu.threads > 0 && (
                    <Progress
                      role="meter"
                      value={cpu.percentClamped}
                      aria-label="vCPU allocation"
                      getValueLabel={() => `${cpuAllocationFigure(cpu)}, ${cpuAllocationText(cpu)}`}
                      className={cn("h-2", cpu.meterClassName)}
                    />
                  )}
                </div>
                <div className="flex justify-between text-sm">
                  <span className="text-muted-foreground">
                    {cpu.over > 0 ? "Beyond capacity" : "Unallocated"}
                  </span>
                  <span className={`font-semibold tabular-nums ${cpu.over > 0 ? cpu.textClassName : ""}`}>
                    {cpu.over > 0 ? `${formatVcpuCount(cpu.over)} (${cpuAllocationText(cpu)})` : formatCpuThreads(cpu.unallocated)}
                  </span>
                </div>
              </div>
            </div>
          </>
        ) : (
          <Unavailable what="CPU" />
        )}
      </CardContent>
    </Card>
  );
}

export function NodeGpuPanel({
  resources,
  nodeLabel,
}: {
  resources: NodeResources | null;
  nodeLabel: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-5 w-5" />
          GPU
        </CardTitle>
        <CardDescription>{GPU_UTILIZATION_NOTE}</CardDescription>
      </CardHeader>
      <CardContent>
        {resources ? (
          <GpuPanel src={resources} />
        ) : (
          <EmptyState
            description={`${nodeLabel} ${UNAVAILABLE}, so its GPUs cannot be listed. This does not mean it has none.`}
            icon={Sparkles}
            title="Resources unavailable"
          />
        )}
      </CardContent>
    </Card>
  );
}

export function NodeMemoryPanel({ resources }: { resources: NodeResources | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MemoryStick className="h-5 w-5" />
          Memory Information
        </CardTitle>
      </CardHeader>
      <CardContent>
        {resources ? (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <Field label="Total Memory" value={formatBytes(resources.memory_total)} />
              <Field
                label="Available Memory"
                value={formatBytes(resources.memory_available)}
              />
            </div>

            <div className="border-t pt-4">
              <h4 className="mb-3 text-sm font-semibold">Resource Usage</h4>
              <UsageMeter
                label="Used"
                total={resources.memory_total}
                used={resources.memory_used}
                value={`${formatBytes(resources.memory_used)} / ${formatBytes(
                  resources.memory_total,
                )} (${usagePercent(resources.memory_used, resources.memory_total).toFixed(1)}%)`}
              />
            </div>
          </div>
        ) : (
          <Unavailable what="memory" />
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Storage for one registered cluster.
 *
 * On a clustered host `storage_total` is usable capacity, counting a shared backing store once
 * however many members report it, while `storage_raw_total` keeps the per-member sum. The panel
 * shows the raw figure whenever the two differ. Backend: services/lxd/storage_model.py.
 */
export function NodeStoragePanel({
  resources,
  nodeName,
}: {
  resources: NodeResources | null;
  nodeName?: string;
}) {
  const { isPartner } = usePermissions();
  const pools = resources?.storage_pools ?? [];
  const lostParts = resources?.degraded_parts ?? [];
  const storageLost = lostParts.some((part) => part.startsWith("storage"));
  const rawTotal = resources?.storage_raw_total ?? 0;
  const sharedSavings = rawTotal - (resources?.storage_total ?? 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HardDrive className="h-5 w-5" />
          Storage Information
        </CardTitle>
        {sharedSavings > 0 && (
          <CardDescription>
            Usable capacity. A shared backing store is counted once, not once per member.
          </CardDescription>
        )}
      </CardHeader>
      <CardContent>
        {resources && (pools.length > 0 || storageLost) ? (
          <div className="space-y-4">
            {storageLost && (
              <PartialReadNotice>
                {pools.length > 0
                  ? "One or more pools did not answer, so the totals below are floors rather than measurements."
                  : "The pool list did not come back, so this cluster's storage is unknown. That is not the same as having none."}{" "}
                Refreshing usually clears it.
              </PartialReadNotice>
            )}

            {pools.length > 0 && (
              <>
                <div className="grid grid-cols-2 gap-4 border-b pb-4 sm:grid-cols-3">
                  <Field label="Total Storage" value={formatBytes(resources.storage_total)} />
                  <Field label="Used Storage" value={formatBytes(resources.storage_used)} />
                  <Field
                    label="Available Storage"
                    value={formatBytes(resources.storage_available)}
                  />
                  {sharedSavings > 0 && (
                    <p className="col-span-2 text-sm text-muted-foreground sm:col-span-3">
                      Members report {formatBytes(rawTotal)} between them. The difference of{" "}
                      {formatBytes(sharedSavings)} is shared storage every member can see.
                    </p>
                  )}
                </div>

                <div>
                  <h4 className="mb-3 text-sm font-semibold">Storage Pools</h4>
                  <div className="space-y-4">
                    {pools.map((pool) => (
                      <div
                        className="space-y-3 rounded-lg border p-4"
                        key={`${pool.name}-${pool.driver}`}
                      >
                        <div>
                          <p className="font-semibold">{pool.name}</p>
                          <p className="text-sm text-muted-foreground">
                            Driver: {formatLabel(pool.driver)}
                            {pool.scope === "shared" && " · shared by the members below"}
                          </p>
                          {/* important: member hostnames are operator detail, gated like the
                              control-plane endpoint on the node card; /cluster-members is
                              require_partner() on the backend. A tenant sees only how many
                              machines the pool spans. A standalone host reports only itself,
                              so the line is skipped there. */}
                          {pool.members &&
                            pool.members.length > 0 &&
                            !(pool.members.length === 1 && pool.members[0] === nodeName) && (
                            <p className="text-sm text-muted-foreground">
                              {isPartner ? (
                                <>
                                  {pool.members.length === 1 ? "Member" : "Members"}:{" "}
                                  <span className="font-mono">{pool.members.join(", ")}</span>
                                </>
                              ) : pool.members.length === 1 ? (
                                "On one cluster member"
                              ) : (
                                `Across ${pool.members.length} cluster members`
                              )}
                            </p>
                          )}
                        </div>

                        <div className="grid grid-cols-3 gap-2 text-sm">
                          <div>
                            <p className="text-muted-foreground">Total</p>
                            <p className="font-semibold">{formatBytes(pool.space_total)}</p>
                          </div>
                          <div>
                            <p className="text-muted-foreground">Used</p>
                            <p className="font-semibold">{formatBytes(pool.space_used)}</p>
                          </div>
                          <div>
                            <p className="text-muted-foreground">Available</p>
                            <p className="font-semibold">{formatBytes(pool.space_available)}</p>
                          </div>
                        </div>

                        <UsageMeter
                          label="Usage"
                          total={pool.space_total}
                          used={pool.space_used}
                          value={`${usagePercent(pool.space_used, pool.space_total).toFixed(1)}%`}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              </>
            )}
          </div>
        ) : (
          <Unavailable what="storage" />
        )}
      </CardContent>
    </Card>
  );
}
