#!/usr/bin/env node
/**
 * Console operation ids are cluster-wide while instance names are project-wide. Every browser
 * cancellation call must therefore send the concrete project that the backend authorizes and
 * verifies against the operation metadata.
 *
 * Run: node scripts/checks/check-console-session-project-scope.mjs [repo-root]
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const files = {
  store: "frontend/src/lib/console-store.ts",
  page: "frontend/src/app/console/[id]/page.tsx",
  window: "frontend/src/components/console/console-window.tsx",
  tasksApi: "frontend/src/lib/tasks-api.ts",
  taskHook: "frontend/src/components/task-panel/hooks/use-tasks.ts",
  taskDialog: "frontend/src/components/task-panel/task-detail-dialog.tsx",
};
const bodies = Object.fromEntries(
  Object.entries(files).map(([key, path]) => [key, readFileSync(join(root, path), "utf8")]),
);

const routeFiles = ["store", "page", "window", "tasksApi"];
const markers = ["kill-all-sessions", "/active-sessions/${"];
const offenders = [];
let calls = 0;

for (const key of routeFiles) {
  const source = bodies[key];
  for (const marker of markers) {
    let index = source.indexOf(marker);
    while (index !== -1) {
      const window = source.slice(Math.max(0, index - 450), index + 650);
      // Ignore prose that names the endpoint but is not a URL construction.
      if (window.includes("/console/") || window.includes("/console/${")) {
        calls += 1;
        if (!/(?:[&?]project=|project:\s*)/.test(window)) {
          offenders.push(`${files[key]}: ${marker} call does not carry project`);
        }
      }
      index = source.indexOf(marker, index + marker.length);
    }
  }
}

// The one write URL lives in tasksAPI: kill one operation, from the Sessions and task views.
// note: kill-all is gone from the browser. Closing a console closes its own sockets, which ends
// its own operations; kill-all cancelled every websocket operation on the instance.
if (calls !== 1) {
  offenders.push(`expected 1 centralized console cancellation URL builder, found ${calls}`);
}
if (!/project:\s*string/.test(bodies.taskHook) ||
    !/killConsoleSession\(instanceName, operationId, host, project\)/.test(bodies.taskHook)) {
  offenders.push(`${files.taskHook}: task mutation does not require and forward project`);
}
if (!/project:\s*task\.project\s*\|\|\s*["']default["']/.test(bodies.taskDialog)) {
  offenders.push(`${files.taskDialog}: task detail does not pass its recorded project`);
}
for (const key of ["store", "page", "window"]) {
  if (/kill-all-sessions|killAllConsoleSessions/.test(bodies[key])) {
    offenders.push(`${files[key]}: a console surface must not cancel other sessions on close`);
  }
}

if (offenders.length) {
  console.error("Console session cancellation lost its project boundary:");
  for (const offender of offenders) console.error(`  ${offender}`);
  process.exit(1);
}

console.log(`console session scope: ${calls} cancellation URL builders carry a project`);
