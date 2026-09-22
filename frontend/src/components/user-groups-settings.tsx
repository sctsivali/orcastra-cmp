"use client";

import { Suspense, useCallback, useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { TableEmptyRow } from "@/components/ui/table-text";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertCircle,
  Loader2,
  Plus,
  Trash2,
  Users,
  Building2,
  WifiOff,
  Wifi,
  ShieldCheck,
} from "lucide-react";
import { api, integrationsAPI } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { PolicyManagementSection } from "@/components/policy-management-section";
import { TenantAccessDialog } from "@/components/tenant-access-dialog";
import { useTableSort } from "@/hooks/use-table-sort";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { formatLabel } from "@/lib/utils";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type {
  UserGroupEntry,
  IntegrationOrganization,
  AuthentikSyncStatus,
} from "@/types";

// role badge styling
function roleBadge(role: string) {
  switch (role) {
    case "admin":
      return (
        <Badge variant="destructive" className="text-xs">
          Admin
        </Badge>
      );
    case "partner":
      return (
        <Badge className="bg-blue-600 hover:bg-blue-700 text-white text-xs">
          Partner
        </Badge>
      );
    case "tenant":
    default:
      return (
        <Badge variant="secondary" className="text-xs">
          Tenant
        </Badge>
      );
  }
}

/**
 * Every assigned user across every organization lands in this one list, so an operator looking
 * for one person is scanning a set that grows with the whole fleet. Search is what makes a row
 * findable once the list is longer than a screen; sorting alone stops helping there.
 *
 * important: an id, and one that is not "policies". PolicyManagementSection renders its own
 * filtered table on this same screen, and two tables without a namespace write `q` into the
 * same key and narrow each other on every keystroke.
 */
const USERS = defineTable<UserGroupEntry>()({
  id: "users",
  searchPlaceholder: "Search users by email or organization",
  defaultSort: { field: "email", direction: "asc" },
  searchExtra: (user) => (user.organizations ?? []).flatMap((org) => [org.name, org.slug]),
  columns: [
    {
      field: "email",
      label: "Email",
      sort: (user) => user.user_email,
      search: (user) => user.user_email,
      filter: fromSort("text"),
    },
    {
      field: "role",
      label: "Role",
      sort: (user) => user.role,
      facet: (user) => user.role,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      // note: sorted by how many organizations a user reaches, filtered by which ones. The
      // count is what the column answers at a glance; the names are what an operator narrows
      // by, and "who can see org X" is the question this list is worst at answering by eye.
      field: "organizations",
      label: "Organizations",
      sort: (user) => user.organizations?.length ?? 0,
      facet: (user) => (user.organizations ?? []).map((org) => org.name),
      facetSpec: { primary: true, emptyLabel: "No organizations" },
    },
  ],
});

/** Email, Role, Organizations, Actions. */
const USER_COLUMN_COUNT = 4;

function UserGroupsSettingsContent() {
  const queryClient = useQueryClient();
  const [assignOpen, setAssignOpen] = useState(false);
  const [deleteEmail, setDeleteEmail] = useState<string | null>(null);
  const [accessEmail, setAccessEmail] = useState<string | null>(null);
  const [accessOrgIds, setAccessOrgIds] = useState<number[]>([]);

  // form state
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("tenant");
  const [orgId, setOrgId] = useState<string>("");

  // fetch user list
  const {
    data: users,
    isLoading: usersLoading,
    error: usersError,
    refetch: refetchUsers,
    dataUpdatedAt: usersUpdatedAt,
  } = useQuery({
    queryKey: ["user-groups"],
    queryFn: () => api.userGroups.list(),
  });

  // fetch organizations synced from external apps (OrcaHub)
  const { data: orgs } = useQuery({
    queryKey: ["integration-organizations"],
    queryFn: () => integrationsAPI.listOrganizations(),
  });

  // fetch identity provider sync status
  const { data: syncStatus, refetch: refetchSync } = useQuery({
    queryKey: ["user-groups-sync-status"],
    queryFn: () => api.userGroups.syncStatus(),
    refetchInterval: 30000, // check every 30s
  });

  // note: memoised. A fresh array every render hands the filter engine a new identity each
  // time and re-runs the whole pass over rows that did not change.
  const userRows = useMemo<UserGroupEntry[]>(() => users ?? [], [users]);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  const filters = useTableFilters(USERS, userRows, {
    // Covers every seam at once: the search box, both facets, chip removal and Clear all.
    // Without it a search lands on page 3 of 1 and paints nothing.
    onFilterChange: resetPage,
    // This table lives inside a settings card, not on a route of its own, so its filters
    // would outlive the screen in the address bar.
    syncUrl: false,
  });

  const {
    sortField: userSortField,
    sortDirection: userSortDirection,
    handleSort: handleUserSort,
    sortItems: sortUsers,
  } = useTableSort(
    USERS.sortAccessors,
    USERS.defaultSort.field,
    USERS.defaultSort.direction,
    resetPage,
  );

  // Filter, then sort, then slice. Sorting therefore covers the whole filtered set rather than
  // the page on screen.
  const sortedUsers = useMemo(
    () => sortUsers(filters.filteredRows),
    [sortUsers, filters.filteredRows],
  );
  // Clamped on read, never corrected in an effect: an effect would snap the operator back a
  // page mid-task and still paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(sortedUsers.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedUsers = sortedUsers.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // assign mutation
  const assignMutation = useMutation({
    mutationFn: api.userGroups.assign,
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "user-group.write").catch(() => undefined);
      setAssignOpen(false);
      resetForm();
    },
  });

  // delete mutation
  const deleteMutation = useMutation({
    mutationFn: (userEmail: string) => api.userGroups.remove(userEmail),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "user-group.write").catch(() => undefined);
      setDeleteEmail(null);
    },
  });

  function resetForm() {
    setEmail("");
    setRole("tenant");
    setOrgId("");
  }

  // org is required for partner/tenant roles
  const orgRequired = role === "partner" || role === "tenant";
  // simple email regex (RFC 5322 simplified)
  const isValidEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);

  function handleAssign() {
    if (!email.trim()) return;
    if (!isValidEmail(email.trim())) return;
    if (orgRequired && !orgId) return;
    assignMutation.mutate({
      user_email: email.trim(),
      role,
      organization_id: orgId || undefined,
    });
  }

  // note: failures propagate to the refresh control, which reports them. Success
  // is reported inline by the same control rather than by a toast.
  async function handleRefreshAll() {
    await Promise.all([
      refetchSync({ throwOnError: true }),
      refetchUsers({ throwOnError: true }),
      queryClient.refetchQueries({ queryKey: ["tenant-access-policies"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["integration-organizations"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["organizations"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["clusters"], type: "active" }),
    ]);
  }

  // helper: open access dialog for a tenant user
  const openAccessDialog = (user: UserGroupEntry) => {
    setAccessEmail(user.user_email);
    setAccessOrgIds(user.organizations?.map(o => parseInt(String(o.id))) || []);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-2xl font-bold">User Groups</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Manage user roles, organizations, access policies, and identity provider sync.
          </p>
        </div>
        <RefreshButton
          label="Refresh user groups, policies, and sync status"
          lastUpdatedAt={usersUpdatedAt}
          onRefresh={handleRefreshAll}
        />
      </div>

      {/* Identity Provider Sync Status */}
      <Card>
        <CardHeader className="pb-3">
          <div>
            <CardTitle className="text-base flex items-center gap-2">
              Identity Provider Sync Status
            </CardTitle>
            <CardDescription className="mt-1 text-xs">
              User roles and group memberships are synchronized from your identity provider (Authentik).
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3">
            {syncStatus?.authentik_configured ? (
              syncStatus.authentik_reachable ? (
                <>
                  <Wifi className="h-4 w-4 text-green-500" />
                  <span className="text-sm text-green-600 dark:text-green-400">
                    {syncStatus.groups_exist
                      ? "Connected. Role changes and groups sync automatically."
                      : "Connected. Role changes sync automatically."}
                  </span>
                </>
              ) : (
                <>
                  <WifiOff className="h-4 w-4 text-yellow-500" />
                  <span className="text-sm text-yellow-600 dark:text-yellow-400">
                    Configured but unreachable. Changes are saved locally only.
                  </span>
                </>
              )
            ) : (
              <>
                <WifiOff className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">
                  Not configured. Set identity provider credentials to enable sync.
                </span>
              </>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Access Policies Management */}
      <PolicyManagementSection />

      {/* User Groups Table */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>
                Assigned Users
              </CardTitle>
              <CardDescription className="mt-1.5">
                Assign roles and organizations to users by email. Changes sync
                to the identity provider when configured.
              </CardDescription>
            </div>
            <div className="flex gap-2">
              <Dialog open={assignOpen} onOpenChange={setAssignOpen}>
                <DialogTrigger asChild>
                  <Button size="sm" onClick={resetForm}>
                    <Plus className="h-4 w-4 mr-1.5" />
                    Assign User
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Assign User Role</DialogTitle>
                    <DialogDescription>
                      Assign a role and optionally an organization to a user by
                      their email address.
                    </DialogDescription>
                  </DialogHeader>

                  <div className="space-y-4 py-2">
                    <div className="space-y-2">
                      <Label htmlFor="user-email">Email Address</Label>
                      <Input
                        id="user-email"
                        type="email"
                        placeholder="user@example.com"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                      />
                      {email.trim() && !isValidEmail(email.trim()) && (
                        <p className="text-xs text-destructive">
                          Please enter a valid email address (e.g. user@example.com)
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label>Role</Label>
                      <Select value={role} onValueChange={setRole}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="admin">Admin</SelectItem>
                          <SelectItem value="partner">Partner</SelectItem>
                          <SelectItem value="tenant">Tenant</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Admin = full access, Partner = manage org &amp; clusters,
                        Tenant = view assigned resources
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label>
                        Organization{" "}
                        {orgRequired ? (
                          <span className="text-destructive font-normal">(required)</span>
                        ) : (
                          <span className="text-muted-foreground font-normal">(optional)</span>
                        )}
                      </Label>
                      <Select value={orgId || "__none__"} onValueChange={(v) => setOrgId(v === "__none__" ? "" : v)}>
                        <SelectTrigger>
                          <SelectValue placeholder="Select organization..." />
                        </SelectTrigger>
                        <SelectContent>
                          {!orgRequired && (
                            <SelectItem value="__none__">None</SelectItem>
                          )}
                          {orgs?.map((org: IntegrationOrganization) => (
                            <SelectItem key={org.id} value={String(org.id)}>
                              {org.name}
                              {org.app_name && (
                                <span className="text-muted-foreground ml-1">
                                  via {org.app_name}
                                </span>
                              )}
                            </SelectItem>
                          ))}
                          {(!orgs || orgs.length === 0) && orgRequired && (
                            <div className="px-2 py-1.5 text-xs text-muted-foreground">
                              No organizations synced. Connect OrcaHub via Integrations first.
                            </div>
                          )}
                        </SelectContent>
                      </Select>
                    </div>

                    {assignMutation.error && (
                      <div className="flex items-center gap-2 text-sm text-destructive">
                        <AlertCircle className="h-4 w-4" />
                        {(assignMutation.error as Error).message}
                      </div>
                    )}
                  </div>

                  <DialogFooter>
                    <Button
                      variant="outline"
                      onClick={() => setAssignOpen(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      onClick={handleAssign}
                      disabled={!email.trim() || !isValidEmail(email.trim()) || (orgRequired && !orgId) || assignMutation.isPending}
                    >
                      {assignMutation.isPending && (
                        <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                      )}
                      Assign
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {usersLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              <span className="ml-2 text-muted-foreground">Loading users...</span>
            </div>
          ) : usersError ? (
            <div className="flex items-center gap-2 py-8 justify-center text-destructive">
              <AlertCircle className="h-5 w-5" />
              <span>Failed to load users: {(usersError as Error).message}</span>
            </div>
          ) : userRows.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground">
              <Users className="h-10 w-10 mx-auto mb-3 opacity-40" />
              <p className="text-sm">No users assigned yet</p>
              <p className="text-xs mt-1">
                Click &quot;Assign User&quot; to add a user with a role
              </p>
            </div>
          ) : (
            <>
            <TableToolbar filters={filters} noun="users" className="mb-4" />
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead
                      label="Email"
                      field="email"
                      currentField={userSortField}
                      direction={userSortDirection}
                      onSort={handleUserSort}
                    />
                    <SortableTableHead
                      label="Role"
                      field="role"
                      currentField={userSortField}
                      direction={userSortDirection}
                      onSort={handleUserSort}
                    />
                    <SortableTableHead
                      label="Organizations"
                      field="organizations"
                      currentField={userSortField}
                      direction={userSortDirection}
                      onSort={handleUserSort}
                    />
                    <TableHead className="w-[120px]">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pagedUsers.length === 0 ? (
                    <TableEmptyRow
                      colSpan={USER_COLUMN_COUNT}
                      icon={Users}
                      title="No user matches the current filters"
                      description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active on ${userRows.length} users.`}
                      action={
                        <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                          Clear filters
                        </Button>
                      }
                    />
                  ) : pagedUsers.map((user: UserGroupEntry) => (
                    <TableRow key={user.user_email}>
                      <TableCell className="font-medium">
                        {user.user_email}
                      </TableCell>
                      <TableCell>{roleBadge(user.role)}</TableCell>
                      <TableCell>
                        <OverflowCell
                          items={user.organizations}
                          preview={3}
                          noun="organizations"
                          ownerLabel={user.user_email}
                          emptyLabel="No organizations"
                          itemLabel="organization"
                          renderInline={(org) => (org as { name: string }).name}
                          renderItem={(org) => {
                            const o = org as { name: string; slug?: string };
                            return (
                              <span className="flex min-w-0 flex-col">
                                <span className="break-words font-medium">{o.name}</span>
                                {o.slug ? (
                                  <span className="break-all font-mono text-xs text-muted-foreground">{o.slug}</span>
                                ) : null}
                              </span>
                            );
                          }}
                        />
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          {/* Access button - only for Tenant users */}
                          {user.role === "tenant" && (
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-7 text-xs gap-1.5 text-blue-600 border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-blue-800 dark:hover:bg-blue-950"
                              onClick={() => openAccessDialog(user)}
                            >
                              <ShieldCheck className="h-3.5 w-3.5" />
                              Access
                            </Button>
                          )}
                          <Dialog
                            open={deleteEmail === user.user_email}
                            onOpenChange={(open) =>
                              setDeleteEmail(open ? user.user_email : null)
                            }
                          >
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <DialogTrigger asChild>
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-7 w-7 text-destructive hover:text-destructive"
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </Button>
                                </DialogTrigger>
                              </TooltipTrigger>
                              <TooltipContent side="top">
                                <p className="text-xs">Remove user</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                          <DialogContent>
                            <DialogHeader className="pb-4">
                              <DialogTitle>Remove User</DialogTitle>
                              <DialogDescription>
                                Remove <strong>{user.user_email}</strong> from
                                all organizations and identity provider groups?
                                This cannot be undone.
                              </DialogDescription>
                            </DialogHeader>
                            <DialogFooter>
                              <Button
                                variant="outline"
                                onClick={() => setDeleteEmail(null)}
                              >
                                Cancel
                              </Button>
                              <Button
                                variant="destructive"
                                onClick={() =>
                                  deleteMutation.mutate(user.user_email)
                                }
                                disabled={deleteMutation.isPending}
                              >
                                {deleteMutation.isPending && (
                                  <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                )}
                                Remove
                              </Button>
                            </DialogFooter>
                          </DialogContent>
                        </Dialog>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            {/* Absent rather than disabled when everything fits on one page. */}
            {totalPages > 1 && (
              <div className="mt-4">
                <DataTablePagination
                  currentPage={currentPage}
                  totalPages={totalPages}
                  totalItems={sortedUsers.length}
                  pageSize={pageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]}
                  onPageChange={setPage}
                  onPageSizeChange={(size) => {
                    setPageSize(size);
                    setPage(1);
                  }}
                />
              </div>
            )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Tenant Access Dialog */}
      {accessEmail && (
        <TenantAccessDialog
          userEmail={accessEmail}
          userOrgIds={accessOrgIds}
          open={!!accessEmail}
          onOpenChange={(open) => {
            if (!open) {
              setAccessEmail(null);
              setAccessOrgIds([]);
            }
          }}
        />
      )}
    </div>
  );
}

/**
 * The boundary the filter hook requires.
 *
 * useTableFilters reads the query string even with syncUrl off, which opts a client component
 * out of prerendering, and Next then fails the build on the route unless a boundary sits above
 * it. Carrying its own means this can be dropped anywhere without the caller knowing that.
 */
export function UserGroupsSettings() {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-xl" />}>
      <UserGroupsSettingsContent />
    </Suspense>
  );
}
