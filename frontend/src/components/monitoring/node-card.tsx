"use client";

import { memo } from "react";
import { cpuAllocation } from "@/lib/cpu-allocation";
import {
  CpuAllocationCaption,
  CpuAllocationFigure,
  CpuAllocationInfo,
  CpuAllocationMeter,
} from "@/components/cpu/cpu-allocation-info";
import { GpuCapacityInfo } from "@/components/gpu";
import { Play, Server } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { usePermissions } from "@/hooks/use-permissions";
import { useByteFormat } from "@/hooks/use-byte-format";
import { ClusterStatus } from "@/types/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import {
  Tooltip as UITooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuCardsCaption,
  gpuNoComputeCaption,
  gpuDenseRatio,
  summarizeGpus,
} from "@/lib/gpu";
import { EMPTY_VALUE, formatHypervisorType, plural } from "@/lib/utils";
import { capacityReading } from "@/lib/capacity-reading";
import { usageLevel } from "@/lib/usage-tone";
import type { ClusterMonitoringMetrics } from "@/types/api";

interface NodeCardProps {
  node: ClusterMonitoringMetrics;
  /** What the Clusters page calls this cluster. Defaults to its id. */
  displayName?: string;
}

// A Tenant is sent a cluster's status and its own instance counts only: every host figure is
// null instead of zero, so there is nothing to draw.
function hostFigures(node: ClusterMonitoringMetrics) {
  const {
    cpu_total, cpu_used, memory_used_bytes, memory_total_bytes,
    storage_used_bytes, storage_total_bytes, gpu_total, gpu_used,
  } = node;
  if (
    cpu_total == null || cpu_used == null || memory_used_bytes == null || memory_total_bytes == null ||
    storage_used_bytes == null || storage_total_bytes == null || gpu_total == null || gpu_used == null
  ) {
    return null;
  }
  return {
    cpu: cpuAllocation({
      cpu_total,
      cpu_used,
      cpu_allocated: node.cpu_allocated ?? undefined,
      cpu_allocation_ratio: node.cpu_allocation_ratio ?? undefined,
    }),
    gpu: summarizeGpus({
      gpu_total,
      gpu_used,
      gpu_available: node.gpu_available ?? undefined,
      gpu_cards_total: node.gpu_cards_total ?? undefined,
      gpu_display_total: node.gpu_display_total ?? undefined,
      gpu_integrated_total: node.gpu_integrated_total ?? undefined,
    }),
    memoryUsed: memory_used_bytes,
    memoryTotal: memory_total_bytes,
    storageUsed: storage_used_bytes,
    storageTotal: storage_total_bytes,
    memory: capacityReading({ used: memory_used_bytes, total: memory_total_bytes }),
    storage: capacityReading({ used: storage_used_bytes, total: storage_total_bytes }),
  };
}

function NodeHostFigures({ host }: { host: NonNullable<ReturnType<typeof hostFigures>> }) {
  const { formatBytes } = useByteFormat();
  const { cpu, gpu, memory } = host;
  // A cluster that sent 0 / 0 for storage has nothing to measure; an empty bar would read as
  // an empty disk.
  const storage = host.storage && host.storage.total > 0 ? host.storage : null;
  const memoryFigure = `${formatBytes(host.memoryUsed)} / ${formatBytes(host.memoryTotal)}`;
  const storageFigure = `${formatBytes(host.storageUsed)} / ${formatBytes(host.storageTotal)}`;
  return (
    <>
      <div className="space-y-1.5">
        <div className="flex justify-between text-sm">
          <span className="inline-flex items-center gap-1.5 text-muted-foreground">
            vCPU Allocation
            <CpuAllocationInfo cpu={cpu} />
          </span>
          <CpuAllocationFigure cpu={cpu} className="text-right font-medium" />
        </div>
        <CpuAllocationMeter cpu={cpu} className="h-2" />
        <p className="text-xs text-right">
          <CpuAllocationCaption cpu={cpu} />
        </p>
      </div>
      
      <div className="space-y-1.5">
        <div className="flex justify-between text-sm">
          <span className="text-muted-foreground">Memory</span>
          <span className="font-medium">{memoryFigure}</span>
        </div>
        {memory && (
          <UsageBar
            percent={memory.percent}
            label="Memory"
            valueText={`${memoryFigure} (${memory.percent.toFixed(1)}%)`}
            className="h-2"
          />
        )}
        <p className="text-xs text-right">
          {memory ? (
            <UsageCaption level={usageLevel(memory.percent)}>{`${memory.percent.toFixed(1)}%`}</UsageCaption>
          ) : (
            <span className="text-muted-foreground">Usage unavailable</span>
          )}
        </p>
      </div>
      
      <div className="space-y-1.5">
        <div className="flex justify-between text-sm">
          <span className="text-muted-foreground">Storage</span>
          <span className="font-medium">{storage ? storageFigure : EMPTY_VALUE}</span>
        </div>
        {storage && (
          <UsageBar
            percent={storage.percent}
            label="Storage"
            valueText={`${storageFigure} (${storage.percent.toFixed(1)}%)`}
            className="h-2"
          />
        )}
        <p className="text-xs text-right">
          {storage ? (
            <UsageCaption level={usageLevel(storage.percent)}>{`${storage.percent.toFixed(1)}%`}</UsageCaption>
          ) : (
            <span className="text-muted-foreground">Usage unavailable</span>
          )}
        </p>
      </div>
      
      {/* GPU allocation only: LXD reports no GPU telemetry. The cards count sits on the
          line below the ratio so "0 / 1" never reads as "not detected". A fully allocated GPU
          is no room for another GPU instance, so the bar is on the same ramp as the others. */}
      <div className="space-y-1.5">
        <div className="flex justify-between text-sm">
          <span className="inline-flex items-center gap-1.5 text-muted-foreground">
            GPU
            <GpuCapacityInfo />
          </span>
          <span className="font-medium">
            {gpu.hasCompute
              ? gpuDenseRatio(gpu)
              : gpu.hasCards
                ? GPU_NO_COMPUTE_TITLE
                : GPU_EMPTY_TITLE}
          </span>
        </div>
        {gpu.hasCompute && (
          <UsageBar
            percent={gpu.allocationPercent ?? 0}
            label="GPU allocation"
            valueText={`${gpuDenseRatio(gpu)} (${(gpu.allocationPercent ?? 0).toFixed(1)}%)`}
            className="h-2"
          />
        )}
        {(gpu.hasCompute || gpu.hasCards) && (
          <p className="text-xs text-muted-foreground text-right">
            {gpu.hasCompute && (
              <UsageCaption level={usageLevel(gpu.allocationPercent ?? 0)}>
                {`${(gpu.allocationPercent ?? 0).toFixed(1)}% allocated`}
              </UsageCaption>
            )}
            {gpu.hasCompute && gpuCardsCaption(gpu) ? " · " : ""}
            {gpu.hasCompute ? gpuCardsCaption(gpu) : gpuNoComputeCaption(gpu)}
          </p>
        )}
      </div>
    </>
  );
}

export const NodeCard = memo(function NodeCard({ node, displayName }: NodeCardProps) {
  const { isPartner } = usePermissions();
  const label = displayName || node.node_name;
  const host = hostFigures(node);
  const rawStatus = clusterStatusMeta(node);
  // important: "Pending Trust" and "Error" are certificate-handshake and platform vocabulary a
  // tenant cannot act on, and the operator-surface gate pins that neither reaches one. To a
  // tenant every not-online state means the same thing: this node is not answering.
  const statusMeta =
    isPartner || rawStatus.status === ClusterStatus.ONLINE
      ? rawStatus
      : {
          ...clusterStatusMeta(ClusterStatus.OFFLINE),
          label: "Unavailable",
          srLabel: "Status: Unavailable",
        };
  const getStatusBadge = () => (
    <Badge variant={statusMeta.tone} className="shrink-0 whitespace-nowrap">
      {statusMeta.label}
    </Badge>
  );

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Server className="h-5 w-5 shrink-0 text-muted-foreground" />
            {/* The display name leads and the id sits under it, as on the Clusters page. */}
            <div className="min-w-0">
              <CardTitle className="text-base break-words">{label}</CardTitle>
              {label !== node.node_name ? (
                <p className="text-xs text-muted-foreground break-all">{node.node_name}</p>
              ) : null}
            </div>
          </div>
          {/* status_message is the backend exception reflected back (monitoring.py builds it
              from node.error, which is str(e)), so it carries the endpoint, port and TLS
              detail. The badge already says the node is not answering. An online Cluster whose
              instance list ran out of time carries its note here too. */}
          {isPartner && node.status_message && (!node.is_online || node.collection_timed_out) ? (
            <UITooltip>
              <TooltipTrigger asChild>
                <div className="cursor-help">
                  {getStatusBadge()}
                </div>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">
                <p className="text-sm">{node.status_message}</p>
              </TooltipContent>
            </UITooltip>
          ) : (
            getStatusBadge()
          )}
        </div>
        <CardDescription>
          {node.node_type ? `${formatHypervisorType(node.node_type)} · ` : ""}
          {node.is_online && node.collection_timed_out
            ? "instances not counted in this collection"
            : plural(node.instance_count, "instance")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {node.is_online ? (
          <>
            {host && <NodeHostFigures host={host} />}

            <div className="flex items-center justify-between pt-2 border-t">
              <span className="text-sm text-muted-foreground">Running</span>
              <div className="flex items-center gap-1">
                <Play className="h-3 w-3 text-status-online" aria-hidden="true" />
                <span className="text-sm font-medium">{node.running_instances} / {node.instance_count}</span>
              </div>
            </div>
          </>
        ) : (
          // The dimming lives here, not on the Card: fading the whole card would fade the solid
          // status chip back to tint strength.
          <div className="text-center py-4 text-muted-foreground opacity-60">
            <Server className="h-8 w-8 mx-auto mb-2 opacity-50" />
            {/* One source for the word, so the badge above and this line cannot disagree. A
                sentence here: the badge is the label, this is the card's empty state. */}
            <p className="text-sm font-medium">
              Cluster is {statusMeta.label.toLowerCase()}
            </p>
            {isPartner && node.status_message && (
              <p className="text-xs mt-1 max-w-[200px] mx-auto break-words">{node.status_message}</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
});
