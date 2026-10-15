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
  parseRetention,
  type MigrationDraftState,
  type MigrationWizardSeed,
} from "./migration-wizard-model";

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
    const memberMove = draft.targetCluster === draft.sourceCluster && draft.targetProject === draft.sourceProject;
    const enabled = memberMove ? rollout.projectMemberRelocationEnabled : rollout.projectCrossClusterEnabled;
    return enabled === true ? undefined : "Whole-project relocation is not available on this deployment yet";
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

export function useMigrationWizard({
  open,
  seed,
  planId,
  onPlanIdChange,
  onStarted,
  onOpenChange,
  writeDisabledReason,
  rollout,
}: {
  open: boolean;
  seed?: MigrationWizardSeed;
  planId?: string | null;
  onPlanIdChange: (planId: string | null) => void;
  onStarted: (job: MigrationJob) => void;
  onOpenChange: (open: boolean) => void;
  writeDisabledReason?: string;
  rollout: MigrationRollout;
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
  const destinationValid =
    !!draft.targetCluster &&
    LXD_NAME_PATTERN.test(draft.targetProject.trim()) &&
    (draft.scope === "project" || LXD_NAME_PATTERN.test(draft.targetResourceName.trim())) &&
    !sameLocation;
  const rolloutDisabledReason = rolloutReason(draft, rollout);
  const expired = plan ? Date.parse(plan.expires_at) <= Date.now() : false;
  const retentionValid = parseRetention(draft.retentionInput) !== null;
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

  const startPreflight = async () => {
    const nextPlan = await createPlan.mutateAsync(createPlanBody(draft));
    adoptPlan(nextPlan);
    loadedPlanRef.current = nextPlan.plan_id;
    onPlanIdChange(nextPlan.plan_id);
    return nextPlan;
  };

  const patchPlan = async (body: MigrationPlanUpdateRequest) => {
    if (!plan || expired) return startPreflight();
    const nextPlan = await updatePlan.mutateAsync({ planId: plan.plan_id, revision: plan.revision, body });
    adoptPlan(nextPlan);
    return nextPlan;
  };

  // Execution found approval-relevant drift and saved a new revision. Show it, and ask again.
  const reloadAfterConflict = async (message: string) => {
    if (!plan) return;
    try {
      const fresh = await migrationsAPI.getPlan(plan.plan_id);
      adoptPlan(fresh);
      setDraft((current) => ({ ...current, acknowledgements: [] }));
      setStep(fresh.blockers?.length ? 4 : 6);
      setNotice(
        `Preflight changed since you reviewed it and is now revision ${fresh.revision}. ` +
          "Review the findings and confirm again.",
      );
    } catch (caught) {
      setError(`${message} The new revision could not be loaded: ${describeReadError(caught).reason}.`);
    }
  };

  const run = async (work: () => Promise<void>, fallback: string) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    setNotice(null);
    try {
      await work();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : fallback;
      if (isRevisionConflict(message)) await reloadAfterConflict(message);
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
    run(async () => {
      if (step === 0) {
        setStep(1);
      } else if (step === 1) {
        const nextBody = createPlanBody(draft);
        if (plan && !expired) await patchPlan({ target: nextBody.target, mappings: nextBody.mappings });
        else await startPreflight();
        setStep(2);
      } else if (step === 2) {
        setStep(3);
      } else if (step === 3) {
        await patchPlan({ mappings: createPlanBody(draft).mappings });
        setStep(4);
      } else if (step === 4) {
        setStep(5);
      } else if (step === 5) {
        await patchPlan(recoveryBody());
        setStep(6);
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
        onStarted(job);
      }
    }, "The migration request failed.");

  const recheck = () =>
    run(async () => {
      await patchPlan({ mappings: createPlanBody(draft).mappings, ...recoveryBody() });
    }, "Preflight could not be refreshed.");

  const saveAndClose = () =>
    run(async () => {
      if (plan && planDirty) {
        const body = createPlanBody(draft);
        await patchPlan({ target: body.target, mappings: body.mappings, ...recoveryBody() });
      }
      onOpenChange(false);
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
    (step === 0
      ? sourceValid && !inventoryError
      : step === 1
        ? destinationValid && !inventoryError
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
