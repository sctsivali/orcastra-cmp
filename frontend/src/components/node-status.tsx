"use client";

import { useState, useMemo, useCallback } from "react";
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
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
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
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import { NodeDetailModal } from "./node-detail/node-detail-modal";
import { useLiveRecord } from "@/hooks/use-live-record";
import { NodeStatusCard } from "./node-status/node-status-card";
import { TagManager, TagFilter } from "@/components/tag-manager";
import { toast } from "@/hooks/use-toast";

const nodeId = (node: NodeStatus) => node.name;

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
  // important: the id, not the row. Storing the clicked NodeStatus object here meant the open
  // dialog rendered a photograph taken at click time: the list kept polling underneath and the
  // numbers behind the dialog moved while the ones inside it did not.
  const [selectedNodeName, setSelectedNodeName] = useState<string | null>(null);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(GRID_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  // important: narrowing the tag filter has to send the operator back to page one. Without this a
  // filter applied from page three keeps that page number, and the clamp then paints the last page
  // of the new result instead of its first.
  const handleTagsChange = useCallback(
    (tags: string[]) => {
      setSelectedTags(tags);
      resetPage();
    },
    [resetPage],
  );

  // note: data.nodes, not filteredNodes. The grid filters to connected && server_version, so a
  // cluster dropping offline would vanish from the filtered list and blank the open dialog.
  // Selecting from the raw list keeps it alive and lets it report that the cluster went
  // offline, which is the thing an operator opened it to find out.
  const { record: selectedNode, isOrphaned } = useLiveRecord(
    data?.nodes,
    selectedNodeName,
    nodeId,
  );

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

  // Filter, then slice for the page. This grid has no sort control, so the filtered set is already
  // the whole ordered set the page is taken from.
  // note: clamped on read rather than corrected in an effect, which would snap the operator back a
  // page mid-task and still paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(filteredNodes.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedNodes = filteredNodes.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const handleViewDetails = (node: NodeStatus) => setSelectedNodeName(node.name);

  const handleCloseModal = () => setSelectedNodeName(null);

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
            onTagsChange={handleTagsChange}
          />
        )}

        {filteredNodes.length === 0 && selectedTags.length > 0 ? (
          // A tag filter that matches nothing is not the same as having no clusters, so it says so
          // and offers the way back out.
          <EmptyState
            icon={Server}
            title="No cluster matches the current tags"
            description={`${selectedTags.length} tag filter${selectedTags.length === 1 ? "" : "s"} active`}
            action={
              <Button size="sm" variant="outline" onClick={() => handleTagsChange([])}>
                Clear filters
              </Button>
            }
          />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* note: the index is the position on the page, not in the list, so the card stagger
                restarts each page instead of delaying page four by several seconds. */}
            {pagedNodes.map((node, index) => (
              <NodeStatusCard
                key={node.name}
                node={node}
                index={index}
                onViewDetails={handleViewDetails}
                onTagsUpdate={refetch}
              />
            ))}
          </div>
        )}

        {/* Absent rather than disabled when everything fits. The parent is already space-y-4. */}
        {totalPages > 1 && (
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            pageSize={pageSize}
            pageSizeOptions={[...GRID_PAGE_SIZES]}
            totalItems={filteredNodes.length}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        )}
      </div>

      <NodeDetailModal
        isOpen={selectedNodeName !== null}
        isOrphaned={isOrphaned}
        listCollectedAt={data?.collected_at}
        node={selectedNode}
        onClose={handleCloseModal}
      />
    </>
  );
}
