"use client";

import { useState, useEffect, useMemo, useCallback, memo, useTransition, useRef, type ComponentType } from "react";
import { useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";

import { Server, Cpu, HardDrive, MemoryStick, Network, AlertTriangle, RefreshCw, TrendingUp, TrendingDown, Minus, Pause, Play, Clock, ZoomIn, ZoomOut, ChevronLeft, ChevronRight, RotateCcw } from "lucide-react";

// Dynamically import Lottie to avoid SSR issues
const Lottie = dynamic(() => import("lottie-react"), { ssr: false });
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend, Brush } from "recharts";

import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { NodeFilterSelect } from "@/components/monitoring/node-filter-select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip as UITooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { InstanceDetailDialog } from "@/components/instance-detail/instance-detail-dialog";
import {
  InstanceDistributionDialog,
  filterDistribution,
  type DistributionFilter,
} from "./components/instance-distribution-dialog";
import { queryKeys, useInstances, useMonitoringDashboard, useMonitoringHistory, useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { useMonitoringWebSocket } from "@/hooks/use-monitoring-ws";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useClusterLabel } from "@/hooks/use-node-picker-options";
import { useByteFormat } from "@/hooks/use-byte-format";
import { BYTE_SCALES } from "@/lib/byte-units";
import { cn, plural } from "@/lib/utils";
import type { IllustrationProps } from "@/components/illustrations/illustration-frame";
import { ClustersIllustration, InstancesIllustration, RunningIllustration, WarningsIllustration } from "@/components/illustrations/scenes/kpi";
import { DisconnectedIllustration } from "@/components/illustrations/scenes/states";
import type { InstanceMonitoringSummary, VMInfo, VMListResponse } from "@/types";
import type {
  MetricsHistoryPoint,
  MonitoringDashboardResponse,
  SystemWarningsResponse,
} from "@/types/api";

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

interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  progress?: number;
  className?: string;
  /** The tile's subject; it drops out when the tile is too narrow (illustrations.css, .illo-kpi). */
  illustration: ComponentType<IllustrationProps>;
}

// No trend arrow: on some tiles a rise is bad news (critical warnings), so a green arrow misleads.
const MetricCard = memo(function MetricCard({ 
  title, 
  value, 
  subtitle, 
  progress,
  className,
  illustration: Illustration,
}: MetricCardProps) {
  return (
    <Card className={cn("illo-kpi", className)}>
      <CardContent className="p-6">
        <div className="space-y-2">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0 space-y-2">
              <p className="text-sm font-medium text-muted-foreground">{title}</p>
              <p className="text-2xl sm:text-3xl font-bold tracking-tight tabular-nums">{value}</p>
              {subtitle && <p className="text-xs text-muted-foreground">{subtitle}</p>}
            </div>
            <Illustration size="sm" />
          </div>
          {progress !== undefined && (
            <Progress value={progress} className="h-2 mt-2" />
          )}
        </div>
      </CardContent>
    </Card>
  );
});

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
        <TrendingDown className="h-3 w-3 sm:h-3.5 sm:w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
        <span className="text-[10px] sm:text-xs text-muted-foreground">Min:</span>
        <span className="text-xs sm:text-sm font-semibold" style={{ color }}>{min.toFixed(2)}{unit}</span>
      </div>
      <div className="flex items-center gap-1 sm:gap-2">
        <Minus className="h-3 w-3 sm:h-3.5 sm:w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
        <span className="text-[10px] sm:text-xs text-muted-foreground">Avg:</span>
        <span className="text-xs sm:text-sm font-semibold" style={{ color }}>{avg.toFixed(2)}{unit}</span>
      </div>
      <div className="flex items-center gap-1 sm:gap-2">
        <TrendingUp className="h-3 w-3 sm:h-3.5 sm:w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
        <span className="text-[10px] sm:text-xs text-muted-foreground">Max:</span>
        <span className="text-xs sm:text-sm font-semibold" style={{ color }}>{max.toFixed(2)}{unit}</span>
      </div>
    </div>
  );
});

// A lightweight skeleton rather than the Lottie animation.
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

import type { CpuUsageReading } from "@/components/monitoring/cpu-usage-gauge";
import { TopConsumersCard, consumerPool } from "@/components/monitoring/top-consumers-card";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { STALE_AFTER_MS } from "@/lib/query-config";
import {
  DEFAULT_HISTORY_WINDOW_SECONDS,
  HISTORY_WINDOWS,
  collectedAtOf,
  toChartPoints,
} from "@/lib/monitoring-history";
import { NodeCard } from "@/components/monitoring/node-card";
import { platformWarnings } from "@/lib/alarm-counts";
import { AlarmStatisticsCard } from "./components/alarm-statistics-card";
import { ResourceUtilizationCard, capacityScope } from "./components/resource-utilization-card";
import { StoragePoolsCard } from "./components/storage-pools-card";

const CHART_COLORS = {
  cpu: "#3b82f6",
  memory: "#10b981",
  storage: "#f59e0b",
  storageRead: "#f59e0b",
  storageWrite: "#ef4444",
  network: "#8b5cf6", // RX (in)
  networkTx: "#ec4899", // TX (out)
};

const PIE_COLORS = ["#10b981", "#ef4444", "#6b7280"];

// Lifts recharts tooltips above the chart Legend. The Legend is a sibling div rendered after
// the tooltip wrapper, so without this it paints over a tall tooltip.
const CHART_TOOLTIP_STYLE = { zIndex: 50 };

// The chart tooltips live at module level so their identity is stable across renders.
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

const MemoryChartTooltip = ({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) => {
  const { formatBytes } = useByteFormat();
  if (active && payload && payload.length) {
    const data = payload[0].payload;
    return (
      <div className="bg-card border rounded-lg shadow-lg p-3">
        <p className="text-xs text-muted-foreground mb-1">{label}</p>
        <p className="text-sm font-medium" style={{ color: CHART_COLORS.memory }}>
          Memory Usage: {data.memory.toFixed(1)}%
        </p>
        {data.memoryUsedBytes > 0 && (
          <p className="text-xs text-muted-foreground mt-1">
            Used: {formatBytes(data.memoryUsedBytes)}
          </p>
        )}
      </div>
    );
  }
  return null;
};

const StorageChartTooltip = ({ active, payload, label, totalStorageBytes }: { active?: boolean; payload?: any[]; label?: string; totalStorageBytes?: number }) => {
  const { formatBytes } = useByteFormat();
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

// Resource chart tooltip. A cluster with no reading at a point shows as offline rather than 0%.
interface ResourceMetricsTooltipProps {
  active?: boolean;
  payload?: any[];
  label?: string;
  metricType: "cpu" | "memory" | "storage";
  nodeStatusMap?: Record<string, { status: string; statusMessage?: string; isOnline: boolean }>;
  nodeNames?: string[];
  selectedNode?: string;
  /** The cluster's display name, from useClusterLabel. */
  labelOf?: (nodeName: string) => string;
}

const ResourceMetricsTooltip = memo(function ResourceMetricsTooltip({ 
  active, 
  payload, 
  label, 
  metricType, 
  nodeStatusMap = {},
  nodeNames = [],
  selectedNode = "all",
  labelOf = (nodeName) => nodeName,
}: ResourceMetricsTooltipProps) {
  const { formatBytes } = useByteFormat();
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

  // Connectivity at a point in the past is inferred from the data point: a node with no
  // non-zero value for any metric at this timestamp was offline then rather than at 0%.
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
    // Many clusters make this tooltip tall: past eight it switches to a two-column grid, and the
    // height is bounded so it never covers the legend below the chart.
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
                  <span className="text-sm truncate">{labelOf(nodeName)}</span>
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
          Used: {formatBytes(data.memoryUsedBytes)}
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

// Downsamples to targetPoints: the first and last point, plus each bucket's peak point.
function downsampleData<T extends Record<string, any>>(
  data: T[], 
  targetPoints: number, 
): T[] {
  if (data.length <= targetPoints) return data;
  
  const result: T[] = [];
  const bucketSize = (data.length - 2) / (targetPoints - 2);
  
  result.push(data[0]);
  
  for (let i = 0; i < targetPoints - 2; i++) {
    const bucketStart = Math.floor((i) * bucketSize) + 1;
    const bucketEnd = Math.min(Math.floor((i + 1) * bucketSize) + 1, data.length - 1);
    
    // The point with the highest value across ALL metrics, so a spike in any one of them
    // survives whichever chart tab is active.
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
  
  result.push(data[data.length - 1]);
  
  return result;
}

const ALARM_COLORS = {
  critical: "#dc2626",
  major: "#ea580c",
  warning: "#eab308",
  info: "#3b82f6",
};

// Node colors for multi-line chart (when "All Nodes" selected)
const NODE_COLORS = [
  "#8b5cf6",
  "#06b6d4",
  "#10b981",
  "#f59e0b",
  "#ef4444",
  "#ec4899",
  "#6366f1",
  "#84cc16",
];

export default function MonitoringContent() {
  // important: this page has no role gate, but a Tenant reads only its own instances and the
  // clusters' status. The backend sends it every host figure as null (core/host_detail.py
  // monitoring_dashboard_for_tier) and refuses it the host history, so the utilization gauges,
  // the history charts and the warnings tile are operator content here, along with
  // status_message (the backend exception verbatim, see monitoring.py).
  const { isPartner } = usePermissions();
  const { scopeKey: actorScope } = useActorQueryScope();
  // Display names, as the Clusters page shows them; the id stays the value every filter uses.
  const labelOf = useClusterLabel();
  const fmt = useByteFormat();
  const { formatNetworkRate } = fmt;
  const queryClient = useQueryClient();
  const [isPaused, setIsPaused] = useState(false);
  const [pausedSnapshot, setPausedSnapshot] = useState<MonitoringDisplaySnapshot | null>(null);
  const [timeRange, setTimeRange] = useState<number>(DEFAULT_HISTORY_WINDOW_SECONDS);
  const [chartTab, setChartTab] = useState<"cpu" | "memory" | "storage" | "network">("cpu");

  const [instanceDialogOpen, setInstanceDialogOpen] = useState(false);
  const [instanceFilterType, setInstanceFilterType] = useState<DistributionFilter | null>(null);
  const [selectedTopConsumer, setSelectedTopConsumer] = useState<InstanceMonitoringSummary | null>(null);
  // A row picked out of the instance list dialog. Kept separate from the top-consumer path
  // because that one holds a monitoring summary and has to convert; this one already has the
  // VMInfo in hand.
  const [selectedListInstance, setSelectedListInstance] = useState<VMInfo | null>(null);
  const [monitoringUiActorScope, setMonitoringUiActorScope] = useState(actorScope);
  const previousActorScopeRef = useRef(actorScope);
  const activeActorScopeRef = useRef(actorScope);
  activeActorScopeRef.current = actorScope;

  // This route can survive a sign-out/sign-in transition without unmounting. Actor-owned UI
  // state is gated during render, then cleared after the change so prior organization rows or
  // node names cannot flash before the new actor's queries settle.
  const monitoringUiIsCurrent = monitoringUiActorScope === actorScope;

  const [selectedNodeState, setSelectedNode] = useState<string>("all");
  const selectedNode = monitoringUiIsCurrent ? selectedNodeState : "all";

  // Visible nodes for multi-line graph (when "all" is selected)
  const [visibleNodesState, setVisibleNodes] = useState<Record<string, boolean>>({});
  const visibleNodes = monitoringUiIsCurrent ? visibleNodesState : {};
  
  // Chart zoom and pan. The brush indices are always numbers, never undefined, so the Recharts
  // Brush stays controlled.
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
    previousActorScopeRef.current = actorScope;
    setMonitoringUiActorScope(actorScope);
    setInstanceDialogOpen(false);
    setInstanceFilterType(null);
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

  // Transition for non-blocking time range switches
  const [isTimeRangePending, startTimeRangeTransition] = useTransition();
  
  // Length of the downsampled chart data; the zoom handlers compute brush indices against it.
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
  } = useMonitoringHistory(timeRange, !isPaused && isPartner);

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
      // Marked stale rather than refetched: the observers are still disabled at this point.
      // Once Resume re-enables them, a stale query reads at once instead of waiting out the
      // 30 s interval on the paused snapshot.
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
      ...(isPartner
        ? [refetchHistory({ throwOnError: true }), refreshWarnings(), refreshSystemWarnings()]
        : []),
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
        // Each section keeps its last good data when another endpoint fails; the rejected
        // refresh still surfaces through RefreshButton below.
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

  // A consumer row carries only the summary its chart needs, so the detail dialog normally waits
  // for one detail read. When the instance list is already cached it holds the full record, and
  // the dialog opens from that without waiting.
  const openTopConsumer = useCallback((instance: InstanceMonitoringSummary) => {
    setMonitoringUiActorScope(actorScope);
    const project = instance.project || "default";
    const cached = queryClient
      .getQueryData<VMListResponse>(queryKeys.instancesFor(actorScope))
      ?.vms.find(
        (vm) => vm.host === instance.host && (vm.project || "default") === project && vm.name === instance.name,
      );
    if (cached) setSelectedListInstance(cached);
    else setSelectedTopConsumer(instance);
  }, [actorScope, queryClient]);


  // The distribution list reads the shared instance cache that Overview and Instances keep warm,
  // so a click shows rows at once. A forced live read waits for the slowest cluster, so only the
  // dialog's Refresh does one. The query runs while the page is open, which keeps the server
  // cache warm for the first click; Pause stops it with the rest of the page.
  const instanceList = useInstances(undefined, {
    enabled: monitoringUiIsCurrent && (!isPaused || instanceDialogOpen),
  });
  const instanceListData = monitoringUiIsCurrent ? instanceList.data : undefined;
  const distributionRows = useMemo(
    () =>
      instanceFilterType && instanceListData
        ? filterDistribution(instanceListData.vms, instanceFilterType, selectedNode)
        : [],
    [instanceListData, instanceFilterType, selectedNode],
  );

  const openInstanceList = (filter: DistributionFilter) => {
    setMonitoringUiActorScope(actorScope);
    setInstanceFilterType(filter);
    setInstanceDialogOpen(true);
  };

  const closeInstanceList = () => setInstanceDialogOpen(false);

  // Node names in dashboard order, for the per-cluster chart lines and legends.
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
  
  const toggleNodeVisibility = (nodeName: string) => {
    setVisibleNodes(prev => ({
      ...prev,
      [nodeName]: !prev[nodeName]
    }));
  };
  
  // Per-node status, handed to the chart tooltips.
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
  
  // The zoom and pan handlers read displayDataLengthRef, since the Brush works on
  // displayChartData (downsampled to at most 200 points) rather than on chartData.
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
  
  // Nodes for the Cluster Status cards.
  const filteredNodes = useMemo(() => {
    if (!dashboardData?.nodes) return [];
    if (selectedNode === "all") return dashboardData.nodes;
    return dashboardData.nodes.filter(n => n.node_name === selectedNode);
  }, [dashboardData?.nodes, selectedNode]);
  
  // Stats for the selected node, or for every node. chartData reads its totals, so it comes first.
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
        // The ?? 0 fallbacks below never reach a screen: a Tenant's host figures are null and
        // hostVisible is false, so no section that shows them renders. The two percentages
        // stay null, since a missing total is not 0%, and the gauges say so.
        hostVisible: dashboardData.total_cpu !== null,
        cpu_usage_percent: singleNode?.cpu_usage_percent ?? dashboardData.cpu_usage_percent ?? 0,
        cpu_reading: {
          status: dashboardData.cpu_status ?? "unavailable",
          busyPercent: dashboardData.cpu_busy_percent ?? null,
          busyThreads: dashboardData.cpu_busy_threads ?? null,
          threads: dashboardData.cpu_reporting_threads ?? dashboardData.total_cpu ?? 0,
          reportingInstances: dashboardData.cpu_reporting_instances ?? 0,
          runningInstances: dashboardData.running_instances,
        } as CpuUsageReading,
        memory_usage_percent: dashboardData.memory_usage_percent ?? null,
        storage_usage_percent: dashboardData.storage_usage_percent ?? null,
        total_cpu: dashboardData.total_cpu ?? 0,
        used_cpu: dashboardData.used_cpu ?? 0,
        total_memory_bytes: dashboardData.total_memory_bytes ?? 0,
        used_memory_bytes: dashboardData.used_memory_bytes ?? 0,
        total_storage_bytes: dashboardData.total_storage_bytes ?? 0,
        used_storage_bytes: dashboardData.used_storage_bytes ?? 0,
        total_gpu: dashboardData.total_gpu ?? 0,
        total_gpu_cards: dashboardData.total_gpu_cards ?? 0,
        total_gpu_display_cards: dashboardData.total_gpu_display_cards ?? undefined,
        total_gpu_integrated_cards: dashboardData.total_gpu_integrated_cards ?? undefined,
        used_gpu: dashboardData.used_gpu ?? 0,
        warnings: dashboardData.warnings,
        cluster_status: singleNode?.cluster_status,
        status_message: singleNode?.status_message,
      };
    }
    
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
      hostVisible: node.cpu_total !== null,
      cpu_usage_percent: node.cpu_usage_percent || 0,
      cpu_reading: {
        status: node.is_online ? node.cpu_status ?? "unavailable" : "unavailable",
        busyPercent: node.cpu_busy_percent ?? null,
        busyThreads: node.cpu_busy_threads ?? null,
        threads: node.cpu_total || 0,
        reportingInstances: node.cpu_reporting_instances ?? 0,
        runningInstances: node.running_instances || 0,
      } as CpuUsageReading,
      memory_usage_percent: node.memory_usage_percent ?? null,
      storage_usage_percent: node.storage_usage_percent ?? null,
      total_cpu: node.cpu_total || 0,
      used_cpu: node.cpu_used || 0,
      total_memory_bytes: node.memory_total_bytes || 0,
      used_memory_bytes: node.memory_used_bytes || 0,
      total_storage_bytes: node.storage_total_bytes || 0,
      used_storage_bytes: node.storage_used_bytes || 0,
      total_gpu: node.gpu_total || 0,
      total_gpu_cards: node.gpu_cards_total ?? undefined,
      total_gpu_display_cards: node.gpu_display_total ?? undefined,
      total_gpu_integrated_cards: node.gpu_integrated_total ?? undefined,
      used_gpu: node.gpu_used || 0,
      warnings: dashboardData.warnings, // Keep global warnings
      // Status message for troubleshooting when metrics are 0
      cluster_status: node.cluster_status,
      status_message: node.status_message,
    };
  }, [dashboardData, selectedNode]);

  // Stable memory and storage totals, so chartData does not recompute on every dashboard poll.
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
  
  // At most 200 points keeps hover and tooltips smooth over long ranges. Independent of
  // chartTab, so a tab switch does not downsample again.
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
    
    // Round axis ceilings, in percent.
    const thresholds = [5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100];
    
    const paddedMax = maxValue * (1 + padding);
    
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
    // Steps in the account's byte base, so the axis labels land on whole units in either system.
    const base = BYTE_SCALES[fmt.units].base;
    const mega = base * base;
    if (!chartData.length) return [0, 10 * mega];
    const rxValues = chartData.map(d => d.networkRxRate).filter(v => typeof v === 'number');
    const txValues = chartData.map(d => d.networkTxRate).filter(v => typeof v === 'number');
    const allValues = [...rxValues, ...txValues];

    if (allValues.length === 0) return [0, 10 * mega];

    const maxValue = Math.max(...allValues);
    const paddedMax = maxValue * 1.2;

    const thresholds = [
      mega,
      2 * mega,
      5 * mega,
      10 * mega,
      20 * mega,
      50 * mega,
      100 * mega,
      200 * mega,
      500 * mega,
      base * mega,
    ];
    let niceCeiling = 10 * mega;
    for (const threshold of thresholds) {
      if (paddedMax <= threshold) {
        niceCeiling = threshold;
        break;
      }
    }

    // For very small values, show at least one mega step per second.
    if (maxValue < 100 * base) {
      niceCeiling = mega;
    }

    return [0, niceCeiling];
  }, [chartData, fmt.units]);

  // Stable tick formatters to avoid Recharts re-renders from new function refs
  const percentTickFormatter = useCallback((v: number) => `${v}%`, []);
  const identityTickFormatter = useCallback((value: string) => value, []);
  const networkTickFormatter = useCallback((v: number) => formatNetworkRate(v, 1), [formatNetworkRate]);
  const networkTooltipFormatter = useCallback((value: number | string | undefined, name?: string) => {
    const numValue = typeof value === 'number' ? value : 0;
    const label = name === "networkRxRate" ? "↓ Inbound" : "↑ Outbound";
    return [formatNetworkRate(numValue), label];
  }, [formatNetworkRate]);
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

    // Single node selected: aggregate values.
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

  // Pie chart slices, from filteredStats so they follow the node selection.
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
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              icon={AlertTriangle}
              illustration={DisconnectedIllustration}
              title="Failed to load monitoring data"
              description={dashboardError instanceof Error ? dashboardError.message : "Unknown error"}
              action={
                <Button variant="outline" size="sm" onClick={() => void refreshMonitoring()}>
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Retry
                </Button>
              }
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  const data = dashboardData;
  if (!data) return null;

  return (
    <div className="space-y-6 p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl md:text-3xl font-bold tracking-tight">Monitoring</h1>
          <p className="text-muted-foreground">
            {isPartner
              ? "Resource metrics across all clusters"
              : "Your instances and the status of the clusters they run on"}
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
          <NodeFilterSelect
            nodes={dashboardData?.nodes ?? []}
            value={selectedNode}
            onValueChange={setSelectedNode}
            labelOf={labelOf}
          />
        </div>
      </div>

      {/* Two a row until there is room for all of them: at md the sidebar would leave four tiles
          about 92px wide, which clips "Instances". */}
      {filteredStats && (
      <div className={`grid grid-cols-2 gap-4 ${isPartner ? "xl:grid-cols-4" : "lg:grid-cols-3"}`}>
        <MetricCard
          title="Total Instances"
          illustration={InstancesIllustration}
          value={filteredStats.total_instances}
          subtitle={selectedNode === "all" ? `${plural(filteredStats.container_count, "container")} · ${plural(filteredStats.vm_count, "VM")}` : `on ${labelOf(selectedNode)}`}
        />
        <MetricCard
          title="Running"
          illustration={RunningIllustration}
          value={filteredStats.running_instances}
          subtitle={`${filteredStats.stopped_instances} stopped`}
          progress={(filteredStats.running_instances / Math.max(filteredStats.total_instances, 1)) * 100}
        />
        <MetricCard
          title="Clusters Online"
          illustration={ClustersIllustration}
          value={`${filteredStats.online_nodes} / ${filteredStats.total_nodes}`}
          subtitle={filteredStats.inactive_nodes > 0 ? `${filteredStats.inactive_nodes} offline` : "All clusters healthy"}
        />
        {isPartner && (
          <MetricCard
            title="Warnings"
            illustration={WarningsIllustration}
            value={filteredStats.warnings?.total || 0}
            subtitle={filteredStats.warnings?.critical ? `${filteredStats.warnings.critical} critical` : "No critical issues"}
          />
        )}
      </div>
      )}

      {filteredStats?.hostVisible && (
        <ResourceUtilizationCard
          stats={filteredStats}
          scope={capacityScope(data, selectedNode)}
          scopeLabel={selectedNode === "all" ? "all" : labelOf(selectedNode)}
          isPartner={isPartner}
        />
      )}

      {/* The hosts' load over time is operator content (see the top of MonitoringContent). */}
      {isPartner && (
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
                      handleResetZoom();
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
          <Tabs value={chartTab} onValueChange={(v) => setChartTab(v as "cpu" | "memory" | "storage" | "network")} className="mb-4">
            {(isTimeRangePending || historyFetching) && (
              <div className="inline-flex items-center gap-1.5 ml-3 align-middle">
                <RefreshCw className="h-3 w-3 animate-spin text-primary" />
                <span className="text-xs text-muted-foreground">Updating…</span>
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
            
            <TabsContent value="cpu" className="mt-4">
              <ChartStats 
                min={chartStats.cpu.min} 
                max={chartStats.cpu.max} 
                avg={chartStats.cpu.avg} 
                unit="%" 
                color={CHART_COLORS.cpu} 
              />
              {selectedNode === "all" && nodeNames.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 mb-3">
                  <span className="text-[10px] sm:text-xs text-muted-foreground">Show:</span>
                  {nodeNames.map((nodeName, idx) => (
                    <Button
                      key={nodeName}
                      variant={visibleNodes[nodeName] ? "default" : "outline"}
                      size="sm"
                      className="h-5 sm:h-6 rounded px-1.5 sm:px-2 text-[10px] sm:text-xs"
                      style={{
                        backgroundColor: visibleNodes[nodeName] ? NODE_COLORS[idx % NODE_COLORS.length] : undefined,
                        borderColor: NODE_COLORS[idx % NODE_COLORS.length],
                        color: visibleNodes[nodeName] ? "white" : NODE_COLORS[idx % NODE_COLORS.length],
                      }}
                      onClick={() => toggleNodeVisibility(nodeName)}
                    >
                      {labelOf(nodeName)}
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
                        labelOf={labelOf}
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
            
            <TabsContent value="memory" className="mt-4">
              <ChartStats 
                min={chartStats.memory.min} 
                max={chartStats.memory.max} 
                avg={chartStats.memory.avg} 
                unit="%" 
                color={CHART_COLORS.memory} 
              />
              {selectedNode === "all" && nodeNames.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 mb-3">
                  <span className="text-[10px] sm:text-xs text-muted-foreground">Show:</span>
                  {nodeNames.map((nodeName, idx) => (
                    <Button
                      key={nodeName}
                      variant={visibleNodes[nodeName] ? "default" : "outline"}
                      size="sm"
                      className="h-5 sm:h-6 rounded px-1.5 sm:px-2 text-[10px] sm:text-xs"
                      style={{
                        backgroundColor: visibleNodes[nodeName] ? NODE_COLORS[idx % NODE_COLORS.length] : undefined,
                        borderColor: NODE_COLORS[idx % NODE_COLORS.length],
                        color: visibleNodes[nodeName] ? "white" : NODE_COLORS[idx % NODE_COLORS.length],
                      }}
                      onClick={() => toggleNodeVisibility(nodeName)}
                    >
                      {labelOf(nodeName)}
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
                        labelOf={labelOf}
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
            
            <TabsContent value="storage" className="mt-4">
              <ChartStats 
                min={chartStats.storage.min} 
                max={chartStats.storage.max} 
                avg={chartStats.storage.avg} 
                unit="%" 
                color={CHART_COLORS.storage} 
              />
              {selectedNode === "all" && nodeNames.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 mb-3">
                  <span className="text-[10px] sm:text-xs text-muted-foreground">Show:</span>
                  {nodeNames.map((nodeName, idx) => (
                    <Button
                      key={nodeName}
                      variant={visibleNodes[nodeName] ? "default" : "outline"}
                      size="sm"
                      className="h-5 sm:h-6 rounded px-1.5 sm:px-2 text-[10px] sm:text-xs"
                      style={{
                        backgroundColor: visibleNodes[nodeName] ? NODE_COLORS[idx % NODE_COLORS.length] : undefined,
                        borderColor: NODE_COLORS[idx % NODE_COLORS.length],
                        color: visibleNodes[nodeName] ? "white" : NODE_COLORS[idx % NODE_COLORS.length],
                      }}
                      onClick={() => toggleNodeVisibility(nodeName)}
                    >
                      {labelOf(nodeName)}
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
                        labelOf={labelOf}
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
            
            <TabsContent value="network" className="mt-4">
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
      )}

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
              <div className="flex flex-col justify-center min-w-[140px] space-y-1">
                {instanceDistribution.map((item) => (
                  <button
                    key={item.name}
                    onClick={() => openInstanceList(item.name.toLowerCase() as DistributionFilter)}
                    className="flex items-center justify-between gap-3 py-2 px-2 rounded-lg hover:bg-muted transition-colors cursor-pointer text-left"
                  >
                    <div className="flex items-center gap-2">
                      {/* A legend key for the chart slice, so it takes the slice's own colour.
                          Square, because a round swatch beside a state word reads as a status dot.
                          Not rounded-sm: tailwind.config.ts maps it to 8px, which turns a 12px
                          square into a circle. */}
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
                onClick={() => openInstanceList("container")}
                className="text-center p-3 rounded-lg hover:bg-muted transition-colors cursor-pointer"
              >
                <p className="text-2xl font-bold">{filteredStats?.container_count ?? data.container_count}</p>
                <p className="text-sm text-muted-foreground">Containers</p>
              </button>
              <button
                onClick={() => openInstanceList("vm")}
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

      {/* Partner+ only: every tile navigates to /inventory/warnings or
          /inventory/clusters/certificates, and useRequireRole(Role.PARTNER) bounces a Tenant
          away from both. */}
      {isPartner && (
        <AlarmStatisticsCard
          warnings={platformWarnings(warningsData)}
          system={systemWarnings}
          selectedNode={selectedNode}
          loaded={warningsData !== undefined && systemWarnings !== undefined}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle>
            Cluster Status
          </CardTitle>
          <CardDescription>
            {isPartner
              ? "Detailed metrics for each connected cluster"
              : "Status of each cluster and your instances on it"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {filteredNodes.map((node) => (
              <NodeCard key={node.node_name} node={node} displayName={labelOf(node.node_name)} />
            ))}
            {filteredNodes.length === 0 && (
              <EmptyState icon={Server} title="No clusters connected" className="col-span-full py-8" />
            )}
          </div>
        </CardContent>
      </Card>

      {/* Storage Pools: host detail, so absent for a Tenant like the gauges above. Shown with
          an empty state rather than dropped when no pool is reported, so the page does not
          change shape under an operator whose clusters went offline. */}
      {filteredStats?.hostVisible && (
        <StoragePoolsCard pools={data.storage_pools} selectedNode={selectedNode} labelOf={labelOf} />
      )}

      <div className="text-center text-xs text-muted-foreground">
        <Clock className="inline-block h-3 w-3 mr-1" />
        Data collection took {(data.collection_time_ms / 1000).toFixed(2)}s
      </div>

      <InstanceDistributionDialog
        open={monitoringUiIsCurrent && instanceDialogOpen}
        onClose={closeInstanceList}
        filter={instanceFilterType}
        instances={distributionRows}
        clusterLabel={selectedNode === "all" ? null : labelOf(selectedNode)}
        // A retry after a failure shows as loading again.
        isLoading={!instanceListData && (!instanceList.isError || instanceList.isFetching)}
        error={!instanceListData && !instanceList.isFetching ? instanceList.error : null}
        onRetry={() => void instanceList.refetch()}
        onRefresh={() => instanceList.refreshNow()}
        lastUpdatedAt={instanceListData ? instanceList.dataUpdatedAt : null}
        unreachableNodes={instanceListData?.unreachable_nodes}
        nodeErrors={instanceListData?.node_errors}
        onSelectInstance={(instance) => {
          // Close the list before opening the detail; modals do not stack.
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
