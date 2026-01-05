"use client";

import * as React from "react";
import { LoadingLottieMedium } from "./loading-lottie";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Cpu, MemoryStick, HardDrive, Layers, Server, Activity, PlayCircle, StopCircle } from "lucide-react";
import { api } from "@/lib/api";
import type { VMInfo } from "@/types/api";

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

  const handleShowInstances = async (filter: "running" | "stopped") => {
    if (!memberName || !nodeName) return;
    
    setInstanceFilter(filter);
    setLoadingInstances(true);
    setShowInstanceModal(true);
    
    try {
      // Note: LXD instances list doesn't include location field when queried via host
      // The instance count shown in the card is accurate (counted directly from member)
      // This modal shows a message that instance details per member require direct LXD API access
      
      // For now, we show an informative message instead of incorrect data
      setInstances([]);
    } catch (error) {
      console.error("Error loading instances:", error);
      setInstances([]);
    } finally {
      setLoadingInstances(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Server className="h-5 w-5" />
            {memberName || "Member Resources"}
          </DialogTitle>
          <DialogDescription>
            Resource usage and availability for this cluster member
          </DialogDescription>
        </DialogHeader>

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
                        <Badge
                          variant="default"
                          className="text-xs cursor-pointer hover:opacity-80 transition-opacity"
                          onClick={() => handleShowInstances("running")}
                        >
                          {resources.instances_running} Running
                        </Badge>
                      )}
                      {resources.instances_count - resources.instances_running > 0 && (
                        <Badge
                          variant="secondary"
                          className="text-xs cursor-pointer hover:opacity-80 transition-opacity"
                          onClick={() => handleShowInstances("stopped")}
                        >
                          {resources.instances_count - resources.instances_running} Stopped
                        </Badge>
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
                  <CardTitle className="text-base">GPU Resources</CardTitle>
                  <CardDescription>Graphics processing units available</CardDescription>
                </CardHeader>
                <CardContent>
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
                              <Badge variant="outline" className="text-xs">
                                {pool.driver}
                              </Badge>
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
      </DialogContent>

      {/* Instance Details Modal */}
      <Dialog open={showInstanceModal} onOpenChange={setShowInstanceModal}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {instanceFilter === "running" ? (
                <PlayCircle className="h-5 w-5 text-green-500" />
              ) : (
                <StopCircle className="h-5 w-5 text-gray-500" />
              )}
              {instanceFilter === "running" ? "Running" : "Stopped"} Instances on {memberName}
            </DialogTitle>
            <DialogDescription>
              {instances.length} {instanceFilter} instance{instances.length !== 1 ? "s" : ""} found
            </DialogDescription>
          </DialogHeader>

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
                          <Badge variant="outline" className="text-xs">
                            {instance.type === "container" ? "Container" : "VM"}
                          </Badge>
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
                              <span className="font-medium">Type:</span> {instance.type}
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
          ) : (
            <div className="text-center py-12">
              <Layers className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">Instance Details Unavailable</h3>
              <p className="text-muted-foreground mb-2">
                This cluster member has {instanceFilter === "running" ? resources?.instances_running : (resources?.instances_count || 0) - (resources?.instances_running || 0)} {instanceFilter} instance{((instanceFilter === "running" ? resources?.instances_running : (resources?.instances_count || 0) - (resources?.instances_running || 0)) !== 1) ? "s" : ""}
              </p>
              <p className="text-xs text-muted-foreground">
                Detailed instance information per cluster member<br />
                requires direct LXD cluster API access
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </Dialog>
  );
}
