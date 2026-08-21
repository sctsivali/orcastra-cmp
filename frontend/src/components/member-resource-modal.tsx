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
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { TableLink } from "@/components/ui/table-link";
import { Progress } from "@/components/ui/progress";
import { Cpu, MemoryStick, HardDrive, Layers, Server, Activity, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import { formatLabel, formatInstanceType } from "@/lib/utils";
import type { VMInfo, GPUCardInfo } from "@/types/api";

interface MemberResources {
  member_name: string;
  cpu_total: number;
  cpu_used: number;
  cpu_available: number;
  cpu_usage_percent: number;
  memory_total: number;
  memory_used: number;
  memory_available: number;
  memory_usage_percent: number;
  gpu_total: number;
  gpu_used: number;
  gpu_available: number;
  gpu_cards: GPUCardInfo[];
  storage_total: number;
  storage_used: number;
  storage_available: number;
  storage_usage_percent: number;
  storage_pools: Array<{
    name: string;
    driver: string;
    space_total: number;
    space_used: number;
    space_available: number;
  }>;
  instances_count: number;
  instances_running: number;
}

interface MemberResourceModalProps {
  isOpen: boolean;
  onClose: () => void;
  memberName: string | null;
  nodeName?: string | null;
  resources: MemberResources | null;
  isLoading: boolean;
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
}: MemberResourceModalProps) {
  const [showInstanceModal, setShowInstanceModal] = React.useState(false);
  const [instanceFilter, setInstanceFilter] = React.useState<"running" | "stopped" | null>(null);
  const [instances, setInstances] = React.useState<VMInfo[]>([]);
  const [loadingInstances, setLoadingInstances] = React.useState(false);

  const [instancesError, setInstancesError] = React.useState<string | null>(null);

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
        <DialogHeader>
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
            {/* Overview Cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {/* CPU */}
              <Card>
                <CardHeader className="pb-3">
                  <CardTitle className="text-sm font-medium flex items-center gap-2">
                    <Cpu className="h-4 w-4 text-blue-500" />
                    CPU
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    <div className="flex items-baseline justify-center gap-1">
                      <span className="text-2xl font-bold">{resources.cpu_used}</span>
                      <span className="text-sm text-muted-foreground">/ {resources.cpu_total} cores</span>
                    </div>
                    <Progress 
                      value={resources.cpu_usage_percent} 
                      className="h-2"
                    />
                    <p className={`text-sm font-medium text-center ${getUsageColor(resources.cpu_usage_percent)}`}>
                      {resources.cpu_usage_percent.toFixed(1)}% used
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
                    Storage
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    <div className="text-center text-sm">
                      <span className="text-2xl font-bold">{formatBytesCompact(resources.storage_used).value}</span>
                      <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.storage_used).unit}</span>
                      <span className="text-xs text-muted-foreground"> / </span>
                      <span className="text-base font-semibold">{formatBytesCompact(resources.storage_total).value}</span>
                      <span className="text-xs text-muted-foreground"> {formatBytesCompact(resources.storage_total).unit}</span>
                    </div>
                    <Progress 
                      value={resources.storage_usage_percent} 
                      className="h-2"
                    />
                    <p className={`text-sm font-medium text-center ${getUsageColor(resources.storage_usage_percent)}`}>
                      {resources.storage_usage_percent.toFixed(1)}% used
                    </p>
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
                  <div className="space-y-2">
                    <div className="flex items-baseline justify-center gap-1">
                      <span className="text-2xl font-bold">{resources.instances_running}</span>
                      <span className="text-sm text-muted-foreground">/ {resources.instances_count} total</span>
                    </div>
                    <div className="flex flex-col items-center justify-center gap-2">
                      {resources.instances_running > 0 && (
                        <TableLink
                          onClick={() => handleShowInstances("running")}
                          aria-label={`Show the running instances on ${memberName}`}
                        >
                          {`${resources.instances_running} Running`}
                        </TableLink>
                      )}
                      {resources.instances_count - resources.instances_running > 0 && (
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

            {/* GPU Information */}
            {resources.gpu_total > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-purple-500" />
                    GPU Resources
                  </CardTitle>
                  <CardDescription>Graphics processing units available</CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="space-y-4">
                    {/* Summary */}
                    <div className="grid grid-cols-3 gap-4">
                      <div>
                        <p className="text-sm text-muted-foreground mb-1">Total GPUs</p>
                        <p className="text-2xl font-bold">{resources.gpu_total}</p>
                      </div>
                      <div>
                        <p className="text-sm text-muted-foreground mb-1">Used</p>
                        <p className="text-2xl font-bold text-orange-500">{resources.gpu_used}</p>
                      </div>
                      <div>
                        <p className="text-sm text-muted-foreground mb-1">Available</p>
                        <p className="text-2xl font-bold text-green-500">{resources.gpu_available}</p>
                      </div>
                    </div>

                    {/* Detailed GPU Cards */}
                    {resources.gpu_cards && resources.gpu_cards.length > 0 && (
                      <div className="pt-4 border-t space-y-3">
                        <p className="text-sm font-medium text-muted-foreground">GPU Cards</p>
                        {resources.gpu_cards.map((gpu, index) => (
                          <div
                            key={index}
                            className={`border rounded-lg p-3 space-y-2 ${
                              gpu.in_use
                                ? "border-purple-500/50 bg-purple-500/5"
                                : "border-border"
                            }`}
                          >
                            {/* GPU Header */}
                            <div className="flex justify-between items-start">
                              <div className="flex-1">
                                <div className="flex items-center gap-2">
                                  <p className="font-semibold">
                                    {gpu.nvidia?.model || gpu.product || `GPU ${gpu.index}`}
                                  </p>
                                  {gpu.in_use ? (
                                    <Badge variant="default" className="bg-purple-600 text-xs">
                                      In Use
                                    </Badge>
                                  ) : (
                                    <Badge variant="secondary" className="text-xs">
                                      Available
                                    </Badge>
                                  )}
                                </div>
                                {gpu.vendor && (
                                  <p className="text-sm text-muted-foreground">{gpu.vendor}</p>
                                )}
                                {gpu.used_by && (
                                  <p className="text-xs text-purple-500 mt-1">
                                    Used by: {gpu.used_by}
                                  </p>
                                )}
                              </div>
                              {gpu.nvidia?.brand && (
                                <Badge variant="outline" className="text-xs">
                                  {gpu.nvidia.brand}
                                </Badge>
                              )}
                            </div>

                            {/* GPU Details */}
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-sm">
                              {gpu.driver && (
                                <div>
                                  <p className="text-muted-foreground text-xs">Driver</p>
                                  <p className="font-medium text-xs">{gpu.driver}</p>
                                </div>
                              )}
                              {gpu.pci_address && (
                                <div>
                                  <p className="text-muted-foreground text-xs">PCI</p>
                                  <p className="font-mono text-xs">{gpu.pci_address}</p>
                                </div>
                              )}
                              {gpu.nvidia?.cuda_version && (
                                <div>
                                  <p className="text-muted-foreground text-xs">CUDA</p>
                                  <p className="font-medium text-xs">{gpu.nvidia.cuda_version}</p>
                                </div>
                              )}
                            </div>

                            {/* NVIDIA UUID */}
                            {gpu.nvidia?.uuid && (
                              <div className="pt-1 border-t border-dashed">
                                <p className="text-muted-foreground text-xs">UUID</p>
                                <p className="font-mono text-xs break-all">{gpu.nvidia.uuid}</p>
                              </div>
                            )}

                            {/* SR-IOV info */}
                            {gpu.sriov && gpu.sriov.maximum_vfs > 0 && (
                              <div className="pt-1 border-t border-dashed">
                                <p className="text-muted-foreground text-xs">
                                  SR-IOV: {gpu.sriov.current_vfs} / {gpu.sriov.maximum_vfs} VFs
                                </p>
                              </div>
                            )}

                            {/* mdev profiles */}
                            {gpu.mdev_profiles && gpu.mdev_profiles.length > 0 && (
                              <div className="pt-1 border-t border-dashed">
                                <p className="text-muted-foreground text-xs mb-1">vGPU Profiles</p>
                                <div className="flex flex-wrap gap-1">
                                  {gpu.mdev_profiles.slice(0, 3).map((profile, pIdx) => (
                                    <Badge key={pIdx} variant="outline" className="text-xs">
                                      {profile.name}
                                    </Badge>
                                  ))}
                                  {gpu.mdev_profiles.length > 3 && (
                                    <Badge variant="outline" className="text-xs">
                                      +{gpu.mdev_profiles.length - 3} more
                                    </Badge>
                                  )}
                                </div>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Storage Pools */}
            {resources.storage_pools && resources.storage_pools.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Storage Pools</CardTitle>
                  <CardDescription>Individual storage pool utilization</CardDescription>
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
                    <p className="text-muted-foreground mb-1">Available CPU</p>
                    <p className="font-medium">{resources.cpu_available} cores</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground mb-1">Available Memory</p>
                    <p className="font-medium">{formatBytes(resources.memory_available)}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground mb-1">Available Storage</p>
                    <p className="font-medium">{formatBytes(resources.storage_available)}</p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        ) : (
          <div className="text-center py-12">
            <Server className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold mb-2">No Resource Data</h3>
            <p className="text-muted-foreground">
              Unable to fetch resource information for this member
            </p>
          </div>
        )}
        </DialogBody>
      </DialogContent>

      {/* Instance Details Modal */}
      <Dialog open={showInstanceModal} onOpenChange={setShowInstanceModal}>
        <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
          <DialogHeader>
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
