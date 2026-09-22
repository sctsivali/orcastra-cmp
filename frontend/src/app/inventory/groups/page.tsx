"use client";

import { Suspense, useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
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
import {
  GroupDetailDialog,
  type GroupDetailTab,
} from "./components/group-detail-dialog";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TableEmptyValue } from "@/components/ui/table-text";
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
import { OverflowText } from "@/components/ui/overflow-text";
import { useRowFocus } from "@/hooks/use-row-focus";
import { requestRowFocus } from "@/lib/row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";
import { NodeLink } from "@/components/ui/node-link";

// filter-coverage: no project dimension here, and not for lack of a mechanism. A group's
// permissions[].url values do carry ?project= when LXD scopes them, and projectsFromUsedBy
// already parses exactly that shape. Sampling the live fleet found one distinct permission URL
// across every group, `/1.0`, with no project qualifier at all, so the facet would derive
// "default" for every row: a single-option control that the pinned-role rule would then render
// unconditionally. That turns an invisible gap into a visible dead control. Revisit if
// per-project group permissions ever appear in the wild.
/** One key for a row and for the dialog's report of what it made. */
const groupRowKey = (g: { node?: string | null; name: string }) => `${g.node ?? ""}/${g.name}`;

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
      label: "Cluster",
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

function GroupsContent() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useGroups();
  const groups = (data as any)?.groups || [];
  const { toast } = useToast();
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const reach = reachabilityOf(data);
  const deleteGroupMutation = useDeleteGroup();
  const { isReadOnly } = useReadOnlyMode();
  
  // Pagination and search state
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  
  // State for modals
  // Which panel the clicked cell wants. null means the dialog is closed.
  const [groupDetailTab, setGroupDetailTab] = useState<GroupDetailTab | null>(null);
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
  const focus = useRowFocus({
    list: "groups",
    rows: groups,
    orderedRows: sortedGroups,
    keyOf: groupRowKey,
    pageSize,
    setPage: setCurrentPage,
    dataUpdatedAt,
    clearFilters: filters.clearAll,
  });

  // Pagination
  const totalPages = Math.ceil(sortedGroups.length / pageSize);
  const paginatedGroups = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return sortedGroups.slice(startIndex, startIndex + pageSize);
  }, [sortedGroups, currentPage, pageSize]);

  // Permissions modal: its own filter and sort instance, state kept out of the URL.

  const openGroupDetail = (group: any, tab: GroupDetailTab) => {
    setSelectedGroup(group);
    setGroupDetailTab(tab);
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

      <UnreachableNodesBanner noun="groups" nodes={reach.unreachable} reasons={reach.reasons} />
      <UnreachableNodesBanner noun="groups" nodes={reach.partial} reasons={reach.reasons} partial />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Groups</CardTitle>
          <TableToolbar filters={filters} noun="groups" />
        </CardHeader>
        <CardContent>
          {focus.hidden && (
            <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
          )}
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
                  <SortableTableHead label="Cluster" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
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
                  <TableRow key={groupRowKey(group)} {...focus.rowProps(groupRowKey(group))}>
                    <TableCell className="font-medium">{group.name}</TableCell>
                    <TableCell className="max-w-xs">
                      {group.description ? (
                        <OverflowText
                          value={group.description}
                          label={`description for group ${group.name}`}
                          lines={2}
                        />
                      ) : (
                        <TableEmptyValue label="No description" />
                      )}
                    </TableCell>
                    <TableCell><NodeLink clusterId={group.node} /></TableCell>
                    <TableCell className="text-center">
                      <TableLink onClick={() => openGroupDetail(group, "members")}>
                        {((group.identities?.tls?.length || 0) + (group.identities?.oidc?.length || 0))} members
                      </TableLink>
                    </TableCell>
                    <TableCell className="text-center">
                      <TableLink onClick={() => openGroupDetail(group, "permissions")}>
                        {group.permissions?.length || 0} permissions
                      </TableLink>
                    </TableCell>
                    <TableCell className="text-right">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="sm" aria-label={`Actions for group ${group.name}`}>
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
                          <DropdownMenuItem onClick={() => openGroupDetail(group, "members")}>
                            <UserCheck className="h-4 w-4 mr-2" />
                            View Members
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => openGroupDetail(group, "permissions")}>
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
        onSaved={(created) => {
          requestRowFocus("groups", groupRowKey(created), created.name);
          // the create already succeeded; a refresh that fails is the list's own error state
          void Promise.resolve(refreshNow()).catch(() => undefined);
        }}
      />

      {/* Edit Group Dialog */}
      <EditGroupDialog
        open={editDialogOpen}
        onOpenChange={setEditDialogOpen}
        group={selectedGroup}
        onRefresh={refetch}
      />

      {/* One dialog for both. Merging them also gave the group its own Overview, which is
          where `description` and `node` finally appear: the members modal dropped both. */}
      <GroupDetailDialog
        group={selectedGroup}
        open={groupDetailTab !== null}
        defaultTab={groupDetailTab ?? "overview"}
        onOpenChange={(next) => {
          if (!next) setGroupDetailTab(null);
        }}
      />

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
                <> from cluster <strong className="text-foreground">{groupToDelete.node}</strong></>
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
