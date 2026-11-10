#!/usr/bin/env node
// Structural guard for create-form request staging and cancellable refresh reads.
//
// These invariants span React intent state, React Query, and the fetch transport. A typecheck
// cannot detect an optional inventory query becoming eager or two consumers sharing an
// abortable GET, so keep the contract explicit here.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// createRequire needs an absolute path, and CI passes the root as ".", so resolve it here the way
// the other guards that load typescript from the frontend do.
const root = resolve(
  process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."),
);
const read = (path) => readFileSync(join(root, path), "utf8");
const failures = [];
const requireFromFrontend = createRequire(join(root, "frontend/package.json"));
const ts = requireFromFrontend("typescript");

function requireText(path, source, snippets) {
  for (const snippet of snippets) {
    if (!source.includes(snippet)) failures.push(`${path} is missing ${JSON.stringify(snippet)}`);
  }
}

const refreshPath = "frontend/src/hooks/use-refreshable-query.ts";
const refresh = read(refreshPath);
requireText(refreshPath, refresh, [
  "signal: AbortSignal",
  "createRefreshIntentTracker",
  "intentRef.current?.isFresh() === true",
]);

const apiPath = "frontend/src/lib/api.ts";
const api = read(apiPath);
requireText(apiPath, api, [
  "const transportIsCallerScoped = options.signal !== undefined",
  "!transportIsCallerScoped",
  "signal?: AbortSignal",
  "{ signal: opts.signal }",
]);
const timeoutTransport = api.match(
  /async function fetchJSONWithTimeout[\s\S]*?\n\}\n\n\/\*\*\n \* In-flight GET/,
)?.[0] ?? "";
for (const snippet of [
  "const callerSignal = options.signal",
  'callerSignal?.addEventListener("abort", callerAbort, { once: true })',
  'callerSignal?.removeEventListener("abort", callerAbort)',
  'abortCause === "timeout"',
  "body: await response.json()",
]) {
  if (!timeoutTransport.includes(snippet)) {
    failures.push(`${apiPath} fetchJSONWithTimeout is missing ${JSON.stringify(snippet)}`);
  }
}
const internalTransport = api.match(
  /async function _fetchAPIInternal[\s\S]*?\n\}\n\n\/\/ VM Operations/,
)?.[0] ?? "";
if (!internalTransport.includes("if (options.signal?.aborted)")) {
  failures.push(`${apiPath} internal transport does not preserve caller cancellation`);
}

const formPath = "frontend/src/components/instance-create-form.tsx";
const form = read(formPath);
const baseInventoryPath = "frontend/src/components/instance-create/use-base-inventory.ts";
const baseInventory = read(baseInventoryPath);
const volumeInventoryPath = "frontend/src/components/instance-create/use-volume-inventory.ts";
const volumeInventory = read(volumeInventoryPath);
const networkInventoryPath = "frontend/src/components/instance-create/use-network-inventory.ts";
const networkInventory = read(networkInventoryPath);
const gpuInventoryPath = "frontend/src/components/instance-create/use-gpu-inventory.ts";
const gpuInventory = read(gpuInventoryPath);
const imageCataloguePath = "frontend/src/components/instance-create/use-image-catalogue.ts";
const imageCatalogue = read(imageCataloguePath);
const seedValidationPath = "frontend/src/components/instance-create/use-seed-validation.ts";
const seedValidationSource = read(seedValidationPath);
const fencePath = "frontend/src/components/instance-create/submission-fence.ts";
const fenceSource = read(fencePath);

requireText(formPath, form, [
  "localImageIntent",
  "remoteImageIntent",
  "volumeIntent",
  "networkIntent",
  "selectionEpochRef",
  "actorScopeRef",
  "seedValidationPending",
  'id="create-local-image-notice"',
  'id="create-iso-notice"',
  "Local images unavailable. Retry",
  "Remote catalogues unavailable. Retry",
  "pendingNicAdd",
  'id="create-default-profile-notice"',
  'pendingNicAdd ? "Preparing interface" : "Attach network"',
  "Cancel attach",
  "captureSubmissionFence",
  "submissionStillCurrent",
  "invalidatePreparedSubmission",
  "useBaseCreateInventory",
  "useCreateVolumeInventory",
  "useCreateNetworkInventory",
  "useCreateGpuInventory",
  "useImageCatalogue",
  "useCreateSeedValidation",
]);

requireText(baseInventoryPath, baseInventory, [
  "useNodePickerOptions()",
  "useProjects(host",
  "useStoragePoolList(host",
  "useInstanceWritePolicy(host",
  "enabled: actorSelectionActive && !!host && projectLimited",
  "requestEpoch: clusterEpoch",
  "requestEpoch: selectionEpoch",
]);
requireText(volumeInventoryPath, volumeInventory, [
  "useStorageVolumes(undefined, host",
  "enabled: actorSelectionActive && !!host && enabledByIntent",
  "requestEpoch: clusterEpoch",
]);
requireText(networkInventoryPath, networkInventory, [
  "useNetworks(host, project || undefined",
  'useProfile("default", host, project || undefined',
  "enabled: actorSelectionActive && !!host && enabledByIntent",
  "requestEpoch: selectionEpoch",
]);
requireText(gpuInventoryPath, gpuInventory, [
  "useNodeGpus(canUseAdvancedCreate ? host : \"\"",
  "actorSelectionActive && canUseAdvancedCreate && !!host && pickerOpen",
  "requestEpoch: clusterEpoch",
]);
requireText(imageCataloguePath, imageCatalogue, [
  "useImagesByHost(host, project",
  "enabled: actorSelectionActive && !!host && localIntent",
  "requestEpoch: selectionEpoch",
  "useRemoteImages({ enabled: actorSelectionActive && remoteIntent, engine })",
  "imageRows.length === 0 && (localQuery.isLoading || remoteQuery.isLoading)",
]);
requireText(seedValidationPath, seedValidationSource, [
  "if (loadingNodes || !nodeNamesLoaded) return",
  "if (fetchingProjects || fetchingImages) return",
  "if (!projectsSucceeded || !imagesSucceeded) return",
  "if (fetchingVolumes) return",
  "if (!volumesSucceeded) return",
]);
requireText(fencePath, fenceSource, [
  "SUBMISSION_TARGET_FENCE_FIELDS",
  "submissionFenceMatches",
]);

const formLines = form.split("\n").length;
if (formLines > 2850) {
  failures.push(`${formPath} regressed to ${formLines} lines; keep request orchestration in focused hooks`);
}
for (const [path, source] of [
  [baseInventoryPath, baseInventory],
  [volumeInventoryPath, volumeInventory],
  [networkInventoryPath, networkInventory],
  [gpuInventoryPath, gpuInventory],
  [imageCataloguePath, imageCatalogue],
  [seedValidationPath, seedValidationSource],
]) {
  const lines = source.split("\n").length;
  if (lines > 500) failures.push(`${path} grew to ${lines} lines; split inventory responsibilities`);
}
for (const directHook of [
  "useImagesByHost",
  "useRemoteImages",
  "useStorageVolumes",
  "useNetworks",
  "useProfile",
  "useNodeGpus",
  "useProjects",
  "useStoragePoolList",
  "useInstanceWritePolicy",
]) {
  const directCall = new RegExp(`\\b${directHook}\\s*\\(`);
  if (directCall.test(form)) {
    failures.push(`${formPath} calls ${directHook} directly; keep request staging in focused hooks`);
  }
}

const imageSeedValidation = seedValidationSource.match(
  /if \(seed\.sourceType === "image"[\s\S]*?if \(seed\.sourceType === "none"/,
)?.[0] ?? "";
const isoSeedValidation = seedValidationSource.match(
  /if \(seed\.sourceType === "none"[\s\S]*?seedCheckedRef\.current = true;\n    setSeedValidationPending\(false\);/,
)?.[0] ?? "";
if (
  imageSeedValidation.indexOf("if (!projectsSucceeded || !imagesSucceeded) return") < 0 ||
  imageSeedValidation.indexOf("if (!projectsSucceeded || !imagesSucceeded) return") >
    imageSeedValidation.indexOf("seedCheckedRef.current = true")
) {
  failures.push(`${seedValidationPath} can finalize an image seed before successful project and image reads`);
}
if (
  isoSeedValidation.indexOf("if (!volumesSucceeded) return") < 0 ||
  isoSeedValidation.indexOf("if (!volumesSucceeded) return") >
    isoSeedValidation.indexOf("seedCheckedRef.current = true")
) {
  failures.push(`${seedValidationPath} can finalize an ISO seed before a successful volume read`);
}

const addNicClick = form.match(/const addNic = \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? "";
requireText(formPath, addNicClick, [
  "if (pendingNicAdd) return",
  "requireNetworks()",
  "setPendingNicAdd(true)",
]);
if (addNicClick.includes("setNics(")) {
  failures.push(`${formPath} Attach network still fabricates a NIC before inventory settles`);
}
const nicIntent = networkInventory.match(
  /\/\/ Attach is two-phase[\s\S]*?\n  \}, \[/,
)?.[0] ?? "";
requireText(networkInventoryPath, nicIntent, [
  "if (networksQuery.isFetching || defaultProfileQuery.isFetching) return",
  "if (networksQuery.isError || defaultProfileQuery.isError) return",
  "if (!networksQuery.isSuccess || !defaultProfileQuery.isSuccess) return",
  "setPendingNicAdd(false)",
  "setNics((current) =>",
]);
for (const guard of [
  "if (networksQuery.isFetching || defaultProfileQuery.isFetching) return",
  "if (networksQuery.isError || defaultProfileQuery.isError) return",
  "if (!networksQuery.isSuccess || !defaultProfileQuery.isSuccess) return",
]) {
  if (nicIntent.indexOf(guard) > nicIntent.indexOf("setNics((current) =>")) {
    failures.push(`${networkInventoryPath} adds a NIC before ${guard}`);
  }
}

const actorReset = form.match(
  /\/\/ The actor scope is part of every read key[\s\S]*?\}, \[[^\]]*scopeKey[^\]]*\]\);/,
)?.[0] ?? "";
requireText(formPath, actorReset, [
  "formRevisionRef.current += 1",
  "seedRef.current = null",
  "setForm({ ...DEFAULT_FORM, tags: [] })",
  "setDisks([])",
  "setNics([])",
  "setGpus([])",
  "setPendingNicAdd(false)",
  "resetForCluster()",
  "resetImageCatalogue()",
  "setGpuPickerOpen(false)",
  "setAdvOpen(false)",
  "setAdv({ ...ADV_DEFAULT })",
  "projectSwitchRef.current = null",
  "setPendingSubmission(null)",
  "ambiguousSubmissionRef.current = null",
  "idempotencyRef.current = null",
  "submitInFlightRef.current = false",
  "setHasAmbiguousSubmission(false)",
  "setFieldError(null)",
  "setError(null)",
  "setChangeNotice(null)",
  'setTagInput("")',
  "setTagError(null)",
]);
if (form.includes("setAdv(ADV_DEFAULT)")) {
  failures.push(`${formPath} reuses the mutable advanced-options default object`);
}

const fenceFieldsMatch = fenceSource.match(
  /const SUBMISSION_TARGET_FENCE_FIELDS = (\[[\s\S]*?\]) as const;/,
);
if (!fenceFieldsMatch) {
  failures.push(`${fencePath} does not expose the submission target fence fields`);
} else {
  const fenceFields = JSON.parse(fenceFieldsMatch[1].replace(/,\s*\]$/, "]"));
  const sameFence = (captured, current, requireRevision) =>
    fenceFields.every((field) => captured[field] === current[field]) &&
    (!requireRevision || captured.formRevision === current.formRevision);
  const captured = {
    actorScope: "admin:1",
    clusterEpoch: 4,
    selectionEpoch: 9,
    formRevision: 12,
    host: "cluster-a",
    project: "project-a",
  };
  assert.equal(sameFence(captured, { ...captured }, true), true);
  assert.equal(sameFence(captured, { ...captured, actorScope: "partner:2" }, false), false);
  assert.equal(sameFence(captured, { ...captured, clusterEpoch: 5 }, false), false);
  assert.equal(sameFence(captured, { ...captured, selectionEpoch: 10 }, false), false);
  assert.equal(sameFence(captured, { ...captured, host: "cluster-b" }, false), false);
  assert.equal(sameFence(captured, { ...captured, project: "project-b" }, false), false);
  assert.equal(sameFence(captured, { ...captured, formRevision: 13 }, true), false);
  assert.equal(
    sameFence(captured, { ...captured, formRevision: 13 }, false),
    true,
    "an accepted POST may finish after an ordinary field edit on the same actor and target",
  );
}

const createAccepted = form.match(
  /const createAccepted = async[\s\S]*?\n  \};\n\n  const confirmPreflight/,
)?.[0] ?? "";
requireText(formPath, createAccepted, [
  "if (!submissionStillCurrent(submission.fence, false)) return false",
  "const result = await createInstance.mutateAsync",
  "addPendingCreate(submission.fence.actorScope",
]);
const createAwait = createAccepted.indexOf("const result = await createInstance.mutateAsync");
const createPostAwaitFence = createAccepted.indexOf(
  "if (!submissionStillCurrent(submission.fence, false)) return false",
  createAwait,
);
if (createAwait < 0 || createPostAwaitFence < createAwait) {
  failures.push(`${formPath} does not fence delayed create completion before side effects`);
}

const submitFlow = form.match(/const submit = async[\s\S]*?\n  \};\n\n  \/\/ Render helpers/)?.[0] ?? "";
requireText(formPath, submitFlow, [
  "const fence = captureSubmissionFence()",
  "const preview = await preflightCreate.mutateAsync(payload)",
  "if (!submissionStillCurrent(fence, true)) return",
  "fence,",
  "if (!submissionStillCurrent(fence, attemptedSubmission === null)) return",
]);
const preflightAwait = submitFlow.indexOf("const preview = await preflightCreate.mutateAsync(payload)");
const preflightFence = submitFlow.indexOf("if (!submissionStillCurrent(fence, true)) return", preflightAwait);
const pendingWrite = submitFlow.indexOf("setPendingSubmission(submission)");
if (preflightAwait < 0 || preflightFence < preflightAwait || pendingWrite < preflightFence) {
  failures.push(`${formPath} can publish a stale preflight result after actor or payload change`);
}

const intentPath = "frontend/src/components/instance-create/use-create-inventory-intent.ts";
const intent = read(intentPath);
requireText(intentPath, intent, [
  "const clusterEpochRef = useRef(0)",
  "const selectionEpochRef = useRef(0)",
  "clusterEpochRef.current += 1",
  "selectionEpochRef.current += 1",
  "setLocalImages(false)",
  "setRemoteImages(false)",
  "setVolumes(false)",
  "setNetworks(false)",
]);

const fenceOutput = ts.transpileModule(fenceSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  fileName: fencePath,
}).outputText;
const fenceModule = { exports: {} };
new Function("require", "module", "exports", fenceOutput)(
  () => { throw new Error("submission fence must not gain runtime dependencies"); },
  fenceModule,
  fenceModule.exports,
);
const capturedFence = {
  actorScope: "admin:1",
  clusterEpoch: 2,
  selectionEpoch: 4,
  formRevision: 9,
  host: "cluster-a",
  project: "project-a",
};
assert.equal(fenceModule.exports.submissionFenceMatches(capturedFence, capturedFence, true), true);
assert.equal(
  fenceModule.exports.submissionFenceMatches(
    capturedFence,
    { ...capturedFence, selectionEpoch: 6 },
    false,
  ),
  false,
  "an A -> B -> A selection must reject the response captured before both transitions",
);

const intentOutput = ts.transpileModule(intent, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  fileName: intentPath,
}).outputText;
const intentModule = { exports: {} };
new Function("require", "module", "exports", intentOutput)(
  (specifier) => {
    if (specifier === "react") {
      return {
        useCallback: (fn) => fn,
        useRef: (value) => ({ current: value }),
        useState: (value) => [value, () => undefined],
      };
    }
    throw new Error(`Unexpected runtime import in create intent: ${specifier}`);
  },
  intentModule,
  intentModule.exports,
);
const selectionIntent = intentModule.exports.useCreateInventoryIntent();
const firstSelection = selectionIntent.nextSelectionEpoch();
const secondSelection = selectionIntent.nextSelectionEpoch();
assert.equal(firstSelection, 1);
assert.equal(secondSelection, 2);
assert.equal(selectionIntent.selectionEpochRef.current, 2);
selectionIntent.resetForCluster();
assert.equal(selectionIntent.clusterEpochRef.current, 1);
assert.equal(selectionIntent.selectionEpochRef.current, 3);

const hooksPath = "frontend/src/hooks/use-lxd.ts";
const hooks = read(hooksPath);
requireText(hooksPath, hooks, [
  "queryKeys.imageStore(scopeKey, host, project)",
  "queryFn: ({ signal }) => imageAPI.listByHost(host, project, { signal })",
  "queryKeys.nodeGpus(scopeKey, nodeName)",
  "queryFn: ({ signal }) => nodeAPI.gpus(nodeName, { signal })",
  "queryKeys.projectsFor(scopeKey, host)",
  "queryFn: ({ signal }) => projectAPI.list(host, { signal })",
  '["projectUsage", scopeKey, host, project, opts?.requestEpoch ?? 0]',
  "queryFn: ({ signal }) => projectAPI.usage(host, project, { signal })",
  "[STORAGE_POOL_LIST_KEY, scopeKey, host, opts?.requestEpoch ?? 0]",
  // The volume and network lists key through shared factories (the sidebar warms the same keys);
  // both the call and the factory must keep the scope, the project and the request epoch.
  "queryKeys.storageVolumesFor(scopeKey, pool, host, opts?.requestEpoch)",
  '["storageVolumes", actorScope, pool, host, requestEpoch ?? 0]',
  "queryKeys.networksFor(scopeKey, host, project, opts?.requestEpoch)",
  '["networks", actorScope, host, project ?? null, requestEpoch ?? 0]',
  '"profile",\n      scopeKey,\n      name,\n      host,\n      project ?? null,\n      opts?.requestEpoch ?? 0',
  "queryFn: ({ signal }) => api.profiles.get(name, host, project, { signal })",
  "queryKeys.instanceWritePolicy(scopeKey, host)",
  "queryFn: ({ signal }) => api.instances.getWritePolicy(host, { signal })",
]);

for (const eager of [
  "useRemoteImages()",
  "useStorageVolumes(undefined, form.selectedNode, { enabled: !!form.selectedNode })",
  "enabled: !!form.selectedNode,\n  });\n  const projectNetworks",
  "useNodeGpus(canUseAdvancedCreate ? form.selectedNode : \"\")",
  "api.tags.updateInstanceTags",
]) {
  const orchestrationSources = [form, volumeInventory, networkInventory, gpuInventory, imageCatalogue];
  if (orchestrationSources.some((source) => source.includes(eager))) {
    failures.push(`instance create restored eager or post-accept behavior ${JSON.stringify(eager)}`);
  }
}

// Exercise the transport, not only its source text: two observers of the same GET must own
// different abort controllers, cancelling one is not a 504, and a transport timeout still is.
const refreshOutput = ts.transpileModule(refresh, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: refreshPath,
}).outputText;
const refreshModule = { exports: {} };
new Function("require", "module", "exports", refreshOutput)(
  (specifier) => {
    if (specifier === "react") {
      return { useCallback: (fn) => fn, useRef: (value) => ({ current: value }) };
    }
    if (specifier === "@tanstack/react-query") return { useQuery: () => ({}) };
    throw new Error(`Unexpected runtime import in refresh tracker: ${specifier}`);
  },
  refreshModule,
  refreshModule.exports,
);
const refreshIntent = refreshModule.exports.createRefreshIntentTracker();
const releaseFirstRefresh = refreshIntent.acquire();
const releaseSecondRefresh = refreshIntent.acquire();
assert.equal(refreshIntent.isFresh(), true);
releaseFirstRefresh();
assert.equal(
  refreshIntent.isFresh(),
  true,
  "one overlapping refresh completion must not clear another refresh's fresh intent",
);
releaseFirstRefresh();
assert.equal(refreshIntent.isFresh(), true, "refresh intent release must be idempotent");
releaseSecondRefresh();
assert.equal(refreshIntent.isFresh(), false);

class TestAPIError extends Error {
  constructor(message, status, detail, retryAfter) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}
const apiOutput = ts.transpileModule(api, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: apiPath,
}).outputText;
const apiModule = { exports: {} };
new Function("require", "module", "exports", apiOutput)(
  (specifier) => {
    if (specifier === "@/types") return { APIError: TestAPIError };
    throw new Error(`Unexpected runtime import in API transport: ${specifier}`);
  },
  apiModule,
  apiModule.exports,
);

const originalFetch = globalThis.fetch;
try {
  let calls = 0;
  let resolveSecond;
  globalThis.fetch = (_url, options) => {
    calls += 1;
    const call = calls;
    return new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        reject(options.signal.reason ?? new DOMException("Cancelled", "AbortError"));
      }, { once: true });
      if (call === 2) {
        resolveSecond = () => resolve(new Response(JSON.stringify({ source: "second" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
    });
  };

  const firstController = new AbortController();
  const secondController = new AbortController();
  const first = apiModule.exports.fetchAPI(
    "/transport-isolation",
    { signal: firstController.signal },
  );
  const second = apiModule.exports.fetchAPI(
    "/transport-isolation",
    { signal: secondController.signal },
  );
  assert.equal(calls, 2, "signalled GETs must bypass the shared in-flight promise");
  firstController.abort();
  await assert.rejects(
    first,
    (error) => error?.name === "AbortError" && !(error instanceof TestAPIError),
    "selection cancellation must remain cancellation",
  );
  resolveSecond();
  assert.deepEqual(await second, { source: "second" });

  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      reject(options.signal.reason ?? new DOMException("Cancelled", "AbortError"));
    }, { once: true });
  });
  await assert.rejects(
    apiModule.exports.fetchAPI("/transport-timeout", {}, 5),
    (error) => error instanceof TestAPIError && error.status === 504,
    "transport timeout must remain a 504",
  );

  const delayedBodyResponse = (signal, onBodyStart) => ({
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers({ "Content-Type": "application/json" }),
    json: () => {
      onBodyStart();
      return new Promise((_resolve, reject) => {
        const rejectAbort = () => reject(
          signal.reason ?? new DOMException("Cancelled", "AbortError"),
        );
        if (signal.aborted) rejectAbort();
        else signal.addEventListener("abort", rejectAbort, { once: true });
      });
    },
  });

  let markCancelBodyStarted;
  const cancelBodyStarted = new Promise((resolve) => { markCancelBodyStarted = resolve; });
  globalThis.fetch = (_url, options) => Promise.resolve(
    delayedBodyResponse(options.signal, markCancelBodyStarted),
  );
  const bodyController = new AbortController();
  const bodyCancelled = apiModule.exports.fetchAPI(
    "/transport-body-cancellation",
    { signal: bodyController.signal },
  );
  await cancelBodyStarted;
  bodyController.abort();
  await assert.rejects(
    bodyCancelled,
    (error) => error?.name === "AbortError" && !(error instanceof TestAPIError),
    "selection cancellation after headers must cancel body consumption",
  );

  let markTimeoutBodyStarted;
  const timeoutBodyStarted = new Promise((resolve) => { markTimeoutBodyStarted = resolve; });
  globalThis.fetch = (_url, options) => Promise.resolve(
    delayedBodyResponse(options.signal, markTimeoutBodyStarted),
  );
  const bodyTimedOut = apiModule.exports.fetchAPI("/transport-body-timeout", {}, 5);
  await timeoutBodyStarted;
  await assert.rejects(
    bodyTimedOut,
    (error) => error instanceof TestAPIError && error.status === 504,
    "transport timeout after headers must remain a 504",
  );
} finally {
  globalThis.fetch = originalFetch;
}

if (failures.length) {
  console.error("Instance create orchestration check failed:\n" + failures.map((f) => `- ${f}`).join("\n"));
  process.exit(1);
}

console.log("Instance create orchestration check passed.");
