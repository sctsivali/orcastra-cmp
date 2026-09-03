"use client";

import * as React from "react";
import { IntentLink } from "@/components/navigation/route-intent";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { formatHypervisorType } from "@/lib/utils";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { 
  Server, 
  CheckCircle, 
  XCircle, 
  Clock, 
  RefreshCw,
  Trash2,
  Plus,
  Users,
  KeyRound,
  Terminal,
  Shield,
  MapPin,
  Loader2
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/ui/code-block";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { ClusterDetailDialog } from "./components/cluster-detail-dialog";
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
import { ClusterInfo, ClusterStatus } from "@/types/api";
import { useClusters, useCheckClusterAuth, useDeleteCluster, useRegenerateCert } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { ClusterLocationDialog } from "@/components/cluster-location-dialog";
import { useRequireRole } from "@/hooks/use-require-role";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { Role } from "@/types/rbac";
import { clusterStatusMeta } from "@/lib/cluster-status";


const CLUSTERS = defineTable<any>()({
  searchPlaceholder: "Search clusters",
  defaultSort: { field: "cluster", direction: "asc" },
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (c) => c.display_name,
      search: (c) => c.display_name, filter: fromSort("text") },
    { field: "endpoint", label: "Endpoint", sort: (c) => c.endpoint, search: (c) => c.endpoint, filter: fromSort("text") },
    {
      field: "type",
      label: "Type",
      sort: (c) => c.cluster_type,
      facet: (c) => c.cluster_type,
      facetSpec: {
        primary: true,
        format: formatHypervisorType,
        // A null type is the empty bucket, which never reaches `format`. Naming it keeps the
        // chip honest instead of reading "(none)" for a fact that is simply not known yet.
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
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useClusters();
  const clusters = (data as any)?.clusters || [];
  const [detailCluster, setDetailCluster] = React.useState<ClusterInfo | null>(null);
  const [checkingAuth, setCheckingAuth] = React.useState<string | null>(null);
  const [regenerateModal, setRegenerateModal] = React.useState<{open: boolean, cluster: any | null}>({open: false, cluster: null});
  const [regeneratedCert, setRegeneratedCert] = React.useState<{identityName: string, certificate: string, cluster_type?: string} | null>(null);
  const [deleteConfirmModal, setDeleteConfirmModal] = React.useState<{open: boolean, cluster: any | null}>({open: false, cluster: null});
  const [deleteConfirmText, setDeleteConfirmText] = React.useState("");
  const [locationModal, setLocationModal] = React.useState<{open: boolean, cluster: any | null}>({open: false, cluster: null});
  const { isReadOnly } = useReadOnlyMode();
  const checkClusterAuth = useCheckClusterAuth();
  const deleteCluster = useDeleteCluster();
  const regenerateCert = useRegenerateCert();

  const filters = useTableFilters(CLUSTERS, clusters as any[]);
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    CLUSTERS.sortAccessors,
    CLUSTERS.defaultSort.field,
    CLUSTERS.defaultSort.direction,
  );

  const visibleClusters = sortItems(filters.filteredRows);

  const handleCheckAuth = async (clusterId: string) => {
    setCheckingAuth(clusterId);
    try {
      const response = await checkClusterAuth.mutateAsync(clusterId);
      
      if (response.authenticated) {
        toast({
          title: "Authentication Successful",
          description: `Cluster ${clusterId} is online. Refreshing instance data...` });
        // refetch is handled by the mutation's onSuccess (includes delay for LXD stabilization)
      } else {
        toast({
          title: "Not Authenticated",
          description: response.message || "Certificate not yet trusted",
          variant: "destructive" });
      }
    } catch (error: any) {
      toast({
        title: "Check Failed",
        description: error.message || "Failed to check authentication",
        variant: "destructive" });
    } finally {
      setCheckingAuth(null);
    }
  };

  const generateIdentityName = (clusterId: string): string => {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    const millis = String(now.getMilliseconds()).padStart(3, '0');
    const randomSuffix = Math.random().toString(36).substring(2, 6);
    const datetime = `${year}${month}${day}-${hours}${minutes}${seconds}${millis}-${randomSuffix}`;
    return `orca-${clusterId}-${datetime}`;
  };

  const handleRegenerateCert = async (cluster: any) => {
    try {
      const response = await regenerateCert.mutateAsync(cluster.cluster_id);
      
      if (response.success && response.certificate) {
        const identityName = generateIdentityName(cluster.cluster_id);
        setRegeneratedCert({
          identityName,
          certificate: response.certificate,
          cluster_type: cluster.cluster_type || response.cluster_type  // Use existing or newly detected type
        });
        setRegenerateModal({open: true, cluster});
        toast({
          title: "Certificate Regenerated",
          description: "Copy and run the command on your hypervisor server to add trust" });
      }
    } catch (error: any) {
      toast({
        title: "Regenerate Failed",
        description: error.message || "Failed to regenerate certificate",
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
      await deleteCluster.mutateAsync(clusterId);
      toast({
        title: "Cluster Removed",
        description: `Cluster ${clusterId} has been removed` });
      setDeleteConfirmModal({ open: false, cluster: null });
      setDeleteConfirmText("");
      // useDeleteCluster.onSuccess invalidates ["clusters"] with refetchType:'all',
      // which refetches this page's active query - no full-page reload needed.
    } catch (error: any) {
      toast({
        title: "Delete Failed",
        description: error.message || "Failed to remove cluster",
        variant: "destructive" });
    }
  };

  // Full-page loading state
  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="container py-8">
      <ReadOnlyBanner />
      {/* Regenerate Certificate Modal */}
      <Dialog open={regenerateModal.open} onOpenChange={(open) => {
        if (!open) {
          setRegenerateModal({open: false, cluster: null});
          setRegeneratedCert(null);
        }
      }}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>
              Certificate Regenerated
            </DialogTitle>
            <DialogDescription>
              Add trust to complete the authentication for {regenerateModal.cluster?.display_name}
            </DialogDescription>
          </DialogHeader>
          
          {regeneratedCert && (
            <div className="space-y-4">
              <Alert>
                <Terminal className="h-4 w-4" />
                <AlertDescription className="space-y-3">
                  <p className="text-sm font-medium">
                    Run this command on your hypervisor server:
                  </p>
                  
                  {/* Dynamic CLI Command based on detected type */}
                  {(() => {
                    const isIncus = regeneratedCert.cluster_type === "Incus";
                    const cliTool = isIncus ? "incus" : "lxc";
                    const trustSubCmd = isIncus ? "add-certificate" : "add";
                    const typeLabel = formatHypervisorType(regeneratedCert.cluster_type);
                    const badgeVariant = isIncus ? "secondary" : "outline";
                    
                    return (
                      <CodeBlock
                        value={`cat <<EOF | ${cliTool} config trust ${trustSubCmd} - --name "${regeneratedCert.identityName}"\n${regeneratedCert.certificate}\nEOF`}
                        label={
                          <>
                            <Badge variant={badgeVariant} className="mr-2">{typeLabel}</Badge>
                            Identity: <span className="font-mono">{regeneratedCert.identityName}</span>
                          </>
                        }
                        copyLabel="CLI command"
                      />
                    );
                  })()}
                </AlertDescription>
              </Alert>
              
              <div className="flex gap-3">
                <Button
                  onClick={async () => {
                    if (regenerateModal.cluster) {
                      setCheckingAuth(regenerateModal.cluster.cluster_id);
                      try {
                        const response = await checkClusterAuth.mutateAsync(regenerateModal.cluster.cluster_id);
                        
                        if (response.authenticated) {
                          toast({
                            title: "Authentication Successful",
                            description: `Cluster ${regenerateModal.cluster.cluster_id} is now online` });
                          // Only close modal if authentication successful
                          setRegenerateModal({open: false, cluster: null});
                          setRegeneratedCert(null);
                          await refetch();
                        } else {
                          toast({
                            title: "Not Authenticated Yet",
                            description: response.message || "Certificate not yet trusted. Please run the command on your hypervisor server.",
                            variant: "destructive" });
                          // Keep modal open for retry
                        }
                      } catch (error: any) {
                        toast({
                          title: "Check Failed",
                          description: error.message || "Failed to check authentication",
                          variant: "destructive" });
                        // Keep modal open for retry
                      } finally {
                        setCheckingAuth(null);
                      }
                    }
                  }}
                  disabled={checkingAuth === regenerateModal.cluster?.cluster_id}
                  className="flex-1"
                >
                  {checkingAuth === regenerateModal.cluster?.cluster_id ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      Checking...
                    </>
                  ) : (
                    "Verify Connection"
                  )}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    setRegenerateModal({open: false, cluster: null});
                    setRegeneratedCert(null);
                  }}
                >
                  Close
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <div className="flex items-center justify-between mb-6">
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
            <span className="hidden sm:inline">Register Node</span>
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
              lastUpdatedAt={dataUpdatedAt}
              onRefresh={refreshNow}
            />
          </div>
          <TableToolbar filters={filters} noun="clusters" />
        </CardHeader>
        <CardContent>
          {clusters.length === 0 ? (
            <div className="text-center py-12">
              <Server className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">No clusters registered</h3>
              <p className="text-muted-foreground mb-4">
                Get started by registering your first hypervisor cluster
              </p>
              <Button asChild className="gap-2">
                <IntentLink href="/register-node">
                  <Plus className="h-4 w-4" />
                  Register Node
                </IntentLink>
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Cluster" field="cluster" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Endpoint" field="endpoint" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Zone" field="zone" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleClusters.length === 0 ? (
                  <TableEmptyRow
                    colSpan={6}
                    icon={Server}
                    title="No clusters found"
                    description={
                      filters.activeFilterCount > 0
                        ? "No cluster matches the current filters."
                        : undefined
                    }
                  />
                ) : (
                  visibleClusters.map((cluster: any) => {
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
                        {/* important: a value, not a chip. documentation/ui/table-conventions.md
                            keeps Badge in a data cell for Status and Severity only, and the pill
                            also inverted the type scale here: it is text-xs font-semibold, so the
                            Type value rendered smaller and bolder than every other value in the
                            row while its own qualifier underneath was the same size. As text both
                            tiers start at the same x, which is what the badge padding broke.
                            The monitoring and technologies node cards already render this value
                            as text. */}
                        <div>
                          {cluster.cluster_type ? (
                            formatHypervisorType(cluster.cluster_type)
                          ) : (
                            /* The backend refuses to guess this before the certificate is
                               trusted, so neither do we. formatHypervisorType(null) answers the
                               literal string "Hypervisor", which reads as a reading. */
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
                          {cluster.clustered && cluster.status === "online" && (
                            <Button asChild variant="outline" size="sm" title="View Cluster Members">
                              <IntentLink href={`/inventory/clustering/members?node=${cluster.cluster_id}`}>
                                <Users className="h-4 w-4" />
                              </IntentLink>
                            </Button>
                          )}
                          {cluster.status === ClusterStatus.NOT_AUTHORIZED && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleRegenerateCert(cluster)}
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
                            title={cluster.location ? `Location: ${cluster.location.city}` : "Set Location"}
                            className={cluster.location ? "text-status-online" : "text-muted-foreground"}
                          >
                            <MapPin className="h-4 w-4" />
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => handleCheckAuth(cluster.cluster_id)}
                            disabled={checkingAuth === cluster.cluster_id}
                            title="Check Authentication"
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
                            title={isReadOnly ? "Disabled in read-only mode" : "Delete Cluster"}
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
        </CardContent>
      </Card>

      <ClusterDetailDialog
        cluster={detailCluster}
        open={!!detailCluster}
        onOpenChange={(next) => {
          if (!next) setDetailCluster(null);
        }}
      />

      {/* Delete Confirmation Modal */}
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
              this dashboard. Only the connection settings, certificates, and metadata stored in the dashboard will be deleted.
              <span className="block mt-2 text-muted-foreground text-xs">
                Note: Your actual instances, VMs, containers, and data on the hypervisor server will NOT be affected.
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
                  Deleting...
                </>
              ) : (
                "Remove Cluster"
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Location Edit Modal */}
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
