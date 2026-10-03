#!/usr/bin/env node
/** Runtime checks for journal durations, source health and bounded queue requests. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = join(root, "frontend", "src");
const requireFrontend = createRequire(join(root, "frontend", "package.json"));
let ts;
try {
  ts = requireFrontend("typescript");
} catch {
  console.log("task journal runtime checks: skipped until frontend dependencies are installed");
  process.exit(0);
}

function load(file, imports) {
  const source = readFileSync(join(src, file), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
    fileName: file,
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", output)((name) => {
    if (name in imports) return imports[name];
    throw new Error(`Unexpected task runtime import: ${name}`);
  }, module, module.exports);
  return module.exports;
}

const utils = load("lib/utils.ts", {
  clsx: requireFrontend("clsx"),
  "tailwind-merge": requireFrontend("tailwind-merge"),
  "@/lib/table-empty": { EMPTY_VALUE: "\u2014" },
});
const format = load("components/task-panel/task-format.ts", {
  "@/lib/utils": utils,
  "@/lib/inventory-links": {},
});
for (const value of [NaN, Infinity, -Infinity, -1]) assert.equal(format.formatDuration(value), "");
assert.equal(format.formatDuration(0), "0s");
assert.equal(format.formatDuration(500), "0s");
assert.equal(format.formatDuration(65_000), "1m 05s");
assert.equal(format.kindLabel("lxd_operation"), "Hypervisor Operation");
const row = {
  status: "success", started_at: "2026-09-30T00:00:00Z", created_at: "2026-09-30T00:00:00Z",
  finished_at: "2026-09-30T00:00:01Z", duration_ms: 0,
};
assert.equal(format.durationMs(row), 0);
for (const duration_ms of [NaN, Infinity, -1]) assert.equal(format.durationMs({ ...row, duration_ms }), null);
assert.equal(format.durationMs({ ...row, status: "pending" }), null);
assert.equal(format.durationMs({ ...row, status: "running", finished_at: null }, Infinity), null);
assert.equal(format.durationMs({ ...row, status: "running", finished_at: null }, 0), 0);
assert.equal(format.startedMs({ created_at: "invalid", started_at: "invalid" }), null);

const live = load("components/task-panel/live-indicator.tsx", {
  react: {}, "react/jsx-runtime": {}, "@/components/ui/tooltip": {}, "@/lib/utils": utils,
});
const feed = { state: "connected", followerActive: true };
assert.equal(live.liveStateOf(feed, false, 0), "connected");
assert.equal(live.liveStateOf(feed, false, 1), "degraded");
assert.equal(live.liveStateOf(feed, true, 1), "offline");
assert.equal(live.liveStateOf({ ...feed, followerActive: false }, false, 1), "journal-paused");
assert.equal(live.liveStateOf({ ...feed, state: "polling" }, false, 1), "polling");

const calls = [];
const { tasksAPI } = load("lib/tasks-api.ts", {
  "@/lib/api": { fetchAPI: async (...args) => { calls.push(args); return {}; } },
});
await tasksAPI.queue({ limit: 250, offset: 500 }, { fresh: true });
assert.equal(calls[0][0], "/tasks/queue?limit=250&offset=500");
assert.equal(calls[0][3], false);
assert.deepEqual(calls[0][4], { force: true });
await tasksAPI.queue();
assert.equal(calls[1][0], "/tasks/queue");
console.log("task journal runtime checks: finite durations, source health and queue request pagination passed");
