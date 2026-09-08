"use client";

import * as React from "react";
import { AlertTriangle, ArrowRight, Info } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Textarea } from "@/components/ui/textarea";
import { useCopyInstance, useMovePreflight } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useToast } from "@/hooks/use-toast";
import { formatBytes } from "@/lib/utils";
import type { VMInfo } from "@/types/api";

interface MoveInstanceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instance: VMInfo;
}

/**
 * Copy a workload to another project, member or cluster.
 *
 * Copy, never move: the source survives, so a destination that turns out wrong is recoverable
 * and the operator decides when to remove the original.
 *
 * The destination list comes from a preflight rather than from the full fleet, so a cluster the
 * operator cannot reach or is not scoped to is never offered. Which transport will be used is
 * shown before the operator commits, because a copy inside one cluster and a transfer between
 * two are minutes apart and that difference should not be a surprise.
 */
export function MoveInstanceDialog({ open, onOpenChange, instance }: MoveInstanceDialogProps) {
  const { toast } = useToast();
  const project = instance.project || "default";
  const preflight = useMovePreflight(instance.name, instance.host, project, open);
  const copyInstance = useCopyInstance();

  const [targetHost, setTargetHost] = React.useState("");
  const [targetProject, setTargetProject] = React.useState("");
  const [targetPool, setTargetPool] = React.useState("");
  const [targetName, setTargetName] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const { isReadOnly } = useReadOnlyMode();

  // The source's own schedule, straight off the row. A copy duplicates an instance whose storage
  // another product meters, so it carries the same acknowledgement as a snapshot rather than an
  // exception an operator has to remember.
  const externalSchedule = instance.config?.["snapshots.schedule"] || null;
  const billingLinked = !!externalSchedule;
  const submitInFlightRef = React.useRef(false);

  React.useEffect(() => {
    if (open) {
      setTargetHost("");
      setTargetProject("");
      setTargetPool("");
      setTargetName(`${instance.name}-copy`);
      setError(null);
    }
  }, [open, instance.name]);

  const destinations = preflight.data?.destinations ?? [];
  const chosen = destinations.find((d) => d.host === targetHost);
  const footprint = preflight.data?.footprint_bytes ?? null;

  // A destination is offered as unavailable with its reason rather than hidden. An operator
  // who cannot see pandora at all will ask why; one who sees "runs aarch64, this is x86_64"
  // already has the answer.
  const hostOptions = destinations.map((d) => {
    const unusableReason =
      d.reachable === false
        ? "Not reachable right now"
        : d.compatible === false
          ? d.reason || "Cannot run this instance's architecture"
          : undefined;
    return {
      value: d.host,
      label: d.host,
      disabled: Boolean(unusableReason),
      description: unusableReason,
    };
  });
  const projectOptions = (chosen?.projects ?? []).map((p) => ({ value: p, label: p }));
  const poolOptions = (chosen?.pools ?? []).map((p) => ({
    value: p.name,
    label: p.name,
    description: p.headroom ? `${formatBytes(p.headroom.free)} free` : undefined,
  }));

  const sameCluster = chosen?.same_cluster ?? true;
  const transportLabel = sameCluster
    ? "Copied inside the cluster, so nothing crosses the network."
    : "Transferred directly between the two clusters. This can take a while for a large instance.";

  const nameValid = /^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$/.test(targetName);
  const sameTarget =
    targetHost === instance.host && targetProject === project && targetName === instance.name;
  const overrideReady = !billingLinked || (acknowledged && reason.trim().length > 0);
  const canSubmit =
    !!targetHost &&
    !!targetProject &&
    nameValid &&
    !sameTarget &&
    !isReadOnly &&
    overrideReady &&
    !copyInstance.isPending;

  const handleSubmit = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      const result = await copyInstance.mutateAsync({
        instanceName: instance.name,
        body: {
          host: instance.host,
          project,
          target_host: targetHost,
          target_project: targetProject,
          target_name: targetName,
          target_pool: targetPool || null,
          reason: reason.trim() || undefined,
          acknowledge_billing_impact: billingLinked ? acknowledged : undefined,
        },
      });
      toast({ title: "Copy started", description: result.message });
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the copy.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] flex flex-col overflow-hidden sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Copy This Instance</DialogTitle>
          <DialogDescription>
            Place a copy of {instance.name} somewhere else. The original stays where it is.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {preflight.isLoading ? (
            <p className="text-sm text-muted-foreground">Checking where this can go...</p>
          ) : preflight.isError ? (
            <p className="text-sm text-destructive">
              {preflight.error instanceof Error
                ? preflight.error.message
                : "Could not read the destinations."}
            </p>
          ) : (
            <>
              {(preflight.data?.blockers ?? []).map((blocker) => (
                <p key={blocker} className="text-sm text-destructive">
                  {blocker}
                </p>
              ))}

              <div className="flex items-center gap-3 rounded-md border bg-muted/40 p-3 text-sm">
                <span className="font-mono">{instance.host}</span>
                <span className="text-muted-foreground">/</span>
                <span className="font-mono">{project}</span>
                <ArrowRight className="h-4 w-4 text-muted-foreground" aria-hidden />
                <span className="font-mono">{targetHost || "choose a cluster"}</span>
                {footprint ? (
                  <span className="ml-auto text-muted-foreground">
                    about {formatBytes(footprint)}
                  </span>
                ) : null}
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="move-cluster">Destination cluster</Label>
                  <SearchableSelect
                    id="move-cluster"
                    value={targetHost}
                    onValueChange={(next) => {
                      setTargetHost(next);
                      setTargetProject("");
                      setTargetPool("");
                    }}
                    options={hostOptions}
                    placeholder="Choose a cluster"
                    searchPlaceholder="Search clusters"
                    emptyText="No cluster available"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="move-project">Destination project</Label>
                  <SearchableSelect
                    id="move-project"
                    value={targetProject}
                    onValueChange={setTargetProject}
                    options={projectOptions}
                    placeholder="Choose a project"
                    searchPlaceholder="Search projects"
                    emptyText="No project available"
                    disabled={!targetHost}
                  />
                </div>
              </div>

              {poolOptions.length > 0 && (
                <div className="space-y-2">
                  <Label htmlFor="move-pool">Storage pool (optional)</Label>
                  <SearchableSelect
                    id="move-pool"
                    value={targetPool}
                    onValueChange={setTargetPool}
                    options={poolOptions}
                    placeholder="Use the destination default"
                    searchPlaceholder="Search pools"
                    emptyText="No pool available"
                    disabled={!targetHost}
                  />
                </div>
              )}

              <div className="space-y-2">
                <Label htmlFor="move-name">Name for the copy</Label>
                <Input
                  id="move-name"
                  value={targetName}
                  onChange={(e) => setTargetName(e.target.value)}
                  autoComplete="off"
                  aria-invalid={targetName.length > 0 && !nameValid}
                />
                {targetName.length > 0 && !nameValid && (
                  <p className="text-xs text-destructive">
                    Letters, digits and dashes only, starting with a letter or digit, up to 63
                    characters.
                  </p>
                )}
                {sameTarget && (
                  <p className="text-xs text-destructive">
                    That is where the instance already is.
                  </p>
                )}
              </div>

              {targetHost && (
                <div className="flex gap-3 rounded-md border bg-muted/40 p-3">
                  <Info className="h-4 w-4 shrink-0 mt-0.5 text-muted-foreground" aria-hidden />
                  <p className="text-sm text-muted-foreground">{transportLabel}</p>
                </div>
              )}

              {billingLinked && (
                <div className="space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-3">
                  <div className="flex gap-3">
                    <AlertTriangle className="h-4 w-4 shrink-0 text-destructive mt-0.5" aria-hidden />
                    <p className="text-sm">
                      This instance is snapshotted on a schedule another product bills, currently
                      {" "}
                      <span className="font-mono">{externalSchedule}</span>. Copying it duplicates
                      a workload someone else is accounting for.
                    </p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="move-reason">Reason</Label>
                    <Textarea
                      id="move-reason"
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      placeholder="Why this instance needs to be copied right now"
                      maxLength={280}
                      rows={3}
                    />
                  </div>

                  <div className="flex items-start gap-3">
                    <Checkbox
                      id="move-acknowledge"
                      checked={acknowledged}
                      onCheckedChange={(checked) => setAcknowledged(checked === true)}
                    />
                    <Label htmlFor="move-acknowledge" className="font-normal">
                      I accept the effect on the schedule this instance is billed under
                    </Label>
                  </div>
                </div>
              )}

              {isReadOnly && (
                <p className="text-sm text-muted-foreground">
                  The dashboard is in read-only mode, so no copy can be started.
                </p>
              )}

              {/* role="alert" so a submit failure is announced. Focus stays on the submit
                  button, which does not move, so without a live region a screen-reader user
                  is told nothing at all and the dialog simply appears not to respond.
                  DestructiveConfirmDialog already does this; these three did not. */}
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={copyInstance.isPending}
          >
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {copyInstance.isPending ? "Starting copy" : "Copy instance"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
