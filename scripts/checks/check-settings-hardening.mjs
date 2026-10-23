#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const frontendRequire = createRequire(join(root, "frontend/package.json"));
const ts = frontendRequire("typescript");
const { QueryClient, QueryObserver } = frontendRequire("@tanstack/react-query");

function load(relativePath, dependencies = {}) {
  const output = ts.transpileModule(readFileSync(join(root, relativePath), "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", output)((name) => {
    if (Object.hasOwn(dependencies, name)) return dependencies[name];
    throw new Error(`Unexpected module dependency: ${name}`);
  }, module, module.exports);
  return module.exports;
}

const types = load("frontend/src/types/personalization.ts");
const preferences = load("frontend/src/components/personalization/preferences.ts", { "@/types/personalization": types });
const cache = load("frontend/src/app/settings/components/browser-cache.ts", {
  "@tanstack/react-query": frontendRequire("@tanstack/react-query"),
});
const tabs = load("frontend/src/app/settings/settings-tabs.ts");

assert.equal(tabs.settingsTabPath("general"), "/settings");
assert.equal(tabs.settingsTabPath("dashboard-host"), "/settings/dashboard-host");
assert.equal(tabs.isSettingsTab("general"), true);
assert.equal(tabs.isSettingsTab("system-info"), false);
assert.equal(tabs.isSettingsTab("unknown"), false);

const middleware = load("frontend/src/middleware.ts", {
  "next-auth": { __esModule: true, default: () => ({ auth: (callback) => callback }) },
  "@/auth": { authConfig: { callbacks: {} } },
  "@/lib/request-security": load("frontend/src/lib/request-security.ts"),
  "next/server": { NextResponse: Response },
  "@/app/settings/settings-tabs": tabs,
  "@/lib/session-cookie-echo": { dropUnchangedSessionCookie: async (_request, response) => response, sessionSecret: () => undefined },
}).default;
for (const path of ["/settings", ...tabs.SETTINGS_TABS.map((tab) => `/settings/${tab}`)]) {
  assert.equal(await middleware({ method: "GET", nextUrl: new URL(`https://qa.invalid${path}`) }), undefined);
}
for (const path of ["/settings/not-a-tab", "/settings/integrations/unknown"]) {
  assert.equal((await middleware({ method: "GET", nextUrl: new URL(`https://qa.invalid${path}`) })).status, 404);
}

const empty = { organization_id: 17, updated_at: null };
assert.equal(preferences.sameOrgVersion(empty, { ...empty }), true);
assert.equal(preferences.sameOrgVersion(empty, { ...empty, updated_at: "2026-09-30T12:00:00Z" }), false);
assert.equal(preferences.sameOrgVersion(empty, { ...empty, organization_id: 18 }), false);
for (const value of [null, {}, { organization_id: "17", updated_at: null }, { organization_id: -1, updated_at: null }, { organization_id: 17, updated_at: 42 }]) {
  assert.equal(preferences.parseOrgVersion(value), null);
}

const originalFetch = globalThis.fetch;
try {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return { ok: true, json: async () => ({ ...empty, settings: {} }) }; };
  const unset = await preferences.readOrgPreferences("synthetic-read-token", () => true);
  assert.equal(unset.ok, true);
  assert.equal(unset.value.settings.colors.accentColor, types.DEFAULT_PERSONALIZATION.colors.accentColor);
  assert.equal(unset.value.updated_at, null);

  let release;
  let current = true;
  globalThis.fetch = () => new Promise((resolveFetch) => { release = resolveFetch; });
  const retired = preferences.readOrgPreferences("synthetic-read-token", () => current);
  current = false;
  release({ ok: true, json: async () => ({ ...empty, settings: {} }) });
  assert.deepEqual(await retired, { ok: false });

  globalThis.fetch = async () => { calls += 1; return { ok: false, status: 403 }; };
  const beforeDenied = calls;
  assert.deepEqual(await preferences.readOrgPreferences(undefined, () => true), { ok: false });
  assert.equal(calls, beforeDenied + 1, "permission denials must not be retried");
} finally {
  globalThis.fetch = originalFetch;
}

const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
const session = { user: "synthetic-actor" };
const personal = { theme: "dark" };
client.setQueryData(["session"], session);
client.setQueryData(["organization-preferences", 17], personal);
client.setQueryData(["instances", "synthetic-actor"], ["cached-instance"]);
client.setQueryData(["networks", "synthetic-actor"], ["cached-network"]);
client.setQueryData(["console-sessions", "synthetic-actor"], ["cached-session"]);

let readCount = 0;
const observer = new QueryObserver(client, {
  queryKey: ["nodes", "synthetic-actor"],
  queryFn: async () => { readCount += 1; return [`fresh-${readCount}`]; },
  staleTime: Infinity,
});
const unsubscribe = observer.subscribe(() => {});
await observer.refetch();
const beforeClear = readCount;

let finishWrite;
const write = client.getMutationCache().build(client, {
  mutationKey: ["instance-create", "synthetic-actor"],
  mutationFn: () => new Promise((resolveWrite) => { finishWrite = resolveWrite; }),
});
const completion = write.execute({});
await new Promise((resolveTurn) => setImmediate(resolveTurn));
assert.equal(write.state.status, "pending");

assert.equal(await cache.clearBrowserReadCache(client, () => true), true);
assert.deepEqual(client.getQueryData(["session"]), session);
assert.deepEqual(client.getQueryData(["organization-preferences", 17]), personal);
assert.equal(client.getQueryData(["instances", "synthetic-actor"]), undefined);
assert.equal(client.getQueryData(["networks", "synthetic-actor"]), undefined);
assert.equal(client.getQueryData(["console-sessions", "synthetic-actor"]), undefined);
assert.equal(readCount, beforeClear + 1, "active read is fetched once using its own query function");
assert.equal(client.getMutationCache().getAll().includes(write), true);
assert.equal(write.state.status, "pending", "clearing reads must preserve accepted writes");
finishWrite("created");
assert.equal(await completion, "created");
assert.equal(write.state.status, "success");
unsubscribe();
client.clear();

const retiredClient = new QueryClient();
retiredClient.setQueryData(["instances", "retired-actor"], ["cached"]);
let stillCurrent = true;
const clear = cache.clearBrowserReadCache(retiredClient, () => stillCurrent);
stillCurrent = false;
assert.equal(await clear, false);
assert.deepEqual(retiredClient.getQueryData(["instances", "retired-actor"]), ["cached"]);
retiredClient.clear();

const failedClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
let failRead = false;
const failingObserver = new QueryObserver(failedClient, {
  queryKey: ["instances", "synthetic-actor"],
  queryFn: async () => {
    if (failRead) throw new Error("Synthetic scoped read failed");
    return ["instance"];
  },
});
const unsubscribeFailure = failingObserver.subscribe(() => {});
await failingObserver.refetch();
failRead = true;
await assert.rejects(cache.clearBrowserReadCache(failedClient, () => true), /Synthetic scoped read failed/);
assert.equal(failedClient.getQueryState(["instances", "synthetic-actor"]).status, "error");
unsubscribeFailure();
failedClient.clear();

// A view that depends on a reset read unmounts while its own reads refetch: the task panel renders
// only on known /system/settings, so the reset unmounts it and React Query cancels its summary
// read with nothing to revert to. That rejects with CancelledError, which is not a failed clear.
const unmountClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
const slowRead = (ms, value) => ({ signal }) => new Promise((resolveRead, rejectRead) => {
  const timer = setTimeout(() => resolveRead(value), ms);
  signal.addEventListener("abort", () => { clearTimeout(timer); rejectRead(new Error("aborted")); });
});
const settingsObserver = new QueryObserver(unmountClient, { queryKey: ["system-settings"], queryFn: slowRead(10, { task_journal_enabled: true }) });
const summaryObserver = new QueryObserver(unmountClient, { queryKey: ["tasks", "synthetic-actor", "summary"], queryFn: slowRead(40, { total: 1 }) });
const unsubscribeSettings = settingsObserver.subscribe(() => {});
let unsubscribeSummary = summaryObserver.subscribe(() => {});
await Promise.all([settingsObserver.refetch(), summaryObserver.refetch()]);
// React unmounts after a commit, so the panel goes away once the summary refetch is in flight.
const unmountOnReset = settingsObserver.subscribe((result) => {
  if (result.data === undefined && unsubscribeSummary) {
    setTimeout(() => { unsubscribeSummary?.(); unsubscribeSummary = null; }, 5);
  }
});
assert.equal(await cache.clearBrowserReadCache(unmountClient, () => true), true, "a view unmounted by the reset must not fail the clear");
assert.deepEqual(unmountClient.getQueryData(["system-settings"]), { task_journal_enabled: true });
unmountOnReset();
unsubscribeSettings();
unsubscribeSummary?.();
unmountClient.clear();

console.log("settings hardening: canonical slugs, organization versions, retired reads, and browser-cache mutation preservation passed");
