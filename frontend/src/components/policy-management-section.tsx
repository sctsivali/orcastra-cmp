"use client";

import { Suspense, useCallback, useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { clusterStatusMeta, clusterSublabel } from "@/lib/cluster-status";
import { ClusterStatus } from "@/types/api";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Loader2, Plus, Trash2, ChevronDown,
  ChevronRight, Edit, Building2, ShieldCheck,
} from "lucide-react";
import { api, projectAPI, instanceAPI, integrationsAPI } from "@/lib/api";
import { queryKeys, useClusters } from "@/hooks/use-lxd";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useToast } from "@/hooks/use-toast";
import { useTableSort } from "@/hooks/use-table-sort";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import type {
  AccessPolicy, PolicyRule, PolicyCreateRequest, PolicyUpdateRequest,
  TenantPermission, IntegrationOrganization,
} from "@/types";
import {
  TENANT_PERMISSION_LABELS, TENANT_PERMISSION_DESCRIPTIONS,
  ALL_TENANT_PERMISSIONS,
} from "@/types";

// filter out snapshot (not ready yet)
const VISIBLE_PERMISSIONS = ALL_TENANT_PERMISSIONS.filter((p) => p !== "snapshot");

// ── Permission color mapping ──
const PERM_COLORS: Record<TenantPermission, string> = {
  view: "bg-blue-500/10 text-blue-600 border-blue-500/20",
  operate: "bg-orange-500/10 text-orange-600 border-orange-500/20",
  console: "bg-purple-500/10 text-purple-600 border-purple-500/20",
  snapshot: "bg-cyan-500/10 text-cyan-600 border-cyan-500/20",
  file_transfer: "bg-green-500/10 text-green-600 border-green-500/20",
};

/** compact permission badges (no icons) */
function PermissionBadges({ permissions }: { permissions: string[] }) {
  return (
    <div className="flex gap-1 flex-wrap">
      {(permissions as TenantPermission[])
        .filter((p) => p !== "snapshot")
        .map((p) => (
          <Badge
            key={p}
            variant="outline"
            className={`text-[10px] py-0 px-1.5 ${PERM_COLORS[p]}`}
          >
            {TENANT_PERMISSION_LABELS[p].split(" ")[0]}
          </Badge>
        ))}
    </div>
  );
}

/** permission checkbox group */
function PermissionCheckboxGroup({
  selected,
  onChange,
}: {
  selected: TenantPermission[];
  onChange: (perms: TenantPermission[]) => void;
}) {
  const toggle = (perm: TenantPermission) => {
    if (selected.includes(perm)) {
      if (selected.length > 1) {
        onChange(selected.filter((p) => p !== perm));
      }
    } else {
      onChange([...selected, perm]);
    }
  };

  return (
    <div className="space-y-1.5">
      {VISIBLE_PERMISSIONS.map((perm) => (
        <label
          key={perm}
          className="flex items-start gap-2.5 p-1.5 rounded-md hover:bg-muted/50 cursor-pointer"
        >
          <Checkbox
            checked={selected.includes(perm)}
            onCheckedChange={() => toggle(perm)}
            className="mt-0.5"
          />
          <div>
            <div className="text-xs font-medium">
              {TENANT_PERMISSION_LABELS[perm]}
            </div>
            <p className="text-[10px] text-muted-foreground leading-tight">
              {TENANT_PERMISSION_DESCRIPTIONS[perm]}
            </p>
          </div>
        </label>
      ))}
    </div>
  );
}

/** single rule editor with project/instance dropdowns */
function RuleEditor({
  rule, index, clusters, onUpdate, onRemove,
}: {
  rule: Omit<PolicyRule, "id">;
  index: number;
  clusters: Array<{ cluster_id: string; display_name: string; endpoint: string; status?: ClusterStatus }>;
  onUpdate: (index: number, rule: Omit<PolicyRule, "id">) => void;
  onRemove: (index: number) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const { scopeKey, isAuthenticated } = useActorQueryScope();

  // find the selected cluster for display & data fetching
  const selectedCluster = clusters.find((c) => c.cluster_id === rule.cluster_id);
  const hostId = rule.cluster_id !== "*" ? rule.cluster_id : "";

  // fetch projects for this cluster (API expects cluster_id as host)
  const { data: projectsData, isLoading: projectsLoading } = useQuery({
    queryKey: queryKeys.projectsFor(scopeKey, hostId),
    queryFn: () => projectAPI.list(hostId),
    enabled: isAuthenticated && !!hostId,
    staleTime: 60_000, // cache for 60s to avoid refetch on re-expand
  });

  // fetch instances for this cluster
  const { data: instancesData, isLoading: instancesLoading } = useQuery({
    queryKey: queryKeys.instancesFor(scopeKey, hostId),
    queryFn: () => instanceAPI.list(hostId),
    enabled: isAuthenticated && !!hostId,
    staleTime: 60_000,
  });

  const projects = projectsData?.projects || [];

  // filter instances by selected project
  const instances = useMemo(() => {
    const allInstances = instancesData?.vms || [];
    if (rule.project_name && rule.project_name !== "*") {
      return allInstances.filter((i: any) => i.project === rule.project_name);
    }
    return allInstances;
  }, [instancesData, rule.project_name]);

  return (
    <div className="border rounded-lg p-2.5 space-y-2 bg-muted/30">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1.5 text-xs font-medium hover:text-primary"
        >
          {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          Rule {index + 1}
          <span className="text-muted-foreground font-normal">
            - {rule.cluster_id === "*" ? "All Clusters" : clusters.find(c => c.cluster_id === rule.cluster_id)?.display_name || rule.cluster_id}
            {rule.project_name !== "*" && ` / ${rule.project_name}`}
            {rule.instance_name !== "*" && ` / ${rule.instance_name}`}
          </span>
        </button>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-destructive hover:text-destructive"
          onClick={() => onRemove(index)}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>

      {expanded && (
        <div className="grid gap-2.5 pl-5">
          <div className="grid grid-cols-3 gap-2">
            <div className="space-y-1">
              <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Cluster</Label>
              <SearchableSelect
                value={rule.cluster_id}
                onValueChange={(v) => onUpdate(index, {
                  ...rule,
                  cluster_id: v,
                  project_name: "*",
                  instance_name: "*",
                })}
                className="h-7 text-xs"
                searchPlaceholder="Search clusters..."
                options={[
                  { value: "*", label: "* (All Clusters)" },
                  // note: a scope picker, not a target picker. An offline cluster is a
                  // perfectly valid thing to write a rule about, so the status is marked but
                  // never disabled and never warned about. Disabling here would block a rule
                  // an operator is writing precisely because the cluster is down.
                  ...clusters.map((c) => {
                    const meta = clusterStatusMeta(c.status);
                    return {
                      value: c.cluster_id,
                      label: c.display_name || c.cluster_id,
                      sublabel: clusterSublabel(c.cluster_id, c.display_name, c.endpoint),
                      status: {
                        label: meta.label,
                        dotClassName: meta.dotClassName,
                        tone: meta.tone,
                      },
                    };
                  }),
                ]}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Project</Label>
              <Select
                value={rule.project_name}
                onValueChange={(v) => onUpdate(index, { ...rule, project_name: v, instance_name: "*" })}
                disabled={rule.cluster_id === "*"}
              >
                <SelectTrigger className="h-7 text-xs">
                  <SelectValue placeholder={projectsLoading ? "Loading..." : "* (all)"} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="*">* (All Projects)</SelectItem>
                  {projectsLoading ? (
                    <div className="flex items-center justify-center py-2">
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                      <span className="ml-1.5 text-xs text-muted-foreground">Loading projects...</span>
                    </div>
                  ) : (
                    projects.map((p: any) => (
                      <SelectItem key={p.name} value={p.name}>{p.name}</SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Instance</Label>
              <Select
                value={rule.instance_name}
                onValueChange={(v) => onUpdate(index, { ...rule, instance_name: v })}
                disabled={rule.cluster_id === "*"}
              >
                <SelectTrigger className="h-7 text-xs">
                  <SelectValue placeholder={instancesLoading ? "Loading..." : "* (all)"} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="*">* (All Instances)</SelectItem>
                  {instancesLoading ? (
                    <div className="flex items-center justify-center py-2">
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                      <span className="ml-1.5 text-xs text-muted-foreground">Loading instances...</span>
                    </div>
                  ) : (
                    instances.map((i: any) => (
                      <SelectItem key={i.name} value={i.name}>
                        {i.name}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Permissions</Label>
            <PermissionCheckboxGroup
              selected={rule.permissions as TenantPermission[]}
              onChange={(perms) => onUpdate(index, { ...rule, permissions: perms })}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================
// Policy Editor Dialog
// ============================================

function PolicyEditorDialog({
  open, onOpenChange, editPolicy, clusters, organizations, onSubmit, isPending,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editPolicy?: AccessPolicy | null;
  clusters: Array<{ cluster_id: string; display_name: string; endpoint: string; status?: ClusterStatus }>;
  organizations: IntegrationOrganization[];
  onSubmit: (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => void;
  isPending: boolean;
}) {
  const isEdit = !!editPolicy;

  const [name, setName] = useState(editPolicy?.name || "");
  const [description, setDescription] = useState(editPolicy?.description || "");
  const [orgId, setOrgId] = useState<string>(
    editPolicy ? String(editPolicy.organization_id) : ""
  );
  const [isDefault, setIsDefault] = useState(editPolicy?.is_default || false);
  const [rules, setRules] = useState<Omit<PolicyRule, "id">[]>(
    editPolicy?.rules?.map(({ cluster_id, project_name, instance_name, permissions }) => ({
      cluster_id, project_name, instance_name, permissions,
    })) || [{ cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]
  );

  const handleOpenChange = (v: boolean) => {
    if (v && editPolicy) {
      setName(editPolicy.name);
      setDescription(editPolicy.description || "");
      setOrgId(String(editPolicy.organization_id));
      setIsDefault(editPolicy.is_default);
      setRules(
        editPolicy.rules?.map(({ cluster_id, project_name, instance_name, permissions }) => ({
          cluster_id, project_name, instance_name, permissions,
        })) || []
      );
    } else if (v && !editPolicy) {
      setName("");
      setDescription("");
      setOrgId("");
      setIsDefault(false);
      setRules([{ cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]);
    }
    onOpenChange(v);
  };

  const addRule = () => {
    setRules([...rules, { cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]);
  };

  const updateRule = (idx: number, rule: Omit<PolicyRule, "id">) => {
    const updated = [...rules];
    updated[idx] = rule;
    setRules(updated);
  };

  const removeRule = (idx: number) => {
    if (rules.length > 1) setRules(rules.filter((_, i) => i !== idx));
  };

  const canSubmit = name.trim() && (isEdit || orgId) && rules.length > 0;

  const handleSubmit = () => {
    if (!canSubmit) return;
    if (isEdit) {
      onSubmit(
        { name: name.trim(), description: description.trim() || undefined, is_default: isDefault, rules } as PolicyUpdateRequest,
        true,
      );
    } else {
      onSubmit(
        {
          name: name.trim(),
          description: description.trim() || undefined,
          organization_id: parseInt(orgId),
          is_default: isDefault,
          rules,
        } as PolicyCreateRequest,
        false,
      );
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>
            {isEdit ? "Edit Policy" : "Create Access Policy"}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update the policy rules and settings."
              : "Define cluster, project, and instance-level access rules for tenants."}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="-mr-2 pr-2">
          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <Label htmlFor="mgmt-policy-name" className="text-xs">Policy Name</Label>
              <Input
                id="mgmt-policy-name"
                placeholder="e.g. Production Read-Only"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
                className="h-8 text-sm"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="mgmt-policy-desc" className="text-xs">Description (optional)</Label>
              <Textarea
                id="mgmt-policy-desc"
                placeholder="Describe what this policy allows..."
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={500}
                className="resize-none h-14 text-sm"
              />
            </div>

            {!isEdit && (
              <div className="space-y-1.5">
                <Label className="text-xs">
                  Organization <span className="text-destructive">(required)</span>
                </Label>
                <Select value={orgId || "__none__"} onValueChange={(v) => setOrgId(v === "__none__" ? "" : v)}>
                  <SelectTrigger className="h-8 text-sm">
                    <SelectValue placeholder="Select organization..." />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__" disabled>Select organization...</SelectItem>
                    {organizations.map((org) => (
                      <SelectItem key={org.id} value={String(org.id)}>{org.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            <div className="flex items-center gap-3">
              <Switch checked={isDefault} onCheckedChange={setIsDefault} />
              <div>
                <Label className="text-xs">Default Policy</Label>
                <p className="text-[10px] text-muted-foreground">Auto-assign to new tenants in this org</p>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-medium">Access Rules ({rules.length})</Label>
                <Button variant="outline" size="sm" onClick={addRule} className="h-6 text-[10px] px-2">
                  <Plus className="h-3 w-3 mr-1" />
                  Add Rule
                </Button>
              </div>
              <div className="space-y-2">
                {rules.map((rule, idx) => (
                  <RuleEditor
                    key={idx}
                    rule={rule}
                    index={idx}
                    clusters={clusters}
                    onUpdate={updateRule}
                    onRemove={removeRule}
                  />
                ))}
              </div>
            </div>
          </div>
        </DialogBody>

        <DialogFooter className="shrink-0 pt-3 border-t">
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button size="sm" onClick={handleSubmit} disabled={!canSubmit || isPending}>
            {isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
            {isEdit ? "Save Changes" : "Create Policy"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

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

  const clusters = useMemo(() => {
    if (!clustersData?.clusters) return [];
    return clustersData.clusters.map((c: any) => ({
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
      queryClient.invalidateQueries({ queryKey: ["tenant-access-policies"] });
      setCreateOpen(false);
      toast({ title: "Policy Created", description: `"${policy.name}" is ready to assign.` });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: PolicyUpdateRequest }) =>
      api.tenantAccess.updatePolicy(id, data),
    onSuccess: (policy) => {
      queryClient.invalidateQueries({ queryKey: ["tenant-access-policies"] });
      queryClient.invalidateQueries({ queryKey: ["tenant-access-assignments"] });
      setEditPolicy(null);
      toast({ title: "Policy Updated", description: `"${policy.name}" updated.` });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.tenantAccess.deletePolicy(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tenant-access-policies"] });
      queryClient.invalidateQueries({ queryKey: ["tenant-access-assignments"] });
      setDeleteId(null);
      toast({ title: "Policy Deleted" });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
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
            {policiesLoading ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                <span className="ml-2 text-sm text-muted-foreground">Loading policies...</span>
              </div>
            ) : policies.length === 0 ? (
              <div className="text-center py-10 text-muted-foreground">
                <ShieldCheck className="h-10 w-10 mx-auto mb-3 opacity-30" />
                <p className="text-sm font-medium">No access policies yet</p>
                <p className="text-xs mt-1 max-w-sm mx-auto">
                  Access policies define what clusters, projects, and instances tenant users can access.
                  Create your first policy to start managing tenant permissions.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-4 text-xs"
                  onClick={() => setCreateOpen(true)}
                >
                  <Plus className="h-3.5 w-3.5 mr-1" />
                  Create Your First Policy
                </Button>
              </div>
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
                      <TableHead className="w-[100px]">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pagedPolicies.length === 0 ? (
                      <TableEmptyRow
                        colSpan={POLICY_COLUMN_COUNT}
                        icon={ShieldCheck}
                        title="No policy matches the current filters"
                        description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active on ${policies.length} policies.`}
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
                            </div>
                          </TableCell>
                          <TableCell>
                            {org?.name || `ID ${policy.organization_id}`}
                          </TableCell>
                          <TableCell>
                            {policy.rules?.length || 0} {(policy.rules?.length || 0) === 1 ? "rule" : "rules"}
                          </TableCell>
                          <TableCell>
                            {policy.assignments_count} {policy.assignments_count === 1 ? "user" : "users"}
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
                                    >
                                      <Edit className="h-3.5 w-3.5" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent side="top"><p className="text-xs">Edit</p></TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <Dialog
                                open={deleteId === policy.id}
                                onOpenChange={(open) => setDeleteId(open ? policy.id : null)}
                              >
                                <DialogTrigger asChild>
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-7 w-7 text-destructive hover:text-destructive"
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </Button>
                                </DialogTrigger>
                                <DialogContent>
                                  <DialogHeader className="pb-4">
                                    <DialogTitle>Delete Policy</DialogTitle>
                                    <DialogDescription>
                                      Delete <strong>&quot;{policy.name}&quot;</strong>?
                                      {policy.assignments_count > 0 && (
                                        <> This will remove {policy.assignments_count} assignment(s).</>
                                      )}
                                      {" "}This cannot be undone.
                                    </DialogDescription>
                                  </DialogHeader>
                                  <DialogFooter>
                                    <Button variant="outline" onClick={() => setDeleteId(null)}>Cancel</Button>
                                    <Button
                                      variant="destructive"
                                      onClick={() => deleteMutation.mutate(policy.id)}
                                      disabled={deleteMutation.isPending}
                                    >
                                      {deleteMutation.isPending && (
                                        <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                                      )}
                                      Delete
                                    </Button>
                                  </DialogFooter>
                                </DialogContent>
                              </Dialog>
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
