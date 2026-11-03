"use client";

import { formatDate, plural } from "@/lib/utils";
import { useState, useMemo, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Dialog, DialogBody, DialogContent, DialogDescription,
  DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table, TableBody, TableCell, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Loader2, Plus, Trash2, ChevronDown,
  ChevronRight, Edit, X, Check,
} from "lucide-react";
import { api, integrationsAPI } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { useClusters } from "@/hooks/use-lxd";
import { useToast } from "@/hooks/use-toast";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { OverflowText } from "@/components/ui/overflow-text";
import { PolicyEditorDialog } from "@/components/access-policies/policy-editor-dialog";
import { PermissionList } from "@/components/access-policies/permission-list";
import type { PolicyCluster } from "@/components/access-policies/rule-editor";
import type {
  AccessPolicy, PolicyCreateRequest, PolicyUpdateRequest,
  PolicyAssignment, AssignmentCreateRequest, TenantPermission,
  IntegrationOrganization, PolicyRule,
} from "@/types";

/** One rule of an assigned policy, named by the policy it came from. */
type EffectiveRule = Omit<PolicyRule, "id"> & { permissions: TenantPermission[]; policy_name: string };

/** The cluster column sorts by the name the row shows, not the id. */
function clusterSortName(clusters: PolicyCluster[], clusterId: string): string {
  return clusterId === "*"
    ? "All Clusters"
    : clusters.find((c) => c.cluster_id === clusterId)?.display_name || clusterId;
}

// ============================================
// Main: Tenant Access Dialog
// ============================================

interface TenantAccessDialogProps {
  /** the tenant user email we're managing */
  userEmail: string;
  /** organization IDs the user belongs to */
  userOrgIds: number[];
  /** dialog open state */
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function TenantAccessDialog({
  userEmail, userOrgIds, open, onOpenChange,
}: TenantAccessDialogProps) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  // nested dialog state
  const [createPolicyOpen, setCreatePolicyOpen] = useState(false);
  const [editPolicyObj, setEditPolicyObj] = useState<AccessPolicy | null>(null);
  const [revokeId, setRevokeId] = useState<number | null>(null);

  // ── Data fetching ──

  // assignments for THIS user
  const {
    data: assignmentsData,
    isLoading: assignmentsLoading,
    error: assignmentsError,
    refetch: refetchAssignments,
  } = useQuery({
    queryKey: ["tenant-access-assignments", userEmail],
    queryFn: () => api.tenantAccess.listAssignments(undefined, userEmail),
    enabled: open,
  });

  // all policies (for the orgs the user belongs to)
  const {
    data: policiesData,
    isLoading: policiesLoading,
    error: policiesError,
    refetch: refetchPolicies,
  } = useQuery({
    queryKey: ["tenant-access-policies"],
    queryFn: () => api.tenantAccess.listPolicies(),
    enabled: open,
  });

  // clusters for rule builder
  const { data: clustersData } = useClusters();

  // orgs for policy editor
  const { data: orgs } = useQuery({
    queryKey: ["integration-organizations"],
    queryFn: () => integrationsAPI.listOrganizations(),
    enabled: open,
  });

  // user's accessible orgs (filtered by membership on backend)
  const { data: myOrgs } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.organizations.list(),
    enabled: open,
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
  const assignments = assignmentsData?.assignments || [];
  const allPolicies = policiesData?.policies || [];

  // policies assigned to this user
  const assignedPolicyIds = new Set(assignments.map((a) => a.policy_id));

  // available policies (in user's orgs & not already assigned)
  const availablePolicies = allPolicies.filter(
    (p) => userOrgIds.includes(p.organization_id) && !assignedPolicyIds.has(p.id),
  );

  // effective permissions: flatten all assigned policy rules
  const effectiveRules = useMemo(() => {
    const rules: EffectiveRule[] = [];
    for (const a of assignments) {
      const policy = allPolicies.find((p) => p.id === a.policy_id);
      if (policy?.rules) {
        for (const r of policy.rules) {
          rules.push({
            cluster_id: r.cluster_id,
            project_name: r.project_name,
            instance_name: r.instance_name,
            permissions: r.permissions,
            policy_name: policy.name,
          });
        }
      }
    }
    return rules;
  }, [assignments, allPolicies]);

  // count unique clusters
  const uniqueClusters = new Set(effectiveRules.map((r) => r.cluster_id));

  // ── Mutations ──

  const assignMutation = useMutation({
    mutationFn: (data: AssignmentCreateRequest) => api.tenantAccess.createAssignment(data),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      toast({ title: "Policy assigned", description: `Access policy assigned to ${userEmail}.` });
    },
    onError: (err: Error) => {
      toast({ title: "Could not assign policy", description: err.message, variant: "destructive" });
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (id: number) => api.tenantAccess.deleteAssignment(id),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setRevokeId(null);
      toast({ title: "Access revoked", description: `Policy revoked from ${userEmail}.` });
    },
    onError: (err: Error) => {
      toast({ title: "Could not revoke access", description: err.message, variant: "destructive" });
    },
  });

  const createPolicyMutation = useMutation({
    mutationFn: (data: PolicyCreateRequest) => api.tenantAccess.createPolicy(data),
    onSuccess: (policy) => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setCreatePolicyOpen(false);
      toast({ title: "Policy created", description: `"${policy.name}" is ready to assign.` });
    },
    onError: (err: Error) => {
      toast({ title: "Could not create policy", description: err.message, variant: "destructive" });
    },
  });

  const updatePolicyMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: PolicyUpdateRequest }) =>
      api.tenantAccess.updatePolicy(id, data),
    onSuccess: (policy) => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setEditPolicyObj(null);
      toast({ title: "Policy updated", description: `"${policy.name}" updated.` });
    },
    onError: (err: Error) => {
      toast({ title: "Could not update policy", description: err.message, variant: "destructive" });
    },
  });

  // ── Handlers ──

  const handleQuickAssign = (policyId: number) => {
    // find org for this policy
    const policy = allPolicies.find((p) => p.id === policyId);
    if (!policy) return;
    assignMutation.mutate({
      policy_id: policyId,
      user_email: userEmail,
      organization_id: policy.organization_id,
    });
  };

  const handlePolicySubmit = (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => {
    if (isEdit && editPolicyObj) {
      updatePolicyMutation.mutate({ id: editPolicyObj.id, data: data as PolicyUpdateRequest });
    } else {
      createPolicyMutation.mutate(data as PolicyCreateRequest);
    }
  };

  const isLoading = assignmentsLoading || policiesLoading;
  // A failed read is not an empty one: "No policies assigned yet" over a failed read invited an
  // operator to assign a second copy of what the tenant already holds.
  const readError = (assignmentsError && !assignmentsData) ? assignmentsError
    : (policiesError && !policiesData) ? policiesError
    : null;
  // note: per row. One shared isPending spun every Assign button while one request was out.
  const assigningPolicyId = assignMutation.isPending ? assignMutation.variables?.policy_id : undefined;

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-3xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden p-0">
          {/* Header */}
          <div className="px-6 pt-6 pb-4 border-b shrink-0">
            <DialogHeader>
              <DialogTitle className="text-lg">
                Manage Access
              </DialogTitle>
              <DialogDescription className="text-sm">
                Configure resource-level permissions for <strong className="text-foreground">{userEmail}</strong>
              </DialogDescription>
            </DialogHeader>

            {/* Summary stats */}
            {!isLoading && !readError && (
              <div className="flex items-center gap-4 mt-3">
                <div className="text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">{assignments.length}</span> {assignments.length === 1 ? "policy" : "policies"}
                </div>
                <div className="text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">{effectiveRules.length}</span> {effectiveRules.length === 1 ? "rule" : "rules"}
                </div>
                <div className="text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">{uniqueClusters.size}</span> {uniqueClusters.size === 1 ? "cluster" : "clusters"}
                </div>
              </div>
            )}
          </div>

          {/* Body */}
          <DialogBody className="px-6 py-4 space-y-6">
              {isLoading ? (
                <div className="flex items-center justify-center py-16">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-hidden="true" />
                  <span className="ml-2 text-muted-foreground text-sm">Loading access data…</span>
                </div>
              ) : readError ? (
                <ReadErrorState
                  noun="access policies"
                  error={readError}
                  onRetry={() => {
                    void refetchAssignments();
                    void refetchPolicies();
                  }}
                />
              ) : (
                <>
                  {/* ── Section 1: Assigned Policies ── */}
                  <div>
                    <div className="flex items-center justify-between mb-3">
                      <h3 className="text-sm font-semibold flex items-center gap-2">
                        Assigned Policies
                        <Badge variant="secondary" className="text-xs px-1.5">
                          {assignments.length}
                        </Badge>
                      </h3>
                    </div>

                    {assignments.length === 0 ? (
                      <div className="text-center py-8 text-muted-foreground border rounded-lg border-dashed">
                        <p className="text-xs">No policies assigned yet</p>
                        <p className="text-xs mt-0.5">Assign a policy below to grant this tenant access.</p>
                      </div>
                    ) : (
                      <div className="space-y-3">
                        {assignments.map((a) => {
                          const policy = allPolicies.find((p) => p.id === a.policy_id);
                          return (
                            <AssignedPolicyCard
                              key={a.id}
                              assignment={a}
                              policy={policy || null}
                              clusters={clusters}
                              revokeId={revokeId}
                              onSetRevokeId={setRevokeId}
                              onRevoke={() => revokeMutation.mutate(a.id)}
                              isRevoking={revokeMutation.isPending}
                              onEditPolicy={() => policy && setEditPolicyObj(policy)}
                            />
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* ── Section 2: Effective Permissions ── */}
                  {effectiveRules.length > 0 && (
                    <EffectivePermissionsSection rules={effectiveRules} clusters={clusters} />
                  )}

                  {/* ── Section 3: Available Policies ── */}
                  <div>
                    <div className="flex items-center justify-between mb-3">
                      <h3 className="text-sm font-semibold flex items-center gap-2">
                        Available Policies
                        <Badge variant="outline" className="text-xs px-1.5">
                          {availablePolicies.length}
                        </Badge>
                      </h3>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 text-xs"
                        onClick={() => setCreatePolicyOpen(true)}
                      >
                        <Plus className="h-3 w-3 mr-1" />
                        Create New
                      </Button>
                    </div>

                    {availablePolicies.length === 0 ? (
                      <div className="text-center py-6 text-muted-foreground border rounded-lg border-dashed">
                        <p className="text-xs">
                          {allPolicies.length === 0
                            ? "No policies created yet. Create one to get started."
                            : "All available policies are already assigned."}
                        </p>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {availablePolicies.map((policy) => (
                          <div
                            key={policy.id}
                            className="flex items-center justify-between border rounded-lg p-3 hover:bg-muted/30 transition-colors"
                          >
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <OverflowText value={policy.name} label={`policy name ${policy.id}`} className="text-sm font-medium" />
                                {policy.is_default && (
                                  <Badge variant="outline" className="px-2 py-0">
                                    Default
                                  </Badge>
                                )}
                                <Badge variant="secondary" className="px-2 py-0">
                                  {plural(policy.rules?.length || 0, "rule")}
                                </Badge>
                              </div>
                              {policy.description && (
                                <p className="mt-0.5 max-w-[400px] text-xs text-muted-foreground">
                                  <OverflowText
                                    value={policy.description}
                                    label={`description for policy ${policy.name}`}
                                    lines={2}
                                  />
                                </p>
                              )}
                            </div>
                            <div className="flex gap-1.5 ml-3">
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-7 w-7"
                                      onClick={() => setEditPolicyObj(policy)}
                                      aria-label={`Edit policy ${policy.name}`}
                                    >
                                      <Edit className="h-3.5 w-3.5" aria-hidden="true" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent side="top"><p className="text-xs">Edit policy</p></TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <Button
                                variant="default"
                                size="sm"
                                className="h-7 text-xs gap-1"
                                onClick={() => handleQuickAssign(policy.id)}
                                disabled={assignMutation.isPending}
                                aria-label={`Assign policy ${policy.name}`}
                              >
                                {assigningPolicyId === policy.id ? (
                                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                                ) : (
                                  <Plus className="h-3 w-3" aria-hidden="true" />
                                )}
                                Assign
                              </Button>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
          </DialogBody>

          {/* Footer */}
          <div className="px-6 py-3 border-t flex justify-end shrink-0">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Nested: Create Policy */}
      <PolicyEditorDialog
        open={createPolicyOpen}
        onOpenChange={setCreatePolicyOpen}
        clusters={clusters}
        organizations={organizations}
        defaultOrganizationId={userOrgIds[0]}
        defaultPolicyHint="Auto-assign to new tenants"
        onSubmit={handlePolicySubmit}
        isPending={createPolicyMutation.isPending}
      />

      {/* Nested: Edit Policy */}
      {editPolicyObj && (
        <PolicyEditorDialog
          open={!!editPolicyObj}
          onOpenChange={(open) => !open && setEditPolicyObj(null)}
          editPolicy={editPolicyObj}
          clusters={clusters}
          organizations={organizations}
          defaultPolicyHint="Auto-assign to new tenants"
          onSubmit={handlePolicySubmit}
          isPending={updatePolicyMutation.isPending}
        />
      )}
    </>
  );
}

// ============================================
// Assigned Policy Card
// ============================================

function AssignedPolicyCard({
  assignment, policy, clusters, revokeId, onSetRevokeId, onRevoke, isRevoking, onEditPolicy,
}: {
  assignment: PolicyAssignment;
  policy: AccessPolicy | null;
  clusters: PolicyCluster[];
  revokeId: number | null;
  onSetRevokeId: (id: number | null) => void;
  onRevoke: () => void;
  isRevoking: boolean;
  onEditPolicy: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  // note: memoised because it feeds the sort and the page slice below. A fresh array on every
  // render would re-sort the whole rule set each time the parent re-renders.
  const rules = useMemo(() => policy?.rules || [], [policy]);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  // rule table sorting (cluster column sorts by the resolved display name shown in the row)
  const ruleSortAccessors = useMemo(
    () => ({
      cluster: (r: PolicyRule) => clusterSortName(clusters, r.cluster_id),
      project: (r: PolicyRule) => r.project_name,
      instance: (r: PolicyRule) => r.instance_name,
      permissions: (r: PolicyRule) => r.permissions?.length ?? 0,
    }),
    [clusters],
  );
  const {
    sortField: ruleSortField,
    sortDirection: ruleSortDirection,
    handleSort: handleRuleSort,
    sortItems: sortRules,
  } = useTableSort(ruleSortAccessors, "cluster", "asc", resetPage);

  // Sort the whole rule set, then take the page from it.
  const sortedRules = useMemo(() => sortRules(rules), [sortRules, rules]);
  // note: clamped on read rather than reset in an effect, so revoking a policy cannot leave
  // this card sitting on a page that no longer exists and painting nothing.
  const totalPages = Math.max(1, Math.ceil(sortedRules.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedRules = sortedRules.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <div className="border rounded-lg overflow-hidden">
      {/* header */}
      <div className="flex items-center justify-between p-3 bg-muted/20">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          // note: the clipped policy name below is still the whole string in the DOM, so this
          // control already announces it in full. The label states the action as well, because
          // "policy-name, button" does not say what the button does.
          aria-label={`${expanded ? "Hide" : "Show"} the rules in policy ${assignment.policy_name}`}
          className="flex items-center gap-2 rounded-sm text-sm font-medium flex-1 min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {expanded
            ? <ChevronDown className="h-4 w-4 shrink-0" aria-hidden="true" />
            : <ChevronRight className="h-4 w-4 shrink-0" aria-hidden="true" />}
          <span className="truncate">{assignment.policy_name}</span>
          {policy?.is_default && (
            <Badge asChild variant="outline" className="shrink-0 px-2 py-0">
              <span>Default</span>
            </Badge>
          )}
          <Badge asChild variant="secondary" className="shrink-0 px-2 py-0">
            <span>
              {plural(rules.length, "rule")}
            </span>
          </Badge>
        </button>
        <div className="flex items-center gap-1 ml-2 shrink-0">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={onEditPolicy}
                  aria-label={`Edit policy ${assignment.policy_name}`}
                >
                  <Edit className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top"><p className="text-xs">Edit policy</p></TooltipContent>
            </Tooltip>
          </TooltipProvider>
          {revokeId === assignment.id ? (
            <div className="flex items-center gap-1 bg-destructive/10 rounded-md px-2 py-0.5">
              <span className="text-xs text-destructive font-medium">Revoke?</span>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-destructive hover:text-destructive"
                onClick={onRevoke}
                disabled={isRevoking}
                aria-label={`Revoke policy ${assignment.policy_name}`}
              >
                {isRevoking
                  ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                  : <Check className="h-3 w-3" aria-hidden="true" />}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={() => onSetRevokeId(null)}
                disabled={isRevoking}
                aria-label="Keep this policy"
              >
                <X className="h-3 w-3" aria-hidden="true" />
              </Button>
            </div>
          ) : (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-destructive hover:text-destructive"
                    onClick={() => onSetRevokeId(assignment.id)}
                    aria-label={`Revoke policy ${assignment.policy_name}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top"><p className="text-xs">Revoke policy</p></TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
        </div>
      </div>

      {/* meta */}
      <div className="px-3 py-1.5 border-t flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground bg-muted/10">
        <span>Organization: <strong className="text-foreground">{assignment.organization_name}</strong></span>
        <span>Assigned by: {assignment.assigned_by}</span>
        <span>{formatDate(assignment.assigned_at)}</span>
        {assignment.expires_at && (
          <span className="text-status-pending">
            Expires: {formatDate(assignment.expires_at)}
          </span>
        )}
      </div>

      {/* expanded rules */}
      {expanded && rules.length > 0 && (
        <div className="border-t">
          <Table density="compact">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                {/* node-link: exempt an editable rule scope inside a form dialog; navigating to Clusters would discard unsaved rules */}
                <SortableTableHead
                  label="Cluster"
                  field="cluster"
                  currentField={ruleSortField}
                  direction={ruleSortDirection}
                  onSort={handleRuleSort}
                />
                <SortableTableHead
                  label="Project"
                  field="project"
                  currentField={ruleSortField}
                  direction={ruleSortDirection}
                  onSort={handleRuleSort}
                />
                <SortableTableHead
                  label="Instance"
                  field="instance"
                  currentField={ruleSortField}
                  direction={ruleSortDirection}
                  onSort={handleRuleSort}
                />
                <SortableTableHead
                  label="Permissions"
                  field="permissions"
                  currentField={ruleSortField}
                  direction={ruleSortDirection}
                  onSort={handleRuleSort}
                />
              </TableRow>
            </TableHeader>
            <TableBody>
              {pagedRules.map((r, idx) => (
                <TableRow key={idx} className="hover:bg-muted/30">
                  <TableCell>
                    {r.cluster_id === "*" ? (
                      <span className="text-muted-foreground ">All Clusters</span>
                    ) : (
                      clusters.find(c => c.cluster_id === r.cluster_id)?.display_name || r.cluster_id
                    )}
                  </TableCell>
                  <TableCell>
                    {r.project_name === "*" ? <span className="text-muted-foreground ">All</span> : r.project_name}
                  </TableCell>
                  <TableCell>
                    {r.instance_name === "*" ? <span className="text-muted-foreground ">All</span> : r.instance_name}
                  </TableCell>
                  <TableCell>
                    <PermissionList permissions={r.permissions} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {/* Absent rather than disabled when everything fits on one page. */}
          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                pageSizeOptions={[...PANEL_PAGE_SIZES]}
                totalItems={sortedRules.length}
                onPageChange={setPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setPage(1);
                }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================
// Effective Permissions Section
// ============================================

function EffectivePermissionsSection({
  rules,
  clusters,
}: {
  rules: EffectiveRule[];
  clusters: PolicyCluster[];
}) {
  const [expanded, setExpanded] = useState(false);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  // effective rules sorting (cluster column sorts by the resolved display name shown in the row)
  const effectiveSortAccessors = useMemo(
    () => ({
      cluster: (r: EffectiveRule) => clusterSortName(clusters, r.cluster_id),
      project: (r: EffectiveRule) => r.project_name,
      instance: (r: EffectiveRule) => r.instance_name,
      permissions: (r: EffectiveRule) => r.permissions?.length ?? 0,
      policy: (r: EffectiveRule) => r.policy_name,
    }),
    [clusters],
  );
  const {
    sortField: effectiveSortField,
    sortDirection: effectiveSortDirection,
    handleSort: handleEffectiveSort,
    sortItems: sortEffectiveRules,
  } = useTableSort(effectiveSortAccessors, "cluster", "asc", resetPage);

  // Sort the whole aggregated set, then take the page from it.
  const sortedRules = useMemo(() => sortEffectiveRules(rules), [sortEffectiveRules, rules]);
  // note: clamped on read rather than reset in an effect, so a policy revoked while this
  // section is open cannot strand the operator on a page past the end.
  const totalPages = Math.max(1, Math.ceil(sortedRules.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedRules = sortedRules.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
        className="flex flex-wrap items-center justify-between gap-2 w-full mb-3 rounded-sm text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="text-sm font-semibold flex items-center gap-2">
          {expanded
            ? <ChevronDown className="h-4 w-4" aria-hidden="true" />
            : <ChevronRight className="h-4 w-4" aria-hidden="true" />}
          Effective Permissions
          <Badge asChild variant="outline" className="text-xs px-1.5">
            <span>{plural(rules.length, "rule")}</span>
          </Badge>
        </span>
        <span className="text-xs text-muted-foreground">
          Aggregated view of all active policy rules
        </span>
      </button>

      {expanded && (
        <div className="rounded-lg border overflow-hidden">
          <Table density="compact">
            <TableHeader>
              <TableRow className="hover:bg-transparent bg-muted/30">
                {/* node-link: exempt a read-back of the same rule scope inside the form dialog, same reason as the editable table above */}
                <SortableTableHead
                  label="Cluster"
                  field="cluster"
                  currentField={effectiveSortField}
                  direction={effectiveSortDirection}
                  onSort={handleEffectiveSort}
                />
                <SortableTableHead
                  label="Project"
                  field="project"
                  currentField={effectiveSortField}
                  direction={effectiveSortDirection}
                  onSort={handleEffectiveSort}
                />
                <SortableTableHead
                  label="Instance"
                  field="instance"
                  currentField={effectiveSortField}
                  direction={effectiveSortDirection}
                  onSort={handleEffectiveSort}
                />
                <SortableTableHead
                  label="Permissions"
                  field="permissions"
                  currentField={effectiveSortField}
                  direction={effectiveSortDirection}
                  onSort={handleEffectiveSort}
                />
                <SortableTableHead
                  label="Source Policy"
                  field="policy"
                  currentField={effectiveSortField}
                  direction={effectiveSortDirection}
                  onSort={handleEffectiveSort}
                />
              </TableRow>
            </TableHeader>
            <TableBody>
              {pagedRules.map((r, idx) => (
                <TableRow key={idx} className="hover:bg-muted/20">
                  <TableCell className="font-medium">
                    {r.cluster_id === "*" ? (
                      <span className="text-muted-foreground ">All Clusters</span>
                    ) : (
                      clusters.find(c => c.cluster_id === r.cluster_id)?.display_name || r.cluster_id
                    )}
                  </TableCell>
                  <TableCell>
                    {r.project_name === "*" ? <span className="text-muted-foreground ">All</span> : r.project_name}
                  </TableCell>
                  <TableCell>
                    {r.instance_name === "*" ? <span className="text-muted-foreground ">All</span> : r.instance_name}
                  </TableCell>
                  <TableCell>
                    <PermissionList permissions={r.permissions} />
                  </TableCell>
                  <TableCell>
                    {r.policy_name}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {/* Absent rather than disabled when everything fits on one page. */}
          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                pageSizeOptions={[...PANEL_PAGE_SIZES]}
                totalItems={sortedRules.length}
                onPageChange={setPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setPage(1);
                }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
