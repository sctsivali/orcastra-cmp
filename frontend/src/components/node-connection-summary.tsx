"use client";

import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { NodeStatus } from "@/types";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { cn } from "@/lib/utils";

interface NodeConnectionSummaryProps {
  nodes?: NodeStatus[];
  allConnected?: boolean;
}

function getDisconnectedReason(node: NodeStatus): string {
  if (node.error) return node.error;
  if (!node.connected) return "Node endpoint is unreachable or not authenticated.";
  if (!node.server_version) return "Node is reachable but has not returned server details yet.";
  return "Connection details are incomplete.";
}

export function NodeConnectionSummary({
  nodes = [],
  allConnected = false,
}: NodeConnectionSummaryProps) {
  if (nodes.length === 0) {
    return <p className="text-xs text-muted-foreground mt-1">No Clusters</p>;
  }

  if (allConnected) {
    return <p className="text-xs text-muted-foreground mt-1">All Connected</p>;
  }

  const disconnectedNodes = nodes.filter((node) => !node.connected || !node.server_version);
  const detailNodes = disconnectedNodes.length > 0 ? disconnectedNodes : nodes;

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {`${nodes.filter((n) => n.connected).length} of ${nodes.length} connected`}
            <Info className="h-3 w-3" aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent align="start" side="bottom" className="max-w-sm p-3">
          <div className="space-y-2">
            <p className="text-xs font-semibold uppercase text-muted-foreground">
              Disconnected node details
            </p>
            <div className="space-y-2">
              {detailNodes.map((node) => (
                <div key={node.name} className="space-y-0.5 border-t border-border/60 pt-2 first:border-t-0 first:pt-0">
                  <p className="text-sm font-medium">{node.display_name || node.name}</p>
                  {node.display_name && (
                    <p className="text-xs text-muted-foreground">Node ID: {node.name}</p>
                  )}
                  <p className="break-all font-mono text-xs text-muted-foreground">{node.endpoint}</p>
                  <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <span
                      aria-hidden
                      className={cn(
                        "h-1.5 w-1.5 shrink-0 rounded-full",
                        clusterStatusMeta(node).dotClassName
                      )}
                    />
                    {clusterStatusMeta(node).label}
                  </p>
                  <p className="text-xs text-destructive">{getDisconnectedReason(node)}</p>
                </div>
              ))}
            </div>
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}