"use client";

import { useEffect, useMemo, useRef } from "react";
import { type InfiniteData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { migrationsAPI } from "@/lib/migrations-api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { APIError } from "@/types/api";
import type {
  MigrationAction,
  MigrationActionRequest,
  MigrationExecuteRequest,
  MigrationJob,
  MigrationJobListResponse,
  MigrationJobPhase,
  MigrationPlanCreateRequest,
  MigrationPlanListResponse,
  MigrationPlanUpdateRequest,
  MigrationView,
  MigrationViewCounts,
} from "@/types/migrations";

// Phases the worker moves through on its own: the next state is seconds away.
const PROGRESSING_PHASES = new Set<MigrationJobPhase>([
  "queued",
  "preparing_target",
  "preparing_recovery",
  "checkpointing",
  "initial_sync",
  "quiescing",
  "final_sync",
  "target_validating",
  "cutting_over",
  "verifying",
  "cleaning_up",
  "unknown",
  "reconciling",
  "rolling_back",
]);

// Phases that wait for an operator or the retention deadline, which can be a week away. An action
// taken here updates the cache from its own response, so a fast poll buys nothing, and each poll
// costs the server an identity-provider lookup.
const WAITING_PHASES = new Set<MigrationJobPhase>([
  "cutover_pending",
  "external_cutover_pending",
  "source_retained",
  "cleanup_due",
  "rollback_available",
  "cleanup_blocked",
  "manual_recovery",
]);

const ACTIVE_PHASES = new Set<MigrationJobPhase>([...PROGRESSING_PHASES, ...WAITING_PHASES]);

const PROGRESS_LIST_POLL_MS = 3_000;
const PROGRESS_JOB_POLL_MS = 2_000;
const WAITING_JOB_POLL_MS = 30_000;
// The list's rate with nothing moving. A waiting job keeps it here rather than slowing it below
// the idle rate, since one list read costs the same whatever the jobs are doing.
const IDLE_LIST_POLL_MS = 15_000;

function retryRead(failureCount: number, error: unknown): boolean {
  return !(error instanceof APIError && [401, 403, 404, 429].includes(error.status)) && failureCount < 2;
}

export const migrationQueryKeys = {
  all: (actorScope: string) => ["migrations", actorScope] as const,
  // An object segment, so a view's list can never match a job id under the same prefix.
  jobList: (actorScope: string, view: MigrationView) => ["migrations", actorScope, "jobs", { view }] as const,
  job: (actorScope: string, jobId: string) => ["migrations", actorScope, "jobs", jobId] as const,
  plans: (actorScope: string) => ["migrations", actorScope, "plans"] as const,
  plan: (actorScope: string, planId: string) => ["migrations", actorScope, "plans", planId] as const,
};

// A move's instance changes cluster at cutover, after which the job waits in retention, still
// "active", for hours. Reaching that phase is when the inventories change.
const CUTOVER_DONE_PHASES = new Set<MigrationJobPhase>(["source_retained"]);
// Per QueryClient (one per actor): the list and the open job dialog watch the same jobs, and
// one transition must reconcile once.
const reconciledTransitions = new WeakMap<object, Set<string>>();

/**
 * Reconcile the inventories a move or copy changed when a job seen running reaches cutover or
 * stops being active.
 *
 * Starting a job reconciles on the response, when nothing has moved yet. The backend clears its
 * caches as the job commits each verdict; this is the browser's half, so the Overview and
 * Instances pages do not show the instance on its previous cluster until their next poll.
 *
 * A list holds one view, so a finished job leaves the Active list instead of showing its final
 * phase there; a rise in `historyTotal`, the exact History count, stands in for that.
 */
function useReconcileFinishedMigrations(
  jobs: readonly MigrationJob[] | undefined,
  historyTotal?: number,
): void {
  const queryClient = useQueryClient();
  const seen = useRef(new Map<string, MigrationJobPhase>());
  const lastHistoryTotal = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!jobs) return;
    let done = reconciledTransitions.get(queryClient);
    if (!done) {
      done = new Set();
      reconciledTransitions.set(queryClient, done);
    }
    let changed = false;
    for (const job of jobs) {
      const previous = seen.current.get(job.job_id);
      seen.current.set(job.job_id, job.phase);
      if (previous === undefined || previous === job.phase) continue;
      const crossed =
        CUTOVER_DONE_PHASES.has(job.phase) || (ACTIVE_PHASES.has(previous) && !migrationIsActive(job));
      const transition = `${job.job_id}:${job.phase}`;
      if (crossed && !done.has(transition)) {
        done.add(transition);
        changed = true;
      }
    }
    if (historyTotal !== undefined) {
      if (lastHistoryTotal.current !== undefined && historyTotal > lastHistoryTotal.current) changed = true;
      lastHistoryTotal.current = historyTotal;
    }
    if (changed) void reconcileMutationImpact(queryClient, "migration.job");
  }, [jobs, historyTotal, queryClient]);
}

/**
 * The exact per-view totals of the newest page read, or null when the server sent none. A
 * refetch reads the pages in order and Load older appends, so the last page is the newest.
 */
export function migrationViewCounts(
  pages: readonly MigrationJobListResponse[] | undefined,
): Partial<MigrationViewCounts> | null {
  return pages?.[pages.length - 1]?.counts ?? null;
}

/** One Migration Queue tab, filtered on the server so an older job is never paged out of it. */
export function useMigrationJobs(view: MigrationView, enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const query = useInfiniteQuery({
    queryKey: migrationQueryKeys.jobList(scopeKey, view),
    queryFn: ({ pageParam, signal }) =>
      migrationsAPI.listJobs({ limit: 100, offset: pageParam, view }, signal),
    // A tab switch keeps the previous view up, with its still exact counts, until the new view
    // lands; the center shows a loading row instead of those rows (isPlaceholderData).
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === scopeKey ? previous : undefined,
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      if (lastPage.items.length === 0) return undefined;
      const nextOffset = lastPage.offset + lastPage.items.length;
      return lastPage.has_more || nextOffset < lastPage.total ? nextOffset : undefined;
    },
    enabled: enabled && isAuthenticated,
    staleTime: 3_000,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) =>
        page.items.some((job) => migrationIsProgressing(job)),
      )
        ? PROGRESS_LIST_POLL_MS
        : IDLE_LIST_POLL_MS,
    refetchIntervalInBackground: false,
    retry: retryRead,
    refetchOnWindowFocus: false,
  });
  useReconcileFinishedMigrations(
    useMemo(() => query.data?.pages.flatMap((page) => page.items), [query.data]),
    migrationViewCounts(query.data?.pages)?.history,
  );
  return query;
}

export function useMigrationPlans(enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useInfiniteQuery({
    queryKey: migrationQueryKeys.plans(scopeKey),
    queryFn: ({ pageParam, signal }) =>
      migrationsAPI.listPlans({ limit: 100, offset: pageParam }, signal),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      if (lastPage.items.length === 0) return undefined;
      const nextOffset = lastPage.offset + lastPage.items.length;
      return lastPage.has_more || nextOffset < lastPage.total ? nextOffset : undefined;
    },
    enabled: enabled && isAuthenticated,
    staleTime: 15_000,
    retry: retryRead,
    refetchOnWindowFocus: false,
  });
}

export function useMigrationJob(jobId: string | null, enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const query = useQuery({
    queryKey: migrationQueryKeys.job(scopeKey, jobId ?? ""),
    queryFn: ({ signal }) => migrationsAPI.getJob(jobId ?? "", signal),
    enabled: enabled && isAuthenticated && !!jobId,
    staleTime: 1_000,
    refetchInterval: (query) => {
      const job = query.state.data;
      if (!job || !migrationIsActive(job)) return false;
      return migrationIsProgressing(job) ? PROGRESS_JOB_POLL_MS : WAITING_JOB_POLL_MS;
    },
    refetchIntervalInBackground: false,
    retry: retryRead,
    refetchOnWindowFocus: false,
  });
  useReconcileFinishedMigrations(useMemo(() => (query.data ? [query.data] : undefined), [query.data]));
  return query;
}

export function useMigrationPlan(planId: string | null, enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery({
    queryKey: migrationQueryKeys.plan(scopeKey, planId ?? ""),
    queryFn: ({ signal }) => migrationsAPI.getPlan(planId ?? "", signal),
    enabled: enabled && isAuthenticated && !!planId,
    staleTime: 5_000,
    retry: retryRead,
    refetchOnWindowFocus: false,
  });
}

export function useCreateMigrationPlan() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: (body: MigrationPlanCreateRequest) => migrationsAPI.createPlan(body),
    retry: false,
    onSuccess: (plan) => {
      queryClient.setQueryData(migrationQueryKeys.plan(scopeKey, plan.plan_id), plan);
      void reconcileMutationImpact(queryClient, "migration.plan");
    },
  });
}

export function useUpdateMigrationPlan() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({ planId, revision, body }: { planId: string; revision: number; body: MigrationPlanUpdateRequest }) =>
      migrationsAPI.updatePlan(planId, revision, body),
    retry: false,
    onSuccess: (plan) => {
      queryClient.setQueryData(migrationQueryKeys.plan(scopeKey, plan.plan_id), plan);
      void reconcileMutationImpact(queryClient, "migration.plan");
    },
  });
}

export function useDiscardMigrationPlan() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: (planId: string) => migrationsAPI.discardPlan(planId),
    retry: false,
    onSuccess: (_result: void, planId) => {
      // The 204 is authoritative: the row leaves now rather than after the list reads again.
      queryClient.setQueryData<InfiniteData<MigrationPlanListResponse, number>>(
        migrationQueryKeys.plans(scopeKey),
        (data) => data && {
          ...data,
          pages: data.pages.map((page) => ({
            ...page,
            items: page.items.filter((plan) => plan.plan_id !== planId),
          })),
        },
      );
      queryClient.removeQueries({ queryKey: migrationQueryKeys.plan(scopeKey, planId), exact: true });
      void reconcileMutationImpact(queryClient, "migration.plan");
    },
    onError: (error: unknown) => {
      // A 409 is a plan started elsewhere and a 404 one already gone: either way the row is stale.
      if (error instanceof APIError && (error.status === 404 || error.status === 409)) {
        void queryClient.invalidateQueries({ queryKey: migrationQueryKeys.plans(scopeKey) });
      }
    },
  });
}

export function useExecuteMigrationPlan() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({
      planId,
      revision,
      idempotencyKey,
      body,
    }: {
      planId: string;
      revision: number;
      idempotencyKey: string;
      body: MigrationExecuteRequest;
    }) => migrationsAPI.executePlan(planId, revision, idempotencyKey, body),
    retry: false,
    onSuccess: (job) => {
      queryClient.setQueryData(migrationQueryKeys.job(scopeKey, job.job_id), job);
      void reconcileMutationImpact(queryClient, "migration.job");
    },
  });
}

export function useMigrationAction() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: ({
      jobId,
      action,
      body,
    }: {
      jobId: string;
      action: MigrationAction;
      body: MigrationActionRequest;
    }) => migrationsAPI.action(jobId, action, body),
    retry: false,
    onSuccess: (job: MigrationJob) => {
      queryClient.setQueryData(migrationQueryKeys.job(scopeKey, job.job_id), job);
      void reconcileMutationImpact(queryClient, "migration.job");
    },
    onError: (error: unknown, { jobId }) => {
      // A 409 means the job moved on (another operator, or the worker). Read it now rather than
      // at the next poll, 30 s away for a waiting job, so the dialog shows what it moved to.
      if (error instanceof APIError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: migrationQueryKeys.job(scopeKey, jobId) });
      }
    },
  });
}

export function migrationIsActive(job: Pick<MigrationJob, "phase" | "status">): boolean {
  return ACTIVE_PHASES.has(job.phase) || ["pending", "running", "waiting", "unknown", "manual_recovery"].includes(job.status);
}

/** Whether the job is moving on its own, so its next state is worth polling for every few seconds. */
export function migrationIsProgressing(job: Pick<MigrationJob, "phase" | "status">): boolean {
  return PROGRESSING_PHASES.has(job.phase) || ["pending", "running", "unknown"].includes(job.status);
}
