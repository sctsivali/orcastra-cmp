"use client";

import { useState, useMemo } from "react";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuCardsCaption,
  gpuDenseRatio,
  summarizeGpus,
} from "@/lib/gpu";

import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { Server, Cloud, Cpu, MemoryStick, Sparkles, MoreVertical, Edit, Users } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { LXDVersionChecker } from "./lxd-version-checker";
import { NodeConnectionSummary } from "./node-connection-summary";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NodeStatus } from "@/types";
import { useNodesStatus, useUpdateNodeTags, useUpdateNodeDisplayName } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { cn } from "@/lib/utils";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { NodeDetailModal } from "./node-detail-modal";
import { NodeStatusCard } from "./node-status/node-status-card";
import { TagManager, TagFilter } from "@/components/tag-manager";
import { toast } from "@/hooks/use-toast";

function LoadingSkeleton() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {[...Array(2)].map((_, i) => (
        <Card key={i} className="overflow-hidden">
          <Skeleton className="h-1 w-full" />
          <CardContent className="p-4">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <Skeleton className="h-10 w-10 rounded-lg" />
                <div className="space-y-2">
                  <Skeleton className="h-4 w-20" />
                  <Skeleton className="h-3 w-32" />
                </div>
              </div>
              <Skeleton className="h-5 w-20 rounded-full" />
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

interface NodeStatusCardsProps {
  /** Refresh the whole overview instead of just this section. Must reject on failure. */
  onRefresh?: () => Promise<unknown> | unknown;
  /** Timestamp of the data the parent refreshed, when onRefresh is supplied. */
  lastUpdatedAt?: number | null;
}

export function NodeStatusCards({ onRefresh, lastUpdatedAt }: NodeStatusCardsProps) {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useNodesStatus();
  const [selectedNode, setSelectedNode] = useState<NodeStatus | null>(null);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);

  // Collect all unique tags from nodes
  const allTags = useMemo(() => {
    const tags = new Set<string>();
    data?.nodes.forEach((node) => {
      node.tags?.forEach((tag) => tags.add(tag));
    });
    return Array.from(tags).sort();
  }, [data?.nodes]);

  // Filter nodes by tags and status (exclude pending/not_authorized nodes)
  const filteredNodes = useMemo(() => {
    if (!data?.nodes) return [];
    
    // Filter out nodes with pending/not_authorized status
    // Only show truly authenticated nodes (connected AND has server_version)
    const onlineNodes = data.nodes.filter(node => node.connected && node.server_version);
    
    if (selectedTags.length === 0) return onlineNodes;
    
    return onlineNodes.filter((node) => {
      const nodeTags = node.tags || [];
      return selectedTags.some(tag => nodeTags.includes(tag));
    });
  }, [data?.nodes, selectedTags]);

  const handleViewDetails = (node: NodeStatus) => {
    setSelectedNode(node);
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setSelectedNode(null);
  };

  if (isLoading) {
    return <LoadingSkeleton />;
  }

  return (
    <>
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Clusters</h2>
            <NodeConnectionSummary nodes={data?.nodes} allConnected={data?.all_connected} />
            {selectedTags.length > 0 && (
              <Badge variant="outline" className="text-xs">
                {filteredNodes.length} of {data?.nodes.length || 0}
              </Badge>
            )}
          </div>
          <RefreshButton
            label="Refresh overview"
            lastUpdatedAt={onRefresh ? lastUpdatedAt : dataUpdatedAt}
            onRefresh={onRefresh ?? refreshNow}
          />
        </div>

        {/* Tag Filter */}
        {allTags.length > 0 && (
          <TagFilter
            availableTags={allTags}
            selectedTags={selectedTags}
            onTagsChange={setSelectedTags}
          />
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {filteredNodes.map((node, index) => (
            <NodeStatusCard
              key={node.name}
              node={node}
              index={index}
              onViewDetails={handleViewDetails}
              onTagsUpdate={refetch}
            />
          ))}
        </div>
      </div>

      <NodeDetailModal
        node={selectedNode}
        isOpen={isModalOpen}
        onClose={handleCloseModal}
      />
    </>
  );
}
