"use client";

import { useState, useEffect, useMemo } from "react";
import dynamic from "next/dynamic";
import {
  Activity,
  Server,
  Cpu,
  HardDrive,
  Network,
  AlertTriangle,
  AlertCircle,
  Container,
  Monitor,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  Minus,
  Zap,
  Database,
  Box,
  Play,
  Square,
  Circle,
  Clock,
  X,
  ArrowDownToLine,
  ArrowUpFromLine,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Bell,
  Info,
  ShieldAlert,
  Flame,
} from "lucide-react";

// Dynamically import Lottie to avoid SSR issues
const Lottie = dynamic(() => import("lottie-react"), { ssr: false });
import {
  LineChart,
  Line,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  Legend,
} from "recharts";
import { format } from "date-fns";

import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip as UITooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useMonitoringDashboard, useMonitoringHistory } from "@/hooks/use-lxd";
import { cn } from "@/lib/utils";
import { MonitoringDashboardResponse, MetricsHistoryPoint, ClusterMonitoringMetrics, InstanceMonitoringSummary, VMInfo } from "@/types";
import { vmAPI } from "@/lib/api";

// Format CPU time from nanoseconds to readable format
const formatCpuTime = (nanoseconds: number): string => {
  if (nanoseconds === 0) return "0s";
  const seconds = nanoseconds / 1e9;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes.toFixed(1)}m`;
  const hours = minutes / 60;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  const days = hours / 24;
  if (days < 365) return `${days.toFixed(0)}d`;
  const years = days / 365;
  return `${years.toFixed(1)}y`;
};

// Format bytes to human readable
const formatBytes = (bytes: number, decimals = 2): string => {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(decimals))} ${sizes[i]}`;
};

// Format bytes per second
const formatBytesPerSec = (bytes: number): string => {
  return `${formatBytes(bytes)}/s`;
};

// Format network speed (MB/s) to human readable with auto-scaling
const formatNetworkSpeed = (mbps: number, decimals = 2): string => {
  if (mbps === 0) return "0 B/s";
  if (mbps < 0.001) return `${(mbps * 1024 * 1024).toFixed(decimals)} B/s`;
  if (mbps < 1) return `${(mbps * 1024).toFixed(decimals)} KB/s`;
  if (mbps < 1024) return `${mbps.toFixed(decimals)} MB/s`;
  if (mbps < 1024 * 1024) return `${(mbps / 1024).toFixed(decimals)} GB/s`;
  return `${(mbps / 1024 / 1024).toFixed(decimals)} TB/s`;
};

// Smart format bytes - automatically chooses best unit
const formatBytesAuto = (bytes: number): { value: string; unit: string; rawValue: number } => {
  if (bytes === 0) return { value: "0", unit: "B", rawValue: 0 };
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  const value = bytes / Math.pow(k, i);
  return { value: value.toFixed(2), unit: sizes[i], rawValue: value };
};

// Convert bytes to specific unit
const bytesToUnit = (bytes: number, unit: "B" | "KB" | "MB" | "GB" | "TB"): number => {
  const units = { B: 0, KB: 1, MB: 2, GB: 3, TB: 4 };
  return bytes / Math.pow(1024, units[unit]);
};

// Metric Card Component
interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: React.ReactNode;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
  color?: string;
  progress?: number;
  className?: string;
}

function MetricCard({ 
  title, 
  value, 
  subtitle, 
  icon, 
  trend, 
  trendValue, 
  color = "text-blue-500",
  progress,
  className 
}: MetricCardProps) {
  return (
    <Card className={cn("transition-all hover:shadow-md", className)}>
      <CardContent className="p-6">
        <div className="flex items-start justify-between">
          <div className="space-y-2 flex-1">
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
          <div className={cn("flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-muted", color)}>
            {icon}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// Gauge Chart Component
interface GaugeChartProps {
  value: number;
  max?: number;
  label: string;
  color?: string;
  size?: number;
}

function GaugeChart({ value, max = 100, label, color = "#3b82f6", size = 120 }: GaugeChartProps) {
  const percentage = Math.min((value / max) * 100, 100);
  const strokeWidth = 12;
  const radius = (size - strokeWidth) / 2;
  const circumference = radius * Math.PI; // Half circle
  const offset = circumference - (percentage / 100) * circumference;
  
  // Determine color based on percentage
  const getColor = () => {
    if (percentage >= 90) return "#ef4444"; // red
    if (percentage >= 75) return "#f59e0b"; // amber
    return color;
  };

  return (
    <div className="flex flex-col items-center">
      <svg width={size} height={size / 2 + 20} className="transform">
        {/* Background arc */}
        <path
          d={`M ${strokeWidth / 2} ${size / 2} A ${radius} ${radius} 0 0 1 ${size - strokeWidth / 2} ${size / 2}`}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-muted"
        />
        {/* Value arc */}
        <path
          d={`M ${strokeWidth / 2} ${size / 2} A ${radius} ${radius} 0 0 1 ${size - strokeWidth / 2} ${size / 2}`}
          fill="none"
          stroke={getColor()}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-500"
        />
        {/* Value text */}
        <text
          x={size / 2}
          y={size / 2}
          textAnchor="middle"
          className="fill-foreground text-xl font-bold"
        >
          {percentage.toFixed(1)}%
        </text>
      </svg>
      <span className="text-sm font-medium text-muted-foreground mt-1">{label}</span>
    </div>
  );
}

// Node Card Component
interface NodeCardProps {
  node: ClusterMonitoringMetrics;
}

function NodeCard({ node }: NodeCardProps) {
  return (
    <Card className={cn(
      "transition-all hover:shadow-md",
      !node.is_online && "opacity-60"
    )}>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{node.node_name}</CardTitle>
          </div>
          <Badge variant={node.is_online ? "default" : "secondary"}>
            {node.is_online ? "Online" : "Offline"}
          </Badge>
        </div>
        <CardDescription>
          {node.node_type?.toUpperCase() || "LXD"} • {node.instance_count} instances
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {node.is_online ? (
          <>
            {/* CPU */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">CPU</span>
                <span className="font-medium">{node.cpu_used} / {node.cpu_total} cores</span>
              </div>
              <Progress value={node.cpu_usage_percent} className="h-2" />
              <p className="text-xs text-muted-foreground text-right">{node.cpu_usage_percent.toFixed(1)}%</p>
            </div>
            
            {/* Memory */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Memory</span>
                <span className="font-medium">{formatBytes(node.memory_used_bytes)} / {formatBytes(node.memory_total_bytes)}</span>
              </div>
              <Progress value={node.memory_usage_percent} className="h-2" />
              <p className="text-xs text-muted-foreground text-right">{node.memory_usage_percent.toFixed(1)}%</p>
            </div>
            
            {/* Storage */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Storage</span>
                <span className="font-medium">{formatBytes(node.storage_used_bytes)} / {formatBytes(node.storage_total_bytes)}</span>
              </div>
              <Progress value={node.storage_usage_percent} className="h-2" />
              <p className="text-xs text-muted-foreground text-right">{node.storage_usage_percent.toFixed(1)}%</p>
            </div>
            
            {/* Instances */}
            <div className="flex items-center justify-between pt-2 border-t">
              <span className="text-sm text-muted-foreground">Running</span>
              <div className="flex items-center gap-1">
                <Play className="h-3 w-3 text-green-500" />
                <span className="text-sm font-medium">{node.running_instances} / {node.instance_count}</span>
              </div>
            </div>
          </>
        ) : (
          <div className="text-center py-4 text-muted-foreground">
            <Server className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">Node is offline</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// Top Consumer Card
interface TopConsumerCardProps {
  instance: InstanceMonitoringSummary;
  rank: number;
  type: "cpu" | "memory" | "storage" | "network";
}

function TopConsumerCard({ instance, rank, type }: TopConsumerCardProps) {
  const renderValue = () => {
    switch (type) {
      case "cpu":
        return (
          <>
            <p className="text-sm font-bold text-blue-600">{instance.cpu_usage.toFixed(1)}%</p>
            <p className="text-xs text-muted-foreground">CPU Usage</p>
          </>
        );
      case "memory":
        return (
          <>
            <p className="text-sm font-bold text-green-600">{formatBytes(instance.memory_usage_bytes)}</p>
            <p className="text-xs text-muted-foreground">Memory</p>
          </>
        );
      case "storage":
        return (
          <>
            <p className="text-sm font-bold text-amber-600">{formatBytes(instance.disk_usage_bytes)}</p>
            <p className="text-xs text-muted-foreground">Disk Usage</p>
          </>
        );
      case "network":
        return (
          <>
            <p className="text-sm font-bold text-purple-600">
              ↓{formatBytes(instance.network_rx_bytes)}
            </p>
            <p className="text-sm font-bold text-pink-600">
              ↑{formatBytes(instance.network_tx_bytes)}
            </p>
          </>
        );
    }
  };

  return (
    <div className="flex items-center gap-3 p-3 rounded-lg bg-muted/50 hover:bg-muted transition-colors">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary font-bold text-sm">
        {rank}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{instance.name}</p>
        <p className="text-xs text-muted-foreground truncate">{instance.host}</p>
      </div>
      <div className="text-right shrink-0">
        {renderValue()}
        <Badge variant="outline" className="text-xs mt-1">
          {instance.type === "virtual-machine" ? "VM" : "CT"}
        </Badge>
      </div>
    </div>
  );
}

// Chart Statistics Component
interface ChartStatsProps {
  min: number;
  max: number;
  avg: number;
  unit?: string;
  color: string;
}

function ChartStats({ min, max, avg, unit = "%", color }: ChartStatsProps) {
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
}

// Loading Component with Lottie Animation
function LoadingSkeleton() {
  const [animationData, setAnimationData] = useState(null);

  useEffect(() => {
    // Load Lottie animation data
    fetch('/loading-lotttie-orca.json')
      .then(response => response.json())
      .then(data => setAnimationData(data))
      .catch(error => console.error('Error loading animation:', error));
  }, []);

  return (
    <div className="flex items-center justify-center min-h-[60vh]">
      <div className="text-center">
        {animationData && Lottie && (
          <Lottie 
            animationData={animationData} 
            loop={true}
            style={{ width: 150, height: 150 }}
          />
        )}
      </div>
    </div>
  );
}

// Colors for charts
const CHART_COLORS = {
  cpu: "#3b82f6",    // blue
  memory: "#10b981", // green
  storage: "#f59e0b", // amber
  storageRead: "#f59e0b", // amber
  storageWrite: "#ef4444", // red
  network: "#8b5cf6", // purple (RX/In)
  networkTx: "#ec4899", // pink (TX/Out)
};

const PIE_COLORS = ["#10b981", "#ef4444", "#6b7280"];

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
            Used: {formatBytes(data.memoryUsedBytes)}
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

// Time range options
const TIME_RANGES = [
  { label: "5m", value: 60, description: "Last 5 minutes" },
  { label: "15m", value: 180, description: "Last 15 minutes" },
  { label: "30m", value: 360, description: "Last 30 minutes" },
  { label: "1h", value: 720, description: "Last 1 hour" },
  { label: "6h", value: 4320, description: "Last 6 hours" },
  { label: "24h", value: 8640, description: "Last 24 hours" },
];

// Instance Details Dialog
interface InstanceDetailsDialogProps {
  open: boolean;
  onClose: () => void;
  filterType: "running" | "stopped" | "other" | "container" | "vm" | null;
  instances: VMInfo[];
  isLoading: boolean;
}

function InstanceDetailsDialog({ open, onClose, filterType, instances, isLoading }: InstanceDetailsDialogProps) {
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
    switch (status) {
      case "Running":
        return <Badge className="bg-green-500 text-white">Running</Badge>;
      case "Stopped":
        return <Badge variant="secondary">Stopped</Badge>;
      default:
        return <Badge variant="outline">{status}</Badge>;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {filterType === "container" ? <Container className="h-5 w-5" /> : 
             filterType === "vm" ? <Monitor className="h-5 w-5" /> : 
             <Box className="h-5 w-5" />}
            {getTitle()}
          </DialogTitle>
          <DialogDescription>
            {instances.length} instance{instances.length !== 1 ? "s" : ""} found
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto space-y-2 pr-2">
          {isLoading ? (
            <div className="space-y-2">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} className="h-16 w-full" />
              ))}
            </div>
          ) : instances.length > 0 ? (
            instances.map((instance) => (
              <div
                key={`${instance.host}-${instance.name}`}
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
                    <p className="font-medium">{instance.name}</p>
                    <p className="text-xs text-muted-foreground">{instance.host}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {getStatusBadge(instance.status)}
                  <Badge variant="outline" className="text-xs">
                    {instance.type === "virtual-machine" ? "VM" : "CT"}
                  </Badge>
                </div>
              </div>
            ))
          ) : (
            <div className="text-center py-8 text-muted-foreground">
              <Box className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>No instances found</p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function MonitoringPage() {
  const [refreshInterval, setRefreshInterval] = useState(5000);
  const [isPaused, setIsPaused] = useState(false);
  const [timeRange, setTimeRange] = useState(60); // Default 5 minutes (60 points at 5s intervals)
  const [chartTab, setChartTab] = useState<"cpu" | "memory" | "storage" | "network">("cpu");
  
  // Instance dialog state
  const [instanceDialogOpen, setInstanceDialogOpen] = useState(false);
  const [instanceFilterType, setInstanceFilterType] = useState<"running" | "stopped" | "other" | "container" | "vm" | null>(null);
  const [allInstances, setAllInstances] = useState<VMInfo[]>([]);
  const [instancesLoading, setInstancesLoading] = useState(false);
  
  // Sort order for top consumers (true = highest first, false = lowest first)
  const [sortDescending, setSortDescending] = useState(true);
  
  // Node selection for filtering
  const [selectedNode, setSelectedNode] = useState<string>("all");
  
  // Top N consumers count
  const [topNCount, setTopNCount] = useState<number>(5);
  
  // Storage pools sort order
  const [storagePoolsSortDesc, setStoragePoolsSortDesc] = useState(true);
  
  // Visible nodes for multi-line graph (when "all" is selected)
  const [visibleNodes, setVisibleNodes] = useState<Record<string, boolean>>({});
  
  const { 
    data: dashboardData, 
    isLoading: dashboardLoading, 
    isFetching: dashboardFetching,
    error: dashboardError,
    refetch: refetchDashboard,
    dataUpdatedAt,
  } = useMonitoringDashboard(!isPaused, refreshInterval);
  
  const { 
    data: historyData, 
    isLoading: historyLoading,
    isFetching: historyFetching,
  } = useMonitoringHistory(timeRange, !isPaused);

  // Fetch instances for dialog
  const fetchInstances = async (filterType: "running" | "stopped" | "other" | "container" | "vm") => {
    setInstancesLoading(true);
    setInstanceFilterType(filterType);
    setInstanceDialogOpen(true);
    try {
      const response = await vmAPI.list();
      let filtered = response.vms;
      
      switch (filterType) {
        case "running":
          filtered = filtered.filter(vm => vm.status === "Running");
          break;
        case "stopped":
          filtered = filtered.filter(vm => vm.status === "Stopped");
          break;
        case "other":
          filtered = filtered.filter(vm => vm.status !== "Running" && vm.status !== "Stopped");
          break;
        case "container":
          filtered = filtered.filter(vm => vm.type === "container");
          break;
        case "vm":
          filtered = filtered.filter(vm => vm.type === "virtual-machine");
          break;
      }
      
      setAllInstances(filtered);
    } catch (error) {
      console.error("Failed to fetch instances:", error);
      setAllInstances([]);
    } finally {
      setInstancesLoading(false);
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
      return {
        total_instances: dashboardData.total_instances,
        running_instances: dashboardData.running_instances,
        stopped_instances: dashboardData.stopped_instances,
        other_instances: dashboardData.other_instances,
        container_count: dashboardData.container_count,
        vm_count: dashboardData.vm_count,
        online_nodes: dashboardData.online_nodes,
        offline_nodes: dashboardData.offline_nodes,
        total_nodes: dashboardData.total_nodes,
        cpu_usage_percent: dashboardData.cpu_usage_percent,
        realtime_cpu_percent: dashboardData.realtime_cpu_percent,
        memory_usage_percent: dashboardData.memory_usage_percent,
        storage_usage_percent: dashboardData.storage_usage_percent,
        total_cpu: dashboardData.total_cpu,
        used_cpu: dashboardData.used_cpu,
        total_memory_bytes: dashboardData.total_memory_bytes,
        used_memory_bytes: dashboardData.used_memory_bytes,
        total_storage_bytes: dashboardData.total_storage_bytes,
        used_storage_bytes: dashboardData.used_storage_bytes,
        total_gpu: dashboardData.total_gpu,
        used_gpu: dashboardData.used_gpu,
        warnings: dashboardData.warnings,
      };
    }
    
    // Find the selected node's data
    const node = dashboardData.nodes?.find(n => n.node_name === selectedNode);
    if (!node) return null;
    
    // Calculate container/vm count from top consumers (they have type and host info)
    const nodeContainers = dashboardData.top_cpu_consumers?.filter(
      i => i.host === selectedNode && i.type === "container"
    ).length || 0;
    const nodeVMs = dashboardData.top_cpu_consumers?.filter(
      i => i.host === selectedNode && i.type === "virtual-machine"
    ).length || 0;
    
    return {
      total_instances: node.instance_count || 0,
      running_instances: node.running_instances || 0,
      stopped_instances: (node.instance_count || 0) - (node.running_instances || 0),
      other_instances: 0,
      container_count: nodeContainers,
      vm_count: nodeVMs,
      online_nodes: node.is_online ? 1 : 0,
      offline_nodes: node.is_online ? 0 : 1,
      total_nodes: 1,
      cpu_usage_percent: node.cpu_usage_percent || 0,
      realtime_cpu_percent: node.cpu_usage_percent || 0,
      memory_usage_percent: node.memory_usage_percent || 0,
      storage_usage_percent: node.storage_usage_percent || 0,
      total_cpu: node.cpu_total || 0,
      used_cpu: node.cpu_used || 0,
      total_memory_bytes: node.memory_total_bytes || 0,
      used_memory_bytes: node.memory_used_bytes || 0,
      total_storage_bytes: node.storage_total_bytes || 0,
      used_storage_bytes: node.storage_used_bytes || 0,
      total_gpu: node.gpu_total || 0,
      used_gpu: node.gpu_used || 0,
      warnings: dashboardData.warnings, // Keep global warnings
    };
  }, [dashboardData, selectedNode]);

  // Format history data for charts - filter by selected node
  const chartData = useMemo(() => {
    if (!historyData) return [];
    
    // Get current memory/storage totals from dashboard data for calculating actual usage
    // Use filtered stats for node-specific data
    const totalMemoryBytes = filteredStats?.total_memory_bytes || dashboardData?.total_memory_bytes || 0;
    const totalStorageBytes = filteredStats?.total_storage_bytes || dashboardData?.total_storage_bytes || 0;
    
    // Calculate network rate (delta between points)
    return historyData.map((point, idx) => {
      const prevPoint = idx > 0 ? historyData[idx - 1] : null;
      
      // Get the data for the selected node or use aggregate
      let cpuPercent = point.cpu_usage_percent;
      let memoryPercent = point.memory_usage_percent;
      let storagePercent = point.storage_usage_percent || 0;
      let networkRxBytes = point.network_rx_bytes;
      let networkTxBytes = point.network_tx_bytes;
      let runningInstances = point.running_instances;
      
      // If a specific node is selected and per-node data is available, use it
      if (selectedNode !== "all" && point.nodes && point.nodes[selectedNode]) {
        const nodeData = point.nodes[selectedNode];
        cpuPercent = nodeData.cpu_usage_percent;
        memoryPercent = nodeData.memory_usage_percent;
        storagePercent = nodeData.storage_usage_percent || 0;
        networkRxBytes = nodeData.network_rx_bytes;
        runningInstances = nodeData.running_instances;
        
        // Get previous point's node data for network rate calculation
        const prevNodeData = prevPoint?.nodes?.[selectedNode];
        networkTxBytes = nodeData.network_tx_bytes;
        
        // Network rate calculation using node-specific data
        let nodeNetworkRxRate = 0;
        let nodeNetworkTxRate = 0;
        
        if (prevNodeData) {
          const timeDelta = (new Date(point.timestamp).getTime() - new Date(prevPoint!.timestamp).getTime()) / 1000;
          if (timeDelta > 0) {
            nodeNetworkRxRate = (networkRxBytes - prevNodeData.network_rx_bytes) / timeDelta;
            nodeNetworkTxRate = (networkTxBytes - prevNodeData.network_tx_bytes) / timeDelta;
            if (nodeNetworkRxRate < 0) nodeNetworkRxRate = 0;
            if (nodeNetworkTxRate < 0) nodeNetworkTxRate = 0;
          }
        }
        
        const memoryUsedBytes = (memoryPercent / 100) * totalMemoryBytes;
        const storageUsedBytes = (storagePercent / 100) * totalStorageBytes;
        
        return {
          time: format(new Date(point.timestamp), "HH:mm:ss"),
          cpu: cpuPercent,
          memory: memoryPercent,
          memoryUsedBytes,
          storage: storagePercent,
          storageUsedBytes,
          diskIoBytes: point.disk_io_bytes || 0,
          networkRxRate: nodeNetworkRxRate / (1024 * 1024),
          networkTxRate: nodeNetworkTxRate / (1024 * 1024),
          networkRxTotal: networkRxBytes / (1024 * 1024 * 1024),
          networkTxTotal: networkTxBytes / (1024 * 1024 * 1024),
          instances: runningInstances,
        };
      }
      
      // Default: use aggregate data for all nodes
      let networkRxRate = 0;
      let networkTxRate = 0;
      
      if (prevPoint) {
        const timeDelta = (new Date(point.timestamp).getTime() - new Date(prevPoint.timestamp).getTime()) / 1000;
        if (timeDelta > 0) {
          networkRxRate = (point.network_rx_bytes - prevPoint.network_rx_bytes) / timeDelta;
          networkTxRate = (point.network_tx_bytes - prevPoint.network_tx_bytes) / timeDelta;
          // Clamp negative values (counter reset)
          if (networkRxRate < 0) networkRxRate = 0;
          if (networkTxRate < 0) networkTxRate = 0;
        }
      }
      
      // Calculate actual bytes based on percentage
      const memoryUsedBytes = (point.memory_usage_percent / 100) * totalMemoryBytes;
      const storageUsedBytes = ((point.storage_usage_percent || 0) / 100) * totalStorageBytes;
      
      // Build per-node data for multi-line chart (when "all" is selected)
      const perNodeData: Record<string, number> = {};
      if (point.nodes) {
        Object.entries(point.nodes).forEach(([nodeName, nodeData]) => {
          // Add per-node CPU, memory, storage values with node name prefix
          perNodeData[`cpu_${nodeName}`] = (nodeData as any).cpu_usage_percent || 0;
          perNodeData[`memory_${nodeName}`] = (nodeData as any).memory_usage_percent || 0;
          perNodeData[`storage_${nodeName}`] = (nodeData as any).storage_usage_percent || 0;
        });
      }
      
      return {
        time: format(new Date(point.timestamp), "HH:mm:ss"),
        cpu: point.cpu_usage_percent,
        memory: point.memory_usage_percent,
        memoryUsedBytes,
        storage: point.storage_usage_percent || 0,
        storageUsedBytes,
        diskIoBytes: point.disk_io_bytes || 0, // Disk I/O for storage chart
        // Network in GB for display
        networkRxRate: networkRxRate / (1024 * 1024), // MB/s
        networkTxRate: networkTxRate / (1024 * 1024), // MB/s
        // Cumulative network in GB for total display
        networkRxTotal: point.network_rx_bytes / (1024 * 1024 * 1024), // GB
        networkTxTotal: point.network_tx_bytes / (1024 * 1024 * 1024), // GB
        instances: point.running_instances,
        // Per-node data for multi-line chart
        ...perNodeData,
      };
    });
  }, [historyData, dashboardData, selectedNode, filteredStats]);
  
  // Filter data by selected node
  const filterByNode = (items: any[]) => {
    if (selectedNode === "all") return items;
    return items.filter(item => item.host === selectedNode);
  };
  
  // Sorted consumers for storage and network
  const topStorageConsumers = useMemo(() => {
    if (!dashboardData?.top_cpu_consumers) return [];
    // Filter by node first, then sort all consumers by disk usage
    const filtered = filterByNode([...dashboardData.top_cpu_consumers]);
    return filtered
      .sort((a, b) => sortDescending 
        ? (b.disk_usage_bytes || 0) - (a.disk_usage_bytes || 0)
        : (a.disk_usage_bytes || 0) - (b.disk_usage_bytes || 0))
      .slice(0, topNCount);
  }, [dashboardData, sortDescending, topNCount, selectedNode]);
  
  const topNetworkConsumers = useMemo(() => {
    if (!dashboardData?.top_cpu_consumers) return [];
    // Filter by node first, then sort by total network (rx + tx)
    const filtered = filterByNode([...dashboardData.top_cpu_consumers]);
    return filtered
      .sort((a, b) => {
        const aTotal = (a.network_rx_bytes || 0) + (a.network_tx_bytes || 0);
        const bTotal = (b.network_rx_bytes || 0) + (b.network_tx_bytes || 0);
        return sortDescending ? bTotal - aTotal : aTotal - bTotal;
      })
      .slice(0, topNCount);
  }, [dashboardData, sortDescending, topNCount, selectedNode]);
  
  // Sorted CPU consumers
  const sortedCpuConsumers = useMemo(() => {
    if (!dashboardData?.top_cpu_consumers) return [];
    // Filter by node first, then sort by CPU
    const filtered = filterByNode([...dashboardData.top_cpu_consumers]);
    return filtered
      .sort((a, b) => sortDescending 
        ? (b.cpu_usage || 0) - (a.cpu_usage || 0)
        : (a.cpu_usage || 0) - (b.cpu_usage || 0))
      .slice(0, topNCount);
  }, [dashboardData, sortDescending, topNCount, selectedNode]);
  
  // Sorted Memory consumers
  const sortedMemoryConsumers = useMemo(() => {
    if (!dashboardData?.top_memory_consumers) return [];
    // Filter by node first, then sort by memory
    const filtered = filterByNode([...dashboardData.top_memory_consumers]);
    return filtered
      .sort((a, b) => sortDescending 
        ? (b.memory_usage_bytes || 0) - (a.memory_usage_bytes || 0)
        : (a.memory_usage_bytes || 0) - (b.memory_usage_bytes || 0))
      .slice(0, topNCount);
  }, [dashboardData, sortDescending, topNCount, selectedNode]);

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
    if (!chartData.length) return [0, 10];
    const rxValues = chartData.map(d => d.networkRxRate).filter(v => typeof v === 'number');
    const txValues = chartData.map(d => d.networkTxRate).filter(v => typeof v === 'number');
    const allValues = [...rxValues, ...txValues];
    
    if (allValues.length === 0) return [0, 10];
    
    const maxValue = Math.max(...allValues);
    const paddedMax = maxValue * 1.2;
    
    // Nice network thresholds in MB/s
    const thresholds = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
    let niceCeiling = 10;
    for (const threshold of thresholds) {
      if (paddedMax <= threshold) {
        niceCeiling = threshold;
        break;
      }
    }
    
    // For very small values, show at least 1 MB/s
    if (maxValue < 0.1) {
      niceCeiling = 1;
    }
    
    return [0, niceCeiling];
  }, [chartData]);

  // Calculate chart statistics (min, max, avg) for each metric
  // When "All Nodes" is selected, calculate stats from all visible per-node values
  const chartStats = useMemo(() => {
    const calcStats = (values: number[]) => {
      if (values.length === 0) return { min: 0, max: 0, avg: 0 };
      const min = Math.min(...values);
      const max = Math.max(...values);
      const avg = values.reduce((a, b) => a + b, 0) / values.length;
      return { min, max, avg };
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
  const lastUpdated = dataUpdatedAt ? format(new Date(dataUpdatedAt), "HH:mm:ss") : null;

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
            <Button onClick={() => refetchDashboard()} className="mt-4">
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
            Real-time resource metrics across all nodes
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {lastUpdated && (
            <span className="text-xs text-muted-foreground">
              Updated: {lastUpdated}
            </span>
          )}
          <TooltipProvider>
            <UITooltip>
              <TooltipTrigger asChild>
                <Button 
                  variant="outline" 
                  size="sm"
                  onClick={() => setIsPaused(!isPaused)}
                >
                  {isPaused ? <Play className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{isPaused ? "Resume" : "Pause"} updates</TooltipContent>
            </UITooltip>
          </TooltipProvider>
          <Button 
            variant="outline" 
            size="sm" 
            onClick={() => refetchDashboard()}
            disabled={dashboardFetching}
          >
            <RefreshCw className={cn("h-4 w-4", dashboardFetching && "animate-spin")} />
          </Button>
          {/* Node Selection - at the end (rightmost) */}
          <Select value={selectedNode} onValueChange={setSelectedNode}>
            <SelectTrigger className="w-[160px] h-9">
              <Server className="h-4 w-4 mr-2" />
              <SelectValue placeholder="All Nodes" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Nodes</SelectItem>
              {nodeNames.map(name => (
                <SelectItem key={name} value={name}>{name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Overview Stats - use filteredStats for node-filtered view */}
      {filteredStats && (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          title="Total Instances"
          value={filteredStats.total_instances}
          subtitle={selectedNode === "all" ? `${filteredStats.container_count} containers • ${filteredStats.vm_count} VMs` : `on ${selectedNode}`}
          icon={<Box className="h-6 w-6 text-blue-500" />}
          color="bg-blue-500/10"
        />
        <MetricCard
          title="Running"
          value={filteredStats.running_instances}
          subtitle={`${filteredStats.stopped_instances} stopped`}
          icon={<Play className="h-6 w-6 text-green-500" />}
          color="bg-green-500/10"
          progress={(filteredStats.running_instances / Math.max(filteredStats.total_instances, 1)) * 100}
        />
        <MetricCard
          title="Nodes Online"
          value={`${filteredStats.online_nodes} / ${filteredStats.total_nodes}`}
          subtitle={filteredStats.offline_nodes > 0 ? `${filteredStats.offline_nodes} offline` : "All nodes healthy"}
          icon={<Server className="h-6 w-6 text-purple-500" />}
          color="bg-purple-500/10"
          trend={filteredStats.offline_nodes > 0 ? "down" : "neutral"}
        />
        <MetricCard
          title="Warnings"
          value={filteredStats.warnings?.total || 0}
          subtitle={filteredStats.warnings?.critical ? `${filteredStats.warnings.critical} critical` : "No critical issues"}
          icon={<AlertTriangle className={cn(
            "h-6 w-6",
            (filteredStats.warnings?.critical || 0) > 0 ? "text-red-500" : "text-amber-500"
          )} />}
          color={(filteredStats.warnings?.critical || 0) > 0 ? "bg-red-500/10" : "bg-amber-500/10"}
          trend={(filteredStats.warnings?.critical || 0) > 0 ? "up" : "neutral"}
        />
      </div>
      )}

      {/* Resource Utilization Gauges */}
      {filteredStats && (
      <Card>
        <CardHeader>
          <div>
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-5 w-5" />
              Resource Utilization
            </CardTitle>
            <CardDescription>
              {selectedNode === "all" ? "Overall resource allocation and usage across all nodes" : `Resource usage for ${selectedNode}`}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
            <div className="text-center">
              <GaugeChart 
                value={filteredStats.realtime_cpu_percent || 0} 
                label="CPU Usage" 
                color={CHART_COLORS.cpu}
              />
              <p className="text-sm text-muted-foreground mt-2">
                Allocated: {filteredStats.used_cpu} / {filteredStats.total_cpu} cores
              </p>
            </div>
            <div className="text-center">
              <GaugeChart 
                value={filteredStats.memory_usage_percent} 
                label="Memory Usage" 
                color={CHART_COLORS.memory}
              />
              <p className="text-sm text-muted-foreground mt-2">
                {formatBytes(filteredStats.used_memory_bytes)} / {formatBytes(filteredStats.total_memory_bytes)}
              </p>
            </div>
            <div className="text-center">
              <GaugeChart 
                value={filteredStats.storage_usage_percent} 
                label="Storage Usage" 
                color={CHART_COLORS.storage}
              />
              <p className="text-sm text-muted-foreground mt-2">
                {formatBytes(filteredStats.used_storage_bytes)} / {formatBytes(filteredStats.total_storage_bytes)}
              </p>
            </div>
            <div className="text-center">
              <GaugeChart 
                value={(filteredStats.used_gpu / Math.max(filteredStats.total_gpu, 1)) * 100} 
                label="GPU Allocation" 
                color="#8b5cf6"
              />
              <p className="text-sm text-muted-foreground mt-2">
                {filteredStats.used_gpu} / {filteredStats.total_gpu} GPUs
              </p>
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
              <CardTitle className="flex items-center gap-2">
                <TrendingUp className="h-5 w-5" />
                Resource Metrics
              </CardTitle>
              <CardDescription>Real-time resource usage over time</CardDescription>
            </div>
            <div className="flex items-center gap-2">
              {/* Time Range Selector */}
              <div className="flex items-center gap-1 bg-muted rounded-lg p-1">
                {TIME_RANGES.map((range) => (
                  <Button
                    key={range.value}
                    variant={timeRange === range.value ? "default" : "ghost"}
                    size="sm"
                    className={cn(
                      "h-7 px-3 text-xs font-medium transition-all",
                      timeRange === range.value 
                        ? "bg-primary text-primary-foreground shadow-sm" 
                        : "hover:bg-muted-foreground/10"
                    )}
                    onClick={() => setTimeRange(range.value)}
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
            <TabsList className="grid w-full max-w-2xl grid-cols-4 h-auto p-1">
              <TabsTrigger value="cpu" className="flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs sm:text-sm sm:gap-2 sm:px-3">
                <Cpu className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0" />
                <span>CPU</span>
              </TabsTrigger>
              <TabsTrigger value="memory" className="flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs sm:text-sm sm:gap-2 sm:px-3">
                <Activity className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0 hidden sm:block" />
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
              <ResponsiveContainer width="100%" height={350}>
                <AreaChart data={chartData}>
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
                    tickFormatter={(v) => `${v}%`}
                    className="text-muted-foreground"
                  />
                  <Tooltip 
                    contentStyle={{ 
                      backgroundColor: "hsl(var(--card))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: "8px"
                    }}
                    formatter={(value, name) => {
                      const displayName = String(name).startsWith('cpu_') ? String(name).replace('cpu_', '') : 'Average';
                      return [`${Number(value || 0).toFixed(1)}%`, displayName];
                    }}
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
                    />
                  )}
                  {selectedNode === "all" && nodeNames.length > 1 && (
                    <Legend 
                      formatter={(value: string) => value.replace('cpu_', '')}
                      wrapperStyle={{ paddingTop: "10px" }}
                    />
                  )}
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
              <ResponsiveContainer width="100%" height={350}>
                <AreaChart data={chartData}>
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
                    tickFormatter={(v) => `${v}%`}
                    className="text-muted-foreground"
                  />
                  <Tooltip 
                    contentStyle={{ 
                      backgroundColor: "hsl(var(--card))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: "8px"
                    }}
                    formatter={(value, name) => {
                      const displayName = String(name).startsWith('memory_') ? String(name).replace('memory_', '') : 'Average';
                      return [`${Number(value || 0).toFixed(1)}%`, displayName];
                    }}
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
                    />
                  )}
                  {selectedNode === "all" && nodeNames.length > 1 && (
                    <Legend 
                      formatter={(value: string) => value.replace('memory_', '')}
                      wrapperStyle={{ paddingTop: "10px" }}
                    />
                  )}
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
              <ResponsiveContainer width="100%" height={350}>
                <AreaChart data={chartData}>
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
                    tickFormatter={(v) => `${v}%`}
                    className="text-muted-foreground"
                  />
                  <Tooltip 
                    contentStyle={{ 
                      backgroundColor: "hsl(var(--card))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: "8px"
                    }}
                    formatter={(value, name) => {
                      const displayName = String(name).startsWith('storage_') ? String(name).replace('storage_', '') : 'Average';
                      return [`${Number(value || 0).toFixed(1)}%`, displayName];
                    }}
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
                    />
                  )}
                  {selectedNode === "all" && nodeNames.length > 1 && (
                    <Legend 
                      formatter={(value: string) => value.replace('storage_', '')}
                      wrapperStyle={{ paddingTop: "10px" }}
                    />
                  )}
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
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.network }}>{formatNetworkSpeed(chartStats.networkRx.min)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Avg:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.network }}>{formatNetworkSpeed(chartStats.networkRx.avg)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Max:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.network }}>{formatNetworkSpeed(chartStats.networkRx.max)}</span>
                  </div>
                </div>
                <div className="hidden sm:block w-px h-6 bg-border" />
                <div className="flex flex-wrap items-center justify-center gap-2 sm:gap-4">
                  <span className="text-[10px] sm:text-xs font-medium" style={{ color: CHART_COLORS.networkTx }}>↑ Outbound (TX)</span>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Min:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.networkTx }}>{formatNetworkSpeed(chartStats.networkTx.min)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Avg:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.networkTx }}>{formatNetworkSpeed(chartStats.networkTx.avg)}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] sm:text-xs text-muted-foreground">Max:</span>
                    <span className="text-[10px] sm:text-sm font-semibold" style={{ color: CHART_COLORS.networkTx }}>{formatNetworkSpeed(chartStats.networkTx.max)}</span>
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
              <ResponsiveContainer width="100%" height={320}>
                <AreaChart data={chartData}>
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
                    tickFormatter={(v) => `${v.toFixed(1)} MB/s`}
                    className="text-muted-foreground"
                  />
                  <Tooltip 
                    contentStyle={{ 
                      backgroundColor: "hsl(var(--card))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: "8px"
                    }}
                    formatter={(value, name) => {
                      const numValue = typeof value === 'number' ? value : 0;
                      const label = name === "networkRxRate" ? "↓ Inbound" : "↑ Outbound";
                      return [`${numValue.toFixed(2)} MB/s`, label];
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="networkRxRate"
                    stroke={CHART_COLORS.network}
                    fill="url(#networkRxGradient)"
                    strokeWidth={2}
                    name="networkRxRate"
                  />
                  <Area
                    type="monotone"
                    dataKey="networkTxRate"
                    stroke={CHART_COLORS.networkTx}
                    fill="url(#networkTxGradient)"
                    strokeWidth={2}
                    name="networkTxRate"
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
            <CardTitle className="flex items-center gap-2">
              <Container className="h-5 w-5" />
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
                  <Tooltip content={<PieChartTooltip />} />
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
                      <div 
                        className="w-3 h-3 rounded-full shrink-0"
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

        {/* Top Consumers - 4 Tabs */}
        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Zap className="h-5 w-5" />
                  Top Consumers
                </CardTitle>
                <CardDescription>Instances with highest resource usage</CardDescription>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {/* Top N Selection */}
                <Select value={topNCount.toString()} onValueChange={(v) => setTopNCount(parseInt(v))}>
                  <SelectTrigger className="w-[80px] sm:w-[90px] h-8">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="5">Top 5</SelectItem>
                    <SelectItem value="10">Top 10</SelectItem>
                    <SelectItem value="15">Top 15</SelectItem>
                    <SelectItem value="20">Top 20</SelectItem>
                  </SelectContent>
                </Select>
                <TooltipProvider>
                  <UITooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setSortDescending(!sortDescending)}
                        className="flex items-center gap-1.5 h-8 px-2.5 sm:px-3 shrink-0"
                      >
                        {sortDescending ? (
                          <>
                            <ArrowDown className="h-4 w-4 sm:h-3.5 sm:w-3.5" />
                            <span className="text-xs">Highest</span>
                          </>
                        ) : (
                          <>
                            <ArrowUp className="h-4 w-4 sm:h-3.5 sm:w-3.5" />
                            <span className="text-xs">Lowest</span>
                          </>
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      {sortDescending ? "Click to show lowest first" : "Click to show highest first"}
                    </TooltipContent>
                  </UITooltip>
                </TooltipProvider>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <Tabs defaultValue="cpu">
              <TabsList className="grid w-full grid-cols-4 mb-4 h-auto p-1">
                <TabsTrigger value="cpu" className="text-xs px-2 py-1.5">CPU</TabsTrigger>
                <TabsTrigger value="memory" className="text-xs px-2 py-1.5">Memory</TabsTrigger>
                <TabsTrigger value="storage" className="text-xs px-2 py-1.5">Storage</TabsTrigger>
                <TabsTrigger value="network" className="text-xs px-2 py-1.5">Network</TabsTrigger>
              </TabsList>
              <TabsContent value="cpu" className="space-y-2 max-h-[280px] overflow-y-auto">
                {sortedCpuConsumers.length > 0 ? (
                  sortedCpuConsumers.map((instance, idx) => (
                    <TopConsumerCard key={instance.name} instance={instance} rank={idx + 1} type="cpu" />
                  ))
                ) : (
                  <p className="text-center text-muted-foreground py-4">No running instances</p>
                )}
              </TabsContent>
              <TabsContent value="memory" className="space-y-2 max-h-[280px] overflow-y-auto">
                {sortedMemoryConsumers.length > 0 ? (
                  sortedMemoryConsumers.map((instance, idx) => (
                    <TopConsumerCard key={instance.name} instance={instance} rank={idx + 1} type="memory" />
                  ))
                ) : (
                  <p className="text-center text-muted-foreground py-4">No running instances</p>
                )}
              </TabsContent>
              <TabsContent value="storage" className="space-y-2 max-h-[280px] overflow-y-auto">
                {topStorageConsumers.length > 0 ? (
                  topStorageConsumers.map((instance, idx) => (
                    <TopConsumerCard key={instance.name} instance={instance} rank={idx + 1} type="storage" />
                  ))
                ) : (
                  <p className="text-center text-muted-foreground py-4">No running instances</p>
                )}
              </TabsContent>
              <TabsContent value="network" className="space-y-2 max-h-[280px] overflow-y-auto">
                {topNetworkConsumers.length > 0 ? (
                  topNetworkConsumers.map((instance, idx) => (
                    <TopConsumerCard key={instance.name} instance={instance} rank={idx + 1} type="network" />
                  ))
                ) : (
                  <p className="text-center text-muted-foreground py-4">No running instances</p>
                )}
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      </div>

      {/* Alarm Statistics */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Bell className="h-5 w-5" />
            Alarm Statistics
          </CardTitle>
          <CardDescription>System alerts and warnings overview</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="flex items-center gap-3 p-4 rounded-lg border bg-red-500/5 border-red-500/20">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-red-500/10">
                <Flame className="h-5 w-5 text-red-500" />
              </div>
              <div>
                <p className="text-2xl font-bold text-red-600">{filteredStats?.warnings?.critical || 0}</p>
                <p className="text-xs text-muted-foreground">Critical</p>
              </div>
            </div>
            <div className="flex items-center gap-3 p-4 rounded-lg border bg-orange-500/5 border-orange-500/20">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-orange-500/10">
                <ShieldAlert className="h-5 w-5 text-orange-500" />
              </div>
              <div>
                <p className="text-2xl font-bold text-orange-600">{Math.max(0, (filteredStats?.warnings?.warning || 0))}</p>
                <p className="text-xs text-muted-foreground">Major</p>
              </div>
            </div>
            <div className="flex items-center gap-3 p-4 rounded-lg border bg-yellow-500/5 border-yellow-500/20">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-yellow-500/10">
                <AlertTriangle className="h-5 w-5 text-yellow-500" />
              </div>
              <div>
                <p className="text-2xl font-bold text-yellow-600">{filteredStats?.warnings?.warning || 0}</p>
                <p className="text-xs text-muted-foreground">Warning</p>
              </div>
            </div>
            <div className="flex items-center gap-3 p-4 rounded-lg border bg-blue-500/5 border-blue-500/20">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-500/10">
                <Info className="h-5 w-5 text-blue-500" />
              </div>
              <div>
                <p className="text-2xl font-bold text-blue-600">{filteredStats?.warnings?.info || 0}</p>
                <p className="text-xs text-muted-foreground">Info</p>
              </div>
            </div>
          </div>
          {(filteredStats?.warnings?.total || 0) === 0 && (
            <div className="mt-4 text-center py-4 bg-green-500/5 rounded-lg border border-green-500/20">
              <p className="text-green-600 font-medium">✓ All systems operational - No active alarms</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Node Status Cards */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Server className="h-5 w-5" />
            Node Status
          </CardTitle>
          <CardDescription>
            Detailed metrics for each connected node
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
                <p>No nodes connected</p>
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
                <CardTitle className="flex items-center gap-2">
                  <Database className="h-5 w-5" />
                  Storage Pools
                </CardTitle>
                <CardDescription>
                  Storage pool utilization across all nodes
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
                .slice(0, 10)
                .map((pool) => (
                <div key={`${pool.node}-${pool.name}`} className="space-y-1.5">
                  <div className="flex items-center justify-between text-sm">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{pool.name}</span>
                      <Badge variant="outline" className="text-xs">{pool.driver}</Badge>
                      <span className="text-muted-foreground">({pool.node})</span>
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
        open={instanceDialogOpen}
        onClose={() => setInstanceDialogOpen(false)}
        filterType={instanceFilterType}
        instances={allInstances}
        isLoading={instancesLoading}
      />
    </div>
  );
}
