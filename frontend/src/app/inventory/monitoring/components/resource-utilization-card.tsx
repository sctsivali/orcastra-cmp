"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CpuUsageGauge, type CpuUsageReading } from "@/components/monitoring/cpu-usage-gauge";
import { GaugeChart } from "@/components/monitoring/gauge-chart";
import { GpuAllocationGauge } from "@/components/monitoring/gpu-allocation-gauge";
import { cpuAllocation } from "@/lib/cpu-allocation";
import { formatBytes, formatMemory } from "@/lib/utils";

/** The scope's host figures, as MonitoringContent's filteredStats carries them. */
export interface ResourceUtilizationStats {
  cpu_reading: CpuUsageReading;
  used_cpu: number;
  total_cpu: number;
  memory_usage_percent: number;
  used_memory_bytes: number;
  total_memory_bytes: number;
  storage_usage_percent: number;
  used_storage_bytes: number;
  total_storage_bytes: number;
  total_gpu: number;
  used_gpu: number;
  total_gpu_cards?: number;
  status_message?: string | null;
}

interface ResourceUtilizationCardProps {
  stats: ResourceUtilizationStats;
  /** A cluster id, or "all". */
  selectedNode: string;
  isPartner: boolean;
  colors: { cpu: string; memory: string; storage: string; gpu: string };
}

export function ResourceUtilizationCard({ stats, selectedNode, isPartner, colors }: ResourceUtilizationCardProps) {
  return (
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
              reading={stats.cpu_reading}
              label="CPU Usage"
              color={colors.cpu}
              statusMessage={isPartner && stats.cpu_reading.status !== "ready" && stats.status_message ? stats.status_message : undefined}
            />
            <p className="text-sm text-muted-foreground mt-2">
              Allocated: {stats.used_cpu} vCPU / {stats.total_cpu} hardware threads (
              {cpuAllocation({ cpu_total: stats.total_cpu, cpu_used: stats.used_cpu }).ratioLabel})
            </p>
          </div>
          <div className="text-center">
            <GaugeChart 
              value={stats.memory_usage_percent} 
              label="Memory Usage" 
              color={colors.memory}
              statusMessage={isPartner && stats.memory_usage_percent === 0 && stats.status_message ? stats.status_message : undefined}
            />
            <p className="text-sm text-muted-foreground mt-2">
              {formatMemory(stats.used_memory_bytes)} / {formatMemory(stats.total_memory_bytes)}
            </p>
          </div>
          <div className="text-center">
            <GaugeChart 
              value={stats.storage_usage_percent} 
              label="Storage Usage" 
              color={colors.storage}
              statusMessage={isPartner && stats.storage_usage_percent === 0 && stats.status_message ? stats.status_message : undefined}
            />
            <p className="text-sm text-muted-foreground mt-2">
              {formatBytes(stats.used_storage_bytes)} / {formatBytes(stats.total_storage_bytes)}
            </p>
          </div>
          <div className="text-center">
            <GpuAllocationGauge
              stats={stats}
              scope={selectedNode}
              color={colors.gpu}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
