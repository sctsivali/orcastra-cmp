"use client";

import * as React from "react";
import { IntentLink } from "@/components/navigation/route-intent";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { collectedAtMs, formatHypervisorType, plural } from "@/lib/utils";
import { placeShort } from "@/lib/regions/place";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { 
  Server, 
  RefreshCw,
  Trash2,
  Plus,
  Users,
  Box,
  KeyRound,
  Shield,
  MapPin,
  Loader2
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { EmptyState } from "@/components/ui/empty-state";
import { TableLink } from "@/components/ui/table-link";
import { ClusterDetailDialog } from "./components/cluster-detail-dialog";
import { LeftoverTrustDialog, type LeftoverTrust } from "./components/leftover-trust-dialog";
import { RegenerateCertificateDialog } from "./components/regenerate-certificate-dialog";
import { RegenerateCertificateConfirm } from "./components/regenerate-certificate-confirm";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle } from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { toast } from "@/hooks/use-toast";
import { ClusterInfo, ClusterStatus, type CertificateLifetime, type ClusterRegisterResponse } from "@/types/api";
import { useClusters, useCheckClusterAuth, useDeleteCluster, useRegenerateCert } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { ClusterLocationDialog } from "@/components/cluster-location-dialog";
import { useRequireRole } from "@/hooks/use-require-role";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { defineTable, fromSort } from "@/lib/table-filter";
import { Role } from "@/types/rbac";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { inventoryLinks } from "@/lib/inventory-links";
import { EmptyClustersIllustration } from "@/components/illustrations/scenes/onboarding";
import { PageReadError } from "@/components/ui/read-error-state";


const CLUSTERS = defineTable<any>()({
  searchPlaceholder: "Search clusters",
  // important: the cluster id must stay searchable, outside the Cluster column's own `search`
  // accessor. Every Node and Cluster cell in the console links here as
  // `/inventory/clusters?q=<cluster_id>` through components/ui/node-link.tsx, and the row's
  // display name can differ entirely from the id.
  //
  // searchExtra instead of a widened accessor: `search` returns one string, so widening means
  // concatenating, and `q=industrial edge` would then match across the two values and
  // correspond to nothing on screen. Each element here is tested on its own.
  searchExtra: (c) => [c.cluster_id],
  defaultSort: { field: "cluster", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (c) => c.display_name,
      search: (c) => c.display_name,
      // fromSort would give the toolbar's "Cluster contains ..." only the display name, so the
      // filter is declared over both values.
      filter: { kind: "text", value: (c) => [c.display_name, c.cluster_id] } },
    { field: "endpoint", label: "Endpoint", sort: (c) => c.endpoint, search: (c) => c.endpoint, filter: fromSort("text") },
    {
      field: "type",
      label: "Type",
      sort: (c) => c.cluster_type,
      facet: (c) => c.cluster_type,
      facetSpec: {
        primary: true,
        format: formatHypervisorType,
        // A null type is the empty bucket, which never reaches `format`. The label says the type
        // is not known yet instead of "(none)".
        emptyLabel: "(not determined)",
      } },
    {
      field: "zone",
      label: "Zone",
      sort: (c) => c.zone,
      search: (c) => c.zone,
      facet: (c) => c.zone,
      facetSpec: { emptyLabel: "(no zone)" } },
    {
      field: "status",
      label: "Status",
      sort: (c) => c.status,
      facet: (c) => c.status,
      facetSpec: { primary: true } },
  ] });

function ClustersContent() {
  const { navigate } = useNavigationProgress();
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt, isError, error } = useClusters();
  const clusters = React.useMemo(() => (data as any)?.clusters || [], [data]);
  const [detailCluster, setDetailCluster] = React.useState<ClusterInfo | null>(null);
  const [checkingAuth, setCheckingAuth] = React.useState<string | null>(null);
  const [regenerated, setRegenerated] = React.useState<{ cluster: ClusterInfo; issued: ClusterRegisterResponse } | null>(null);
  const [regenerating, setRegenerating] = React.useState<ClusterInfo | null>(null);
  const [deleteConfirmModal, setDeleteConfirmModal] = React.useState<{open: boolean, cluster: any | null}>({open: false, cluster: null});
  const [leftoverTrust, setLeftoverTrust] = React.useState<LeftoverTrust | null>(null);
  const [deleteConfirmText, setDeleteConfirmText] = React.useState("");
  const [locationModal, setLocationModal] = React.useState<{open: boolean, cluster: any | null}>({open: false, cluster: null});
  const { isReadOnly } = useReadOnlyMode();
  const checkClusterAuth = useCheckClusterAuth();
  const deleteCluster = useDeleteCluster();
  const regenerateCert = useRegenerateCert();

  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(TABLE_PAGE_SIZE);
  const resetPage = React.useCallback(() => setPage(1), []);

  const filters = useTableFilters(CLUSTERS, clusters as any[], {
    onFilterChange: resetPage,
  });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    CLUSTERS.sortAccessors,
    CLUSTERS.defaultSort.field,
    CLUSTERS.defaultSort.direction,
    resetPage,
  );

  const sortedClusters = React.useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // Clamped on read instead of corrected in an effect. Deleting a cluster and the background
  // refetch both shrink this list under the operator, and an effect would fire only after an
  // out-of-range page had already painted an empty table.
  const totalPages = Math.max(1, Math.ceil(sortedClusters.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedClusters = sortedClusters.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  const handleCheckAuth = async (clusterId: string) => {
    setCheckingAuth(clusterId);
    try {
      const response = await checkClusterAuth.mutateAsync(clusterId);
      
      if (response.authenticated) {
        toast({
          title: "Cluster authenticated",
          description: `Cluster ${clusterId} is online. Refreshing instance data…` });
        // the mutation's onSuccess reconciles cluster.auth-check, which covers every list a
        // newly trusted cluster adds to (instances, projects, images, pickers)
      } else {
        toast({
          title: "Cluster not authenticated",
          description: response.message || "Certificate not yet trusted.",
          variant: "destructive" });
      }
    } catch (error: any) {
      toast({
        title: "Could not check authentication",
        description: error.message || "Try again.",
        variant: "destructive" });
    } finally {
      setCheckingAuth(null);
    }
  };


  const handleRegenerateCert = async (cluster: ClusterInfo, lifetime: CertificateLifetime | undefined) => {
    if (regenerateCert.isPending) return;
    try {
      const response = await regenerateCert.mutateAsync({ clusterId: cluster.cluster_id, lifetime });
      setRegenerating(null);
      if (response.success && response.certificate) {
        setRegenerated({
          cluster,
          // The type this page already knows wins over the unauthenticated probe's answer.
          issued: { ...response, cluster_type: cluster.cluster_type || response.cluster_type },
        });
        toast({
          title: "Certificate regenerated",
          description: response.pending_cert_id
            ? "The current certificate stays active. Run the command on the cluster (any one member is enough), then verify."
            : "Copy and run the command on the cluster (any one member is enough) to add trust." });
      }
    } catch (error: any) {
      toast({
        title: "Could not regenerate certificate",
        description: error.message || "Try again.",
        variant: "destructive" });
    }
  };

  const handleDeleteClick = (cluster: any) => {
    setDeleteConfirmModal({ open: true, cluster });
    setDeleteConfirmText("");
  };

  const handleDeleteConfirm = async () => {
    if (!deleteConfirmModal.cluster) return;
    const clusterId = deleteConfirmModal.cluster.cluster_id;
    
    try {
      const result = await deleteCluster.mutateAsync(clusterId);
      toast({
        title: "Cluster removed",
        description: `Cluster ${clusterId} has been removed.` });
      setDeleteConfirmModal({ open: false, cluster: null });
      setDeleteConfirmText("");
      if (result?.manual_trust_commands?.length || result?.trust_cleanup_complete === false) {
        setLeftoverTrust({
          clusterId,
          commands: result.manual_trust_commands ?? [],
          clusterType: deleteConfirmModal.cluster.cluster_type,
        });
      }
      // useDeleteCluster.onSuccess reconciles cluster.delete: this page's active queries
      // refetch now, and every other list it touched refetches when its page next mounts.
    } catch (error: any) {
      toast({
        title: "Could not remove cluster",
        description: error.message || "Try again.",
        variant: "destructive" });
    }
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="Registered Clusters"
        description="Manage hypervisor clusters"
        noun="clusters"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="container py-8">
      <ReadOnlyBanner />
      <RegenerateCertificateConfirm
        cluster={regenerating}
        pending={regenerateCert.isPending}
        onCancel={() => setRegenerating(null)}
        onConfirm={(lifetime) => regenerating && void handleRegenerateCert(regenerating, lifetime)}
      />
      <RegenerateCertificateDialog
        cluster={regenerated?.cluster ?? null}
        issued={regenerated?.issued ?? null}
        onClose={() => setRegenerated(null)}
        onActivated={() => {
          setRegenerated(null);
          void refetch();
        }}
      />

      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Registered Clusters</h1>
          <p className="text-muted-foreground mt-1">
            Manage hypervisor clusters
          </p>
        </div>
        <div className="flex gap-2">
          <Button asChild variant="outline" className="gap-2 shrink-0" size="sm">
            <IntentLink href="/inventory/clusters/certificates">
              <Shield className="h-4 w-4" />
              <span className="hidden sm:inline">Certificates</span>
            </IntentLink>
          </Button>
          <Button
            onClick={() => navigate("/register-node")}
            className="gap-2 shrink-0"
            size="sm"
            disabled={isReadOnly}
            title={isReadOnly ? "Disabled in read-only mode" : undefined}
          >
            <Plus className="h-4 w-4" />
            <span className="hidden sm:inline">Register Cluster</span>
            <span className="sm:hidden">Register</span>
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader className="space-y-4">
          <div className="flex items-center justify-between">
            <CardTitle>Cluster Overview</CardTitle>
            <RefreshButton
              label="Refresh clusters"
              lastUpdatedAt={collectedAtMs(data?.collected_at) || dataUpdatedAt}
              onRefresh={refreshNow}
            />
          </div>
          <TableToolbar filters={filters} noun="clusters" />
        </CardHeader>
        <CardContent>
          {clusters.length === 0 ? (
            <EmptyState
              icon={Server}
              illustration={EmptyClustersIllustration}
              title="No clusters registered"
              description='Use "Register Cluster" above to add your first hypervisor cluster.'
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  {/* node-link: exempt this is the Clusters page itself; the name opens the record's own detail and a self-link would land where the reader already is */}
                  <SortableTableHead label="Cluster" field="cluster" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Endpoint" field="endpoint" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Zone" field="zone" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <TableHead className="text-right"><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedClusters.length === 0 ? (
                  <TableEmptyRow
                    colSpan={6}
                    icon={Server}
                    title="No clusters found"
                    description={
                      filters.activeFilterCount > 0
                        ? `No cluster matches the current filters. ${plural(filters.activeFilterCount, "filter")} active.`
                        : undefined
                    }
                    action={
                      filters.activeFilterCount > 0 ? (
                        <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                          Clear filters
                        </Button>
                      ) : undefined
                    }
                  />
                ) : (
                  pagedClusters.map((cluster: any) => {
                  const statusMeta = clusterStatusMeta(cluster.status);
                  const StatusIcon = statusMeta.icon;
                  return (
                    <TableRow key={cluster.cluster_id}>
                      <TableCell className="font-medium">
                        <div className="flex items-center gap-2">
                          <Server className="h-4 w-4 text-muted-foreground" />
                          <div>
                            <div className="font-semibold">
                              <TableLink
                                onClick={() => setDetailCluster(cluster)}
                                aria-label={`Show details for ${cluster.display_name}`}
                              >
                                {cluster.display_name}
                              </TableLink>
                            </div>
                            <TableSubText>{cluster.cluster_id}</TableSubText>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <TableLink href={cluster.endpoint} external title="Open the hypervisor endpoint">
                          {cluster.endpoint}
                        </TableLink>
                      </TableCell>
                      <TableCell>
                        {/* A value, not a chip: Badge in a data cell is for Status and Severity
                            only, and its text-xs font-semibold would set the type smaller and
                            bolder than every other value in the row. As text, the type and its
                            qualifier underneath start at the same x. */}
                        <div>
                          {cluster.cluster_type ? (
                            formatHypervisorType(cluster.cluster_type)
                          ) : (
                            /* The backend does not guess this before the certificate is
                               trusted, and formatHypervisorType(null) answers the literal
                               string "Hypervisor", which would read as a real value. */
                            <TableEmptyValue label="Hypervisor type not determined" />
                          )}
                          {cluster.clustered && <TableSubText>Clustered</TableSubText>}
                        </div>
                      </TableCell>
                      <TableCell>
                        {cluster.zone || <TableEmptyValue label="No zone" />}
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusMeta.tone}>
                          <StatusIcon className="h-3 w-3 mr-1" />
                          {statusMeta.label}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          {/* From a cluster to the instances running on it. Named as well as
                              titled, unlike its neighbours, so a screen reader reaches it. */}
                          <Button
                            asChild
                            variant="outline"
                            size="sm"
                            aria-label={`View instances on ${cluster.cluster_id}`}
                            title="View Instances"
                          >
                            <IntentLink
                              href={inventoryLinks.instances({ host: cluster.cluster_id })}
                            >
                              <Box className="h-4 w-4" aria-hidden="true" />
                            </IntentLink>
                          </Button>
                          {cluster.clustered && cluster.status === "online" && (
                            <Button asChild variant="outline" size="sm" title="View Cluster Members">
                              <IntentLink href={inventoryLinks.members(cluster.cluster_id)}>
                                <Users className="h-4 w-4" />
                              </IntentLink>
                            </Button>
                          )}
                          {cluster.status === ClusterStatus.NOT_AUTHORIZED && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => setRegenerating(cluster)}
                              title="Regenerate Certificate"
                              className="text-status-pending"
                            >
                              <KeyRound className="h-4 w-4" />
                            </Button>
                          )}
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setLocationModal({ open: true, cluster })}
                            title={cluster.location ? `Location: ${placeShort(cluster.location)}` : "Set Location"}
                            className={cluster.location ? "text-status-online" : "text-muted-foreground"}
                          >
                            <MapPin className="h-4 w-4" />
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleCheckAuth(cluster.cluster_id)}
                            disabled={checkingAuth === cluster.cluster_id}
                            title={
                              cluster.status === ClusterStatus.NOT_AUTHORIZED
                                ? "Check Authentication after adding trust on the cluster"
                                : "Check Authentication"
                            }
                          >
                            {checkingAuth === cluster.cluster_id ? (
                              <RefreshCw className="h-4 w-4 animate-spin" />
                            ) : (
                              <RefreshCw className="h-4 w-4" />
                            )}
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleDeleteClick(cluster)}
                            title={isReadOnly ? "Disabled in read-only mode" : "Remove Cluster"}
                            disabled={isReadOnly}
                          >
                            <Trash2 className="h-4 w-4 text-destructive" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
              </TableBody>
            </Table>
          )}

          {/* Absent rather than disabled when everything fits on one page. */}
          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                pageSizeOptions={[...TABLE_PAGE_SIZES]}
                totalItems={sortedClusters.length}
                onPageChange={setPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setPage(1);
                }}
              />
            </div>
          )}
        </CardContent>
      </Card>

      <ClusterDetailDialog
        cluster={detailCluster}
        open={!!detailCluster}
        onOpenChange={(next) => {
          if (!next) setDetailCluster(null);
        }}
      />

      <Dialog 
        open={deleteConfirmModal.open} 
        onOpenChange={(open) => {
          if (!open) {
            setDeleteConfirmModal({ open: false, cluster: null });
            setDeleteConfirmText("");
          }
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-destructive">
              Remove Cluster Registration
            </DialogTitle>
            <DialogDescription>
              This will remove the cluster 
              <strong className="text-foreground"> {deleteConfirmModal.cluster?.display_name || deleteConfirmModal.cluster?.cluster_id}</strong> from 
              this dashboard. Only the connection settings, certificates, and metadata stored in the dashboard will be deleted,
              and the dashboard removes its own trust entries from the cluster when it can reach it.
              <span className="block mt-2 text-muted-foreground text-xs">
                Note: Your actual instances, VMs, containers, and data on the cluster will NOT be affected.
              </span>
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-2">
            <Alert variant="destructive">
              <AlertDescription>
                To confirm, type <strong>DELETE {deleteConfirmModal.cluster?.cluster_id?.toUpperCase()}</strong> below.
              </AlertDescription>
            </Alert>
            
            <div className="space-y-2">
              <Label htmlFor="confirmDelete">Confirmation</Label>
              <Input
                id="confirmDelete"
                placeholder={`Type DELETE ${deleteConfirmModal.cluster?.cluster_id?.toUpperCase() || ''} to confirm`}
                value={deleteConfirmText}
                onChange={(e) => setDeleteConfirmText(e.target.value)}
                className="font-mono"
              />
            </div>
          </div>

          <div className="flex justify-end gap-3 mt-4">
            <Button
              variant="outline"
              disabled={deleteCluster.isPending}
              onClick={() => {
                setDeleteConfirmModal({ open: false, cluster: null });
                setDeleteConfirmText("");
              }}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteConfirm}
              disabled={
                deleteCluster.isPending ||
                deleteConfirmText !== `DELETE ${deleteConfirmModal.cluster?.cluster_id?.toUpperCase()}`
              }
            >
              {deleteCluster.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Removing…
                </>
              ) : (
                "Remove Cluster"
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <LeftoverTrustDialog leftover={leftoverTrust} onClose={() => setLeftoverTrust(null)} />

      <ClusterLocationDialog
        open={locationModal.open}
        onOpenChange={(open) => setLocationModal({ open, cluster: locationModal.cluster })}
        cluster={locationModal.cluster}
        onSuccess={() => refetch()}
      />
    </div>
  );
}

// The role gate stays in this file: check-role-gates.mjs reads page text and does not follow
// imports. Suspense is required because useTableFilters reads useSearchParams.
export default function ClustersPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <React.Suspense fallback={<LoadingLottiePage />}>
      <ClustersContent />
    </React.Suspense>
  );
}
