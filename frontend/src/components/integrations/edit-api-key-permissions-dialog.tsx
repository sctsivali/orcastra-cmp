"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, KeyRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ClusterAccessSelector } from "@/components/cluster-access-selector";
import { PausedGrantsNotice } from "@/components/integrations/paused-grants-notice";
import { ProjectScopePicker } from "@/components/integrations/project-scope-picker";
import { useProjectScopeOptions } from "@/hooks/use-project-scope-options";
import type { APIKeyInfo, AvailableCluster, ClusterPermission } from "@/types";

type ProjectScope = "default" | "all" | "specific";

interface SeededGrant {
  clusterIds: string[];
  permissionLevel: string;
  scope: ProjectScope;
  projects: string[];
  /** True when the stored grants do not all say the same thing. */
  diverged: boolean;
}

/**
 * Read one editable shape out of a stored grant array.
 *
 * The console has only ever written the same permission level and project list to every cluster
 * on a key, but the API takes a list and an integration may have written something else. Rather
 * than silently flattening that, the divergence is detected and the dialog says so before the
 * operator overwrites it.
 */
function seedFromGrants(permissions: ClusterPermission[] | undefined): SeededGrant {
  const grants = (permissions ?? []).filter((grant) => grant && grant.cluster_id);
  const clusterIds = grants.map((grant) => String(grant.cluster_id));
  const first = grants[0];
  const levels = new Set(grants.map((grant) => (grant.permissions ?? ["read"])[0] ?? "read"));
  const shapes = new Set(grants.map((grant) => JSON.stringify(grant.projects ?? null)));

  const projects = (first?.projects ?? ["default"]).filter(Boolean);
  let scope: ProjectScope = "specific";
  if (projects.includes("*")) scope = "all";
  else if (projects.length === 1 && projects[0] === "default") scope = "default";

  return {
    clusterIds,
    permissionLevel: (first?.permissions ?? ["read"])[0] ?? "read",
    scope,
    projects: scope === "specific" ? projects : [],
    diverged: levels.size > 1 || shapes.size > 1,
  };
}

interface EditAPIKeyPermissionsDialogProps {
  apiKey: APIKeyInfo;
  clusters: AvailableCluster[];
  /** True while the cluster list is being read again. */
  clustersChecking?: boolean;
  /** Called each time the dialog opens, so the page can read the cluster and key lists again. */
  onOpen?: () => void;
  /** Resolves when the update lands and rejects when it does not. */
  onSubmit: (keyId: string, permissions: ClusterPermission[], rebindClusterIds: string[]) => Promise<unknown>;
  isSubmitting: boolean;
}

/**
 * Change which clusters and projects an existing key may reach, without reissuing it.
 *
 * The endpoint behind this has existed since the key surface was built and had no caller, so
 * narrowing an over-broad grant meant revoking the credential and asking the integrator for a
 * new one. Its authorization is the same as every other single-key handler: the key must overlap
 * the caller's clusters, and the requested clusters are re-checked against them.
 */
export function EditAPIKeyPermissionsDialog({
  apiKey,
  clusters,
  clustersChecking = false,
  onOpen,
  onSubmit,
  isSubmitting,
}: EditAPIKeyPermissionsDialogProps) {
  const [open, setOpen] = useState(false);
  const seed = useMemo(() => seedFromGrants(apiKey.cluster_permissions), [apiKey]);
  // note: by content, not identity. Opening reads the key list again, and a refetch that changes
  // only a last use or a registered flag must not wipe what the operator is typing.
  const seedKey = JSON.stringify(seed);

  const [selectedClusterIds, setSelectedClusterIds] = useState<string[]>(seed.clusterIds);
  const [permissionLevel, setPermissionLevel] = useState(seed.permissionLevel);
  const [projectScope, setProjectScope] = useState<ProjectScope>(seed.scope);
  const [selectedProjects, setSelectedProjects] = useState<string[]>(seed.projects);
  const [rebind, setRebind] = useState<string[]>([]);
  const submitInFlightRef = useRef(false);
  const pausedGrants = useMemo(
    () => (apiKey.cluster_permissions ?? []).filter((grant) => grant?.registered === false),
    [apiKey],
  );

  // Re-seed each time it opens. The key can change under the dialog between visits (another
  // session, or this one after a successful save), and a stale form would silently write the
  // grant back to what it was when the page loaded.
  useEffect(() => {
    if (!open) return;
    setSelectedClusterIds(seed.clusterIds);
    setPermissionLevel(seed.permissionLevel);
    setProjectScope(seed.scope);
    setSelectedProjects(seed.projects);
    setRebind([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seedKey stands for seed
  }, [open, seedKey]);

  const projectScopeOptions = useProjectScopeOptions({
    selectedClusterIds,
    clusters,
    selectedProjects,
    enabled: open && projectScope === "specific",
  });

  const specificScopeIncomplete =
    projectScope === "specific" &&
    (selectedProjects.length === 0 || projectScopeOptions.orphanedProjects.length > 0);

  const toggleCluster = (clusterId: string) => {
    setSelectedClusterIds((prev) =>
      prev.includes(clusterId)
        ? prev.filter((id) => id !== clusterId)
        : [...prev, clusterId],
    );
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitInFlightRef.current) return;

    const projects =
      projectScope === "all"
        ? ["*"]
        : projectScope === "specific"
          ? selectedProjects
          : ["default"];
    if (projects.length === 0) return;

    const permissions: ClusterPermission[] = selectedClusterIds.map((clusterId) => ({
      cluster_id: clusterId,
      permissions: [permissionLevel],
      projects,
    }));

    submitInFlightRef.current = true;
    try {
      // Only a paused grant the operator ticked, and only if it is still part of the selection.
      await onSubmit(apiKey.id, permissions, rebind.filter((id) => selectedClusterIds.includes(id)));
    } catch {
      // The parent surfaces the error and the dialog stays open with the operator's edit.
      return;
    } finally {
      submitInFlightRef.current = false;
    }
    setOpen(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) onOpen?.();
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <KeyRound className="mr-1 h-4 w-4" aria-hidden="true" />
          Edit Access
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>Edit Cluster Access</DialogTitle>
          <DialogDescription>
            Change what {String(apiKey.name || "this key")} may reach. The credential itself does
            not change, so the integration keeps working.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
          <DialogBody className="-mr-2 pr-2">
            <div className="grid gap-4 py-4">
              {seed.diverged && (
                <div role="status" className="flex items-start gap-2 text-xs">
                  <AlertTriangle
                    aria-hidden="true"
                    className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-offline"
                  />
                  <p className="text-muted-foreground">
                    This key currently grants different access per cluster. Saving here applies one
                    permission level and one project list to every cluster you select.
                  </p>
                </div>
              )}

              <ClusterAccessSelector
                clusters={clusters}
                selectedClusterIds={selectedClusterIds}
                onToggle={toggleCluster}
                listChecking={clustersChecking}
              />

              <PausedGrantsNotice
                grants={pausedGrants}
                rebind={rebind}
                onToggle={(clusterId) =>
                  setRebind((prev) =>
                    prev.includes(clusterId) ? prev.filter((id) => id !== clusterId) : [...prev, clusterId],
                  )
                }
                disabled={isSubmitting}
              />

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="edit_permission_level">Permission Level</Label>
                  <Select value={permissionLevel} onValueChange={setPermissionLevel}>
                    <SelectTrigger id="edit_permission_level">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="read">Read Only</SelectItem>
                      <SelectItem value="write">Read &amp; Write</SelectItem>
                      <SelectItem value="admin">Full Admin</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="edit_project_scope">Project Scope</Label>
                  <Select
                    value={projectScope}
                    onValueChange={(next) => setProjectScope(next as ProjectScope)}
                  >
                    <SelectTrigger id="edit_project_scope">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="default">Default project only</SelectItem>
                      <SelectItem value="all">All projects</SelectItem>
                      <SelectItem value="specific">Specific projects</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              {projectScope === "specific" && (
                <ProjectScopePicker
                  id="edit_project_list"
                  value={selectedProjects}
                  onChange={setSelectedProjects}
                  selectedClusterIds={selectedClusterIds}
                  options={projectScopeOptions.options}
                  answeredCount={projectScopeOptions.answeredCount}
                  unansweredClusterLabels={projectScopeOptions.unansweredClusterLabels}
                  orphanedProjects={projectScopeOptions.orphanedProjects}
                  partiallyCoveredProjects={projectScopeOptions.partiallyCoveredProjects}
                  isLoading={projectScopeOptions.isLoading}
                  isError={projectScopeOptions.isError}
                  onRetry={projectScopeOptions.refreshNow}
                  disabled={isSubmitting}
                />
              )}
            </div>
          </DialogBody>
          <DialogFooter className="shrink-0 border-t pt-3">
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                isSubmitting || selectedClusterIds.length === 0 || specificScopeIncomplete
              }
            >
              {isSubmitting ? "Saving…" : "Save Access"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
