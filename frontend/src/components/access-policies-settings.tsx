"use client";

import { useState, useMemo } from "react";
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
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertCircle, Loader2, Plus, RefreshCw, Trash2, Shield,
  ShieldCheck, Eye, Terminal, Camera, Upload, Power,
  Building2, ChevronDown, ChevronRight, Edit, Copy,
} from "lucide-react";
import { api, integrationsAPI } from "@/lib/api";
import { useClusters } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { useToast } from "@/hooks/use-toast";
import type {
  AccessPolicy, PolicyRule, PolicyCreateRequest, PolicyUpdateRequest,
  PolicyListResponse, PolicyAssignment, AssignmentListResponse,
  AssignmentCreateRequest, MyAccessResponse, TenantPermission,
  IntegrationOrganization, UserGroupEntry,
} from "@/types";
import {
  TENANT_PERMISSION_LABELS, TENANT_PERMISSION_DESCRIPTIONS,
  ALL_TENANT_PERMISSIONS,
} from "@/types";

// permission icon mapping
const PERM_ICONS: Record<TenantPermission, React.ReactNode> = {
  view: <Eye className="h-3.5 w-3.5" />,
  operate: <Power className="h-3.5 w-3.5" />,
  console: <Terminal className="h-3.5 w-3.5" />,
  snapshot: <Camera className="h-3.5 w-3.5" />,
  file_transfer: <Upload className="h-3.5 w-3.5" />,
};

// ============================================
// Sub-components
// ============================================

/** render permission badges */
function PermissionBadges({ permissions }: { permissions: string[] }) {
  return (
    <div className="flex gap-1 flex-wrap">
      {(permissions as TenantPermission[]).map((p) => (
        <Badge key={p} variant="outline" className="text-[10px] gap-1 py-0.5 px-1.5">
          {PERM_ICONS[p]}
          {TENANT_PERMISSION_LABELS[p]}
        </Badge>
      ))}
    </div>
  );
}

/** Permission checkbox group */
function PermissionCheckboxGroup({
  selected,
  onChange,
}: {
  selected: TenantPermission[];
  onChange: (perms: TenantPermission[]) => void;
}) {
  const toggle = (perm: TenantPermission) => {
    if (selected.includes(perm)) {
      // don't allow removing last permission
      if (selected.length > 1) {
        onChange(selected.filter((p) => p !== perm));
      }
    } else {
      onChange([...selected, perm]);
    }
  };

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
      {ALL_TENANT_PERMISSIONS.map((perm) => (
        <label
          key={perm}
          className="flex items-center gap-2 p-1.5 rounded-md hover:bg-muted/50 cursor-pointer"
        >
          <Checkbox
            checked={selected.includes(perm)}
            onCheckedChange={() => toggle(perm)}
          />
          <div className="flex items-center gap-1.5 text-xs font-medium">
            {PERM_ICONS[perm]}
            {TENANT_PERMISSION_LABELS[perm]}
          </div>
        </label>
      ))}
    </div>
  );
}

/** Single rule editor row */
interface RuleEditorProps {
  rule: Omit<PolicyRule, "id">;
  index: number;
  clusters: Array<{ cluster_id: string; display_name: string }>;
  onUpdate: (index: number, rule: Omit<PolicyRule, "id">) => void;
  onRemove: (index: number) => void;
  readOnlyPolicy?: boolean;
}

function RuleEditor({ rule, index, clusters, onUpdate, onRemove, readOnlyPolicy }: RuleEditorProps) {
  const [expanded, setExpanded] = useState(true);

  return (
    <div className="border rounded-lg p-3 space-y-3 bg-card">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-2 text-sm font-medium hover:text-primary"
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
          Rule {index + 1}
          <span className="text-muted-foreground font-normal">
            — {rule.cluster_id === "*" ? "All Clusters" : clusters.find((c) => c.cluster_id === rule.cluster_id)?.display_name || rule.cluster_id}
            {rule.project_name !== "*" && ` / ${rule.project_name}`}
            {rule.instance_name !== "*" && ` / ${rule.instance_name}`}
          </span>
        </button>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-destructive hover:text-destructive"
          onClick={() => onRemove(index)}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>

      {expanded && (
        <div className="grid gap-3 pl-6">
          {/* Cluster selector */}
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1.5">
              <Label className="text-xs">Cluster</Label>
              <Select
                value={rule.cluster_id}
                onValueChange={(v) => onUpdate(index, { ...rule, cluster_id: v })}
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="*">* (All Clusters)</SelectItem>
                  {clusters.map((c) => (
                    <SelectItem key={c.cluster_id} value={c.cluster_id}>
                      {c.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Project</Label>
              <Input
                className="h-8 text-xs"
                placeholder="* (all projects)"
                value={rule.project_name}
                onChange={(e) =>
                  onUpdate(index, { ...rule, project_name: e.target.value || "*" })
                }
              />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Instance</Label>
              <Input
                className="h-8 text-xs"
                placeholder="* (all instances)"
                value={rule.instance_name}
                onChange={(e) =>
                  onUpdate(index, { ...rule, instance_name: e.target.value || "*" })
                }
              />
            </div>
          </div>

          {/* Permissions */}
          <div className="space-y-1.5">
            <Label className="text-xs">Permissions</Label>
            {readOnlyPolicy ? (
              <div className="flex items-center gap-2 p-2 bg-muted/50 rounded-md">
                <Eye className="h-3.5 w-3.5 text-muted-foreground" />
                <span className="text-xs text-muted-foreground">View only (read-only policy)</span>
              </div>
            ) : (
              <ScrollArea className="max-h-[140px]">
                <PermissionCheckboxGroup
                  selected={rule.permissions as TenantPermission[]}
                  onChange={(perms) => onUpdate(index, { ...rule, permissions: perms })}
                />
              </ScrollArea>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================
// Policy Editor Dialog
// ============================================

interface PolicyEditorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editPolicy?: AccessPolicy | null;
  clusters: Array<{ cluster_id: string; display_name: string }>;
  organizations: IntegrationOrganization[];
  onSubmit: (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => void;
  isPending: boolean;
}

function PolicyEditorDialog({
  open, onOpenChange, editPolicy, clusters, organizations, onSubmit, isPending,
}: PolicyEditorDialogProps) {
  const isEdit = !!editPolicy;

  // form state
  const [name, setName] = useState(editPolicy?.name || "");
  const [description, setDescription] = useState(editPolicy?.description || "");
  const [orgId, setOrgId] = useState<string>(
    editPolicy ? String(editPolicy.organization_id) : ""
  );
  const [isDefault, setIsDefault] = useState(editPolicy?.is_default || false);
  const [rules, setRules] = useState<Omit<PolicyRule, "id">[]>(
    editPolicy?.rules?.map(({ cluster_id, project_name, instance_name, permissions }) => ({
      cluster_id,
      project_name,
      instance_name,
      permissions,
    })) || [{ cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]
  );
  const [isReadOnlyPolicy, setIsReadOnlyPolicy] = useState(() => {
    const initRules = editPolicy?.rules || [];
    return initRules.length > 0 && initRules.every(r => 
      r.permissions.length === 1 && r.permissions[0] === "view"
    );
  });

  // When read-only toggle changes, update all rules
  const handleReadOnlyToggle = (enabled: boolean) => {
    setIsReadOnlyPolicy(enabled);
    if (enabled) {
      setRules(rules.map(r => ({ ...r, permissions: ["view"] })));
    }
  };

  // reset when dialog opens with new data
  const handleOpenChange = (v: boolean) => {
    if (v && editPolicy) {
      setName(editPolicy.name);
      setDescription(editPolicy.description || "");
      setOrgId(String(editPolicy.organization_id));
      setIsDefault(editPolicy.is_default);
      const editRules = editPolicy.rules?.map(({ cluster_id, project_name, instance_name, permissions }) => ({
          cluster_id, project_name, instance_name, permissions,
        })) || [];
      setRules(editRules);
      setIsReadOnlyPolicy(editRules.length > 0 && editRules.every(r =>
        r.permissions.length === 1 && r.permissions[0] === "view"
      ));
    } else if (v && !editPolicy) {
      setName("");
      setDescription("");
      setOrgId("");
      setIsDefault(false);
      setIsReadOnlyPolicy(false);
      setRules([{ cluster_id: "*", project_name: "*", instance_name: "*", permissions: ["view"] }]);
    }
    onOpenChange(v);
  };

  const addRule = () => {
    const perms: TenantPermission[] = isReadOnlyPolicy ? ["view"] : ["view"];
    setRules([...rules, { cluster_id: "*", project_name: "*", instance_name: "*", permissions: perms }]);
  };

  const updateRule = (index: number, rule: Omit<PolicyRule, "id">) => {
    const updated = [...rules];
    // enforce view-only when read-only mode is on
    if (isReadOnlyPolicy) {
      rule = { ...rule, permissions: ["view"] };
    }
    updated[index] = rule;
    setRules(updated);
  };

  const removeRule = (index: number) => {
    if (rules.length > 1) {
      setRules(rules.filter((_, i) => i !== index));
    }
  };

  const canSubmit = name.trim() && (isEdit || orgId) && rules.length > 0;

  const handleSubmit = () => {
    if (!canSubmit) return;
    if (isEdit) {
      onSubmit(
        { name: name.trim(), description: description.trim() || undefined, is_default: isDefault, rules } as PolicyUpdateRequest,
        true
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
        false
      );
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit Policy" : "Create Access Policy"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update the policy rules and settings."
              : "Define what clusters, projects, and instances tenants can access."}
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className="flex-1 pr-4 -mr-4">
          <div className="space-y-4 py-2">
            {/* Name */}
            <div className="space-y-2">
              <Label htmlFor="policy-name">Policy Name</Label>
              <Input
                id="policy-name"
                placeholder="e.g. Production Read-Only"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
              />
            </div>

            {/* Description */}
            <div className="space-y-2">
              <Label htmlFor="policy-desc">Description (optional)</Label>
              <Textarea
                id="policy-desc"
                placeholder="Describe what this policy allows..."
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={500}
                className="resize-none h-16"
              />
            </div>

            {/* Organization (only for create) */}
            {!isEdit && (
              <div className="space-y-2">
                <Label>
                  Organization <span className="text-destructive">(required)</span>
                </Label>
                <Select value={orgId || "__none__"} onValueChange={(v) => setOrgId(v === "__none__" ? "" : v)}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select organization..." />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__" disabled>
                      Select organization...
                    </SelectItem>
                    {organizations.map((org) => (
                      <SelectItem key={org.id} value={String(org.id)}>
                        {org.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Default */}
            <div className="flex items-center gap-3">
              <Switch checked={isDefault} onCheckedChange={setIsDefault} />
              <div>
                <Label className="text-sm">Default Policy</Label>
                <p className="text-xs text-muted-foreground">
                  Auto-assign to new tenants in this organization
                </p>
              </div>
            </div>

            {/* Read-Only Mode */}
            <div className="flex items-center gap-3 p-3 bg-muted/50 rounded-lg border">
              <Switch checked={isReadOnlyPolicy} onCheckedChange={handleReadOnlyToggle} />
              <div>
                <Label className="text-sm flex items-center gap-1.5">
                  <Eye className="h-3.5 w-3.5" />
                  Read-Only Policy
                </Label>
                <p className="text-xs text-muted-foreground">
                  Restrict to view-only access — no operate, console, or file transfer
                </p>
              </div>
            </div>

            {/* Rules */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label className="text-sm font-medium">
                  Access Rules ({rules.length})
                </Label>
                <Button variant="outline" size="sm" onClick={addRule} className="h-7 text-xs">
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
                    readOnlyPolicy={isReadOnlyPolicy}
                  />
                ))}
              </div>
            </div>
          </div>
        </ScrollArea>

        <DialogFooter className="pt-4 border-t">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={!canSubmit || isPending}>
            {isPending && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />}
            {isEdit ? "Save Changes" : "Create Policy"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ============================================
// Assignment Dialog
// ============================================

interface AssignPolicyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  policies: AccessPolicy[];
  organizations: IntegrationOrganization[];
  onSubmit: (data: AssignmentCreateRequest) => void;
  isPending: boolean;
}

function AssignPolicyDialog({
  open, onOpenChange, policies, organizations, onSubmit, isPending,
}: AssignPolicyDialogProps) {
  const [policyId, setPolicyId] = useState<string>("");
  const [email, setEmail] = useState("");
  const [orgId, setOrgId] = useState<string>("");

  const isValidEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);

  const handleOpen = (v: boolean) => {
    if (v) {
      setPolicyId("");
      setEmail("");
      setOrgId("");
    }
    onOpenChange(v);
  };

  const canSubmit = policyId && email.trim() && isValidEmail(email.trim()) && orgId;

  const handleSubmit = () => {
    if (!canSubmit) return;
    onSubmit({
      policy_id: parseInt(policyId),
      user_email: email.trim(),
      organization_id: parseInt(orgId),
    });
  };

  // filter policies by selected org
  const filteredPolicies = orgId
    ? policies.filter((p) => p.organization_id === parseInt(orgId))
    : policies;

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Assign Policy to Tenant</DialogTitle>
          <DialogDescription>
            Link an access policy to a tenant user, controlling what resources they can see and interact with.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label>Organization</Label>
            <Select value={orgId || "__none__"} onValueChange={(v) => {
              setOrgId(v === "__none__" ? "" : v);
              setPolicyId(""); // reset policy when org changes
            }}>
              <SelectTrigger>
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

          <div className="space-y-2">
            <Label>Tenant Email</Label>
            <Input
              type="email"
              placeholder="tenant@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            {email.trim() && !isValidEmail(email.trim()) && (
              <p className="text-xs text-destructive">Invalid email address</p>
            )}
          </div>

          <div className="space-y-2">
            <Label>Access Policy</Label>
            <Select value={policyId || "__none__"} onValueChange={(v) => setPolicyId(v === "__none__" ? "" : v)}>
              <SelectTrigger>
                <SelectValue placeholder="Select policy..." />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__" disabled>Select policy...</SelectItem>
                {filteredPolicies.map((p) => (
                  <SelectItem key={p.id} value={String(p.id)}>
                    {p.name}
                    {p.is_default && " (default)"}
                  </SelectItem>
                ))}
                {filteredPolicies.length === 0 && (
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    {orgId ? "No policies for this organization" : "Select an organization first"}
                  </div>
                )}
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={handleSubmit} disabled={!canSubmit || isPending}>
            {isPending && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />}
            Assign
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ============================================
// Main Component
// ============================================

export function AccessPoliciesSettings() {
  const queryClient = useQueryClient();
  const { isAdmin } = usePermissions();
  const { toast } = useToast();

  // dialog state
  const [createOpen, setCreateOpen] = useState(false);
  const [editPolicy, setEditPolicy] = useState<AccessPolicy | null>(null);
  const [assignOpen, setAssignOpen] = useState(false);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [deleteAssignId, setDeleteAssignId] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<"policies" | "assignments">("policies");

  // data queries
  const {
    data: policiesData,
    isLoading: policiesLoading,
    error: policiesError,
    refetch: refetchPolicies,
  } = useQuery({
    queryKey: ["tenant-access-policies"],
    queryFn: () => api.tenantAccess.listPolicies(),
  });

  const {
    data: assignmentsData,
    isLoading: assignmentsLoading,
    error: assignmentsError,
    refetch: refetchAssignments,
  } = useQuery({
    queryKey: ["tenant-access-assignments"],
    queryFn: () => api.tenantAccess.listAssignments(),
  });

  const { data: clustersData } = useClusters();
  const { data: orgs } = useQuery({
    queryKey: ["integration-organizations"],
    queryFn: () => integrationsAPI.listOrganizations(),
  });

  // flat cluster list for rule builder
  const clusters = useMemo(() => {
    if (!clustersData?.clusters) return [];
    return clustersData.clusters.map((c: any) => ({
      cluster_id: c.cluster_id,
      display_name: c.display_name || c.cluster_id,
    }));
  }, [clustersData]);

  const organizations = orgs || [];
  const policies = policiesData?.policies || [];
  const assignments = assignmentsData?.assignments || [];

  // ── Mutations ──

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
      setEditPolicy(null);
      toast({ title: "Policy Updated", description: `"${policy.name}" has been updated.` });
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

  const assignMutation = useMutation({
    mutationFn: (data: AssignmentCreateRequest) => api.tenantAccess.createAssignment(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tenant-access-assignments"] });
      setAssignOpen(false);
      toast({ title: "Policy Assigned", description: "Tenant can now access the defined resources." });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  const revokeAssignMutation = useMutation({
    mutationFn: (id: number) => api.tenantAccess.deleteAssignment(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tenant-access-assignments"] });
      setDeleteAssignId(null);
      toast({ title: "Assignment Revoked" });
    },
    onError: (err: Error) => {
      toast({ title: "Error", description: err.message, variant: "destructive" });
    },
  });

  // ── Handlers ──

  const handlePolicySubmit = (data: PolicyCreateRequest | PolicyUpdateRequest, isEdit: boolean) => {
    if (isEdit && editPolicy) {
      updateMutation.mutate({ id: editPolicy.id, data: data as PolicyUpdateRequest });
    } else {
      createMutation.mutate(data as PolicyCreateRequest);
    }
  };

  // ── Render ──

  const isLoading = policiesLoading || assignmentsLoading;

  return (
    <div className="space-y-6">
      {/* Stats overview */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-blue-500/10">
                <Shield className="h-5 w-5 text-blue-500" />
              </div>
              <div>
                <p className="text-2xl font-bold">{policies.length}</p>
                <p className="text-xs text-muted-foreground">Access Policies</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-green-500/10">
                <ShieldCheck className="h-5 w-5 text-green-500" />
              </div>
              <div>
                <p className="text-2xl font-bold">{assignments.length}</p>
                <p className="text-xs text-muted-foreground">Active Assignments</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-purple-500/10">
                <Building2 className="h-5 w-5 text-purple-500" />
              </div>
              <div>
                <p className="text-2xl font-bold">{organizations.length}</p>
                <p className="text-xs text-muted-foreground">Organizations</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Tab toggle */}
      <div className="flex items-center gap-2 border-b pb-1">
        <Button
          variant={activeTab === "policies" ? "default" : "ghost"}
          size="sm"
          onClick={() => setActiveTab("policies")}
          className="gap-1.5"
        >
          <Shield className="h-4 w-4" />
          Policies
        </Button>
        <Button
          variant={activeTab === "assignments" ? "default" : "ghost"}
          size="sm"
          onClick={() => setActiveTab("assignments")}
          className="gap-1.5"
        >
          <ShieldCheck className="h-4 w-4" />
          Assignments
        </Button>
      </div>

      {/* ── Policies Tab ── */}
      {activeTab === "policies" && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Shield className="h-5 w-5" />
                  Access Policies
                </CardTitle>
                <CardDescription className="mt-1.5">
                  Define resource-level access rules that can be assigned to tenant users.
                </CardDescription>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => refetchPolicies()}
                  disabled={policiesLoading}
                >
                  <RefreshCw className={`h-4 w-4 mr-1.5 ${policiesLoading ? "animate-spin" : ""}`} />
                  Refresh
                </Button>
                <Button size="sm" onClick={() => setCreateOpen(true)}>
                  <Plus className="h-4 w-4 mr-1.5" />
                  Create Policy
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {policiesLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                <span className="ml-2 text-muted-foreground">Loading policies...</span>
              </div>
            ) : policiesError ? (
              <div className="flex items-center gap-2 py-8 justify-center text-destructive">
                <AlertCircle className="h-5 w-5" />
                <span>Failed to load policies: {(policiesError as Error).message}</span>
              </div>
            ) : policies.length === 0 ? (
              <div className="text-center py-12 text-muted-foreground">
                <Shield className="h-10 w-10 mx-auto mb-3 opacity-40" />
                <p className="text-sm">No access policies yet</p>
                <p className="text-xs mt-1">
                  Create a policy to define which resources tenants can access.
                </p>
              </div>
            ) : (
              <div className="rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Policy Name</TableHead>
                      <TableHead>Rules</TableHead>
                      <TableHead>Assignments</TableHead>
                      <TableHead>Source</TableHead>
                      <TableHead className="w-[120px]">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {policies.map((policy) => (
                      <TableRow key={policy.id}>
                        <TableCell>
                          <div>
                            <div className="font-medium text-sm flex items-center gap-2">
                              {policy.name}
                              {policy.is_default && (
                                <Badge variant="outline" className="text-[10px] bg-blue-500/10 text-blue-600 border-blue-500/20">
                                  Default
                                </Badge>
                              )}
                            </div>
                            {policy.description && (
                              <p className="text-xs text-muted-foreground mt-0.5 max-w-[300px] truncate">
                                {policy.description}
                              </p>
                            )}
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary" className="text-xs">
                            {policy.rules?.length || 0} rule{(policy.rules?.length || 0) !== 1 ? "s" : ""}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className="text-xs">
                            {policy.assignments_count} user{policy.assignments_count !== 1 ? "s" : ""}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className="text-[10px]">
                            {policy.source}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <div className="flex gap-1">
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() => setEditPolicy(policy)}
                              title="Edit"
                            >
                              <Edit className="h-4 w-4" />
                            </Button>
                            <Dialog
                              open={deleteId === policy.id}
                              onOpenChange={(open) => setDeleteId(open ? policy.id : null)}
                            >
                              <DialogTrigger asChild>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-8 w-8 text-destructive hover:text-destructive"
                                  title="Delete"
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              </DialogTrigger>
                              <DialogContent>
                                <DialogHeader>
                                  <DialogTitle>Delete Policy</DialogTitle>
                                  <DialogDescription>
                                    Delete <strong>&quot;{policy.name}&quot;</strong>? This will also
                                    remove all {policy.assignments_count} assignment(s). This cannot
                                    be undone.
                                  </DialogDescription>
                                </DialogHeader>
                                <DialogFooter>
                                  <Button variant="outline" onClick={() => setDeleteId(null)}>
                                    Cancel
                                  </Button>
                                  <Button
                                    variant="destructive"
                                    onClick={() => deleteMutation.mutate(policy.id)}
                                    disabled={deleteMutation.isPending}
                                  >
                                    {deleteMutation.isPending && (
                                      <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                    )}
                                    Delete
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
            )}
          </CardContent>
        </Card>
      )}

      {/* ── Assignments Tab ── */}
      {activeTab === "assignments" && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <ShieldCheck className="h-5 w-5" />
                  Policy Assignments
                </CardTitle>
                <CardDescription className="mt-1.5">
                  See which tenants have which access policies assigned.
                </CardDescription>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => refetchAssignments()}
                  disabled={assignmentsLoading}
                >
                  <RefreshCw className={`h-4 w-4 mr-1.5 ${assignmentsLoading ? "animate-spin" : ""}`} />
                  Refresh
                </Button>
                <Button size="sm" onClick={() => setAssignOpen(true)}>
                  <Plus className="h-4 w-4 mr-1.5" />
                  Assign Policy
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {assignmentsLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                <span className="ml-2 text-muted-foreground">Loading assignments...</span>
              </div>
            ) : assignmentsError ? (
              <div className="flex items-center gap-2 py-8 justify-center text-destructive">
                <AlertCircle className="h-5 w-5" />
                <span>Failed to load assignments: {(assignmentsError as Error).message}</span>
              </div>
            ) : assignments.length === 0 ? (
              <div className="text-center py-12 text-muted-foreground">
                <ShieldCheck className="h-10 w-10 mx-auto mb-3 opacity-40" />
                <p className="text-sm">No assignments yet</p>
                <p className="text-xs mt-1">
                  Assign a policy to a tenant to grant them resource access.
                </p>
              </div>
            ) : (
              <div className="rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>User</TableHead>
                      <TableHead>Policy</TableHead>
                      <TableHead>Organization</TableHead>
                      <TableHead>Assigned By</TableHead>
                      <TableHead>Assigned</TableHead>
                      <TableHead className="w-[80px]">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {assignments.map((a) => (
                      <TableRow key={a.id}>
                        <TableCell className="font-medium text-sm">{a.user_email}</TableCell>
                        <TableCell>
                          <Badge variant="secondary" className="text-xs">
                            {a.policy_name}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className="text-xs">
                            <Building2 className="h-3 w-3 mr-1" />
                            {a.organization_name}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">{a.assigned_by}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {new Date(a.assigned_at).toLocaleDateString()}
                          {a.expires_at && (
                            <span className="text-yellow-600 ml-1">
                              (expires {new Date(a.expires_at).toLocaleDateString()})
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          <Dialog
                            open={deleteAssignId === a.id}
                            onOpenChange={(open) => setDeleteAssignId(open ? a.id : null)}
                          >
                            <DialogTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8 text-destructive hover:text-destructive"
                                title="Revoke"
                              >
                                <Trash2 className="h-4 w-4" />
                              </Button>
                            </DialogTrigger>
                            <DialogContent>
                              <DialogHeader>
                                <DialogTitle>Revoke Assignment</DialogTitle>
                                <DialogDescription>
                                  Revoke <strong>&quot;{a.policy_name}&quot;</strong> from{" "}
                                  <strong>{a.user_email}</strong>? They will lose access to the
                                  resources defined in this policy.
                                </DialogDescription>
                              </DialogHeader>
                              <DialogFooter>
                                <Button variant="outline" onClick={() => setDeleteAssignId(null)}>
                                  Cancel
                                </Button>
                                <Button
                                  variant="destructive"
                                  onClick={() => revokeAssignMutation.mutate(a.id)}
                                  disabled={revokeAssignMutation.isPending}
                                >
                                  {revokeAssignMutation.isPending && (
                                    <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                  )}
                                  Revoke
                                </Button>
                              </DialogFooter>
                            </DialogContent>
                          </Dialog>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ── Dialogs ── */}

      {/* Create policy */}
      <PolicyEditorDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        clusters={clusters}
        organizations={organizations}
        onSubmit={handlePolicySubmit}
        isPending={createMutation.isPending}
      />

      {/* Edit policy */}
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

      {/* Assign policy */}
      <AssignPolicyDialog
        open={assignOpen}
        onOpenChange={setAssignOpen}
        policies={policies}
        organizations={organizations}
        onSubmit={(data) => assignMutation.mutate(data)}
        isPending={assignMutation.isPending}
      />
    </div>
  );
}
