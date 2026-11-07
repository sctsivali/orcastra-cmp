"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CpuUsageGauge, type CpuUsageReading } from "@/components/monitoring/cpu-usage-gauge";
import { GaugeChart } from "@/components/monitoring/gauge-chart";
import { GpuAllocationGauge } from "@/components/monitoring/gpu-allocation-gauge";
import { cpuAllocation, formatCpuThreads } from "@/lib/cpu-allocation";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { formatBytes, formatVcpuCount, plural } from "@/lib/utils";
import { ClusterStatus, type MonitoringDashboardResponse } from "@/types/api";

/** The scope's host figures, as MonitoringContent's filteredStats carries them. */
export interface ResourceUtilizationStats {
  cpu_reading: CpuUsageReading;
  used_cpu: number;
  total_cpu: number;
  /** null when no total was reported: never read as 0%. */
  memory_usage_percent: number | null;
  used_memory_bytes: number;
  total_memory_bytes: number;
  storage_usage_percent: number | null;
  used_storage_bytes: number;
  total_storage_bytes: number;
  total_gpu: number;
  used_gpu: number;
  total_gpu_cards?: number;
  status_message?: string | null;
}

/** Whether the gauges below have anything behind them, and how much of the scope that is. */
export interface CapacityScope {
  /** Why no gauge is a reading, the caption under every one of them; null when one is. */
  unavailable: string | null;
  /** One line for the card when only some clusters reported, ex; "From 4 of 6 clusters". */
  partial: string | null;
}

export const NO_CLUSTER_REPORTED = "No cluster reported usage";
const NOT_REPORTED = "Not reported";

export function capacityScope(data: MonitoringDashboardResponse, selectedNode: string): CapacityScope {
  if (selectedNode !== "all") {
    const node = data.nodes.find((row) => row.node_name === selectedNode);
    if (!node || node.is_online) return { unavailable: null, partial: null };
    const meta = clusterStatusMeta(node);
    return {
      unavailable: meta.status === ClusterStatus.OFFLINE ? "Cluster offline" : `Cluster status: ${meta.label}`,
      partial: null,
    };
  }
  // note: an older backend sends no counts. online_nodes is the nearest it has, and it can only
  // overstate, so the fallback never invents an outage.
  const reporting = data.clusters_reporting ?? data.online_nodes;
  const total = data.clusters_total ?? data.total_nodes;
  if (reporting === 0) return { unavailable: NO_CLUSTER_REPORTED, partial: null };
  return {
    unavailable: null,
    partial: reporting < total ? `From ${reporting} of ${plural(total, "cluster")}` : null,
  };
}

/** A share with something under it, or null: a missing total is not 0%. */
function reading(percent: number | null, total: number): number | null {
  return percent !== null && total > 0 ? percent : null;
}

function Unreported({ label, caption, statusMessage }: { label: string; caption: string; statusMessage?: string }) {
  return (
    <>
      <GaugeChart value={null} label={label} statusMessage={statusMessage} />
      <p className="text-sm text-muted-foreground mt-2">{caption}</p>
    </>
  );
}

interface ResourceUtilizationCardProps {
  stats: ResourceUtilizationStats;
  scope: CapacityScope;
  /** What the description calls the scope: a cluster's display name, or "all". */
  scopeLabel: string;
  isPartner: boolean;
}

export function ResourceUtilizationCard({ stats, scope, scopeLabel, isPartner }: ResourceUtilizationCardProps) {
  // status_message is the backend's own error text, so it is operator content.
  const statusMessage = isPartner && stats.status_message ? stats.status_message : undefined;
  const memory = scope.unavailable ? null : reading(stats.memory_usage_percent, stats.total_memory_bytes);
  const storage = scope.unavailable ? null : reading(stats.storage_usage_percent, stats.total_storage_bytes);
  const caption = scope.unavailable ?? NOT_REPORTED;

  return (
    <Card data-slot="resource-utilization">
      <CardHeader>
        <div>
          <CardTitle>
            Resource Utilization
          </CardTitle>
          <CardDescription>
            {scopeLabel === "all" ? "Usage and allocation across all clusters" : `Usage and allocation on ${scopeLabel}`}
          </CardDescription>
          {scope.partial ? (
            <p className="text-xs text-muted-foreground tabular-nums mt-1">{scope.partial}</p>
          ) : null}
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
          <div className="text-center">
            {scope.unavailable ? (
              <Unreported label="CPU Usage" caption={scope.unavailable} statusMessage={statusMessage} />
            ) : (
              <>
                <CpuUsageGauge
                  reading={stats.cpu_reading}
                  label="CPU Usage"
                  statusMessage={stats.cpu_reading.status !== "ready" ? statusMessage : undefined}
                />
                <p className="text-sm text-muted-foreground mt-2">
                  Allocated: {formatVcpuCount(stats.used_cpu)} / {formatCpuThreads(stats.total_cpu)} (
                  {cpuAllocation({ cpu_total: stats.total_cpu, cpu_used: stats.used_cpu }).ratioLabel})
                </p>
              </>
            )}
          </div>
          <div className="text-center">
            {memory === null ? (
              <Unreported label="Memory Usage" caption={caption} statusMessage={statusMessage} />
            ) : (
              <>
                <GaugeChart value={memory} label="Memory Usage" />
                <p className="text-sm text-muted-foreground mt-2">
                  {formatBytes(stats.used_memory_bytes)} / {formatBytes(stats.total_memory_bytes)}
                </p>
              </>
            )}
          </div>
          <div className="text-center">
            {storage === null ? (
              <Unreported label="Storage Usage" caption={caption} statusMessage={statusMessage} />
            ) : (
              <>
                <GaugeChart value={storage} label="Storage Usage" />
                <p className="text-sm text-muted-foreground mt-2">
                  {formatBytes(stats.used_storage_bytes)} / {formatBytes(stats.total_storage_bytes)}
                </p>
              </>
            )}
          </div>
          <div className="text-center">
            {scope.unavailable ? (
              <Unreported label="GPU Allocation" caption={scope.unavailable} statusMessage={statusMessage} />
            ) : (
              <GpuAllocationGauge stats={stats} scope={scopeLabel} />
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
