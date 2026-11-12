#!/usr/bin/env node
/**
 * Exercise the browser auth boundary without a live IdP.
 *
 * The runtime cases compile the production API client, replace only fetch, and drive direct
 * authenticated A -> B transitions. Structural assertions cover the React gate, which cannot be
 * mounted here because the repository has no frontend test runner.
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
  { "@/types": { APIError: TestAPIError }, "@/lib/auth-headers": compileCommonJs("frontend/src/lib/auth-headers.ts", {}) },
  { fetch: fetchStub, window: {} },
);
// The browser never holds the access token. Each request names its actor, and the proxy refuses
// one whose actor is no longer the cookie's (lib/api-proxy-auth.ts).
const ACTOR = "x-orcastra-actor";
const actorOf = (call) => decodeURIComponent(call.options.headers?.[ACTOR] ?? "");

// A and B ask for the same endpoint while A is still in flight. B must issue its own request,
// name B as its actor, and be able to complete before A.
const actorARead = deferred();
const actorBRead = deferred();
let readNumber = 0;
fetchImplementation = () => {
  readNumber += 1;
  return readNumber === 1 ? actorARead.promise : actorBRead.promise;
};

api.setSessionAuth("stamp-a", "actor-a", "actor-a");
const aRead = api.fetchAPI("/auth-race");
assert.equal(api.isAuthContextSynchronized("stamp-b", "actor-b"), false);
api.setSessionAuth("stamp-b", "actor-b", "actor-b");
assert.equal(api.isAuthContextSynchronized("stamp-b", "actor-b"), true);
const bRead = api.fetchAPI("/auth-race");

assert.equal(calls.length, 2, "B joined A's endpoint-only in-flight GET");
assert.equal(actorOf(calls[0]), "actor-a");
assert.equal(actorOf(calls[1]), "actor-b");

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

// A read asked for after a write must not join a GET that started before the write, or a
// delete's own refetch gets the pre-delete list.
const preWriteRead = deferred();
let writeEpochReads = 0;
fetchImplementation = (call) => {
  if (call.options.method === "DELETE") return Promise.resolve(response(200, { deleted: true }));
  writeEpochReads += 1;
  return writeEpochReads === 1 ? preWriteRead.promise : Promise.resolve(response(200, { after: true }));
};
const beforeWriteEpoch = calls.length;
const stalePoll = api.fetchAPI("/write-epoch");
await api.fetchAPI("/write-epoch/item", { method: "DELETE" });
const postWriteRead = api.fetchAPI("/write-epoch");
assert.equal(calls.length, beforeWriteEpoch + 3, "a post-write GET joined a GET from before the write");
assert.deepEqual(await postWriteRead, { after: true });
preWriteRead.resolve(response(200, { before: true }));
assert.deepEqual(await stalePoll, { before: true });

// A pending A mutation that gets a 401 after a direct A -> B refresh must not replay its body
// as B. One network call is the invariant.
api.setSessionAuth("stamp-a2", "actor-a", "actor-a");
api.onSessionRefreshNeeded(async () => {
  api.setSessionAuth("stamp-b2", "actor-b", "actor-b");
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
  "A mutation retried as B",
);

// The normal recovery path is retained: a same-actor silent token rotation retries once.
api.setSessionAuth("stamp-a3-old", "actor-a", "actor-a");
api.onSessionRefreshNeeded(async () => {
  api.setSessionAuth("stamp-a3-new", "actor-a", "actor-a");
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
assert.equal(actorOf(calls.at(-2)), "actor-a");
assert.equal(actorOf(calls.at(-1)), "actor-a");

// A debounced personalization callback captures both actor label and monotonic generation.
// It must dispatch nothing after A -> B, including the ABA case where the label becomes A again.
const actorMutationFence = compileCommonJs(
  "frontend/src/lib/actor-mutation-fence.ts",
  { "@/lib/api": api },
  { fetch: fetchStub, window: {} },
);
const { personalizationAPI } = compileCommonJs(
  "frontend/src/lib/personalization-api.ts",
  { "@/lib/api": api },
  { fetch: fetchStub, window: {} },
);
fetchImplementation = async () => response(200, { saved: true });

api.setSessionAuth("stamp-pref-a", "actor-pref-a", "actor-pref-a");
const stalePreferenceFence = actorMutationFence.captureActorMutationFence("actor-pref-a");
api.setSessionAuth("stamp-pref-b", "actor-pref-b", "actor-pref-b");
const beforeRetiredPreference = calls.length;
assert.deepEqual(
  await actorMutationFence.dispatchActorMutation(
    stalePreferenceFence,
    () => personalizationAPI.update({ theme: "light" }),
  ),
  { retired: true },
);
assert.equal(calls.length, beforeRetiredPreference, "A's delayed preference save went out as B");

api.setSessionAuth("stamp-pref-a2", "actor-pref-a", "actor-pref-a");
const abaPreferenceFence = actorMutationFence.captureActorMutationFence("actor-pref-a");
api.setSessionAuth("stamp-pref-b2", "actor-pref-b", "actor-pref-b");
api.setSessionAuth("stamp-pref-a3", "actor-pref-a", "actor-pref-a");
const beforeAbaPreference = calls.length;
assert.deepEqual(
  await actorMutationFence.dispatchActorMutation(
    abaPreferenceFence,
    () => personalizationAPI.update({ theme: null }),
  ),
  { retired: true },
);
assert.equal(calls.length, beforeAbaPreference, "an A -> B -> A theme clear was revived");

const currentPreferenceFence = actorMutationFence.captureActorMutationFence("actor-pref-a");
api.setSessionAuth("stamp-pref-a4", "actor-pref-a", "actor-pref-a");
const beforeCurrentPreference = calls.length;
const currentPreference = await actorMutationFence.dispatchActorMutation(
  currentPreferenceFence,
  () => personalizationAPI.update({ theme: "dark" }),
);
assert.equal(currentPreference.retired, false, "same-actor token rotation retired a valid save");
assert.equal(calls.length, beforeCurrentPreference + 1);
assert.equal(actorOf(calls.at(-1)), "actor-pref-a");

// A socket opens with a single-use ticket minted over the authenticated fetch, so it carries the
// installed actor's session and never a credential of its own. Once B is installed, a newly mounted
// stream must be minted under B and its URL must hold no token at all.
const wsTicketApi = compileCommonJs(
  "frontend/src/lib/ws-ticket-api.ts",
  { "@/lib/api": api },
  { fetch: fetchStub, window: {} },
);
const wsUrls = compileCommonJs(
  "frontend/src/lib/ws-url.ts",
  {
    "@/lib/ws-ticket-api": wsTicketApi,
    "@/lib/ws-close": compileCommonJs("frontend/src/lib/ws-close.ts", {}),
    "@/types/api": { APIError: TestAPIError },
  },
  { fetch: fetchStub, window: { location: { hostname: "localhost", protocol: "http:" } } },
);
fetchImplementation = async ({ options }) => {
  const actor = actorOf({ options });
  return response(201, { ticket: `ticket-of-${actor.slice(-1)}`, tickets: null, expires_in: 30 });
};
api.setSessionAuth("stamp-ws-a", "actor-a", "actor-a");
const aWs = await wsUrls.ticketedSocketUrl({ kind: "events", host: "node-1" });
const aMint = calls.at(-1);
api.setSessionAuth("stamp-ws-b", "actor-b", "actor-b");
const bWs = await wsUrls.ticketedSocketUrl({ kind: "events", host: "node-1" });
const bMint = calls.at(-1);
assert.equal(aMint.url, "/api/v1/ws-tickets");
assert.equal(aMint.options.method, "POST");
assert.deepEqual(JSON.parse(aMint.options.body), { kind: "events", host: "node-1" });
assert.equal(actorOf(bMint), "actor-b");
assert.match(aWs, /\/api\/v1\/events\/ws\?host=node-1&ticket=ticket-of-a$/);
assert.match(bWs, /&ticket=ticket-of-b$/);
assert.doesNotMatch(aWs + bWs, /token=|stamp-|actor-/);

// Providers listens to the same actor boundary. Model its exact replacement operation with a
// real QueryClient: same-actor token rotation keeps cache, while A -> B retires and clears it.
const { QueryClient } = requireFromFrontend("@tanstack/react-query");
api.setSessionAuth("stamp-cache-a", "actor-cache-a", "actor-cache-a");
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
api.setSessionAuth("stamp-cache-a-rotated", "actor-cache-a", "actor-cache-a");
assert.equal(actorCacheResets, 0, "same-actor token rotation cleared the QueryClient");
assert.equal(
  api.isAuthActorSynchronized("actor-cache-a"),
  true,
  "same-actor token rotation crossed the render gate",
);
assert.deepEqual(cacheClient.getQueryData(["unscoped-sensitive-body"]), { actor: "a" });
api.setSessionAuth("stamp-cache-b", "actor-cache-b", "actor-cache-b");
assert.equal(actorCacheResets, 1, "A -> B did not replace the QueryClient");
assert.equal(cacheClient.getQueryData(["unscoped-sensitive-body"]), undefined);
assert.equal(retiredClient.getQueryData(["unscoped-sensitive-body"]), undefined);
stopWatchingActor();

// Break-glass lives outside the React tree, so exercise its actor epoch directly. A delayed
// A request or grant must stay retired even when the identity cycles A -> B -> A, where comparing
// only an actor label would hit the ABA race.
function createTestZustandStore(initializer) {
  let state;
  const get = () => state;
  const set = (patch) => {
    const next = typeof patch === "function" ? patch(state) : patch;
    state = { ...state, ...next };
  };
  state = initializer(set, get, {});
  const useStore = () => state;
  useStore.getState = get;
  useStore.setState = set;
  return useStore;
}

let actorChanged;
let checkBreakGlass = async () => {
  throw new Error("No break-glass check scenario installed");
};
let createBreakGlassGrant = async () => {
  throw new Error("No break-glass grant scenario installed");
};
let reconcileBreakGlass = async () => {};
const breakGlassModule = compileCommonJs(
  "frontend/src/lib/break-glass-store.ts",
  {
    zustand: { create: createTestZustandStore },
    "@/lib/api": {
      api: {
        breakGlass: {
          check: (...args) => checkBreakGlass(...args),
          createGrant: (...args) => createBreakGlassGrant(...args),
        },
      },
      onAuthActorChanged: (listener) => {
        actorChanged = listener;
        return () => {};
      },
    },
    "@/lib/mutation-coherence": {
      captureExternalMutationContext: () => ({ actorScope: "captured-actor" }),
      reconcileExternalMutation: (...args) => reconcileBreakGlass(...args),
    },
  },
  { fetch: fetchStub, window: {} },
);
const breakGlassStore = breakGlassModule.useBreakGlassStore;

const delayedBreakGlassCheck = deferred();
let staleCheckSignal;
checkBreakGlass = (_clusterId, options) => {
  staleCheckSignal = options.signal;
  return delayedBreakGlassCheck.promise;
};
const staleBreakGlassRequest = breakGlassStore
  .getState()
  .request("cluster-a", "instance-a");
actorChanged(); // A -> B
actorChanged(); // B -> A
assert.equal(staleCheckSignal.aborted, true, "actor change did not abort the old grant check");
delayedBreakGlassCheck.resolve({
  required: true,
  has_active_grant: false,
  target_organization_slug: "org-a",
});
assert.equal(
  await staleBreakGlassRequest,
  false,
  "an A check completed after A -> B -> A and reopened its stale prompt",
);
assert.equal(breakGlassStore.getState().prompt, null);

const requiredGrantCheck = {
  required: true,
  has_active_grant: false,
  target_organization_slug: "org-current",
};
checkBreakGlass = async () => requiredGrantCheck;
createBreakGlassGrant = async () => ({ id: "grant-a" });
const delayedGrantReconcile = deferred();
const reconciliationStarted = deferred();
let grantReconcileCalls = 0;
reconcileBreakGlass = async () => {
  grantReconcileCalls += 1;
  reconciliationStarted.resolve();
  return delayedGrantReconcile.promise;
};

const oldGrantOutcome = breakGlassStore.getState().request("cluster-a", "instance-a");
await Promise.resolve();
assert.equal(breakGlassStore.getState().prompt?.instanceName, "instance-a");
const oldGrantSubmit = breakGlassStore.getState().submit("incident", 15);
await reconciliationStarted.promise;
actorChanged(); // A -> B while the accepted grant is reconciling
actorChanged(); // B -> A before that reconciliation resolves
assert.equal(await oldGrantOutcome, false, "actor retirement did not close the old prompt");

const replacementOutcome = breakGlassStore
  .getState()
  .request("cluster-a", "instance-replacement");
await Promise.resolve();
const replacementPrompt = breakGlassStore.getState().prompt;
assert.equal(replacementPrompt?.instanceName, "instance-replacement");
delayedGrantReconcile.resolve();
await oldGrantSubmit;
assert.equal(grantReconcileCalls, 1);
assert.equal(
  breakGlassStore.getState().prompt,
  replacementPrompt,
  "an old actor's post-grant reconciliation cleared the replacement actor's prompt",
);
breakGlassStore.getState().cancel();
assert.equal(await replacementOutcome, false);

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
const breakGlassStoreSource = readFileSync(
  join(root, "frontend/src/lib/break-glass-store.ts"),
  "utf8",
);
const personalizationProvider = readFileSync(
  join(root, "frontend/src/components/personalization/use-personalization-mutations.ts"),
  "utf8",
);

const authSession = readFileSync(join(root, "frontend/src/auth-session.ts"), "utf8");
const rootLayout = readFileSync(join(root, "frontend/src/app/layout.tsx"), "utf8");
const nextAuthReact = readFileSync(
  requireFromFrontend.resolve("next-auth/react").replace(/react\.(?:d\.ts|js)$/, "react.js"),
  "utf8",
);
// Every source file importing the server-side session reader: only the root layout may.
const { readdirSync, statSync } = await import("node:fs");
function sourceFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(?:ts|tsx)$/.test(name) ? [path] : [];
  });
}
const authSessionImporters = sourceFiles(join(root, "frontend/src"))
  .filter((path) => /from ["']@\/auth-session["']/.test(readFileSync(path, "utf8")))
  .map((path) => path.slice(join(root, "frontend/src").length + 1));
const hydrateReader = authSession.slice(authSession.indexOf("export async function readHydratableSession"));

// The browser learns that the token rotated from a stamp of it (lib/token-stamp.ts), never the
// token. The real module, against the host's WebCrypto: the frontend runs on Node 24 and in
// browsers, both with a global one; the host's Node 18 only exposes it as a module.
globalThis.crypto ??= (await import("node:crypto")).webcrypto;
const { tokenStamp } = compileCommonJs("frontend/src/lib/token-stamp.ts", {});
{
  const token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJxYS1hZG1pbiJ9.c2lnbmF0dXJl-_x";
  const stamp = await tokenStamp(token);
  assert.match(stamp, /^[0-9a-f]{24}$/);
  assert.equal(await tokenStamp(token), stamp, "one token, one stamp");
  assert.notEqual(await tokenStamp(`${token}2`), stamp, "a rotated token gets another stamp");
  assert.ok(!token.includes(stamp) && !stamp.includes("eyJ"), "a stamp carries nothing of the token");
  assert.equal(await tokenStamp(undefined), undefined);
  assert.equal(await tokenStamp(""), undefined);
}
const authConfigSource = readFileSync(join(root, "frontend/src/auth.ts"), "utf8");
const sessionCallback = authConfigSource.slice(
  authConfigSource.indexOf("async session({ session, token })"),
  authConfigSource.indexOf("authorized({ auth, request })"),
);

// Every Cache-Control rule next.config.js declares, evaluated rather than pattern-matched: a
// hydrated document must keep Next's own private, no-store, which any configured rule replaces.
const nextConfigModule = requireFromFrontend(join(root, "frontend/next.config.js"));
const pageCacheRules = (await nextConfigModule.headers())
  .filter((rule) => rule.headers.some((h) => h.key.toLowerCase() === "cache-control"))
  .map((rule) => rule.source)
  .filter((source) => !/^\/geo\//.test(source));

const structuralChecks = [
  // Session hydration (auth-session.ts). A server component cannot persist a rotated refresh
  // token, so the peek instance must never refresh, and a doubtful session must stay undefined.
  [
    !/refreshAccessToken|jwtCallback/.test(authSession) &&
      /token\.error/.test(authSession) &&
      /token\.expiresAt - nowSeconds >= HYDRATE_MIN_VALIDITY_S/.test(authSession),
    "the server-side session peek can refresh a token or accept one without a validity margin",
  ],
  [
    !/return null/.test(hydrateReader) &&
      /tokenStamp: session\.tokenStamp/.test(hydrateReader) &&
      !/accessToken/.test(hydrateReader) &&
      !/\.\.\.session\b/.test(hydrateReader),
    "readHydratableSession can return null, the access token, or more than its projection",
  ],
  [
    sessionCallback.length > 0 &&
      /tokenStamp: await tokenStamp\(token\.accessToken\)/.test(sessionCallback) &&
      !/accessToken:/.test(sessionCallback) &&
      !/refreshToken/.test(sessionCallback),
    "the session callback hands the browser the access or refresh token instead of the stamp",
  ],
  [
    /const session = initialSession;/.test(providers) &&
      /<AuthProvider session=\{session\}>/.test(providers) &&
      /<Providers [^>]*initialSession=\{initialSession\}/.test(rootLayout),
    "the layout's session does not reach SessionProvider as it was read",
  ],
  [
    JSON.stringify(authSessionImporters) === JSON.stringify(["app/layout.tsx"]) &&
      !/from ["']@\/auth["']/.test(rootLayout),
    `only the root layout may read the server session, never through auth(): ${authSessionImporters}`,
  ],
  [
    /<SessionProvider\s+session=\{session\}/.test(authProvider) &&
      /<TokenSync hydrated=\{session !== undefined\}>/.test(authProvider) &&
      /if \(!hydrated \|\| alignedRef\.current \|\| !authenticatedContextReady\) return;/.test(authProvider) &&
      /__NEXTAUTH\._getSession\(\{ event: "storage" \}\)/.test(authProvider),
    "a hydrated session is not refreshed once in the background after the session is installed",
  ],
  [
    pageCacheRules.length === 0,
    `next.config.js sets Cache-Control on page routes, replacing Next's no-store: ${pageCacheRules}`,
  ],
  [
    /hasInitialSession = props\.session !== undefined/.test(nextAuthReact) &&
      /storageEvent \|\| __NEXTAUTH\._session === undefined/.test(nextAuthReact),
    "next-auth/react no longer behaves as the session hydration relies on; re-verify after upgrading",
  ],
  [
    /getActorQueryScope\(session\)/.test(authProvider) &&
      /setSessionAuth\(token, scope, actor\)/.test(authProvider) &&
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
    "a stale sign-out confirmation can clear a newer actor's session",
  ],
  [
    // The write epoch keeps a post-write read from joining a GET that predates the write
    /const inflightKey = `\$\{authGeneration\}:\$\{_writeEpoch\}:\$\{endpoint\}`/.test(apiSource) &&
      /_inflightGets\.get\(inflightKey\)/.test(apiSource) &&
      /_inflightGets\.set\(inflightKey, request\)/.test(apiSource),
    "GET in-flight dedupe is not scoped by non-secret auth generation and write epoch",
  ],
  [
    /_authActorGeneration !== requestActorGeneration/.test(apiSource) &&
      /same actor's potentially refreshed token/.test(apiSource),
    "401 recovery can replay a previous actor's request as the current actor",
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
    /api\.snapshots\.list\(instanceName, host, project(?:, \{ signal \})?\)/.test(hooks) &&
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
      (taskMutations.match(
        /reconcileMutationImpact\(queryClient, "task\.cancel"\)/g,
      )?.length ?? 0) === 3 &&
      (taskMutations.match(
        /reconcileMutationImpact\(queryClient, "task\.session"\)/g,
      )?.length ?? 0) === 1 &&
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
  [
    /await reconcileExternalMutation\("break-glass\.write"[\s\S]*?controller\.signal\.aborted[\s\S]*?submitEpoch !== actorEpoch[\s\S]*?get\(\)\.prompt !== p[\s\S]*?p\.resolve\(true\)/.test(
      breakGlassStoreSource,
    ),
    "break-glass submit does not re-check its actor epoch and prompt after awaited reconciliation",
  ],
  [
    // The one personal write is the theme (no debounce, so nothing pending to clear): it is
    // fenced before the optimistic update, dispatched through the fence, and re-checked after
    // the await before it touches saving state or toasts.
    /onAuthActorChanged\(\(\) => setIsSaving\(false\)\)/.test(personalizationProvider) &&
      (personalizationProvider.match(/captureActorMutationFence\(scopeKey\)/g)?.length ?? 0) >= 2 &&
      (personalizationProvider.match(/dispatchActorMutation\(/g)?.length ?? 0) >= 1 &&
      (personalizationProvider.match(/isActorMutationFenceCurrent\(actorFence\)/g)?.length ?? 0) >= 3,
    "personalization writes are not retired before dispatch and after actor-boundary awaits",
  ],
];

const failures = structuralChecks.filter(([passed]) => !passed).map(([, reason]) => reason);
if (failures.length > 0) {
  console.error(`auth transition isolation: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log(
  `auth transition isolation: runtime race cases and ${structuralChecks.length} structural guards passed`,
);
