"use client";

import { Suspense, useCallback, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyValue } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { OverflowText } from "@/components/ui/overflow-text";
import { OperationDetailDialog } from "./components/operation-detail-dialog";
import { operationStatusTone } from "./components/operation-status";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle } from "@/components/ui/alert-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger } from "@/components/ui/tooltip";
import { Loader2, XCircle, Unplug } from "lucide-react";
import { useOperations } from "@/hooks/use-lxd";
import { useLiveRecord } from "@/hooks/use-live-record";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { toast } from "@/hooks/use-toast";
import { API_URL, getAccessToken } from "@/lib/api";
import type { OperationInfo } from "@/types/api";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { formatDateTime, formatLabel} from "@/lib/utils";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { PageReadError } from "@/components/ui/read-error-state";
import { NodeLink } from "@/components/ui/node-link";

// The instance an operation belongs to: its own field, else the name inside a websocket
// console operation's resources. Hoisted so sorting, searching and the row cannot disagree.
function operationInstance(o: any): string {
  if (o.instance_name) return o.instance_name;
  if (o.class_name === "websocket" && o.description === "Executing command" && !o.metadata?.command) {
    const instances = o.metadata?.resources?.instances || [];
    if (instances.length > 0) {
      const match = String(instances[0]).match(/\/instances\/([^?]+)/);
      if (match) return match[1];
    }
  }
  return "";
}

const OPERATIONS = defineTable<any>()({
  searchPlaceholder: "Search operations",
  defaultSort: { field: "created", direction: "desc" },
  columns: [
    { field: "id", label: "Operation ID", sort: (o) => o.id, search: (o) => o.id, filter: fromSort("text") },
    {
      field: "instance",
      label: "Instance",
      sort: operationInstance,
      search: operationInstance, filter: fromSort("text") },
    {
      field: "description",
      label: "Description",
      sort: (o) => o.description,
      search: (o) => o.description, filter: fromSort("text") },
    {
      field: "node",
      label: "Node",
      sort: (o) => o.node_name,
      facet: (o) => o.node_name,
      facetSpec: { role: "node", primary: true } },
    {
      field: "project",
      label: "Project",
      sort: (o) => o.project || "default",
      facet: (o) => o.project || "default",
      facetSpec: { role: "project",} },
    {
      field: "status",
      label: "Status",
      sort: (o) => o.status,
      facet: (o) => o.status,
      facetSpec: { primary: true } },
    {
      field: "class",
      label: "Class",
      sort: (o) => o.class_name,
      facet: (o) => o.class_name,
      facetSpec: { primary: true, format: formatLabel } },
    {
      field: "created",
      label: "Created",
      sort: (o) => (o.created_at ? new Date(o.created_at).getTime() : null), filter: fromSort("date", { horizon: "past" }) },
  ] });

function OperationsContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt, isError, error } = useOperations();
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const operations: OperationRow[] = useMemo(
    () => ((data as any)?.operations as OperationRow[]) || [],
    [data],
  );
  const nodes = (data as any)?.nodes || [];
  
  // note: the id, not the row. Operations are the clearest case for re-deriving: the list
  // polls every 15s and an operation's status changes underneath an open dialog, then the
  // operation completes and leaves the list entirely.
  const [detailOperation, setDetailOperation] = useState<{ id: string; instanceName?: string } | null>(null);
  // important: keeps the last known row when the operation finishes and leaves the list.
  // Snapping the dialog shut mid-read is a worse failure than the freeze it replaces.
  const { record: liveOperation } = useLiveRecord(
    operations,
    detailOperation?.id ?? null,
    operationId,
  );
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [killingId, setKillingId] = useState<string | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    operationId: string;
    host: string;
    description: string;
    isKillSession?: boolean;
    instanceName?: string;
  }>({ open: false, operationId: "", host: "", description: "" });


  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  const filters = useTableFilters(OPERATIONS, operations as any[], {
    onFilterChange: resetPage,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    OPERATIONS.sortAccessors,
    OPERATIONS.defaultSort.field,
    OPERATIONS.defaultSort.direction,
    resetPage,
  );
  const sortedOperations = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // Clamped on read rather than corrected in an effect. This list polls every 15s and
  // operations leave it as they finish, so the page under the operator shrinks on its own;
  // an effect would fire after a wrong frame had already painted.
  const totalPages = Math.max(1, Math.ceil(sortedOperations.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedOperations = sortedOperations.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // Find the host for an operation (check metadata, node_name, or iterate nodes)
  const findOperationHost = (operation: any): string => {
    // Check if operation has node_name from backend
    if (operation.node_name) {
      return operation.node_name;
    }
    // Check if operation has host in metadata
    if (operation.metadata?.host) {
      return operation.metadata.host;
    }
    // Default to first node if we can't determine
    return nodes[0] || "";
  };

  // Extract instance name from console operation description
  const extractInstanceName = (operation: any): string | null => {
    // Console operations have description like "Executing command" and metadata with fds
    if (operation.class_name === "websocket" && operation.description === "Executing command") {
      // Try to get from metadata
      if (operation.metadata?.command) {
        return null; // exec command, not console
      }
      // Check resources for instance name
      const resources = operation.metadata?.resources || {};
      const instances = resources.instances || [];
      if (instances.length > 0) {
        // Path format: /1.0/instances/name?project=xxx
        const path = instances[0];
        const match = path.match(/\/instances\/([^?]+)/);
        if (match) return match[1];
      }
    }
    return null;
  };

  const handleCancelOperation = async (operationId: string, host: string) => {
    setCancellingId(operationId);
    try {
      const response = await fetch(
        `${API_URL}/operations/${operationId}?host=${encodeURIComponent(host)}`,
        {
          method: "DELETE",
          headers: {
            ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
      );
      
      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: "Failed to cancel" }));
        throw new Error(error.detail || "Failed to cancel operation");
      }
      
      toast({
        title: "Operation cancelled",
        description: `Operation ${operationId} has been cancelled` });
      
      // Refresh operations list
      refetch();
    } catch (error) {
      toast({
        title: "Failed to cancel operation",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      setCancellingId(null);
      setConfirmDialog({ open: false, operationId: "", host: "", description: "" });
    }
  };

  const handleKillSession = async (operationId: string, instanceName: string, host: string) => {
    setKillingId(operationId);
    try {
      const response = await fetch(
        `${API_URL}/console/${instanceName}/active-sessions/${operationId}?host=${encodeURIComponent(host)}`,
        {
          method: "DELETE",
          headers: {
            ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
      );
      
      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: "Failed to kill session" }));
        throw new Error(error.detail || "Failed to kill session");
      }
      
      toast({
        title: "Session killed",
        description: `Console session for ${instanceName} has been terminated` });
      
      // Refresh operations list
      refetch();
    } catch (error) {
      toast({
        title: "Failed to kill session",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      setKillingId(null);
      setConfirmDialog({ open: false, operationId: "", host: "", description: "" });
    }
  };

  const openCancelConfirm = (operation: any, isKillSession = false, instanceName?: string) => {
    const host = findOperationHost(operation);
    setConfirmDialog({
      open: true,
      operationId: operation.id,
      host,
      description: operation.description || `Operation ${operation.id}`,
      isKillSession,
      instanceName });
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="Operations"
        description="Monitor hypervisor operations and background tasks"
        noun="operations"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Operations
          </h1>
          <p className="text-muted-foreground mt-2">
            Monitor hypervisor operations and background tasks
          </p>
        </div>
        <RefreshButton
          label="Refresh operations"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      {/* Cancel/Kill Confirmation Dialog */}
      <AlertDialog open={confirmDialog.open} onOpenChange={(open) => setConfirmDialog((prev) => ({ ...prev, open }))}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmDialog.isKillSession ? "Kill Console Session?" : "Cancel Operation?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmDialog.isKillSession ? (
                <>
                  Are you sure you want to kill this console session?
                  <br />
                  <span className="font-medium text-foreground">Instance: {confirmDialog.instanceName}</span>
                </>
              ) : (
                <>
                  Are you sure you want to cancel this operation?
                  <br />
                  <span className="font-medium text-foreground">{confirmDialog.description}</span>
                </>
              )}
              <br /><br />
              This action cannot be undone. The {confirmDialog.isKillSession ? "session" : "operation"} will be terminated immediately.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={cancellingId !== null && killingId !== null}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (confirmDialog.isKillSession && confirmDialog.instanceName) {
                  handleKillSession(confirmDialog.operationId, confirmDialog.instanceName, confirmDialog.host);
                } else {
                  handleCancelOperation(confirmDialog.operationId, confirmDialog.host);
                }
              }}
              disabled={cancellingId !== null || killingId !== null}
              className="bg-status-offline text-status-offline-foreground hover:bg-status-offline/90"
            >
              {(cancellingId === confirmDialog.operationId || killingId === confirmDialog.operationId) ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {confirmDialog.isKillSession ? "Killing..." : "Cancelling..."}
                </>
              ) : (
                confirmDialog.isKillSession ? "Kill Session" : "Cancel Operation"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <UnreachableNodesBanner noun="operations" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>Active Operations</CardTitle>
          <TableToolbar filters={filters} noun="operations" />
        </CardHeader>
        <CardContent>
          {sortedOperations.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              {filters.activeFilterCount > 0
                ? "No operation matches the current filters."
                : "No active operations"}
            </div>
          ) : (
            <TooltipProvider>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Operation ID" field="id" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Instance" field="instance" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Class" field="class" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Created" field="created" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pagedOperations.map((operation: any) => {
                    // Use instance_name from backend, or extract from metadata
                    const instanceName = operation.instance_name || extractInstanceName(operation) || "";
                    const isConsoleOp = operation.class_name === "websocket";
                    
                    return (
                    <TableRow key={operation.id}>
                      <TableCell className="max-w-[18rem] font-mono">
                        <OverflowText
                          value={operation.id}
                          label={`id for the ${operation.description || "operation"}`}
                          reveal="always"
                          mono
                        >
                          {(expanded) => (
                            <TableLink
                              wrap={expanded}
                              onClick={() => setDetailOperation({ id: operation.id, instanceName })}
                              aria-label={`Show details for operation ${operation.id}`}
                            >
                              {operation.id}
                            </TableLink>
                          )}
                        </OverflowText>
                      </TableCell>
                      <TableCell>
                        {instanceName ? (
                          <span className="font-medium">{instanceName}</span>
                        ) : (
                          <TableEmptyValue label="No instance" />
                        )}
                      </TableCell>
                      <TableCell>{operation.description || <TableEmptyValue label="No description" />}</TableCell>
                      <TableCell><NodeLink clusterId={operation.node_name} /></TableCell>
                      <TableCell>
                        <span className="text-sm">{operation.project || "default"}</span>
                      </TableCell>
                      <TableCell>
                        <Badge variant={operationStatusTone(operation.status)}>
                          {formatLabel(operation.status)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {formatLabel(operation.class_name)}
                      </TableCell>
                      <TableCell>
                        {formatDateTime(operation.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        {operation.may_cancel ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8 text-status-offline"
                                onClick={() => openCancelConfirm(operation)}
                                disabled={cancellingId !== null || killingId !== null}
                              >
                                {cancellingId === operation.id ? (
                                  <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                  <XCircle className="h-4 w-4" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Cancel Operation</TooltipContent>
                          </Tooltip>
                        ) : isConsoleOp ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8 text-status-pending"
                                onClick={() => openCancelConfirm(operation, true, instanceName || "N/A")}
                                disabled={cancellingId !== null || killingId !== null}
                              >
                                {killingId === operation.id ? (
                                  <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                  <Unplug className="h-4 w-4" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Kill Console Session</TooltipContent>
                          </Tooltip>
                        ) : (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="text-xs text-muted-foreground">
                                Cannot cancel
                              </span>
                            </TooltipTrigger>
                            <TooltipContent>This operation cannot be cancelled</TooltipContent>
                          </Tooltip>
                        )}
                      </TableCell>
                    </TableRow>
                  )})}
                </TableBody>
              </Table>

              {/* Absent rather than disabled when everything fits on one page. */}
              {totalPages > 1 && (
                <div className="mt-4">
                  <DataTablePagination
                    currentPage={currentPage}
                    totalPages={totalPages}
                    pageSize={pageSize}
                    pageSizeOptions={[...TABLE_PAGE_SIZES]}
                    totalItems={sortedOperations.length}
                    onPageChange={setPage}
                    onPageSizeChange={(size) => {
                      setPageSize(size);
                      setPage(1);
                    }}
                  />
                </div>
              )}
            </TooltipProvider>
          )}
        </CardContent>
      </Card>

      <OperationDetailDialog
        operation={liveOperation}
        instanceName={detailOperation?.instanceName}
        open={!!detailOperation}
        onOpenChange={(next) => {
          if (!next) setDetailOperation(null);
        }}
      />
    </div>
  );
}

// The role gate stays in this file: check-role-gates.mjs reads page text and does not follow
// imports. Suspense is required because useTableFilters reads useSearchParams.
/** Row shape the operations table and its detail dialog share. */
type OperationRow = OperationInfo & { node_name?: string; project?: string };

const operationId = (operation: OperationRow) => operation.id;

export default function OperationsPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <OperationsContent />
    </Suspense>
  );
}
