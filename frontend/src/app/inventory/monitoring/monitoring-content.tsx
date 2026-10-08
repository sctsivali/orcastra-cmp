"use client";

import { useState, useEffect, useMemo, useCallback, memo, useTransition, useRef } from "react";
import { cpuAllocation } from "@/lib/cpu-allocation";
import { useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
// note: the alarm cards point at /inventory/warnings with distinct query strings, which
// are distinct router cache keys. They warm on intent rather than on sight.
import { IntentLink as Link } from "@/components/navigation/route-intent";

import { Server, Cpu, HardDrive, MemoryStick, Network, AlertTriangle, Container, Monitor, RefreshCw, TrendingUp, TrendingDown, Minus, Box, Pause, Play, Clock, ArrowUp, ArrowDown, Info, ShieldAlert, Flame, Shield, WifiOff, ZoomIn, ZoomOut, ChevronLeft, ChevronRight, RotateCcw } from "lucide-react";

// Dynamically import Lottie to avoid SSR issues
const Lottie = dynamic(() => import("lottie-react"), { ssr: false });
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend, Brush } from "recharts";

import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { NodeFilterSelect } from "@/components/monitoring/node-filter-select";
import { TableLink } from "@/components/ui/table-link";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tooltip as UITooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { InstanceDetailDialog } from "@/components/instance-detail/instance-detail-dialog";
import { useMonitoringDashboard, useMonitoringHistory, useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { useMonitoringWebSocket } from "@/hooks/use-monitoring-ws";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { cn, formatBytes, formatMemory, formatNetworkRate } from "@/lib/utils";
import { instanceStatusDisplay } from "@/lib/instance-status";
import { InstanceMonitoringSummary, VMInfo } from "@/types";
import type {
  MetricsHistoryPoint,
  MonitoringDashboardResponse,
  SystemWarningsResponse,
} from "@/types/api";
import { api } from "@/lib/api";

type MonitoringDisplaySnapshot = {
  actorScope: string;
  dashboard: MonitoringDashboardResponse | undefined;
  history: MetricsHistoryPoint[] | undefined;
  warnings: unknown;
  systemWarnings: SystemWarningsResponse | undefined;
  dashboardError: Error | null;
  dashboardLoading: boolean;
  historyLoading: boolean;
  updatedAt: number;
};

type MonitoringInstanceRequest = {
  actorScope: string;
  project: string;
  epoch: number;
  controller: AbortController;
};

const toVmInfoFromMonitoringSummary = (instance: InstanceMonitoringSummary): VMInfo => ({
  name: instance.name,
  status: instance.status as VMInfo["status"],
  status_code: instance.status === "Running" ? 103 : 102,
  ip_addresses: [],
  host: instance.host,
  host_endpoint: null,
  project: instance.project || "default",
  architecture: null,
  created_at: null,
  last_used_at: null,
  description: null,
  type: instance.type,
  resources: null,
  config: null,
  profiles: [],
  tags: [],
});

// Metric Card Component
interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
  progress?: number;
  className?: string;
}

const MetricCard = memo(function MetricCard({ 
  title, 
  value, 
  subtitle, 
  trend, 
  trendValue, 
  progress,
  className 
}: MetricCardProps) {
  return (
    <Card className={cn("transition-all hover:shadow-md", className)}>
      <CardContent className="p-6">
        <div className="space-y-2">
          <p className="text-sm font-medium text-muted-foreground">{title}</p>
          <div className="flex items-baseline gap-2">
            <p className="text-2xl sm:text-3xl font-bold tracking-tight">{value}</p>
            {trend && trendValue && (
              <span className={cn(
                "flex items-center text-xs font-medium",
                trend === "up" && "text-green-500",
                trend === "down" && "text-red-500",
                trend === "neutral" && "text-muted-foreground"
              )}>
                {trend === "up" && <TrendingUp className="h-3 w-3 mr-0.5" />}
                {trend === "down" && <TrendingDown className="h-3 w-3 mr-0.5" />}
                {trend === "neutral" && <Minus className="h-3 w-3 mr-0.5" />}
                {trendValue}
              </span>
            )}
          </div>
          {subtitle && <p className="text-xs text-muted-foreground">{subtitle}</p>}
          {progress !== undefined && (
            <Progress value={progress} className="h-2 mt-2" />
          )}
        </div>
      </CardContent>
    </Card>
  );
});

// Chart Statistics Component
interface ChartStatsProps {
  min: number;
  max: number;
  avg: number;
  unit?: string;
  color: string;
}

const ChartStats = memo(function ChartStats({ min, max, avg, unit = "%", color }: ChartStatsProps) {
  return (
    <div className="flex flex-wrap items-center justify-center gap-3 sm:gap-6 mb-3 py-2 px-2 sm:px-4 bg-muted/50 rounded-lg">
      <div className="flex items-center gap-1 sm:gap-2">
        <TrendingDown className="h-3 w-3 sm:h-3.5 sm:w-3.5 text-blue-500 shrink-0" />
        <span className="text-[10px] sm:text-xs text-muted-foreground">Min:</span>
        <span className="text-xs sm:text-sm font-semibold" style={{ color }}>{min.toFixed(2)}{unit}</span>
      </div>
      <div className="flex items-center gap-1 sm:gap-2">
        <Minus className="h-3 w-3 sm:h-3.5 sm:w-3.5 text-gray-500 shrink-0" />
        <span className="text-[10px] sm:text-xs text-muted-foreground">Avg:</span>
        <span className="text-xs sm:text-sm font-semibold" style={{ color }}>{avg.toFixed(2)}{unit}</span>
      </div>
      <div className="flex items-center gap-1 sm:gap-2">
        <TrendingUp className="h-3 w-3 sm:h-3.5 sm:w-3.5 text-red-500 shrink-0" />
        <span className="text-[10px] sm:text-xs text-muted-foreground">Max:</span>
        <span className="text-xs sm:text-sm font-semibold" style={{ color }}>{max.toFixed(2)}{unit}</span>
      </div>
    </div>
  );
});

// Loading Component - lightweight skeleton instead of heavy Lottie animation
function LoadingSkeleton() {
  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-32 rounded-lg" />
        ))}
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-72 rounded-lg" />
        ))}
      </div>
    </div>
  );
}

// Colors for charts
import { GaugeChart } from "@/components/monitoring/gauge-chart";
import { CpuUsageGauge, type CpuUsageReading } from "@/components/monitoring/cpu-usage-gauge";
import { TopConsumersCard, consumerPool } from "@/components/monitoring/top-consumers-card";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { STALE_AFTER_MS } from "@/lib/query-config";
import {
  DEFAULT_HISTORY_WINDOW_SECONDS,
  HISTORY_WINDOWS,
  collectedAtOf,
  toChartPoints,
} from "@/lib/monitoring-history";
import { GpuAllocationGauge } from "@/components/monitoring/gpu-allocation-gauge";
import { NodeCard } from "@/components/monitoring/node-card";
import { inventoryLinks } from "@/lib/inventory-links";
import { severityBucket, warningClusterValue, warningNodeValue } from "@/lib/warning-facets";

const CHART_COLORS = {
  cpu: "#3b82f6",    // blue
  memory: "#10b981", // green
  storage: "#f59e0b", // amber
  storageRead: "#f59e0b", // amber
  storageWrite: "#ef4444", // red
  gpu: "#8b5cf6", // violet
  network: "#8b5cf6", // purple (RX/In)
  networkTx: "#ec4899", // pink (TX/Out)
};

const PIE_COLORS = ["#10b981", "#ef4444", "#6b7280"];

// important: lift recharts tooltips above the chart Legend - the Legend is a sibling div
// rendered after the tooltip wrapper, so without this it paints over a tall tooltip
const CHART_TOOLTIP_STYLE = { zIndex: 50 };

// Custom tooltip for pie chart - moved outside component for performance
const PieChartTooltip = ({ active, payload }: { active?: boolean; payload?: any[] }) => {
  if (active && payload && payload.length) {
    const data = payload[0].payload;
    return (
      <div className="bg-card border rounded-lg shadow-lg p-2">
        <p className="text-sm font-medium">{data.name} : {data.value}</p>
        <p className="text-xs text-muted-foreground">{data.percent.toFixed(1)}% of total</p>
      </div>
    );
  }
  return null;
};

// Custom tooltip for Memory chart - moved outside component for performance
const MemoryChartTooltip = ({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) => {
  if (active && payload && payload.length) {
    const data = payload[0].payload;
    return (
      <div className="bg-card border rounded-lg shadow-lg p-3">
        <p className="text-xs text-muted-foreground mb-1">{label}</p>
        <p className="text-sm font-medium" style={{ color: CHART_COLORS.memory }}>
          Memory Usage : {data.memory.toFixed(1)}%
        </p>
        {data.memoryUsedBytes > 0 && (
          <p className="text-xs text-muted-foreground mt-1">
            Used: {formatMemory(data.memoryUsedBytes)}
          </p>
        )}
      </div>
    );
  }
  return null;
};

// Custom tooltip for Storage chart - moved outside component for performance
const StorageChartTooltip = ({ active, payload, label, totalStorageBytes }: { active?: boolean; payload?: any[]; label?: string; totalStorageBytes?: number }) => {
  if (active && payload && payload.length) {
    const data = payload[0].payload;
    const totalBytes = totalStorageBytes || 0;
    const availableBytes = totalBytes > 0 ? totalBytes - data.storageUsedBytes : 0;
    return (
      <div className="bg-card border rounded-lg shadow-lg p-3">
        <p className="text-xs text-muted-foreground mb-1">{label}</p>
        <p className="text-sm font-medium" style={{ color: CHART_COLORS.storage }}>
          Storage Usage : {data.storage.toFixed(1)}%
        </p>
        {data.storageUsedBytes > 0 && (
          <p className="text-xs text-muted-foreground mt-1">
            Used: {formatBytes(data.storageUsedBytes)}
          </p>
        )}
        {availableBytes > 0 && (
          <p className="text-xs text-muted-foreground">
            Available: {formatBytes(availableBytes)}
          </p>
        )}
        {totalBytes > 0 && (
          <p className="text-xs text-muted-foreground">
            Total: {formatBytes(totalBytes)}
          </p>
        )}
      </div>
    );
  }
  return null;
};

// Custom tooltip for Resource Metrics chart with cluster status for 0% values
// Memoized to prevent unnecessary re-renders
interface ResourceMetricsTooltipProps {
  active?: boolean;
  payload?: any[];
  label?: string;
  metricType: "cpu" | "memory" | "storage";
  nodeStatusMap?: Record<string, { status: string; statusMessage?: string; isOnline: boolean }>;
  nodeNames?: string[];
  selectedNode?: string;
}

const ResourceMetricsTooltip = memo(function ResourceMetricsTooltip({ 
  active, 
  payload, 
  label, 
  metricType, 
  nodeStatusMap = {},
  nodeNames = [],
  selectedNode = "all"
}: ResourceMetricsTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  
  const colors = {
    cpu: CHART_COLORS.cpu,
    memory: CHART_COLORS.memory,
    storage: CHART_COLORS.storage,
  };
  
  const labels = {
    cpu: "CPU Usage",
    memory: "Memory Usage",
    storage: "Storage Usage",
  };

  // important: infer historical connectivity from the data point - a node with no value for
  // any metric at this timestamp was offline/disconnected then (vs a genuine 0% reading)
  const isNodeOffline = (nodeName: string, value: number, dataPoint?: any): boolean => {
    if (value !== undefined && value > 0) return false;
    const otherMetricValue = dataPoint?.[`cpu_${nodeName}`] ?? dataPoint?.[`memory_${nodeName}`] ?? dataPoint?.[`storage_${nodeName}`];
    const nodeHadAnyData = otherMetricValue !== undefined && otherMetricValue > 0;
    return !nodeHadAnyData;
  };

  const getStatusBadge = (nodeName: string, value: number, dataPoint?: any) => {
    if (!isNodeOffline(nodeName, value, dataPoint)) return null;
    return (
      <div className="flex items-center gap-1.5 mt-1">
        <span className="text-xs text-muted-foreground italic">Not connected at this time</span>
      </div>
    );
  };

  // Multi-node view (when "All Nodes" selected)
  if (selectedNode === "all" && payload.length > 1) {
    const dataPoint = payload[0]?.payload;
    // Many registered nodes make this tooltip very tall - switch to a compact 2-column grid
    // and bound the height so it never sprawls over the legend below the chart
    const dense = payload.length > 8;
    return (
      <div className={cn(
        "bg-card border rounded-lg shadow-lg p-3 max-h-[70vh] overflow-hidden",
        dense ? "max-w-md" : "max-w-xs"
      )}>
        <p className="text-xs text-muted-foreground mb-2 font-medium border-b pb-1">{label}</p>
        <div className={cn("grid gap-x-4 gap-y-1", dense ? "grid-cols-2" : "grid-cols-1")}>
          {payload.map((entry: any, idx: number) => {
            const nodeName = entry.dataKey?.replace(`${metricType}_`, '') || 'Unknown';
            const value = Number(entry.value || 0);
            const offline = isNodeOffline(nodeName, value, dataPoint);

            return (
              <div key={idx} className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 min-w-0">
                  <span
                    className="inline-block w-2.5 h-2.5 rounded-[2px] shrink-0"
                    style={{ backgroundColor: entry.color }}
                  />
                  <span className="text-sm truncate">{nodeName}</span>
                </div>
                {offline ? (
                  <span className="text-xs text-muted-foreground italic shrink-0">Offline</span>
                ) : (
                  <span className="text-sm font-semibold shrink-0" style={{ color: entry.color }}>
                    {value.toFixed(1)}%
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    );
  }
  
  // Single node view
  const data = payload[0]?.payload;
  const value = data?.[metricType] ?? 0;
  const nodeName = selectedNode !== "all" ? selectedNode : (nodeNames[0] || "");
  
  return (
    <div className="bg-card border rounded-lg shadow-lg p-3">
      <p className="text-xs text-muted-foreground mb-1">{label}</p>
      <p className="text-sm font-medium" style={{ color: colors[metricType] }}>
        {labels[metricType]} : {value.toFixed(1)}%
      </p>
      {getStatusBadge(nodeName, value, data)}
      {metricType === "memory" && data?.memoryUsedBytes > 0 && (
        <p className="text-xs text-muted-foreground mt-1">
          Used: {formatMemory(data.memoryUsedBytes)}
        </p>
      )}
      {metricType === "storage" && data?.storageUsedBytes > 0 && (
        <p className="text-xs text-muted-foreground mt-1">
          Used: {formatBytes(data.storageUsedBytes)}
        </p>
      )}
    </div>
  );
});

// Downsample data for better performance with large datasets
// Uses LTTB (Largest-Triangle-Three-Buckets) inspired algorithm
function downsampleData<T extends Record<string, any>>(
  data: T[], 
  targetPoints: number, 
): T[] {
  if (data.length <= targetPoints) return data;
  
  const result: T[] = [];
  const bucketSize = (data.length - 2) / (targetPoints - 2);
  
  // Always include first point
  result.push(data[0]);
  
  for (let i = 0; i < targetPoints - 2; i++) {
    const bucketStart = Math.floor((i) * bucketSize) + 1;
    const bucketEnd = Math.min(Math.floor((i + 1) * bucketSize) + 1, data.length - 1);
    
    // Multi-metric peak preservation: select the point with the highest
    // peak across ALL metrics so peaks are preserved regardless of active tab.
    // Uses Math.max to ensure any single metric spike gets captured.
    let maxIdx = bucketStart;
    let maxScore = -Infinity;
    
    for (let j = bucketStart; j < bucketEnd; j++) {
      const d = data[j];
      const score = Math.max(
        Number(d?.cpu) || 0,
        Number(d?.memory) || 0,
        Number(d?.storage) || 0,
        (Number(d?.networkRxRate) || 0) + (Number(d?.networkTxRate) || 0),
      );
      if (score > maxScore) {
        maxScore = score;
        maxIdx = j;
      }
    }
    
    result.push(data[maxIdx]);
  }
  
  // Always include last point
  result.push(data[data.length - 1]);
  
  return result;
}

// Alarm severity colors
const ALARM_COLORS = {
  critical: "#dc2626",
  major: "#ea580c",
  warning: "#eab308",
  info: "#3b82f6",
};

// Node colors for multi-line chart (when "All Nodes" selected)
const NODE_COLORS = [
  "#8b5cf6", // violet
  "#06b6d4", // cyan
  "#10b981", // emerald
  "#f59e0b", // amber
  "#ef4444", // red
  "#ec4899", // pink
  "#6366f1", // indigo
  "#84cc16", // lime
];

// Instance Details Dialog
interface InstanceDetailsDialogProps {
  open: boolean;
  onClose: () => void;
  filterType: "running" | "stopped" | "other" | "container" | "vm" | null;
  instances: VMInfo[];
  isLoading: boolean;
  /** Opens the instance dialog for one row. This list closes first; modals do not stack. */
  onSelectInstance: (instance: VMInfo) => void;
}

function InstanceDetailsDialog({ open, onClose, filterType, instances, isLoading, onSelectInstance }: InstanceDetailsDialogProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);

  // important: this dialog stays mounted between openings, so its page survives a close. The
  // parent reopens it with a different filterType (running, then containers), which is a
  // different list entirely, and without this an operator who was on page 3 lands on page 3 of
  // a list they have not seen. Compared during render rather than in an effect, which would
  // paint the wrong page once before correcting it.
  // note: filterType only. Keying on the row count too would reset the page every time the
  // poll saw an instance start or stop, throwing the operator back to page 1 mid-read. A list
  // that merely shrank is what the clamp below is for.
  const [lastFilterType, setLastFilterType] = useState(filterType);
  if (lastFilterType !== filterType) {
    setLastFilterType(filterType);
    setPage(1);
  }

  // important: the list behind this dialog is live, so instances appear and vanish under the
  // operator. Clamp the page on read rather than correcting it in an effect, or a shrinking
  // list leaves them parked past the end painting nothing.
  const totalPages = Math.max(1, Math.ceil(instances.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedInstances = instances.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const getTitle = () => {
    switch (filterType) {
      case "running": return "Running Instances";
      case "stopped": return "Stopped Instances";
      case "other": return "Other Instances";
      case "container": return "Containers";
      case "vm": return "Virtual Machines";
      default: return "Instances";
    }
  };

  const getStatusBadge = (status: string) => {
    const display = instanceStatusDisplay(status);
    return <Badge variant={display.tone}>{display.label}</Badge>;
  };

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader className="pb-4">
          <DialogTitle>
            {getTitle()}
          </DialogTitle>
          <DialogDescription>
            {instances.length} instance{instances.length !== 1 ? "s" : ""} found
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2 pr-2">
          {isLoading ? (
            <div className="space-y-2">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} className="h-16 w-full" />
              ))}
            </div>
          ) : instances.length > 0 ? (
            pagedInstances.map((instance) => {
              const project = instance.project || "default";
              return (
                <div
                  key={`${instance.host}:${project}:${instance.name}`}
                  className="flex items-center justify-between p-3 rounded-lg border hover:bg-muted/50 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted">
                      {instance.type === "container" ? (
                        <Container className="h-5 w-5 text-muted-foreground" />
                      ) : (
                        <Monitor className="h-5 w-5 text-muted-foreground" />
                      )}
                    </div>
                    <div>
                      {/* The hover tint on this row promised something for as long as it existed
                          and never delivered it. The name is now a real control. */}
                      <p className="font-medium">
                        <TableLink
                          onClick={() => onSelectInstance(instance)}
                          aria-label={`Show details for ${instance.name} on ${instance.host} in project ${project}`}
                        >
                          {instance.name}
                        </TableLink>
                      </p>
                      <p className="text-xs text-muted-foreground">{instance.host} / {project}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {getStatusBadge(instance.status)}
                    <Badge variant="outline" className="text-xs">
                      {instance.type === "virtual-machine" ? "VM" : "CT"}
                    </Badge>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="text-center py-8 text-muted-foreground">
              <Box className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>No instances found</p>
            </div>
          )}
        </div>
        {/* Absent rather than disabled when everything fits on one page. Sits outside the
            scroll area so paging does not require scrolling to the bottom first. */}
        {totalPages > 1 && (
          <div className="mt-4">
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              pageSizeOptions={[...PANEL_PAGE_SIZES]}
              totalItems={instances.length}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function MonitoringContent() {
  // important: this page has no role gate, deliberately: a tenant is entitled to watch the
  // load on the nodes their own instances run on. What it must not carry for them is operator
  // content, which here is the alarm card (every tile links to a partner-gated page) and
  // status_message (the backend exception verbatim, see monitoring.py building it from str(e)).
  const { isPartner } = usePermissions();
  const { scopeKey: actorScope } = useActorQueryScope();
  const queryClient = useQueryClient();
  const [isPaused, setIsPaused] = useState(false);
  const [pausedSnapshot, setPausedSnapshot] = useState<MonitoringDisplaySnapshot | null>(null);
  const [timeRange, setTimeRange] = useState<number>(DEFAULT_HISTORY_WINDOW_SECONDS);
  const [chartTab, setChartTab] = useState<"cpu" | "memory" | "storage" | "network">("cpu");
  
  // Instance dialog state
  const [instanceDialogOpen, setInstanceDialogOpen] = useState(false);
  const [instanceFilterType, setInstanceFilterType] = useState<"running" | "stopped" | "other" | "container" | "vm" | null>(null);
  const [allInstances, setAllInstances] = useState<VMInfo[]>([]);
  const [instancesLoading, setInstancesLoading] = useState(false);
  const [selectedTopConsumer, setSelectedTopConsumer] = useState<InstanceMonitoringSummary | null>(null);
  // A row picked out of the instance list dialog. Kept separate from the top-consumer path
  // because that one holds a monitoring summary and has to convert; this one already has the
  // VMInfo in hand.
  const [selectedListInstance, setSelectedListInstance] = useState<VMInfo | null>(null);
  const [monitoringUiActorScope, setMonitoringUiActorScope] = useState(actorScope);
  const previousActorScopeRef = useRef(actorScope);
  const activeActorScopeRef = useRef(actorScope);
  const instanceRequestEpochRef = useRef(0);
  const activeInstanceRequestRef = useRef<MonitoringInstanceRequest | null>(null);
  activeActorScopeRef.current = actorScope;

  // This route can survive a sign-out/sign-in transition without unmounting. Actor-owned UI
  // state is gated during render, then cleared after the change so prior organization rows or
  // node names cannot flash before the new actor's queries settle.
  const monitoringUiIsCurrent = monitoringUiActorScope === actorScope;
  
  // Sort order for top consumers (true = highest first, false = lowest first)
  
  // Node selection for filtering
  const [selectedNodeState, setSelectedNode] = useState<string>("all");
  const selectedNode = monitoringUiIsCurrent ? selectedNodeState : "all";
  
  // Top N consumers count
  
  // Storage pools sort order
  const [storagePoolsSortDesc, setStoragePoolsSortDesc] = useState(true);
  
  // Visible nodes for multi-line graph (when "all" is selected)
  const [visibleNodesState, setVisibleNodes] = useState<Record<string, boolean>>({});
  const visibleNodes = monitoringUiIsCurrent ? visibleNodesState : {};
  
  // Zoom and pan state for charts
  // brushStart/EndIndex: always explicit numbers (never undefined) so Recharts Brush stays controlled
  const [zoomLevelState, setZoomLevel] = useState(1); // 1 = 100%, 2 = 50% of data (zoomed in), etc.
  const [brushStartIndexState, setBrushStartIndex] = useState(0);
  const [brushEndIndexState, setBrushEndIndex] = useState(0);
  // Whether the user has actively zoomed (enables reset button even for manual brush drags)
  const [isZoomedState, setIsZoomed] = useState(false);
  const zoomLevel = monitoringUiIsCurrent ? zoomLevelState : 1;
  const brushStartIndex = monitoringUiIsCurrent ? brushStartIndexState : 0;
  const brushEndIndex = monitoringUiIsCurrent ? brushEndIndexState : 0;
  const isZoomed = monitoringUiIsCurrent ? isZoomedState : false;

  useEffect(() => {
    if (previousActorScopeRef.current === actorScope) return;
    activeInstanceRequestRef.current?.controller.abort();
    activeInstanceRequestRef.current = null;
    previousActorScopeRef.current = actorScope;
    setMonitoringUiActorScope(actorScope);
    setInstanceDialogOpen(false);
    setInstanceFilterType(null);
    setAllInstances([]);
    setInstancesLoading(false);
    setSelectedTopConsumer(null);
    setSelectedListInstance(null);
    setSelectedNode("all");
    setVisibleNodes({});
    setZoomLevel(1);
    setBrushStartIndex(0);
    setBrushEndIndex(0);
    setIsZoomed(false);
    setIsPaused(false);
    setPausedSnapshot(null);
  }, [actorScope]);

  useEffect(() => () => {
    activeInstanceRequestRef.current?.controller.abort();
    activeInstanceRequestRef.current = null;
  }, []);
  
  // Transition for non-blocking time range switches
  const [isTimeRangePending, startTimeRangeTransition] = useTransition();
  
  // Ref to track display data length for zoom calculations
  // (handlers reference this to compute valid brush indices on the downsampled data array)
  const displayDataLengthRef = useRef(0);
  
  // WebSocket-first: receive push updates from the backend metrics collector.
  // When WS is connected, polling is disabled to save bandwidth.
  const { connected: wsConnected } = useMonitoringWebSocket(!isPaused);

  const { 
    data: liveDashboardData,
    isLoading: liveDashboardLoading,
    error: liveDashboardError,
    refreshNow: refreshDashboard,
    dataUpdatedAt: liveDataUpdatedAt,
  } = useMonitoringDashboard(!isPaused, !wsConnected);
  
  const { 
    data: liveHistoryData,
    isLoading: liveHistoryLoading,
    isFetching: liveHistoryFetching,
    refetch: refetchHistory,
  } = useMonitoringHistory(timeRange, !isPaused);

  // Warnings share the monitoring pause contract. Manual refresh below still bypasses the
  // disabled observer for Partner/Admin, while Tenant never calls the privileged endpoints.
  const {
    data: liveWarningsData,
    refreshNow: refreshWarnings,
  } = useWarnings(undefined, { enabled: !isPaused });
  const {
    data: liveSystemWarnings,
    refreshNow: refreshSystemWarnings,
  } = useSystemWarnings({ enabled: !isPaused });

  const frozenSnapshot =
    isPaused && pausedSnapshot?.actorScope === actorScope ? pausedSnapshot : null;
  const dashboardData = isPaused ? frozenSnapshot?.dashboard : liveDashboardData;
  const historyData = isPaused ? frozenSnapshot?.history : liveHistoryData;
  const warningsData = isPaused ? frozenSnapshot?.warnings : liveWarningsData;
  const systemWarnings = isPaused ? frozenSnapshot?.systemWarnings : liveSystemWarnings;
  const dashboardLoading = isPaused
    ? frozenSnapshot?.dashboardLoading ?? false
    : liveDashboardLoading;
  const dashboardError = isPaused
    ? frozenSnapshot?.dashboardError ?? null
    : liveDashboardError;
  const historyLoading = isPaused
    ? frozenSnapshot?.historyLoading ?? false
    : liveHistoryLoading;
  const historyFetching = isPaused ? false : liveHistoryFetching;
  const liveCollectedAt = collectedAtOf(liveDashboardData) || liveDataUpdatedAt;
  const dataUpdatedAt = isPaused
    ? frozenSnapshot?.updatedAt ?? liveCollectedAt
    : liveCollectedAt;

  const toggleMonitoringPause = useCallback(() => {
    if (isPaused) {
      // note: marked stale, not refetched here. The observers are still disabled at this
      // point; once Resume re-enables them a stale query reads at once instead of waiting out
      // the 30 s cadence, which left a resumed page on the paused snapshot for half a minute.
      void queryClient.invalidateQueries({
        queryKey: ["monitoring"],
        refetchType: "none",
      });
      setIsPaused(false);
      setPausedSnapshot(null);
      return;
    }

    setPausedSnapshot({
      actorScope,
      dashboard: liveDashboardData,
      history: liveHistoryData,
      warnings: liveWarningsData,
      systemWarnings: liveSystemWarnings,
      dashboardError: liveDashboardError,
      dashboardLoading: liveDashboardLoading,
      historyLoading: liveHistoryLoading,
      updatedAt: liveCollectedAt,
    });
    setIsPaused(true);
    void Promise.all([
      queryClient.cancelQueries({
        queryKey: ["monitoring", "dashboard", actorScope],
        exact: true,
      }),
      queryClient.cancelQueries({
        // Cancel the selected request and any staggered range prefetch that already started.
        // Timers that have not fired are cleared when the hook observes enabled=false.
        queryKey: ["monitoring", "history", actorScope],
      }),
      queryClient.cancelQueries({
        queryKey: ["warnings", actorScope, undefined],
        exact: true,
      }),
      queryClient.cancelQueries({
        queryKey: ["systemWarnings", actorScope],
        exact: true,
      }),
    ]);
  }, [
    actorScope,
    isPaused,
    liveDashboardData,
    liveDashboardError,
    liveDashboardLoading,
    liveCollectedAt,
    liveHistoryData,
    liveHistoryLoading,
    liveSystemWarnings,
    liveWarningsData,
    queryClient,
  ]);

  const refreshMonitoring = useCallback(async () => {
    const results = await Promise.allSettled([
      refreshDashboard(),
      refetchHistory({ throwOnError: true }),
      ...(isPartner ? [refreshWarnings(), refreshSystemWarnings()] : []),
    ]);
    if (isPaused && activeActorScopeRef.current === actorScope) {
      const previous =
        pausedSnapshot?.actorScope === actorScope ? pausedSnapshot : null;
      const dashboardState = queryClient.getQueryState<MonitoringDashboardResponse>([
        "monitoring",
        "dashboard",
        actorScope,
      ]);
      setPausedSnapshot({
        actorScope,
        // A paused screen is an operator snapshot, not an all-or-nothing transaction.
        // Keep each last-good section when another endpoint fails, while still surfacing
        // the rejected refresh through RefreshButton below.
        dashboard: dashboardState?.data ?? previous?.dashboard,
        history:
          queryClient.getQueryData<MetricsHistoryPoint[]>([
            "monitoring",
            "history",
            actorScope,
            timeRange,
          ]) ?? previous?.history,
        warnings:
          queryClient.getQueryData([
            "warnings",
            actorScope,
            undefined,
          ]) ?? previous?.warnings,
        systemWarnings:
          queryClient.getQueryData<SystemWarningsResponse>([
            "systemWarnings",
            actorScope,
          ]) ?? previous?.systemWarnings,
        dashboardError: null,
        dashboardLoading: false,
        historyLoading: false,
        updatedAt:
          collectedAtOf(dashboardState?.data) || dashboardState?.dataUpdatedAt || previous?.updatedAt || 0,
      });
    }

    const rejected = results.find(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    );
    if (rejected) {
      throw rejected.reason instanceof Error
        ? rejected.reason
        : new Error("One or more monitoring sections could not be refreshed");
    }
  }, [
    actorScope,
    isPartner,
    isPaused,
    pausedSnapshot,
    queryClient,
    refreshDashboard,
    refreshSystemWarnings,
    refreshWarnings,
    refetchHistory,
    timeRange,
  ]);

  const openTopConsumer = useCallback((instance: InstanceMonitoringSummary) => {
    setMonitoringUiActorScope(actorScope);
    setSelectedTopConsumer(instance);
  }, [actorScope]);

  const actualWarnings = (warningsData as any)?.warnings || [];
  const certWarnings = systemWarnings?.certificate_warnings || [];
  const nodeWarnings = systemWarnings?.node_warnings || [];
  
  // Count warnings by severity (including cert and node warnings)
  // Filter by selectedNode if a specific node is selected
  const warningsCounts = useMemo(() => {
    const counts = { critical: 0, high: 0, warning: 0, info: 0, total: 0, certIssues: 0, inactiveNodes: 0 };

    // important: the same node accessor and the same severity buckets the warnings page
    // filters by, so a tile's number is the count the list it opens will show. The substring
    // match this used to do counted a member's warnings under a cluster the page could not
    // filter to, and the tile said 5 above a list that said "No warning matches".
    const filteredWarnings = selectedNode === "all"
      ? actualWarnings
      : actualWarnings.filter((w: any) => warningNodeValue(w) === selectedNode);

    // LXD warnings
    filteredWarnings.forEach((w: any) => {
      counts.total++;
      const bucket = severityBucket(w.severity);
      if (bucket) counts[bucket]++;
    });

    // Certificate warnings - filter by cluster_id matching selected node
    const filteredCertWarnings = selectedNode === "all"
      ? certWarnings
      : certWarnings.filter((w: any) => (w.cluster_id || "") === selectedNode);
    
    filteredCertWarnings.forEach((w: any) => {
      counts.total++;
      counts.certIssues++;
    });
    
    // Inactive node warnings - filter by node_id matching selected node
    const filteredNodeWarnings = selectedNode === "all"
      ? nodeWarnings
      : nodeWarnings.filter((w: any) => {
          const nodeId = w.node_id || w.cluster_id || "";
          return nodeId === selectedNode;
        });
    
    filteredNodeWarnings.forEach(() => {
      counts.total++;
      counts.inactiveNodes++;
    });
    
    return counts;
  }, [actualWarnings, certWarnings, nodeWarnings, selectedNode]);

  // The offline-nodes tab facets on the row's own value (display name, else id), so the tile
  // link carries that row's value rather than the picker's id.
  const selectedNodeWarning = useMemo(
    () => (selectedNode === "all" ? undefined : nodeWarnings.find((w: any) => (w.node_id || w.cluster_id || "") === selectedNode)),
    [nodeWarnings, selectedNode],
  );

  // The distribution is fleet-wide, so project "*" is an explicit part of the request
  // identity. Always force an authoritative read: the retired VM cache is not patched by the
  // instance event stream and could otherwise survive indefinitely. The identity object, not
  // actor scope alone, also fences a rapid A -> B -> A transition.
  const fetchInstances = async (
    filterType: "running" | "stopped" | "other" | "container" | "vm",
    project?: string,
  ) => {
    const requestActorScope = actorScope;
    const requestProject = project || "*";
    const controller = new AbortController();
    const request: MonitoringInstanceRequest = {
      actorScope: requestActorScope,
      project: requestProject,
      epoch: ++instanceRequestEpochRef.current,
      controller,
    };
    activeInstanceRequestRef.current?.controller.abort();
    activeInstanceRequestRef.current = request;
    setMonitoringUiActorScope(requestActorScope);
    setInstanceFilterType(filterType);
    setInstanceDialogOpen(true);
    setAllInstances([]);
    setInstancesLoading(true);
    try {
      const response = await api.instances.list(
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        {
          fresh: true,
          signal: controller.signal,
          ...(project ? { project } : {}),
        },
      );
      if (
        activeActorScopeRef.current !== requestActorScope ||
        activeInstanceRequestRef.current !== request
      ) return;
      setAllInstances(filterInstanceList(response.vms, filterType));
    } catch (error) {
      if (
        controller.signal.aborted ||
        activeActorScopeRef.current !== requestActorScope ||
        activeInstanceRequestRef.current !== request
      ) return;
      console.error("Failed to fetch instances:", error);
      setAllInstances([]);
    } finally {
      if (
        activeActorScopeRef.current === requestActorScope &&
        activeInstanceRequestRef.current === request
      ) {
        activeInstanceRequestRef.current = null;
        setInstancesLoading(false);
      }
    }
  };

  const closeInstanceList = () => {
    activeInstanceRequestRef.current?.controller.abort();
    activeInstanceRequestRef.current = null;
    setInstancesLoading(false);
    setInstanceDialogOpen(false);
  };

  // Helper to filter instance list by type
  const filterInstanceList = (vms: VMInfo[], filterType: string) => {
    switch (filterType) {
      case "running": return vms.filter(vm => vm.status === "Running");
      case "stopped": return vms.filter(vm => vm.status === "Stopped");
      case "other": return vms.filter(vm => vm.status !== "Running" && vm.status !== "Stopped");
      case "container": return vms.filter(vm => vm.type === "container");
      case "vm": return vms.filter(vm => vm.type === "virtual-machine");
      default: return vms;
    }
  };

  // Get unique node names for dropdown - MUST be before any code using selectedNode
  const nodeNames = useMemo(() => {
    if (!dashboardData?.nodes) return [];
    return dashboardData.nodes.map(n => n.node_name);
  }, [dashboardData?.nodes]);
  
  // Initialize visible nodes when node names change
  useEffect(() => {
    if (nodeNames.length > 0) {
      setVisibleNodes(prev => {
        const newState = { ...prev };
        // Add any new nodes as visible by default
        nodeNames.forEach(name => {
          if (!(name in newState)) {
            newState[name] = true;
          }
        });
        return newState;
      });
    }
  }, [nodeNames]);
  
  // Toggle node visibility in multi-line chart
  const toggleNodeVisibility = (nodeName: string) => {
    setVisibleNodes(prev => ({
      ...prev,
      [nodeName]: !prev[nodeName]
    }));
  };
  
  // Node status map for tooltip - shows cluster status when hovering on 0% metrics
  const nodeStatusMap = useMemo(() => {
    if (!dashboardData?.nodes) return {};
    const map: Record<string, { status: string; statusMessage?: string; isOnline: boolean }> = {};
    dashboardData.nodes.forEach(node => {
      map[node.node_name] = {
        status: node.cluster_status || (node.is_online ? "online" : "offline"),
        statusMessage: node.status_message,
        isOnline: node.is_online,
      };
    });
    return map;
  }, [dashboardData?.nodes]);
  
  // Zoom and pan helper functions
  // NOTE: These use displayDataLengthRef (not chartData.length) because the Brush component
  // operates on displayChartData (downsampled to ~200 points), not the full chartData array.
  const handleZoomIn = useCallback(() => {
    const totalPoints = displayDataLengthRef.current;
    if (totalPoints === 0) return;
    const newZoom = Math.min(zoomLevel * 2, 8);
    const visiblePoints = Math.max(1, Math.ceil(totalPoints / newZoom));
    const midpoint = Math.floor((brushStartIndex + brushEndIndex) / 2);
    const halfVisible = Math.floor(visiblePoints / 2);
    const newStart = Math.max(0, midpoint - halfVisible);
    const newEnd = Math.min(totalPoints - 1, newStart + visiblePoints - 1);
    setZoomLevel(newZoom);
    setBrushStartIndex(newStart);
    setBrushEndIndex(newEnd);
    setIsZoomed(true);
  }, [zoomLevel, brushStartIndex, brushEndIndex]);
  
  const handleZoomOut = useCallback(() => {
    const totalPoints = displayDataLengthRef.current;
    const newZoom = Math.max(zoomLevel / 2, 1);
    if (newZoom <= 1 || totalPoints === 0) {
      // Reset to full view
      setZoomLevel(1);
      setBrushStartIndex(0);
      setBrushEndIndex(Math.max(0, totalPoints - 1));
      setIsZoomed(false);
      return;
    }
    const visiblePoints = Math.max(1, Math.ceil(totalPoints / newZoom));
    const midpoint = Math.floor((brushStartIndex + brushEndIndex) / 2);
    const halfVisible = Math.floor(visiblePoints / 2);
    const newStart = Math.max(0, midpoint - halfVisible);
    const newEnd = Math.min(totalPoints - 1, newStart + visiblePoints - 1);
    setZoomLevel(newZoom);
    setBrushStartIndex(newStart);
    setBrushEndIndex(newEnd);
  }, [zoomLevel, brushStartIndex, brushEndIndex]);
  
  const handleResetZoom = useCallback(() => {
    const totalPoints = displayDataLengthRef.current;
    setZoomLevel(1);
    setBrushStartIndex(0);
    setBrushEndIndex(Math.max(0, totalPoints - 1));
    setIsZoomed(false);
  }, []);
  
  const handlePanLeft = useCallback(() => {
    const totalPoints = displayDataLengthRef.current;
    if (totalPoints === 0 || zoomLevel <= 1) return;
    const visiblePoints = Math.max(1, Math.ceil(totalPoints / zoomLevel));
    const step = Math.max(1, Math.floor(visiblePoints / 4));
    const newStart = Math.max(0, brushStartIndex - step);
    const newEnd = Math.min(totalPoints - 1, newStart + visiblePoints - 1);
    setBrushStartIndex(newStart);
    setBrushEndIndex(newEnd);
  }, [zoomLevel, brushStartIndex]);
  
  const handlePanRight = useCallback(() => {
    const totalPoints = displayDataLengthRef.current;
    if (totalPoints === 0 || zoomLevel <= 1) return;
    const visiblePoints = Math.max(1, Math.ceil(totalPoints / zoomLevel));
    const step = Math.max(1, Math.floor(visiblePoints / 4));
    const newEnd = Math.min(totalPoints - 1, brushEndIndex + step);
    const newStart = Math.max(0, newEnd - visiblePoints + 1);
    setBrushStartIndex(newStart);
    setBrushEndIndex(newEnd);
  }, [zoomLevel, brushEndIndex]);
  
  // Handle brush change from user dragging the Brush slider
  const handleBrushChange = useCallback((params: { startIndex?: number; endIndex?: number }) => {
    if (params.startIndex !== undefined && params.endIndex !== undefined) {
      setBrushStartIndex(params.startIndex);
      setBrushEndIndex(params.endIndex);
      // Sync zoomLevel based on visible proportion so reset button appears
      const totalPoints = displayDataLengthRef.current;
      if (totalPoints > 0) {
        const visibleRange = params.endIndex - params.startIndex + 1;
        const newZoom = totalPoints / visibleRange;
        setZoomLevel(Math.max(1, Math.round(newZoom * 10) / 10));
        // Mark as zoomed if the brush doesn't cover the full range
        setIsZoomed(params.startIndex > 0 || params.endIndex < totalPoints - 1);
      }
    }
  }, []);
  
  // Filtered nodes for Node Status section
  const filteredNodes = useMemo(() => {
    if (!dashboardData?.nodes) return [];
    if (selectedNode === "all") return dashboardData.nodes;
    return dashboardData.nodes.filter(n => n.node_name === selectedNode);
  }, [dashboardData?.nodes, selectedNode]);
  
  // Filtered stats based on selected node - MUST be before chartData
  const filteredStats = useMemo(() => {
    if (!dashboardData) return null;
    if (selectedNode === "all") {
      const singleNode = dashboardData.nodes?.length === 1 ? dashboardData.nodes[0] : null;

      return {
        total_instances: dashboardData.total_instances,
        running_instances: dashboardData.running_instances,
        stopped_instances: dashboardData.stopped_instances,
        other_instances: dashboardData.other_instances,
        container_count: dashboardData.container_count,
        vm_count: dashboardData.vm_count,
        online_nodes: dashboardData.online_nodes,
        inactive_nodes: dashboardData.inactive_nodes,
        total_nodes: dashboardData.total_nodes,
        cpu_usage_percent: singleNode?.cpu_usage_percent ?? dashboardData.cpu_usage_percent,
        cpu_reading: {
          status: dashboardData.cpu_status ?? "unavailable",
          busyPercent: dashboardData.cpu_busy_percent ?? null,
          busyThreads: dashboardData.cpu_busy_threads ?? null,
          threads: dashboardData.cpu_reporting_threads ?? dashboardData.total_cpu,
          reportingInstances: dashboardData.cpu_reporting_instances ?? 0,
          runningInstances: dashboardData.running_instances,
        } as CpuUsageReading,
        memory_usage_percent: dashboardData.memory_usage_percent,
        storage_usage_percent: dashboardData.storage_usage_percent,
        total_cpu: dashboardData.total_cpu,
        used_cpu: dashboardData.used_cpu,
        total_memory_bytes: dashboardData.total_memory_bytes,
        used_memory_bytes: dashboardData.used_memory_bytes,
        total_storage_bytes: dashboardData.total_storage_bytes,
        used_storage_bytes: dashboardData.used_storage_bytes,
        total_gpu: dashboardData.total_gpu,
        total_gpu_cards: dashboardData.total_gpu_cards,
        used_gpu: dashboardData.used_gpu,
        warnings: dashboardData.warnings,
        cluster_status: singleNode?.cluster_status,
        status_message: singleNode?.status_message,
      };
    }
    
    // Find the selected node's data
    const node = dashboardData.nodes?.find(n => n.node_name === selectedNode);
    if (!node) return null;
    
    return {
      total_instances: node.instance_count || 0,
      running_instances: node.running_instances || 0,
      stopped_instances: (node.instance_count || 0) - (node.running_instances || 0),
      other_instances: 0,
      container_count: node.container_count ?? 0,
      vm_count: node.vm_count ?? 0,
      online_nodes: node.is_online ? 1 : 0,
      inactive_nodes: node.is_online ? 0 : 1,
      total_nodes: 1,
      cpu_usage_percent: node.cpu_usage_percent || 0,
      cpu_reading: {
        status: node.is_online ? node.cpu_status ?? "unavailable" : "unavailable",
        busyPercent: node.cpu_busy_percent ?? null,
        busyThreads: node.cpu_busy_threads ?? null,
        threads: node.cpu_total || 0,
        reportingInstances: node.cpu_reporting_instances ?? 0,
        runningInstances: node.running_instances || 0,
      } as CpuUsageReading,
      memory_usage_percent: node.memory_usage_percent || 0,
      storage_usage_percent: node.storage_usage_percent || 0,
      total_cpu: node.cpu_total || 0,
      used_cpu: node.cpu_used || 0,
      total_memory_bytes: node.memory_total_bytes || 0,
      used_memory_bytes: node.memory_used_bytes || 0,
      total_storage_bytes: node.storage_total_bytes || 0,
      used_storage_bytes: node.storage_used_bytes || 0,
      total_gpu: node.gpu_total || 0,
      total_gpu_cards: node.gpu_cards_total,
      used_gpu: node.gpu_used || 0,
      warnings: dashboardData.warnings, // Keep global warnings
      // Status message for troubleshooting when metrics are 0
      cluster_status: node.cluster_status,
      status_message: node.status_message,
    };
  }, [dashboardData, selectedNode]);

  // Format history data for charts - filter by selected node
  // Extract stable memory/storage totals so chartData doesn't recompute on every dashboard poll
  const totalMemoryBytes = useMemo(
    () => filteredStats?.total_memory_bytes || dashboardData?.total_memory_bytes || 0,
    [filteredStats?.total_memory_bytes, dashboardData?.total_memory_bytes]
  );
  const totalStorageBytes = useMemo(
    () => filteredStats?.total_storage_bytes || dashboardData?.total_storage_bytes || 0,
    [filteredStats?.total_storage_bytes, dashboardData?.total_storage_bytes]
  );

  const chartData = useMemo(
    () =>
      toChartPoints(
        historyData,
        selectedNode,
        { memoryBytes: totalMemoryBytes, storageBytes: totalStorageBytes },
        timeRange,
      ),
    [historyData, selectedNode, totalMemoryBytes, totalStorageBytes, timeRange],
  );
  
  // Downsample chart data for performance - limit to ~200 points for smooth rendering
  // This significantly improves hover/tooltip performance for large time ranges (6h, 24h)
  // Multi-metric downsampling: no dependency on chartTab so tab switches don't re-downsample
  const MAX_DISPLAY_POINTS = 200;
  const displayChartData = useMemo(() => {
    if (chartData.length <= MAX_DISPLAY_POINTS) return chartData;
    return downsampleData(chartData, MAX_DISPLAY_POINTS);
  }, [chartData]);
  
  // Keep ref in sync for zoom handlers (updated synchronously during render)
  displayDataLengthRef.current = displayChartData.length;
  
  // Validate brush indices when display data length changes (polling, range switch)
  useEffect(() => {
    const len = displayChartData.length;
    if (len === 0) {
      setBrushStartIndex(0);
      setBrushEndIndex(0);
      setIsZoomed(false);
      return;
    }
    // Clamp brush indices to valid range
    if (brushEndIndex >= len) {
      setBrushEndIndex(len - 1);
      const visiblePoints = Math.max(1, Math.ceil(len / zoomLevel));
      setBrushStartIndex(Math.max(0, len - visiblePoints));
    }
    // If not zoomed, keep brush tracking full range (so new data points appear)
    if (!isZoomed) {
      setBrushStartIndex(0);
      setBrushEndIndex(len - 1);
    }
  }, [displayChartData.length, zoomLevel, brushEndIndex, isZoomed]);
  
  const selectedTopConsumerVm = useMemo(
    () => selectedTopConsumer ? toVmInfoFromMonitoringSummary(selectedTopConsumer) : null,
    [selectedTopConsumer]
  );

  // Calculate dynamic Y-axis domain based on data
  // This creates a "zoom-in" effect when values are low
  const calculateDynamicDomain = (values: number[], padding = 0.2): [number, number] => {
    if (values.length === 0) return [0, 100];
    
    const maxValue = Math.max(...values);
    const minValue = Math.min(...values);
    
    // Determine appropriate max based on data range
    // Use nice round numbers: 5, 10, 15, 20, 25, 50, 75, 100
    const thresholds = [5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100];
    
    // Add padding to max value
    const paddedMax = maxValue * (1 + padding);
    
    // Find the smallest threshold that fits the data
    let niceCeiling = 100;
    for (const threshold of thresholds) {
      if (paddedMax <= threshold) {
        niceCeiling = threshold;
        break;
      }
    }
    
    // For very small values (< 1%), use smaller scale
    if (maxValue < 1) {
      niceCeiling = Math.max(2, Math.ceil(paddedMax));
    }
    
    return [0, niceCeiling];
  };

  // Memoized chart domains for each metric type
  const cpuDomain = useMemo((): [number, number] => {
    if (!chartData.length) return [0, 100];
    const values = chartData.map(d => d.cpu).filter(v => typeof v === 'number');
    return calculateDynamicDomain(values);
  }, [chartData]);

  const memoryDomain = useMemo((): [number, number] => {
    if (!chartData.length) return [0, 100];
    const values = chartData.map(d => d.memory).filter(v => typeof v === 'number');
    return calculateDynamicDomain(values);
  }, [chartData]);

  const storageDomain = useMemo((): [number, number] => {
    if (!chartData.length) return [0, 100];
    const values = chartData.map(d => d.storage).filter(v => typeof v === 'number');
    return calculateDynamicDomain(values);
  }, [chartData]);

  const networkDomain = useMemo((): [number, number] => {
    if (!chartData.length) return [0, 10_000_000];
    const rxValues = chartData.map(d => d.networkRxRate).filter(v => typeof v === 'number');
    const txValues = chartData.map(d => d.networkTxRate).filter(v => typeof v === 'number');
    const allValues = [...rxValues, ...txValues];
    
    if (allValues.length === 0) return [0, 10_000_000];
    
    const maxValue = Math.max(...allValues);
    const paddedMax = maxValue * 1.2;
    
    // Decimal byte-per-second thresholds, matching the SI labels printed on the axis.
    const thresholds = [
      1_000_000,
      2_000_000,
      5_000_000,
      10_000_000,
      20_000_000,
      50_000_000,
      100_000_000,
      200_000_000,
      500_000_000,
      1_000_000_000,
    ];
    let niceCeiling = 10_000_000;
    for (const threshold of thresholds) {
      if (paddedMax <= threshold) {
        niceCeiling = threshold;
        break;
      }
    }
    
    // For very small values, show at least 1 MB/s.
    if (maxValue < 100_000) {
      niceCeiling = 1_000_000;
    }
    
    return [0, niceCeiling];
  }, [chartData]);

  // Stable tick formatters to avoid Recharts re-renders from new function refs
  const percentTickFormatter = useCallback((v: number) => `${v}%`, []);
  const identityTickFormatter = useCallback((value: string) => value, []);
  const networkTickFormatter = useCallback((v: number) => formatNetworkRate(v, 1), []);
  const networkTooltipFormatter = useCallback((value: number | string | undefined, name?: string) => {
    const numValue = typeof value === 'number' ? value : 0;
    const label = name === "networkRxRate" ? "↓ Inbound" : "↑ Outbound";
    return [formatNetworkRate(numValue), label];
  }, []);
  // Stable object ref for tooltip contentStyle
  const tooltipContentStyle = useMemo(() => ({
    backgroundColor: "hsl(var(--card))",
    border: "1px solid hsl(var(--border))",
    borderRadius: "8px"
  }), []);

  // Calculate chart statistics (min, max, avg) for each metric
  // When "All Nodes" is selected, calculate stats from all visible per-node values
  const chartStats = useMemo(() => {
    // Single-pass min/max/avg instead of 3 separate array traversals
    const calcStats = (values: number[]) => {
      if (values.length === 0) return { min: 0, max: 0, avg: 0 };
      let min = values[0], max = values[0], sum = 0;
      for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (v < min) min = v;
        if (v > max) max = v;
        sum += v;
      }
      return { min, max, avg: sum / values.length };
    };

    // Get visible node names for per-node stats
    const activeNodeNames = nodeNames.filter(name => visibleNodes[name]);
    
    // When "All Nodes" is selected, extract per-node values from chartData
    if (selectedNode === "all" && activeNodeNames.length > 0) {
      const cpuValues: number[] = [];
      const memoryValues: number[] = [];
      const storageValues: number[] = [];
      
      chartData.forEach(d => {
        // Cast to Record for dynamic key access (per-node metrics are spread into object)
        const dataPoint = d as Record<string, number | string>;
        activeNodeNames.forEach(nodeName => {
          const cpuKey = `cpu_${nodeName}`;
          const memKey = `memory_${nodeName}`;
          const storageKey = `storage_${nodeName}`;
          
          if (typeof dataPoint[cpuKey] === 'number') cpuValues.push(dataPoint[cpuKey] as number);
          if (typeof dataPoint[memKey] === 'number') memoryValues.push(dataPoint[memKey] as number);
          if (typeof dataPoint[storageKey] === 'number') storageValues.push(dataPoint[storageKey] as number);
        });
      });
      
      // Network stats remain aggregated (RX/TX are already totals, not per-node)
      const networkRxValues = chartData.map(d => d.networkRxRate).filter(v => typeof v === 'number');
      const networkTxValues = chartData.map(d => d.networkTxRate).filter(v => typeof v === 'number');

      return {
        cpu: calcStats(cpuValues),
        memory: calcStats(memoryValues),
        storage: calcStats(storageValues),
        networkRx: calcStats(networkRxValues),
        networkTx: calcStats(networkTxValues),
      };
    }

    // Single node selected - use aggregate values
    const cpuValues = chartData.map(d => d.cpu).filter(v => typeof v === 'number');
    const memoryValues = chartData.map(d => d.memory).filter(v => typeof v === 'number');
    const storageValues = chartData.map(d => d.storage).filter(v => typeof v === 'number');
    const networkRxValues = chartData.map(d => d.networkRxRate).filter(v => typeof v === 'number');
    const networkTxValues = chartData.map(d => d.networkTxRate).filter(v => typeof v === 'number');

    return {
      cpu: calcStats(cpuValues),
      memory: calcStats(memoryValues),
      storage: calcStats(storageValues),
      networkRx: calcStats(networkRxValues),
      networkTx: calcStats(networkTxValues),
    };
  }, [chartData, selectedNode, nodeNames, visibleNodes]);

  // Last updated time

  // Instance distribution for pie chart - uses filteredStats to respect node selection
  const instanceDistribution = useMemo(() => {
    if (!filteredStats) return [];
    const total = filteredStats.running_instances + filteredStats.stopped_instances + filteredStats.other_instances;
    return [
      { name: "Running", value: filteredStats.running_instances, color: PIE_COLORS[0], percent: total > 0 ? (filteredStats.running_instances / total) * 100 : 0 },
      { name: "Stopped", value: filteredStats.stopped_instances, color: PIE_COLORS[1], percent: total > 0 ? (filteredStats.stopped_instances / total) * 100 : 0 },
      { name: "Other", value: filteredStats.other_instances, color: PIE_COLORS[2], percent: total > 0 ? (filteredStats.other_instances / total) * 100 : 0 },
    ].filter(item => item.value > 0);
  }, [filteredStats]);

  if (dashboardLoading && !dashboardData) {
    return <LoadingSkeleton />;
  }

  if (dashboardError) {
    return (
      <div className="p-6">
        <Card className="border-destructive">
          <CardContent className="p-6 text-center">
            <AlertTriangle className="h-12 w-12 mx-auto text-destructive mb-4" />
            <h3 className="text-lg font-semibold">Failed to load monitoring data</h3>
            <p className="text-muted-foreground mt-2">
              {dashboardError instanceof Error ? dashboardError.message : "Unknown error"}
            </p>
            <Button onClick={() => void refreshMonitoring()} className="mt-4">
              <RefreshCw className="h-4 w-4 mr-2" />
              Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const data = dashboardData;
  if (!data) return null;

  return (
    <div className="space-y-6 p-4 md:p-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl md:text-3xl font-bold tracking-tight">Monitoring</h1>
          <p className="text-muted-foreground">
            Resource metrics across all clusters
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <LiveTimestamp
            updatedAt={dataUpdatedAt || null}
            staleAfterMs={isPaused ? undefined : STALE_AFTER_MS.active}
          />
          <RefreshButton
            hideTimestamp
            label="Refresh monitoring"
            onRefresh={refreshMonitoring}
          />
          <UITooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                aria-label={isPaused ? "Resume automatic monitoring updates" : "Pause automatic monitoring updates"}
                aria-pressed={isPaused}
                onClick={toggleMonitoringPause}
              >
                {isPaused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {isPaused
                ? "Resume automatic monitoring updates. Workloads kept running while paused."
                : "Pause automatic monitoring updates. Workloads will keep running."}
            </TooltipContent>
          </UITooltip>
          {/* Node Selection - at the end (rightmost) */}
          <NodeFilterSelect
            nodes={dashboardData?.nodes ?? []}
            value={selectedNode}
            onValueChange={setSelectedNode}
          />
        </div>
      </div>

      {/* Overview Stats - use filteredStats for node-filtered view */}
      {filteredStats && (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          title="Total Instances"
          value={filteredStats.total_instances}
          subtitle={selectedNode === "all" ? `${filteredStats.container_count} containers • ${filteredStats.vm_count} VMs` : `on ${selectedNode}`}
        />
        <MetricCard
          title="Running"
          value={filteredStats.running_instances}
          subtitle={`${filteredStats.stopped_instances} stopped`}
          progress={(filteredStats.running_instances / Math.max(filteredStats.total_instances, 1)) * 100}
        />
        <MetricCard
          title="Clusters Online"
          value={`${filteredStats.online_nodes} / ${filteredStats.total_nodes}`}
          subtitle={filteredStats.inactive_nodes > 0 ? `${filteredStats.inactive_nodes} inactive` : "All clusters healthy"}
          trend={filteredStats.inactive_nodes > 0 ? "down" : "neutral"}
        />
        <MetricCard
          title="Warnings"
          value={filteredStats.warnings?.total || 0}
          subtitle={filteredStats.warnings?.critical ? `${filteredStats.warnings.critical} critical` : "No critical issues"}
          trend={(filteredStats.warnings?.critical || 0) > 0 ? "up" : "neutral"}
        />
      </div>
      )}

      {/* Resource Utilization Gauges */}
      {filteredStats && (
      <Card>
        <CardHeader>
          <div>
            <CardTitle>
              Resource Utilization
            </CardTitle>
            <CardDescription>
              {selectedNode === "all" ? "Usage and allocation across all clusters" : `Usage and allocation on ${selectedNode}`}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
            <div className="text-center">
              <CpuUsageGauge
                reading={filteredStats.cpu_reading}
                label="CPU Usage"
                color={CHART_COLORS.cpu}
                statusMessage={isPartner && filteredStats.cpu_reading.status !== "ready" && filteredStats.status_message ? filteredStats.status_message : undefined}
              />
              <p className="text-sm text-muted-foreground mt-2">
                Allocated: {filteredStats.used_cpu} vCPU / {filteredStats.total_cpu} hardware threads (
                {cpuAllocation({ cpu_total: filteredStats.total_cpu, cpu_used: filteredStats.used_cpu }).ratioLabel})
              </p>
            </div>
            <div className="text-center">
              <GaugeChart 
                value={filteredStats.memory_usage_percent} 
                label="Memory Usage" 
                color={CHART_COLORS.memory}
                statusMessage={isPartner && filteredStats.memory_usage_percent === 0 && filteredStats.status_message ? filteredStats.status_message : undefined}
              />
              <p className="text-sm text-muted-foreground mt-2">
                {formatMemory(filteredStats.used_memory_bytes)} / {formatMemory(filteredStats.total_memory_bytes)}
              </p>
            </div>
            <div className="text-center">
              <GaugeChart 
                value={filteredStats.storage_usage_percent} 
                label="Storage Usage" 
                color={CHART_COLORS.storage}
                statusMessage={isPartner && filteredStats.storage_usage_percent === 0 && filteredStats.status_message ? filteredStats.status_message : undefined}
              />
              <p className="text-sm text-muted-foreground mt-2">
                {formatBytes(filteredStats.used_storage_bytes)} / {formatBytes(filteredStats.total_storage_bytes)}
              </p>
            </div>
            <div className="text-center">
              <GpuAllocationGauge
                stats={filteredStats}
                scope={selectedNode}
                color={CHART_COLORS.gpu}
              />
            </div>
          </div>
        </CardContent>
      </Card>
      )}

      {/* Live Charts */}
      <Card>
        <CardHeader>
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div>
              <CardTitle>
                Resource Metrics
              </CardTitle>
              <CardDescription>Usage over time, one point per collection</CardDescription>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              {/* Zoom/Pan Controls */}
              <div className="flex items-center gap-1 bg-muted rounded-lg p-1">
                <UITooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0"
                      onClick={handlePanLeft}
                      disabled={zoomLevel <= 1 || brushStartIndex <= 0}
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Pan Left (Earlier)</TooltipContent>
                </UITooltip>
                
                <UITooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0"
                      onClick={handleZoomOut}
                      disabled={zoomLevel <= 1}
                    >
                      <ZoomOut className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Zoom Out</TooltipContent>
                </UITooltip>
                
                <span className="text-xs text-muted-foreground px-1 min-w-[40px] text-center">
                  {zoomLevel > 1 ? `${Math.round(100 / zoomLevel)}%` : '100%'}
                </span>
                
                <UITooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0"
                      onClick={handleZoomIn}
                      disabled={zoomLevel >= 8}
                    >
                      <ZoomIn className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Zoom In</TooltipContent>
                </UITooltip>
                
                <UITooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0"
                      onClick={handlePanRight}
                      disabled={zoomLevel <= 1 || brushEndIndex >= displayChartData.length - 1}
                    >
                      <ChevronRight className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Pan Right (Recent)</TooltipContent>
                </UITooltip>
                
                {isZoomed && (
                  <UITooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0"
                        onClick={handleResetZoom}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Reset Zoom</TooltipContent>
                  </UITooltip>
                )}
              </div>
              
              {/* Time Range Selector */}
              <div className="flex items-center gap-1 bg-muted rounded-lg p-1">
                {HISTORY_WINDOWS.map((range) => (
                  <Button
                    key={range.seconds}
                    variant={timeRange === range.seconds ? "default" : "ghost"}
                    size="sm"
                    className={cn(
                      "h-7 px-3 text-xs font-medium transition-all",
                      timeRange === range.seconds 
                        ? "bg-primary text-primary-foreground shadow-sm" 
                        : "hover:bg-muted-foreground/10"
                    )}
                    disabled={isPaused}
                    title={isPaused ? "Resume automatic monitoring updates to change the time range" : undefined}
                    onClick={() => {
                      // Use transition for non-blocking UI during heavy chart recomputation
                      startTimeRangeTransition(() => {
                        setTimeRange(range.seconds);
                      });
                      handleResetZoom(); // Reset zoom when changing time range
                    }}
                  >
                    {range.label}
                  </Button>
                ))}
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {/* Chart Type Tabs */}
          <Tabs value={chartTab} onValueChange={(v) => setChartTab(v as "cpu" | "memory" | "storage" | "network")} className="mb-4">
            {/* Updating indicator inline with tabs */}
            {(isTimeRangePending || historyFetching) && (
              <div className="inline-flex items-center gap-1.5 ml-3 align-middle">
                <RefreshCw className="h-3 w-3 animate-spin text-primary" />
                <span className="text-xs text-muted-foreground">Updating...</span>
              </div>
            )}
            <TabsList className="grid w-full max-w-2xl grid-cols-4 h-auto p-1">
              <TabsTrigger value="cpu" className="flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs sm:text-sm sm:gap-2 sm:px-3">
                <Cpu className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0" />
                <span>CPU</span>
              </TabsTrigger>
              <TabsTrigger value="memory" className="flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs sm:text-sm sm:gap-2 sm:px-3">
                <MemoryStick className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0" />
                <span>Memory</span>
              </TabsTrigger>
              <TabsTrigger value="storage" className="flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs sm:text-sm sm:gap-2 sm:px-3">
                <HardDrive className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0 hidden sm:block" />
                <span>Storage</span>
              </TabsTrigger>
              <TabsTrigger value="network" className="flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs sm:text-sm sm:gap-2 sm:px-3">
                <Network className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0 hidden sm:block" />
                <span>Network</span>
              </TabsTrigger>
            </TabsList>
            
            {/* CPU Chart */}
            <TabsContent value="cpu" className="mt-4">
              <ChartStats 
                min={chartStats.cpu.min} 
                max={chartStats.cpu.max} 
                avg={chartStats.cpu.avg} 
                unit="%" 
                color={CHART_COLORS.cpu} 
              />
              {/* Node toggle buttons when "All Nodes" is selected */}
              {selectedNode === "all" && nodeNames.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 mb-3">
                  <span className="text-[10px] sm:text-xs text-muted-foreground">Show:</span>
                  {nodeNames.map((nodeName, idx) => (
                    <Button
                      key={nodeName}
                      variant={visibleNodes[nodeName] ? "default" : "outline"}
                      size="sm"
                      className="h-5 sm:h-6 px-1.5 sm:px-2 text-[10px] sm:text-xs"
                      style={{
                        backgroundColor: visibleNodes[nodeName] ? NODE_COLORS[idx % NODE_COLORS.length] : undefined,
                        borderColor: NODE_COLORS[idx % NODE_COLORS.length],
                        color: visibleNodes[nodeName] ? "white" : NODE_COLORS[idx % NODE_COLORS.length],
                      }}
                      onClick={() => toggleNodeVisibility(nodeName)}
                    >
                      {nodeName}
                    </Button>
                  ))}
                </div>
              )}
              <ResponsiveContainer width="100%" height={400}>
                <AreaChart 
                  data={displayChartData}
                  margin={{ top: 10, right: 80, left: 20, bottom: 40 }}
                >
                  <defs>
                    <linearGradient id="cpuGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_COLORS.cpu} stopOpacity={0.3}/>
                      <stop offset="95%" stopColor={CHART_COLORS.cpu} stopOpacity={0}/>
                    </linearGradient>
                    {/* Gradients for each node */}
                    {nodeNames.map((nodeName, idx) => (
                      <linearGradient key={nodeName} id={`cpuGradient_${nodeName}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={NODE_COLORS[idx % NODE_COLORS.length]} stopOpacity={0.2}/>
                        <stop offset="95%" stopColor={NODE_COLORS[idx % NODE_COLORS.length]} stopOpacity={0}/>
                      </linearGradient>
                    ))}
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis 
                    dataKey="time" 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    className="text-muted-foreground"
                    interval="preserveStartEnd"
                  />
                  <YAxis 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    domain={cpuDomain}
                    tickFormatter={percentTickFormatter}
                    className="text-muted-foreground"
                  />
                  <Tooltip
                    wrapperStyle={CHART_TOOLTIP_STYLE}
                    content={
                      <ResourceMetricsTooltip
                        metricType="cpu"
                        nodeStatusMap={nodeStatusMap}
                        nodeNames={nodeNames}
                        selectedNode={selectedNode}
                      />
                    }
                  />
                  {selectedNode === "all" && nodeNames.length > 1 ? (
                    /* Multi-node lines when "All Nodes" selected */
                    <>
                      {nodeNames.map((nodeName, idx) => (
                        visibleNodes[nodeName] && (
                          <Area
                            key={nodeName}
                            type="monotone"
                            dataKey={`cpu_${nodeName}`}
                            stroke={NODE_COLORS[idx % NODE_COLORS.length]}
                            fill={`url(#cpuGradient_${nodeName})`}
                            strokeWidth={2}
                            name={`cpu_${nodeName}`}
                            isAnimationActive={false}
                          />
                        )
                      ))}
                    </>
                  ) : (
                    /* Single line for specific node or aggregate */
                    <Area
                      type="monotone"
                      dataKey="cpu"
                      stroke={CHART_COLORS.cpu}
                      fill="url(#cpuGradient)"
                      strokeWidth={2}
                      name="CPU Usage"
                      isAnimationActive={false}
                    />
                  )}
                  {selectedNode === "all" && nodeNames.length > 1 && (
                    <Legend 
                      formatter={(value: string) => value.replace('cpu_', '')}
                      wrapperStyle={{ paddingTop: "10px" }}
                    />
                  )}
                  {/* Brush for pan/zoom */}
                  <Brush
                    dataKey="time"
                    height={30}
                    stroke="hsl(var(--primary))"
                    fill="hsl(var(--muted))"
                    startIndex={brushStartIndex}
                    endIndex={brushEndIndex}
                    onChange={handleBrushChange}
                    tickFormatter={identityTickFormatter}
                    padding={{ top: 0, bottom: 0, left: 20, right: 70 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </TabsContent>
            
            {/* Memory Chart */}
            <TabsContent value="memory" className="mt-4">
              <ChartStats 
                min={chartStats.memory.min} 
                max={chartStats.memory.max} 
                avg={chartStats.memory.avg} 
                unit="%" 
                color={CHART_COLORS.memory} 
              />
              {/* Node toggle buttons when "All Nodes" is selected */}
              {selectedNode === "all" && nodeNames.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 mb-3">
                  <span className="text-[10px] sm:text-xs text-muted-foreground">Show:</span>
                  {nodeNames.map((nodeName, idx) => (
                    <Button
                      key={nodeName}
                      variant={visibleNodes[nodeName] ? "default" : "outline"}
                      size="sm"
                      className="h-5 sm:h-6 px-1.5 sm:px-2 text-[10px] sm:text-xs"
                      style={{
                        backgroundColor: visibleNodes[nodeName] ? NODE_COLORS[idx % NODE_COLORS.length] : undefined,
                        borderColor: NODE_COLORS[idx % NODE_COLORS.length],
                        color: visibleNodes[nodeName] ? "white" : NODE_COLORS[idx % NODE_COLORS.length],
                      }}
                      onClick={() => toggleNodeVisibility(nodeName)}
                    >
                      {nodeName}
                    </Button>
                  ))}
                </div>
              )}
              <ResponsiveContainer width="100%" height={400}>
                <AreaChart 
                  data={displayChartData}
                  margin={{ top: 10, right: 80, left: 20, bottom: 40 }}
                >
                  <defs>
                    <linearGradient id="memoryGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_COLORS.memory} stopOpacity={0.3}/>
                      <stop offset="95%" stopColor={CHART_COLORS.memory} stopOpacity={0}/>
                    </linearGradient>
                    {/* Gradients for each node */}
                    {nodeNames.map((nodeName, idx) => (
                      <linearGradient key={nodeName} id={`memoryGradient_${nodeName}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={NODE_COLORS[idx % NODE_COLORS.length]} stopOpacity={0.2}/>
                        <stop offset="95%" stopColor={NODE_COLORS[idx % NODE_COLORS.length]} stopOpacity={0}/>
                      </linearGradient>
                    ))}
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis 
                    dataKey="time" 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    className="text-muted-foreground"
                    interval="preserveStartEnd"
                  />
                  <YAxis 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    domain={memoryDomain}
                    tickFormatter={percentTickFormatter}
                    className="text-muted-foreground"
                  />
                  <Tooltip
                    wrapperStyle={CHART_TOOLTIP_STYLE}
                    content={
                      <ResourceMetricsTooltip
                        metricType="memory"
                        nodeStatusMap={nodeStatusMap}
                        nodeNames={nodeNames}
                        selectedNode={selectedNode}
                      />
                    }
                  />
                  {selectedNode === "all" && nodeNames.length > 1 ? (
                    <>
                      {nodeNames.map((nodeName, idx) => (
                        visibleNodes[nodeName] && (
                          <Area
                            key={nodeName}
                            type="monotone"
                            dataKey={`memory_${nodeName}`}
                            stroke={NODE_COLORS[idx % NODE_COLORS.length]}
                            fill={`url(#memoryGradient_${nodeName})`}
                            strokeWidth={2}
                            name={`memory_${nodeName}`}
                            isAnimationActive={false}
                          />
                        )
                      ))}
                    </>
                  ) : (
                    <Area
                      type="monotone"
                      dataKey="memory"
                      stroke={CHART_COLORS.memory}
                      fill="url(#memoryGradient)"
                      strokeWidth={2}
                      name="Memory Usage"
                      isAnimationActive={false}
                    />
                  )}
                  {selectedNode === "all" && nodeNames.length > 1 && (
                    <Legend 
                      formatter={(value: string) => value.replace('memory_', '')}
                      wrapperStyle={{ paddingTop: "10px" }}
                    />
                  )}
                  {/* Brush for pan/zoom */}
                  <Brush
                    dataKey="time"
                    height={30}
                    stroke="hsl(var(--primary))"
                    fill="hsl(var(--muted))"
                    startIndex={brushStartIndex}
                    endIndex={brushEndIndex}
                    onChange={handleBrushChange}
                    tickFormatter={identityTickFormatter}
                    padding={{ top: 0, bottom: 0, left: 20, right: 70 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </TabsContent>
            
            {/* Storage Chart */}
            <TabsContent value="storage" className="mt-4">
              <ChartStats 
                min={chartStats.storage.min} 
                max={chartStats.storage.max} 
                avg={chartStats.storage.avg} 
                unit="%" 
                color={CHART_COLORS.storage} 
              />
              {/* Node toggle buttons when "All Nodes" is selected */}
              {selectedNode === "all" && nodeNames.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 mb-3">
                  <span className="text-[10px] sm:text-xs text-muted-foreground">Show:</span>
                  {nodeNames.map((nodeName, idx) => (
                    <Button
                      key={nodeName}
                      variant={visibleNodes[nodeName] ? "default" : "outline"}
                      size="sm"
                      className="h-5 sm:h-6 px-1.5 sm:px-2 text-[10px] sm:text-xs"
                      style={{
                        backgroundColor: visibleNodes[nodeName] ? NODE_COLORS[idx % NODE_COLORS.length] : undefined,
                        borderColor: NODE_COLORS[idx % NODE_COLORS.length],
                        color: visibleNodes[nodeName] ? "white" : NODE_COLORS[idx % NODE_COLORS.length],
                      }}
                      onClick={() => toggleNodeVisibility(nodeName)}
                    >
                      {nodeName}
                    </Button>
                  ))}
                </div>
              )}
              <ResponsiveContainer width="100%" height={400}>
                <AreaChart 
                  data={displayChartData}
                  margin={{ top: 10, right: 80, left: 20, bottom: 40 }}
                >
                  <defs>
                    <linearGradient id="storageGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_COLORS.storage} stopOpacity={0.3}/>
                      <stop offset="95%" stopColor={CHART_COLORS.storage} stopOpacity={0}/>
                    </linearGradient>
                    {/* Gradients for each node */}
                    {nodeNames.map((nodeName, idx) => (
                      <linearGradient key={nodeName} id={`storageGradient_${nodeName}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={NODE_COLORS[idx % NODE_COLORS.length]} stopOpacity={0.2}/>
                        <stop offset="95%" stopColor={NODE_COLORS[idx % NODE_COLORS.length]} stopOpacity={0}/>
                      </linearGradient>
                    ))}
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis 
                    dataKey="time" 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    className="text-muted-foreground"
                    interval="preserveStartEnd"
                  />
                  <YAxis 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    domain={storageDomain}
                    tickFormatter={percentTickFormatter}
                    className="text-muted-foreground"
                  />
                  <Tooltip
                    wrapperStyle={CHART_TOOLTIP_STYLE}
                    content={
                      <ResourceMetricsTooltip
                        metricType="storage"
                        nodeStatusMap={nodeStatusMap}
                        nodeNames={nodeNames}
                        selectedNode={selectedNode}
                      />
                    }
                  />
                  {selectedNode === "all" && nodeNames.length > 1 ? (
                    <>
                      {nodeNames.map((nodeName, idx) => (
                        visibleNodes[nodeName] && (
                          <Area
                            key={nodeName}
                            type="monotone"
                            dataKey={`storage_${nodeName}`}
                            stroke={NODE_COLORS[idx % NODE_COLORS.length]}
                            fill={`url(#storageGradient_${nodeName})`}
                            strokeWidth={2}
                            name={`storage_${nodeName}`}
                            isAnimationActive={false}
                          />
                        )
                      ))}
                    </>
                  ) : (
                    <Area
                      type="monotone"
                      dataKey="storage"
                      stroke={CHART_COLORS.storage}
                      fill="url(#storageGradient)"
                      strokeWidth={2}
                      name="Storage Usage"
                      isAnimationActive={false}
                    />
                  )}
                  {selectedNode === "all" && nodeNames.length > 1 && (
                    <Legend 
                      formatter={(value: string) => value.replace('storage_', '')}
                      wrapperStyle={{ paddingTop: "10px" }}
                    />
                  )}
                  {/* Brush for pan/zoom */}
                  <Brush
                    dataKey="time"
                    height={30}
                    stroke="hsl(var(--primary))"
                    fill="hsl(var(--muted))"
                    startIndex={brushStartIndex}
                    endIndex={brushEndIndex}
                    onChange={handleBrushChange}
                    tickFormatter={identityTickFormatter}
                    padding={{ top: 0, bottom: 0, left: 20, right: 70 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </TabsContent>
            
            {/* Network Chart */}
            <TabsContent value="network" className="mt-4">
              {/* Network Stats - RX and TX */}
              <div className="flex flex-col sm:flex-row items-center justify-center gap-2 sm:gap-8 mb-3 py-2 px-2 sm:px-4 bg-muted/50 rounded-lg">
                <div className="flex flex-wrap items-center justify-center gap-2 sm:gap-4">
                  <span className="text-[10px] sm:text-xs font-medium" style={{ color: CHART_COLORS.network }}>↓ Inbound (RX)</span>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Min:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.network }}>{formatNetworkRate(chartStats.networkRx.min)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Avg:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.network }}>{formatNetworkRate(chartStats.networkRx.avg)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Max:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.network }}>{formatNetworkRate(chartStats.networkRx.max)}</span>
                  </div>
                </div>
                <div className="hidden sm:block w-px h-6 bg-border" />
                <div className="flex flex-wrap items-center justify-center gap-2 sm:gap-4">
                  <span className="text-[10px] sm:text-xs font-medium" style={{ color: CHART_COLORS.networkTx }}>↑ Outbound (TX)</span>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Min:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.networkTx }}>{formatNetworkRate(chartStats.networkTx.min)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Avg:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.networkTx }}>{formatNetworkRate(chartStats.networkTx.avg)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Max:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.networkTx }}>{formatNetworkRate(chartStats.networkTx.max)}</span>
                  </div>
                </div>
              </div>
              <div className="mb-2 flex items-center justify-end gap-4 text-sm">
                <div className="flex items-center gap-2">
                  <div className="w-3 h-3 rounded" style={{ backgroundColor: CHART_COLORS.network }} />
                  <span className="text-muted-foreground">↓ Inbound (RX)</span>
                </div>
                <div className="flex items-center gap-2">
                  <div className="w-3 h-3 rounded" style={{ backgroundColor: CHART_COLORS.networkTx }} />
                  <span className="text-muted-foreground">↑ Outbound (TX)</span>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={370}>
                <AreaChart 
                  data={displayChartData}
                  margin={{ top: 10, right: 80, left: 20, bottom: 40 }}
                >
                  <defs>
                    <linearGradient id="networkRxGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_COLORS.network} stopOpacity={0.3}/>
                      <stop offset="95%" stopColor={CHART_COLORS.network} stopOpacity={0}/>
                    </linearGradient>
                    <linearGradient id="networkTxGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_COLORS.networkTx} stopOpacity={0.3}/>
                      <stop offset="95%" stopColor={CHART_COLORS.networkTx} stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis 
                    dataKey="time" 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    className="text-muted-foreground"
                    interval="preserveStartEnd"
                  />
                  <YAxis 
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    domain={networkDomain}
                    tickFormatter={networkTickFormatter}
                    className="text-muted-foreground"
                  />
                  <Tooltip
                    wrapperStyle={CHART_TOOLTIP_STYLE}
                    contentStyle={tooltipContentStyle}
                    formatter={networkTooltipFormatter}
                  />
                  <Area
                    type="monotone"
                    dataKey="networkRxRate"
                    stroke={CHART_COLORS.network}
                    fill="url(#networkRxGradient)"
                    strokeWidth={2}
                    name="networkRxRate"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="networkTxRate"
                    stroke={CHART_COLORS.networkTx}
                    fill="url(#networkTxGradient)"
                    strokeWidth={2}
                    name="networkTxRate"
                    isAnimationActive={false}
                  />
                  {/* Brush for pan/zoom */}
                  <Brush
                    dataKey="time"
                    height={30}
                    stroke="hsl(var(--primary))"
                    fill="hsl(var(--muted))"
                    startIndex={brushStartIndex}
                    endIndex={brushEndIndex}
                    onChange={handleBrushChange}
                    tickFormatter={identityTickFormatter}
                    padding={{ top: 0, bottom: 0, left: 20, right: 70 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      {/* Instance Distribution & Top Consumers */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle>
              Instance Distribution
            </CardTitle>
            <CardDescription>Click to view instance details</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-col md:flex-row items-center justify-around gap-4">
              <ResponsiveContainer width="100%" height={200}>
                <PieChart>
                  <Pie
                    data={instanceDistribution}
                    cx="50%"
                    cy="50%"
                    innerRadius={60}
                    outerRadius={80}
                    paddingAngle={5}
                    dataKey="value"
                  >
                    {instanceDistribution.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip wrapperStyle={CHART_TOOLTIP_STYLE} content={<PieChartTooltip />} />
                </PieChart>
              </ResponsiveContainer>
              {/* Clickable Legend */}
              <div className="flex flex-col justify-center min-w-[140px] space-y-1">
                {instanceDistribution.map((item) => (
                  <button
                    key={item.name}
                    onClick={() => fetchInstances(item.name.toLowerCase() as "running" | "stopped" | "other")}
                    className="flex items-center justify-between gap-3 py-2 px-2 rounded-lg hover:bg-muted transition-colors cursor-pointer text-left"
                  >
                    <div className="flex items-center gap-2">
                      {/* A legend key for the chart slice, so it takes the slice's own colour.
                          Square rather than round: a round swatch beside a state word reads as a
                          status dot, which this console does not use. note: not rounded-sm, which
                          tailwind.config.ts maps to 8px and renders a 12px square as a circle. */}
                      <div
                        aria-hidden="true"
                        className="h-3 w-3 shrink-0 rounded-[2px]"
                        style={{ backgroundColor: item.color }}
                      />
                      <span className="text-sm">{item.name}</span>
                    </div>
                    <span className="text-sm font-bold tabular-nums">{item.value}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="mt-4 pt-4 border-t grid grid-cols-2 gap-4">
              <button
                onClick={() => fetchInstances("container")}
                className="text-center p-3 rounded-lg hover:bg-muted transition-colors cursor-pointer"
              >
                <p className="text-2xl font-bold">{filteredStats?.container_count ?? data.container_count}</p>
                <p className="text-sm text-muted-foreground">Containers</p>
              </button>
              <button
                onClick={() => fetchInstances("vm")}
                className="text-center p-3 rounded-lg hover:bg-muted transition-colors cursor-pointer"
              >
                <p className="text-2xl font-bold">{filteredStats?.vm_count ?? data.vm_count}</p>
                <p className="text-sm text-muted-foreground">Virtual Machines</p>
              </button>
            </div>
          </CardContent>
        </Card>

        {/* Top Consumers: each resource ranked on its own */}
        <TopConsumersCard
          pool={consumerPool(dashboardData)}
          selectedNode={selectedNode}
          onOpen={openTopConsumer}
        />
      </div>

      {/* Alarm Statistics. Partner+ only: every tile below navigates to /inventory/warnings or
          /inventory/clusters/certificates, both of which useRequireRole(Role.PARTNER) bounces a
          tenant away from, so for a tenant this card is a wall of dead ends. */}
      {isPartner && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>
                Alarm Statistics
              </CardTitle>
              <CardDescription>System alerts and warnings overview</CardDescription>
            </div>
            {warningsCounts.total > 0 && (
              <Link 
                href={inventoryLinks.warnings({ node: selectedNode === "all" ? undefined : selectedNode })}
                className="text-sm text-primary hover:underline flex items-center gap-1"
              >
                View All ({warningsCounts.total})
                <ChevronRight className="h-4 w-4" />
              </Link>
            )}
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <Link 
                href={inventoryLinks.warnings({ severity: "critical", node: selectedNode === "all" ? undefined : selectedNode })}
                className="flex items-center gap-3 p-4 rounded-lg border bg-red-500/5 border-red-500/20 hover:bg-red-500/10 hover:border-red-500/40 transition-all cursor-pointer group"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-red-500/10 group-hover:bg-red-500/20 transition-colors">
                  <Flame className="h-5 w-5 text-red-500" />
                </div>
                <div>
                  <p className="text-2xl font-bold text-red-600">{warningsCounts.critical}</p>
                  <p className="text-xs text-muted-foreground">Critical</p>
                </div>
              </Link>
              <Link 
                href={inventoryLinks.warnings({ severity: "high", node: selectedNode === "all" ? undefined : selectedNode })}
                className="flex items-center gap-3 p-4 rounded-lg border bg-orange-500/5 border-orange-500/20 hover:bg-orange-500/10 hover:border-orange-500/40 transition-all cursor-pointer group"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-orange-500/10 group-hover:bg-orange-500/20 transition-colors">
                  <ShieldAlert className="h-5 w-5 text-orange-500" />
                </div>
                <div>
                  <p className="text-2xl font-bold text-orange-600">{warningsCounts.high}</p>
                  <p className="text-xs text-muted-foreground">Major</p>
                </div>
              </Link>
              <Link 
                href={inventoryLinks.warnings({ severity: "warning", node: selectedNode === "all" ? undefined : selectedNode })}
                className="flex items-center gap-3 p-4 rounded-lg border bg-yellow-500/5 border-yellow-500/20 hover:bg-yellow-500/10 hover:border-yellow-500/40 transition-all cursor-pointer group"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-yellow-500/10 group-hover:bg-yellow-500/20 transition-colors">
                  <AlertTriangle className="h-5 w-5 text-yellow-500" />
                </div>
                <div>
                  <p className="text-2xl font-bold text-yellow-600">{warningsCounts.warning}</p>
                  <p className="text-xs text-muted-foreground">Warning</p>
                </div>
              </Link>
              <Link 
                href={inventoryLinks.warnings({ severity: "info", node: selectedNode === "all" ? undefined : selectedNode })}
                className="flex items-center gap-3 p-4 rounded-lg border bg-blue-500/5 border-blue-500/20 hover:bg-blue-500/10 hover:border-blue-500/40 transition-all cursor-pointer group"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-500/10 group-hover:bg-blue-500/20 transition-colors">
                  <Info className="h-5 w-5 text-blue-500" />
                </div>
                <div>
                  <p className="text-2xl font-bold text-blue-600">{warningsCounts.info}</p>
                  <p className="text-xs text-muted-foreground">Info</p>
                </div>
              </Link>
            </div>
          
            {/* Certificate Issues & Inactive Nodes */}
            {(warningsCounts.certIssues > 0 || warningsCounts.inactiveNodes > 0) && (
              <div className="grid grid-cols-2 gap-4 mt-4">
                {warningsCounts.certIssues > 0 && (
                  <Link 
                    href="/inventory/clusters/certificates"
                    className="flex items-center gap-3 p-4 rounded-lg border border-primary/20 bg-primary/5 transition-all hover:border-primary/40 hover:bg-primary/10 cursor-pointer group"
                  >
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 transition-colors group-hover:bg-primary/20">
                      <Shield className="h-5 w-5 text-primary" />
                    </div>
                    <div>
                      <p className="text-2xl font-bold text-foreground">{warningsCounts.certIssues}</p>
                      <p className="text-xs text-muted-foreground">Cert Issues</p>
                    </div>
                  </Link>
                )}
                {warningsCounts.inactiveNodes > 0 && (
                  <Link 
                    href={inventoryLinks.warnings({ tab: "nodes", cluster: selectedNodeWarning ? warningClusterValue(selectedNodeWarning) : undefined })}
                    className="flex items-center gap-3 p-4 rounded-lg border bg-rose-500/5 border-rose-500/20 hover:bg-rose-500/10 hover:border-rose-500/40 transition-all cursor-pointer group"
                  >
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-rose-500/10 group-hover:bg-rose-500/20 transition-colors">
                      <WifiOff className="h-5 w-5 text-rose-500" />
                    </div>
                    <div>
                      <p className="text-2xl font-bold text-rose-600">{warningsCounts.inactiveNodes}</p>
                      <p className="text-xs text-muted-foreground">Inactive Clusters</p>
                    </div>
                  </Link>
                )}
              </div>
            )}
          
            {warningsCounts.total === 0 && (
              <div className="mt-4 text-center py-4 bg-green-500/5 rounded-lg border border-green-500/20">
                <p className="text-green-600 font-medium">✓ All systems operational - No active alarms</p>
              </div>
            )}
            {warningsCounts.total > 0 && (
              <div className="mt-4 text-xs text-muted-foreground">
                <span>Click any category to view details</span>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Cluster status cards */}
      <Card>
        <CardHeader>
          <CardTitle>
            Cluster Status
          </CardTitle>
          <CardDescription>
            Detailed metrics for each connected cluster
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {filteredNodes.map((node) => (
              <NodeCard key={node.node_name} node={node} />
            ))}
            {filteredNodes.length === 0 && (
              <div className="col-span-full text-center py-8 text-muted-foreground">
                <Server className="h-12 w-12 mx-auto mb-4 opacity-50" />
                <p>No clusters connected</p>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Storage Pools */}
      {data.storage_pools.length > 0 && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle>
                  Storage Pools
                </CardTitle>
                <CardDescription>
                  Storage pool utilization across all clusters
                </CardDescription>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setStoragePoolsSortDesc(!storagePoolsSortDesc)}
                className="flex items-center gap-1.5 h-8 px-2.5 sm:px-3 shrink-0"
              >
                {storagePoolsSortDesc ? (
                  <>
                    <ArrowDown className="h-4 w-4" />
                    <span className="text-xs">Highest</span>
                  </>
                ) : (
                  <>
                    <ArrowUp className="h-4 w-4" />
                    <span className="text-xs">Lowest</span>
                  </>
                )}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {[...data.storage_pools]
                .filter(pool => selectedNode === "all" || pool.node === selectedNode)
                .sort((a, b) => storagePoolsSortDesc 
                  ? (b.usage_percent || 0) - (a.usage_percent || 0)
                  : (a.usage_percent || 0) - (b.usage_percent || 0))
                .map((pool) => (
                <div
                  key={`${pool.node}-${pool.name}-${pool.driver}`}
                  className="space-y-1.5"
                >
                  <div className="flex items-center justify-between text-sm">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{pool.name}</span>
                      <Badge variant="outline" className="text-xs">{pool.driver}</Badge>
                      {/* note: `node` is the registered cluster. On a MicroCloud the bytes live
                          on the members, and a shared pool is one store all of them can see,
                          so the row says which rather than implying the cluster owns a disk.
                          Member hostnames are operator content, gated like status_message
                          above: a tenant is told the shape, not the names. */}
                      <span className="text-muted-foreground">
                        ({pool.node}
                        {pool.members && pool.members.length > 0
                          ? isPartner
                            ? `: ${pool.scope === "shared" ? "shared by " : ""}${pool.members.join(", ")}`
                            : pool.scope === "shared"
                              ? `, shared by ${pool.members.length} members`
                              : `, ${pool.members.length} members`
                          : ""}
                        )
                      </span>
                    </div>
                    <span className="font-medium">
                      {formatBytes(pool.used_bytes)} / {formatBytes(pool.total_bytes)}
                    </span>
                  </div>
                  <Progress value={pool.usage_percent} className="h-2" />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Collection Time Footer */}
      <div className="text-center text-xs text-muted-foreground">
        <Clock className="inline-block h-3 w-3 mr-1" />
        Data collection took {(data.collection_time_ms / 1000).toFixed(2)}s
      </div>

      {/* Instance Details Dialog */}
      <InstanceDetailsDialog
        open={monitoringUiIsCurrent && instanceDialogOpen}
        onClose={closeInstanceList}
        filterType={instanceFilterType}
        instances={monitoringUiIsCurrent ? allInstances : []}
        isLoading={monitoringUiIsCurrent && instancesLoading}
        onSelectInstance={(instance) => {
          // Close the list before opening the detail. Stacking one modal on another is the
          // pattern being removed from member-resource-modal, not one to copy.
          closeInstanceList();
          setMonitoringUiActorScope(actorScope);
          setSelectedListInstance(instance);
        }}
      />

      <InstanceDetailDialog
        instance={monitoringUiIsCurrent ? selectedListInstance ?? selectedTopConsumerVm : null}
        isOpen={monitoringUiIsCurrent && (!!selectedListInstance || !!selectedTopConsumer)}
        hydrateDetail={monitoringUiIsCurrent && !selectedListInstance && !!selectedTopConsumer}
        onClose={() => {
          setSelectedListInstance(null);
          setSelectedTopConsumer(null);
        }}
        project={
          monitoringUiIsCurrent
            ? selectedListInstance?.project || selectedTopConsumer?.project || "default"
            : "default"
        }
      />
    </div>
  );
}
