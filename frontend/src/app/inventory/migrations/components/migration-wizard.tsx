"use client";

import * as React from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
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
import {
  useAllProjects,
  useClusterMembers,
  useClusters,
  useInstances,
  useStoragePoolList,
} from "@/hooks/use-lxd";
import {
  useCreateMigrationPlan,
  useExecuteMigrationPlan,
  useMigrationPlan,
  useUpdateMigrationPlan,
} from "@/hooks/use-migrations";
import { describeReadError } from "@/lib/read-error";
import { safeRandomUUID } from "@/lib/utils";
import type { MigrationJob, MigrationPlan, MigrationPlanUpdateRequest } from "@/types/migrations";
import type { StoragePoolListResponse } from "@/types/api";
import { MigrationPlanStages, requiredAcknowledgements } from "./migration-plan-stages";
import { MigrationSourceDestination } from "./migration-source-destination";
import {
  LXD_NAME_PATTERN,
  createPlanBody,
  draftFromPlan,
  initialMigrationDraft,
  type MigrationDraftState,
  type MigrationWizardSeed,
} from "./migration-wizard-model";
import { MigrationStepRail, MigrationStepStatus } from "./migration-step-rail";

const STEP_COPY = [
  ["Choose the source", "Select one instance or a complete project environment."],
  ["Choose the destination", "Select a compatible target and its placement defaults."],
  ["Review the strategy", "The server derives transfer mode and expected downtime."],
  ["Resolve mappings", "Map every infrastructure dependency before cutover."],
  ["Review preflight", "Resolve every blocker and understand each warning."],
  ["Set recovery policy", "Choose source retention and external cutover gating."],
  ["Approve execution", "Record the reason and acknowledge operational impact."],
] as const;

interface MigrationWizardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  seed?: MigrationWizardSeed;
  planId?: string | null;
  onPlanIdChange: (planId: string | null) => void;
  onStarted: (job: MigrationJob) => void;
  writeDisabledReason?: string;
  rollout: {
    durableCopyEnabled?: boolean;
    instanceMoveEnabled?: boolean;
    projectMemberRelocationEnabled?: boolean;
    projectCrossClusterEnabled?: boolean;
  };
}

export function MigrationWizard({
  open,
  onOpenChange,
  seed,
  planId,
  onPlanIdChange,
  onStarted,
  writeDisabledReason,
  rollout,
}: MigrationWizardProps) {
  const [step, setStep] = React.useState(0);
  const [draft, setDraft] = React.useState<MigrationDraftState>(() => initialMigrationDraft(seed));
  const [plan, setPlan] = React.useState<MigrationPlan | null>(null);
  const [planDirty, setPlanDirty] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const loadedPlanRef = React.useRef<string | null>(null);
  const preserveDraftOnDetachRef = React.useRef(false);
  const submitInFlightRef = React.useRef(false);
  const executionAttemptRef = React.useRef<{ signature: string; key: string } | null>(null);

  const clusterQuery = useClusters();
  const projectQuery = useAllProjects({ enabled: open });
  const instanceQuery = useInstances(draft.sourceCluster, {
    enabled: open && draft.scope === "instance" && !!draft.sourceCluster && !!draft.sourceProject,
    project: draft.sourceProject,
  });
  const memberQuery = useClusterMembers(open ? draft.targetCluster : "");
  const poolQuery = useStoragePoolList(draft.targetCluster, {
    enabled: open && !!draft.targetCluster,
  });
  const planQuery = useMigrationPlan(planId ?? null, open && !!planId);
  const createPlan = useCreateMigrationPlan();
  const updatePlan = useUpdateMigrationPlan();
  const executePlan = useExecuteMigrationPlan();
  const pending = createPlan.isPending || updatePlan.isPending || executePlan.isPending;

  React.useEffect(() => {
    if (!open || planId) return;
    if (preserveDraftOnDetachRef.current) {
      preserveDraftOnDetachRef.current = false;
      return;
    }
    loadedPlanRef.current = null;
    setStep(0);
    setDraft(initialMigrationDraft(seed));
    setPlan(null);
    setPlanDirty(false);
    setError(null);
  }, [
    open,
    planId,
    seed?.mode,
    seed?.resourceName,
    seed?.scope,
    seed?.sourceCluster,
    seed?.sourceProject,
  ]);

  React.useEffect(() => {
    if (!open || !planId || loadedPlanRef.current === planId) return;
    setPlan(null);
    setPlanDirty(false);
    setStep(4);
    setError(null);
  }, [open, planId]);

  React.useEffect(() => {
    if (!open || !planId || !planQuery.data || loadedPlanRef.current === planQuery.data.plan_id) return;
    loadedPlanRef.current = planQuery.data.plan_id;
    setDraft(draftFromPlan(planQuery.data));
    setPlan(planQuery.data);
    setPlanDirty(false);
    setStep(4);
    setError(null);
  }, [open, planId, planQuery.data]);

  const updateDraft = React.useCallback((patch: Partial<MigrationDraftState>, invalidate = false) => {
    setDraft((current) => ({ ...current, ...patch }));
    setError(null);
    if (
      Object.keys(patch).some((key) =>
        [
          "targetCluster",
          "targetProject",
          "targetResourceName",
          "targetMember",
          "targetStoragePool",
          "mappings",
          "retentionHours",
          "checkpointRequested",
          "externalCutoverRequired",
        ].includes(key),
      )
    ) {
      setPlanDirty(true);
    }
    if (invalidate) {
      const detachingPersistedPlan = !!planId;
      if (detachingPersistedPlan) preserveDraftOnDetachRef.current = true;
      loadedPlanRef.current = null;
      setPlan(null);
      setPlanDirty(false);
      if (detachingPersistedPlan) onPlanIdChange(null);
    }
  }, [onPlanIdChange, planId]);

  const sourceResourcePresent =
    draft.scope === "project" ||
    (instanceQuery.data?.vms ?? []).some((instance) => instance.name === draft.resourceName);
  const sourceValid =
    !!draft.sourceCluster &&
    !!draft.sourceProject.trim() &&
    (draft.scope === "project" || !!draft.resourceName.trim()) &&
    sourceResourcePresent;
  const sameLocation =
    draft.targetCluster === draft.sourceCluster &&
    draft.targetProject.trim() === draft.sourceProject.trim() &&
    (draft.scope === "project" || draft.targetResourceName.trim() === draft.resourceName.trim()) &&
    (!draft.targetMember.trim() || draft.targetMember.trim() === draft.sourceMember.trim());
  const destinationValid =
    !!draft.targetCluster &&
    LXD_NAME_PATTERN.test(draft.targetProject.trim()) &&
    (draft.scope === "project" || LXD_NAME_PATTERN.test(draft.targetResourceName.trim())) &&
    !sameLocation;
  const rolloutDisabledReason =
    draft.intent === "copy" && rollout.durableCopyEnabled === false
      ? "Durable copy is disabled by rollout policy"
      : draft.intent === "move" && draft.scope === "instance" && rollout.instanceMoveEnabled === false
        ? "Instance move is disabled by rollout policy"
        : draft.intent === "move" && draft.scope === "project" && draft.targetCluster === draft.sourceCluster && rollout.projectMemberRelocationEnabled === false
          ? "Project workload relocation is disabled by rollout policy"
          : draft.intent === "move" && draft.scope === "project" && !!draft.targetCluster && draft.targetCluster !== draft.sourceCluster && rollout.projectCrossClusterEnabled === false
            ? "Cross-cluster project relocation is disabled by rollout policy"
            : undefined;
  const expired = plan ? Date.parse(plan.expires_at) <= Date.now() : false;
  const requiredAcks = requiredAcknowledgements(draft, plan?.strategy);
  const reviewValid =
    draft.reason.trim().length >= 8 &&
    requiredAcks.every((acknowledgement) => draft.acknowledgements.includes(acknowledgement)) &&
    !(plan?.blockers?.length) &&
    !expired;

  const startPreflight = async () => {
    const nextPlan = await createPlan.mutateAsync(createPlanBody(draft));
    setPlan(nextPlan);
    setPlanDirty(false);
    setDraft((current) => ({ ...current, mappings: nextPlan.mappings }));
    loadedPlanRef.current = nextPlan.plan_id;
    onPlanIdChange(nextPlan.plan_id);
    return nextPlan;
  };

  const patchPlan = async (body: MigrationPlanUpdateRequest) => {
    if (!plan) return startPreflight();
    const nextPlan = await updatePlan.mutateAsync({ planId: plan.plan_id, revision: plan.revision, body });
    setPlan(nextPlan);
    setPlanDirty(false);
    setDraft((current) => ({ ...current, mappings: nextPlan.mappings }));
    return nextPlan;
  };

  const advance = async () => {
    if (submitInFlightRef.current) return;
    setError(null);
    if (step === 0) {
      setStep(1);
      return;
    }
    submitInFlightRef.current = true;
    try {
      if (step === 1) {
        const nextBody = createPlanBody(draft);
        if (plan && !expired) {
          await patchPlan({ target: nextBody.target, mappings: nextBody.mappings });
        } else {
          await startPreflight();
        }
        setStep(2);
      } else if (step === 2) {
        setStep(3);
      } else if (step === 3) {
        await patchPlan({ mappings: draft.mappings });
        setStep(4);
      } else if (step === 4) {
        setStep(5);
      } else if (step === 5) {
        await patchPlan({
          retention_hours: draft.retentionHours,
          checkpoint_requested: draft.checkpointRequested,
          external_cutover_required: draft.externalCutoverRequired,
        });
        setStep(6);
      } else if (step === 6 && plan) {
        const body = { reason: draft.reason.trim(), acknowledgements: draft.acknowledgements };
        const signature = JSON.stringify({
          planId: plan.plan_id,
          revision: plan.revision,
          body,
        });
        const attempt = executionAttemptRef.current?.signature === signature
          ? executionAttemptRef.current
          : { signature, key: safeRandomUUID() };
        executionAttemptRef.current = attempt;
        const job = await executePlan.mutateAsync({
          planId: plan.plan_id,
          revision: plan.revision,
          idempotencyKey: attempt.key,
          body,
        });
        onStarted(job);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The migration request failed.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  const recheck = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      const nextPlan = expired
        ? await startPreflight()
        : await patchPlan({
            mappings: createPlanBody(draft).mappings,
            checkpoint_requested: draft.checkpointRequested,
            retention_hours: draft.retentionHours,
            external_cutover_required: draft.externalCutoverRequired,
          });
      setPlan(nextPlan);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Preflight could not be refreshed.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  const saveAndClose = async () => {
    if (submitInFlightRef.current) return;
    if (!plan || !planDirty) {
      onOpenChange(false);
      return;
    }
    submitInFlightRef.current = true;
    setError(null);
    try {
      if (expired) {
        await startPreflight();
      } else {
        const body = createPlanBody(draft);
        await patchPlan({
          target: body.target,
          mappings: body.mappings,
          retention_hours: body.retention_hours,
          checkpoint_requested: body.checkpoint_requested,
          external_cutover_required: body.external_cutover_required,
        });
      }
      onOpenChange(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The migration draft could not be saved.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  const inventoryErrors = [
    clusterQuery.isError
      ? `Clusters could not be read: ${describeReadError(clusterQuery.error).reason}.`
      : null,
    projectQuery.isError
      ? `Projects could not be read: ${describeReadError(projectQuery.error).reason}.`
      : null,
    step === 0 && draft.scope === "instance" && instanceQuery.isError
      ? `Source instances could not be read: ${describeReadError(instanceQuery.error).reason}.`
      : null,
    step === 1 && memberQuery.isError
      ? `Destination members could not be read: ${describeReadError(memberQuery.error).reason}.`
      : null,
    step === 1 && poolQuery.isError
      ? `Destination storage pools could not be read: ${describeReadError(poolQuery.error).reason}.`
      : null,
  ].filter((message): message is string => !!message);
  const inventoryError = inventoryErrors.length ? inventoryErrors.join(" ") : null;
  const executionDisabledReason = writeDisabledReason ?? rolloutDisabledReason;
  const canAdvance =
    !pending &&
    (step === 0
      ? sourceValid && !inventoryError
      : step === 1
        ? destinationValid && !inventoryError
        : step === 4
          ? !!plan && !plan.blockers?.length && !expired
          : step === 6
            ? !executionDisabledReason && reviewValid
            : !!plan);
  const [title, description] =
    draft.intent === "copy" && step === 5
      ? (["Confirm the copy outcome", "Review how the independent destination affects the active source."] as const)
      : STEP_COPY[step] ?? STEP_COPY[6];

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (pending) return;
        if (next) onOpenChange(true);
        else void saveAndClose();
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <DialogBody className="min-h-0 p-0">
          <div className="grid min-h-[520px] md:grid-cols-[220px_minmax(0,1fr)]">
            <MigrationStepRail
              step={step}
              intent={draft.intent}
              title={title}
              pending={pending}
              onStepChange={setStep}
            />

            <div className="min-w-0 space-y-4 overflow-y-auto p-4 sm:p-6">
              <MigrationStepStatus step={step} intent={draft.intent} title={title} />

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
                      plan expired or was removed, start a separate preflight.
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
                        {planQuery.isFetching && (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
                        )}
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
                  onChange={(patch) => updateDraft(patch, step === 0)}
                  clusters={clusterQuery.data?.clusters ?? []}
                  projects={projectQuery.data ?? []}
                  instances={instanceQuery.data?.vms ?? []}
                  targetMembers={memberQuery.data?.members ?? []}
                  targetPools={(poolQuery.data as StoragePoolListResponse | undefined)?.pools ?? []}
                  inventoryLoading={clusterQuery.isLoading || projectQuery.isLoading}
                  resourceLoading={instanceQuery.isLoading || memberQuery.isLoading || poolQuery.isLoading}
                  inventoryError={inventoryError}
                />
              ) : plan ? (
                <MigrationPlanStages
                  stage={(["strategy", "mappings", "preflight", "recovery", "review"] as const)[step - 2]}
                  plan={plan}
                  draft={draft}
                  onChange={(patch) => updateDraft(patch)}
                />
              ) : null}

              {writeDisabledReason && (
                <Alert>
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>Execution is unavailable</AlertTitle>
                  <AlertDescription>
                    {writeDisabledReason}. You can continue planning and run side-effect-free preflight.
                  </AlertDescription>
                </Alert>
              )}
              {rolloutDisabledReason && (
                <Alert>
                  <AlertTriangle className="h-4 w-4" aria-hidden />
                  <AlertTitle>This execution path is not enabled</AlertTitle>
                  <AlertDescription>
                    {rolloutDisabledReason}. You can save and recheck the plan while rollout is read-only.
                  </AlertDescription>
                </Alert>
              )}
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
            </div>
          </div>
        </DialogBody>

        <DialogFooter className="gap-2 border-t pt-4 sm:flex-wrap sm:space-x-0">
          <Button
            className="min-h-11 w-full sm:min-h-10 sm:w-auto"
            variant="outline"
            onClick={() => void saveAndClose()}
            disabled={pending}
          >
            {plan && planDirty ? "Save and close" : "Close"}
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
          {step === 4 && (
            <Button className="min-h-11 w-full sm:min-h-10 sm:w-auto" variant="outline" onClick={recheck} disabled={pending}>
              {pending ? "Checking" : "Recheck"}
            </Button>
          )}
          <Button className="min-h-11 w-full sm:min-h-10 sm:w-auto" onClick={advance} disabled={!canAdvance}>
            {pending ? "Working" : step === 6 ? "Start migration" : "Continue"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
