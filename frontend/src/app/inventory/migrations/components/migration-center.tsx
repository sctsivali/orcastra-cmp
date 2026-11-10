"use client";

import * as React from "react";
import { AlertTriangle, ArrowLeftRight, FileClock, LockKeyhole, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { AccessGateIllustration } from "@/components/illustrations/scenes/states";
import { LoadingLottieMedium, LoadingLottiePage } from "@/components/loading-lottie";
import { PageReadError, ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { migrationViewCounts, useMigrationJobs, useMigrationPlans } from "@/hooks/use-migrations";
import { usePermissions } from "@/hooks/use-permissions";
import { useMigrationCapabilities, useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { describeReadError } from "@/lib/read-error";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { Role } from "@/types/rbac";
import { APIError } from "@/types/api";
import type { MigrationJob, MigrationPlan, MigrationView } from "@/types/migrations";
import { usePathname, useSearchParams } from "next/navigation";
import { MigrationDraftTable } from "./migration-draft-table";
import { JOB_VIEW_TABS, migrationInView } from "./migration-format";
import { MigrationJobDialog } from "./migration-job-dialog";
import { MigrationJobTable } from "./migration-job-table";
import { MigrationWizard } from "./migration-wizard";
import { useOverlayHistory } from "./use-overlay-history";
import {
  PLANNING_STEP_KEYS,
  planningStepFromKey,
  type MigrationWizardSeed,
  type ProjectRelocationMode,
} from "./migration-wizard-model";

const PROJECT_MODES: readonly ProjectRelocationMode[] = ["member", "cluster", "rename"];

type CenterView = MigrationView | "drafts";

function uniqueJobs(pages: Array<{ items: MigrationJob[] }> | undefined): MigrationJob[] {
  const jobs = new Map<string, MigrationJob>();
  for (const page of pages ?? []) {
    for (const job of page.items) {
      if (!jobs.has(job.job_id)) jobs.set(job.job_id, job);
    }
  }
  return [...jobs.values()];
}

function uniquePlans(pages: Array<{ items: MigrationPlan[] }> | undefined): MigrationPlan[] {
  const plans = new Map<string, MigrationPlan>();
  for (const page of pages ?? []) {
    for (const plan of page.items) {
      if (!plans.has(plan.plan_id)) plans.set(plan.plan_id, plan);
    }
  }
  return [...plans.values()];
}

function viewFromQuery(value: string | null): CenterView {
  return value === "attention" || value === "history" || value === "drafts" ? value : "active";
}

export function MigrationCenter() {
  return (
    <React.Suspense fallback={<LoadingLottiePage />}>
      <MigrationCenterContent />
    </React.Suspense>
  );
}

function MigrationCenterContent() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const permissions = usePermissions();
  const allowed = permissions.hasRole(Role.PARTNER);
  const migrationCapabilities = useMigrationCapabilities();
  const centerEnabled = migrationCapabilities.centerEnabled === true;
  // A failed settings read is not a deployment that turned migrations off. Both refuse new plans,
  // but only one is fixed by an administrator setting MIGRATION_CENTER_ENABLED.
  const capabilitiesUnreadable = migrationCapabilities.isError;
  const newMigrationsOffReason = capabilitiesUnreadable
    ? "Migration availability could not be read"
    : "New migrations are turned off on this deployment";
  const view = viewFromQuery(searchParams.get("view"));
  // Drafts reads the Active jobs: the queue keeps polling, and a resumed plan finds its job.
  const jobView: MigrationView = view === "drafts" ? "active" : view;
  const jobsQuery = useMigrationJobs(jobView, allowed);
  const plansQuery = useMigrationPlans(allowed);
  const { writeAvailability, writeUnavailableCause, recheckWriteAvailability } = useReadOnlyMode();

  const jobId = searchParams.get("job");
  const planId = searchParams.get("plan");
  const wizardOpen = (centerEnabled && searchParams.get("new") === "1") || !!planId;
  const initialStep = planningStepFromKey(searchParams.get("step"));
  const overlayOpen = wizardOpen || !!jobId;

  const seed = React.useMemo<MigrationWizardSeed>(() => {
    const scope = searchParams.get("scope");
    const intent = searchParams.get("intent");
    return {
      scope: scope === "project" ? "project" : scope === "instance" ? "instance" : undefined,
      intent: intent === "copy" ? "copy" : intent === "move" ? "move" : undefined,
      sourceCluster: searchParams.get("source_cluster") || undefined,
      sourceProject: searchParams.get("source_project") || undefined,
      resourceName: searchParams.get("resource_name") || undefined,
      sourceMember: searchParams.get("source_member") || undefined,
      mode: PROJECT_MODES.find((mode) => mode === searchParams.get("mode")),
    };
  }, [searchParams]);

  const writeDisabledReason =
    writeAvailability === "checking"
      ? "Checking system write availability"
      : writeAvailability === "unavailable"
        ? writeUnavailableCause ?? "System write availability could not be verified"
        : writeAvailability === "read_only"
          ? "Disabled in read-only mode"
          : undefined;
  const planWriteDisabledReason = centerEnabled ? writeDisabledReason : newMigrationsOffReason;
  const patchQuery = React.useCallback(
    (patch: Record<string, string | null>) => {
      // important: a request that resolves after Back left this page still holds this callback.
      // Writing then would put a Migration Center query onto the page the operator is now on.
      if (window.location.pathname !== pathname) return;
      const params = currentQuery();
      for (const [key, value] of Object.entries(patch)) {
        if (value) params.set(key, value);
        else params.delete(key);
      }
      replaceQuery(pathname, params);
    },
    [pathname],
  );

  const { openOverlay, closeOverlay } = useOverlayHistory(pathname, overlayOpen, patchQuery);

  const closeWizard = React.useCallback(() => {
    closeOverlay({
      new: null,
      plan: null,
      step: null,
      scope: null,
      source_cluster: null,
      source_project: null,
      resource_name: null,
      mode: null,
      intent: null,
      source_member: null,
    });
  }, [closeOverlay]);

  const syncStep = React.useCallback(
    (step: number) => {
      const key = PLANNING_STEP_KEYS[step];
      if (key && currentQuery().get("step") !== key) patchQuery({ step: key });
    },
    [patchQuery],
  );

  const loadedJobs = React.useMemo(
    () => uniqueJobs(jobsQuery.data?.pages),
    [jobsQuery.data?.pages],
  );
  const loadedPlans = React.useMemo(
    () => uniquePlans(plansQuery.data?.pages),
    [plansQuery.data?.pages],
  );
  // The server already filtered the view; filtering again keeps a server that ignores `view`
  // (one that predates it) from listing every job under every tab.
  const jobs = React.useMemo(() => {
    return [...loadedJobs]
      .filter((job) => migrationInView(job, jobView))
      .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  }, [loadedJobs, jobView]);
  const draftPlans = React.useMemo(
    () => loadedPlans.filter((plan) => plan.status !== "executed"),
    [loadedPlans],
  );
  const startedJobId = planId ? loadedJobs.find((job) => job.plan_id === planId)?.job_id ?? null : null;

  const showJob = (id: string) =>
    patchQuery({
      job: id,
      new: null,
      plan: null,
      step: null,
      scope: null,
      source_cluster: null,
      source_project: null,
      resource_name: null,
      mode: null,
      intent: null,
      source_member: null,
    });

  if (permissions.isLoading || migrationCapabilities.isLoading) return <LoadingLottiePage />;

  if (!allowed) {
    return (
      <div className="p-8 space-y-6">
        <div>
          <h1 className="text-3xl font-bold">Migration Center</h1>
          <p className="mt-2 text-muted-foreground">Plan and supervise staged workload moves.</p>
        </div>
        <Card>
          <CardContent>
            <EmptyState
              icon={LockKeyhole}
              illustration={AccessGateIllustration}
              title="Partner access required"
              description="Tenant accounts cannot plan, inspect, or control infrastructure migrations."
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  const routeUnavailable =
    jobsQuery.error instanceof APIError &&
    jobsQuery.error.status === 404 &&
    migrationCapabilities.centerEnabled !== true;

  if (routeUnavailable) {
    return (
      <div className="p-8 space-y-6">
        <div>
          <h1 className="text-3xl font-bold">Migration Center</h1>
          <p className="mt-2 text-muted-foreground">Plan and supervise staged workload moves.</p>
        </div>
        <Card>
          <CardContent>
            <EmptyState
              icon={LockKeyhole}
              illustration={AccessGateIllustration}
              title="Migration Center is not enabled"
              description="This deployment has not enabled the migration rollout. Existing infrastructure is unchanged."
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  if (jobsQuery.isLoading && !jobsQuery.data) return <LoadingLottiePage />;

  const jobsUnavailable = jobsQuery.isError && !jobsQuery.data;
  const plansUnavailable = plansQuery.isError && !plansQuery.data;

  if (jobsUnavailable && plansUnavailable) {
    return (
      <PageReadError
        title="Migration Center"
        description="Plan and supervise staged workload moves."
        noun="migrations"
        error={jobsQuery.error}
        onRetry={() => void jobsQuery.refetch()}
      />
    );
  }

  const moreJobsAvailable = jobsQuery.hasNextPage === true;
  const moreDraftsAvailable = plansQuery.hasNextPage === true;
  const serverCounts = migrationViewCounts(jobsQuery.data?.pages);
  // The server counts every view exactly. Only a server without counts, which also ignores
  // `view` and sends every job, falls back to counting the rows loaded so far.
  const jobCount = (tab: MigrationView): string => {
    const exact = serverCounts?.[tab];
    if (typeof exact === "number") return String(exact);
    const loaded = loadedJobs.filter((job) => migrationInView(job, tab)).length;
    return `${loaded}${moreJobsAvailable ? "+" : ""}`;
  };
  // Until a newly chosen view lands, the data is the previous view's: its counts still hold, its
  // rows belong to another tab.
  const switchingView = jobsQuery.isPlaceholderData;

  return (
    <div className="p-8 space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-3xl font-bold">Migration Center</h1>
          <p className="mt-2 text-muted-foreground">
            Plan, preflight, supervise, and safely reconcile workload migrations.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh migrations"
            lastUpdatedAt={jobsQuery.dataUpdatedAt}
            onRefresh={() => Promise.all([jobsQuery.refetch(), plansQuery.refetch()])}
          />
          {/* Planning is not a write, so read-only mode leaves it on; the wizard refuses Start. */}
          <Button
            className="min-h-11 sm:min-h-10"
            disabled={!centerEnabled}
            onClick={() => openOverlay({ new: "1", plan: null, job: null, step: null })}
            title={
              centerEnabled
                ? "Create a migration plan and run side-effect-free preflight"
                : newMigrationsOffReason
            }
          >
            <Plus className="mr-2 h-4 w-4" aria-hidden />
            New Migration
          </Button>
        </div>
      </div>

      {capabilitiesUnreadable ? (
        <Alert>
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertTitle>Migration availability could not be read</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>
              New migrations stay unavailable until it is read, because the settings check failed:{" "}
              {describeReadError(migrationCapabilities.error).reason}. Existing jobs, their actions,
              saved drafts, and history stay available.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void migrationCapabilities.refetch()}
            >
              Check Again
            </Button>
          </AlertDescription>
        </Alert>
      ) : !centerEnabled && (
        <Alert>
          <LockKeyhole className="h-4 w-4" aria-hidden />
          <AlertTitle>New migrations are turned off on this deployment</AlertTitle>
          <AlertDescription>
            Existing jobs, their rollback and cleanup actions, saved drafts, and history stay
            available. An administrator enables new plans with MIGRATION_CENTER_ENABLED.
          </AlertDescription>
        </Alert>
      )}

      {jobsQuery.isError && (
        <Alert>
          <ArrowLeftRight className="h-4 w-4" aria-hidden />
          <AlertTitle>
            {jobsQuery.data ? "Migration updates are reconnecting" : "Migration queue is unavailable"}
          </AlertTitle>
          <AlertDescription>
            {jobsQuery.data ? "Last known state is shown. " : "Drafts remain available. "}
            Refresh failed because {describeReadError(jobsQuery.error).reason}.
          </AlertDescription>
        </Alert>
      )}

      {plansQuery.isError && plansQuery.data && (
        <Alert>
          <FileClock className="h-4 w-4" aria-hidden />
          <AlertTitle>Draft updates are reconnecting</AlertTitle>
          <AlertDescription>
            Last known drafts are shown. Refresh failed because {describeReadError(plansQuery.error).reason}.
          </AlertDescription>
        </Alert>
      )}

      {(moreJobsAvailable || moreDraftsAvailable) && (
        <Alert>
          <FileClock className="h-4 w-4" aria-hidden />
          <AlertTitle>Older migration records are available</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>Load the next server page without losing the records already shown.</p>
            <div className="flex flex-wrap gap-2">
              {moreJobsAvailable && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void jobsQuery.fetchNextPage()}
                  disabled={jobsQuery.isFetchingNextPage}
                >
                  {jobsQuery.isFetchingNextPage ? "Loading older jobs" : "Load older jobs"}
                </Button>
              )}
              {moreDraftsAvailable && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void plansQuery.fetchNextPage()}
                  disabled={plansQuery.isFetchingNextPage}
                >
                  {plansQuery.isFetchingNextPage ? "Loading older drafts" : "Load older drafts"}
                </Button>
              )}
            </div>
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <Tabs
          value={view}
          onValueChange={(value) => patchQuery({ view: value === "active" ? null : value })}
        >
          <CardHeader className="space-y-4">
            <CardTitle>Migration Queue</CardTitle>
            <TabsList className="h-auto w-full justify-start overflow-x-auto sm:w-auto">
              {JOB_VIEW_TABS.map((tab) => (
                <TabsTrigger key={tab.view} className="min-h-11 sm:min-h-9" value={tab.view}>
                  {tab.label} ({jobCount(tab.view)})
                </TabsTrigger>
              ))}
              <TabsTrigger className="min-h-11 sm:min-h-9" value="drafts">Drafts ({draftPlans.length}{moreDraftsAvailable ? "+" : ""})</TabsTrigger>
            </TabsList>
          </CardHeader>
          <CardContent>
            {JOB_VIEW_TABS.map((tab) => (
              <TabsContent key={tab.view} value={tab.view} className="mt-0">
                {jobsUnavailable ? (
                  <ReadErrorState
                    noun="migration jobs"
                    error={jobsQuery.error}
                    onRetry={() => void jobsQuery.refetch()}
                  />
                ) : switchingView ? (
                  <div className="flex flex-col items-center gap-3 py-12 text-sm text-muted-foreground" role="status">
                    <LoadingLottieMedium />
                    Loading migration jobs…
                  </div>
                ) : (
                  <MigrationJobTable
                    jobs={jobs}
                    emptyTitle={tab.emptyTitle}
                    emptyDescription={tab.emptyDescription}
                    onOpen={(id) => openOverlay({ job: id, plan: null, new: null })}
                  />
                )}
              </TabsContent>
            ))}
            <TabsContent value="drafts" className="mt-0">
              {plansQuery.isError && !plansQuery.data ? (
                <ReadErrorState
                  noun="migration drafts"
                  error={plansQuery.error}
                  onRetry={() => void plansQuery.refetch()}
                />
              ) : (
                <MigrationDraftTable
                  plans={draftPlans}
                  onOpen={(id) => openOverlay({ plan: id, new: null, job: null, step: null })}
                />
              )}
            </TabsContent>
          </CardContent>
        </Tabs>
      </Card>

      <MigrationWizard
        open={wizardOpen}
        onOpenChange={(next) => !next && closeWizard()}
        seed={seed}
        planId={planId}
        onPlanIdChange={(id) => patchQuery(id ? { plan: id, new: null } : { plan: null, new: "1", step: null })}
        initialStep={initialStep}
        onStepChange={syncStep}
        onStarted={(job) => showJob(job.job_id)}
        startedJobId={startedJobId}
        onOpenJob={showJob}
        writeDisabledReason={planWriteDisabledReason}
        writeAvailability={centerEnabled ? writeAvailability : undefined}
        onRecheckWrite={() => void recheckWriteAvailability()}
        rollout={{
          durableCopyEnabled: migrationCapabilities.durableCopyEnabled,
          instanceMoveEnabled: migrationCapabilities.instanceMoveEnabled,
          projectMemberRelocationEnabled: migrationCapabilities.projectMemberRelocationEnabled,
          projectCrossClusterEnabled: migrationCapabilities.projectCrossClusterEnabled,
          defaultRetentionHours: migrationCapabilities.defaultRetentionHours,
        }}
      />

      <MigrationJobDialog
        jobId={jobId}
        onOpenChange={(next) => !next && closeOverlay({ job: null })}
        writeDisabledReason={writeDisabledReason}
      />
    </div>
  );
}
