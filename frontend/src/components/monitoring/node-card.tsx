"use client";

import { memo } from "react";
import { cpuAllocation, cpuAllocationText } from "@/lib/cpu-allocation";
import { Play, Server } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { usePermissions } from "@/hooks/use-permissions";
import { ClusterStatus } from "@/types/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import {
  Tooltip as UITooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuCardsCaption,
  gpuDenseRatio,
  summarizeGpus,
} from "@/lib/gpu";
import { formatBytes, formatHypervisorType, formatMemory } from "@/lib/utils";
import { capacityReading } from "@/lib/capacity-reading";
import type { ClusterMonitoringMetrics } from "@/types/api";

// Node Card Component
interface NodeCardProps {
  node: ClusterMonitoringMetrics;
}

export const NodeCard = memo(function NodeCard({ node }: NodeCardProps) {
  const { isPartner } = usePermissions();
  const gpu = summarizeGpus(node);
  const cpu = cpuAllocation(node);
  const rawStatus = clusterStatusMeta(node);
  const memory = capacityReading({ used: node.memory_used_bytes, total: node.memory_total_bytes });
  const storage = capacityReading({ used: node.storage_used_bytes, total: node.storage_total_bytes });
  // important: "Pending Trust" and "Error" are certificate-handshake and platform vocabulary a
  // tenant cannot act on, and the operator-surface gate pins that neither reaches one. The body
  // copy below already drew that line; the badge did not, and rendered the raw label. Every
  // not-online state means one thing to a tenant: this node is not answering.
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
    <Card className="transition-all hover:shadow-md">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Server className="h-5 w-5 shrink-0 text-muted-foreground" />
            <CardTitle className="text-base">{node.node_name}</CardTitle>
          </div>
          {/* status_message is the backend exception reflected back (monitoring.py builds it
              from node.error, which is str(e)), so it carries the endpoint, port and TLS
              detail. The badge already says the node is not answering. */}
          {isPartner && node.status_message && !node.is_online ? (
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
          {formatHypervisorType(node.node_type)} • {node.instance_count} instances
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {node.is_online ? (
          <>
            {/* CPU */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">vCPU Allocation</span>
                <span className="font-medium">{cpu.allocated} vCPU / {cpu.threads} hardware threads</span>
              </div>
              <Progress value={cpu.percentClamped} className={`h-2 ${cpu.meterClassName}`} />
              <p className={`text-xs text-right ${cpu.textClassName}`}>{cpuAllocationText(cpu)}</p>
            </div>
            
            {/* Memory */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Memory</span>
                <span className="font-medium">{formatMemory(node.memory_used_bytes)} / {formatMemory(node.memory_total_bytes)}</span>
              </div>
              {memory && <Progress value={memory.percent} className="h-2" />}
              <p className="text-xs text-muted-foreground text-right">{memory ? `${memory.percent.toFixed(1)}%` : "Usage unavailable"}</p>
            </div>
            
            {/* Storage */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Storage</span>
                <span className="font-medium">{formatBytes(node.storage_used_bytes)} / {formatBytes(node.storage_total_bytes)}</span>
              </div>
              {storage && <Progress value={storage.percent} className="h-2" />}
              <p className="text-xs text-muted-foreground text-right">{storage ? `${storage.percent.toFixed(1)}%` : "Usage unavailable"}</p>
            </div>
            
            {/* GPU. Allocation only: LXD reports no GPU telemetry. The cards count sits on
                the line below the ratio so "0 / 1" can never read as "not detected". */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">GPU</span>
                <span className="font-medium">
                  {gpu.hasCompute
                    ? gpuDenseRatio(gpu)
                    : gpu.hasCards
                      ? GPU_NO_COMPUTE_TITLE
                      : GPU_EMPTY_TITLE}
                </span>
              </div>
              {gpu.hasCompute && <Progress value={gpu.allocationPercent ?? 0} className="h-2" />}
              {(gpu.hasCompute || gpu.hasCards) && (
                <p className="text-xs text-muted-foreground text-right">
                  {gpu.hasCompute && `${(gpu.allocationPercent ?? 0).toFixed(1)}% allocated`}
                  {gpu.hasCompute && gpuCardsCaption(gpu) ? " • " : ""}
                  {gpuCardsCaption(gpu)}
                </p>
              )}
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
          // note: the dimming lives here and not on the Card. Fading the whole card faded the
          // status chip with it, which put a solid chip back at tint strength.
          <div className="text-center py-4 text-muted-foreground opacity-60">
            <Server className="h-8 w-8 mx-auto mb-2 opacity-50" />
            {/* One source for the word, so the badge above and this line can never disagree.
                It used to compare node.cluster_status to the literal "pending_trust", which the
                backend no longer emits. Kept as a sentence: the badge is the label, this is the
                card's empty state and reads better saying what it means. */}
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
