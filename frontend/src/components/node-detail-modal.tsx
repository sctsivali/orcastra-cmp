"use client";

import {
  Cpu,
  MemoryStick,
  Sparkles,
  HardDrive,
  Network,
  Info,
  X,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Badge } from "./ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { EmptyState } from "./ui/empty-state";
import { GpuPanel } from "./gpu";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";
import { NodeStatus } from "@/types";
import { cn, formatLabel } from "@/lib/utils";

interface NodeDetailModalProps {
  node: NodeStatus | null;
  isOpen: boolean;
  onClose: () => void;
}

export function NodeDetailModal({
  node,
  isOpen,
  onClose,
}: NodeDetailModalProps) {
  if (!node) return null;

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`;
  };

  const getUsagePercentage = (used: number, total: number) => {
    if (total === 0) return 0;
    return ((used / total) * 100).toFixed(1);
  };

  // Determine if node is truly connected (authenticated) or just pending trust
  const isAuthenticated = node.connected && node.server_version;
  const isPendingTrust = !node.connected && !node.error;

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-5xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <div className="flex-1">
            <DialogTitle className="text-2xl flex items-center gap-2">
              {node.display_name || node.name}
              {isPendingTrust && (
                <Badge variant="secondary" className="text-xs">
                  Pending Trust
                </Badge>
              )}
            </DialogTitle>
            <DialogDescription className="font-mono">
              {node.endpoint}
            </DialogDescription>
          </div>
        </DialogHeader>

        <Tabs defaultValue="system" className="w-full">
          <TabsList className="grid w-full grid-cols-5">
            <TabsTrigger value="system">System</TabsTrigger>
            <TabsTrigger value="cpu">CPU</TabsTrigger>
            <TabsTrigger value="gpu">GPU</TabsTrigger>
            <TabsTrigger value="memory">Memory</TabsTrigger>
            <TabsTrigger value="storage">Storage</TabsTrigger>
          </TabsList>

          {/* System Tab */}
          <TabsContent value="system" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Info className="h-5 w-5" />
                  System Information
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Server Version
                    </p>
                    <p className="text-lg font-semibold">
                      {node.server_version || "N/A"}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      API Version
                    </p>
                    <p className="text-lg font-semibold">
                      {node.api_version || "N/A"}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Authentication
                    </p>
                    <p className="text-lg font-semibold">
                      {node.auth || "N/A"}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Server Name
                    </p>
                    <p className="text-lg font-semibold">
                      {String(node.environment?.server_name || "N/A")}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      OS
                    </p>
                    <p className="text-lg font-semibold">
                      {String(node.environment?.os_name || "N/A")}{" "}
                      {String(node.environment?.os_version || "")}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Kernel
                    </p>
                    <p className="text-lg font-semibold">
                      {String(node.environment?.kernel_version || "N/A")}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Architecture
                    </p>
                    <p className="text-lg font-semibold">
                      {String(node.environment?.kernel_architecture || "N/A")}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Clustered
                    </p>
                    <p className="text-lg font-semibold">
                      {node.environment?.server_clustered ? "Yes" : "No"}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Storage Backend
                    </p>
                    <p className="text-lg font-semibold">
                      {String(node.environment?.storage || "N/A")}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-muted-foreground">
                      Firewall
                    </p>
                    <p className="text-lg font-semibold">
                      {String(node.environment?.firewall || "N/A")}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          {/* CPU Tab */}
          <TabsContent value="cpu" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Cpu className="h-5 w-5" />
                  CPU Information
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {node.resources ? (
                  <>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Architecture
                        </p>
                        <p className="text-lg font-semibold">
                          {String(node.environment?.kernel_architecture || "N/A")}
                        </p>
                      </div>
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Total Cores
                        </p>
                        <p className="text-lg font-semibold">
                          {node.resources.cpu_total}
                        </p>
                      </div>
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          CPU Sockets
                        </p>
                        <p className="text-lg font-semibold">
                          {node.resources.cpu_sockets}
                        </p>
                      </div>
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Cores per Socket
                        </p>
                        <p className="text-lg font-semibold">
                          {node.resources.cpu_sockets > 0
                            ? Math.floor(
                                node.resources.cpu_total /
                                  node.resources.cpu_sockets
                              )
                            : "N/A"}
                        </p>
                      </div>
                    </div>

                    <div className="pt-4 border-t">
                      <h4 className="text-sm font-semibold mb-3">
                        Resource Usage
                      </h4>
                      <div className="space-y-3">
                        <div>
                          <div className="flex justify-between text-sm mb-1">
                            <span className="text-muted-foreground">Used</span>
                            <span className="font-semibold">
                              {node.resources.cpu_used} /{" "}
                              {node.resources.cpu_total} cores (
                              {getUsagePercentage(
                                node.resources.cpu_used,
                                node.resources.cpu_total
                              )}
                              %)
                            </span>
                          </div>
                          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
                            <div
                              className="bg-blue-600 h-2.5 rounded-full"
                              style={{
                                width: `${getUsagePercentage(
                                  node.resources.cpu_used,
                                  node.resources.cpu_total
                                )}%`,
                              }}
                            ></div>
                          </div>
                        </div>
                        <div>
                          <div className="flex justify-between text-sm mb-1">
                            <span className="text-muted-foreground">
                              Available
                            </span>
                            <span className="font-semibold">
                              {node.resources.cpu_available} cores
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>
                  </>
                ) : (
                  <p className="text-muted-foreground">
                    No CPU information available
                  </p>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {/* GPU Tab */}
          <TabsContent value="gpu" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Sparkles className="h-5 w-5" />
                  GPU
                </CardTitle>
                <CardDescription>{GPU_UTILIZATION_NOTE}</CardDescription>
              </CardHeader>
              <CardContent>
                {node.resources ? (
                  <GpuPanel src={node.resources} />
                ) : (
                  <EmptyState
                    icon={Sparkles}
                    title="Resources unavailable"
                    description={`${node.display_name || node.name} did not report its hardware, so its GPUs cannot be listed. This does not mean it has none.`}
                  />
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {/* Memory Tab */}
          <TabsContent value="memory" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <MemoryStick className="h-5 w-5" />
                  Memory Information
                </CardTitle>
              </CardHeader>
              <CardContent>
                {node.resources ? (
                  <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Total Memory
                        </p>
                        <p className="text-lg font-semibold">
                          {formatBytes(node.resources.memory_total)}
                        </p>
                      </div>
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Available Memory
                        </p>
                        <p className="text-lg font-semibold">
                          {formatBytes(node.resources.memory_available)}
                        </p>
                      </div>
                    </div>

                    <div className="pt-4 border-t">
                      <h4 className="text-sm font-semibold mb-3">
                        Resource Usage
                      </h4>
                      <div className="space-y-3">
                        <div>
                          <div className="flex justify-between text-sm mb-1">
                            <span className="text-muted-foreground">Used</span>
                            <span className="font-semibold">
                              {formatBytes(node.resources.memory_used)} /{" "}
                              {formatBytes(node.resources.memory_total)} (
                              {getUsagePercentage(
                                node.resources.memory_used,
                                node.resources.memory_total
                              )}
                              %)
                            </span>
                          </div>
                          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
                            <div
                              className="bg-green-600 h-2.5 rounded-full"
                              style={{
                                width: `${getUsagePercentage(
                                  node.resources.memory_used,
                                  node.resources.memory_total
                                )}%`,
                              }}
                            ></div>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                ) : (
                  <p className="text-muted-foreground">
                    No memory information available
                  </p>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {/* Storage Tab */}
          <TabsContent value="storage" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <HardDrive className="h-5 w-5" />
                  Storage Information
                </CardTitle>
              </CardHeader>
              <CardContent>
                {node.resources && node.resources.storage_pools.length > 0 ? (
                  <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-4 pb-4 border-b">
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Total Storage
                        </p>
                        <p className="text-lg font-semibold">
                          {formatBytes(node.resources.storage_total)}
                        </p>
                      </div>
                      <div>
                        <p className="text-sm font-medium text-muted-foreground">
                          Available Storage
                        </p>
                        <p className="text-lg font-semibold">
                          {formatBytes(node.resources.storage_available)}
                        </p>
                      </div>
                    </div>

                    <div>
                      <h4 className="text-sm font-semibold mb-3">
                        Storage Pools
                      </h4>
                      <div className="space-y-4">
                        {node.resources.storage_pools.map((pool, index) => (
                          <div
                            key={index}
                            className="border rounded-lg p-4 space-y-3"
                          >
                            <div className="flex justify-between items-center">
                              <div>
                                <p className="font-semibold">{pool.name}</p>
                                <p className="text-sm text-muted-foreground">
                                  Driver: {formatLabel(pool.driver)}
                                </p>
                              </div>
                            </div>

                            <div className="grid grid-cols-3 gap-2 text-sm">
                              <div>
                                <p className="text-muted-foreground">Total</p>
                                <p className="font-semibold">
                                  {formatBytes(pool.space_total)}
                                </p>
                              </div>
                              <div>
                                <p className="text-muted-foreground">Used</p>
                                <p className="font-semibold">
                                  {formatBytes(pool.space_used)}
                                </p>
                              </div>
                              <div>
                                <p className="text-muted-foreground">
                                  Available
                                </p>
                                <p className="font-semibold">
                                  {formatBytes(pool.space_available)}
                                </p>
                              </div>
                            </div>

                            <div>
                              <div className="flex justify-between text-xs mb-1">
                                <span className="text-muted-foreground">
                                  Usage
                                </span>
                                <span className="font-semibold">
                                  {getUsagePercentage(
                                    pool.space_used,
                                    pool.space_total
                                  )}
                                  %
                                </span>
                              </div>
                              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                                <div
                                  className="bg-orange-600 h-2 rounded-full"
                                  style={{
                                    width: `${getUsagePercentage(
                                      pool.space_used,
                                      pool.space_total
                                    )}%`,
                                  }}
                                ></div>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <p className="text-muted-foreground">
                    No storage information available
                  </p>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
