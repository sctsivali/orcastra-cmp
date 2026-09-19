#!/usr/bin/env node
/**
 * Exercise the browser auth boundary without a live IdP.
 *
 * The runtime cases compile the production API client, replace only fetch, and drive direct
 * authenticated A -> B transitions. Structural assertions cover the React gate that cannot be
 * mounted in this repository because it intentionally has no frontend test runner.
 *
 * Run: node scripts/checks/check-auth-transition-isolation.mjs [repo-root]
 */

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

class TestAPIError extends Error {
  constructor(message, status, detail, retryAfter) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

function compileCommonJs(file, stubs, globals = {}) {
  const source = readFileSync(join(root, file), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      esModuleInterop: true,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
    fileName: file,
  }).outputText;
  const module = { exports: {} };
  const localRequire = (specifier) => {
    if (Object.hasOwn(stubs, specifier)) return stubs[specifier];
    throw new Error(`Unexpected runtime import while checking auth isolation: ${specifier}`);
  };
  const execute = new Function(
    "require",
    "module",
    "exports",
    "fetch",
    "window",
    output,
  );
  execute(localRequire, module, module.exports, globals.fetch, globals.window);
  return module.exports;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 401 ? "Unauthorized" : "OK",
    headers: { get: () => null },
    json: async () => body,
  };
}

const calls = [];
let fetchImplementation = () => {
  throw new Error("The auth isolation check did not install a fetch scenario");
};
const fetchStub = (url, options) => {
  const call = { url, options };
  calls.push(call);
  return fetchImplementation(call);
};

const api = compileCommonJs(
  "frontend/src/lib/api.ts",
  { "@/types": { APIError: TestAPIError } },
  { fetch: fetchStub, window: {} },
);

// A and B ask for the same endpoint while A is still in flight. B must issue its own request,
// carry B's bearer, and be able to complete before A.
const actorARead = deferred();
const actorBRead = deferred();
let readNumber = 0;
fetchImplementation = () => {
  readNumber += 1;
  return readNumber === 1 ? actorARead.promise : actorBRead.promise;
};

api.setAccessToken("test-token-a", "actor-a");
const aRead = api.fetchAPI("/auth-race");
assert.equal(api.isAuthContextSynchronized("test-token-b", "actor-b"), false);
api.setAccessToken("test-token-b", "actor-b");
assert.equal(api.isAuthContextSynchronized("test-token-b", "actor-b"), true);
const bRead = api.fetchAPI("/auth-race");

assert.equal(calls.length, 2, "B joined A's endpoint-only in-flight GET");
assert.equal(calls[0].options.headers.Authorization, "Bearer test-token-a");
assert.equal(calls[1].options.headers.Authorization, "Bearer test-token-b");

actorBRead.resolve(response(200, { actor: "b" }));
assert.deepEqual(await bRead, { actor: "b" });
actorARead.resolve(response(200, { actor: "a" }));
assert.deepEqual(await aRead, { actor: "a" });

// Deduplication remains effective within one auth generation.
const sameActorRead = deferred();
fetchImplementation = () => sameActorRead.promise;
const beforeSameActor = calls.length;
const firstSameActorRead = api.fetchAPI("/same-actor");
const secondSameActorRead = api.fetchAPI("/same-actor");
assert.equal(calls.length, beforeSameActor + 1, "same-actor GET dedupe was lost");
sameActorRead.resolve(response(200, { shared: true }));
assert.deepEqual(await Promise.all([firstSameActorRead, secondSameActorRead]), [
  { shared: true },
  { shared: true },
]);

// A pending A mutation that gets a 401 after a direct A -> B refresh must not replay its body
// with B's bearer. One network call is the invariant.
api.setAccessToken("test-token-a2", "actor-a");
api.onSessionRefreshNeeded(async () => {
  api.setAccessToken("test-token-b2", "actor-b");
});
fetchImplementation = async () => response(401, { detail: "expired" });
const beforeCrossActorMutation = calls.length;
await assert.rejects(
  api.fetchAPI("/mutation-race", { method: "POST", body: "{}" }),
  (error) => error instanceof TestAPIError && error.status === 401,
);
assert.equal(
  calls.length,
  beforeCrossActorMutation + 1,
  "A mutation retried under B's bearer",
);

// The normal recovery path is retained: a same-actor silent token rotation retries once.
api.setAccessToken("test-token-a3-old", "actor-a");
api.onSessionRefreshNeeded(async () => {
  api.setAccessToken("test-token-a3-new", "actor-a");
});
let sameActorMutationAttempt = 0;
fetchImplementation = async () => {
  sameActorMutationAttempt += 1;
  return sameActorMutationAttempt === 1
    ? response(401, { detail: "expired" })
    : response(200, { recovered: true });
};
const beforeSameActorMutation = calls.length;
assert.deepEqual(
  await api.fetchAPI("/same-actor-refresh", { method: "POST", body: "{}" }),
  { recovered: true },
);
assert.equal(calls.length, beforeSameActorMutation + 2);
assert.equal(calls.at(-2).options.headers.Authorization, "Bearer test-token-a3-old");
assert.equal(calls.at(-1).options.headers.Authorization, "Bearer test-token-a3-new");

// WebSocket URL builders read the same installed bearer. Once B is marked synchronized, a newly
// mounted stream must carry B and not A.
const wsUrls = compileCommonJs(
  "frontend/src/lib/ws-url.ts",
  { "@/lib/api": api },
  { fetch: fetchStub, window: { location: { hostname: "localhost", protocol: "http:" } } },
);
api.setAccessToken("test-token-ws-a", "actor-a");
const aWs = wsUrls.getEventsWsUrl("node-1");
api.setAccessToken("test-token-ws-b", "actor-b");
const bWs = wsUrls.getEventsWsUrl("node-1");
assert.match(aWs, /token=test-token-ws-a/);
assert.match(bWs, /token=test-token-ws-b/);
assert.doesNotMatch(bWs, /test-token-ws-a/);

// Providers listens to the same actor boundary. Model its exact replacement operation with a
// real QueryClient: same-actor token rotation keeps cache, while A -> B retires and clears it.
const { QueryClient } = requireFromFrontend("@tanstack/react-query");
api.setAccessToken("test-token-cache-a", "actor-cache-a");
let cacheClient = new QueryClient();
let retiredClient = null;
let actorCacheResets = 0;
const stopWatchingActor = api.onAuthActorChanged(() => {
  retiredClient = cacheClient;
  cacheClient = new QueryClient();
  retiredClient.clear();
  actorCacheResets += 1;
});
cacheClient.setQueryData(["unscoped-sensitive-body"], { actor: "a" });
api.setAccessToken("test-token-cache-a-rotated", "actor-cache-a");
assert.equal(actorCacheResets, 0, "same-actor token rotation cleared the QueryClient");
assert.equal(
  api.isAuthActorSynchronized("actor-cache-a"),
  true,
  "same-actor token rotation crossed the render gate",
);
assert.deepEqual(cacheClient.getQueryData(["unscoped-sensitive-body"]), { actor: "a" });
api.setAccessToken("test-token-cache-b", "actor-cache-b");
assert.equal(actorCacheResets, 1, "A -> B did not replace the QueryClient");
assert.equal(cacheClient.getQueryData(["unscoped-sensitive-body"]), undefined);
assert.equal(retiredClient.getQueryData(["unscoped-sensitive-body"]), undefined);
stopWatchingActor();

const authProvider = readFileSync(
  join(root, "frontend/src/components/auth-provider.tsx"),
  "utf8",
);
const providers = readFileSync(join(root, "frontend/src/components/providers.tsx"), "utf8");
const hooks = readFileSync(join(root, "frontend/src/hooks/use-lxd.ts"), "utf8");
const apiSource = readFileSync(join(root, "frontend/src/lib/api.ts"), "utf8");
const taskMutations = readFileSync(
  join(root, "frontend/src/components/task-panel/hooks/use-tasks.ts"),
  "utf8",
);
const taskCancelDialog = readFileSync(
  join(root, "frontend/src/components/task-panel/task-cancel-dialog.tsx"),
  "utf8",
);

const structuralChecks = [
  [
    /getActorQueryScope\(session\)/.test(authProvider) &&
      /setAccessToken\(token, scope\)/.test(authProvider) &&
      /isAuthActorSynchronized\(actorScope\)/.test(authProvider),
    "TokenSync does not install and verify the complete actor auth context",
  ],
  [
    /if \(!authContextReady\)\s*\{\s*return null;\s*\}/.test(authProvider) &&
      /setSynchronizedAuth/.test(authProvider) &&
      /synchronizedAuth\?\.actorScope === actorScope/.test(authProvider) &&
      !/synchronizedAuth\?\.token === sessionToken/.test(authProvider),
    "TokenSync does not isolate A -> B without unmounting a same-actor token rotation",
  ],
  [
    /if \(!cancelled && !confirmed\)/.test(authProvider) &&
      /cancelled = true/.test(authProvider),
    "a stale sign-out confirmation can clear a newer actor's bearer",
  ],
  [
    /const inflightKey = `\$\{authGeneration\}:\$\{endpoint\}`/.test(apiSource) &&
      /_inflightGets\.get\(inflightKey\)/.test(apiSource) &&
      /_inflightGets\.set\(inflightKey, request\)/.test(apiSource),
    "GET in-flight dedupe is not scoped by non-secret auth generation",
  ],
  [
    /_authActorGeneration !== requestActorGeneration/.test(apiSource) &&
      /same actor's potentially refreshed token/.test(apiSource),
    "401 recovery can replay a previous actor's request with the current bearer",
  ],
  [
    /onAuthActorChanged\(\(\) =>/.test(providers) &&
      /const next = makeQueryClient\(\)/.test(providers) &&
      /previous\.clear\(\)/.test(providers) &&
      /setQueryClient\(next\)/.test(providers),
    "Providers does not retire the complete QueryClient before B remounts",
  ],
  [
    /queryKeys\.instanceSnapshots\(scopeKey, host, project, instanceName\)/.test(hooks) &&
      /queryKeys\.snapshotSchedules\([\s\S]{0,180}scopeKey/.test(hooks) &&
      /queryKeys\.memberResources\(scopeKey, node, memberName\)/.test(hooks),
    "an instance-detail or member-resource query can reuse another actor's cached body",
  ],
  [
    /api\.snapshots\.list\(instanceName, host, project\)/.test(hooks) &&
      /api\.snapshotSchedules\.list\(filters, opts\)/.test(hooks) &&
      /vmAPI\.getMetrics\(host, instanceName, project\)/.test(hooks),
    "a project-qualified detail query stopped propagating its project to the API",
  ],
  [
    /activeGuardRef\.current = \{ scopeKey: currentScopeKey \}/.test(taskMutations) &&
      /activeGuardRef\.current = \{ scopeKey: null \}/.test(taskMutations) &&
      (taskMutations.match(/const requestActorGuard = activeActorGuardRef\.current/g)?.length ??
        0) >= 2,
    "task mutations do not capture and retire a unique authenticated-actor guard",
  ],
  [
    (taskMutations.match(/activeActorGuardRef\.current !== requestActorGuard/g)?.length ?? 0) >=
      4 &&
      /taskKeys\.all\(requestActorGuard\.scopeKey\)/.test(taskMutations) &&
      (taskMutations.match(
        /mutation\.variables\?\.requestActorGuard === activeActorGuardRef\.current/g,
      )?.length ?? 0) === 2,
    "a stale task cancel or console kill can still toast, invalidate, or expose pending state",
  ],
  [
    /shown\.current\?\.actorGuard === activeActorGuardRef\.current/.test(taskCancelDialog) &&
      /ownedError\?\.actorGuard === activeActorGuardRef\.current/.test(taskCancelDialog) &&
      /resultIsCurrent &&[\s\S]{0,100}activeActorGuardRef\.current === requestActorGuard/.test(
        taskCancelDialog,
      ) &&
      /cancelInFlightRef\.current\?\.actorGuard === requestActorGuard/.test(taskCancelDialog),
    "the cancel dialog can apply or render an old actor's mutation response",
  ],
];

const failures = structuralChecks.filter(([passed]) => !passed).map(([, reason]) => reason);
if (failures.length > 0) {
  console.error(`auth transition isolation: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log("auth transition isolation: runtime race cases and 11 structural guards passed");
