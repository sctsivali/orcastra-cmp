"use client";

import * as React from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  RotateCcw,
  Scissors,
  ShieldCheck,
  Trash2,
  Undo2,
  Unlock,
  XCircle,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
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
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowText } from "@/components/ui/overflow-text";
import { Progress } from "@/components/ui/progress";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useMigrationJob } from "@/hooks/use-migrations";
import { usePermissions } from "@/hooks/use-permissions";
import { useMigrationCapabilities } from "@/hooks/use-read-only-mode";
import { describeReadError } from "@/lib/read-error";
import { formatDateTimeSeconds, formatLabel, plural } from "@/lib/utils";
import type { MigrationAction, MigrationJob } from "@/types/migrations";
import { Role } from "@/types/rbac";
import { MigrationActionDialog, migrationActionCopy } from "./migration-action-dialog";
import { endpointLabel, jobProgress, migrationStatusMeta, strategyLabel } from "./migration-format";
import { MigrationResourceTable } from "./migration-resource-table";
import { MigrationRetentionPanel } from "./migration-retention-panel";

const ACTIONS: Record<
  MigrationAction,
  { label: string; icon: React.ComponentType<{ className?: string }>; destructive?: boolean }
> = {
  cancel: { label: "Cancel", icon: XCircle },
  cutover: { label: "Approve Cutover", icon: Scissors },
  confirm_external_cutover: { label: "Confirm Traffic Cutover", icon: CheckCircle2 },
  rollback: { label: "Roll Back", icon: Undo2, destructive: true },
  restore_checkpoint: { label: "Restore Checkpoint", icon: RotateCcw, destructive: true },
  cleanup: { label: "Finalize", icon: Trash2, destructive: true },
  recheck: { label: "Recheck", icon: RefreshCw },
  release: { label: "Release", icon: Unlock },
};

// Only an administrator can release a blocked job. The endpoint enforces the role too.
const ADMIN_ACTIONS = new Set<MigrationAction>(["release"]);

interface MigrationJobDialogProps {
  jobId: string | null;
  onOpenChange: (open: boolean) => void;
  writeDisabledReason?: string;
}

export function MigrationJobDialog({ jobId, onOpenChange, writeDisabledReason }: MigrationJobDialogProps) {
  const query = useMigrationJob(jobId, !!jobId);
  const [action, setAction] = React.useState<MigrationAction | null>(null);
  const isAdmin = usePermissions().hasRole(Role.ADMIN);
  const { autoCleanupEnabled } = useMigrationCapabilities();

  React.useEffect(() => setAction(null), [jobId]);

  const job = query.data;
  const meta = job ? migrationStatusMeta(job) : null;
  const overallProgress = job ? jobProgress(job) : null;

  return (
    <Dialog open={!!jobId} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden sm:max-w-5xl">
        <DialogHeader>
          <div className="flex flex-wrap items-start justify-between gap-3 pr-6">
            <div className="min-w-0">
              <DialogTitle>
                {job
                  ? `${job.scope === "project" ? "Relocate" : formatLabel(job.intent)} ${job.source.resource_name || job.source.project}`
                  : "Migration Details"}
              </DialogTitle>
              <DialogDescription>
                Durable migration state, authoritative resource evidence, and available operator actions.
              </DialogDescription>
              {job && (
                <div className="mt-1 max-w-xl text-xs text-muted-foreground">
                  <OverflowText value={job.job_id} label="migration job ID" className="font-mono" />
                </div>
              )}
            </div>
            {meta && <Badge variant={meta.tone}>{meta.label}</Badge>}
          </div>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {query.isLoading && !job ? (
            <div className="flex min-h-[360px] items-center justify-center" role="status">
              <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" aria-hidden />
              <span className="sr-only">Loading migration details</span>
            </div>
          ) : query.isError && !job ? (
            <EmptyState
              icon={AlertTriangle}
              title="Could not load migration"
              description={`The job could not be read: ${describeReadError(query.error).reason}.`}
              action={
                <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
                  Retry
                </Button>
              }
            />
          ) : job ? (
            <>
              {query.isError && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Live updates are interrupted</AlertTitle>
                  <AlertDescription>
                    Last known state is shown. The current job state could not be read: {describeReadError(query.error).reason}.
                  </AlertDescription>
                </Alert>
              )}

              {(job.phase === "manual_recovery" || job.status === "manual_recovery") && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Manual recovery required</AlertTitle>
                  <AlertDescription>
                    Orcastra stopped because it could not prove the next step is safe, and every
                    fence is still held. Inspect both endpoints, fix the cause, then use Recheck.
                  </AlertDescription>
                </Alert>
              )}
              {job.phase === "cleanup_due" && (
                <Alert>
                  <Trash2 className="h-4 w-4" aria-hidden />
                  <AlertTitle>Retention ended</AlertTitle>
                  <AlertDescription>
                    The source is still stopped and fenced. Use Finalize to delete it, or Roll Back
                    to restore it.
                  </AlertDescription>
                </Alert>
              )}
              {job.phase === "cleanup_blocked" && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Cleanup or rollback is blocked</AlertTitle>
                  <AlertDescription>
                    {job.scope === "project"
                      ? "A project changed during retention or a new instance failed its health check, so nothing was deleted. The reason is shown below."
                      : "The source changed during retention or the new instance failed its health check, so nothing was deleted. The reason is shown below."}
                  </AlertDescription>
                </Alert>
              )}
              {(job.status === "unknown" || job.phase === "unknown" || job.phase === "reconciling") && (
                <Alert>
                  <RefreshCw className="h-4 w-4" aria-hidden />
                  <AlertTitle>Operation outcome is being reconciled</AlertTitle>
                  <AlertDescription>
                    Orcastra will read authoritative daemon and resource state before offering a
                    retry. The original mutation will not be replayed automatically.
                  </AlertDescription>
                </Alert>
              )}
              {job.error_message && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Migration reported an error</AlertTitle>
                  <AlertDescription>{job.error_message}</AlertDescription>
                </Alert>
              )}

              <Tabs defaultValue="overview">
                <TabsList aria-label="Migration details" className="w-full justify-start overflow-x-auto">
                  <TabsTrigger value="overview">Overview</TabsTrigger>
                  <TabsTrigger value="resources">Resources</TabsTrigger>
                  <TabsTrigger value="timeline">Timeline</TabsTrigger>
                </TabsList>

                <TabsContent value="overview" className="space-y-4 pt-2">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <DetailField label="Source" value={endpointLabel(job.source)} mono />
                    <DetailField label="Destination" value={endpointLabel(job.target)} mono />
                    <DetailField label="Strategy" value={strategyLabel(job)} />
                    <DetailField label="Current Phase" value={formatLabel(job.phase)} />
                    <DetailField label="Started" value={formatDateTimeSeconds(job.created_at)} />
                    <DetailField
                      label={
                        job.intent === "copy" || job.strategy === "member_relocation"
                          ? "Recovery Window"
                          : "Source Retention Deadline"
                      }
                      value={
                        job.intent === "copy" || job.strategy === "member_relocation"
                          ? "Not applicable"
                          : job.retention_deadline
                            ? formatDateTimeSeconds(job.retention_deadline)
                            : "Not started"
                      }
                    />
                  </div>

                  <MigrationRetentionPanel job={job} autoCleanupEnabled={autoCleanupEnabled} />

                  {job.scope === "project" && <ProjectProgress job={job} />}

                  {overallProgress !== null && (
                    <div className="space-y-2 rounded-md border p-3">
                      <div className="flex items-center justify-between gap-2 text-sm">
                        <span className="font-medium">Overall progress</span>
                        <span>{Math.round(overallProgress)}%</span>
                      </div>
                      <Progress value={overallProgress} className="h-2" aria-label="Overall migration progress" />
                    </div>
                  )}

                  <Alert>
                    <ShieldCheck className="h-4 w-4" aria-hidden />
                    <AlertTitle>Authoritative phase</AlertTitle>
                    <AlertDescription>
                      {formatLabel(job.phase)}. Actions are offered only when the server reports
                      them as safe for this exact revision.
                    </AlertDescription>
                  </Alert>
                </TabsContent>

                <TabsContent value="resources" className="pt-2">
                  <MigrationResourceTable resources={job.resources ?? []} />
                </TabsContent>

                <TabsContent value="timeline" className="pt-2">
                  <Timeline job={job} />
                </TabsContent>
              </Tabs>
            </>
          ) : null}
        </DialogBody>

        <DialogFooter className="gap-2 border-t pt-4 sm:flex-wrap sm:space-x-0">
          {writeDisabledReason && job?.available_actions?.length ? (
            <span className="mr-auto text-xs text-muted-foreground">{writeDisabledReason}</span>
          ) : null}
          <Button className="min-h-11 w-full sm:min-h-10 sm:w-auto" variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {job?.available_actions?.map((availableAction) => {
            if (ADMIN_ACTIONS.has(availableAction) && !isAdmin) return null;
            if (availableAction === "cancel" && job.phase !== "queued" && job.phase !== "cutover_pending") {
              return null;
            }
            const config = ACTIONS[availableAction];
            if (!config) return null;
            const Icon = config.icon;
            const label = availableAction === "cancel"
              ? migrationActionCopy(job, availableAction).submit
              : config.label;
            return (
              <Button
                key={availableAction}
                className="min-h-11 w-full sm:min-h-10 sm:w-auto"
                variant={
                  config.destructive
                    ? "destructive"
                    : availableAction === "cutover"
                      ? "default"
                      : "outline"
                }
                onClick={() => setAction(availableAction)}
                disabled={!!writeDisabledReason || query.isError}
                title={writeDisabledReason}
              >
                <Icon className="mr-2 h-4 w-4" aria-hidden />
                {label}
              </Button>
            );
          })}
        </DialogFooter>
      </DialogContent>

      {job && (
        <MigrationActionDialog
          job={job}
          action={action}
          onOpenChange={(open) => !open && setAction(null)}
          onCompleted={() => setAction(null)}
        />
      )}
    </Dialog>
  );
}

/** How far a project job got, counted by instance: transferred, then running where it ran. */
function ProjectProgress({ job }: { job: MigrationJob }) {
  const instances = (job.resources ?? []).filter((resource) => resource.resource_type === "instance");
  if (!instances.length) return null;
  const done = instances.filter((resource) =>
    ["ready", "cutting_over", "retained", "completed"].includes(resource.state),
  ).length;
  const verb = job.strategy === "member_relocation" ? "moved" : "transferred";
  return (
    <p className="text-sm text-muted-foreground" aria-live="polite">
      {done} of {plural(instances.length, "instance")} {verb}
    </p>
  );
}

function DetailField({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-md border bg-muted/30 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-1 break-words text-sm font-medium ${mono ? "font-mono" : ""}`}>{value}</p>
    </div>
  );
}

function Timeline({ job }: { job: MigrationJob }) {
  const entries = job.timeline ?? [];
  if (entries.length === 0) {
    return (
      <EmptyState
        icon={RefreshCw}
        title="No timeline events reported"
        description="The current phase remains visible in Overview while the next durable event is recorded."
        className="py-8 rounded-md border"
      />
    );
  }
  return (
    <ol className="divide-y rounded-md border">
      {entries.map((entry) => (
        <li key={entry.sequence} className="grid gap-1 p-3 sm:grid-cols-[180px_minmax(0,1fr)]">
          <div>
            <p className="text-sm font-medium">{formatLabel(entry.phase)}</p>
            <p className="text-xs text-muted-foreground">{formatDateTimeSeconds(entry.created_at)}</p>
          </div>
          <div className="text-sm text-muted-foreground">
            {entry.message}
          </div>
        </li>
      ))}
    </ol>
  );
}
