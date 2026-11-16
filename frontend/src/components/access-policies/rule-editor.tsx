"use client";

import { useId, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { clusterStatusMeta, clusterSublabel } from "@/lib/cluster-status";
import { projectAPI, instanceAPI } from "@/lib/api";
import { queryKeys } from "@/hooks/use-lxd";
import { readInstanceList } from "@/lib/instance-reconciliation";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import type { ClusterStatus } from "@/types/api";
import type { PolicyRule, TenantPermission } from "@/types";
import {
  ALL_TENANT_PERMISSIONS, TENANT_PERMISSION_DESCRIPTIONS, TENANT_PERMISSION_LABELS, TENANT_PERMISSION_REQUIRES,
} from "@/types";

/** A cluster as the rule picker offers it. */
export interface PolicyCluster {
  cluster_id: string;
  display_name: string;
  endpoint: string;
  status?: ClusterStatus;
}

/**
 * A rule being edited. `key` is client-only and never sent: rows can be removed from the middle,
 * and an index key would hand one rule's expanded state and pending reads to the next.
 */
export type DraftRule = Omit<PolicyRule, "id"> & { key: string };

// The snapshot permission is not offered in the editor.
export const VISIBLE_PERMISSIONS = ALL_TENANT_PERMISSIONS.filter((p) => p !== "snapshot");

function PermissionCheckboxGroup({
  selected,
  onChange,
}: {
  selected: TenantPermission[];
  onChange: (perms: TenantPermission[]) => void;
}) {
  const baseId = useId();
  const toggle = (perm: TenantPermission) => {
    if (selected.includes(perm)) {
      // A rule must grant something; the last permission stays until another is ticked. Taking
      // away a grant another one needs takes that one too (Operate takes Rebuild).
      const dependents = ALL_TENANT_PERMISSIONS.filter((p) => TENANT_PERMISSION_REQUIRES[p]?.includes(perm));
      const next = selected.filter((p) => p !== perm && !dependents.includes(p));
      if (next.length > 0) onChange(next);
    } else {
      // Ticking a grant ticks what it needs, so a rule never stores one the server cannot honour.
      const needed = TENANT_PERMISSION_REQUIRES[perm] ?? [];
      onChange(Array.from(new Set([...selected, ...needed, perm])));
    }
  };

  return (
    <div className="space-y-1.5">
      {VISIBLE_PERMISSIONS.map((perm) => {
        const id = `${baseId}-${perm}`;
        return (
          <div key={perm} className="flex items-start gap-2.5 p-1.5 rounded-md hover:bg-muted/50">
            <Checkbox
              id={id}
              checked={selected.includes(perm)}
              onCheckedChange={() => toggle(perm)}
              aria-describedby={`${id}-desc`}
              className="mt-0.5"
            />
            <div>
              <Label htmlFor={id} className="text-xs font-medium cursor-pointer">
                {TENANT_PERMISSION_LABELS[perm]}
              </Label>
              <p id={`${id}-desc`} className="text-xs text-muted-foreground leading-tight">
                {TENANT_PERMISSION_DESCRIPTIONS[perm]}
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** One rule: cluster, project and instance scope, then the permissions it grants there. */
export function RuleEditor({
  rule, index, clusters, canRemove, onUpdate, onRemove,
}: {
  rule: DraftRule;
  index: number;
  clusters: PolicyCluster[];
  canRemove: boolean;
  onUpdate: (rule: DraftRule) => void;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const fieldId = useId();
  const hostId = rule.cluster_id !== "*" ? rule.cluster_id : "";

  // The projects API takes the cluster id as the host.
  const { data: projectsData, isLoading: projectsLoading } = useQuery({
    queryKey: queryKeys.projectsFor(scopeKey, hostId),
    queryFn: () => projectAPI.list(hostId),
    enabled: isAuthenticated && !!hostId,
    staleTime: 60_000, // re-expanding a rule should not read again
  });

  // Read through the pending-delete filter: this key is the per-host instance list other pages
  // read too.
  const instanceQueryClient = useQueryClient();
  const { data: instancesData, isLoading: instancesLoading } = useQuery({
    queryKey: queryKeys.instancesFor(scopeKey, hostId),
    queryFn: () => readInstanceList(instanceQueryClient, scopeKey, () => instanceAPI.list(hostId)),
    enabled: isAuthenticated && !!hostId,
    staleTime: 60_000,
  });

  const projects = projectsData?.projects ?? [];
  const instances = useMemo(() => {
    const all = instancesData?.vms ?? [];
    if (rule.project_name && rule.project_name !== "*") {
      return all.filter((instance) => instance.project === rule.project_name);
    }
    return all;
  }, [instancesData, rule.project_name]);

  const clusterLabel = rule.cluster_id === "*"
    ? "All Clusters"
    : clusters.find((c) => c.cluster_id === rule.cluster_id)?.display_name || rule.cluster_id;

  return (
    <div className="border rounded-lg p-2.5 space-y-2 bg-muted/30">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          className="flex items-center gap-1.5 rounded-sm text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {expanded
            ? <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
            : <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />}
          Rule {index + 1}
          <span className="text-muted-foreground font-normal">
            {clusterLabel}
            {rule.project_name !== "*" && ` / ${rule.project_name}`}
            {rule.instance_name !== "*" && ` / ${rule.instance_name}`}
          </span>
        </button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-destructive hover:text-destructive"
          onClick={onRemove}
          disabled={!canRemove}
          aria-label={`Remove rule ${index + 1}`}
          title={canRemove ? undefined : "A policy needs at least one rule"}
        >
          <Trash2 className="h-3 w-3" aria-hidden="true" />
        </Button>
      </div>

      {expanded && (
        <div className="grid gap-2.5 pl-5">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor={`${fieldId}-cluster`} className="text-xs text-muted-foreground">Cluster</Label>
              <SearchableSelect
                id={`${fieldId}-cluster`}
                value={rule.cluster_id}
                onValueChange={(v) => onUpdate({
                  ...rule,
                  cluster_id: v,
                  project_name: "*",
                  instance_name: "*",
                })}
                className="h-7 text-xs"
                searchPlaceholder="Search clusters…"
                options={[
                  { value: "*", label: "* (All Clusters)" },
                  // A scope picker: a rule may name an offline cluster, so the status is shown
                  // but never disables or warns about an option.
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
              <Label htmlFor={`${fieldId}-project`} className="text-xs text-muted-foreground">Project</Label>
              <Select
                value={rule.project_name}
                onValueChange={(v) => onUpdate({ ...rule, project_name: v, instance_name: "*" })}
                disabled={rule.cluster_id === "*"}
              >
                <SelectTrigger id={`${fieldId}-project`} className="h-7 text-xs">
                  <SelectValue placeholder={projectsLoading ? "Loading…" : "* (all)"} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="*">* (All Projects)</SelectItem>
                  {projectsLoading ? (
                    <div className="flex items-center justify-center py-2">
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-hidden="true" />
                      <span className="ml-1.5 text-xs text-muted-foreground">Loading projects…</span>
                    </div>
                  ) : (
                    projects.map((p) => (
                      <SelectItem key={p.name} value={p.name}>{p.name}</SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${fieldId}-instance`} className="text-xs text-muted-foreground">Instance</Label>
              <Select
                value={rule.instance_name}
                onValueChange={(v) => onUpdate({ ...rule, instance_name: v })}
                disabled={rule.cluster_id === "*"}
              >
                <SelectTrigger id={`${fieldId}-instance`} className="h-7 text-xs">
                  <SelectValue placeholder={instancesLoading ? "Loading…" : "* (all)"} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="*">* (All Instances)</SelectItem>
                  {instancesLoading ? (
                    <div className="flex items-center justify-center py-2">
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-hidden="true" />
                      <span className="ml-1.5 text-xs text-muted-foreground">Loading instances…</span>
                    </div>
                  ) : (
                    instances.map((instance) => (
                      <SelectItem key={`${instance.project}/${instance.name}`} value={instance.name}>
                        {instance.name}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </div>
          </div>
          <fieldset className="space-y-1">
            <legend className="text-xs text-muted-foreground">Permissions</legend>
            <PermissionCheckboxGroup
              selected={rule.permissions as TenantPermission[]}
              onChange={(perms) => onUpdate({ ...rule, permissions: perms })}
            />
          </fieldset>
        </div>
      )}
    </div>
  );
}
