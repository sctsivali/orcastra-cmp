"use client";

import * as React from "react";
import { AlertTriangle, Loader2 } from "lucide-react";

import {
  SnapshotCreateForm,
  type SnapshotCreateTarget,
} from "@/components/snapshots/snapshot-create-form";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import {
  SearchableSelect,
  type SearchableSelectOption,
} from "@/components/ui/searchable-select";
import {
  useInstance,
  useInstances,
  useInstanceSnapshots,
  useProjects,
} from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useSnapshotProjectOptions } from "@/hooks/use-snapshot-project-options";
import { instanceStatusDisplay } from "@/lib/instance-status";
import { formatInstanceType } from "@/lib/utils";
import type { SnapshotCreateResponse } from "@/types/api";

interface TakeSnapshotDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialHost?: string;
  initialProject?: string;
  onCreated: (result: SnapshotCreateResponse, target: SnapshotCreateTarget) => void;
  onViewSnapshots: (target: SnapshotCreateTarget) => Promise<void> | void;
}

export function TakeSnapshotDialog({
  open,
  onOpenChange,
  initialHost,
  initialProject,
  onCreated,
  onViewSnapshots,
}: TakeSnapshotDialogProps) {
  const [busy, setBusy] = React.useState(false);
  const handleOpenChange = (next: boolean) => {
    if (!next && busy) return;
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={`max-h-[calc(100dvh-2rem)] max-w-2xl overflow-hidden flex flex-col ${busy ? "[&>button:last-child]:hidden" : ""}`}
      >
        <DialogHeader className="shrink-0 pr-8 pb-4">
          <DialogTitle>Take a Snapshot</DialogTitle>
          <DialogDescription>
            Choose one exact instance, then create a stateless restore point that expires
            automatically.
          </DialogDescription>
        </DialogHeader>
        <TakeSnapshotDialogBody
          busy={busy}
          initialHost={initialHost}
          initialProject={initialProject}
          onBusyChange={setBusy}
          onCancel={() => handleOpenChange(false)}
          onCreated={(result, target) => {
            onOpenChange(false);
            onCreated(result, target);
          }}
          onViewSnapshots={async (target) => {
            await onViewSnapshots(target);
            onOpenChange(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

interface TakeSnapshotDialogBodyProps {
  busy: boolean;
  initialHost?: string;
  initialProject?: string;
  onBusyChange: (busy: boolean) => void;
  onCancel: () => void;
  onCreated: (result: SnapshotCreateResponse, target: SnapshotCreateTarget) => void;
  onViewSnapshots: (target: SnapshotCreateTarget) => Promise<void> | void;
}

function TakeSnapshotDialogBody({
  busy,
  initialHost,
  initialProject,
  onBusyChange,
  onCancel,
  onCreated,
  onViewSnapshots,
}: TakeSnapshotDialogBodyProps) {
  const [host, setHost] = React.useState(initialHost ?? "");
  const [project, setProject] = React.useState(
    initialHost && initialProject ? initialProject : "",
  );
  const [instanceName, setInstanceName] = React.useState("");
  const [selectionNotice, setSelectionNotice] = React.useState<string | null>(null);
  const nodePicker = useNodePickerOptions({ disableUnreachable: true });
  // A known non-online cluster cannot complete this write. Keep it visible with status, but
  // unlike a diagnostic filter, do not let it become a create target.
  const selectedNode = nodePicker.options.find((option) => option.value === host);
  const hostVerified = Boolean(selectedNode && !selectedNode.disabled);
  const projects = useProjects(host, { enabled: hostVerified, verifyFresh: true });
  // The page already keeps this actor-scoped, ACL-filtered fleet project inventory warm for its
  // filter. Use it only to make the picker immediately interactive while the authoritative
  // per-cluster read lands. It never verifies a target or enables submit by itself.
  const suggestedProjects = useSnapshotProjectOptions(host);
  const verifiedProjectOptions = React.useMemo<SearchableSelectOption[]>(() => {
    if (!projects.data || projects.data.reachable === false) return [];
    const names = new Set(
      projects.data.projects
        .map((row) => row.name)
        .filter((name) => Boolean(name) && name !== "*"),
    );
    return Array.from(names)
      .sort((a, b) => {
        if (a === "default") return -1;
        if (b === "default") return 1;
        return a.localeCompare(b);
      })
      .map((name) => ({ value: name, label: name }));
  }, [projects.data]);
  const projectOptions = projects.data ? verifiedProjectOptions : suggestedProjects.options;
  const projectCandidate =
    Boolean(project) && projectOptions.some((option) => option.value === project);
  const projectVerified =
    projectCandidate &&
    projects.isSuccess &&
    !projects.isFetching &&
    projects.data.reachable !== false &&
    verifiedProjectOptions.some((option) => option.value === project);

  const instances = useInstances(host, {
    project,
    // This scoped endpoint enforces cluster, project and actor authorization itself. Starting it
    // from an actor-scoped candidate overlaps two independent reads, while every create control
    // below still waits for the per-cluster project verification to finish.
    enabled: hostVerified && projectCandidate && !projects.isError,
    // A point-in-time target choice does not need the background poll used by list pages.
    realtime: true,
    verifyFresh: true,
    transientRetries: 3,
  });
  const instancesUnreachable = Boolean(instances.data?.unreachable_nodes?.includes(host));
  const instanceRows = React.useMemo(
    () =>
      (instances.data?.vms ?? []).filter(
        (row) => row.host === host && (row.project || "default") === project,
      ),
    [instances.data, host, project],
  );
  const instanceOptions = React.useMemo<SearchableSelectOption[]>(
    () =>
      instanceRows
        .slice()
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((row) => ({
          value: row.name,
          label: row.name,
          sublabel:
            formatInstanceType(row.type) + " · " + instanceStatusDisplay(row.status).label,
        })),
    [instanceRows],
  );
  const instanceVerified =
    Boolean(instanceName) &&
    projectVerified &&
    instances.isSuccess &&
    !instances.isFetching &&
    !instancesUnreachable &&
    instanceRows.some((row) => row.name === instanceName);

  const detail = useInstance(instanceName, host, instanceVerified, project);
  const hydratedMatches =
    detail.data?.name === instanceName &&
    detail.data.host === host &&
    (detail.data.project || "default") === project;
  const snapshots = useInstanceSnapshots(
    instanceName,
    host,
    project,
    instanceVerified,
    true,
  );

  React.useEffect(() => {
    if (busy || !host || nodePicker.isLoading || nodePicker.isError) return;
    const option = nodePicker.options.find((candidate) => candidate.value === host);
    if (option && !option.disabled) return;
    setHost("");
    setProject("");
    setInstanceName("");
    setSelectionNotice(
      option
        ? "The selected cluster is not online, so it was removed from the target."
        : "The filtered cluster is no longer available. Choose another cluster.",
    );
  }, [busy, host, nodePicker.isError, nodePicker.isLoading, nodePicker.options]);

  React.useEffect(() => {
    if (busy || !project || !projects.isSuccess || projects.isFetching) return;
    if (
      projects.data.reachable !== false &&
      projectOptions.some((option) => option.value === project)
    ) {
      return;
    }
    setProject("");
    setInstanceName("");
    setSelectionNotice(
      projects.data.reachable === false
        ? "The selected cluster did not answer the project read."
        : "The filtered project is no longer available. Choose another project.",
    );
  }, [busy, project, projectOptions, projects.data, projects.isFetching, projects.isSuccess]);

  React.useEffect(() => {
    if (busy || !instanceName || !instances.isSuccess || instances.isFetching) return;
    if (instancesUnreachable) return;
    if (instanceRows.some((row) => row.name === instanceName)) return;
    setInstanceName("");
    setSelectionNotice("The selected instance is no longer available. Choose another instance.");
  }, [
    busy,
    instanceName,
    instanceRows,
    instances.isFetching,
    instances.isSuccess,
    instancesUnreachable,
  ]);

  const verifiedTarget: SnapshotCreateTarget | undefined =
    hostVerified &&
    projectVerified &&
    instanceVerified &&
    detail.data &&
    detail.isSuccess &&
    !detail.isFetching &&
    hydratedMatches &&
    snapshots.isSuccess &&
    !snapshots.isFetching &&
    snapshots.data
      ? {
          instance: detail.data,
          project,
          billingLinked: Boolean(snapshots.data.policy.externally_managed),
          operatorCount: snapshots.data.items.filter((row) => row.operator_managed).length,
        }
      : undefined;
  const verifiedTargetRef = React.useRef<SnapshotCreateTarget | undefined>(undefined);
  if (verifiedTarget) verifiedTargetRef.current = verifiedTarget;
  // Keep the reviewed tuple and summary stable while its non-idempotent POST or an explicit
  // verification read is running. Selectors are frozen for the same interval.
  const target = busy ? verifiedTargetRef.current : verifiedTarget;
  const hydrating =
    Boolean(instanceName) &&
    (projects.isFetching ||
      instances.isFetching ||
      detail.isLoading ||
      detail.isFetching ||
      snapshots.isLoading ||
      snapshots.isFetching);
  const preflightError =
    instances.isError ||
    instancesUnreachable ||
    detail.isError ||
    (instanceVerified && snapshots.isError);

  const resetAfterHost = (next: string) => {
    if (busy) return;
    setHost(next);
    setProject("");
    setInstanceName("");
    setSelectionNotice(null);
  };
  const resetAfterProject = (next: string) => {
    if (busy) return;
    setProject(next);
    setInstanceName("");
    setSelectionNotice(null);
  };

  return (
    <SnapshotCreateForm
      key={[host, project, instanceName].join("/")}
      target={target}
      onBusyChange={onBusyChange}
      onCancel={onCancel}
      onResult={(result) => {
        if (target) onCreated(result, target);
      }}
      onViewSnapshots={async () => {
        // A failed verification read removes the active target fail-closed, but an ambiguous POST
        // still needs a safe read-only recovery action. Reuse only the exact tuple that previously
        // passed every preflight. Changing any selector remounts the keyed form and removes the
        // recovery action before this callback can be exposed for a different target.
        const recoveryTarget = target ?? verifiedTargetRef.current;
        if (!recoveryTarget) {
          throw new Error("No previously verified snapshot target is available");
        }
        await snapshots.refreshNow();
        await onViewSnapshots(recoveryTarget);
      }}
    >
      <section className="space-y-4" aria-label="Snapshot target">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="space-y-2">
            <Label htmlFor="take-snapshot-cluster">Cluster</Label>
            <SearchableSelect
              id="take-snapshot-cluster"
              value={host}
              onValueChange={resetAfterHost}
              options={nodePicker.isError ? [] : nodePicker.options}
              placeholder={nodePicker.isLoading ? "Reading clusters" : "Select a cluster"}
              searchPlaceholder="Search clusters"
              emptyText={
                nodePicker.isError ? "The cluster list could not be read" : "No online cluster found"
              }
              disabled={busy || nodePicker.isLoading || nodePicker.isError}
              ariaDescribedBy="take-snapshot-cluster-status"
              ariaInvalid={nodePicker.isError}
            />
            <div id="take-snapshot-cluster-status">
              <NodeStatusNotice
                meta={host ? nodePicker.statusOf(host) : undefined}
                reason={host ? nodePicker.availabilityOf(host)?.reason ?? undefined : undefined}
                nodeLabel={host ? nodePicker.labelOf(host) : undefined}
                unavailable={nodePicker.statusUnavailable}
                noSelectableNodes={!nodePicker.isLoading && nodePicker.selectableCount === 0}
                updatedAt={nodePicker.statusUpdatedAt}
                onRecheck={busy ? undefined : nodePicker.refreshNow}
              />
              {nodePicker.isError && (
                <p role="alert" className="text-xs text-destructive">
                  Clusters could not be read.{" "}
                  <button
                    type="button"
                    className="underline underline-offset-2"
                    onClick={() => void nodePicker.retryNames()}
                    disabled={busy}
                  >
                    Try again
                  </button>
                </p>
              )}
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="take-snapshot-project">Project</Label>
            <SearchableSelect
              id="take-snapshot-project"
              value={project}
              onValueChange={resetAfterProject}
              options={projectOptions}
              placeholder={
                !host
                  ? "Select a cluster first"
                  : (projects.isLoading || projects.isFetching) && projectOptions.length === 0
                    ? "Reading projects"
                    : "Select project"
              }
              searchPlaceholder="Search projects"
              emptyText={
                projects.isError || projects.data?.reachable === false
                  ? "Projects could not be read"
                  : "No project found"
              }
              disabled={
                busy ||
                !hostVerified ||
                ((projects.isLoading || projects.isFetching) && projectOptions.length === 0) ||
                projects.isError
              }
              ariaInvalid={projects.isError || projects.data?.reachable === false}
              ariaDescribedBy="take-snapshot-project-status"
            />
            <div id="take-snapshot-project-status">
              {(projects.isLoading || projects.isFetching) && projectOptions.length > 0 && (
                <p role="status" className="text-xs text-muted-foreground">
                  Verifying projects on this cluster
                </p>
              )}
              {(projects.isError || projects.data?.reachable === false) && (
                <p role="alert" className="text-xs text-destructive">
                  The project list could not be verified.{" "}
                  <button
                    type="button"
                    className="underline underline-offset-2"
                    onClick={() => void projects.refetch()}
                    disabled={busy}
                  >
                    Try again
                  </button>
                </p>
              )}
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="take-snapshot-instance">Instance</Label>
            <SearchableSelect
              id="take-snapshot-instance"
              value={instanceName}
              onValueChange={(next) => {
                if (busy) return;
                setInstanceName(next);
                setSelectionNotice(null);
              }}
              options={instanceOptions}
              placeholder={
                !projectCandidate
                  ? "Select project first"
                  : (instances.isLoading || instances.isFetching) && instanceOptions.length === 0
                    ? "Reading instances"
                    : "Select instance"
              }
              searchPlaceholder="Search instances"
              emptyText={
                instances.isError || instancesUnreachable
                  ? "Instances could not be read"
                  : "No instance found"
              }
              disabled={
                busy ||
                !projectCandidate ||
                ((instances.isLoading || instances.isFetching) && instanceOptions.length === 0) ||
                instances.isError ||
                instancesUnreachable
              }
              ariaInvalid={instances.isError || instancesUnreachable}
              ariaDescribedBy="take-snapshot-instance-status"
            />
            <div id="take-snapshot-instance-status">
              {(instances.isLoading || instances.isFetching) && instanceOptions.length > 0 && (
                <p role="status" className="text-xs text-muted-foreground">
                  Verifying instances in this project
                </p>
              )}
              {(instances.isError || instancesUnreachable) && (
                <p className="text-xs text-destructive">
                  {instancesUnreachable
                    ? "The selected cluster did not answer the instance read. "
                    : "Instances could not be read. "}
                  <button
                    type="button"
                    className="underline underline-offset-2"
                    onClick={() => void instances.refetch()}
                    disabled={busy}
                  >
                    Try again
                  </button>
                </p>
              )}
            </div>
          </div>
        </div>

        {selectionNotice && (
          <p role="status" className="text-sm text-warning">
            {selectionNotice}
          </p>
        )}

        {hydrating && (
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
            Checking the instance and snapshot policy
          </p>
        )}

        {preflightError && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertTitle>Target could not be verified</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>
                The instance may have moved, disappeared, or become unavailable to your role.
                Select it again after refreshing the target.
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => {
                  if (instances.isError || instancesUnreachable) {
                    void instances.refreshNow();
                    return;
                  }
                  if (detail.isError) void detail.refetch();
                  if (instanceVerified && snapshots.isError) void snapshots.refetch();
                }}
                disabled={busy}
              >
                Retry target check
              </Button>
            </AlertDescription>
          </Alert>
        )}
      </section>
    </SnapshotCreateForm>
  );
}
