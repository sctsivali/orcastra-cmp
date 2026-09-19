"use client";

/**
 * React Query hooks for the task panel and the Operations page.
 *
 * Every key nests under one of two prefixes, `["tasks"]` and `["cluster-log"]`, so a single
 * invalidateQueries per prefix reaches everything (the feed does that on a late `hello` and on
 * `resync`). Polling is a fallback: each list polls at its POLL_MS tier only while the feed is
 * not connected, and never in a background tab.
 */

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { toast } from "@/hooks/use-toast";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { tasksAPI } from "@/lib/tasks-api";
import { DYNAMIC_DATA_CONFIG, POLL_MS, jitter } from "@/lib/query-config";
import type {
  CancelResponse,
  ClusterLogEntry,
  ClusterLogResponse,
  TaskDetailResponse,
  TaskListResponse,
  TaskRecord,
} from "@/types/tasks";

export const taskKeys = {
  all: (scopeKey: string) => ["tasks", scopeKey] as const,
  summary: (scopeKey: string) => ["tasks", scopeKey, "summary"] as const,
  list: (scopeKey: string, limit: number) => ["tasks", scopeKey, "list", { limit }] as const,
  window: (scopeKey: string, since: string) => ["tasks", scopeKey, "window", { since }] as const,
  queue: (scopeKey: string) => ["tasks", scopeKey, "queue"] as const,
  detail: (scopeKey: string, id: string) => ["tasks", scopeKey, "detail", id] as const,
};

export const logKeys = {
  all: (scopeKey: string) => ["cluster-log", scopeKey] as const,
  list: (scopeKey: string, limit: number) =>
    ["cluster-log", scopeKey, "list", { limit }] as const,
};

/** The Operations page window is bounded by the server's page cap. */
const WINDOW_LIMIT = 500;
const LOG_CAP = 500;
const INVALIDATE_DEBOUNCE_MS = 1000;

const isOpen = (status: TaskRecord["status"]): boolean =>
  status === "pending" || status === "running";

export interface ActorMutationGuard {
  readonly scopeKey: string | null;
}

/**
 * Identity guard for a mutation started by the current authenticated actor.
 *
 * The object is replaced for every auth or role scope transition, rather than comparing only
 * the scope string. That also rejects a late response across an A -> B -> A transition. The
 * request already sent is deliberately left alone; only browser-side effects are suppressed.
 */
export function useActorMutationGuard(): MutableRefObject<ActorMutationGuard> {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const currentScopeKey = isAuthenticated ? scopeKey : null;
  const activeGuardRef = useRef<ActorMutationGuard>({ scopeKey: currentScopeKey });
  if (activeGuardRef.current.scopeKey !== currentScopeKey) {
    activeGuardRef.current = { scopeKey: currentScopeKey };
  }
  useEffect(
    () => () => {
      activeGuardRef.current = { scopeKey: null };
    },
    [],
  );
  return activeGuardRef;
}

/** The +/-10% spread, resolved once per hook instance (query-config.ts explains why). */
function usePollInterval(base: number, live: boolean): number | false {
  const [interval] = useState(() => jitter(base));
  return live ? false : interval;
}

export function useTaskSummary(feedConnected: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const refetchInterval = usePollInterval(POLL_MS.active, feedConnected);
  return useRefreshableQuery<TaskListResponse>({
    queryKey: taskKeys.summary(scopeKey),
    queryFn: ({ fresh }) => tasksAPI.summary({ fresh }),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated,
    refetchInterval,
    refetchIntervalInBackground: false,
  });
}

export function useTaskList(feedConnected: boolean, limit: number) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const refetchInterval = usePollInterval(POLL_MS.watched, feedConnected);
  return useRefreshableQuery<TaskListResponse>({
    queryKey: taskKeys.list(scopeKey, limit),
    queryFn: ({ fresh }) => tasksAPI.list({ limit }, { fresh }),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated,
    refetchInterval,
    refetchIntervalInBackground: false,
    // Show More changes the key; the rows in hand stay up until the larger page lands, so the
    // toolbar, the search text and the scroll position survive the click.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === scopeKey ? previous : undefined,
  });
}

export function useTaskWindow(since: string, feedConnected: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const refetchInterval = usePollInterval(POLL_MS.watched, feedConnected);
  return useRefreshableQuery<TaskListResponse>({
    queryKey: taskKeys.window(scopeKey, since),
    queryFn: ({ fresh }) => tasksAPI.list({ since, limit: WINDOW_LIMIT }, { fresh }),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated && since !== "",
    refetchInterval,
    refetchIntervalInBackground: false,
  });
}

/** A grouped view derived server side, so it polls at the watched tier whatever the feed does. */
export function useTaskQueue(enabled: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [refetchInterval] = useState(() => jitter(POLL_MS.watched));
  return useRefreshableQuery({
    queryKey: taskKeys.queue(scopeKey),
    queryFn: ({ fresh }) => tasksAPI.queue({ fresh }),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated && enabled,
    refetchInterval,
    refetchIntervalInBackground: false,
  });
}

/** Patched by `task` and `log` frames for its id, so it tracks status without a poll. */
export function useTaskDetail(id: string | null) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery<TaskDetailResponse>({
    queryKey: taskKeys.detail(scopeKey, id ?? ""),
    queryFn: () => tasksAPI.get(id ?? ""),
    ...DYNAMIC_DATA_CONFIG,
    staleTime: 0,
    enabled: isAuthenticated && id !== null && id !== "",
  });
}

export function useClusterLog(enabled: boolean, feedConnected: boolean, limit: number) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const refetchInterval = usePollInterval(POLL_MS.watched, feedConnected);
  return useRefreshableQuery<ClusterLogResponse>({
    queryKey: logKeys.list(scopeKey, limit),
    queryFn: ({ fresh }) => tasksAPI.log({ limit }, { fresh }),
    ...DYNAMIC_DATA_CONFIG,
    enabled: isAuthenticated && enabled,
    refetchInterval,
    refetchIntervalInBackground: false,
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === scopeKey ? previous : undefined,
  });
}

export function useCancelTask() {
  const queryClient = useQueryClient();
  const activeActorGuardRef = useActorMutationGuard();
  const mutation = useMutation<
    CancelResponse,
    Error,
    { id: string; requestActorGuard: ActorMutationGuard }
  >({
    mutationFn: ({ id }) => tasksAPI.cancel(id),
    onSuccess: (_response, { requestActorGuard }) => {
      if (
        activeActorGuardRef.current !== requestActorGuard ||
        requestActorGuard.scopeKey === null
      ) {
        return;
      }
      void queryClient.invalidateQueries({
        queryKey: taskKeys.all(requestActorGuard.scopeKey),
      });
    },
    onError: (error, { requestActorGuard }) => {
      if (activeActorGuardRef.current !== requestActorGuard) return;
      toast({
        variant: "destructive",
        title: "Could not cancel task",
        description: error.message,
      });
    },
  });

  return {
    isPending:
      mutation.isPending &&
      mutation.variables?.requestActorGuard === activeActorGuardRef.current,
    /** Resolves false when the request completed after its actor scope was retired. */
    mutateAsync: async (id: string): Promise<boolean> => {
      const requestActorGuard = activeActorGuardRef.current;
      if (requestActorGuard.scopeKey === null) return false;
      try {
        await mutation.mutateAsync({ id, requestActorGuard });
        return activeActorGuardRef.current === requestActorGuard;
      } catch (error) {
        if (activeActorGuardRef.current !== requestActorGuard) return false;
        throw error;
      }
    },
  };
}

export interface KillConsoleSessionInput {
  instanceName: string;
  operationId: string;
  host: string;
  project: string;
}

export function useKillConsoleSession() {
  const queryClient = useQueryClient();
  const activeActorGuardRef = useActorMutationGuard();
  const mutation = useMutation<
    void,
    Error,
    KillConsoleSessionInput & { requestActorGuard: ActorMutationGuard }
  >({
    mutationFn: ({ instanceName, operationId, host, project }) =>
      tasksAPI.killConsoleSession(instanceName, operationId, host, project),
    onSuccess: (_data, { instanceName, requestActorGuard }) => {
      if (
        activeActorGuardRef.current !== requestActorGuard ||
        requestActorGuard.scopeKey === null
      ) {
        return;
      }
      toast({
        title: "Session killed",
        description: `Console session for ${instanceName} has been terminated`,
      });
      void queryClient.invalidateQueries({
        queryKey: taskKeys.all(requestActorGuard.scopeKey),
      });
    },
    onError: (error, { requestActorGuard }) => {
      if (activeActorGuardRef.current !== requestActorGuard) return;
      toast({
        variant: "destructive",
        title: "Could not kill session",
        description: error.message,
      });
    },
  });

  return {
    isPending:
      mutation.isPending &&
      mutation.variables?.requestActorGuard === activeActorGuardRef.current,
    mutate: (input: KillConsoleSessionInput): void => {
      const requestActorGuard = activeActorGuardRef.current;
      if (requestActorGuard.scopeKey === null) return;
      mutation.mutate({ ...input, requestActorGuard });
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Cache patching from feed frames
// ---------------------------------------------------------------------------------------------

const pendingInvalidations = new Map<string, ReturnType<typeof setTimeout>>();

/** One invalidation per key per second, however many frames land in between. */
function invalidateDebounced(queryClient: QueryClient, queryKey: readonly unknown[]): void {
  const id = JSON.stringify(queryKey);
  if (pendingInvalidations.has(id)) return;
  pendingInvalidations.set(
    id,
    setTimeout(() => {
      pendingInvalidations.delete(id);
      void queryClient.invalidateQueries({ queryKey });
    }, INVALIDATE_DEBOUNCE_MS),
  );
}

/**
 * Replace a row in a list-shaped cache when the frame's `seq` is higher, insert it at the top
 * when it is new, and drop past the cache's own limit. Returns the row it replaced, if any.
 */
function upsertInList(
  data: TaskListResponse,
  task: TaskRecord,
): { next: TaskListResponse; previous: TaskRecord | null; stale?: boolean } {
  const index = data.tasks.findIndex((row) => row.id === task.id);
  if (index === -1) {
    const tasks = [task, ...data.tasks];
    const capped = data.limit > 0 ? tasks.slice(0, data.limit) : tasks;
    return {
      next: {
        ...data,
        tasks: capped,
        total: data.total + 1,
        has_more: data.has_more || capped.length < tasks.length,
      },
      previous: null,
    };
  }
  const previous = data.tasks[index];
  // A frame older than the row in hand is not a change: it must not move the counts either.
  if (previous.seq >= task.seq) return { next: data, previous: null, stale: true };
  const tasks = data.tasks.slice();
  tasks[index] = task;
  return { next: { ...data, tasks }, previous };
}

export function upsertTaskInCaches(
  queryClient: QueryClient,
  scopeKey: string,
  task: TaskRecord,
): void {
  let previous: TaskRecord | null = null;
  let seen = false;
  let stale = false;

  for (const prefix of [
    ["tasks", scopeKey, "list"],
    ["tasks", scopeKey, "window"],
  ] as const) {
    for (const [key, data] of queryClient.getQueriesData<TaskListResponse>({ queryKey: prefix })) {
      if (!data) continue;
      const result = upsertInList(data, task);
      if (result.stale) stale = true;
      if (result.previous) {
        seen = true;
        previous = previous ?? result.previous;
      }
      if (result.next !== data) queryClient.setQueryData(key, result.next);
    }
  }
  // Rejected as older than what a list already holds: the row and the counts stay as they are.
  if (stale) return;

  const detailKey = taskKeys.detail(scopeKey, task.id);
  const detail = queryClient.getQueryData<TaskDetailResponse>(detailKey);
  if (detail) {
    if (!seen) {
      seen = true;
      previous = detail.task;
    }
    if (detail.task.seq < task.seq) {
      queryClient.setQueryData<TaskDetailResponse>(detailKey, { ...detail, task });
      // A line linked to the task after the fact bumps its seq without a log frame of its
      // own (the lifecycle line the writer could only attach in a later sweep), so the open
      // dialog re-reads its lines.
      invalidateDebounced(queryClient, detailKey);
    }
  }

  // The summary is counts only. With the old row in hand the running delta is exact; without
  // it the count could be anything, so a debounced refetch is the honest move.
  const summaryKey = taskKeys.summary(scopeKey);
  const summary = queryClient.getQueryData<TaskListResponse>(summaryKey);
  if (summary && previous !== null) {
    const delta = (isOpen(task.status) ? 1 : 0) - (isOpen(previous.status) ? 1 : 0);
    const failed = task.status === "failure" && previous.status !== "failure" ? 1 : 0;
    if (delta !== 0 || failed !== 0) {
      queryClient.setQueryData<TaskListResponse>(summaryKey, {
        ...summary,
        running: Math.max(0, summary.running + delta),
        failed_recent: summary.failed_recent + failed,
      });
    }
  } else if (summary) {
    invalidateDebounced(queryClient, summaryKey);
  }

  const queueKey = taskKeys.queue(scopeKey);
  if (queryClient.getQueryData(queueKey) !== undefined) {
    invalidateDebounced(queryClient, queueKey);
  }
}

export function appendLogInCaches(
  queryClient: QueryClient,
  scopeKey: string,
  entry: ClusterLogEntry,
): void {
  const lists = queryClient.getQueriesData<ClusterLogResponse>({
    queryKey: ["cluster-log", scopeKey, "list"],
  });
  for (const [key, data] of lists) {
    if (!data || data.entries.some((row) => row.id === entry.id)) continue;
    const entries = [entry, ...data.entries].slice(0, LOG_CAP);
    queryClient.setQueryData<ClusterLogResponse>(key, {
      ...data,
      entries,
      total: data.total + 1,
    });
  }

  if (!entry.task_id) return;
  const detailKey = taskKeys.detail(scopeKey, entry.task_id);
  const detail = queryClient.getQueryData<TaskDetailResponse>(detailKey);
  if (!detail || detail.log.some((row) => row.id === entry.id)) return;
  // The detail log is oldest first (the server orders by ts), so a new line goes at the end.
  queryClient.setQueryData<TaskDetailResponse>(detailKey, {
    ...detail,
    log: [...detail.log, entry].slice(-LOG_CAP),
  });
}
