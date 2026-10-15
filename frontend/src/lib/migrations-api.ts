import { fetchAPI } from "@/lib/api";
import type {
  MigrationAction,
  MigrationActionRequest,
  MigrationExecuteRequest,
  MigrationJob,
  MigrationJobListResponse,
  MigrationPlan,
  MigrationPlanCreateRequest,
  MigrationPlanListResponse,
  MigrationPlanUpdateRequest,
} from "@/types/migrations";

function migrationPath(id: string): string {
  return `/migrations/${encodeURIComponent(id)}`;
}

function planPath(id: string): string {
  return `/migrations/plans/${encodeURIComponent(id)}`;
}

const ACTION_PATH: Record<MigrationAction, string> = {
  cancel: "cancel",
  cutover: "cutover",
  confirm_external_cutover: "confirm-external-cutover",
  rollback: "rollback",
  restore_checkpoint: "restore-checkpoint",
  cleanup: "cleanup",
  recheck: "recheck",
  release: "release",
};

export const migrationsAPI = {
  listJobs: (
    { limit = 100, offset = 0 }: { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) =>
    fetchAPI<MigrationJobListResponse>(
      `/migrations?limit=${limit}&offset=${offset}`,
      { signal },
    ),

  getJob: (jobId: string, signal?: AbortSignal) =>
    fetchAPI<MigrationJob>(migrationPath(jobId), { signal }),

  listPlans: (
    { limit = 100, offset = 0 }: { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) =>
    fetchAPI<MigrationPlanListResponse>(
      `/migrations/plans?limit=${limit}&offset=${offset}`,
      { signal },
    ),

  getPlan: (planId: string, signal?: AbortSignal) =>
    fetchAPI<MigrationPlan>(planPath(planId), { signal }),

  createPlan: (body: MigrationPlanCreateRequest) =>
    fetchAPI<MigrationPlan>(
      "/migrations/plans",
      {
        method: "POST",
        body: JSON.stringify(body),
      },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    ),

  updatePlan: (planId: string, revision: number, body: MigrationPlanUpdateRequest) =>
    fetchAPI<MigrationPlan>(
      planPath(planId),
      {
        method: "PATCH",
        headers: { "If-Match": String(revision) },
        body: JSON.stringify(body),
      },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    ),

  executePlan: (
    planId: string,
    revision: number,
    idempotencyKey: string,
    body: MigrationExecuteRequest,
  ) =>
    fetchAPI<MigrationJob>(
      `${planPath(planId)}/execute`,
      {
        method: "POST",
        headers: {
          "If-Match": String(revision),
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(body),
      },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    ),

  action: (jobId: string, action: MigrationAction, body: MigrationActionRequest) =>
    fetchAPI<MigrationJob>(
      `${migrationPath(jobId)}/${ACTION_PATH[action]}`,
      { method: "POST", body: JSON.stringify(body) },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    ),
};
