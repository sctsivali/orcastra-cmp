#!/usr/bin/env node
/**
 * Controlled API enums keep their machine value for filters and comparisons, but never leak
 * that spelling into operator-facing copy. The shared formatLabel helper owns unknown enum
 * fallback; small closed vocabularies may use typed metadata maps so label and tone stay paired.
 *
 * This is deliberately a focused registry rather than a broad `.status` ban. User-authored
 * names, endpoints and architecture values must not be title-cased, and JSX comparisons such as
 * `status === "active"` must continue to use the raw wire value.
 *
 * Run: node scripts/checks/check-status-display.mjs [repo-root]
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const checks = [
  {
    file: "frontend/src/app/inventory/monitoring/monitoring-content.tsx",
    required: [
      [/>Offline<\/span>/, "the chart tooltip must display Offline"],
      [
        /<RefreshButton[\s\S]{0,700}aria-label=\{isPaused \? "Resume automatic monitoring updates" : "Pause automatic monitoring updates"\}[\s\S]{0,200}aria-pressed=\{isPaused\}[\s\S]{0,700}<NodeFilterSelect/,
        "the toolbar must keep Refresh, Pause/Resume, then the node filter",
      ],
      [
        /refreshMonitoring[\s\S]{0,500}Promise\.allSettled[\s\S]{0,300}refreshDashboard\(\)[\s\S]{0,300}refetchHistory[\s\S]{0,300}refreshWarnings\(\)[\s\S]{0,100}refreshSystemWarnings\(\)/,
        "manual refresh must update dashboard, history, and warnings while paused",
      ],
      [
        /useWarnings\(undefined, \{ enabled: !isPaused \}\)/,
        "Pause must disable automatic warning reads",
      ],
      [
        /useSystemWarnings\(\{ enabled: !isPaused \}\)/,
        "Pause must disable automatic system-warning reads",
      ],
      [
        /isPaused \? <Play[\s\S]{0,100}: <Pause/,
        "the monitoring toggle must use Play/Pause semantics",
      ],
      [
        /Workloads (?:kept|will keep) running/,
        "the pause tooltip must state that workloads keep running",
      ],
      [
        /isPaused && pausedSnapshot\?\.actorScope === actorScope/,
        "Pause must render an actor-owned frozen snapshot",
      ],
      [
        /dashboard: dashboardState\?\.data \?\? previous\?\.dashboard/,
        "manual refresh while paused must replace each successful section and keep last-good data after a partial failure",
      ],
    ],
    forbidden: [
      [/>offline<\/span>/, "the chart tooltip renders lowercase offline"],
      [/<Square className="h-4 w-4" \/>/, "the monitoring toggle still uses a Stop glyph"],
    ],
  },
  {
    // The distribution card's instance list, extracted from monitoring-content.tsx.
    file: "frontend/src/app/inventory/monitoring/components/instance-distribution-dialog.tsx",
    required: [
      [/instanceStatusDisplay\(instance\.status\)/, "instance status fallback must use its display map"],
      [/\{status\.label\}/, "the instance list must render the status display label"],
    ],
    forbidden: [[/\{instance\.status\}/, "the instance list renders a raw instance status"]],
  },
  {
    file: "frontend/src/app/technologies/components/tenant-instance-technologies.tsx",
    required: [
      [/instanceStatusDisplay\(instance\.status\)/, "Tenant instance status needs a display map"],
      [/\{status\.label\}/, "Tenant instance status must render its display label"],
    ],
    forbidden: [[/\{instance\.status\}/, "Tenant Technologies renders a raw instance status"]],
  },
  {
    file: "frontend/src/app/inventory/clustering/members/page.tsx",
    required: [
      [/memberStatusDisplay\(member\.status\)/, "cluster member status needs a display map"],
      [/\{status\.label\}/, "cluster member status must render its display label"],
    ],
    forbidden: [[/\{member\.status\}/, "cluster member status is rendered directly"]],
  },
  {
    file: "frontend/src/components/dashboard/instance-row.tsx",
    required: [
      [/instanceStatusDisplay\(vm\.status\)/, "dashboard instance status needs a display map"],
      [/\{statusDisplay\.label\}/, "dashboard instance status must render its display label"],
    ],
    forbidden: [[/\{vm\.status\}/, "dashboard renders a raw instance status"]],
  },
  {
    file: "frontend/src/components/instance-detail/instance-detail-dialog.tsx",
    required: [
      [/instanceStatusDisplay\(detail\.status\)/, "instance detail status needs a display map"],
      [/\{statusDisplay\.label\}/, "instance detail status must render its display label"],
    ],
    forbidden: [[/\{detail\.status\}/, "instance detail renders a raw status"]],
  },
  {
    file: "frontend/src/components/instance-detail/instance-overview-tab.tsx",
    required: [
      [/instanceStatusDisplay\(instance\.status\)/, "instance overview status needs a display map"],
      [/\{statusDisplay\.label\}/, "instance overview status must render its display label"],
    ],
    forbidden: [[/\{instance\.status\}/, "instance overview renders a raw status"]],
  },
  {
    file: "frontend/src/app/inventory/instances/components/instance-table-row.tsx",
    required: [
      [
        /instanceStatusDisplay\(instance\.status\)/,
        "instance inventory status must render its display label",
      ],
      [/\{statusDisplay\.label\}/, "instance inventory status must render its mapped label"],
    ],
    forbidden: [[/\{instance\.status\}/, "instance inventory renders a raw status"]],
  },
  {
    file: "frontend/src/components/member-resources/member-instances-dialog.tsx",
    required: [
      [
        /instanceStatusDisplay\(instance\.status\)\.label/,
        "member instance status must render its display label",
      ],
    ],
    forbidden: [[/\{instance\.status\}/, "member dialog renders a raw instance status"]],
  },
  {
    file: "frontend/src/components/console/boot-priority-panel.tsx",
    required: [
      [
        /instanceStatusDisplay\(entry\.status\)\.label/,
        "boot-priority status must render its display label",
      ],
    ],
    forbidden: [[/\{entry\.status\}/, "boot-priority panel renders a raw instance status"]],
  },
  {
    file: "frontend/src/components/break-glass/break-glass-panel.tsx",
    required: [
      [/Record<BreakGlassGrantStatus,/, "break-glass status needs a typed display map"],
      [/meta\.label/, "break-glass status must render its display label"],
      [
        /grantsQueryKey = \["break-glass-grants", scopeKey\] as const/,
        "break-glass grants must use an actor-scoped browser-cache key",
      ],
      [
        /enabled: isAuthenticated/,
        "break-glass grants must not fetch before authentication resolves",
      ],
      [
        /setQueryData<BreakGlassGrant\[\]>\(grantsQueryKey/,
        "break-glass revoke must update only the actor that issued it",
      ],
      [
        /reconcileMutationImpact\(queryClient, "break-glass\.write", \{[\s\S]{0,120}queryKeys: \[grantsQueryKey\],[\s\S]{0,80}refetchType: "none"/,
        "break-glass invalidation must not refetch through a stale actor callback",
      ],
    ],
    forbidden: [
      [/<Badge[^>]*>\s*(?:active|expired|revoked)\s*<\/Badge>/, "a break-glass wire value is rendered directly"],
      [/await refetch\(\)/, "break-glass revoke can refetch a stale actor key after sign-in changes"],
    ],
  },
  {
    file: "frontend/src/app/inventory/sessions/page.tsx",
    required: [
      [
        /format: \(status\) => sessionConnectionStatusDisplay\(status\)\.label/,
        "session status facet must use the shared display map",
      ],
      [/\{connectionStatus\.label\}/, "session status must render its display label"],
      [
        /accessToken: actorAccessToken,[\s\S]{0,80}= useActorQueryScope\(\)/,
        "session tabs must bind requests to the authenticated actor's token",
      ],
      [
        /const currentSessions = sessionsAreCurrent \? sessions : \[\]/,
        "active sessions need a render-time actor gate",
      ],
      [
        /const currentLogs = logsAreCurrent \? logs : \[\]/,
        "session history needs a render-time actor gate",
      ],
      [
        /signal: controller\.signal/,
        "session reads must support cancellation",
      ],
      [
        /Authorization: `Bearer \$\{actorAccessToken\}`/,
        "session reads must not use a lagging module-global token during actor transitions",
      ],
      [
        /if \(bound\.scope !== requestActorScope\) return;\s*const actorAccessToken = bound\.token;/,
        "a session read must only carry the token of the actor it was made for",
      ],
      [
        /activeActorScopeRef\.current !== requestActorScope/,
        "late session responses must verify their actor",
      ],
    ],
    forbidden: [[/\{\s*session\.connection_status\s*\}/, "session connection_status is rendered directly"]],
  },
  {
    file: "frontend/src/app/inventory/sessions/components/session-connection-status.ts",
    required: [
      [
        /Record<[\s\S]{0,80}SessionConnectionStatus,/,
        "session status needs a typed shared display map",
      ],
      [
        /export function sessionConnectionStatusDisplay/,
        "session status display helper must be reusable",
      ],
    ],
    forbidden: [],
  },
  {
    file: "frontend/src/app/inventory/sessions/components/session-detail-dialog.tsx",
    required: [
      [
        /sessionConnectionStatusDisplay\(record\.connection_status\)/,
        "active-session detail must display the actual connection status",
      ],
      [
        /status\.label/,
        "session detail must render its mapped label",
      ],
    ],
    forbidden: [
      [
        /isActive \? "Active"/,
        "session detail must not hardcode every active record as Active",
      ],
    ],
  },
  {
    file: "frontend/src/components/sidebar/session-menu-items.tsx",
    required: [
      [/Record<[\s\S]{0,80}SessionWindowStatus,/, "sidebar window status needs a typed map"],
      [/\{status\.label\}/, "sidebar window status must render its display label"],
    ],
    forbidden: [
      [/>minimized<\//, "sidebar renders lowercase minimized"],
      [/>open<\//, "sidebar renders lowercase open"],
    ],
  },
  {
    file: "frontend/src/components/task-panel/task-format.ts",
    required: [
      [
        /const TASK_STATUS_DISPLAY: Record<TaskStatus, TaskStatusDisplay>/,
        "task status needs an exhaustive typed display map",
      ],
      [
        /const LOG_SEVERITY_DISPLAY: Record<LogSeverity, LogSeverityDisplay>/,
        "task-log severity needs an exhaustive typed display map",
      ],
      [/export function taskStatusDisplay/, "task status metadata must be shared"],
      [/export function taskStatusLabel/, "task status facets need a shared label helper"],
      [/export function logSeverityDisplay/, "task-log severity metadata must be shared"],
      [/export function logSeverityLabel/, "task-log severity facets need a shared label helper"],
    ],
    forbidden: [],
  },
  {
    file: "frontend/src/components/task-panel/task-cells.tsx",
    required: [
      [/taskStatusDisplay\(task\.status\)/, "task rows need the typed status display map"],
      [/<Badge variant=\{status\.tone\}>\{status\.label\}<\/Badge>/, "task rows must render the mapped status label"],
      [/logSeverityDisplay\(severity\)/, "task-log rows need the typed severity display map"],
    ],
    forbidden: [
      [/formatLabel\(task\.status\)/, "task rows bypass the typed status display map"],
      [/formatLabel\(severity\)/, "task-log rows bypass the typed severity display map"],
      [/\{\s*task\.status\s*\}/, "task status is rendered directly"],
    ],
  },
  {
    file: "frontend/src/components/task-panel/task-detail-dialog.tsx",
    required: [
      [/taskStatusDisplay\(task\.status\)/, "task detail needs the typed status display map"],
      [/<Badge variant=\{status\.tone\}>\{status\.label\}<\/Badge>/, "task detail must render the mapped status label"],
      [/logSeverityLabel\(entry\.severity\)/, "task detail log must display mapped severity labels"],
    ],
    forbidden: [
      [/formatLabel\(task\.status\)/, "task detail bypasses the typed status display map"],
      [/\{\s*task\.status\s*\}/, "task detail renders the raw wire status"],
      [/\[\$\{entry\.severity\}\]/, "task detail log renders a raw wire severity"],
    ],
  },
  {
    file: "frontend/src/components/task-panel/tasks-tab.tsx",
    required: [
      [/format: taskStatusLabel/, "Task dock status facets need the shared label helper"],
    ],
    forbidden: [[/format: formatLabel[\s\S]{0,80}STATUS_ORDER/, "Task dock status facets use a generic formatter"]],
  },
  {
    file: "frontend/src/components/task-panel/queue-tab.tsx",
    required: [
      [/taskStatusDisplay\(task\.status\)/, "queued tasks need the typed status display map"],
      [/<Badge variant=\{status\.tone\}>\{status\.label\}<\/Badge>/, "queued tasks must render the mapped status label"],
    ],
    forbidden: [[/formatLabel\(task\.status\)/, "queued tasks use a generic status formatter"]],
  },
  {
    file: "frontend/src/components/task-panel/log-tab.tsx",
    required: [
      [/format: logSeverityLabel/, "Task log severity facets need the shared label helper"],
      [/logSeverityLabel\(entry\.severity\)/, "copied task log lines must use the mapped severity label"],
    ],
    forbidden: [[/format: formatLabel[\s\S]{0,80}SEVERITY_ORDER/, "Task log severity facets use a generic formatter"]],
  },
  {
    file: "frontend/src/app/inventory/operations/page.tsx",
    required: [
      [/useTaskWindow/, "Operations must remain the latest Task Journal page"],
      [/format: taskStatusLabel/, "Task Journal status facets must use the shared label helper"],
      [
        /taskJournalUiIsCurrent = taskJournalUiActorScope === scopeKey/,
        "Task Journal dialogs need a render-time actor-owner gate",
      ],
      [
        /previousTaskJournalActorScopeRef\.current === scopeKey[\s\S]{0,350}setDetailTaskId\(null\)[\s\S]{0,120}setCancelTask\(null\)/,
        "Task Journal detail and destructive confirmation state must clear on actor change",
      ],
      [
        /open=\{taskJournalUiIsCurrent && currentCancelTask !== null\}/,
        "Task Journal cancellation can render for its previous actor",
      ],
      [
        /open=\{taskJournalUiIsCurrent && currentDetailTaskId !== null\}/,
        "Task Journal detail can render for its previous actor",
      ],
    ],
    forbidden: [
      [/useOperations/, "Operations restored the deleted legacy operation inventory"],
      [/OperationDetailDialog/, "Operations restored the deleted legacy operation dialog"],
      [/operationStatusLabel/, "Operations restored the legacy status vocabulary"],
      [/format: formatLabel[\s\S]{0,80}STATUS_ORDER/, "Task Journal status facets use a generic formatter"],
    ],
  },
  {
    file: "frontend/src/components/task-panel/hooks/use-tasks.ts",
    required: [
      [
        /all: \(scopeKey: string\) => \["tasks", scopeKey\]/,
        "Task Journal cache roots must include the actor scope",
      ],
      [
        /list: \(scopeKey: string, limit: number\) => \["tasks", scopeKey, "list"/,
        "Task Journal list keys must include the actor scope",
      ],
      [
        /detail: \(scopeKey: string, id: string\) => \["tasks", scopeKey, "detail", id\]/,
        "Task Journal detail keys must include the actor scope",
      ],
      [
        /all: \(scopeKey: string\) => \["cluster-log", scopeKey\]/,
        "Task log cache roots must include the actor scope",
      ],
      [
        /previousQuery\?\.queryKey\[1\] === scopeKey \? previous : undefined/,
        "Task Journal placeholder data must not cross actor scope",
      ],
      [
        /upsertTaskInCaches\([\s\S]{0,120}scopeKey: string/,
        "Task feed patches must name their actor scope",
      ],
      [
        /\["cluster-log", scopeKey, "list"\]/,
        "Task log feed patches must only reach the active actor's lists",
      ],
    ],
    forbidden: [
      [/queryKey: taskKeys\.(?:summary|queue)\b(?!\()/, "Task queries still use a global cache key"],
      [/queryKey: taskKeys\.(?:list|window|detail)\([^s]/, "Task query keys omit their actor scope"],
      [/queryKey: logKeys\.list\(limit\)/, "Task log queries still use a global cache key"],
    ],
  },
  {
    file: "frontend/src/components/task-panel/use-task-feed.ts",
    required: [
      [/let activeScopeKey: string \| null = null/, "Task feed must own the active actor scope"],
      [
        /taskKeys\.all\(activeScopeKey\)[\s\S]{0,160}logKeys\.all\(activeScopeKey\)/,
        "Task feed resync must invalidate only the active actor",
      ],
      [
        /upsertTaskInCaches\(queryClient, activeScopeKey, frame\.task\)/,
        "Task frames must patch only the active actor's cache",
      ],
      [
        /appendLogInCaches\(queryClient, activeScopeKey, frame\.entry\)/,
        "Task log frames must patch only the active actor's cache",
      ],
      [
        /function acquire\(client: QueryClient, scopeKey: string\)[\s\S]{0,100}activeScopeKey = scopeKey/,
        "Task feed acquisition must bind the socket to its actor scope",
      ],
      [
        /function release\(\)[\s\S]{0,180}clearRetry\(\)[\s\S]{0,180}closeSocket\(\)[\s\S]{0,320}activeScopeKey = null/,
        "Task feed must cancel retries, close the socket, and release actor scope",
      ],
      [
        /function release\(\)[\s\S]{0,500}handshakeFailures = 0[\s\S]{0,120}halted = false[\s\S]{0,400}snapshot = \{ \.\.\.IDLE \}/,
        "Task feed must discard the previous actor's authentication and follower state",
      ],
      [
        /acquire\(client, scopeKey\)[\s\S]{0,80}return release[\s\S]{0,80}\[enabled, client, scopeKey\]/,
        "Task feed must reconnect when the authenticated actor changes",
      ],
    ],
    forbidden: [],
  },
  {
    file: "frontend/src/components/task-panel/task-panel.tsx",
    required: [
      [
        /taskPanelUiIsCurrent = taskPanelUiActorScope === scopeKey/,
        "Task dock dialogs need a render-time actor-owner gate",
      ],
      [
        /previousTaskPanelActorScopeRef\.current === scopeKey[\s\S]{0,350}setDetailTaskId\(null\)[\s\S]{0,120}setCancelTask\(null\)/,
        "Task dock detail and cancellation state must clear on actor change",
      ],
      [
        /open=\{taskPanelUiIsCurrent && currentCancelTask !== null\}/,
        "Task dock cancellation can render for its previous actor",
      ],
      [
        /open=\{taskPanelUiIsCurrent && currentDetailTaskId !== null\}/,
        "Task dock detail can render for its previous actor",
      ],
      [
        /currentAnnouncement = taskPanelUiIsCurrent \? announcement : ""/,
        "Task dock live announcements need a render-time actor-owner gate",
      ],
    ],
    forbidden: [],
  },
  {
    file: "frontend/src/app/inventory/warnings/page.tsx",
    required: [
      [/format: warningSeverityLabel/, "warning severity facets need the typed display map"],
      [/warningSeverityDisplay\(warning\.severity/, "warning rows need the typed display map"],
      [
        /warningUiIsCurrent = warningUiActorScope === scopeKey/,
        "warning detail needs a render-time actor-owner gate",
      ],
      [
        /previousWarningActorScopeRef\.current === scopeKey[\s\S]{0,250}setSelectedWarning\(null\)[\s\S]{0,100}setDetailModalOpen\(false\)/,
        "warning detail state must clear on actor change",
      ],
      [
        /selected=\{currentSelectedWarning\}[\s\S]{0,100}open=\{warningUiIsCurrent && detailModalOpen\}/,
        "warning detail can render another actor's selected record",
      ],
    ],
    forbidden: [
      [/\{\s*warning\.severity\s*\}/, "warning severity is rendered directly"],
      [/formatLabel\(warning\.severity/, "warning severity bypasses its typed display map"],
    ],
  },
  {
    file: "frontend/src/app/inventory/warnings/components/warning-detail-dialog.tsx",
    required: [
      [/warningSeverityDisplay\(record\.severity/, "warning detail severity needs its typed display map"],
      [/warningStatusDisplay\(record\.status\)/, "warning detail status needs its typed display map"],
    ],
    forbidden: [
      [/\{\s*record\.severity\s*\}/, "warning detail severity is rendered directly"],
      [/value=\{record\.status\}/, "warning detail status is rendered directly"],
      [/formatLabel\(record\.(?:severity|status)/, "warning detail bypasses a typed display map"],
    ],
  },
  {
    file: "frontend/src/app/inventory/warnings/components/warning-severity.ts",
    required: [
      [/Record<WarningSeverityKey, WarningDisplay>/, "warning severity needs a typed display map"],
      [/Record<WarningStatusKey, WarningDisplay>/, "warning status needs a typed display map"],
    ],
    forbidden: [],
  },
  {
    file: "frontend/src/hooks/use-lxd.ts",
    required: [
      [
        /queryKey: \["warnings", scopeKey, host\]/,
        "warning rows must use an actor-scoped browser-cache key",
      ],
      [
        /queryKey: \["systemWarnings", scopeKey\]/,
        "system warnings must use an actor-scoped browser-cache key",
      ],
      [
        /enabled: isAuthenticated && isPartner && \(options\.enabled \?\? true\)/,
        "warning reads must wait for an authenticated Partner/Admin actor",
      ],
    ],
    forbidden: [
      [/queryKey: \["warnings", host\]/, "warning rows still use a global cache key"],
      [/queryKey: \["systemWarnings"\]/, "system warnings still use a global cache key"],
    ],
  },
  {
    file: "frontend/src/lib/resource-status.ts",
    required: [
      [
        /const RESOURCE_STATUS_DISPLAY: Record<ResourceStatusKey, ResourceStatusDisplay>/,
        "network and storage-pool statuses need an exhaustive typed display map",
      ],
      [
        /status\?\.trim\(\)\.toLowerCase\(\)/,
        "resource status matching must ignore API casing",
      ],
      [
        /export const networkStatusDisplay = resourceStatusDisplay/,
        "network status needs a shared display helper",
      ],
      [
        /export const storagePoolStatusDisplay = resourceStatusDisplay/,
        "storage-pool status needs a shared display helper",
      ],
    ],
    forbidden: [],
  },
  {
    file: "frontend/src/app/inventory/networks/page.tsx",
    required: [
      [/import \{ networkStatusDisplay \}/, "network inventory must import the shared status helper"],
      [
        /format: \(status\) => networkStatusDisplay\(status\)\.label/,
        "network status facets must use the shared display helper",
      ],
      [/networkStatusDisplay\(network\.status\)/, "network rows need the shared status helper"],
      [/variant=\{status\.tone\}/, "network status must render its mapped semantic tone"],
      [/\{status\.label\}/, "network status must render its display label"],
      [
        /label: "Status", value: networkStatusDisplay\(network\.status\)\.label/,
        "network detail facts must render the mapped status label",
      ],
    ],
    forbidden: [
      [/\{\s*network\.status\s*\}/, "network status is rendered directly"],
      [/label: "Status", value: network\.status/, "network detail facts expose the raw status"],
    ],
  },
  {
    file: "frontend/src/components/network-detail.tsx",
    required: [
      [/import \{ networkStatusDisplay \}/, "network dialog must import the shared status helper"],
      [
        /networkStatusDisplay\(network\.status\)\.label/,
        "network dialog status must use its mapped display label",
      ],
    ],
    forbidden: [
      [/\{network\.status \|\|/, "network detail renders the raw status"],
      [/formatLabel\(network\.status\)/, "network detail bypasses the typed status display map"],
    ],
  },
  {
    file: "frontend/src/app/inventory/storage-pools/page.tsx",
    required: [
      [
        /import \{ storagePoolStatusDisplay \}/,
        "storage-pool inventory must import the shared status helper",
      ],
      [
        /format: \(status\) => storagePoolStatusDisplay\(status\)\.label/,
        "storage-pool status facets must use the shared display helper",
      ],
      [
        /storagePoolStatusDisplay\(pool\.status\)/,
        "storage-pool rows need the shared status helper",
      ],
      [/variant=\{status\.tone\}/, "storage-pool status must render its mapped semantic tone"],
      [/\{status\.label\}/, "storage-pool status must render its display label"],
      [
        /label: "Status", value: storagePoolStatusDisplay\(pool\.status\)\.label/,
        "storage-pool detail facts must render the mapped status label",
      ],
    ],
    forbidden: [
      [/\{\s*pool\.status\s*\}/, "storage-pool status is rendered directly"],
      [/label: "Status", value: pool\.status/, "storage-pool detail facts expose the raw status"],
    ],
  },
];

const failures = [];
for (const check of checks) {
  const body = readFileSync(join(root, check.file), "utf8");
  for (const [pattern, reason] of check.required) {
    if (!pattern.test(body)) failures.push(`${check.file}: ${reason}`);
  }
  for (const [pattern, reason] of check.forbidden) {
    if (pattern.test(body)) failures.push(`${check.file}: ${reason}`);
  }
}

const monitoringWs = readFileSync(
  join(root, "frontend/src/hooks/use-monitoring-ws.ts"),
  "utf8",
);
if (
  !/let active = true/.test(monitoringWs) ||
  !/active = false[\s\S]{0,500}clearReconnect\(\)/.test(monitoringWs) ||
  !/ws\.onclose = null/.test(monitoringWs) ||
  !/if \(active\) connect\(\)/.test(monitoringWs)
) {
  failures.push(
    "frontend/src/hooks/use-monitoring-ws.ts: Pause/unmount must cancel timers and detach close handlers before closing",
  );
}

if (failures.length > 0) {
  console.error(`controlled status display: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log(`controlled status display: ${checks.length} surfaces use operator-facing labels`);
