"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyValue } from "@/components/ui/table-text";
import { OverflowText } from "@/components/ui/overflow-text";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { formatLabel } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Trash2, MoreVertical } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useIdentities, useDeleteIdentity } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";

const IDENTITIES = defineTable<any>()({
  searchPlaceholder: "Search identities",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "id", label: "ID", sort: (i) => i.id, search: (i) => i.id, filter: fromSort("text") },
    { field: "name", label: "Name", sort: (i) => i.name, search: (i) => i.name, filter: fromSort("text") },
    {
      field: "type",
      label: "Type",
      sort: (i) => i.type,
      facet: (i) => i.type,
      facetSpec: { primary: true, format: formatLabel } },
    {
      field: "node",
      label: "Node",
      sort: (i) => i.node,
      search: (i) => i.node,
      facet: (i) => i.node,
      facetSpec: { role: "node", primary: true } },
    {
      field: "groups",
      label: "Groups",
      sort: (i) => i.groups?.length ?? 0,
      search: (i) => (i.groups ?? []).join(" "),
      // Multi-value: an identity matches when any of its groups is selected. Behind
      // "Add filter" because group cardinality grows with the fleet.
      facet: (i) => i.groups ?? [],
      facetSpec: { emptyLabel: "(no group)" } },
  ] });

function IdentitiesContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useIdentities();
  const identities = (data as any)?.identities || [];
  const { toast } = useToast();
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const reach = reachabilityOf(data);
  const deleteIdentityMutation = useDeleteIdentity();
  const { isReadOnly } = useReadOnlyMode();
  
  // Pagination and search state
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  
  // Delete confirmation state
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [identityToDelete, setIdentityToDelete] = useState<any>(null);

  // Get unique nodes and types for filters
  // Filter and search identities
  const resetPage = () => setCurrentPage(1);
  const filters = useTableFilters(IDENTITIES, identities as any[], { onFilterChange: resetPage });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    IDENTITIES.sortAccessors,
    IDENTITIES.defaultSort.field,
    IDENTITIES.defaultSort.direction,
    resetPage,
  );
  const sortedIdentities = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // Pagination
  const totalPages = Math.ceil(sortedIdentities.length / pageSize);
  const paginatedIdentities = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return sortedIdentities.slice(startIndex, startIndex + pageSize);
  }, [sortedIdentities, currentPage, pageSize]);

  const handleDeleteClick = (identity: any) => {
    setIdentityToDelete(identity);
    setDeleteDialogOpen(true);
  };

  const handleDeleteConfirm = async () => {
    if (!identityToDelete) return;
    
    try {
      await deleteIdentityMutation.mutateAsync({
        authType: identityToDelete.auth_type || "tls",
        identityId: identityToDelete.id,
        host: identityToDelete.node });
      toast({
        title: "Success",
        description: `Identity deleted successfully` });
      refetch();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to delete identity",
        variant: "destructive" });
    } finally {
      setDeleteDialogOpen(false);
      setIdentityToDelete(null);
    }
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <ReadOnlyBanner />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Identities
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor user identities
          </p>
        </div>
        <RefreshButton
          label="Refresh identities"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="identities" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="identities" nodes={reach.partial} reasons={reach.reasons} partial />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Identities</CardTitle>
          <TableToolbar filters={filters} noun="identities" />
        </CardHeader>
        <CardContent>
          {identities.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No identities found
            </div>
          ) : sortedIdentities.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No identities match your search criteria
            </div>
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="ID" field="id" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Groups" field="groups" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginatedIdentities.map((identity: any, index: number) => (
                    <TableRow key={identity.node + "-" + identity.id + "-" + index}>
                      {/* important: this page has no detail dialog, so the cell is the only place
                          the id exists. It used to elide the middle and hang the whole value on a
                          title, which is a mouse-only reveal. */}
                      <TableCell className="max-w-[16rem] font-mono">
                        <OverflowText value={identity.id} label="identity id" reveal="always" mono />
                      </TableCell>
                      <TableCell className="font-medium">
                        {identity.name || <TableEmptyValue label="No name" />}
                      </TableCell>
                      <TableCell>
                        {formatLabel(identity.type)}
                      </TableCell>
                      <TableCell>
                        {identity.node || <TableEmptyValue label="No node" />}
                      </TableCell>
                      <TableCell>
                        <OverflowCell
                          items={identity.groups}
                          preview={3}
                          noun="groups"
                          ownerLabel={identity.name}
                          emptyLabel="No groups"
                          itemLabel="group"
                        />
                      </TableCell>
                      <TableCell className="text-right">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="sm" aria-label={`Actions for identity ${identity.id}`}>
                              <MoreVertical className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem 
                              onClick={() => handleDeleteClick(identity)}
                              className="text-destructive focus:text-destructive"
                              disabled={isReadOnly}
                            >
                              <Trash2 className="h-4 w-4 mr-2" />
                              Delete Identity
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages || 1}
                totalItems={sortedIdentities.length}
                pageSize={pageSize}
                onPageChange={setCurrentPage}
                onPageSizeChange={setPageSize}
              />
            </>
          )}
        </CardContent>
      </Card>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Identity</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this identity? 
              This action cannot be undone and will remove this identity from the hypervisor.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setIdentityToDelete(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction 
              onClick={handleDeleteConfirm}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// important: the five sibling partner pages gate here and these two did not, so a tenant
// deep-linking straight to the route got the page shell and a wall of 403s instead of a
// redirect. The backend is the real gate (require_partner on both routers); this keeps the
// UI honest and stops the request being made at all, because the content component's
// queries only mount once the role resolves.
export default function IdentitiesPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  // Suspense is required because useTableFilters reads useSearchParams, which opts the
  // component out of prerendering. The role gate stays in this file: check-role-gates.mjs
  // reads page text and does not follow imports.
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <IdentitiesContent />
    </Suspense>
  );
}
