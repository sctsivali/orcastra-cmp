"use client";

import { Suspense, useState, useMemo, useCallback, useId } from "react";
import { AlertTriangle, Server } from "lucide-react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { NodeConnectionSummary } from "./node-connection-summary";
import { NodeStatus } from "@/types";
import { useNodesStatus } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import { NodeDetailModal } from "./node-detail/node-detail-modal";
import { useLiveRecord } from "@/hooks/use-live-record";
import { NodeStatusCard } from "./node-status/node-status-card";
import {
  CLUSTER_CARDS,
  ClusterGridToolbar,
  type ClusterSortField,
} from "./node-status/cluster-grid-toolbar";
import { collectedAtMs } from "@/lib/utils";

const nodeId = (node: NodeStatus) => node.name;

// Shaped like the section it stands in for: heading, toolbar, then cards with a real card's
// blocks. The fleet read takes seconds on a fleet with a WAN member, and a placeholder a third
// of the height pushed the Projects grid down by a few hundred pixels when the cards landed,
// under an operator who had already started reading it. The chrome here is the fixed-height
// part; the card blocks are the ones a NodeStatusCard renders, in order.
function LoadingSkeleton() {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} aria-busy="true" data-slot="clusters-section" className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-2">
          <h2 id={headingId} className="text-lg font-semibold">Clusters</h2>
          <Skeleton className="h-4 w-40" />
        </div>
        <Skeleton className="h-9 w-36" />
      </div>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Skeleton className="h-10 w-72" />
          <Skeleton className="h-10 w-24" />
          <Skeleton className="ml-auto h-10 w-44" />
        </div>
        <Skeleton className="h-4 w-28" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        {[...Array(3)].map((_, i) => (
          <Card key={i} className="overflow-hidden">
            <CardContent className="p-3 md:p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <Skeleton className="h-10 w-10 rounded-lg" />
                  <div className="space-y-2">
                    <Skeleton className="h-4 w-24" />
                    <Skeleton className="h-3 w-32" />
                    <Skeleton className="h-3 w-40" />
                  </div>
                </div>
                <Skeleton className="h-5 w-20 rounded" />
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="h-4 w-36" />
                <Skeleton className="h-4 w-44" />
              </div>
              <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
                {/* vCPU and Memory carry a meter between the figure and its caption; GPU is text. */}
                {[true, true, false].map((meter, j) => (
                  <div key={j} className="flex flex-col gap-1">
                    <Skeleton className="h-3.5 w-10" />
                    <Skeleton className="h-4 w-24" />
                    {meter && <Skeleton className="h-2 w-full rounded-full" />}
                    <Skeleton className="h-4 w-20" />
                  </div>
                ))}
              </div>
              <div className="mt-3 pt-3 border-t">
                <Skeleton className="h-8 w-24" />
              </div>
              <div className="mt-3 pt-3 border-t">
                <Skeleton className="h-8 w-full" />
              </div>
              <Skeleton className="mt-3 h-9 w-full" />
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}

interface NodeStatusCardsProps {
  /** Refresh the whole overview instead of just this section. Must reject on failure. */
  onRefresh?: () => Promise<unknown> | unknown;
}

// The boundary lives here, next to the useTableFilters read that needs it, so the section can
// be mounted from any page without that page having to know why.
export function NodeStatusCards(props: NodeStatusCardsProps) {
  return (
    <Suspense fallback={<LoadingSkeleton />}>
      <NodeStatusCardsContent {...props} />
    </Suspense>
  );
}

function NodeStatusCardsContent({ onRefresh }: NodeStatusCardsProps) {
  const { data, isLoading, isError, error, refetch, refreshNow, dataUpdatedAt } = useNodesStatus();
  const { navigate } = useNavigationProgress();
  const { isPartner } = usePermissions();
  const headingId = useId();
  // important: the id, not the row. Storing the clicked NodeStatus object here meant the open
  // dialog rendered a photograph taken at click time: the list kept polling underneath and the
  // numbers behind the dialog moved while the ones inside it did not.
  const [selectedNodeName, setSelectedNodeName] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(GRID_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  // note: data.nodes, not the online subset. The grid filters to connected && server_version, so
  // a cluster dropping offline would vanish from the filtered list and blank the open dialog.
  // Selecting from the raw list keeps it alive and lets it report that the cluster went
  // offline, which is the thing an operator opened it to find out.
  const { record: selectedNode, isOrphaned } = useLiveRecord(
    data?.nodes,
    selectedNodeName,
    nodeId,
  );

  // Only authenticated nodes draw a card (connected AND a server_version); pending and offline
  // ones are reported by NodeConnectionSummary above the grid instead.
  const onlineNodes = useMemo(
    () => (data?.nodes ?? []).filter((node) => node.connected && node.server_version),
    [data?.nodes],
  );

  // Built over the online subset so a tag carried only by an offline node cannot be picked
  // into an empty result.
  const allTags = useMemo(() => {
    const tags = new Set<string>();
    onlineNodes.forEach((node) => {
      node.tags?.forEach((tag) => tags.add(tag));
    });
    return Array.from(tags).sort();
  }, [onlineNodes]);

  // important: the endpoint is searchable only for the roles the card shows it to
  // (node-status-card.tsx gates it on isPartner). Matching text a tenant cannot see would
  // reveal it through the result count.
  const searchEndpoint = useCallback(
    (node: NodeStatus, query: string) => isPartner && node.endpoint.toLowerCase().includes(query),
    [isPartner],
  );

  const filters = useTableFilters(CLUSTER_CARDS, onlineNodes, {
    extraSearch: searchEndpoint,
    // One line covers every way the result set can change: the toolbar search, chip removal,
    // Clear all, and the TagFilter, which reaches setFacet and therefore the same callback.
    // Without it a filter applied from page three keeps that page number, and the clamp then
    // paints the last page of the new result instead of its first.
    onFilterChange: resetPage,
    // Tags render through TagFilter rather than the toolbar, so keep the facet out of the
    // toolbar's own controls. It still applies, and it still shows as a removable chip.
    availableFields: CLUSTER_CARDS.facetFields.filter((f) => f !== "tags"),
  });
  const selectedTags = filters.selectedValues("tags");

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    CLUSTER_CARDS.sortAccessors,
    CLUSTER_CARDS.defaultSort.field,
    CLUSTER_CARDS.defaultSort.direction,
    resetPage,
  );

  // Filter, then sort, then slice, the contract use-table-filters.ts states: the sort covers the
  // whole filtered set rather than the page an operator happens to be looking at.
  const sortedNodes = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // note: clamped on read rather than corrected in an effect, which would snap the operator back a
  // page mid-task and still paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(sortedNodes.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedNodes = sortedNodes.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const handleViewDetails = (node: NodeStatus) => setSelectedNodeName(node.name);

  const handleCloseModal = () => setSelectedNodeName(null);

  if (isLoading) {
    return <LoadingSkeleton />;
  }

  const registeredCount = data?.nodes.length ?? 0;

  return (
    <>
      {/* A landmark named by its heading: the page carries a second toolbar (Projects) with the
          same "Sort by" control, status counter and Clear search button, and the region is what
          lets a screen reader and a locator tell the two apart. */}
      <section aria-labelledby={headingId} data-slot="clusters-section" className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id={headingId} className="text-lg font-semibold">Clusters</h2>
            <NodeConnectionSummary nodes={data?.nodes} allConnected={data?.all_connected} />
          </div>
          {/* important: when the cluster data was collected. This showed the instance list's
              fetch time, so cards behind a 180 s cache read "10s ago" while minutes old. */}
          <RefreshButton
            label="Refresh overview"
            lastUpdatedAt={collectedAtMs(data?.collected_at) || dataUpdatedAt}
            onRefresh={onRefresh ?? refreshNow}
          />
        </div>

        <ClusterGridToolbar
          filters={filters}
          sortField={sortField as ClusterSortField}
          sortDirection={sortDirection}
          onSort={handleSort}
          availableTags={allTags}
          selectedTags={selectedTags}
        />

        {isError && !data ? (
          // A read that failed is not an empty fleet: a 503 while Vault is sealed or a 429 must
          // never read as "No clusters yet" with a Register Node button under it.
          <ReadErrorState noun="clusters" error={error} onRetry={() => refetch()} />
        ) : registeredCount === 0 ? (
          <EmptyState
            icon={Server}
            title="No clusters yet"
            description="Clusters appear here once one is registered and authenticated."
            action={
              isPartner ? (
                <Button size="sm" variant="outline" onClick={() => navigate("/register-node")}>
                  Register Cluster
                </Button>
              ) : undefined
            }
          />
        ) : onlineNodes.length === 0 ? (
          // Checked before the filter: a bookmarked clusters.q on a fleet that just went dark
          // would otherwise blame the search.
          <EmptyState
            icon={AlertTriangle}
            title="No cluster is online"
            description="Every registered cluster is offline or still pending. The summary above says why."
          />
        ) : sortedNodes.length === 0 && filters.activeFilterCount > 0 ? (
          // A filter that matches nothing is not the same as having no clusters, so it says so
          // and offers the way back out.
          <EmptyState
            icon={Server}
            title="No cluster matches the current filters"
            description="Try a different search, or clear a filter."
            action={
              <Button size="sm" variant="outline" onClick={filters.clearAll}>
                Clear filters
              </Button>
            }
          />
        ) : (
          // Same breakpoints as the Projects grid beside it, for the reason recorded there: a
          // two-up card at md is 224px wide and cannot hold its own header.
          <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
            {pagedNodes.map((node) => (
              <NodeStatusCard
                key={node.name}
                node={node}
                onViewDetails={handleViewDetails}
                onTagsUpdate={refetch}
              />
            ))}
          </div>
        )}

        {/* Absent rather than disabled when everything fits. The parent is already space-y-4. */}
        {totalPages > 1 && (
          <div data-slot="clusters-pagination">
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              pageSizeOptions={[...GRID_PAGE_SIZES]}
              totalItems={sortedNodes.length}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          </div>
        )}
      </section>

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
