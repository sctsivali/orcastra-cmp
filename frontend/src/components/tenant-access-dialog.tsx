"use client";

import { useState, useMemo, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
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
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Loader2, Plus, Trash2, ChevronDown,
  ChevronRight, Edit, X, Check,
} from "lucide-react";
import { api, projectAPI, instanceAPI, integrationsAPI } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { queryKeys, useClusters } from "@/hooks/use-lxd";
import { readInstanceList } from "@/lib/instance-reconciliation";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useToast } from "@/hooks/use-toast";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { OverflowText } from "@/components/ui/overflow-text";
import type {
  AccessPolicy, PolicyRule, PolicyCreateRequest, PolicyUpdateRequest,
  PolicyAssignment, AssignmentCreateRequest, TenantPermission,
  IntegrationOrganization,
} from "@/types";
import {
  TENANT_PERMISSION_LABELS, TENANT_PERMISSION_DESCRIPTIONS,
  ALL_TENANT_PERMISSIONS,
} from "@/types";

// filter out snapshot (not ready yet)
const VISIBLE_PERMISSIONS = ALL_TENANT_PERMISSIONS.filter((p) => p !== "snapshot");

// ============================================
// Sub-components
// ============================================

/** render permission badges (compact, no icons) */
function PermissionBadges({ permissions }: { permissions: string[] }) {
  return (
    <div className="flex gap-1 flex-wrap">
      {(permissions as TenantPermission[])
        .filter((p) => p !== "snapshot")
        .map((p) => (
          <Badge key={p} variant="secondary" className="px-2 py-0">
            {TENANT_PERMISSION_LABELS[p].split(" ")[0]}
          </Badge>
        ))}
    </div>
  );
}

/** Inline permission checkbox group */
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

/** Single rule editor with project/instance dropdowns */
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
    staleTime: 60_000,
  });

  // fetch instances for this cluster (through the pending-delete filter: this key is the
  // per-host instance list other pages read too)
  const instanceQueryClient = useQueryClient();
  const { data: instancesData, isLoading: instancesLoading } = useQuery({
    queryKey: queryKeys.instancesFor(scopeKey, hostId),
    queryFn: () => readInstanceList(instanceQueryClient, scopeKey, () => instanceAPI.list(hostId)),
    enabled: isAuthenticated && !!hostId,
    staleTime: 60_000,
  });

  const projects = projectsData?.projects || [];

  // filter instances by selected project if one is chosen
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
// Inline Policy Editor (nested dialog)
// ============================================

function InlinePolicyEditor({
  open, onOpenChange, editPolicy, clusters, organizations, orgIdFilter,
  onSubmit, isPending,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editPolicy?: AccessPolicy | null;
  clusters: Array<{ cluster_id: string; display_name: string; endpoint: string; status?: ClusterStatus }>;
  organizations: IntegrationOrganization[];
  orgIdFilter?: number;
  onSubmit: (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => void;
  isPending: boolean;
}) {
  const isEdit = !!editPolicy;

  const [name, setName] = useState(editPolicy?.name || "");
  const [description, setDescription] = useState(editPolicy?.description || "");
  const [orgId, setOrgId] = useState<string>(
    editPolicy ? String(editPolicy.organization_id)
    : orgIdFilter ? String(orgIdFilter)
    : ""
  );
  const [isDefault, setIsDefault] = useState(editPolicy?.is_default || false);
  const [rules, setRules] = useState<Omit<PolicyRule, "id">[]>(
    editPolicy?.rules?.map(({ cluster_id, project_name, instance_name, permissions }) => ({
      cluster_id, project_name, instance_name, permissions,
    })) || [{ cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]
  );

  // reset when dialog opens
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
      setOrgId(orgIdFilter ? String(orgIdFilter) : "");
      setIsDefault(false);
      setRules([{ cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]);
    }
    onOpenChange(v);
  };

  const addRule = () => {
    setRules([...rules, { cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]);
  };

  const updateRule = (index: number, rule: Omit<PolicyRule, "id">) => {
    const updated = [...rules];
    updated[index] = rule;
    setRules(updated);
  };

  const removeRule = (index: number) => {
    if (rules.length > 1) setRules(rules.filter((_, i) => i !== index));
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
              <Label htmlFor="inline-policy-name" className="text-xs">Policy Name</Label>
              <Input
                id="inline-policy-name"
                placeholder="e.g. Production Read-Only"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
                className="h-8 text-sm"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="inline-policy-desc" className="text-xs">Description (optional)</Label>
              <Textarea
                id="inline-policy-desc"
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
                <p className="text-[10px] text-muted-foreground">Auto-assign to new tenants</p>
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
  const [deletePolicyId, setDeletePolicyId] = useState<number | null>(null);

  // ── Data fetching ──

  // assignments for THIS user
  const {
    data: assignmentsData,
    isLoading: assignmentsLoading,
  } = useQuery({
    queryKey: ["tenant-access-assignments", userEmail],
    queryFn: () => api.tenantAccess.listAssignments(undefined, userEmail),
    enabled: open,
  });

  // all policies (for the orgs the user belongs to)
  const {
    data: policiesData,
    isLoading: policiesLoading,
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
    const rules: Array<{
      cluster_id: string;
      project_name: string;
      instance_name: string;
      permissions: TenantPermission[];
      policy_name: string;
    }> = [];
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
      toast({ title: "Policy Assigned", description: `Access policy assigned to ${userEmail}.` });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (id: number) => api.tenantAccess.deleteAssignment(id),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setRevokeId(null);
      toast({ title: "Access Revoked", description: `Policy revoked from ${userEmail}.` });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const createPolicyMutation = useMutation({
    mutationFn: (data: PolicyCreateRequest) => api.tenantAccess.createPolicy(data),
    onSuccess: (policy) => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setCreatePolicyOpen(false);
      toast({ title: "Policy Created", description: `"${policy.name}" is ready to assign.` });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const updatePolicyMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: PolicyUpdateRequest }) =>
      api.tenantAccess.updatePolicy(id, data),
    onSuccess: (policy) => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setEditPolicyObj(null);
      toast({ title: "Policy Updated", description: `"${policy.name}" updated.` });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const deletePolicyMutation = useMutation({
    mutationFn: (id: number) => api.tenantAccess.deletePolicy(id),
    onSuccess: () => {
      void reconcileMutationImpact(queryClient, "tenant-access.write").catch(() => undefined);
      setDeletePolicyId(null);
      toast({ title: "Policy Deleted" });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
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
            {!isLoading && (
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
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                  <span className="ml-2 text-muted-foreground text-sm">Loading access data...</span>
                </div>
              ) : (
                <>
                  {/* ── Section 1: Assigned Policies ── */}
                  <div>
                    <div className="flex items-center justify-between mb-3">
                      <h3 className="text-sm font-semibold flex items-center gap-2">
                        Assigned Policies
                        <Badge variant="secondary" className="text-[10px] px-1.5">
                          {assignments.length}
                        </Badge>
                      </h3>
                    </div>

                    {assignments.length === 0 ? (
                      <div className="text-center py-8 text-muted-foreground border rounded-lg border-dashed">
                        <p className="text-xs">No policies assigned yet</p>
                        <p className="text-[10px] mt-0.5">Assign a policy below to grant this tenant access.</p>
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
                        <Badge variant="outline" className="text-[10px] px-1.5">
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
                                  {policy.rules?.length || 0} {(policy.rules?.length || 0) === 1 ? "rule" : "rules"}
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
                                    >
                                      <Edit className="h-3.5 w-3.5" />
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
                              >
                                {assignMutation.isPending ? (
                                  <Loader2 className="h-3 w-3 animate-spin" />
                                ) : (
                                  <Plus className="h-3 w-3" />
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
      <InlinePolicyEditor
        open={createPolicyOpen}
        onOpenChange={setCreatePolicyOpen}
        clusters={clusters}
        organizations={organizations}
        orgIdFilter={userOrgIds[0]}
        onSubmit={handlePolicySubmit}
        isPending={createPolicyMutation.isPending}
      />

      {/* Nested: Edit Policy */}
      {editPolicyObj && (
        <InlinePolicyEditor
          open={!!editPolicyObj}
          onOpenChange={(open) => !open && setEditPolicyObj(null)}
          editPolicy={editPolicyObj}
          clusters={clusters}
          organizations={organizations}
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
  clusters: Array<{ cluster_id: string; display_name: string; endpoint: string; status?: ClusterStatus }>;
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
      cluster: (r: any) =>
        r.cluster_id === "*"
          ? "All Clusters"
          : clusters.find((c) => c.cluster_id === r.cluster_id)?.display_name || r.cluster_id,
      project: (r: any) => r.project_name,
      instance: (r: any) => r.instance_name,
      permissions: (r: any) => r.permissions?.length ?? 0,
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
          className="flex items-center gap-2 text-sm font-medium hover:text-primary flex-1 min-w-0 text-left"
        >
          {expanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
          <span className="truncate">{assignment.policy_name}</span>
          {policy?.is_default && (
            <Badge asChild variant="outline" className="shrink-0 px-2 py-0">
              <span>Default</span>
            </Badge>
          )}
          <Badge asChild variant="secondary" className="shrink-0 px-2 py-0">
            <span>
              {rules.length} {rules.length === 1 ? "rule" : "rules"}
            </span>
          </Badge>
        </button>
        <div className="flex items-center gap-1 ml-2 shrink-0">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onEditPolicy}>
                  <Edit className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top"><p className="text-xs">Edit policy</p></TooltipContent>
            </Tooltip>
          </TooltipProvider>
          {revokeId === assignment.id ? (
            <div className="flex items-center gap-1 bg-destructive/10 rounded-md px-2 py-0.5">
              <span className="text-[10px] text-destructive font-medium">Revoke?</span>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-destructive hover:text-destructive"
                onClick={onRevoke}
                disabled={isRevoking}
              >
                {isRevoking ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={() => onSetRevokeId(null)}
              >
                <X className="h-3 w-3" />
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
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top"><p className="text-xs">Revoke policy</p></TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
        </div>
      </div>

      {/* meta */}
      <div className="px-3 py-1.5 border-t flex items-center gap-4 text-[10px] text-muted-foreground bg-muted/10">
        <span>Organization: <strong className="text-foreground">{assignment.organization_name}</strong></span>
        <span>Assigned by: {assignment.assigned_by}</span>
        <span>{new Date(assignment.assigned_at).toLocaleDateString()}</span>
        {assignment.expires_at && (
          <span className="text-status-pending">
            Expires: {new Date(assignment.expires_at).toLocaleDateString()}
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
                    <PermissionBadges permissions={r.permissions} />
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
  rules: Array<{
    cluster_id: string;
    project_name: string;
    instance_name: string;
    permissions: TenantPermission[];
    policy_name: string;
  }>;
  clusters: Array<{ cluster_id: string; display_name: string; endpoint: string; status?: ClusterStatus }>;
}) {
  const [expanded, setExpanded] = useState(false);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);

  // effective rules sorting (cluster column sorts by the resolved display name shown in the row)
  const effectiveSortAccessors = useMemo(
    () => ({
      cluster: (r: any) =>
        r.cluster_id === "*"
          ? "All Clusters"
          : clusters.find((c) => c.cluster_id === r.cluster_id)?.display_name || r.cluster_id,
      project: (r: any) => r.project_name,
      instance: (r: any) => r.instance_name,
      permissions: (r: any) => r.permissions?.length ?? 0,
      policy: (r: any) => r.policy_name,
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
        className="flex items-center justify-between w-full mb-3"
      >
        <h3 className="text-sm font-semibold flex items-center gap-2">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          Effective Permissions
          <Badge variant="outline" className="text-[10px] px-1.5">
            {rules.length} {rules.length === 1 ? "rule" : "rules"}
          </Badge>
        </h3>
        <span className="text-[10px] text-muted-foreground">
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
                    <PermissionBadges permissions={r.permissions} />
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
