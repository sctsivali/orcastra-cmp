"use client";

import { useState, useEffect } from "react";
import { X, Activity, Cpu, HardDrive, Network, Clock } from "lucide-react";
import { LoadingLottieMedium } from "./loading-lottie";
import { useInstanceMetrics } from "@/hooks";
import { VMInfo } from "@/types";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "./ui/dialog";
import { Badge } from "./ui/badge";
import { Card } from "./ui/card";

interface InstanceMonitoringModalProps {
  instance: VMInfo | null;
  isOpen: boolean;
  onClose: () => void;
  project?: string;
}

export function InstanceMonitoringModal({
  instance,
  isOpen,
  onClose,
  project = "default",
}: InstanceMonitoringModalProps) {
  const { data: metrics, isLoading } = useInstanceMetrics(
    instance?.host || "",
    instance?.name || "",
    project,
    isOpen && !!instance
  );

  if (!instance) return null;

  // Format bytes to human readable
  const formatBytes = (bytes: number): string => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`;
  };

  // Format uptime
  const formatUptime = (seconds: number): string => {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    
    if (days > 0) return `${days}d ${hours}h ${minutes}m`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[85dvh] max-h-[90vh] overflow-y-auto overscroll-contain pt-10 sm:pt-6">
        <DialogHeader>
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-3 min-w-0">
              <Activity className="h-5 w-5 sm:h-6 sm:w-6 text-blue-500 shrink-0" />
              <div className="min-w-0">
                <DialogTitle className="text-lg sm:text-2xl truncate">{instance.name}</DialogTitle>
                <DialogDescription className="truncate">
                  Real-time monitoring • {instance.host}
                </DialogDescription>
              </div>
            </div>
            <Badge
              variant={instance.status === "Running" ? "default" : "secondary"}
              className="shrink-0"
            >
              {instance.status}
            </Badge>
          </div>
        </DialogHeader>

        <div className="space-y-6 mt-4">
          {/* Instance Info */}
          <Card className="p-4 bg-muted/50">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div>
                <p className="text-muted-foreground">Type</p>
                <p className="font-medium">
                  {instance.type === "virtual-machine" ? "VM" : "Container"}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground">Architecture</p>
                <p className="font-medium">{instance.architecture || "N/A"}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Processes</p>
                <p className="font-medium">{metrics?.processes || 0}</p>
              </div>
              <div>
                <p className="text-muted-foreground">
                  <Clock className="inline h-3 w-3 mr-1" />
                  Uptime
                </p>
                <p className="font-medium">
                  {metrics?.uptime_seconds
                    ? formatUptime(metrics.uptime_seconds)
                    : "N/A"}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground">PID</p>
                <p className="font-medium">{metrics?.pid || "N/A"}</p>
              </div>
              <div className="col-span-3">
                <p className="text-muted-foreground">Base Image</p>
                <p className="font-medium text-xs truncate">
                  {metrics?.base_image || "N/A"}
                </p>
              </div>
            </div>
          </Card>

          {isLoading ? (
            <div className="flex justify-center py-8">
              <LoadingLottieMedium />
            </div>
          ) : instance.status !== "Running" ? (
            <div className="text-center py-8 text-muted-foreground">
              Instance is not running. Start the instance to see metrics.
            </div>
          ) : metrics ? (
            <>
              {/* CPU Usage */}
              <Card className="p-4">
                <div className="flex items-center gap-2 mb-4">
                  <Cpu className="h-5 w-5 text-blue-500" />
                  <h3 className="font-semibold text-lg">CPU</h3>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center gap-3">
                    <div className="text-3xl font-bold">
                      {metrics.cpu_usage_percent?.toFixed(1) ?? '0.0'}%
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {metrics.cpu_usage_percent === 0 ? 'calculating...' : 'across all cores'}
                    </div>
                  </div>
                </div>
              </Card>

              {/* Memory Usage */}
              <Card className="p-4">
                <div className="flex items-center gap-2 mb-4">
                  <Activity className="h-5 w-5 text-green-500" />
                  <h3 className="font-semibold text-lg">Memory Usage</h3>
                </div>
                <div className="space-y-2">
                  <div className="flex justify-between text-sm">
                    <span>Used / Total</span>
                    <span className="font-medium">
                      {formatBytes(metrics.memory_used_bytes)} /{" "}
                      {formatBytes(metrics.memory_total_bytes)}
                    </span>
                  </div>
                  <div className="w-full bg-muted rounded-full h-4 overflow-hidden">
                    <div
                      className="bg-green-500 h-full transition-all duration-300 rounded-full"
                      style={{
                        width: `${Math.min(metrics.memory_usage_percent, 100)}%`,
                      }}
                    />
                  </div>
                  <div className="text-right text-sm font-medium">
                    {metrics.memory_usage_percent.toFixed(2)}%
                  </div>
                </div>
              </Card>

              {/* Disk Usage */}
              {Object.keys(metrics.disk_usage_bytes).length > 0 && (
                <Card className="p-4">
                  <div className="flex items-center gap-2 mb-4">
                    <HardDrive className="h-5 w-5 text-orange-500" />
                    <h3 className="font-semibold text-lg">Disk Usage</h3>
                  </div>
                  <div className="space-y-3">
                    {Object.entries(metrics.disk_usage_bytes).map(
                      ([device, bytes]) => (
                        <div key={device} className="space-y-1">
                          <div className="flex justify-between text-sm">
                            <span className="text-muted-foreground">
                              {device}
                            </span>
                            <span className="font-medium">
                              {formatBytes(bytes)}
                            </span>
                          </div>
                        </div>
                      )
                    )}
                  </div>
                </Card>
              )}

              {/* Network Usage */}
              {(Object.keys(metrics.network_rx_bytes).length > 0 ||
                Object.keys(metrics.network_tx_bytes).length > 0) && (
                <Card className="p-4">
                  <div className="flex items-center gap-2 mb-4">
                    <Network className="h-5 w-5 text-purple-500" />
                    <h3 className="font-semibold text-lg">Network Usage</h3>
                  </div>
                  <div className="space-y-4">
                    {Object.keys(metrics.network_rx_bytes).map((iface) => (
                      <div key={iface} className="space-y-2">
                        <div className="font-medium text-sm border-b pb-1">
                          {iface}
                        </div>
                        <div className="grid grid-cols-2 gap-4 text-sm">
                          <div>
                            <p className="text-muted-foreground">Received</p>
                            <p className="font-medium text-green-600">
                              ↓ {formatBytes(metrics.network_rx_bytes[iface])}
                            </p>
                          </div>
                          <div>
                            <p className="text-muted-foreground">Transmitted</p>
                            <p className="font-medium text-blue-600">
                              ↑ {formatBytes(metrics.network_tx_bytes[iface])}
                            </p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </Card>
              )}

              {/* IP Addresses */}
              {(metrics?.ipv4_addresses.length > 0 || metrics?.ipv6_addresses.length > 0) && (
                <Card className="p-4">
                  <div className="mb-4">
                    <h3 className="font-semibold text-lg">Network Addresses</h3>
                  </div>
                  <div className="space-y-3">
                    {metrics?.ipv4_addresses.length > 0 && (
                      <div>
                        <p className="text-sm text-muted-foreground mb-2">IPv4</p>
                        <div className="flex flex-wrap gap-2">
                          {metrics.ipv4_addresses.map((ip) => (
                            <Badge key={ip} variant="outline" className="font-mono">
                              {ip}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    )}
                    {metrics?.ipv6_addresses.length > 0 && (
                      <div>
                        <p className="text-sm text-muted-foreground mb-2">IPv6</p>
                        <div className="flex flex-wrap gap-2">
                          {metrics.ipv6_addresses.map((ip) => (
                            <Badge key={ip} variant="outline" className="font-mono text-xs">
                              {ip}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </Card>
              )}
            </>
          ) : (
            <div className="text-center py-8 text-muted-foreground">
              No metrics available
            </div>
          )}
        </div>

        <div className="mt-6 flex justify-end">
          <Button onClick={onClose} variant="outline">
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
