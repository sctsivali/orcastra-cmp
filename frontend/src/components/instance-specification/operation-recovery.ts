import type { InstanceSpecificationUpdateRequest } from "@/types/api";

/** Exact retry material. It lives only in this JavaScript document and is lost on reload. */
export interface SpecificationOperationAttempt {
  instanceName: string;
  host: string;
  project: string;
  etag: string;
  idempotencyKey: string;
  request: InstanceSpecificationUpdateRequest;
  createdAt: number;
}

/** The only specification-operation data allowed in sessionStorage. */
export interface SpecificationJobIdentity {
  instanceName: string;
  host: string;
  project: string;
  jobId: string;
}

const STORAGE_PREFIX = "orcastra.specification-job.v2";
const LEGACY_STORAGE_PREFIX = "orcastra.specification-operation.v1";
const volatileAttempts = new Map<string, SpecificationOperationAttempt>();

function hash(value: string): string {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return (result >>> 0).toString(16);
}

function identityHash(
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): string {
  return hash(`${actorScope}\n${host}\n${project}\n${instanceName}`);
}

function storageKey(
  prefix: string,
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): string {
  return `${prefix}.${identityHash(actorScope, instanceName, host, project)}`;
}

function volatileKey(
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): string {
  return identityHash(actorScope, instanceName, host, project);
}

export function readSpecificationAttempt(
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): SpecificationOperationAttempt | null {
  return volatileAttempts.get(volatileKey(actorScope, instanceName, host, project)) ?? null;
}

export function rememberSpecificationAttempt(
  actorScope: string,
  attempt: SpecificationOperationAttempt,
): void {
  volatileAttempts.set(
    volatileKey(actorScope, attempt.instanceName, attempt.host, attempt.project),
    attempt,
  );
}

export function forgetSpecificationAttempt(
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): void {
  volatileAttempts.delete(volatileKey(actorScope, instanceName, host, project));
}

export function readSpecificationJob(
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): SpecificationJobIdentity | null {
  if (typeof window === "undefined") return null;
  try {
    // v1 stored retry bodies, ETags, and idempotency keys. Purge it on first access.
    window.sessionStorage.removeItem(
      storageKey(LEGACY_STORAGE_PREFIX, actorScope, instanceName, host, project),
    );
    const raw = window.sessionStorage.getItem(
      storageKey(STORAGE_PREFIX, actorScope, instanceName, host, project),
    );
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const item = parsed as Partial<SpecificationJobIdentity>;
    if (
      item.instanceName !== instanceName ||
      item.host !== host ||
      item.project !== project ||
      typeof item.jobId !== "string" ||
      !item.jobId
    ) return null;
    return { instanceName, host, project, jobId: item.jobId };
  } catch {
    return null;
  }
}

export function writeSpecificationJob(
  actorScope: string,
  job: SpecificationJobIdentity,
): void {
  if (typeof window === "undefined") return;
  try {
    const safe: SpecificationJobIdentity = {
      instanceName: job.instanceName,
      host: job.host,
      project: job.project,
      jobId: job.jobId,
    };
    window.sessionStorage.setItem(
      storageKey(STORAGE_PREFIX, actorScope, job.instanceName, job.host, job.project),
      JSON.stringify(safe),
    );
  } catch {
    // A blocked session store only removes reload recovery. Exact in-document polling remains.
  }
}

export function clearSpecificationRecovery(
  actorScope: string,
  instanceName: string,
  host: string,
  project: string,
): void {
  forgetSpecificationAttempt(actorScope, instanceName, host, project);
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(
      storageKey(STORAGE_PREFIX, actorScope, instanceName, host, project),
    );
    window.sessionStorage.removeItem(
      storageKey(LEGACY_STORAGE_PREFIX, actorScope, instanceName, host, project),
    );
  } catch {
    // A blocked session store requires no cleanup.
  }
}
