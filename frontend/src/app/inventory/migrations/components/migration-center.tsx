"use client";

import * as React from "react";
import { ArrowLeftRight, FileClock, LockKeyhole, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { PageReadError, ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useMigrationJobs, useMigrationPlans } from "@/hooks/use-migrations";
import { usePermissions } from "@/hooks/use-permissions";
import { useMigrationCapabilities, useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { describeReadError } from "@/lib/read-error";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { Role } from "@/types/rbac";
import { APIError } from "@/types/api";
import type { MigrationJob, MigrationPlan } from "@/types/migrations";
import { usePathname, useSearchParams } from "next/navigation";
import { MigrationDraftTable } from "./migration-draft-table";
import { endpointLabel, migrationInView, type MigrationView } from "./migration-format";
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
  const jobsQuery = useMigrationJobs(allowed);
  const plansQuery = useMigrationPlans(allowed);
  const { writeAvailability } = useReadOnlyMode();

  const view = viewFromQuery(searchParams.get("view"));
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
        ? "System write availability could not be verified"
        : writeAvailability === "read_only"
          ? "Disabled in read-only mode"
          : undefined;
  const planWriteDisabledReason = centerEnabled
    ? writeDisabledReason
    : "New migrations are turned off on this deployment";
  const patchQuery = React.useCallback(
    (patch: Record<string, string | null>) => {
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
  const jobs = React.useMemo(() => {
    return [...loadedJobs]
      .filter((job) => migrationInView(job, view === "drafts" ? "active" : view))
      .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  }, [loadedJobs, view]);
  const draftPlans = React.useMemo(
    () => loadedPlans.filter((plan) => plan.status !== "executed"),
    [loadedPlans],
  );

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

  const counts = {
    active: loadedJobs.filter((job) => migrationInView(job, "active")).length,
    attention: loadedJobs.filter((job) => migrationInView(job, "attention")).length,
    history: loadedJobs.filter((job) => migrationInView(job, "history")).length,
    drafts: draftPlans.length,
  };
  const moreJobsAvailable = jobsQuery.hasNextPage === true;
  const moreDraftsAvailable = plansQuery.hasNextPage === true;

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
          <Button
            className="min-h-11 sm:min-h-10"
            disabled={!centerEnabled || !!writeDisabledReason}
            onClick={() => openOverlay({ new: "1", plan: null, job: null, step: null })}
            title={
              planWriteDisabledReason ??
              "Create a migration plan and run side-effect-free preflight"
            }
          >
            <Plus className="mr-2 h-4 w-4" aria-hidden />
            New Migration
          </Button>
        </div>
      </div>

      {!centerEnabled && (
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
              <TabsTrigger className="min-h-11 sm:min-h-9" value="active">Active ({counts.active}{moreJobsAvailable ? "+" : ""})</TabsTrigger>
              <TabsTrigger className="min-h-11 sm:min-h-9" value="attention">Needs Attention ({counts.attention}{moreJobsAvailable ? "+" : ""})</TabsTrigger>
              <TabsTrigger className="min-h-11 sm:min-h-9" value="history">History ({counts.history}{moreJobsAvailable ? "+" : ""})</TabsTrigger>
              <TabsTrigger className="min-h-11 sm:min-h-9" value="drafts">Drafts ({counts.drafts}{moreDraftsAvailable ? "+" : ""})</TabsTrigger>
            </TabsList>
          </CardHeader>
          <CardContent>
            <TabsContent value="active" className="mt-0">
              {jobsUnavailable ? (
                <ReadErrorState
                  noun="migration jobs"
                  error={jobsQuery.error}
                  onRetry={() => void jobsQuery.refetch()}
                />
              ) : (
                <MigrationJobTable
                  jobs={jobs}
                  emptyTitle="No active migrations"
                  emptyDescription="Queued and in-progress jobs appear here."
                  onOpen={(id) => openOverlay({ job: id, plan: null, new: null })}
                />
              )}
            </TabsContent>
            <TabsContent value="attention" className="mt-0">
              {jobsUnavailable ? (
                <ReadErrorState
                  noun="migration jobs"
                  error={jobsQuery.error}
                  onRetry={() => void jobsQuery.refetch()}
                />
              ) : (
                <MigrationJobTable
                  jobs={jobs}
                  emptyTitle="No migration needs attention"
                  emptyDescription="Blocked, uncertain, failed, and overdue jobs appear here."
                  onOpen={(id) => openOverlay({ job: id, plan: null, new: null })}
                />
              )}
            </TabsContent>
            <TabsContent value="history" className="mt-0">
              {jobsUnavailable ? (
                <ReadErrorState
                  noun="migration jobs"
                  error={jobsQuery.error}
                  onRetry={() => void jobsQuery.refetch()}
                />
              ) : (
                <MigrationJobTable
                  jobs={jobs}
                  emptyTitle="No migration history"
                  emptyDescription="Completed, cancelled, and failed jobs appear here."
                  onOpen={(id) => openOverlay({ job: id, plan: null, new: null })}
                />
              )}
            </TabsContent>
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
        onStarted={(job) =>
          patchQuery({
            job: job.job_id,
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
          })
        }
        writeDisabledReason={planWriteDisabledReason}
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
