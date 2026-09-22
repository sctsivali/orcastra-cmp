export interface SubmissionFence {
  actorScope: string;
  clusterEpoch: number;
  selectionEpoch: number;
  formRevision: number;
  host: string;
  project: string;
}

// A POST may complete after an ordinary same-target edit. It may never publish across an actor,
// cluster, project, or selection generation. Preflight additionally owns the exact form revision.
export const SUBMISSION_TARGET_FENCE_FIELDS = [
  "actorScope",
  "clusterEpoch",
  "selectionEpoch",
  "host",
  "project",
] as const;

export function submissionFenceMatches(
  captured: SubmissionFence,
  current: SubmissionFence,
  requireFormRevision: boolean,
): boolean {
  return (
    SUBMISSION_TARGET_FENCE_FIELDS.every((field) => captured[field] === current[field]) &&
    (!requireFormRevision || captured.formRevision === current.formRevision)
  );
}
