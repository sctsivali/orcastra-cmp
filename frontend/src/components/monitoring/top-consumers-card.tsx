"use client";

import { memo, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatVcpuAmount, formatVcpuCount } from "@/lib/instance-metrics-view";
import { formatBytes, formatMemory, formatNetworkRate } from "@/lib/utils";
import type { InstanceMonitoringSummary, MonitoringDashboardResponse } from "@/types/api";

type Resource = "cpu" | "memory" | "storage" | "network";

function consumerKey(instance: InstanceMonitoringSummary): string {
  return `${instance.host}:${instance.project || "default"}:${instance.name}`;
}

/**
 * Every instance the dashboard named as a leader of any resource.
 *
 * note: an older backend sends only its top CPU and top memory lists; their union is the best
 * pool it offers, so a mixed deploy still renders.
 */
export function consumerPool(
  data: Pick<MonitoringDashboardResponse, "consumers" | "top_cpu_consumers" | "top_memory_consumers">,
): InstanceMonitoringSummary[] {
  if (data.consumers) return data.consumers;
  const pool = new Map<string, InstanceMonitoringSummary>();
  for (const instance of [...(data.top_cpu_consumers ?? []), ...(data.top_memory_consumers ?? [])]) {
    if (!pool.has(consumerKey(instance))) pool.set(consumerKey(instance), instance);
  }
  return [...pool.values()];
}

function networkRate(instance: InstanceMonitoringSummary): number | null {
  const rx = instance.network_rx_bytes_per_sec;
  const tx = instance.network_tx_bytes_per_sec;
  if ((rx === null || rx === undefined) && (tx === null || tx === undefined)) return null;
  return (rx ?? 0) + (tx ?? 0);
}

function cpuWeight(instance: InstanceMonitoringSummary): number | null {
  if (instance.cpu_busy_vcpus !== null && instance.cpu_busy_vcpus !== undefined) {
    return instance.cpu_busy_vcpus;
  }
  return instance.cpu_usage !== null && instance.cpu_usage !== undefined ? instance.cpu_usage / 100 : null;
}

/** Ranks one resource on its own: the heaviest disk user is listed even when idle on CPU. */
function rank(pool: InstanceMonitoringSummary[], resource: Resource, topN: number) {
  const scored = pool
    .map((instance) => {
      const score =
        resource === "cpu"
          ? cpuWeight(instance)
          : resource === "memory"
            ? instance.memory_usage_bytes
            : resource === "storage"
              ? instance.disk_usage_bytes
              : networkRate(instance);
      return { instance, score };
    })
    .filter((entry): entry is { instance: InstanceMonitoringSummary; score: number } => entry.score !== null);
  return scored
    .sort((a, b) => b.score - a.score)
    .slice(0, topN)
    .map((entry) => entry.instance);
}

const EMPTY: Record<Resource, string> = {
  cpu: "CPU readings appear after the next collection",
  memory: "No running instances",
  storage: "No running instances",
  network: "Network rates appear after the next collection",
};

export function TopConsumersCard({
  pool,
  selectedNode,
  onOpen,
}: {
  pool: InstanceMonitoringSummary[];
  selectedNode: string;
  onOpen: (instance: InstanceMonitoringSummary) => void;
}) {
  const [topN, setTopN] = useState(5);
  const scoped = useMemo(
    () => (selectedNode === "all" ? pool : pool.filter((instance) => instance.host === selectedNode)),
    [pool, selectedNode],
  );
  const rankings = useMemo(
    () => ({
      cpu: rank(scoped, "cpu", topN),
      memory: rank(scoped, "memory", topN),
      storage: rank(scoped, "storage", topN),
      network: rank(scoped, "network", topN),
    }),
    [scoped, topN],
  );

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <CardTitle>Top Consumers</CardTitle>
            <CardDescription>Running instances using the most of each resource</CardDescription>
          </div>
          <Select value={topN.toString()} onValueChange={(v) => setTopN(parseInt(v, 10))}>
            <SelectTrigger className="w-[80px] sm:w-[90px] h-8 shrink-0" aria-label="Instances to list">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="5">Top 5</SelectItem>
              <SelectItem value="10">Top 10</SelectItem>
              <SelectItem value="15">Top 15</SelectItem>
              <SelectItem value="20">Top 20</SelectItem>
            </SelectContent>
          </Select>
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
          {(Object.keys(rankings) as Resource[]).map((resource) => (
            <TabsContent key={resource} value={resource} className="space-y-2 max-h-[280px] overflow-y-auto">
              {rankings[resource].length > 0 ? (
                rankings[resource].map((instance, index) => (
                  <TopConsumerRow
                    key={consumerKey(instance)}
                    instance={instance}
                    rank={index + 1}
                    type={resource}
                    onOpen={onOpen}
                  />
                ))
              ) : (
                <p className="text-center text-muted-foreground py-4">
                  {scoped.length === 0 ? "No running instances" : EMPTY[resource]}
                </p>
              )}
            </TabsContent>
          ))}
        </Tabs>
      </CardContent>
    </Card>
  );
}

const TopConsumerRow = memo(function TopConsumerRow({
  instance,
  rank: position,
  type,
  onOpen,
}: {
  instance: InstanceMonitoringSummary;
  rank: number;
  type: Resource;
  onOpen: (instance: InstanceMonitoringSummary) => void;
}) {
  const project = instance.project || "default";
  return (
    <button
      type="button"
      aria-label={`Open resource details for ${instance.name} on ${instance.host} in project ${project}`}
      onClick={() => onOpen(instance)}
      className="flex w-full items-center gap-3 rounded-lg bg-muted/50 p-3 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border bg-card text-sm font-semibold tabular-nums">
        {position}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{instance.name}</p>
        <p className="text-xs text-muted-foreground truncate">{instance.host} / {project}</p>
      </div>
      <div className="text-right shrink-0">
        <ConsumerValue instance={instance} type={type} />
        <Badge variant="outline" className="text-xs mt-1">
          {instance.type === "virtual-machine" ? "VM" : "CT"}
        </Badge>
      </div>
    </button>
  );
});

function ConsumerValue({ instance, type }: { instance: InstanceMonitoringSummary; type: Resource }) {
  switch (type) {
    case "cpu": {
      const busy = instance.cpu_busy_vcpus;
      const vcpus = instance.cpu_vcpus;
      return (
        <>
          <p className="text-sm font-bold text-blue-600 tabular-nums">
            {instance.cpu_usage !== null && instance.cpu_usage !== undefined
              ? `${instance.cpu_usage.toFixed(1)}%`
              : `${formatVcpuAmount(busy ?? 0)} vCPUs`}
          </p>
          <p className="text-xs text-muted-foreground tabular-nums">
            {busy !== null && busy !== undefined && vcpus
              ? `${formatVcpuAmount(busy)} of ${formatVcpuCount(vcpus)} busy`
              : "CPU Usage"}
          </p>
        </>
      );
    }
    case "memory":
      return (
        <>
          <p className="text-sm font-bold text-green-600 tabular-nums">{formatMemory(instance.memory_usage_bytes)}</p>
          <p className="text-xs text-muted-foreground">Memory, with page cache</p>
        </>
      );
    case "storage":
      return (
        <>
          <p className="text-sm font-bold text-amber-600 tabular-nums">{formatBytes(instance.disk_usage_bytes)}</p>
          <p className="text-xs text-muted-foreground">Disk Usage</p>
        </>
      );
    case "network":
      return (
        <>
          <p className="text-sm font-bold text-purple-600 tabular-nums">
            ↓{formatNetworkRate(instance.network_rx_bytes_per_sec ?? 0)}
          </p>
          <p className="text-sm font-bold text-pink-600 tabular-nums">
            ↑{formatNetworkRate(instance.network_tx_bytes_per_sec ?? 0)}
          </p>
        </>
      );
  }
}
