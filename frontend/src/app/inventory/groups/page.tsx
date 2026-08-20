"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle } from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyRow } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { getEntitlementLabel } from "@/lib/lxd-entitlements";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TableEmptyValue } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { 
  Users, 
  UserCheck, 
  Shield, 
  Plus, 
  Edit, 
  Trash2, 
  MoreVertical,
  
  Loader2 } from "lucide-react";
import { IdentityInfo, IdentityListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { useGroups, useDeleteGroup, useIdentities } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { CreateGroupDialog } from "@/components/groups/create-group-dialog";
import { EditGroupDialog } from "@/components/groups/edit-group-dialog";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

// filter-coverage: no project dimension here, and not for lack of a mechanism. A group's
// permissions[].url values do carry ?project= when LXD scopes them, and projectsFromUsedBy
// already parses exactly that shape. Sampling the live fleet found one distinct permission URL
// across every group, `/1.0`, with no project qualifier at all, so the facet would derive
// "default" for every row: a single-option control that the pinned-role rule would then render
// unconditionally. That turns an invisible gap into a visible dead control. Revisit if
// per-project group permissions ever appear in the wild.
const GROUPS = defineTable<any>()({
  searchPlaceholder: "Search groups",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (g) => g.name, search: (g) => g.name, filter: fromSort("text") },
    {
      field: "description",
      label: "Description",
      sort: (g) => g.description,
      search: (g) => g.description, filter: fromSort("text") },
    {
      field: "node",
      label: "Node",
      sort: (g) => g.node,
      search: (g) => g.node,
      facet: (g) => g.node,
      facetSpec: { role: "node", primary: true } },
    // Identities and Permissions stay count columns with no facet: "which group has the
    // most" and "which has none" are already answered by sorting.
    {
      field: "identities",
      label: "Identities",
      sort: (g) => (g.identities?.tls?.length || 0) + (g.identities?.oidc?.length || 0), filter: fromSort("number", { unit: "count" }) },
    { field: "permissions", label: "Permissions", sort: (g) => g.permissions?.length || 0, filter: fromSort("number", { unit: "count", presets: [{ label: "None", min: null, max: 0 }, { label: "Any", min: 1, max: null }] }) },
  ] });

// The permissions table lives inside a dialog, so its filter state stays local rather than
// being written into the address bar of the page behind it.
const GROUP_PERMISSIONS = defineTable<any>()({
  id: "perms",
  searchPlaceholder: "Search permissions",
  defaultSort: { field: "entity_type", direction: "asc" },
  columns: [
    {
      field: "entity_type",
      label: "Entity Type",
      sort: (p) => p.entity_type,
      facet: (p) => p.entity_type,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "url", label: "Resource URL", sort: (p) => p.url, search: (p) => p.url, filter: fromSort("text") },
    {
      field: "entitlement",
      label: "Entitlement",
      sort: (p) => p.entitlement,
      facet: (p) => p.entitlement,
      facetSpec: { primary: true, format: getEntitlementLabel } },
  ] });

function GroupsContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useGroups();
  const groups = (data as any)?.groups || [];
  const nodes = (data as any)?.nodes || [];
  const { toast } = useToast();
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const deleteGroupMutation = useDeleteGroup();
  const { isReadOnly } = useReadOnlyMode();
  
  // Pagination and search state
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  
  // State for modals
  const [identitiesModalOpen, setIdentitiesModalOpen] = useState(false);
  const [permissionsModalOpen, setPermissionsModalOpen] = useState(false);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<any>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [groupToDelete, setGroupToDelete] = useState<any>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  // Get unique nodes for filter
  // Fetch identities for the selected group's node (for view members modal)
  const { data: identitiesData } = useIdentities(selectedGroup?.node || "");
  const allIdentities: IdentityInfo[] = (identitiesData as IdentityListResponse)?.identities || [];
  
  // Helper to find identity name from ID
  const getIdentityName = (identityId: string): string | null => {
    const identity = allIdentities.find(i => i.id === identityId);
    return identity?.name || null;
  };

  const resetPage = () => setCurrentPage(1);
  const filters = useTableFilters(GROUPS, groups as any[], { onFilterChange: resetPage });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    GROUPS.sortAccessors,
    GROUPS.defaultSort.field,
    GROUPS.defaultSort.direction,
    resetPage,
  );

  // Sort across the full filtered set before paginating
  const sortedGroups = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // Pagination
  const totalPages = Math.ceil(sortedGroups.length / pageSize);
  const paginatedGroups = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return sortedGroups.slice(startIndex, startIndex + pageSize);
  }, [sortedGroups, currentPage, pageSize]);

  // Permissions modal: its own filter and sort instance, state kept out of the URL.
  const permFilters = useTableFilters(
    GROUP_PERMISSIONS,
    (selectedGroup?.permissions ?? []) as any[],
    { syncUrl: false },
  );
  const {
    sortField: permSortField,
    sortDirection: permSortDirection,
    handleSort: permHandleSort,
    sortItems: permSortItems } = useTableSort(
    GROUP_PERMISSIONS.sortAccessors,
    GROUP_PERMISSIONS.defaultSort.field,
    GROUP_PERMISSIONS.defaultSort.direction,
  );

  const handleShowIdentities = (group: any) => {
    setSelectedGroup(group);
    setIdentitiesModalOpen(true);
  };

  const handleShowPermissions = (group: any) => {
    setSelectedGroup(group);
    setPermissionsModalOpen(true);
  };

  const handleEdit = (group: any) => {
    setSelectedGroup(group);
    setEditDialogOpen(true);
  };

  const handleDelete = async (group: any) => {
    setGroupToDelete(group);
    setDeleteDialogOpen(true);
  };

  const confirmDelete = async () => {
    if (!groupToDelete) return;
    setIsDeleting(true);

    try {
      await deleteGroupMutation.mutateAsync({
        name: groupToDelete.name,
        host: groupToDelete.node });

      toast({
        title: "Success",
        description: `Group "${groupToDelete.name}" deleted successfully` });

      refetch();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to delete group",
        variant: "destructive" });
    } finally {
      setIsDeleting(false);
      setDeleteDialogOpen(false);
      setGroupToDelete(null);
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
            Groups
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor user groups with TLS/OIDC identities and permissions
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <RefreshButton
            label="Refresh groups"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          <Button 
            onClick={() => setCreateDialogOpen(true)} 
            size="sm" 
            className="gap-2"
            disabled={isReadOnly}
            title={isReadOnly ? "Disabled in read-only mode" : undefined}
          >
            <Plus className="h-4 w-4" />
            <span className="hidden sm:inline">Create Group</span>
            <span className="sm:hidden">Create</span>
          </Button>
        </div>
      </div>

      <UnreachableNodesBanner noun="groups" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Groups</CardTitle>
          <TableToolbar filters={filters} noun="groups" />
        </CardHeader>
        <CardContent>
          {groups.length === 0 ? (
            <div className="text-center py-12">
              <Users className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">No groups found</h3>
              <p className="text-muted-foreground mb-4">
                Create your first group to manage permissions and identities
              </p>
              <Button onClick={() => setCreateDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-2" />
                Create Group
              </Button>
            </div>
          ) : (
            <>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Identities" field="identities" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Permissions" field="permissions" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {paginatedGroups.length === 0 ? (
                  <TableEmptyRow
                    colSpan={6}
                    icon={Users}
                    title="No groups found"
                    description={
                      filters.activeFilterCount > 0
                        ? "No group matches the current filters."
                        : undefined
                    }
                  />
                ) : (
                  paginatedGroups.map((group: any) => (
                  <TableRow key={`${group.node}-${group.name}`}>
                    <TableCell className="font-medium">{group.name}</TableCell>
                    <TableCell className="max-w-xs truncate">
                      {group.description || <span className="text-muted-foreground italic">No description</span>}
                    </TableCell>
                    <TableCell>
                      {group.node || <TableEmptyValue label="No node" />}
                    </TableCell>
                    <TableCell className="text-center">
                      <TableLink onClick={() => handleShowIdentities(group)}>
                        {((group.identities?.tls?.length || 0) + (group.identities?.oidc?.length || 0))} members
                      </TableLink>
                    </TableCell>
                    <TableCell className="text-center">
                      <TableLink onClick={() => handleShowPermissions(group)}>
                        {group.permissions?.length || 0} permissions
                      </TableLink>
                    </TableCell>
                    <TableCell className="text-right">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="sm">
                            <MoreVertical className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem 
                            onClick={() => handleEdit(group)}
                            disabled={isReadOnly}
                          >
                            <Edit className="h-4 w-4 mr-2" />
                            Edit Group
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleShowIdentities(group)}>
                            <UserCheck className="h-4 w-4 mr-2" />
                            View Members
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleShowPermissions(group)}>
                            <Shield className="h-4 w-4 mr-2" />
                            View Permissions
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem 
                            onClick={() => handleDelete(group)}
                            className="text-destructive focus:text-destructive"
                            disabled={isReadOnly}
                          >
                            <Trash2 className="h-4 w-4 mr-2" />
                            Delete Group
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </TableCell>
                  </TableRow>
                ))
              )}
              </TableBody>
            </Table>
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages || 1}
              totalItems={sortedGroups.length}
              pageSize={pageSize}
              onPageChange={setCurrentPage}
              onPageSizeChange={setPageSize}
            />
            </>
          )}
        </CardContent>
      </Card>

      {/* Create Group Dialog */}
      <CreateGroupDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
        nodes={nodes}
      />

      {/* Edit Group Dialog */}
      <EditGroupDialog
        open={editDialogOpen}
        onOpenChange={setEditDialogOpen}
        group={selectedGroup}
        onRefresh={refetch}
      />

      {/* Identities Modal */}
      <Dialog open={identitiesModalOpen} onOpenChange={setIdentitiesModalOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              Group Members: {selectedGroup?.name}
            </DialogTitle>
            <DialogDescription>
              {((selectedGroup?.identities?.tls?.length || 0) + (selectedGroup?.identities?.oidc?.length || 0))} member(s) in this group
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-4">
            {/* TLS Identities */}
            {selectedGroup?.identities?.tls?.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2">
                  TLS Identities ({selectedGroup.identities.tls.length})
                </h4>
                <div className="space-y-2">
                  {selectedGroup.identities.tls.map((identity: string, index: number) => {
                    const identityName = getIdentityName(identity);
                    return (
                      <div 
                        key={`tls-${index}`}
                        className="flex flex-col gap-1 p-3 rounded-lg bg-secondary/50 hover:bg-secondary transition-colors"
                      >
                        <div className="flex items-center gap-2">
                          <Shield className="h-4 w-4 text-muted-foreground shrink-0" />
                          <span className="font-medium text-sm">
                            {identityName || "N/A"}
                          </span>
                        </div>
                        <span className="font-mono text-xs text-muted-foreground ml-6">
                          {identity}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            
            {/* OIDC Identities */}
            {selectedGroup?.identities?.oidc?.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2 flex items-center gap-2">
                  <Users className="h-4 w-4" />
                  OIDC Identities ({selectedGroup.identities.oidc.length})
                </h4>
                <div className="space-y-2">
                  {selectedGroup.identities.oidc.map((identity: string, index: number) => {
                    const identityName = getIdentityName(identity);
                    return (
                      <div 
                        key={`oidc-${index}`}
                        className="flex items-center gap-2 p-3 rounded-lg bg-secondary/50 hover:bg-secondary transition-colors"
                      >
                        <Users className="h-4 w-4 text-muted-foreground shrink-0" />
                        <span className="font-medium text-sm">
                          {identityName || identity}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            
            {/* No members */}
            {(!selectedGroup?.identities?.tls?.length && !selectedGroup?.identities?.oidc?.length) && (
              <div className="text-center py-8 text-muted-foreground">
                No members in this group
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Permissions Modal */}
      <Dialog open={permissionsModalOpen} onOpenChange={setPermissionsModalOpen}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              Permissions: {selectedGroup?.name}
            </DialogTitle>
            <DialogDescription>
              {selectedGroup?.permissions?.length || 0} permission(s) configured for this group
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-4">
            {selectedGroup?.permissions?.length > 0 ? (
              <>
              <TableToolbar filters={permFilters} noun="permissions" />
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Entity Type" field="entity_type" currentField={permSortField} direction={permSortDirection} onSort={permHandleSort} />
                    <SortableTableHead label="Resource URL" field="url" currentField={permSortField} direction={permSortDirection} onSort={permHandleSort} />
                    <SortableTableHead label="Entitlement" field="entitlement" currentField={permSortField} direction={permSortDirection} onSort={permHandleSort} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {permSortItems(permFilters.filteredRows).map((permission: any, index: number) => (
                    <TableRow key={index}>
                      <TableCell>
                        {formatLabel(permission.entity_type)}
                      </TableCell>
                      <TableCell className="font-mono max-w-md truncate">
                        {permission.url || "N/A"}
                      </TableCell>
                      <TableCell>
                        {getEntitlementLabel(permission.entitlement)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              </>
            ) : (
              <div className="text-center py-8 text-muted-foreground">
                No permissions configured for this group
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete Group Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={(open) => {
        if (!open && !isDeleting) {
          setDeleteDialogOpen(false);
          setGroupToDelete(null);
        }
      }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-destructive">Delete Group</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete group
              <strong className="text-foreground"> {groupToDelete?.name}</strong>
              {groupToDelete?.node && (
                <> from node <strong className="text-foreground">{groupToDelete.node}</strong></>
              )}?
              <span className="block mt-2 text-muted-foreground text-xs">
                This action cannot be undone. All identities and permissions assigned to this group will be removed.
              </span>
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-3 mt-4">
            <Button
              variant="outline"
              onClick={() => {
                setDeleteDialogOpen(false);
                setGroupToDelete(null);
              }}
              disabled={isDeleting}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={confirmDelete}
              disabled={isDeleting}
            >
              {isDeleting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Deleting...
                </>
              ) : (
                "Delete Group"
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// important: the five sibling partner pages gate here and these two did not, so a tenant
// deep-linking straight to the route got the page shell and a wall of 403s instead of a
// redirect. The backend is the real gate (require_partner on both routers); this keeps the
// UI honest and stops the request being made at all, because the content component's
// queries only mount once the role resolves.
export default function GroupsPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  // Suspense is required because useTableFilters reads useSearchParams, which opts the
  // component out of prerendering. The role gate stays in this file: check-role-gates.mjs
  // reads page text and does not follow imports.
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <GroupsContent />
    </Suspense>
  );
}
