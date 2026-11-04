"use client";

import Link from "next/link";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { ToastAction } from "@/components/ui/toast";
import type { WriteAvailability } from "@/hooks/use-read-only-mode";
import { toast } from "@/hooks/use-toast";
import { inventoryLinks } from "@/lib/inventory-links";
import { describeReadError } from "@/lib/read-error";
import type { NetworkListResponse, StoragePoolListResponse } from "@/types/api";
import type { MigrationJob } from "@/types/migrations";
import { MigrationPlanStages } from "./migration-plan-stages";
import { MigrationSourceDestination } from "./migration-source-destination";
import { MigrationStepRail, MigrationStepStatus } from "./migration-step-rail";
import type { MigrationWizardSeed } from "./migration-wizard-model";
import {
  useMigrationWizard,
  type MigrationDetachedResult,
  type MigrationRollout,
} from "./use-migration-wizard";

const STEP_COPY = [
  ["Choose the source", "Select one instance or a complete project environment."],
  ["Choose the destination", "Select the cluster, project, and placement for the result."],
  ["Review the strategy", "The server derives the transfer mode and expected downtime."],
  ["Review mappings", "Check where each network, profile, and disk lands."],
  ["Review preflight", "Resolve every blocker and understand each warning."],
  ["Set recovery policy", "Choose how long the stopped source is kept for rollback."],
  ["Approve execution", "Record the reason and acknowledge the operational impact."],
] as const;

const PLAN_STAGES = ["strategy", "mappings", "preflight", "recovery", "review"] as const;

/**
 * Where a draft or a started migration went when browser Back closed the wizard before the server
 * answered. The wizard does not reopen itself for it, and the page under it may be another one.
 */
function announceDetached(result: MigrationDetachedResult) {
  const draft = result.kind === "plan";
  const href = inventoryLinks.migrations(draft ? { plan: result.planId } : { job: result.jobId });
  toast({
    title: draft ? "Migration draft saved" : "Migration started",
    description: draft
      ? "Preflight finished after the wizard was closed. The draft is listed under Drafts."
      : "The migration started after the wizard was closed.",
    action: (
      <ToastAction asChild altText={draft ? "Open the migration draft" : "Open the migration"}>
        <Link href={href}>{draft ? "Open Draft" : "Open Migration"}</Link>
      </ToastAction>
    ),
  });
}

interface MigrationWizardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  seed?: MigrationWizardSeed;
  planId?: string | null;
  onPlanIdChange: (planId: string | null) => void;
  onStarted: (job: MigrationJob) => void;
  writeDisabledReason?: string;
  /** The verdict behind writeDisabledReason; the notice stays hidden while it is first read. */
  writeAvailability?: WriteAvailability;
  onRecheckWrite?: () => void;
  rollout: MigrationRollout;
  initialStep?: number | null;
  onStepChange?: (step: number) => void;
  /** The job a started plan created, when the loaded queue has it. */
  startedJobId?: string | null;
  onOpenJob?: (jobId: string) => void;
}

export function MigrationWizard(props: MigrationWizardProps) {
  const {
    open,
    onPlanIdChange,
    planId,
    writeDisabledReason,
    writeAvailability,
    onRecheckWrite,
    startedJobId,
    onOpenJob,
  } = props;
  const showWriteNotice = !!writeDisabledReason && writeAvailability !== "checking";
  const wizard = useMigrationWizard({ ...props, onDetached: announceDetached });
  const { step, setStep, draft, plan, pending, queries } = wizard;
  const [title, description] =
    draft.intent === "copy" && step === 5
      ? (["Confirm the copy outcome", "A copy leaves the source as it is."] as const)
      : STEP_COPY[step] ?? STEP_COPY[6];
  const planQuery = queries.planQuery;
  const networks = (queries.networkQuery.data as NetworkListResponse | undefined)?.networks ?? [];

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (pending) return;
        if (next) props.onOpenChange(true);
        else void wizard.saveAndClose();
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <DialogBody className="min-h-0 p-0">
          <div className="grid min-h-[520px] md:grid-cols-[220px_minmax(0,1fr)]">
            <MigrationStepRail step={step} intent={draft.intent} title={title} pending={pending} onStepChange={setStep} />

            <div className="min-w-0 space-y-4 overflow-y-auto p-4 sm:p-6">
              <MigrationStepStatus step={step} intent={draft.intent} title={title} />

              {wizard.executed && (
                <Alert>
                  <Info className="h-4 w-4" aria-hidden />
                  <AlertTitle>This plan was started</AlertTitle>
                  <AlertDescription className="space-y-3">
                    <p>
                      A started plan cannot be changed or started again.{" "}
                      {startedJobId
                        ? "Its migration shows the progress and the actions still available."
                        : "Its migration is listed in the Migration Queue."}
                    </p>
                    {startedJobId && onOpenJob ? (
                      <Button type="button" variant="outline" size="sm" onClick={() => onOpenJob(startedJobId)}>
                        Open Migration
                      </Button>
                    ) : null}
                  </AlertDescription>
                </Alert>
              )}

              {planId && planQuery.isLoading && !plan ? (
                <div className="flex min-h-[320px] items-center justify-center" role="status">
                  <Loader2 className="h-7 w-7 animate-spin text-muted-foreground" aria-hidden />
                  <span className="sr-only">Loading migration draft</span>
                </div>
              ) : planId && planQuery.isError && !plan ? (
                <Alert variant="destructive" aria-live="assertive">
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Draft could not be restored</AlertTitle>
                  <AlertDescription className="space-y-3">
                    <p>
                      {describeReadError(planQuery.error).reason}. Retry keeps this draft URL. If the
                      plan expired or was removed, start a new one.
                    </p>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="w-full sm:w-auto"
                        onClick={() => void planQuery.refetch()}
                        disabled={planQuery.isFetching}
                      >
                        {planQuery.isFetching && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />}
                        {planQuery.isFetching ? "Retrying" : "Retry draft"}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="w-full sm:w-auto"
                        onClick={() => onPlanIdChange(null)}
                        disabled={planQuery.isFetching}
                      >
                        Start a new plan
                      </Button>
                    </div>
                  </AlertDescription>
                </Alert>
              ) : step < 2 ? (
                <MigrationSourceDestination
                  stage={step === 0 ? "source" : "destination"}
                  draft={draft}
                  onChange={(patch) => wizard.updateDraft(patch, step === 0)}
                  clusters={queries.clusterQuery.data?.clusters ?? []}
                  projects={queries.projectQuery.data ?? []}
                  instances={queries.instanceQuery.data?.vms ?? []}
                  targetMembers={queries.memberQuery.data?.members ?? []}
                  targetPools={(queries.poolQuery.data as StoragePoolListResponse | undefined)?.pools ?? []}
                  inventoryLoading={queries.clusterQuery.isLoading}
                  projectsLoading={queries.projectQuery.isLoading}
                  resourceLoading={
                    queries.instanceQuery.isLoading || queries.memberQuery.isLoading || queries.poolQuery.isLoading
                  }
                  inventoryError={wizard.inventoryError}
                />
              ) : plan ? (
                <MigrationPlanStages
                  stage={PLAN_STAGES[step - 2]}
                  plan={plan}
                  draft={draft}
                  onChange={(patch) => wizard.updateDraft(patch)}
                  networks={networks}
                  networksLoading={queries.networkQuery.isLoading}
                />
              ) : null}

              {wizard.notice && (
                <Alert>
                  <Info className="h-4 w-4" aria-hidden />
                  <AlertTitle>The plan was updated</AlertTitle>
                  <AlertDescription>{wizard.notice}</AlertDescription>
                </Alert>
              )}
              {showWriteNotice && (
                <Alert>
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Execution is unavailable</AlertTitle>
                  <AlertDescription>
                    {writeDisabledReason}. A saved draft can still be reviewed and rechecked.
                    {writeAvailability === "unavailable" && onRecheckWrite ? (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="mt-2 block"
                        onClick={onRecheckWrite}
                      >
                        Check Again
                      </Button>
                    ) : null}
                  </AlertDescription>
                </Alert>
              )}
              {wizard.rolloutDisabledReason && (
                <Alert>
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>This migration cannot be started here</AlertTitle>
                  <AlertDescription>{wizard.rolloutDisabledReason}. The plan can still be reviewed.</AlertDescription>
                </Alert>
              )}
            </div>
          </div>
        </DialogBody>

        <DialogSubmitError message={wizard.error} className="mb-4" />

        <DialogFooter className="gap-2 border-t pt-4 sm:flex-wrap sm:space-x-0">
          <Button
            className="min-h-11 w-full sm:min-h-10 sm:w-auto"
            variant="outline"
            onClick={() => void wizard.saveAndClose()}
            disabled={pending}
          >
            {plan && wizard.planDirty && !wizard.executed ? "Save and Close" : "Close"}
          </Button>
          <div className="hidden sm:block sm:flex-1" />
          <Button
            className="min-h-11 w-full sm:min-h-10 sm:w-auto"
            variant="outline"
            onClick={() => setStep((current) => Math.max(0, current - 1))}
            disabled={step === 0 || pending}
          >
            Back
          </Button>
          {(step === 4 || step === 6) && plan && !wizard.executed && (
            <Button
              className="min-h-11 w-full sm:min-h-10 sm:w-auto"
              variant="outline"
              onClick={() => void wizard.recheck()}
              disabled={pending}
            >
              {pending ? "Checking" : "Recheck"}
            </Button>
          )}
          <Button
            className="min-h-11 w-full sm:min-h-10 sm:w-auto"
            onClick={() => void wizard.advance()}
            disabled={!wizard.canAdvance}
          >
            {pending
              ? "Working"
              : step === 6
                ? draft.intent === "copy"
                  ? "Start copy"
                  : draft.scope === "project"
                    ? "Start relocation"
                    : "Start move"
                : "Continue"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
