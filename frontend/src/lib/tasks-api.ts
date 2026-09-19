/**
 * The task journal surface: /tasks, /tasks/queue, /tasks/{id} and /cluster-log.
 *
 * Its own file for the same reason extensions-api.ts is: lib/api.ts is past 2000 lines. It uses
 * the same fetchAPI so timeouts, GET deduplication and the 401 recovery path behave identically.
 *
 * important: these handlers keep no response cache, so `fresh` only bypasses the in-flight
 * dedupe (`force`) and never sends `?fresh=true`. The parameter would be dropped by FastAPI
 * while the middleware still charged the request against the scarce per-operator bypass
 * budget, which is exactly the defect fetchUncachedRead in lib/api.ts documents.
 */

import { fetchAPI, type ReadOptions } from "@/lib/api";
import type {
  CancelResponse,
  ClusterLogResponse,
  LogKind,
  LogSeverity,
  TaskDetailResponse,
  TaskKind,
  TaskListResponse,
  TaskQueueResponse,
  TaskStatus,
} from "@/types/tasks";

export interface TaskListParams {
  status?: TaskStatus[];
  cluster_id?: string;
  project?: string;
  actor?: string;
  kind?: TaskKind[];
  since?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface LogListParams {
  kind?: LogKind[];
  severity?: LogSeverity[];
  cluster_id?: string;
  project?: string;
  actor?: string;
  q?: string;
  since?: string;
  task_id?: string;
  limit?: number;
  offset?: number;
}

type ParamValue = string | number | readonly string[] | undefined;

/** Lists go on the wire as csv; an empty list or an undefined value is omitted. */
function withQuery(path: string, params: Record<string, ParamValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      if (value.length) search.set(key, value.join(","));
      continue;
    }
    if (typeof value === "number") {
      search.set(key, String(value));
      continue;
    }
    if (value !== "") search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

function uncachedRead<T>(endpoint: string, opts: ReadOptions = {}): Promise<T> {
  return fetchAPI<T>(endpoint, {}, undefined, false, { force: opts.fresh === true });
}

const seg = (value: string): string => encodeURIComponent(value);

export const tasksAPI = {
  list: (params: TaskListParams = {}, opts: ReadOptions = {}): Promise<TaskListResponse> =>
    uncachedRead<TaskListResponse>(withQuery("/tasks", { ...params }), opts),

  /** Counts only: `running`, `failed_recent`, `disconnected_clusters`, `follower_active`. */
  summary: (opts: ReadOptions = {}): Promise<TaskListResponse> =>
    uncachedRead<TaskListResponse>(withQuery("/tasks", { limit: 0 }), opts),

  get: (id: string): Promise<TaskDetailResponse> =>
    fetchAPI<TaskDetailResponse>(`/tasks/${seg(id)}`),

  queue: (opts: ReadOptions = {}): Promise<TaskQueueResponse> =>
    uncachedRead<TaskQueueResponse>("/tasks/queue", opts),

  /** DELETE /tasks/{id}; a refusal is a 409 whose APIError message is the server's sentence. */
  cancel: (id: string): Promise<CancelResponse> =>
    fetchAPI<CancelResponse>(`/tasks/${seg(id)}`, { method: "DELETE" }),

  log: (params: LogListParams = {}, opts: ReadOptions = {}): Promise<ClusterLogResponse> =>
    uncachedRead<ClusterLogResponse>(withQuery("/cluster-log", { ...params }), opts),

  /**
   * Websocket-class operations (exec, console) are never cancelled through /tasks; they are
   * ended the way the Sessions page ends them.
   */
  // The console route answers 200 with `success: false` when the cluster did not act (its
  // ActionResponse idiom), so the outcome is read from the body, not the status.
  killConsoleSession: async (
    instanceName: string,
    operationId: string,
    host: string,
    project: string,
  ): Promise<void> => {
    const result = await fetchAPI<{ success?: boolean; error?: string | null; message?: string | null }>(
      `/console/${seg(instanceName)}/active-sessions/${seg(operationId)}` +
        `?host=${seg(host)}&project=${seg(project || "default")}`,
      { method: "DELETE" },
    );
    if (result && result.success === false) {
      throw new Error(result.error || result.message || "The cluster did not end the session");
    }
  },
};
