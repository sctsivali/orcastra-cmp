"use client";

import * as React from "react";
import { LoadingLottieMedium } from "./loading-lottie";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { TableLink } from "@/components/ui/table-link";
import { Progress } from "@/components/ui/progress";
import { cpuAllocation, cpuAllocationText } from "@/lib/cpu-allocation";
import { Cpu, MemoryStick, HardDrive, Layers, Server, Activity, Sparkles, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { formatLabel, formatInstanceType } from "@/lib/utils";
import type { VMInfo, MemberResources } from "@/types/api";
import { GpuPanel } from "./gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";

interface MemberResourceModalProps {
  isOpen: boolean;
  onClose: () => void;
  memberName: string | null;
  nodeName?: string | null;
  resources: MemberResources | null;
  isLoading: boolean;
  /** Why the read failed, in the words of the API, when there is nothing to show. */
  error?: string | null;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`;
}

function formatBytesCompact(bytes: number): { value: string; unit: string } {
  if (bytes === 0) return { value: "0", unit: "B" };
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return {
    value: (bytes / Math.pow(k, i)).toFixed(2),
    unit: sizes[i]
  };
}

function getUsageColor(percent: number): string {
  if (percent >= 90) return "text-red-500";
  if (percent >= 75) return "text-orange-500";
  if (percent >= 50) return "text-yellow-500";
  return "text-green-500";
}

function getProgressColor(percent: number): string {
  if (percent >= 90) return "bg-red-500";
  if (percent >= 75) return "bg-orange-500";
  if (percent >= 50) return "bg-yellow-500";
  return "bg-green-500";
}

export function MemberResourceModal({
  isOpen,
  onClose,
  memberName,
  nodeName,
  resources,
  isLoading,
  error,
}: MemberResourceModalProps) {
  const [showInstanceModal, setShowInstanceModal] = React.useState(false);
  const [instanceFilter, setInstanceFilter] = React.useState<"running" | "stopped" | null>(null);
  const [instances, setInstances] = React.useState<VMInfo[]>([]);
  const [loadingInstances, setLoadingInstances] = React.useState(false);

  const [instancesError, setInstancesError] = React.useState<string | null>(null);

  // What the storage card headlines. A build that reports the local/shared split headlines
  // the member's own disks; an older payload (no split) falls back to the sum of its rows,
  // which is what it always showed.
  const cpu = cpuAllocation(resources ?? { cpu_total: 0, cpu_used: 0 });
  const storage = React.useMemo(() => {
    const local = resources?.storage_local_total;
    const hasSplit = typeof local === "number" && typeof resources?.storage_shared_total === "number";
    const total = hasSplit ? local : resources?.storage_total ?? 0;
    const used = hasSplit ? resources?.storage_local_used ?? 0 : resources?.storage_used ?? 0;
    return {
      headline: hasSplit ? "Storage (Local)" : "Storage",
      summaryLabel: hasSplit ? "Available Storage (Local)" : "Available Storage",
      total,
      used,
      percent: total > 0 ? (used / total) * 100 : 0,
      sharedTotal: hasSplit ? resources?.storage_shared_total ?? 0 : 0,
      sharedUsed: hasSplit ? resources?.storage_shared_used ?? 0 : 0,
    };
  }, [resources]);

  // important: this used to set an empty array on purpose, with a comment saying LXD does not
  // report which member an instance runs on. It does; VMInfo simply did not carry the field, so
  // the drill-down always rendered "Instance Details Unavailable". `location` is now modelled
  // and both builders populate it.
  //
  // important: it filters the tenant-scoped /instances list rather than reading the member scan
  // that produced the badge count. That endpoint applies no tenant ACL, so returning its
  // instance names here would leak across organisations. The consequence is that the list can
  // be shorter than the badge for a tenant, and the copy says "visible to you" rather than
  // pretending otherwise. Do not "fix" the mismatch by filtering the count.
  const handleShowInstances = async (filter: "running" | "stopped") => {
    if (!memberName || !nodeName) return;

    setInstanceFilter(filter);
    setLoadingInstances(true);
    setInstancesError(null);
    setShowInstanceModal(true);

    try {
      const response = await api.instances.list(nodeName);
      const wanted = filter === "running" ? "Running" : "Stopped";
      setInstances(
        (response.vms ?? []).filter(
          (vm) => vm.location === memberName && vm.status === wanted,
        ),
      );
    } catch (error) {
      console.error("Error loading instances:", error);
      setInstances([]);
      setInstancesError(error instanceof Error ? error.message : "Could not load instances");
    } finally {
      setLoadingInstances(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="pb-4">
          <DialogTitle>
            {memberName || "Member Resources"}
          </DialogTitle>
          <DialogDescription>
            Resource usage and availability for this cluster member
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="pr-2">
        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <LoadingLottieMedium />
          </div>
        ) : resources ? (
          <div className="space-y-6">
            {(resources.instances_partial || resources.storage_partial) && (
              // note: a floor presented as a fact reads as free capacity. This says which
              // figures could not be read, above the cards that would otherwise show zeros.
              <Alert data-testid="member-partial-notice" role="status">
                <Activity className="h-4 w-4" />
                <AlertDescription>
                  {resources.instances_partial && resources.storage_partial
                    ? "The instance list and part of the storage could not be read from this member, so the instance count, the CPU and GPU allocation and the storage figures are unknown."
                    : resources.instances_partial
                      ? "The instance list could not be read from this member, so the instance count and the CPU and GPU allocation are unknown."
                      : "A storage pool could not be read for this member, so the storage figures are a floor."}
                </AlertDescription>
              </Alert>
            )}

            {/* Overview Cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {/* CPU */}
              <Card>
                <CardHeader className="pb-3">
                  <CardTitle className="text-sm font-medium flex items-center gap-2">
                    <Cpu className="h-4 w-4 text-blue-500" />
                    CPU Allocated
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {/* note: allocation, not utilisation. The figure is the sum of limits.cpu of the
                      running instances placed here (one core for an instance with no limit); LXD's
                      resources API carries no host CPU utilisation. */}
                  <div className="space-y-2" data-partial={resources.instances_partial ? "true" : undefined}>
                    <div className="flex items-baseline justify-center gap-1">
                      <span className="text-2xl font-bold">{resources.instances_partial ? "?" : cpu.allocated}</span>
                      <span className="text-sm text-muted-foreground">vCPU on {cpu.threads} threads</span>
                    </div>
                    <Progress
                      value={resources.instances_partial ? 0 : cpu.percentClamped}
                      className={`h-2 ${cpu.meterClassName}`}
                    />
                    <p className={`flex items-center justify-center gap-1 text-sm font-medium ${resources.instances_partial ? "text-muted-foreground" : cpu.textClassName}`}>
                      {cpu.tone === "warning" && !resources.instances_partial && (
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      )}
                      {resources.instances_partial ? "allocation unknown" : cpuAllocationText(cpu)}
                    </p>
                  </div>
                </CardContent>
              </Card>

              {/* Memory */}
              <Card>
                <CardHeader className="pb-3">
                  <CardTitle className="text-sm font-medium flex items-center gap-2">
                    <MemoryStick className="h-4 w-4 text-purple-500" />
                    Memory
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    <div className="text-center text-sm">
                      <span className="text-2xl font-bold">{formatBytesCompact(resources.memory_used).value}</span>
                      <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.memory_used).unit}</span>
                      <span className="text-xs text-muted-foreground"> / </span>
                      <span className="text-base font-semibold">{formatBytesCompact(resources.memory_total).value}</span>
                      <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.memory_total).unit}</span>
                    </div>
                    <Progress 
                      value={resources.memory_usage_percent} 
                      className="h-2"
                    />
                    <p className={`text-sm font-medium text-center ${getUsageColor(resources.memory_usage_percent)}`}>
                      {resources.memory_usage_percent.toFixed(1)}% used
                    </p>
                  </div>
                </CardContent>
              </Card>

              {/* Storage */}
              <Card>
                <CardHeader className="pb-3">
                  <CardTitle className="text-sm font-medium flex items-center gap-2">
                    <HardDrive className="h-4 w-4 text-green-500" />
                    {storage.headline}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {/* note: the headline is what this member itself holds. A cluster-wide pool
                      (ceph) is seen from every member, and adding it to each one made a 1.7 TB
                      member read as 12.4 TB; it is reported once, on its own line. */}
                  <div className="space-y-2" data-partial={resources.storage_partial ? "true" : undefined}>
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
                    <p className={`text-sm font-medium text-center ${getUsageColor(storage.percent)}`}>
                      {storage.percent.toFixed(1)}% used{resources.storage_partial ? " (incomplete)" : ""}
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
                    <Layers className="h-4 w-4 text-orange-500" />
                    Instances
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2" data-partial={resources.instances_partial ? "true" : undefined}>
                    {resources.instances_partial ? (
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
                      {!resources.instances_partial && resources.instances_running > 0 && (
                        <TableLink
                          onClick={() => handleShowInstances("running")}
                          aria-label={`Show the running instances on ${memberName}`}
                        >
                          {`${resources.instances_running} Running`}
                        </TableLink>
                      )}
                      {!resources.instances_partial && resources.instances_count - resources.instances_running > 0 && (
                        <TableLink
                          onClick={() => handleShowInstances("stopped")}
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
                <GpuPanel src={resources} dense allocationUnknown={Boolean(resources.instances_partial)} />
              </CardContent>
            </Card>

            {/* Storage Pools */}
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
                            <p className={`text-xs ${getUsageColor(usagePercent)}`}>
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
                    <p className="text-muted-foreground mb-1">{cpu.over > 0 ? "CPU over-commit" : "Unallocated CPU"}</p>
                    <p className="font-medium">
                      {resources.instances_partial
                        ? "unknown"
                        : cpu.over > 0
                          ? `${cpu.over} vCPU beyond capacity (${cpu.ratioLabel})`
                          : `${cpu.unallocated} threads`}
                    </p>
                  </div>
                  <div>
                    <p className="text-muted-foreground mb-1">Available Memory</p>
                    <p className="font-medium">{formatBytes(resources.memory_available)}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground mb-1">{storage.summaryLabel}</p>
                    <p className="font-medium">{formatBytes(storage.total - storage.used)}</p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        ) : (
          // note: the API's reason is the cluster's own, such as "Member 'mc3' is Offline: No
          // heartbeat for 27s". A generic line here sent the operator looking for a dashboard
          // fault when the member was simply powered off.
          <div className="text-center py-12" role="status">
            <Server className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold mb-2">
              {error ? "This member could not be read" : "No resource data"}
            </h3>
            <p className="text-muted-foreground">
              {error || "Unable to fetch resource information for this member"}
            </p>
          </div>
        )}
        </DialogBody>
      </DialogContent>

      {/* Instance Details Modal */}
      <Dialog open={showInstanceModal} onOpenChange={setShowInstanceModal}>
        <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
          <DialogHeader className="pb-4">
            <DialogTitle>
              {instanceFilter === "running" ? "Running" : "Stopped"} Instances on {memberName}
            </DialogTitle>
            <DialogDescription>
              {`${instances.length} ${instanceFilter} instance${instances.length === 1 ? "" : "s"} visible to you`}
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="pr-2">
          {loadingInstances ? (
            <div className="flex items-center justify-center py-12">
              <Activity className="h-8 w-8 animate-spin text-muted-foreground" />
            </div>
          ) : instances.length > 0 ? (
            <div className="space-y-3">
              {instances.map((instance, index) => (
                <Card key={index}>
                  <CardContent className="pt-6">
                    <div className="flex items-start justify-between">
                      <div className="space-y-1 flex-1">
                        <div className="flex items-center gap-2">
                          <h4 className="font-semibold">{instance.name}</h4>
                          <Badge variant={instance.status === "Running" ? "default" : "secondary"} className="text-xs">
                            {instance.status}
                          </Badge>
                          <span className="text-xs text-muted-foreground">
                            {formatInstanceType(instance.type)}
                          </span>
                        </div>
                        <div className="text-sm text-muted-foreground space-y-1">
                          {instance.ip_addresses && instance.ip_addresses.length > 0 && (
                            <div className="flex items-center gap-2">
                              <span className="font-medium">IP:</span>
                              <span className="font-mono text-xs">{instance.ip_addresses.join(", ")}</span>
                            </div>
                          )}
                          <div className="flex items-center gap-4">
                            <span>
                              <span className="font-medium">Type:</span> {formatInstanceType(instance.type)}
                            </span>
                            <span>
                              <span className="font-medium">Project:</span> {instance.project || "default"}
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          ) : instancesError ? (
            <EmptyState
              icon={Layers}
              title="Could not load instances"
              description={instancesError}
            />
          ) : (
            <EmptyState
              icon={Layers}
              title={`No ${instanceFilter} instances visible to you`}
              description={
                `The member reports ${
                  instanceFilter === "running"
                    ? resources?.instances_running ?? 0
                    : (resources?.instances_count || 0) - (resources?.instances_running || 0)
                } ${instanceFilter}. Any not listed here belong to an organization you cannot see.`
              }
            />
          )}
          </DialogBody>
        </DialogContent>
      </Dialog>
    </Dialog>
  );
}
