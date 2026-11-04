"use client";

import * as React from "react";
import {
  useAllProjects,
  useClusterMembers,
  useClusters,
  useInstances,
  useNetworks,
  useStoragePoolList,
} from "@/hooks/use-lxd";
import {
  useCreateMigrationPlan,
  useExecuteMigrationPlan,
  useMigrationPlan,
  useUpdateMigrationPlan,
} from "@/hooks/use-migrations";
import { toast } from "@/hooks/use-toast";
import { migrationsAPI } from "@/lib/migrations-api";
import { describeReadError } from "@/lib/read-error";
import { safeRandomUUID } from "@/lib/utils";
import type { MigrationJob, MigrationPlan, MigrationPlanUpdateRequest } from "@/types/migrations";
import { requiredAcknowledgements } from "./migration-stage-review";
import {
  LXD_NAME_PATTERN,
  createPlanBody,
  draftFromPlan,
  initialMigrationDraft,
  isSameLocation,
  mappingProblem,
  parseRetention,
  projectDestinationProblem,
  type MigrationDraftState,
  type MigrationWizardSeed,
} from "./migration-wizard-model";

/** A create or start that finished after browser Back closed the wizard or left the page. */
export type MigrationDetachedResult = { kind: "plan"; planId: string } | { kind: "job"; jobId: string };

export interface MigrationRollout {
  durableCopyEnabled?: boolean;
  instanceMoveEnabled?: boolean;
  projectMemberRelocationEnabled?: boolean;
  projectCrossClusterEnabled?: boolean;
  defaultRetentionHours?: number;
}

const DRAFT_PLAN_KEYS: ReadonlyArray<keyof MigrationDraftState> = [
  "targetCluster",
  "targetProject",
  "targetResourceName",
  "targetMember",
  "targetStoragePool",
  "mappings",
  "retentionHours",
  "checkpointRequested",
  "externalCutoverRequired",
];

function rolloutReason(draft: MigrationDraftState, rollout: MigrationRollout): string | undefined {
  if (draft.scope === "project") {
    const memberMove = draft.projectMode === "member";
    const enabled = memberMove ? rollout.projectMemberRelocationEnabled : rollout.projectCrossClusterEnabled;
    return enabled === true
      ? undefined
      : `${memberMove ? "Relocate Workloads" : "Relocate Project"} is turned off on this deployment`;
  }
  if (draft.intent === "copy" && rollout.durableCopyEnabled === false) {
    return "Copy Instance is turned off on this deployment";
  }
  if (draft.intent === "move" && rollout.instanceMoveEnabled === false) {
    return "Move Instance is turned off on this deployment";
  }
  return undefined;
}

function isRevisionConflict(message: string): boolean {
  return /Reload and approve plan revision|plan changed/i.test(message);
}

function asSentence(message: string): string {
  return /[.!?]$/.test(message.trim()) ? message.trim() : `${message.trim()}.`;
}

export function useMigrationWizard({
  open,
  seed,
  planId,
  onPlanIdChange,
  onStarted,
  onOpenChange,
  writeDisabledReason,
  rollout,
  initialStep,
  onStepChange,
  onDetached,
}: {
  open: boolean;
  seed?: MigrationWizardSeed;
  planId?: string | null;
  onPlanIdChange: (planId: string | null) => void;
  onStarted: (job: MigrationJob) => void;
  onOpenChange: (open: boolean) => void;
  writeDisabledReason?: string;
  rollout: MigrationRollout;
  /** The step a plan link names. A plan opened without one starts at Preflight. */
  initialStep?: number | null;
  onStepChange?: (step: number) => void;
  /** Says where a result went when the wizard that asked for it is no longer open. */
  onDetached?: (result: MigrationDetachedResult) => void;
}) {
  const defaultRetention = rollout.defaultRetentionHours ?? 24;
  const [step, setStep] = React.useState(0);
  const [draft, setDraft] = React.useState<MigrationDraftState>(() =>
    initialMigrationDraft(seed, defaultRetention),
  );
  const [plan, setPlan] = React.useState<MigrationPlan | null>(null);
  const [planDirty, setPlanDirty] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const loadedPlanRef = React.useRef<string | null>(null);
  const preserveDraftOnDetachRef = React.useRef(false);
  const submitInFlightRef = React.useRef(false);
  const executionAttemptRef = React.useRef<{ signature: string; key: string } | null>(null);
  // important: bumped when the wizard opens, closes or unmounts. A request still running when
  // browser Back closed the wizard, or left the page, sees another value afterwards, and must
  // not reopen the wizard, write a URL, or adopt its answer into a wizard started since.
  const sessionRef = React.useRef(0);
  React.useEffect(() => {
    sessionRef.current += 1;
    // A plan opened again later is read from the cache, which every save keeps current, rather
    // than from this session's state.
    if (!open) loadedPlanRef.current = null;
    return () => {
      sessionRef.current += 1;
    };
  }, [open]);

  const clusterQuery = useClusters();
  const projectQuery = useAllProjects({ enabled: open });
  const instanceQuery = useInstances(draft.sourceCluster, {
    enabled: open && draft.scope === "instance" && !!draft.sourceCluster && !!draft.sourceProject,
    project: draft.sourceProject,
  });
  const memberQuery = useClusterMembers(open ? draft.targetCluster : "");
  const poolQuery = useStoragePoolList(draft.targetCluster, { enabled: open && !!draft.targetCluster });
  const targetProjectExists = (projectQuery.data ?? []).some(
    (entry) =>
      entry.host === draft.targetCluster &&
      entry.projects.some((project) => project.name === draft.targetProject),
  );
  const networkQuery = useNetworks(
    draft.targetCluster || undefined,
    targetProjectExists ? draft.targetProject : "default",
    { enabled: open && !!draft.targetCluster && step >= 3 },
  );
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
    setDraft(initialMigrationDraft(seed, defaultRetention));
    setPlan(null);
    setPlanDirty(false);
    setError(null);
    setNotice(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, planId, seed?.mode, seed?.resourceName, seed?.scope, seed?.intent, seed?.sourceCluster, seed?.sourceProject, seed?.sourceMember]);

  React.useEffect(() => {
    if (!open || !planId || loadedPlanRef.current === planId) return;
    setPlan(null);
    setPlanDirty(false);
    setStep(initialStep ?? 4);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, planId]);

  React.useEffect(() => {
    if (!open || !planId || !planQuery.data || loadedPlanRef.current === planQuery.data.plan_id) return;
    loadedPlanRef.current = planQuery.data.plan_id;
    setDraft(draftFromPlan(planQuery.data));
    setPlan(planQuery.data);
    setPlanDirty(false);
    setStep(initialStep ?? 4);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, planId, planQuery.data]);

  // Keep the plan link on the step in view. Only once the plan is this wizard's own, so a link
  // being loaded is not overwritten by the step shown while it loads.
  React.useEffect(() => {
    if (open && planId && loadedPlanRef.current === planId) onStepChange?.(step);
  }, [open, planId, step, onStepChange, plan]);

  // A deep link names the instance but not its member. Fill it from inventory once known.
  const sourceInstance = (instanceQuery.data?.vms ?? []).find((instance) => instance.name === draft.resourceName);
  React.useEffect(() => {
    if (draft.scope !== "instance" || draft.sourceMember || !sourceInstance?.location) return;
    setDraft((current) =>
      current.sourceMember || current.resourceName !== sourceInstance.name
        ? current
        : { ...current, sourceMember: sourceInstance.location ?? "" },
    );
  }, [draft.scope, draft.sourceMember, sourceInstance?.location, sourceInstance?.name]);

  const updateDraft = React.useCallback(
    (patch: Partial<MigrationDraftState>, invalidate = false) => {
      setDraft((current) => ({ ...current, ...patch }));
      setError(null);
      if (Object.keys(patch).some((key) => DRAFT_PLAN_KEYS.includes(key as keyof MigrationDraftState))) {
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
    },
    [onPlanIdChange, planId],
  );

  const sourceValid =
    !!draft.sourceCluster &&
    !!draft.sourceProject.trim() &&
    (draft.scope === "project" || (!!draft.resourceName.trim() && !!sourceInstance));
  const sameLocation = isSameLocation(draft);
  const targetProjects = (projectQuery.data ?? [])
    .filter((entry) => entry.host === draft.targetCluster)
    .flatMap((entry) => entry.projects.map((project) => project.name));
  const destinationValid =
    draft.scope === "project"
      ? !projectDestinationProblem(draft, draft.projectMode === "member" ? [] : targetProjects)
      : !!draft.targetCluster &&
        LXD_NAME_PATTERN.test(draft.targetProject.trim()) &&
        LXD_NAME_PATTERN.test(draft.targetResourceName.trim()) &&
        !sameLocation;
  const rolloutDisabledReason = rolloutReason(draft, rollout);
  // A started plan is history: the server refuses to change or start it again.
  const executed = plan?.status === "executed";
  const expired = plan && !executed ? Date.parse(plan.expires_at) <= Date.now() : false;
  const retentionValid = parseRetention(draft.retentionInput) !== null;
  const mappingIssue = mappingProblem(draft.mappings);
  const requiredAcks = requiredAcknowledgements(draft, plan);
  const reviewValid =
    draft.reason.trim().length >= 8 &&
    requiredAcks.every((acknowledgement) => draft.acknowledgements.includes(acknowledgement)) &&
    !plan?.blockers?.length &&
    !expired;

  const adoptPlan = (nextPlan: MigrationPlan) => {
    setPlan(nextPlan);
    setPlanDirty(false);
    setDraft((current) => ({ ...current, mappings: nextPlan.mappings }));
  };

  type Live = () => boolean;

  /** Creates the plan. Null when the wizard was closed while preflight ran. */
  const startPreflight = async (live: Live) => {
    const nextPlan = await createPlan.mutateAsync(createPlanBody(draft));
    if (!live()) {
      onDetached?.({ kind: "plan", planId: nextPlan.plan_id });
      return null;
    }
    adoptPlan(nextPlan);
    loadedPlanRef.current = nextPlan.plan_id;
    onPlanIdChange(nextPlan.plan_id);
    return nextPlan;
  };

  // note: an expired plan is patched too. The server re-runs preflight and renews the plan in
  // place, where a new POST would leave the expired draft behind as a duplicate.
  const patchPlan = async (body: MigrationPlanUpdateRequest, live: Live) => {
    if (!plan) return startPreflight(live);
    const nextPlan = await updatePlan.mutateAsync({ planId: plan.plan_id, revision: plan.revision, body });
    if (!live()) return null;
    adoptPlan(nextPlan);
    return nextPlan;
  };

  // The server has a newer revision. Its plan wins: a retention or destination edited here and
  // lost to the conflict must not be shown as what Start would run. Only the reason is kept.
  const reloadAfterConflict = async (message: string, live: Live) => {
    if (!plan) return;
    try {
      const fresh = await migrationsAPI.getPlan(plan.plan_id);
      if (!live()) return;
      setPlan(fresh);
      setPlanDirty(false);
      setDraft((current) => ({ ...draftFromPlan(fresh), reason: current.reason }));
      if (fresh.blockers?.length) setStep((current) => Math.min(current, 4));
      setNotice(
        `Preflight changed since you reviewed it and is now revision ${fresh.revision}. ` +
          "Review the findings and confirm again.",
      );
    } catch (caught) {
      if (live()) setError(`${message} The new revision could not be loaded: ${describeReadError(caught).reason}.`);
    }
  };

  const run = async (work: (live: Live) => Promise<void>, fallback: string) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    const session = sessionRef.current;
    const live = () => sessionRef.current === session;
    setError(null);
    setNotice(null);
    try {
      await work(live);
    } catch (caught) {
      if (!live()) return;
      const message = caught instanceof Error ? caught.message : fallback;
      if (isRevisionConflict(message)) await reloadAfterConflict(message, live);
      else setError(message);
    } finally {
      submitInFlightRef.current = false;
    }
  };

  const recoveryBody = (): MigrationPlanUpdateRequest => ({
    retention_hours: parseRetention(draft.retentionInput) ?? draft.retentionHours,
    checkpoint_requested: false,
    external_cutover_required: false,
  });

  const advance = () =>
    run(async (live) => {
      if (step === 0) {
        setStep(1);
      } else if (step === 1) {
        const nextBody = createPlanBody(draft);
        const saved = plan
          ? await patchPlan({ target: nextBody.target, mappings: nextBody.mappings }, live)
          : await startPreflight(live);
        if (saved) setStep(2);
      } else if (step === 2) {
        setStep(3);
      } else if (step === 3) {
        if (await patchPlan({ mappings: createPlanBody(draft).mappings }, live)) setStep(4);
      } else if (step === 4) {
        setStep(5);
      } else if (step === 5) {
        if (await patchPlan(recoveryBody(), live)) setStep(6);
      } else if (step === 6 && plan) {
        const body = { reason: draft.reason.trim(), acknowledgements: draft.acknowledgements };
        const signature = JSON.stringify({ planId: plan.plan_id, revision: plan.revision, body });
        const attempt =
          executionAttemptRef.current?.signature === signature
            ? executionAttemptRef.current
            : { signature, key: safeRandomUUID() };
        executionAttemptRef.current = attempt;
        const job = await executePlan.mutateAsync({
          planId: plan.plan_id,
          revision: plan.revision,
          idempotencyKey: attempt.key,
          body,
        });
        if (live()) onStarted(job);
        else onDetached?.({ kind: "job", jobId: job.job_id });
      }
    }, "The migration request failed.");

  const recheck = () =>
    run(async (live) => {
      await patchPlan({ mappings: createPlanBody(draft).mappings, ...recoveryBody() }, live);
    }, "Preflight could not be refreshed.");

  // important: Close always closes. A save that fails leaves the server's draft as it was last
  // saved and says so in a toast, instead of holding the operator in a dialog whose every exit
  // repeats the failing request.
  const saveAndClose = () =>
    run(async (live) => {
      if (plan && planDirty && !executed) {
        try {
          if (mappingIssue) throw new Error(mappingIssue);
          const body = createPlanBody(draft);
          await patchPlan({ target: body.target, mappings: body.mappings, ...recoveryBody() }, live);
        } catch (caught) {
          if (!live()) return;
          const reason = caught instanceof Error && caught.message ? caught.message : "The save failed.";
          toast({
            variant: "destructive",
            title: "Migration draft not saved",
            description: `${asSentence(reason)} The saved draft keeps its earlier settings.`,
          });
        }
      }
      if (live()) onOpenChange(false);
    }, "The migration draft could not be saved.");

  const inventoryErrors = [
    clusterQuery.isError ? `Clusters could not be read: ${describeReadError(clusterQuery.error).reason}.` : null,
    projectQuery.isError ? `Projects could not be read: ${describeReadError(projectQuery.error).reason}.` : null,
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
    !executed &&
    (step === 0
      ? sourceValid && !inventoryError
      : step === 1
        ? destinationValid && !inventoryError
        : step === 3
          ? !!plan && !mappingIssue
          : step === 4
            ? !!plan && !plan.blockers?.length && !expired
            : step === 5
              ? !!plan && retentionValid
              : step === 6
                ? !executionDisabledReason && reviewValid
                : !!plan);

  return {
    step,
    setStep,
    draft,
    updateDraft,
    plan,
    planDirty,
    error,
    notice,
    pending,
    expired,
    executed,
    canAdvance,
    sameLocation,
    rolloutDisabledReason,
    inventoryError,
    targetProjectExists,
    advance,
    recheck,
    saveAndClose,
    queries: { clusterQuery, projectQuery, instanceQuery, memberQuery, poolQuery, networkQuery, planQuery },
  };
}
