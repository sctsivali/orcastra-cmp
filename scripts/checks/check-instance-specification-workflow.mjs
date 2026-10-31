#!/usr/bin/env node
// Structural guard for the typed instance specification editor and durable create tracking.
//
// These checks protect the cross-file safety contract that a typecheck cannot see: the UI must
// preflight before writing, carry ETag and idempotency headers, never offer disk shrink, keep
// Tenant users out of the edit action, and persist only the safe operation identity needed to
// resume polling after navigation or reload.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(root, path), "utf8");
const failures = [];

function requireText(path, source, snippets) {
  for (const snippet of snippets) {
    if (!source.includes(snippet)) failures.push(`${path} is missing ${JSON.stringify(snippet)}`);
  }
}

const apiPath = "frontend/src/lib/api.ts";
const api = read(apiPath);
requireText(apiPath, api, [
  '"/instances/preflight"',
  '"/instances/write-policy"',
  '"Idempotency-Key": idempotencyKey',
  '"If-Match": etag',
  "/specification/preflight",
  "/instances/operations/",
]);

const hooksPath = "frontend/src/hooks/use-lxd.ts";
const hooks = read(hooksPath);
requireText(hooksPath, hooks, [
  "queryFn: ({ signal }) =>\n      api.instances.getSpecification(name, host, project, { signal })",
  "queryFn: ({ signal }) => api.instances.getOperation(jobId!, { signal })",
]);

const specificationRead = api.match(
  /getSpecification: async \([\s\S]*?\n  \},\n\n  preflightSpecification:/,
)?.[0] ?? "";
for (const marker of ["opts: ReadOptions = {}", "{ signal: opts.signal }"]) {
  if (!specificationRead.includes(marker)) {
    failures.push(`${apiPath} specification reads do not propagate ${marker}`);
  }
}

const createPath = "frontend/src/components/instance-create-form.tsx";
const create = read(createPath);
requireText(createPath, create, [
  "preflightCreate.mutateAsync(payload)",
  "preview.plan_revision",
  "preview.required_acknowledgements",
  "jobId: result.job_id",
  '"user.tags": [...form.tags].sort().join(",")',
  "submission.payload.name",
  "submission.payload.host",
  "submission.payload.project",
  "submission.payload.instance_type",
  "error.status === 408",
  "error.status >= 500",
  "!(error instanceof APIError)",
  "focusCreateError(mapped, submission.fence, false)",
  "useReadOnlyMode()",
  "createDisabledReason",
  "policyMutationDisabledReason",
  "submissionDisabledReason",
  'htmlFor="create-cpu"',
  'htmlFor="create-memory"',
  'htmlFor="create-storage"',
  'matchMedia("(prefers-reduced-motion: reduce)")',
]);
const baseInventoryPath = "frontend/src/components/instance-create/use-base-inventory.ts";
const baseInventory = read(baseInventoryPath);
requireText(baseInventoryPath, baseInventory, [
  "useInstanceWritePolicy(host, {",
  "!writePolicy.isFetching",
  "!writePolicy.isError",
  'writePolicy.data.mode === "full"',
  "writePolicy.data.can_use_advanced_create === true",
]);
if (create.indexOf("preflightCreate.mutateAsync(payload)") > create.indexOf("createAccepted(submission)")) {
  failures.push(`${createPath} can submit before its server preflight`);
}
const advancedPolicyGate = baseInventory.match(
  /const canUseAdvancedCreate =([\s\S]*?);\n\s*const hadFullAdvancedPolicy/,
)?.[1] ?? "";
for (const guard of ["!writePolicy.isFetching", "!writePolicy.isError", 'data.mode === "full"']) {
  if (!advancedPolicyGate.includes(guard)) {
    failures.push(`${baseInventoryPath} advanced create gate is missing ${guard}`);
  }
}
const acceptedCreate = create.match(
  /const createAccepted = async[\s\S]*?\n\s*const confirmPreflight/,
)?.[0] ?? "";
for (const mutable of [
  "form.name",
  "form.selectedNode",
  "form.selectedProject",
  "form.type",
  "form.tags",
  "wantedImageType",
]) {
  if (acceptedCreate.includes(mutable)) {
    failures.push(`${createPath} reads mutable ${mutable} after replaying an accepted snapshot`);
  }
}
const pendingAt = acceptedCreate.indexOf("addPendingCreate");
const navigationAt = acceptedCreate.indexOf('navigate("/inventory/instances")');
const tagSyncAt = acceptedCreate.indexOf("api.tags.updateInstanceTags");
if (!(pendingAt >= 0 && navigationAt > pendingAt)) {
  failures.push(`${createPath} must persist the accepted job before navigation`);
}
if (tagSyncAt >= 0) {
  failures.push(`${createPath} must leave tag materialization to the durable create job`);
}

const createRouterPath = "backend/app/routers/instances.py";
const createRouter = read(createRouterPath);
requireText(createRouterPath, createRouter, [
  "def _normalize_create_tags",
  'if "user.tags" not in config',
  "TagService.validate_tag_name(tag)",
  '"tags": tags_value.split(",") if tags_value else []',
  "request = _normalize_create_tags(request)",
]);
const firstNormalize = createRouter.indexOf("request = _normalize_create_tags(request)");
const firstHashAfterNormalize = createRouter.indexOf("_create_request_hash(request)", firstNormalize);
if (!(firstNormalize >= 0 && firstHashAfterNormalize > firstNormalize)) {
  failures.push(`${createRouterPath} must normalize create tags before durable hashing`);
}

const createServicePath = "backend/app/services/lxd/instance.py";
const createService = read(createServicePath);
requireText(createServicePath, createService, [
  "async def _upsert_materialized_create_tags",
  '.on_conflict_do_nothing(index_elements=[Tag.name])',
  '.on_conflict_do_nothing(constraint="uq_tag_assignment_resource")',
  "materialized_tags = _requested_create_tags(persisted)",
  "await _upsert_materialized_create_tags(db, persisted, materialized_tags)",
]);
const finishCreate = createService.match(
  /async def _finish_create_job[\s\S]*?\n\ndef _is_graceful_timeout/,
)?.[0] ?? "";
const tagUpsertAt = finishCreate.indexOf("await _upsert_materialized_create_tags");
const terminalCommitAt = finishCreate.indexOf("await db.commit()");
if (!(tagUpsertAt >= 0 && terminalCommitAt > tagUpsertAt)) {
  failures.push(`${createServicePath} must commit tag assignments with the terminal create CAS`);
}

const createErrorPath = "frontend/src/components/instance-create/daemon-error.ts";
const createError = read(createErrorPath);
requireText(createErrorPath, createError, [
  'normalizedCode.includes("NETWORK")',
  'normalizedCode.includes("STORAGE")',
  'normalizedCode.includes("IMAGE")',
  'normalizedCode.includes("PROFILE")',
  'normalized === "name"',
  'normalized.includes("device")',
]);

const rulesPath = "frontend/src/components/instance-specification/rules.ts";
const rules = read(rulesPath);
requireText(rulesPath, rules, [
  "Root disk size can only be increased.",
  "rootBytes <= specification.root_disk.value_bytes",
  "Number.isSafeInteger(cpu)",
]);

const dialogPath = "frontend/src/components/instance-specification/instance-specification-dialog.tsx";
const dialog = read(dialogPath);
requireText(dialogPath, dialog, [
  'caught.status === 412',
  '"graceful_restart"',
  '"Apply and restart"',
  'job.status === "partial"',
  'job.status === "unknown"',
  "preflightInFlightRef.current",
  "writeInFlightRef.current",
  "!(error instanceof APIError)",
  "error.status === 408",
  "error.status >= 500",
  "writeSpecificationJob(scopeKey, acceptedJob)",
  "matchesSpecificationOperation(acceptedJob, nextJob)",
  "matchesSpecificationOperation(expectedJob, jobRead.data)",
  "setPollingHalted(true)",
  "setIdentityConflict",
  "writeDisabledReason",
]);

const readOnlyPath = "frontend/src/hooks/use-read-only-mode.ts";
const readOnly = read(readOnlyPath);
// Fail-closed, but on the verdict rather than the fetch: a good verdict answers until it is five
// minutes old, "checking" only before the first one, and "unavailable" once there is none.
requireText(readOnlyPath, readOnly, [
  "data !== undefined && Date.now() - dataUpdatedAt < WRITE_VERDICT_MAX_AGE_MS",
  'isReadOnly\n      ? "read_only"',
  'isLoading || isFetching\n      ? "checking"\n      : "unavailable"',
  "writeAvailability",
]);

const recoveryPath = "frontend/src/components/instance-specification/operation-recovery.ts";
const recovery = read(recoveryPath);
requireText(recoveryPath, recovery, [
  "orcastra.specification-job.v2",
  "orcastra.specification-operation.v1",
  "volatileAttempts",
  "writeSpecificationJob",
]);
const safeJobProjection = recovery.match(
  /const safe: SpecificationJobIdentity = \{([\s\S]*?)\n\s*\};/,
)?.[1] ?? "";
for (const identity of ["instanceName", "host", "project", "jobId"]) {
  if (!safeJobProjection.includes(`${identity}:`)) {
    failures.push(`${recoveryPath} safe job projection is missing ${identity}`);
  }
}
for (const mutationMaterial of ["etag", "idempotencyKey", "request", "cpu_count", "memory_bytes", "root_disk_bytes"]) {
  if (safeJobProjection.includes(mutationMaterial)) {
    failures.push(`${recoveryPath} persists forbidden mutation material ${mutationMaterial}`);
  }
}
const rememberAttempt = recovery.match(
  /export function rememberSpecificationAttempt[\s\S]*?\n\}/,
)?.[0] ?? "";
if (rememberAttempt.includes("sessionStorage")) {
  failures.push(`${recoveryPath} must keep ambiguous PATCH retry material in memory only`);
}

const actionsPath = "frontend/src/app/inventory/instances/components/instance-row-actions.ts";
const actions = read(actionsPath);
requireText(actionsPath, actions, [
  'id: "edit-specification"',
  'label: "Edit Specifications"',
  "if (canManage)",
]);

const pendingPath = "frontend/src/lib/pending-creates.ts";
const pending = read(pendingPath);
requireText(pendingPath, pending, [
  "window.sessionStorage.setItem",
  "jobId: entry.jobId",
  "Explicit projection prevents a later form field from being persisted by accident.",
  "lastSignalAt?: number",
  "p.lastSignalAt ?? p.createdAt",
]);
const persistedProjection = pending.match(/const safe = entries\.map\(\(entry\) => \(\{([\s\S]*?)\n\s*\}\)\);/)?.[1] ?? "";
for (const transient of ["operationId", "phase", "jobStatus", "lastSignalAt", "description", "status", "progress", "error"]) {
  if (persistedProjection.includes(`${transient}:`)) {
    failures.push(`${pendingPath} must reload ${transient} from the authorized job API, not sessionStorage`);
  }
}
if (persistedProjection.includes("cloud_init") || persistedProjection.includes("cloud-init")) {
  failures.push(`${pendingPath} must not persist cloud-init content`);
}

const streamPath = "frontend/src/hooks/use-instance-stream.ts";
const stream = read(streamPath);
requireText(streamPath, stream, [
  'entry.jobId && entry.jobStatus !== "success"',
  "Waiting for exact create job status",
]);

const pendingJobsPath = "frontend/src/hooks/use-pending-create-jobs.ts";
const pendingJobs = read(pendingJobsPath);
requireText(pendingJobsPath, pendingJobs, [
  "api.instances.getOperation(entry.jobId!, {",
  'job.status === "partial"',
  'job.status === "unknown"',
  "reconcileInstanceFleet(queryClient, scopeKey, { fresh: true })",
  "lastSignalAt: Date.now()",
  "matchesCreateOperation(entry, job)",
  "TERMINAL.has(job.status)",
]);

const identityPath = "frontend/src/lib/instance-operation-identity.ts";
const identity = read(identityPath);
requireText(identityPath, identity, [
  "matchesCreateOperation",
  "matchesSpecificationOperation",
  "job.kind === kind",
  "job.job_id === expected.jobId",
  "job.instance_name === expected.instanceName",
  "job.host === expected.host",
  '(job.project || "default") === (expected.project || "default")',
]);

const instancesPagePath = "frontend/src/app/inventory/instances/page.tsx";
const instancesPage = read(instancesPagePath);
requireText(instancesPagePath, instancesPage, [
  '(!p.jobId || p.jobStatus === "success")',
]);

const pendingRowsPath = "frontend/src/app/inventory/instances/components/pending-create-rows.tsx";
const pendingRows = read(pendingRowsPath);
requireText(pendingRowsPath, pendingRows, [
  "Review configuration",
  "View instance",
  "Reload current state",
  'p.jobStatus === "failure"',
  'p.jobStatus === "partial"',
  'p.jobStatus === "unknown"',
]);

const tablePath = "frontend/src/app/inventory/instances/components/instances-table.tsx";
const table = read(tablePath);
requireText(tablePath, table, [
  "disabled={!!props.writeDisabledReason}",
  'title={props.writeDisabledReason ?? "Create a new instance"}',
]);

const rowActionsPath = "frontend/src/components/ui/row-actions.tsx";
const rowActions = read(rowActionsPath);
requireText(rowActionsPath, rowActions, [
  "triggerRef.current?.focus({ preventScroll: true })",
]);

const preflightDialogPath = "frontend/src/components/instance-create/create-preflight-dialog.tsx";
const preflightDialog = read(preflightDialogPath);
requireText(preflightDialogPath, preflightDialog, [
  "sm:grid-cols-[7rem_minmax(0,1fr)]",
  'className="min-w-0 break-words"',
  'className="min-w-0 break-all"',
]);

for (const path of [
  "frontend/src/hooks/use-instance-stream.ts",
  "frontend/src/hooks/use-pending-create-jobs.ts",
  "frontend/src/components/instance-specification/instance-specification-dialog.tsx",
]) {
  const lines = read(path).split("\n").length - 1;
  if (lines >= 500) failures.push(`${path} has ${lines} lines; focused frontend files must stay under 500`);
}

const accessE2EPath = "frontend/e2e/instance-access-policy.spec.cjs";
const accessE2E = read(accessE2EPath);
requireText(accessE2EPath, accessE2E, [
  "Partner safe policy",
  "Admin full policy",
  "cached full policy cannot authorize advanced create after a refetch error",
  "read-only mode disables direct and empty-state create actions",
  "direct create stays disabled while global write availability is loading",
  "specification edit stays disabled when global write availability cannot be verified",
  "Tenant direct create is redirected",
  "mismatched durable job",
]);

const operationE2EPath = "frontend/e2e/instance-operation-reliability.spec.cjs";
const operationE2E = read(operationE2EPath);
requireText(operationE2EPath, operationE2E, [
  "mismatched specification job in the PATCH response",
  "mismatched specification poll halts",
  "Operation identity conflict",
]);

const summaryPath = "frontend/src/components/instance-specification/operation-summary.tsx";
const summary = read(summaryPath);
requireText(summaryPath, summary, [
  "return formatBytes(value);",
  "<dd>{formatBytes(disk)}</dd>",
]);

if (failures.length > 0) {
  console.error("Instance workflow safety contract failed:");
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log("instance workflow: preflight, typed edit, concurrency, and durable polling are wired");
