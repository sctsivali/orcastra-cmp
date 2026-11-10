"use client";

import { Suspense, useCallback, useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Loader2, Plus, ShieldCheck, Trash2, Users } from "lucide-react";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { TableEmptyRow } from "@/components/ui/table-text";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { Skeleton } from "@/components/ui/skeleton";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TenantAccessDialog } from "@/components/tenant-access-dialog";
import { api, integrationsAPI } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { usePermissions } from "@/hooks/use-permissions";
import { useTableSort } from "@/hooks/use-table-sort";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { formatLabel, plural } from "@/lib/utils";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type {
  IntegrationOrganization, UserGroupEntry, UserGroupOrganization, UserGroupRole,
} from "@/types";
import { UsersIllustration } from "@/components/illustrations/scenes/onboarding";

/**
 * Every assigned user across every organization the caller runs, in one list that grows with the
 * fleet, so it has search as well as sorting.
 *
 * important: an id, and one that is not "policies". The Access Policies section renders its own
 * filtered table under the same tab, and two tables without a namespace write `q` into the same
 * key and narrow each other on every keystroke.
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
      // Sorted by how many organizations a user reaches, filtered by which ones, so an operator
      // can narrow the list to who can see a given organization.
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

const isValidEmail = (value: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);

/** The organizations where this person is a tenant: the only ones the Access dialog governs. */
function tenantOrgIds(user: UserGroupEntry): number[] {
  return (user.organizations ?? []).filter((org) => org.role === "tenant").map((org) => Number(org.id));
}

function AssignedUsersPanelContent() {
  const queryClient = useQueryClient();
  const { isAdmin } = usePermissions();
  const fieldId = useId();
  const [assignOpen, setAssignOpen] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [accessUser, setAccessUser] = useState<{ email: string; orgIds: number[] } | null>(null);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserGroupRole>("tenant");
  const [orgId, setOrgId] = useState<string>("");

  const {
    data: users,
    isLoading: usersLoading,
    error: usersError,
    refetch: refetchUsers,
  } = useQuery({
    queryKey: ["user-groups"],
    queryFn: () => api.userGroups.list(),
  });

  // The organizations a role is assigned in: those synced from OrcaHub and the local ones an
  // administrator created here. The list's default leaves local organizations out, so without the
  // second read a partner could not be given the row to run an organization made in the console.
  const { data: orgs } = useQuery({
    queryKey: ["integration-organizations", "with-local"],
    queryFn: async () => {
      const [synced, local] = await Promise.all([
        integrationsAPI.listOrganizations(),
        integrationsAPI.listOrganizations("local"),
      ]);
      const known = new Set(synced.map((org) => org.id));
      return [...synced, ...local.filter((org) => !known.has(org.id))];
    },
  });

  // Memoised: a fresh array every render would re-run the filter pass over unchanged rows.
  const userRows = useMemo<UserGroupEntry[]>(() => users ?? [], [users]);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  const filters = useTableFilters(USERS, userRows, {
    // Covers every seam at once: the search box, both facets, chip removal and Clear all.
    // Without it a search lands on page 3 of 1 and paints nothing.
    onFilterChange: resetPage,
    // A section of a settings tab, not a route of its own, so its filters would outlive the
    // screen in the address bar.
    syncUrl: false,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    USERS.sortAccessors,
    USERS.defaultSort.field,
    USERS.defaultSort.direction,
    resetPage,
  );

  // Filter, then sort, then slice, so sorting covers the whole filtered set.
  const sortedUsers = useMemo(() => sortItems(filters.filteredRows), [sortItems, filters.filteredRows]);
  // Clamped on read, never corrected in an effect: an effect would snap the operator back a
  // page mid-task and still paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(sortedUsers.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedUsers = sortedUsers.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const assignMutation = useMutation({
    mutationFn: api.userGroups.assign,
    onSuccess: (answer) => {
      void reconcileMutationImpact(queryClient, "user-group.write").catch(() => undefined);
      setAssignOpen(false);
      // Two outcomes the list cannot show by itself. An administrator with no organization has
      // no membership row, so it never appears here, and a role the identity provider did not
      // confirm is saved here but not yet in force.
      const notes: string[] = [];
      if (answer.organization_id == null) notes.push("An administrator with no organization is not listed here.");
      if (!answer.authentik_synced) notes.push("The identity provider did not confirm the role. The account may not exist there yet.");
      toast({
        title: `${formatLabel(answer.role)} role assigned to ${answer.user_email}`,
        description: notes.join(" ") || undefined,
      });
    },
  });

  const removeMutation = useMutation({
    mutationFn: (userEmail: string) => api.userGroups.remove(userEmail),
    onSuccess: (answer, userEmail) => {
      void reconcileMutationImpact(queryClient, "user-group.write").catch(() => undefined);
      setRemoving(null);
      toast(answer.kept.length > 0
        ? { title: `${userEmail} partly removed`, description: answer.detail }
        : { title: `${userEmail} removed` });
    },
    // important: read the list again on a refusal too. An administrator's removal commits the
    // membership rows before it asks the identity provider, so a 502 can follow a removal that
    // happened, and the row would otherwise stay on screen.
    onError: () => {
      void reconcileMutationImpact(queryClient, "user-group.write").catch(() => undefined);
    },
  });

  // important: claimed synchronously. isPending reaches the button only on the next render, so a
  // double click (or Enter twice) would otherwise send two requests.
  const submitInFlightRef = useRef(false);
  const removeInFlightRef = useRef(false);

  const orgRequired = role === "partner" || role === "tenant";
  const trimmedEmail = email.trim().toLowerCase();
  const emailInvalid = trimmedEmail !== "" && !isValidEmail(trimmedEmail);
  const canAssign = !!trimmedEmail && !emailInvalid && (!orgRequired || !!orgId) && !assignMutation.isPending;

  function openAssign() {
    setEmail("");
    setRole("tenant");
    setOrgId("");
    // Clear the previous attempt's error before the dialog opens again.
    assignMutation.reset();
    setAssignOpen(true);
  }

  const handleSubmit = async () => {
    if (submitInFlightRef.current || !canAssign) return;
    submitInFlightRef.current = true;
    try {
      await assignMutation.mutateAsync({
        user_email: trimmedEmail,
        role,
        organization_id: orgId ? Number(orgId) : undefined,
      });
    } catch {
      // Shown in the dialog from assignMutation.error.
    } finally {
      submitInFlightRef.current = false;
    }
  };

  const handleRemove = async (userEmail: string) => {
    if (removeInFlightRef.current) return;
    removeInFlightRef.current = true;
    try {
      await removeMutation.mutateAsync(userEmail);
    } catch {
      // Shown in the dialog from removeMutation.error.
    } finally {
      removeInFlightRef.current = false;
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>Assigned Users</CardTitle>
            <CardDescription className="mt-1.5">
              Assign roles and organizations to users by email. Changes sync to the identity provider
              when configured.
            </CardDescription>
          </div>
          <Button size="sm" onClick={openAssign} className="shrink-0">
            <Plus className="h-4 w-4 mr-1.5" aria-hidden="true" />
            Assign User
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {usersLoading ? (
          <div className="flex items-center justify-center py-12" role="status">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-hidden="true" />
            <span className="ml-2 text-muted-foreground">Loading users…</span>
          </div>
        ) : usersError && !users ? (
          <ReadErrorState noun="users" error={usersError} onRetry={() => void refetchUsers()} />
        ) : userRows.length === 0 ? (
          <EmptyState
            icon={Users}
            illustration={UsersIllustration}
            title="No users assigned yet"
            description={'Click "Assign User" to add a user with a role'}
          />
        ) : (
          <>
            <TableToolbar filters={filters} noun="users" pageSize={pageSize} className="mb-4" />
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Email" field="email" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Role" field="role" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Organizations" field="organizations" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableHead className="w-[120px]"><span className="sr-only">Actions</span></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pagedUsers.length === 0 ? (
                    <TableEmptyRow
                      colSpan={USER_COLUMN_COUNT}
                      icon={Users}
                      title="No user matches the current filters"
                      description={`${plural(filters.activeFilterCount, "filter")} active on ${plural(userRows.length, "user")}.`}
                      action={
                        <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                          Clear filters
                        </Button>
                      }
                    />
                  ) : pagedUsers.map((user) => {
                    const accessOrgIds = tenantOrgIds(user);
                    return (
                      <TableRow key={user.user_email}>
                        <TableCell className="font-medium">{user.user_email}</TableCell>
                        <TableCell>{formatLabel(user.role)}</TableCell>
                        <TableCell>
                          <OverflowCell
                            items={user.organizations}
                            preview={3}
                            noun="organizations"
                            ownerLabel={user.user_email}
                            emptyLabel="No organizations"
                            itemLabel="organization"
                            renderInline={(org) => (org as UserGroupOrganization).name}
                            renderItem={(org) => {
                              const o = org as UserGroupOrganization;
                              return (
                                <span className="flex min-w-0 flex-col">
                                  <span className="break-words font-medium">{o.name}</span>
                                  <span className="text-xs text-muted-foreground">
                                    {formatLabel(o.role)}
                                    {o.slug ? <span className="break-all font-mono"> · {o.slug}</span> : null}
                                  </span>
                                </span>
                              );
                            }}
                          />
                        </TableCell>
                        <TableCell>
                          {/* Right-aligned, so the remove control sits in one column whether or
                              not the row has an Access button before it. */}
                          <div className="flex items-center justify-end gap-1.5">
                            {/* Access governs tenant memberships, which a partner elsewhere may also hold. */}
                            {accessOrgIds.length > 0 && (
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-7 text-xs gap-1.5"
                                onClick={() => setAccessUser({ email: user.user_email, orgIds: accessOrgIds })}
                              >
                                <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                                Access<span className="sr-only"> for {user.user_email}</span>
                              </Button>
                            )}
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 text-destructive hover:text-destructive"
                              onClick={() => {
                                removeMutation.reset();
                                setRemoving(user.user_email);
                              }}
                              aria-label={`Remove ${user.user_email}`}
                              title="Remove user"
                            >
                              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
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

      <Dialog open={assignOpen} onOpenChange={(open) => !assignMutation.isPending && setAssignOpen(open)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Assign User Role</DialogTitle>
            <DialogDescription>
              {isAdmin
                ? "Assign a role to a user by email. Partner and tenant roles need an organization."
                : "Add a tenant to an organization you run, by email."}
            </DialogDescription>
          </DialogHeader>

          <form
            className="space-y-4 py-2"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSubmit();
            }}
          >
            <div className="space-y-2">
              <Label htmlFor={`${fieldId}-email`}>Email Address</Label>
              <Input
                id={`${fieldId}-email`}
                type="email"
                autoComplete="off"
                placeholder="user@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                maxLength={255}
                aria-invalid={emailInvalid || undefined}
                aria-describedby={emailInvalid ? `${fieldId}-email-error` : undefined}
              />
              {emailInvalid && (
                <p id={`${fieldId}-email-error`} className="text-xs text-destructive">
                  Enter a valid email address (for example, user@example.com).
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor={`${fieldId}-role`}>Role</Label>
              <Select value={role} onValueChange={(v) => setRole(v as UserGroupRole)} disabled={!isAdmin}>
                <SelectTrigger id={`${fieldId}-role`} aria-describedby={`${fieldId}-role-hint`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {/* The server refuses any other role from a partner with 403, so a partner is
                      offered Tenant only. */}
                  {isAdmin && <SelectItem value="admin">Admin</SelectItem>}
                  {isAdmin && <SelectItem value="partner">Partner</SelectItem>}
                  <SelectItem value="tenant">Tenant</SelectItem>
                </SelectContent>
              </Select>
              <p id={`${fieldId}-role-hint`} className="text-xs text-muted-foreground">
                {isAdmin
                  ? "Admin = full access, Partner = manage org & clusters, Tenant = view assigned resources"
                  : "Partners add tenants. An administrator assigns the other roles."}
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor={`${fieldId}-org`}>
                Organization{" "}
                {orgRequired ? (
                  <span className="text-destructive font-normal">(required)</span>
                ) : (
                  <span className="text-muted-foreground font-normal">(optional)</span>
                )}
              </Label>
              <Select value={orgId || "__none__"} onValueChange={(v) => setOrgId(v === "__none__" ? "" : v)}>
                <SelectTrigger id={`${fieldId}-org`}>
                  <SelectValue placeholder="Select organization…" />
                </SelectTrigger>
                <SelectContent>
                  {!orgRequired && <SelectItem value="__none__">None</SelectItem>}
                  {orgs?.map((org: IntegrationOrganization) => (
                    <SelectItem key={org.id} value={String(org.id)}>
                      {org.name}
                      {org.app_name && (
                        <span className="text-muted-foreground ml-1">via {org.app_name}</span>
                      )}
                      {org.source === "local" && <span className="text-muted-foreground ml-1">local</span>}
                    </SelectItem>
                  ))}
                  {(!orgs || orgs.length === 0) && orgRequired && (
                    <div className="px-2 py-1.5 text-xs text-muted-foreground">
                      No organizations yet. Create one, or connect OrcaHub under Integrations.
                    </div>
                  )}
                </SelectContent>
              </Select>
            </div>

            {assignMutation.error && (
              <div className="flex items-center gap-2 text-sm text-destructive" role="alert">
                <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
                {assignMutation.error.message}
              </div>
            )}

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setAssignOpen(false)} disabled={assignMutation.isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={!canAssign}>
                {assignMutation.isPending && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" aria-hidden="true" />}
                Assign
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={removing !== null}
        onOpenChange={(open) => !open && !removeMutation.isPending && setRemoving(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove User</AlertDialogTitle>
            <AlertDialogDescription>
              {isAdmin
                ? <>Remove <strong>{removing}</strong> from every organization and identity provider role group? This cannot be undone.</>
                : <>Remove <strong>{removing}</strong> from the organizations you run? This cannot be undone.</>}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {removeMutation.error && (
            <div className="flex items-start gap-2 text-sm text-destructive" role="alert">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <span>{removeMutation.error.message}</span>
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removeMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              // Held open until the answer, so a refusal (a 409 for a synced partner row, a 502
              // when the identity provider did not confirm) shows in this dialog.
              onClick={(event) => {
                event.preventDefault();
                if (removing) void handleRemove(removing);
              }}
              disabled={removeMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {removeMutation.isPending && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" aria-hidden="true" />}
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {accessUser && (
        <TenantAccessDialog
          userEmail={accessUser.email}
          userOrgIds={accessUser.orgIds}
          open
          onOpenChange={(open) => {
            if (!open) setAccessUser(null);
          }}
        />
      )}
    </Card>
  );
}

/**
 * The boundary the filter hook requires: useTableFilters reads the query string even with
 * syncUrl off, which opts a client component out of prerendering, and Next fails the build on
 * the route without one.
 */
export function AssignedUsersPanel() {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-xl" />}>
      <AssignedUsersPanelContent />
    </Suspense>
  );
}
