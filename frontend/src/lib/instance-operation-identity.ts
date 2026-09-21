import type { PendingCreate } from "@/lib/pending-creates";
import type { InstanceOperationJob } from "@/types/api";

export interface SpecificationOperationIdentity {
  jobId: string;
  instanceName: string;
  host: string;
  project: string;
}

function matchesOperationResource(
  expected: SpecificationOperationIdentity,
  job: InstanceOperationJob,
  kind: InstanceOperationJob["kind"],
): boolean {
  return job.kind === kind &&
    job.job_id === expected.jobId &&
    job.instance_name === expected.instanceName &&
    job.host === expected.host &&
    (job.project || "default") === (expected.project || "default");
}

/** Refuse to project an operation read onto a different resource identity. */
export function matchesCreateOperation(
  pending: Pick<PendingCreate, "jobId" | "name" | "host" | "project">,
  job: InstanceOperationJob,
): boolean {
  if (!pending.jobId) return false;
  return matchesOperationResource({
    jobId: pending.jobId,
    instanceName: pending.name,
    host: pending.host,
    project: pending.project || "default",
  }, job, "create");
}

/** A specification status is safe to project only onto the exact accepted target. */
export function matchesSpecificationOperation(
  expected: SpecificationOperationIdentity,
  job: InstanceOperationJob,
): boolean {
  return matchesOperationResource(expected, job, "specification");
}
