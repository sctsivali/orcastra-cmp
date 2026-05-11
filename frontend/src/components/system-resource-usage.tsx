"use client";

import { useState, useEffect, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REALTIME_DATA_CONFIG } from "@/lib/query-config";
import {
  SystemResourceUsage,
  ContainerInfo,
  CPUInfo,
  MemoryInfo,
  DiskInfo,
  NetworkInterfaceInfo,
  ProcessInfo,
  OSInfo,
  PythonInfo,
} from "@/types";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Cpu,
  MemoryStick,
  Activity,
  Clock,
  RefreshCw,
  Gauge,
  ArrowUpFromLine,
  ArrowDownToLine,
  Wifi,
  WifiOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// Helper function to format bytes
function formatBytes(bytes: number, decimals: number = 2): string {
  if (bytes === 0) return "0 Bytes";
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["Bytes", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
}

// Helper function to format uptime
function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  
  const parts = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  
  return parts.length > 0 ? parts.join(" ") : "< 1m";
}

// Progress bar with color based on percentage
function UsageProgress({
  value,
  className,
}: {
  value: number;
  className?: string;
}) {
  const getColor = (val: number) => {
    if (val < 50) return "bg-green-500";
    if (val < 75) return "bg-yellow-500";
    if (val < 90) return "bg-orange-500";
    return "bg-red-500";
  };

  return (
    <div className={cn("relative h-3 w-full overflow-hidden rounded-full bg-secondary", className)}>
      <div
        className={cn("h-full transition-all duration-300", getColor(value))}
        style={{ width: `${Math.min(value, 100)}%` }}
      />
    </div>
  );
}

// Stat card component
function StatCard({
  icon: Icon,
  title,
  value,
  subtitle,
  trend,
  children,
  className,
}: {
  icon: React.ElementType;
  title: string;
  value: string | number;
  subtitle?: string;
  trend?: "up" | "down" | "neutral";
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("transition-all duration-200 hover:shadow-md", className)}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <Icon className="h-4 w-4" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        {subtitle && (
          <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>
        )}
        {children}
      </CardContent>
    </Card>
  );
}

// CPU Section
function CPUSection({ cpu }: { cpu: CPUInfo }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          CPU Usage
        </CardTitle>
        <CardDescription>
          {cpu.count_physical} physical cores, {cpu.count_logical} logical processors
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>Overall Usage</span>
            <span className="font-medium">{cpu.percent.toFixed(1)}%</span>
          </div>
          <UsageProgress value={cpu.percent} />
        </div>
        
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-2">
          {cpu.frequency_current && (
            <div className="text-center p-2 bg-muted/50 rounded-lg">
              <Gauge className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
              <p className="text-xs text-muted-foreground">Frequency</p>
              <p className="text-sm font-medium">{cpu.frequency_current.toFixed(0)} MHz</p>
            </div>
          )}
          
          {cpu.load_avg_1m !== null && (
            <>
              <div className="text-center p-2 bg-muted/50 rounded-lg">
                <Activity className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
                <p className="text-xs text-muted-foreground">Load 1m</p>
                <p className="text-sm font-medium">{cpu.load_avg_1m.toFixed(2)}</p>
              </div>
              <div className="text-center p-2 bg-muted/50 rounded-lg">
                <Activity className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
                <p className="text-xs text-muted-foreground">Load 5m</p>
                <p className="text-sm font-medium">{cpu.load_avg_5m?.toFixed(2)}</p>
              </div>
              <div className="text-center p-2 bg-muted/50 rounded-lg">
                <Activity className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
                <p className="text-xs text-muted-foreground">Load 15m</p>
                <p className="text-sm font-medium">{cpu.load_avg_15m?.toFixed(2)}</p>
              </div>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// Memory Section
function MemorySection({ memory }: { memory: MemoryInfo }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Memory Usage
        </CardTitle>
        <CardDescription>
          {formatBytes(memory.used)} of {formatBytes(memory.total)} used
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span>RAM Usage</span>
            <span className="font-medium">{memory.percent.toFixed(1)}%</span>
          </div>
          <UsageProgress value={memory.percent} />
        </div>
        
        {memory.swap_total > 0 && (
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span>Swap Usage</span>
              <span className="font-medium">{memory.swap_percent.toFixed(1)}%</span>
            </div>
            <UsageProgress value={memory.swap_percent} />
            <p className="text-xs text-muted-foreground">
              {formatBytes(memory.swap_used)} of {formatBytes(memory.swap_total)}
            </p>
          </div>
        )}
        
        <div className="grid grid-cols-3 gap-4 pt-2">
          <div className="text-center p-2 bg-muted/50 rounded-lg">
            <p className="text-xs text-muted-foreground">Total</p>
            <p className="text-sm font-medium">{formatBytes(memory.total)}</p>
          </div>
          <div className="text-center p-2 bg-muted/50 rounded-lg">
            <p className="text-xs text-muted-foreground">Used</p>
            <p className="text-sm font-medium">{formatBytes(memory.used)}</p>
          </div>
          <div className="text-center p-2 bg-muted/50 rounded-lg">
            <p className="text-xs text-muted-foreground">Available</p>
            <p className="text-sm font-medium">{formatBytes(memory.available)}</p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// Disk Section
function DiskSection({ disks }: { disks: DiskInfo[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Storage
        </CardTitle>
        <CardDescription>
          {disks.length} partition{disks.length !== 1 ? "s" : ""} detected
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {disks.map((disk, index) => (
          <div key={index} className="space-y-2 p-3 bg-muted/30 rounded-lg">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="font-mono text-xs">
                  {disk.fstype}
                </Badge>
                <span className="text-sm font-medium truncate max-w-[200px]" title={disk.mountpoint}>
                  {disk.mountpoint}
                </span>
              </div>
              <span className="text-sm font-medium">{disk.percent.toFixed(1)}%</span>
            </div>
            <UsageProgress value={disk.percent} />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>{formatBytes(disk.used)} used</span>
              <span>{formatBytes(disk.free)} free</span>
              <span>{formatBytes(disk.total)} total</span>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// Network Section
function NetworkSection({ 
  network, 
  previousNetwork 
}: { 
  network: NetworkInterfaceInfo[]; 
  previousNetwork?: NetworkInterfaceInfo[] | null;
}) {
  // Calculate network speed if we have previous data
  const calculateSpeed = (current: number, previous?: number) => {
    if (previous === undefined) return null;
    const diff = current - previous;
    // Assuming 5-second refresh interval
    return diff > 0 ? diff / 5 : 0;
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Network Interfaces
        </CardTitle>
        <CardDescription>
          {network.filter(n => n.is_up).length} of {network.length} interfaces active
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {network
            .filter(iface => iface.bytes_sent > 0 || iface.bytes_recv > 0 || iface.is_up)
            .map((iface, index) => {
              const prevIface = previousNetwork?.find(p => p.name === iface.name);
              const rxSpeed = calculateSpeed(iface.bytes_recv, prevIface?.bytes_recv);
              const txSpeed = calculateSpeed(iface.bytes_sent, prevIface?.bytes_sent);
              
              return (
                <div key={index} className="p-3 bg-muted/30 rounded-lg space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      {iface.is_up ? (
                        <Wifi className="h-4 w-4 text-green-500" />
                      ) : (
                        <WifiOff className="h-4 w-4 text-muted-foreground" />
                      )}
                      <span className="font-medium">{iface.name}</span>
                    </div>
                    <Badge variant={iface.is_up ? "default" : "secondary"}>
                      {iface.is_up ? "UP" : "DOWN"}
                    </Badge>
                  </div>
                  
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div className="flex items-center gap-2">
                      <ArrowDownToLine className="h-4 w-4 text-blue-500" />
                      <div>
                        <p className="text-muted-foreground">Received</p>
                        <p className="font-medium">{formatBytes(iface.bytes_recv)}</p>
                        {rxSpeed !== null && (
                          <p className="text-xs text-blue-500">{formatBytes(rxSpeed)}/s</p>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <ArrowUpFromLine className="h-4 w-4 text-green-500" />
                      <div>
                        <p className="text-muted-foreground">Sent</p>
                        <p className="font-medium">{formatBytes(iface.bytes_sent)}</p>
                        {txSpeed !== null && (
                          <p className="text-xs text-green-500">{formatBytes(txSpeed)}/s</p>
                        )}
                      </div>
                    </div>
                  </div>
                  
                  {iface.addresses.length > 0 && (
                    <div className="flex flex-wrap gap-1 pt-1">
                      {iface.addresses.map((addr, i) => (
                        <Badge key={i} variant="outline" className="font-mono text-xs">
                          {addr}
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
        </div>
      </CardContent>
    </Card>
  );
}

// Process Section (App Process Info)
function ProcessSection({ process }: { process: ProcessInfo }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Application Process
        </CardTitle>
        <CardDescription>
          Backend server process information (PID: {process.pid})
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="text-center p-3 bg-muted/50 rounded-lg">
            <Cpu className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
            <p className="text-xs text-muted-foreground">CPU</p>
            <p className="text-lg font-medium">{process.cpu_percent.toFixed(2)}%</p>
          </div>
          <div className="text-center p-3 bg-muted/50 rounded-lg">
            <MemoryStick className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
            <p className="text-xs text-muted-foreground">Memory</p>
            <p className="text-lg font-medium">{formatBytes(process.memory_rss)}</p>
            <p className="text-xs text-muted-foreground">({process.memory_percent.toFixed(1)}%)</p>
          </div>
          <div className="text-center p-3 bg-muted/50 rounded-lg">
            <ArrowDownToLine className="h-4 w-4 mx-auto mb-1 text-blue-500" />
            <p className="text-xs text-muted-foreground">Read I/O</p>
            <p className="text-lg font-medium">
              {process.io_read_bytes !== null ? formatBytes(process.io_read_bytes) : "N/A"}
            </p>
          </div>
          <div className="text-center p-3 bg-muted/50 rounded-lg">
            <ArrowUpFromLine className="h-4 w-4 mx-auto mb-1 text-green-500" />
            <p className="text-xs text-muted-foreground">Write I/O</p>
            <p className="text-lg font-medium">
              {process.io_write_bytes !== null ? formatBytes(process.io_write_bytes) : "N/A"}
            </p>
          </div>
          <div className="text-center p-3 bg-muted/50 rounded-lg col-span-2 md:col-span-4">
            <Clock className="h-4 w-4 mx-auto mb-1 text-muted-foreground" />
            <p className="text-xs text-muted-foreground">Uptime</p>
            <p className="text-lg font-medium">{formatUptime(process.uptime_seconds)}</p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// System Info Section
function SystemInfoSection({ os, python }: { os: OSInfo; python: PythonInfo }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          System Information
        </CardTitle>
        <CardDescription>
          Operating system and runtime environment
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid gap-4">
          <div className="space-y-2">
            <h4 className="text-sm font-medium">
              Operating System
            </h4>
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div className="p-2 bg-muted/30 rounded">
                <span className="text-muted-foreground">System:</span>{" "}
                <span className="font-medium">{os.system} {os.release}</span>
              </div>
              <div className="p-2 bg-muted/30 rounded">
                <span className="text-muted-foreground">Architecture:</span>{" "}
                <span className="font-medium">{os.machine}</span>
              </div>
              <div className="p-2 bg-muted/30 rounded">
                <span className="text-muted-foreground">Hostname:</span>{" "}
                <span className="font-medium">{os.hostname}</span>
              </div>
              <div className="p-2 bg-muted/30 rounded">
                <span className="text-muted-foreground">System Uptime:</span>{" "}
                <span className="font-medium">{formatUptime(os.uptime_seconds)}</span>
              </div>
            </div>
          </div>
          
          <div className="space-y-2">
            <h4 className="text-sm font-medium">
              Python Runtime
            </h4>
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div className="p-2 bg-muted/30 rounded">
                <span className="text-muted-foreground">Version:</span>{" "}
                <span className="font-medium">{python.version}</span>
              </div>
              <div className="p-2 bg-muted/30 rounded">
                <span className="text-muted-foreground">Implementation:</span>{" "}
                <span className="font-medium">{python.implementation}</span>
              </div>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// Main Component
export function SystemResourceUsagePanel() {
  const [previousNetwork, setPreviousNetwork] = useState<NetworkInterfaceInfo[] | null>(null);
  const [refreshInterval] = useState(5000); // 5 seconds auto-refresh

  const { data, isLoading, isFetching, isError, error, refetch, dataUpdatedAt } = useQuery({
    queryKey: ["systemResourceUsage"],
    queryFn: api.system.getResourceUsage,
    refetchInterval: refreshInterval, // Always auto-refresh every 5 seconds
    refetchOnWindowFocus: true,
    staleTime: 3000,
    gcTime: 60000,
  });

  // Store previous network data for speed calculation
  useEffect(() => {
    if (data?.network) {
      // Delay storing previous data to allow speed calculation
      const timer = setTimeout(() => {
        setPreviousNetwork(data.network);
      }, refreshInterval - 100);
      return () => clearTimeout(timer);
    }
  }, [data?.network, refreshInterval]);

  const handleRefresh = useCallback(() => {
    refetch();
  }, [refetch]);

  // Format time with colons (HH:MM:SS)
  const lastUpdated = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString('en-GB', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
      })
    : null;

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {[...Array(4)].map((_, i) => (
            <Card key={i}>
              <CardHeader>
                <Skeleton className="h-6 w-32" />
                <Skeleton className="h-4 w-48" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-24 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <Card className="border-destructive">
        <CardHeader>
          <CardTitle className="text-destructive">Error Loading System Information</CardTitle>
          <CardDescription>
            {error instanceof Error ? error.message : "Failed to fetch system resource usage"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button onClick={handleRefresh} variant="outline">
            <RefreshCw className="h-4 w-4 mr-2" />
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return null;
  }

  return (
    <div className="space-y-6">
      {/* Header with controls */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h2 className="text-2xl font-bold flex items-center gap-2">
            Dashboard Host Resources
          </h2>
          <p className="text-muted-foreground text-sm mt-1">
            Resource usage of the deployment instance running this dashboard application, not the managed VMs or clusters
          </p>
        </div>
        <div className="flex items-center gap-3">
          {lastUpdated && (
            <span className="text-xs text-muted-foreground">
              Updated: {lastUpdated}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={isFetching}
          >
            <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          </Button>
        </div>
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <CPUSection cpu={data.cpu} />
        <MemorySection memory={data.memory} />
        <DiskSection disks={data.disks} />
        <NetworkSection network={data.network} previousNetwork={previousNetwork} />
      </div>

      {/* Process Info */}
      {data.process && <ProcessSection process={data.process} />}

      {/* System Info */}
      <SystemInfoSection os={data.os} python={data.python} />
    </div>
  );
}

export default SystemResourceUsagePanel;
