"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Loader2, RefreshCw, SlidersHorizontal } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useByteUnitsRef } from "@/hooks/use-byte-format";
import {
  useInstanceOperationJob,
  useInstanceSpecificationForEdit,
  usePreflightInstanceSpecification,
  useUpdateInstanceSpecification,
} from "@/hooks/use-lxd";
import { formatInstanceType, safeRandomUUID } from "@/lib/utils";
import { matchesSpecificationOperation } from "@/lib/instance-operation-identity";
import {
  APIError,
  type InstanceOperationJob,
  type InstanceSpecificationPreflightRequest,
  type InstanceSpecificationPreflightResponse,
  type InstanceSpecificationUpdateRequest,
  type VMInfo,
} from "@/types/api";
import { ChangeSummary } from "./operation-summary";
import { SpecificationOperationView, type DialogPhase } from "./operation-view";
import { SpecificationLoadState } from "./specification-load-state";
import {
  clearSpecificationRecovery,
  forgetSpecificationAttempt,
  readSpecificationAttempt,
  readSpecificationJob,
  rememberSpecificationAttempt,
  writeSpecificationJob,
  type SpecificationJobIdentity,
  type SpecificationOperationAttempt,
} from "./operation-recovery";
import { SpecificationFields } from "./specification-fields";
import { draftFromSpecification, validateSpecificationDraft, type SpecificationDraft } from "./rules";
interface InstanceSpecificationDialogProps {
  instance: Pick<VMInfo, "name" | "host" | "project" | "type" | "status">;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone?: () => void;
  writeDisabledReason?: string;
}
const TERMINAL = new Set(["success", "failure", "partial", "unknown"]);
function isAmbiguousSpecificationError(error: unknown): boolean {
  if (!(error instanceof APIError)) return true;
  return error.status === 0 || error.status === 408 || error.status >= 500;
}
export function InstanceSpecificationDialog(props: InstanceSpecificationDialogProps) {
  const [busy, setBusy] = useState(false);
  const handleOpenChange = (next: boolean) => {
    if (!next && busy) return;
    props.onOpenChange(next);
  };
  return (
    <Dialog open={props.open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0 pr-8 pb-4">
          <DialogTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
            Edit Specifications
          </DialogTitle>
          <DialogDescription>
            {props.instance.name} in {props.instance.project || "default"} on {props.instance.host}
          </DialogDescription>
        </DialogHeader>
        {props.open && (
          <InstanceSpecificationBody
            {...props}
            onBusyChange={setBusy}
            onComplete={() => {
              props.onDone?.();
              props.onOpenChange(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
interface BodyProps extends InstanceSpecificationDialogProps {
  onBusyChange: (busy: boolean) => void;
  onComplete: () => void;
}
function InstanceSpecificationBody({
  instance,
  onOpenChange,
  onBusyChange,
  onComplete,
  writeDisabledReason,
}: BodyProps) {
  const { toast } = useToast();
  const { scopeKey } = useActorQueryScope();
  const unitsRef = useByteUnitsRef();
  const project = instance.project || "default";
  const [attempt, setAttempt] = useState<SpecificationOperationAttempt | null>(() =>
    readSpecificationAttempt(scopeKey, instance.name, instance.host, project),
  );
  const [savedJob, setSavedJob] = useState<SpecificationJobIdentity | null>(() =>
    readSpecificationJob(scopeKey, instance.name, instance.host, project),
  );
  const [nonce, setNonce] = useState(0);
  const [draft, setDraft] = useState<SpecificationDraft | null>(null);
  const [phase, setPhase] = useState<DialogPhase>(() =>
    savedJob ? "running" : attempt ? "retry" : "form",
  );
  const [plan, setPlan] = useState<InstanceSpecificationPreflightResponse | null>(null);
  const [pendingRequest, setPendingRequest] =
    useState<InstanceSpecificationPreflightRequest | null>(null);
  const [submittedJob, setSubmittedJob] = useState<InstanceOperationJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [pollingHalted, setPollingHalted] = useState(false);
  const [identityConflict, setIdentityConflict] = useState<string | null>(null);
  const handledJobRef = useRef<string | null>(null);
  const preflightInFlightRef = useRef(false);
  const writeInFlightRef = useRef(false);
  const writeDisabledReasonRef = useRef(writeDisabledReason);
  writeDisabledReasonRef.current = writeDisabledReason;
  const specification = useInstanceSpecificationForEdit(
    instance.name,
    instance.host,
    project,
    nonce,
    true,
  );
  const preflight = usePreflightInstanceSpecification();
  const update = useUpdateInstanceSpecification();
  // Unknown remains active until operation state plus daemon readback establish a verdict.
  const jobId = submittedJob?.job_id ?? savedJob?.jobId ?? null;
  const jobRead = useInstanceOperationJob(jobId, !!jobId && !pollingHalted);
  const expectedJob = useMemo<SpecificationJobIdentity | null>(() =>
    savedJob ?? (submittedJob ? {
      instanceName: instance.name,
      host: instance.host,
      project,
      jobId: submittedJob.job_id,
    } : null), [instance.host, instance.name, project, savedJob, submittedJob]);
  const pollMatches = !jobRead.data || (!!expectedJob &&
    matchesSpecificationOperation(expectedJob, jobRead.data));
  const job = pollMatches ? jobRead.data ?? submittedJob : null;
  useEffect(() => {
    if (!jobRead.data || !expectedJob || pollMatches) return;
    clearSpecificationRecovery(scopeKey, instance.name, instance.host, project);
    setSavedJob(null);
    setSubmittedJob(null);
    setPollingHalted(true);
    setIdentityConflict("The returned operation did not match this instance. Its status was not applied, and polling stopped.");
    setPhase("result");
  }, [expectedJob, instance.host, instance.name, jobRead.data, pollMatches, project, scopeKey]);
  useEffect(() => {
    if (!specification.data) return;
    setDraft(draftFromSpecification(specification.data, unitsRef.current));
    setError(null);
    setStale(false);
  }, [specification.data]);
  const validation = useMemo(
    () => (specification.data && draft ? validateSpecificationDraft(specification.data, draft) : null),
    [draft, specification.data],
  );

  const terminal = !!job && TERMINAL.has(job.status);
  const busy = preflight.isPending || update.isPending;
  useEffect(() => onBusyChange(busy), [busy, onBusyChange]);

  useEffect(() => {
    if (!job || job.status !== "success" || handledJobRef.current === job.job_id) return;
    handledJobRef.current = job.job_id;
    toast({
      title: "Specifications updated",
      description: `The cluster verified the new specification for ${instance.name}.`,
    });
    setPhase("result");
  }, [instance.name, job, toast]);

  useEffect(() => {
    if (!job) return;
    const nonSuccessTerminal = job.status === "failure" ||
      job.status === "partial" ||
      job.status === "unknown";
    if (nonSuccessTerminal) setPhase("result");
    else if (!terminal && jobId) setPhase("running");
  }, [job, jobId, terminal]);

  useEffect(() => {
    if (!(jobRead.error instanceof APIError)) return;
    if (![401, 403, 404].includes(jobRead.error.status)) return;
    setPollingHalted(true);
    setPhase("result");
  }, [jobRead.error]);

  const reload = () => {
    clearSpecificationRecovery(scopeKey, instance.name, instance.host, project);
    setAttempt(null);
    setSavedJob(null);
    setDraft(null);
    setPlan(null);
    setPendingRequest(null);
    setSubmittedJob(null);
    setPhase("form");
    setError(null);
    setStale(false);
    setPollingHalted(false);
    setIdentityConflict(null);
    handledJobRef.current = null;
    setNonce((value) => value + 1);
  };

  const executeAttempt = async (nextAttempt: SpecificationOperationAttempt) => {
    if (writeDisabledReasonRef.current || writeInFlightRef.current) return;
    writeInFlightRef.current = true;
    setError(null);
    setIdentityConflict(null);
    setAttempt(nextAttempt);
    rememberSpecificationAttempt(scopeKey, nextAttempt);
    try {
      const nextJob = await update.mutateAsync({
        name: instance.name,
        etag: nextAttempt.etag,
        idempotencyKey: nextAttempt.idempotencyKey,
        data: nextAttempt.request,
      });
      if (!nextJob.job_id) {
        throw new Error("The server accepted the update without a readable job identity.");
      }
      const acceptedJob: SpecificationJobIdentity = {
        instanceName: nextAttempt.instanceName,
        host: nextAttempt.host,
        project: nextAttempt.project,
        jobId: nextJob.job_id,
      };
      if (!matchesSpecificationOperation(acceptedJob, nextJob)) {
        forgetSpecificationAttempt(scopeKey, instance.name, instance.host, project);
        setAttempt(null);
        setPollingHalted(true);
        setIdentityConflict("The accepted operation did not match this instance. No status was saved or applied to this dialog.");
        setPhase("result");
        return;
      }
      forgetSpecificationAttempt(scopeKey, instance.name, instance.host, project);
      setAttempt(null);
      setSavedJob(acceptedJob);
      writeSpecificationJob(scopeKey, acceptedJob);
      setSubmittedJob(nextJob);
      setPhase(TERMINAL.has(nextJob.status) ? "result" : "running");
    } catch (caught) {
      const ambiguous = isAmbiguousSpecificationError(caught);
      if (ambiguous) {
        setError("The server did not provide a reliable mutation result. Retry in this open console reuses the exact body and operation key. If you reload first, check actual state before any new update.");
        setPhase("retry");
      } else if (caught instanceof APIError && (caught.status === 409 || caught.status === 412)) {
        forgetSpecificationAttempt(scopeKey, instance.name, instance.host, project);
        setAttempt(null);
        setStale(true);
        setError(caught.status === 412
          ? "This instance changed after the dialog loaded. Reload before applying."
          : "This plan is no longer valid or another operation is active. Reload actual state before applying again.");
        setPhase("form");
      } else {
        forgetSpecificationAttempt(scopeKey, instance.name, instance.host, project);
        setAttempt(null);
        setError(caught instanceof Error ? caught.message : "Could not update specifications.");
        setPhase("form");
      }
    } finally {
      writeInFlightRef.current = false;
    }
  };

  const applyPlan = async (
    approvedPlan: InstanceSpecificationPreflightResponse,
    request: InstanceSpecificationPreflightRequest,
  ) => {
    if (!specification.data || writeDisabledReasonRef.current || writeInFlightRef.current) return;
    if (!specification.data.etag) {
      setError("The cluster did not provide a version for this instance. Reload before applying.");
      setPhase("form");
      return;
    }
    const data: InstanceSpecificationUpdateRequest = {
      ...request,
      plan_revision: approvedPlan.plan_revision,
      apply_mode:
        approvedPlan.execution === "restart_required"
          ? "graceful_restart"
          : "live_only",
      acknowledgements: approvedPlan.required_acknowledgements,
    };
    await executeAttempt({
      instanceName: instance.name,
      host: instance.host,
      project,
      etag: specification.data.etag,
      idempotencyKey: safeRandomUUID(),
      request: data,
      createdAt: Date.now(),
    });
  };

  const review = async () => {
    if (!validation?.request || writeDisabledReasonRef.current || preflightInFlightRef.current) return;
    preflightInFlightRef.current = true;
    setError(null);
    try {
      const nextPlan = await preflight.mutateAsync({
        name: instance.name,
        data: validation.request,
      });
      if (writeDisabledReasonRef.current) {
        setError(writeDisabledReasonRef.current);
        return;
      }
      if (nextPlan.blockers.length > 0) {
        setError(nextPlan.blockers.map((item) => item.message).join(" "));
        return;
      }
      setPlan(nextPlan);
      setPendingRequest(validation.request);
      const needsReview =
        nextPlan.execution === "restart_required" ||
        nextPlan.warnings.length > 0 ||
        nextPlan.required_acknowledgements.length > 0;
      if (needsReview) setPhase("confirm");
      else await applyPlan(nextPlan, validation.request);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not check this change.");
    } finally {
      preflightInFlightRef.current = false;
    }
  };

  if (specification.isError || !specification.data) {
    return (
      <SpecificationLoadState
        pending={specification.isPending}
        error={specification.error}
        onClose={() => onOpenChange(false)}
        onRetry={reload}
      />
    );
  }

  if (!draft || !validation) {
    return (
      <DialogBody className="space-y-3" aria-busy="true" aria-label="Preparing specifications">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-28 w-full" />
      </DialogBody>
    );
  }

  if (phase === "retry" || phase === "running" || phase === "result") {
    return (
      <SpecificationOperationView
        phase={phase}
        attempt={attempt}
        error={error}
        updating={update.isPending}
        writeDisabledReason={writeDisabledReason}
        pollingHalted={pollingHalted}
        identityConflict={identityConflict}
        statusError={jobRead.error}
        job={job}
        plan={plan}
        onClose={() => onOpenChange(false)}
        onRetryAttempt={(saved) => void executeAttempt(saved)}
        onRetryStatus={() => void jobRead.refetch()}
        onReload={reload}
        onDone={() => {
          clearSpecificationRecovery(scopeKey, instance.name, instance.host, project);
          onComplete();
        }}
      />
    );
  }

  if (phase === "confirm" && plan && pendingRequest) {
    const restart = plan.execution === "restart_required";
    return (
      <>
        <DialogBody className="space-y-4">
          <div>
            <h3 className="text-sm font-semibold">Review Changes</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {restart
                ? "This change requires a graceful stop, update, and start. No force stop will be used."
                : plan.execution === "stopped"
                  ? "The instance is stopped. The new limits apply on its next start."
                  : "The cluster reports that this change can be applied live."}
            </p>
          </div>
          <ChangeSummary changes={plan.changes} />
          {plan.warnings.map((warning) => (
            <Alert key={warning.code}>
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
              <AlertDescription>{warning.message}</AlertDescription>
            </Alert>
          ))}
          {writeDisabledReason && (
            <Alert><AlertDescription>{writeDisabledReason}</AlertDescription></Alert>
          )}
        </DialogBody>
        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={() => setPhase("form")} disabled={update.isPending}>
            Back
          </Button>
          <Button
            onClick={() => void applyPlan(plan, pendingRequest)}
            disabled={update.isPending || !!writeDisabledReason}
            title={writeDisabledReason}
          >
            {update.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {restart ? "Apply and restart" : "Apply changes"}
          </Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <>
      <DialogBody className="space-y-4 pr-2">
        <div className="grid gap-2 rounded-md border p-3 text-sm sm:grid-cols-3">
          <div><span className="text-muted-foreground">Type</span><p>{formatInstanceType(specification.data.type)}</p></div>
          <div><span className="text-muted-foreground">Status</span><p>{specification.data.status}</p></div>
          <div><span className="text-muted-foreground">Project</span><p>{specification.data.project}</p></div>
        </div>
        <SpecificationFields
          specification={specification.data}
          draft={draft}
          validation={validation}
          onChange={setDraft}
          disabled={busy || stale || !!writeDisabledReason || !specification.data.etag}
        />
        {writeDisabledReason && (
          <Alert><AlertDescription>{writeDisabledReason}</AlertDescription></Alert>
        )}
        {!specification.data.etag && (
          <Alert variant="destructive">
            <AlertDescription>
              Editing is disabled because the cluster did not provide an ETag. Reload before applying any change.
            </AlertDescription>
          </Alert>
        )}
        {validation.overridesInherited && (
          <Alert>
            <AlertDescription>
              This change creates an instance override. Later profile changes will no longer control that value.
            </AlertDescription>
          </Alert>
        )}
        {validation.memoryDecreases && (
          <Alert>
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <AlertDescription>
              Lowering memory can pressure or terminate workloads. The server will decide whether a restart is required.
            </AlertDescription>
          </Alert>
        )}
        {validation.rootDiskGrows && (
          <Alert>
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <AlertDescription>
              Root disk growth cannot be rolled back by shrinking the disk.
            </AlertDescription>
          </Alert>
        )}
        {(error || validation.errors.form) && (
          <Alert variant="destructive">
            <AlertDescription>{error || validation.errors.form}</AlertDescription>
          </Alert>
        )}
        {stale && (
          <Button variant="outline" size="sm" onClick={reload}>
            <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
            Reload current state
          </Button>
        )}
      </DialogBody>
      <DialogFooter className="shrink-0 pt-4">
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</Button>
        <Button
          onClick={() => void review()}
          disabled={!validation.request || busy || stale || !!writeDisabledReason || !specification.data.etag}
          title={writeDisabledReason}
        >
          {preflight.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Review and apply
        </Button>
      </DialogFooter>
    </>
  );
}
