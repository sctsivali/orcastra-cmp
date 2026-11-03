"use client";

import { Suspense, useCallback, useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { OrphanRulesNotice } from "@/components/tenant-access/orphan-rules-notice";
import { PolicyTermStatus } from "@/components/tenant-access/policy-term-status";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Loader2, Plus, Trash2, ChevronDown,
  ChevronRight, Edit, ShieldCheck,
} from "lucide-react";
import { api, integrationsAPI } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { useClusters } from "@/hooks/use-lxd";
import { useToast } from "@/hooks/use-toast";
import { useTableSort } from "@/hooks/use-table-sort";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { PolicyEditorDialog } from "@/components/access-policies/policy-editor-dialog";
import type { PolicyCluster } from "@/components/access-policies/rule-editor";
import type {
  AccessPolicy, PolicyCreateRequest, PolicyUpdateRequest, IntegrationOrganization,
} from "@/types";
import { plural } from "@/lib/utils";

// ============================================
// Main Exported Component
// ============================================

/** Resolved once in the component, where the organization list is in scope. */
interface PolicyContext {
  orgName: (organizationId: number) => string;
}

/**
 * A policy list is mostly `auto:cust-*` rows minted per customer project, so the names differ
 * by a hash and nothing else. Search is what makes one findable; sorting alone is not enough
 * once the list is longer than a screen.
 *
 * important: an id. This section renders inside the user-groups settings screen, which carries
 * its own filtered table, and without a namespace both would write `q` into the same key and
 * narrow each other on every keystroke.
 */
const POLICIES = defineTable<AccessPolicy, PolicyContext>()({
  id: "policies",
  searchPlaceholder: "Search policies by name or description",
  defaultSort: { field: "name", direction: "asc" },
  searchExtra: (policy) => [policy.description ?? ""],
  columns: [
    {
      field: "name",
      label: "Policy Name",
      sort: (policy) => policy.name,
      search: (policy) => policy.name,
      filter: fromSort("text"),
    },
    {
      // The resolved display name rather than the id, because that is what the row shows and
      // what an operator would pick from a list. The sort side is merged in at the call site:
      // a declared sort accessor is handed the row and nothing else, and the name lives in a
      // separate query.
      field: "organization",
      label: "Organization",
      facet: (policy, ctx) => ctx.orgName(policy.organization_id),
      facetSpec: { primary: true },
    },
    {
      field: "rules",
      label: "Rules",
      sort: (policy) => policy.rules?.length ?? 0,
      filter: fromSort("number", {
        unit: "count",
        presets: [
          { label: "None", min: null, max: 0 },
          { label: "Any", min: 1, max: null },
        ],
      }),
    },
    {
      field: "assigned",
      label: "Assigned",
      sort: (policy) => policy.assignments_count,
      filter: fromSort("number", {
        unit: "count",
        // "Which policies are minted but reaching nobody" is the question this list is worst
        // at answering by eye, and the one that finds provisioning left half-done.
        presets: [
          { label: "Unassigned", min: null, max: 0 },
          { label: "Assigned", min: 1, max: null },
        ],
      }),
    },
  ],
});

const POLICY_COLUMN_COUNT = 5;

function PolicyManagementSectionContent() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [createOpen, setCreateOpen] = useState(false);
  const [editPolicy, setEditPolicy] = useState<AccessPolicy | null>(null);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);

  // data
  const {
    data: policiesData,
    isLoading: policiesLoading,
    error: policiesError,
    refetch: refetchPolicies,
  } = useQuery({
    queryKey: ["tenant-access-policies"],
    queryFn: () => api.tenantAccess.listPolicies(),
  });

  const { data: clustersData } = useClusters();
  const { data: orgs } = useQuery({
    queryKey: ["integration-organizations"],
    queryFn: () => integrationsAPI.listOrganizations(),
  });

  // fetch user's accessible orgs (filtered by membership on backend)
  const { data: myOrgs } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.organizations.list(),
  });

  const clusters = useMemo<PolicyCluster[]>(() => {
    if (!clustersData?.clusters) return [];
    return clustersData.clusters.map((c) => ({
      cluster_id: c.cluster_id,
      display_name: c.display_name || c.cluster_id,
      endpoint: c.endpoint || "",
      status: c.status,
    }));
  }, [clustersData]);

  // filter orgs to only those the user has access to
  const myOrgIds = useMemo(
    () => new Set((myOrgs || []).map((o) => Number(o.id))),
    [myOrgs],
  );
  const organizations: IntegrationOrganization[] = useMemo(
    () => (orgs || []).filter((o) => myOrgIds.has(o.id)),
    [orgs, myOrgIds],
  );
  // note: memoised. A fresh array every render hands the filter engine a new identity each
  // time and re-runs the whole pass over rows that did not change.
  const policies: AccessPolicy[] = useMemo(
    () => policiesData?.policies ?? [],
    [policiesData],
  );

  // The name an organization id resolves to, shared by the facet and the sort so the control
  // and the column cannot end up ordering by one value and filtering by another.
  const orgName = useCallback(
    (organizationId: number) =>
      organizations.find((o) => o.id === organizationId)?.name || `ID ${organizationId}`,
    [organizations],
  );

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  const filterContext = useMemo<PolicyContext>(() => ({ orgName }), [orgName]);
  const filters = useTableFilters(POLICIES, policies, {
    context: filterContext,
    // Covers every seam at once: the search box, the Organization facet, both count ranges,
    // chip removal and Clear all. Without it a search lands on page 3 of 1 and paints nothing.
    onFilterChange: resetPage,
    // This section lives inside a settings card, not on a route of its own, so its filters
    // would outlive the screen in the address bar.
    syncUrl: false,
  });

  // The organization column sorts by the resolved name shown in the row, which is not a field
  // on a policy. Merged here, where the org list is in scope; the rest still come from POLICIES.
  const policySortAccessors = useMemo(
    () => ({
      ...POLICIES.sortAccessors,
      organization: (policy: AccessPolicy) => orgName(policy.organization_id),
    }),
    [orgName],
  );
  const {
    sortField: policySortField,
    sortDirection: policySortDirection,
    handleSort: handlePolicySort,
    sortItems: sortPolicies,
  } = useTableSort(
    policySortAccessors,
    POLICIES.defaultSort.field,
    POLICIES.defaultSort.direction,
    resetPage,
  );

  // Filter, then sort, then slice. Sorting therefore covers the whole filtered set rather than
  // the page on screen.
  const sortedPolicies = useMemo(
    () => sortPolicies(filters.filteredRows),
    [sortPolicies, filters.filteredRows],
  );
  // Clamped on read, never corrected in an effect: an effect would snap the operator back a
  // page mid-task and still paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(sortedPolicies.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedPolicies = sortedPolicies.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // mutations
  const createMutation = useMutation({
    mutationFn: (data: PolicyCreateRequest) => api.tenantAccess.createPolicy(data),
    onSuccess: (policy) => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setCreateOpen(false);
      toast({ title: "Policy created", description: `"${policy.name}" is ready to assign.` });
    },
    onError: (err: Error) => {
      toast({ title: "Could not create policy", description: err.message, variant: "destructive" });
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: PolicyUpdateRequest }) =>
      api.tenantAccess.updatePolicy(id, data),
    onSuccess: (policy) => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setEditPolicy(null);
      toast({ title: "Policy updated", description: `"${policy.name}" updated.` });
    },
    onError: (err: Error) => {
      toast({ title: "Could not update policy", description: err.message, variant: "destructive" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.tenantAccess.deletePolicy(id),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setDeleteId(null);
      toast({ title: "Policy deleted" });
    },
    onError: (err: Error) => {
      toast({ title: "Could not delete policy", description: err.message, variant: "destructive" });
    },
  });

  const handlePolicySubmit = (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => {
    if (isEdit && editPolicy) {
      updateMutation.mutate({ id: editPolicy.id, data: data as PolicyUpdateRequest });
    } else {
      createMutation.mutate(data as PolicyCreateRequest);
    }
  };

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setExpanded(!expanded)}
                  className="hover:text-primary"
                >
                  {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                </button>
                Access Policies
                <Badge variant="secondary" className="text-xs">
                  {policies.length}
                </Badge>
              </CardTitle>
              <CardDescription className="mt-1 text-xs ml-6">
                Manage reusable access policy templates. Assign them to tenant users via the Access button.
              </CardDescription>
            </div>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => setCreateOpen(true)} className="h-7 text-xs">
                <Plus className="h-3.5 w-3.5 mr-1" />
                Create Policy
              </Button>
            </div>
          </div>
        </CardHeader>

        {expanded && (
          <CardContent>
            <OrphanRulesNotice />
            {policiesLoading ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-hidden="true" />
                <span className="ml-2 text-sm text-muted-foreground">Loading policies…</span>
              </div>
            ) : policiesError && !policiesData ? (
              // A failed read is not an empty list: "No access policies yet" here once told an
              // operator to create policies that already existed.
              <ReadErrorState noun="access policies" error={policiesError} onRetry={() => void refetchPolicies()} />
            ) : policies.length === 0 ? (
              <EmptyState
                icon={ShieldCheck}
                title="No access policies yet"
                description="Access policies define what clusters, projects, and instances tenant users can access. Create your first policy to start managing tenant permissions."
                action={
                  <Button
                    variant="outline"
                    size="sm"
                    className="text-xs"
                    onClick={() => setCreateOpen(true)}
                  >
                    <Plus className="h-3.5 w-3.5 mr-1" />
                    Create Your First Policy
                  </Button>
                }
              />
            ) : (
              <>
                <TableToolbar filters={filters} noun="policies" className="mb-4" />
                {/* note: named because the User Groups table sits above this one on the same
                    screen, so "the table on this page" is ambiguous to anything reading the DOM.
                    Same device as data-slot="projects-pagination" on the project grid. */}
                <div className="rounded-md border" data-slot="policies-table">
                <Table density="compact">
                  <TableHeader>
                    <TableRow>
                      <SortableTableHead
                        label="Policy Name"
                        field="name"
                        currentField={policySortField}
                        direction={policySortDirection}
                        onSort={handlePolicySort}
                      />
                      <SortableTableHead
                        label="Organization"
                        field="organization"
                        currentField={policySortField}
                        direction={policySortDirection}
                        onSort={handlePolicySort}
                      />
                      <SortableTableHead
                        label="Rules"
                        field="rules"
                        currentField={policySortField}
                        direction={policySortDirection}
                        onSort={handlePolicySort}
                      />
                      <SortableTableHead
                        label="Assigned"
                        field="assigned"
                        currentField={policySortField}
                        direction={policySortDirection}
                        onSort={handlePolicySort}
                      />
                      <TableHead className="w-[100px]"><span className="sr-only">Actions</span></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pagedPolicies.length === 0 ? (
                      <TableEmptyRow
                        colSpan={POLICY_COLUMN_COUNT}
                        icon={ShieldCheck}
                        title="No policy matches the current filters"
                        description={`${plural(filters.activeFilterCount, "filter")} active on ${plural(policies.length, "policy", "policies")}.`}
                        action={
                          <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                            Clear filters
                          </Button>
                        }
                      />
                    ) : pagedPolicies.map((policy) => {
                      // find org name
                      const org = organizations.find(o => o.id === policy.organization_id);
                      return (
                        <TableRow key={policy.id}>
                          <TableCell>
                            <div>
                              <div className="text-sm font-medium flex items-center gap-1.5">
                                {policy.name}
                                {policy.is_default && (
                                  <span className="text-muted-foreground">(default)</span>
                                )}
                              </div>
                              {policy.description && (
                                <TableSubText className="mt-0.5 max-w-[250px]">
                                  {policy.description}
                                </TableSubText>
                              )}
                              <PolicyTermStatus policy={policy} />
                            </div>
                          </TableCell>
                          <TableCell>
                            {org?.name || `ID ${policy.organization_id}`}
                          </TableCell>
                          <TableCell>
                            {plural(policy.rules?.length || 0, "rule")}
                          </TableCell>
                          <TableCell>
                            {plural(policy.assignments_count, "user")}
                          </TableCell>
                          <TableCell>
                            <div className="flex gap-1">
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-7 w-7"
                                      onClick={() => setEditPolicy(policy)}
                                      aria-label={`Edit policy ${policy.name}`}
                                    >
                                      <Edit className="h-3.5 w-3.5" aria-hidden="true" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent side="top"><p className="text-xs">Edit</p></TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <AlertDialog
                                open={deleteId === policy.id}
                                onOpenChange={(open) => setDeleteId(open ? policy.id : null)}
                              >
                                <AlertDialogTrigger asChild>
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-7 w-7 text-destructive hover:text-destructive"
                                    aria-label={`Delete policy ${policy.name}`}
                                  >
                                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                                  </Button>
                                </AlertDialogTrigger>
                                <AlertDialogContent>
                                  <AlertDialogHeader>
                                    <AlertDialogTitle>Delete Policy</AlertDialogTitle>
                                    <AlertDialogDescription>
                                      Delete <strong>&quot;{policy.name}&quot;</strong>?
                                      {policy.assignments_count > 0 && (
                                        <> This will remove {plural(policy.assignments_count, "assignment")}.</>
                                      )}
                                      {" "}This cannot be undone.
                                    </AlertDialogDescription>
                                  </AlertDialogHeader>
                                  <AlertDialogFooter>
                                    <AlertDialogCancel disabled={deleteMutation.isPending}>Cancel</AlertDialogCancel>
                                    <AlertDialogAction
                                      // note: held open until the answer, so a refusal is read in context
                                      onClick={(event) => {
                                        event.preventDefault();
                                        deleteMutation.mutate(policy.id);
                                      }}
                                      disabled={deleteMutation.isPending}
                                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                                    >
                                      {deleteMutation.isPending && (
                                        <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" aria-hidden="true" />
                                      )}
                                      Delete
                                    </AlertDialogAction>
                                  </AlertDialogFooter>
                                </AlertDialogContent>
                              </AlertDialog>
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
                      totalItems={sortedPolicies.length}
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
        )}
      </Card>

      {/* Create Policy Dialog */}
      <PolicyEditorDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        clusters={clusters}
        organizations={organizations}
        defaultPolicyHint="Auto-assign to new tenants in this org"
        onSubmit={handlePolicySubmit}
        isPending={createMutation.isPending}
      />

      {/* Edit Policy Dialog */}
      {editPolicy && (
        <PolicyEditorDialog
          open={!!editPolicy}
          onOpenChange={(open) => !open && setEditPolicy(null)}
          editPolicy={editPolicy}
          clusters={clusters}
          organizations={organizations}
          defaultPolicyHint="Auto-assign to new tenants in this org"
          onSubmit={handlePolicySubmit}
          isPending={updateMutation.isPending}
        />
      )}
    </>
  );
}

/**
 * The boundary the filter hook requires.
 *
 * useTableFilters reads the query string even with syncUrl off, which opts a client component
 * out of prerendering, and Next then fails the build on the route unless a boundary sits above
 * it. Carrying its own means this can be dropped anywhere without the caller knowing that.
 */
export function PolicyManagementSection() {
  return (
    <Suspense fallback={<Skeleton className="h-32 rounded-xl" />}>
      <PolicyManagementSectionContent />
    </Suspense>
  );
}
