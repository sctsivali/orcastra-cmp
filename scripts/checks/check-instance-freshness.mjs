#!/usr/bin/env node
/** Runtime and structural contract for instance inventory convergence. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(
  process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."),
);
const requireFromFrontend = createRequire(join(root, "frontend/package.json"));
const ts = requireFromFrontend("typescript");
const { QueryClient } = requireFromFrontend("@tanstack/react-query");
const read = (path) => readFileSync(join(root, path), "utf8");
const failures = [];

function requireText(path, source, snippets) {
  for (const snippet of snippets) {
    if (!source.includes(snippet)) failures.push(`${path} is missing ${JSON.stringify(snippet)}`);
  }
}

const pagePath = "frontend/src/app/inventory/instances/page.tsx";
const page = read(pagePath);
requireText(pagePath, page, [
  "VMListResponse",
  "(data as VMListResponse | undefined)?.nodes ?? []",
  "useInstanceStream(authorizedHosts)",
]);

const streamPath = "frontend/src/hooks/use-instance-stream.ts";
const stream = read(streamPath);
requireText(streamPath, stream, [
  "createInstanceFreshnessCoordinator({",
  "getInstanceStreamHosts(",
  "actorScope: scopeKey",
  "areAllStreamsHealthy: allStreamsHealthy",
  "signal: controller.signal",
  "classifyRateLimit:",
  "retainRateLimit: retainInstanceReconcileRateLimit",
  "freshness.start()",
  "freshness.streamOpened()",
  "freshness.visibilityChanged",
  "freshness.stop()",
  "event.code === 1008",
  "const readFence = createExactInstanceReadFence()",
  "if (!active || !readFence.isCurrent(lease)) return false",
  "readFence.tombstone(identity)",
  "readFence.tombstoneName(scopeKey, host, name)",
  "upsertExactInstance(queryClient, identity, vm)",
  "removeExactInstance(queryClient, identity)",
]);

const pendingJobsPath = "frontend/src/hooks/use-pending-create-jobs.ts";
const pendingJobs = read(pendingJobsPath);
requireText(pendingJobsPath, pendingJobs, [
  'const TERMINAL = new Set(["success", "failure", "partial"])',
  "TERMINAL.has(job.status)",
  "if (!isAuthActorSynchronized(scopeKey)) return",
  'await reconcileMutationImpact(queryClient, "instance.create")',
  'reconcileMutationImpact(queryClient, "instance.create")',
  "await reconcileInstanceFleet(queryClient, scopeKey, { fresh: true })",
  "error.retryAfter ?? 30",
]);
const pendingTerminalReconcile = pendingJobs.match(
  /if \(TERMINAL\.has\(job\.status\)\) \{[\s\S]*?\}\)\(\)\.catch\(\(\) => \{\}\);/,
)?.[0] ?? "";
const pendingBeforeInvalidation = pendingTerminalReconcile.indexOf(
  "if (!isAuthActorSynchronized(scopeKey)) return",
);
const pendingInvalidation = pendingTerminalReconcile.indexOf(
  'await reconcileMutationImpact(queryClient, "instance.create")',
);
const pendingAfterInvalidation = pendingTerminalReconcile.indexOf(
  "if (!isAuthActorSynchronized(scopeKey)) return",
  pendingInvalidation + 1,
);
const pendingForcedFleet = pendingTerminalReconcile.indexOf(
  "await reconcileInstanceFleet(queryClient, scopeKey, { fresh: true })",
);
const pendingAfterFleet = pendingTerminalReconcile.indexOf(
  "if (!isAuthActorSynchronized(scopeKey)) return",
  pendingForcedFleet + 1,
);
if (
  pendingBeforeInvalidation < 0 ||
  pendingInvalidation < pendingBeforeInvalidation ||
  pendingAfterInvalidation < pendingInvalidation ||
  pendingForcedFleet < pendingAfterInvalidation ||
  pendingAfterFleet < pendingForcedFleet
) {
  failures.push(
    `${pendingJobsPath} does not fence the actor before and after sequential terminal invalidation/fleet reconciliation`,
  );
}
if (/Promise\.all(?:Settled)?\([\s\S]*?instance\.create[\s\S]*?reconcileInstanceFleet/.test(
  pendingTerminalReconcile,
)) {
  failures.push(
    `${pendingJobsPath} can race terminal invalidation against the final forced fleet snapshot`,
  );
}

const rebuildPath = "frontend/src/components/rebuild-dialog.tsx";
const rebuild = read(rebuildPath);
requireText(rebuildPath, rebuild, [
  "const [selectedImageKey, setSelectedImageKey] = useState<string>(\"\")",
  "const [remoteImageIntent, setRemoteImageIntent] = useState(false)",
  "useImagesByHost(",
  "useRemoteImages({ enabled: open && remoteImageIntent })",
  "const local = ((localResp?.images ?? []) as ImageInfo[])",
  "const remote = ((remoteResp?.images ?? []) as RemoteImageInfo[])",
  "return [...local, ...remote].filter",
  'Local: {loadingLocal ? "loading" : localImagesFailed ? "unavailable"',
  'Remote: {!remoteImageIntent ? "loads when opened" : loadingRemote ? "loading" : remoteImagesFailed ? "unavailable"',
  "transientFailures += 1",
  "REBUILD_MAX_TRANSIENT_FAILURES",
  "continue",
  'reconcileMutationImpact(queryClient, "instance.rebuild")',
  "reconcileInstanceFleet(queryClient, requestScope, { fresh: true })",
]);
if ((rebuild.match(/setSelectedImageKey\(/g)?.length ?? 0) !== 1) {
  failures.push(
    `${rebuildPath} clears the chosen image outside the explicit dialog-context reset`,
  );
}
const rebuildReconcile = rebuild.match(
  /const reconcileAcceptedOutcome = async \(\): Promise<boolean> => \{[\s\S]*?\n\s*\};/,
)?.[0] ?? "";
const rebuildInvalidateAt = rebuildReconcile.indexOf(
  'reconcileMutationImpact(queryClient, "instance.rebuild")',
);
const rebuildFenceAt = rebuildReconcile.indexOf(
  "if (!isCurrentRequest()) return false",
  rebuildInvalidateAt + 1,
);
const rebuildFleetAt = rebuildReconcile.indexOf(
  "reconcileInstanceFleet(queryClient, requestScope, { fresh: true })",
);
const rebuildFinalFenceAt = rebuildReconcile.indexOf(
  "return isCurrentRequest()",
  rebuildFleetAt + 1,
);
if (
  rebuildInvalidateAt < 0 ||
  rebuildFenceAt < rebuildInvalidateAt ||
  rebuildFleetAt < rebuildFenceAt ||
  rebuildFinalFenceAt < rebuildFleetAt ||
  (rebuild.match(/if \(!\(await reconcileAcceptedOutcome\(\)\)\) return;/g)?.length ?? 0) !== 2
) {
  failures.push(
    `${rebuildPath} does not serialize invalidation before the final fleet read with post-await actor fences`,
  );
}
const rebuildStatusRead = rebuild.match(
  /try \{\s*j = await api\.instances\.getRebuildJob[\s\S]*?\n\s*\}\s*catch \(error\) \{[\s\S]*?\n\s*\}\s*if \(!isCurrentRequest\(\)\) return;/,
)?.[0] ?? "";
if (
  !rebuildStatusRead.includes("transientFailures += 1") ||
  rebuildStatusRead.includes("setSelectedImageKey") ||
  rebuildStatusRead.includes("setRemoteImageIntent")
) {
  failures.push(
    `${rebuildPath} does not preserve image selection and source intent across transient status-read failures`,
  );
}

let listImplementation = async () => {
  throw new Error("No inventory read configured");
};
const reconciliationPath = "frontend/src/lib/instance-reconciliation.ts";
const reconciliationSource = read(reconciliationPath);
requireText(reconciliationPath, reconciliationSource, [
  'await queryClient.cancelQueries({ queryKey: ["instances", actorScope] })',
]);
const reconciliationOutput = ts.transpileModule(reconciliationSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: reconciliationPath,
}).outputText;
const reconciliationModule = { exports: {} };
new Function("require", "module", "exports", reconciliationOutput)(
  (specifier) => {
    if (specifier === "@/lib/api") {
      return { api: { instances: { list: (...args) => listImplementation(...args) } } };
    }
    throw new Error(`Unexpected runtime import in instance reconciliation: ${specifier}`);
  },
  reconciliationModule,
  reconciliationModule.exports,
);

const {
  createExactInstanceReadFence,
  isExactInstance,
  optimisticallyRemoveExactInstance,
  removeExactInstance,
  reconcileInstanceFleet,
  restoreExactInstanceRollback,
  retainInstanceReconcileRateLimit,
  upsertExactInstance,
} = reconciliationModule.exports;

const freshnessPath = "frontend/src/lib/instance-freshness.ts";
const freshnessOutput = ts.transpileModule(read(freshnessPath), {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: freshnessPath,
}).outputText;
const freshnessModule = { exports: {} };
new Function("require", "module", "exports", freshnessOutput)(
  (specifier) => {
    throw new Error(`Unexpected runtime import in instance freshness: ${specifier}`);
  },
  freshnessModule,
  freshnessModule.exports,
);
const {
  INSTANCE_OPEN_RESYNC_COALESCE_MS,
  INSTANCE_SAFETY_READ_MS,
  createInstanceFreshnessCoordinator,
  getInstanceStreamHosts,
} = freshnessModule.exports;

const actorA = "actor-a";
const actorB = "actor-b";
const row = (host, project, name, status) => ({ host, project, name, status });
const response = (vms, nodes = ["cluster-a"]) => ({ vms, nodes, total: vms.length });
const client = new QueryClient({
  defaultOptions: { queries: { retry: false, gcTime: Infinity } },
});
const fleetAKey = ["instances", actorA, "*", "*"];
const p1Key = ["instances", actorA, "cluster-a", "p1"];
const p2Key = ["instances", actorA, "cluster-a", "p2"];
const fleetBKey = ["instances", actorB, "*", "*"];
const aP1 = row("cluster-a", "p1", "shared-name", "Running");
const aP2 = row("cluster-a", "p2", "shared-name", "Frozen");
const bP1 = row("cluster-a", "p1", "shared-name", "Stopped");
client.setQueryData(fleetAKey, response([aP1, aP2]));
client.setQueryData(p1Key, response([aP1]));
client.setQueryData(p2Key, response([aP2]));
client.setQueryData(fleetBKey, response([bP1]));

const identity = {
  actorScope: actorA,
  host: "cluster-a",
  project: "p1",
  name: "shared-name",
};
const updated = row("cluster-a", "p1", "shared-name", "Stopped");
assert.equal(isExactInstance(updated, identity), true);
assert.equal(
  upsertExactInstance(client, identity, row("cluster-a", "p2", "shared-name", "Error")),
  false,
  "a response from another project must be rejected",
);
assert.equal(upsertExactInstance(client, identity, updated), true);
assert.equal(client.getQueryData(p1Key).vms[0].status, "Stopped");
assert.equal(client.getQueryData(p2Key).vms[0].status, "Frozen");
assert.equal(client.getQueryData(fleetBKey).vms[0].status, "Stopped");
assert.equal(
  client.getQueryData(["instance", actorA, "cluster-a", "p1", "shared-name"]).status,
  "Stopped",
);

removeExactInstance(client, identity);
assert.equal(client.getQueryData(p1Key).vms.length, 0);
assert.equal(client.getQueryData(p2Key).vms.length, 1);
assert.equal(client.getQueryData(fleetBKey).vms.length, 1);
assert.equal(
  client.getQueryData(["instance", actorA, "cluster-a", "p1", "shared-name"]),
  undefined,
);

// Two overlapping deletes must carry row-level inverse operations. Restoring failed A after
// successful B must preserve B's removal and any unrelated row update that landed meanwhile.
const deleteClient = new QueryClient({
  defaultOptions: { queries: { retry: false, gcTime: Infinity } },
});
const deleteFleetKey = ["instances", actorA, "*", "*"];
const deleteVmKey = ["vms", actorA, { host: undefined, status: undefined }];
const deleteA = row("cluster-a", "p1", "delete-a", "Running");
const deleteB = row("cluster-a", "p1", "delete-b", "Stopped");
const survivor = row("cluster-a", "p1", "survivor", "Running");
deleteClient.setQueryData(deleteFleetKey, response([deleteA, deleteB, survivor]));
deleteClient.setQueryData(deleteVmKey, response([deleteA, deleteB, survivor]));
const rollbackA = await optimisticallyRemoveExactInstance(deleteClient, {
  actorScope: actorA,
  host: "cluster-a",
  project: "p1",
  name: "delete-a",
});
await optimisticallyRemoveExactInstance(deleteClient, {
  actorScope: actorA,
  host: "cluster-a",
  project: "p1",
  name: "delete-b",
});
for (const key of [deleteFleetKey, deleteVmKey]) {
  deleteClient.setQueryData(key, response([row("cluster-a", "p1", "survivor", "Frozen")]));
}
restoreExactInstanceRollback(deleteClient, rollbackA);
for (const key of [deleteFleetKey, deleteVmKey]) {
  const restored = deleteClient.getQueryData(key);
  assert.deepEqual(restored.vms.map((vm) => vm.name), ["delete-a", "survivor"]);
  assert.equal(restored.vms[1].status, "Frozen");
  assert.equal(restored.total, 2);
}

const lastGood = response([aP2]);
client.setQueryData(fleetAKey, lastGood);
listImplementation = async () => {
  throw new Error("rate limited");
};
await assert.rejects(() => reconcileInstanceFleet(client, actorA, { fresh: true }));
assert.deepEqual(client.getQueryData(fleetAKey), lastGood, "a failed read must retain last-good rows");

const controller = new AbortController();
let receivedReadOptions;
const authoritative = response([updated], ["cluster-a", "empty-cluster"]);
listImplementation = async (...args) => {
  receivedReadOptions = args[5];
  return authoritative;
};
await reconcileInstanceFleet(client, actorA, { fresh: true, signal: controller.signal });
assert.equal(receivedReadOptions.fresh, true);
assert.equal(receivedReadOptions.signal, controller.signal);
assert.deepEqual(client.getQueryData(fleetAKey), authoritative);
assert.deepEqual(client.getQueryData(fleetBKey), response([bP1]));

let resolveCanonicalFleet;
let canonicalFleetSignal;
const olderCanonicalFleet = client.fetchQuery({
  queryKey: fleetAKey,
  queryFn: ({ signal }) => new Promise((resolveRead) => {
    canonicalFleetSignal = signal;
    resolveCanonicalFleet = resolveRead;
  }),
});
await Promise.resolve();
const resyncedSnapshot = response([row("cluster-a", "p1", "resynced", "Running")]);
listImplementation = async () => resyncedSnapshot;
await reconcileInstanceFleet(client, actorA, { fresh: true });
resolveCanonicalFleet(response([row("cluster-a", "p1", "older-query", "Stopped")]));
await olderCanonicalFleet;
assert.equal(
  canonicalFleetSignal.aborted,
  true,
  "fleet reconciliation must abort an older React Query list transport",
);
assert.deepEqual(
  client.getQueryData(fleetAKey),
  resyncedSnapshot,
  "an older React Query list must not overwrite the authoritative fleet resync",
);

let resolveOlderFleet;
let resolveNewerFleet;
let fleetRead = 0;
listImplementation = () => new Promise((resolveRead) => {
  fleetRead += 1;
  if (fleetRead === 1) resolveOlderFleet = resolveRead;
  else resolveNewerFleet = resolveRead;
});
const olderFleet = reconcileInstanceFleet(client, actorA, { fresh: true });
const newerFleet = reconcileInstanceFleet(client, actorA, { fresh: true });
await new Promise((resolveTurn) => setImmediate(resolveTurn));
const newestSnapshot = response([row("cluster-a", "p1", "newest", "Running")]);
resolveNewerFleet(newestSnapshot);
await newerFleet;
resolveOlderFleet(response([row("cluster-a", "p1", "stale", "Stopped")]));
await olderFleet;
assert.deepEqual(
  client.getQueryData(fleetAKey),
  newestSnapshot,
  "a late older fleet read must not replace a newer actor snapshot",
);

let resolveBeforePatch;
listImplementation = () => new Promise((resolveRead) => {
  resolveBeforePatch = resolveRead;
});
const beforePatch = reconcileInstanceFleet(client, actorA, { fresh: true });
await new Promise((resolveTurn) => setImmediate(resolveTurn));
const eventRow = row("cluster-a", "p1", "event-row", "Running");
upsertExactInstance(client, {
  actorScope: actorA,
  host: "cluster-a",
  project: "p1",
  name: "event-row",
}, eventRow);
resolveBeforePatch(response([row("cluster-a", "p1", "pre-event", "Stopped")]));
await beforePatch;
assert.equal(
  client.getQueryData(fleetAKey).vms.some((vm) => vm.name === "event-row"),
  true,
  "a late fleet read must not undo a newer exact event patch",
);

let resolveOrdinaryRead;
let ordinaryReadSignal;
const ordinaryRead = client.fetchQuery({
  queryKey: p1Key,
  queryFn: ({ signal }) => new Promise((resolveRead) => {
    ordinaryReadSignal = signal;
    resolveOrdinaryRead = resolveRead;
  }),
});
await Promise.resolve();
const socketRow = row("cluster-a", "p1", "socket-row", "Running");
upsertExactInstance(client, {
  actorScope: actorA,
  host: "cluster-a",
  project: "p1",
  name: "socket-row",
}, socketRow);
resolveOrdinaryRead(response([row("cluster-a", "p1", "pre-socket", "Stopped")]));
await ordinaryRead;
assert.equal(ordinaryReadSignal.aborted, true, "the older ordinary list transport must be aborted");
assert.equal(
  client.getQueryData(p1Key).vms.some((vm) => vm.name === "socket-row"),
  true,
  "ordinary query cancellation must preserve the newer exact socket patch",
);

const readFence = createExactInstanceReadFence();
const staleRead = readFence.start(identity);
readFence.tombstone(identity);
assert.equal(staleRead.signal.aborted, true, "delete must abort an exact in-flight read");
assert.equal(
  readFence.isCurrent(staleRead),
  false,
  "a response started before delete must not be allowed to repopulate the cache",
);
const postDeleteRows = [];
if (readFence.isCurrent(staleRead)) postDeleteRows.push(updated);
assert.deepEqual(postDeleteRows, [], "late GET resolution must stay behind the delete tombstone");

const olderUpdate = readFence.start(identity);
const newerUpdate = readFence.start(identity);
assert.equal(olderUpdate.signal.aborted, true, "a newer lifecycle must cancel the older exact read");
assert.equal(readFence.isCurrent(olderUpdate), false);
assert.equal(readFence.isCurrent(newerUpdate), true);

assert.deepEqual(
  retainInstanceReconcileRateLimit(false, true, true, 12, 1_000),
  { queuedFresh: true, retryAt: 13_000 },
  "a forced reconcile must survive its Retry-After window",
);
assert.deepEqual(
  retainInstanceReconcileRateLimit(false, false, true, 12, 1_000),
  { queuedFresh: false, retryAt: 13_000 },
  "a cached safety read must not become a forced retry",
);
assert.deepEqual(
  retainInstanceReconcileRateLimit(false, true, true, 0, 1_000),
  { queuedFresh: true, retryAt: 2_000 },
  "a first zero Retry-After response still receives one bounded delayed retry",
);
assert.deepEqual(
  retainInstanceReconcileRateLimit(false, true, false, 12, 13_000),
  { queuedFresh: false, retryAt: 25_000 },
  "a rate-limited retry must not schedule itself again",
);
assert.deepEqual(
  retainInstanceReconcileRateLimit(true, true, false, 12, 13_000),
  { queuedFresh: true, retryAt: 25_000 },
  "a new external intent queued during the retry must survive Retry-After",
);

function createFakeClock(startAt = 0) {
  let currentTime = startAt;
  let nextId = 1;
  const timers = new Map();
  const clock = {
    now: () => currentTime,
    setTimeout: (callback, delayMs) => {
      const id = nextId;
      nextId += 1;
      timers.set(id, {
        id,
        at: currentTime + Math.max(0, delayMs),
        callback,
      });
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id);
    },
  };

  const advanceBy = async (durationMs) => {
    assert.ok(durationMs >= 0, "fake-clock advances must be non-negative");
    const target = currentTime + durationMs;
    while (true) {
      const next = [...timers.values()]
        .filter((timer) => timer.at <= target)
        .sort((left, right) => left.at - right.at || left.id - right.id)[0];
      if (!next) break;
      currentTime = next.at;
      timers.delete(next.id);
      await next.callback();
      await Promise.resolve();
    }
    currentTime = target;
    await Promise.resolve();
  };

  return { clock, advanceBy };
}

function createFreshnessHarness({
  actorScope = actorA,
  initiallyVisible = true,
  initiallyHealthy = true,
  reconcile,
  safetyReadMs = INSTANCE_SAFETY_READ_MS,
} = {}) {
  const fakeClock = createFakeClock();
  const requests = [];
  let visible = initiallyVisible;
  let healthy = initiallyHealthy;
  const coordinator = createInstanceFreshnessCoordinator({
    actorScope,
    isVisible: () => visible,
    areAllStreamsHealthy: () => healthy,
    reconcile: async (request) => {
      requests.push({ ...request, at: fakeClock.clock.now() });
      await reconcile?.(request, requests.length);
    },
    classifyRateLimit: (error) =>
      error && typeof error === "object" && error.status === 429
        ? { retryAfterSeconds: error.retryAfter }
        : null,
    retainRateLimit: retainInstanceReconcileRateLimit,
    clock: fakeClock.clock,
    safetyReadMs,
  });

  return {
    ...fakeClock,
    coordinator,
    requests,
    setHealthy: (next) => {
      healthy = next;
    },
    setVisible: (next) => {
      visible = next;
      coordinator.visibilityChanged();
    },
    notifyVisibility: () => coordinator.visibilityChanged(),
  };
}

assert.equal(INSTANCE_SAFETY_READ_MS, 30_000, "the visible-tab safety cadence is 30 seconds");
assert.equal(
  INSTANCE_OPEN_RESYNC_COALESCE_MS,
  100,
  "simultaneous stream opens have a bounded coalescing window",
);
assert.deepEqual(
  getInstanceStreamHosts(
    ["cluster-with-rows", "empty-cluster", "cluster-with-rows"],
    ["pending-cluster", "empty-cluster"],
  ),
  ["cluster-with-rows", "empty-cluster", "pending-cluster"],
  "authorized empty clusters must still receive a socket while duplicate pending hosts collapse",
);

const healthyCadence = createFreshnessHarness();
healthyCadence.coordinator.start();
await healthyCadence.advanceBy(INSTANCE_SAFETY_READ_MS - 1);
assert.deepEqual(healthyCadence.requests, [], "a visible safety read must not run before 30 seconds");
await healthyCadence.advanceBy(1);
assert.deepEqual(
  healthyCadence.requests,
  [{ actorScope: actorA, fresh: false, at: INSTANCE_SAFETY_READ_MS }],
  "a healthy visible fleet uses a cached read on the exact 30-second cadence",
);
await healthyCadence.advanceBy(INSTANCE_SAFETY_READ_MS);
assert.deepEqual(
  healthyCadence.requests.map(({ fresh, at }) => ({ fresh, at })),
  [
    { fresh: false, at: 30_000 },
    { fresh: false, at: 60_000 },
  ],
  "visible safety reads repeat every 30 seconds",
);
healthyCadence.coordinator.stop();

const degradedCadence = createFreshnessHarness({ initiallyHealthy: false });
degradedCadence.coordinator.start();
await degradedCadence.advanceBy(INSTANCE_SAFETY_READ_MS);
assert.deepEqual(
  degradedCadence.requests,
  [{ actorScope: actorA, fresh: true, at: INSTANCE_SAFETY_READ_MS }],
  "any degraded stream promotes the safety read to fresh=true",
);
degradedCadence.coordinator.stop();

const visibilityCadence = createFreshnessHarness();
visibilityCadence.coordinator.start();
visibilityCadence.setVisible(false);
await visibilityCadence.advanceBy(INSTANCE_SAFETY_READ_MS * 2);
assert.deepEqual(visibilityCadence.requests, [], "hidden tabs must not poll");
visibilityCadence.setVisible(true);
await visibilityCadence.advanceBy(0);
assert.deepEqual(
  visibilityCadence.requests,
  [{ actorScope: actorA, fresh: true, at: 60_000 }],
  "a hidden-to-visible transition performs one immediate forced read",
);
await visibilityCadence.advanceBy(1_000);
visibilityCadence.notifyVisibility();
await visibilityCadence.advanceBy(0);
assert.equal(
  visibilityCadence.requests.length,
  1,
  "duplicate visible notifications must not trigger another resume read",
);
await visibilityCadence.advanceBy(INSTANCE_SAFETY_READ_MS - 1_001);
assert.equal(
  visibilityCadence.requests.length,
  1,
  "resuming visibility restarts the safety cadence instead of polling early",
);
await visibilityCadence.advanceBy(1);
assert.deepEqual(
  visibilityCadence.requests.map(({ fresh, at }) => ({ fresh, at })),
  [
    { fresh: true, at: 60_000 },
    { fresh: false, at: 90_000 },
  ],
  "the first post-resume safety read waits a full visible 30-second interval",
);
visibilityCadence.coordinator.stop();

const openBurst = createFreshnessHarness({
  actorScope: "actor-open-burst",
  safetyReadMs: 1_000_000,
});
openBurst.coordinator.start();
openBurst.coordinator.streamOpened();
openBurst.coordinator.streamOpened();
openBurst.coordinator.streamOpened();
await openBurst.advanceBy(INSTANCE_OPEN_RESYNC_COALESCE_MS - 1);
assert.deepEqual(openBurst.requests, [], "a stream-open burst waits for its coalescing window");
await openBurst.advanceBy(1);
assert.deepEqual(
  openBurst.requests,
  [{ actorScope: "actor-open-burst", fresh: true, at: 100 }],
  "simultaneous stream opens and reconnects collapse into one actor-scoped fleet resync",
);
openBurst.coordinator.stop();

const rateLimitError = Object.assign(new Error("rate limited"), {
  status: 429,
  retryAfter: 5,
});
const rateLimited = createFreshnessHarness({
  safetyReadMs: 1_000_000,
  reconcile: async () => {
    throw rateLimitError;
  },
});
rateLimited.coordinator.start();
rateLimited.coordinator.requestReconcile(true);
await rateLimited.advanceBy(INSTANCE_OPEN_RESYNC_COALESCE_MS);
assert.deepEqual(
  rateLimited.requests.map(({ fresh, at }) => ({ fresh, at })),
  [{ fresh: true, at: 100 }],
  "the initial forced request runs after the normal coalescing window",
);
await rateLimited.advanceBy(4_999);
assert.equal(rateLimited.requests.length, 1, "Retry-After must block an early forced retry");
await rateLimited.advanceBy(1);
assert.deepEqual(
  rateLimited.requests.map(({ fresh, at }) => ({ fresh, at })),
  [
    { fresh: true, at: 100 },
    { fresh: true, at: 5_100 },
  ],
  "one forced retry runs when Retry-After expires",
);
await rateLimited.advanceBy(100_000);
assert.equal(
  rateLimited.requests.length,
  2,
  "a rate-limited retry must not create a forced-refresh loop",
);
rateLimited.coordinator.stop();

if (failures.length > 0) {
  console.error("Instance freshness contract failed:");
  failures.forEach((failure) => console.error(`  ${failure}`));
  process.exit(1);
}

console.log(
  "instance freshness: empty-cluster subscriptions, bounded recovery, last-good retention, and exact cache isolation are wired",
);
