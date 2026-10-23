"use client";

import { useEffect, useMemo, useRef } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { migrationsAPI } from "@/lib/migrations-api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { APIError } from "@/types/api";
import type {
  MigrationAction,
  MigrationActionRequest,
  MigrationExecuteRequest,
  MigrationJob,
  MigrationJobPhase,
  MigrationPlanCreateRequest,
  MigrationPlanUpdateRequest,
} from "@/types/migrations";

const ACTIVE_PHASES = new Set<MigrationJobPhase>([
  "queued",
  "preparing_target",
  "preparing_recovery",
  "checkpointing",
  "initial_sync",
  "cutover_pending",
  "quiescing",
  "final_sync",
  "target_validating",
  "external_cutover_pending",
  "cutting_over",
  "verifying",
  "source_retained",
  "cleanup_due",
  "cleaning_up",
  "unknown",
  "reconciling",
  "rollback_available",
  "rolling_back",
  "cleanup_blocked",
  "manual_recovery",
]);

function retryRead(failureCount: number, error: unknown): boolean {
  return !(error instanceof APIError && [401, 403, 404, 429].includes(error.status)) && failureCount < 2;
}

export const migrationQueryKeys = {
  all: (actorScope: string) => ["migrations", actorScope] as const,
  jobs: (actorScope: string) => ["migrations", actorScope, "jobs"] as const,
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
 * Starting a job reconciles on the response, when nothing has moved yet; the backend clears its
 * caches when the job commits each verdict, and this is the browser's half, so the Overview and
 * Instances pages do not keep the instance on its old cluster until their next poll.
 */
function useReconcileFinishedMigrations(jobs: readonly MigrationJob[] | undefined): void {
  const queryClient = useQueryClient();
  const seen = useRef(new Map<string, MigrationJobPhase>());
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
    if (changed) void reconcileMutationImpact(queryClient, "migration.job");
  }, [jobs, queryClient]);
}

export function useMigrationJobs(enabled = true) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const query = useInfiniteQuery({
    queryKey: migrationQueryKeys.jobs(scopeKey),
    queryFn: ({ pageParam, signal }) =>
      migrationsAPI.listJobs({ limit: 100, offset: pageParam }, signal),
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
        page.items.some((job) => migrationIsActive(job)),
      )
        ? 3_000
        : 15_000,
    refetchIntervalInBackground: false,
    retry: retryRead,
    refetchOnWindowFocus: false,
  });
  useReconcileFinishedMigrations(
    useMemo(() => query.data?.pages.flatMap((page) => page.items), [query.data]),
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
    refetchInterval: (query) =>
      query.state.data && migrationIsActive(query.state.data) ? 2_000 : false,
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
  });
}

export function migrationIsActive(job: Pick<MigrationJob, "phase" | "status">): boolean {
  return ACTIVE_PHASES.has(job.phase) || ["pending", "running", "waiting", "unknown", "manual_recovery"].includes(job.status);
}
