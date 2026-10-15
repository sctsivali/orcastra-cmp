#!/usr/bin/env node
/**
 * Guard the instance detail split: Overview is static row detail, while Metrics owns live reads.
 * The Monitoring chart is the one exception because its top-consumer row is deliberately partial
 * and must opt into hydration. This check keeps that exception explicit and prevents a future
 * cleanup from restoring an eager detail request for every dialog opening.
 *
 * Run: node scripts/checks/check-instance-live-metrics.mjs [repo-root]
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const files = {
  dialog: "frontend/src/components/instance-detail/instance-detail-dialog.tsx",
  metrics: "frontend/src/components/instance-detail/instance-metrics-tab.tsx",
  monitoring: "frontend/src/app/inventory/monitoring/monitoring-content.tsx",
  overview: "frontend/src/components/instance-detail/instance-overview-tab.tsx",
  queryConfig: "frontend/src/lib/query-config.ts",
  queryScope: "frontend/src/hooks/use-query-scope.ts",
  hooks: "frontend/src/hooks/use-lxd.ts",
  types: "frontend/src/types/api.ts",
  stream: "frontend/src/hooks/use-instance-stream.ts",
  sidebar: "frontend/src/components/sidebar/nav-config.tsx",
  tenantAccess: "frontend/src/components/tenant-access-dialog.tsx",
  policyManagement: "frontend/src/components/policy-management-section.tsx",
  pendingCreates: "frontend/src/lib/pending-creates.ts",
  createForm: "frontend/src/components/instance-create-form.tsx",
  instancesPage: "frontend/src/app/inventory/instances/page.tsx",
  instancesTable: "frontend/src/app/inventory/instances/components/instances-table.tsx",
  instanceRow: "frontend/src/app/inventory/instances/components/instance-table-row.tsx",
  instanceActions: "frontend/src/app/inventory/instances/components/use-instance-actions.ts",
  pendingRows: "frontend/src/app/inventory/instances/components/pending-create-rows.tsx",
  polling: "frontend/src/lib/instance-metrics-polling.ts",
  metricsView: "frontend/src/lib/instance-metrics-view.ts",
  consumers: "frontend/src/components/monitoring/top-consumers-card.tsx",
};

const bodies = Object.fromEntries(
  Object.entries(files).map(([name, file]) => [name, readFileSync(join(root, file), "utf8")]),
);

// The Metrics panel is split into components/instance-detail/metrics/; its contracts read the
// panel, its pieces and the shared view-model as one body.
const metricsDir = join(root, "frontend/src/components/instance-detail/metrics");
bodies.metrics = [
  bodies.metrics,
  bodies.metricsView,
  ...readdirSync(metricsDir)
    .filter((name) => /\.tsx?$/.test(name))
    .sort()
    .map((name) => readFileSync(join(metricsDir, name), "utf8")),
].join("\n");

const checks = [
  [
    !existsSync(
      join(root, "frontend/src/components/instance-detail/instance-resources-section.tsx"),
    ),
    "the retired Overview Resources section still exists",
  ],
  [
    !/InstanceResourcesSection|title=["']Resources["']/.test(bodies.overview),
    "Overview renders the retired Resources section",
  ],
  [
    /isOpen && !!instance && hydrateDetail/.test(bodies.dialog),
    "the detail request is not gated by the explicit hydrateDetail opt-in",
  ],
  [
    /hydrateDetail=\{monitoringUiIsCurrent && !selectedListInstance && !!selectedTopConsumer\}/.test(
      bodies.monitoring,
    ),
    "Monitoring does not limit hydration to an incomplete top-consumer summary",
  ],
  [
    /isOpen && !!record && tab === "metrics" && pageVisible,/.test(bodies.dialog) &&
      !/tab === "metrics" && pageVisible && \w+\.status === "Running"/.test(bodies.dialog),
    "metrics must be gated by the open Metrics tab and visibility, never by a row status captured at click time",
  ],
  [
    /live:\s*5 \* 1000/.test(bodies.queryConfig) &&
      /live:\s*3 \* POLL_MS\.live/.test(bodies.queryConfig),
    "live metrics must poll at five seconds and become stale after three intervals",
  ],
  [
    /refetchInterval: metricsRefetchInterval/.test(bodies.hooks) &&
      /refetchIntervalInBackground:\s*false/.test(bodies.hooks) &&
      /!isMetricsAccessError\(query\.state\.error\)/.test(bodies.hooks) &&
      /backingOffInterval\(POLL_MS\.live/.test(bodies.polling) &&
      /status === 401 \|\| status === 403/.test(bodies.polling) &&
      /if \(isMetricsAccessError\(error\)\) return false;/.test(bodies.polling) &&
      /isMetricsRateLimited\(error\)[\s\S]{0,200}retryAfter/.test(bodies.polling) &&
      /data\.status !== "Running"\) return POLL_MS\.active;/.test(bodies.polling) &&
      /cpu_sample_status === "calculating"\) return METRICS_SECOND_SAMPLE_MS;/.test(bodies.polling),
    "the metrics query lost its refusal stop, Retry-After resume, backoff, stopped-instance, second-sample, or background guards",
  ],
  [
    /errorUpdatedAt - dataUpdatedAt/.test(bodies.queryConfig) &&
      !/state\.fetchFailureCount/.test(bodies.queryConfig),
    "backingOffInterval must grow with how long a query has failed; React Query resets fetchFailureCount on every fetch",
  ],
  [
    /queryKeys\.instanceMetrics\(scopeKey, host, instanceName, project\)/.test(bodies.hooks) &&
      /queryKeys\.instanceDetail\(scopeKey, host, effectiveProject, name\)/.test(bodies.hooks) &&
      /queryKeys\.vmsFor\(scopeKey, host, status\)/.test(bodies.hooks) &&
      /actorScope: requestActorScope/.test(bodies.monitoring) &&
      /project: requestProject/.test(bodies.monitoring) &&
      /user\?\.id \?\? user\?\.email/.test(bodies.queryScope) &&
      /user\?\.groups/.test(bodies.queryScope),
    "live Metrics, Top Consumer hydration, or its VM list is not isolated by actor scope",
  ],
  [
    !/queryKeys\.vmsFor\(|vmAPI\.list\(|\["vms",/.test(bodies.monitoring) &&
      /api\.instances\.list\([\s\S]{0,300}fresh: true,[\s\S]{0,80}signal: controller\.signal/.test(
        bodies.monitoring,
      ) &&
      /activeInstanceRequestRef\.current !== request/.test(bodies.monitoring) &&
      /epoch: \+\+instanceRequestEpochRef\.current/.test(bodies.monitoring) &&
      /activeInstanceRequestRef\.current\?\.controller\.abort\(\)/.test(bodies.monitoring),
    "Monitoring must bypass the stale legacy VM cache with a cancellable, authoritative actor/project-fenced read",
  ],
  [
    /queryKeys\.instancesFor\(scopeKey, host, opts\?\.project\)/.test(bodies.hooks) &&
      /createInstanceFreshnessCoordinator\(\{[\s\S]{0,300}actorScope: scopeKey/.test(
        bodies.stream,
      ) &&
      /reconcile: async \(\{ actorScope, fresh \}\)[\s\S]{0,180}reconcileInstanceFleet\(queryClient, actorScope/.test(
        bodies.stream,
      ) &&
      /queryKeys\.instancesFor\(actorScope, undefined\)/.test(bodies.sidebar) &&
      /queryKeys\.instancesFor\(scopeKey, hostId\)/.test(bodies.tenantAccess) &&
      /queryKeys\.instancesFor\(scopeKey, hostId\)/.test(bodies.policyManagement),
    "an instance-list producer or consumer is not isolated by actor scope",
  ],
  [
    /let active = true/.test(bodies.stream) &&
      /reconnectTimers\.forEach\(clearTimeout\)/.test(bodies.stream) &&
      /ipRetryTimers\.forEach\(clearTimeout\)/.test(bodies.stream) &&
      /conn\.ws\.onclose = null/.test(bodies.stream) &&
      /active = false/.test(bodies.stream),
    "the instance event stream can survive an actor change through a socket or retry timer",
  ],
  [
    !/queryKey:\s*\["instances",\s*(?:undefined|host|hostId)\]/.test(
      [bodies.stream, bodies.sidebar, bodies.tenantAccess, bodies.policyManagement].join("\n"),
    ),
    "a concrete instance-list query still uses an unscoped browser-cache key",
  ],
  [
    /previousActorScopeRef\.current === actorScope/.test(bodies.monitoring) &&
      /setInstanceDialogOpen\(false\)/.test(bodies.monitoring) &&
      /setAllInstances\(\[\]\)/.test(bodies.monitoring) &&
      /setSelectedTopConsumer\(null\)/.test(bodies.monitoring) &&
      /setSelectedListInstance\(null\)/.test(bodies.monitoring) &&
      /setSelectedNode\("all"\)/.test(bodies.monitoring) &&
      /setVisibleNodes\(\{\}\)/.test(bodies.monitoring) &&
      /setZoomLevel\(1\)/.test(bodies.monitoring) &&
      /open=\{monitoringUiIsCurrent && instanceDialogOpen\}/.test(bodies.monitoring) &&
      /activeActorScopeRef\.current !== requestActorScope/.test(bodies.monitoring),
    "Monitoring dialogs or node/chart selections can retain another actor's local state",
  ],
  [
    /key=\{`\$\{instance\.host\}:\$\{project\}:\$\{instance\.name\}`\}/.test(
      bodies.monitoring,
    ) &&
      /\{instance\.host\} \/ \{project\}/.test(bodies.monitoring) &&
      /project \$\{project\}`\}/.test(bodies.monitoring) &&
      /key=\{keyFor\(instance\.host, instance\.project \|\| "default", instance\.name\)\}/.test(
        bodies.instancesTable,
      ) &&
      /const rowLabel = `\$\{instance\.name\} in \$\{project\} on \$\{instance\.host\}`/.test(
        bodies.instanceRow,
      ),
    "same-named instances must keep project-qualified React and accessible identities",
  ],
  [
    /const actionUiIsCurrent = actionUiActorScope === scopeKey/.test(bodies.instanceActions) &&
      /deleteTarget: actionUiIsCurrent \? deleteTarget : null/.test(
        bodies.instanceActions,
      ) &&
      /const currentSelectedInstance = selectionIsCurrent \? selectedInstance : null/.test(
        bodies.instancesPage,
      ) &&
      /setDeleteTarget\(null\)/.test(bodies.instanceActions) &&
      /setRebuildTarget\(null\)/.test(bodies.instanceActions) &&
      /setSelectedInstance\(null\)/.test(bodies.instancesPage) &&
      /setConfirmAction\(\{[\s\S]{0,160}open: false/.test(bodies.instanceActions) &&
      /activeActorScopeRef\.current !== scopeKey/.test(bodies.instanceActions),
    "Instances action targets need render-time actor gates and transition cleanup",
  ],
  [
    /type MonitoringDisplaySnapshot/.test(bodies.monitoring) &&
      /isPaused && pausedSnapshot\?\.actorScope === actorScope/.test(bodies.monitoring) &&
      /const dashboardData = isPaused \? frozenSnapshot\?\.dashboard : liveDashboardData/.test(
        bodies.monitoring,
      ) &&
      /const historyData = isPaused \? frozenSnapshot\?\.history : liveHistoryData/.test(
        bodies.monitoring,
      ) &&
      /Promise\.allSettled/.test(bodies.monitoring) &&
      /setPausedSnapshot\(\{[\s\S]{0,700}dashboardState\?\.data \?\? previous\?\.dashboard/.test(
        bodies.monitoring,
      ) &&
      /cancelQueries\(\{[\s\S]{0,100}monitoring[\s\S]{0,100}dashboard/.test(
        bodies.monitoring,
      ),
    "Pause must freeze displayed Monitoring payloads and manual refresh must replace the snapshot",
  ],
  [
    /pendingByActor = new Map<string, PendingCreate\[\]>/.test(bodies.pendingCreates) &&
      /subscribePending\(actorScope: string, listener: Listener\)/.test(bodies.pendingCreates) &&
      /getPendingSnapshot\(actorScope: string\)/.test(bodies.pendingCreates) &&
      /addPendingCreate\(\s*actorScope: string,/.test(bodies.pendingCreates) &&
      /removePendingCreate\(actorScope: string, id: string\)/.test(bodies.pendingCreates),
    "the module-global pending-create registry is not partitioned by actor",
  ],
  [
    /subscribePending\(scopeKey, listener\)/.test(bodies.stream) &&
      /getPendingSnapshot\(scopeKey\)/.test(bodies.stream) &&
      /addPendingCreate\(submission\.fence\.actorScope, \{/.test(bodies.createForm) &&
      /resolvePendingCreate\(scopeKey, p\.id\)/.test(bodies.instancesPage) &&
      /removePendingCreate\(scopeKey, p\.id\)/.test(bodies.pendingRows),
    "a pending-create producer or consumer can cross actor scope",
  ],
  [
    /<LiveTimestamp/.test(bodies.metrics) &&
      /staleAfterMs=\{STALE_AFTER_MS\.live\}/.test(bodies.metrics) &&
      /<RefreshButton/.test(bodies.metrics) &&
      /Showing the last successful readings/.test(bodies.metrics),
    "the Metrics panel lost freshness, manual refresh, or last-good error feedback",
  ],
  [
    /refetchMetrics\(\{ throwOnError: true \}\)/.test(bodies.dialog),
    "manual metrics refresh must reject so RefreshButton can report a failed request",
  ],
  [
    /metrics && metricsUpdatedAt >= openedAt && metrics\.status !== record\?\.status/.test(
      bodies.dialog,
    ) &&
      /const updatedAt = metricsUpdatedAt \|\| null;/.test(bodies.dialog) &&
      /label="Check instance metrics again"/.test(bodies.metrics),
    "only a metrics reading taken since the dialog opened may override the row's runtime state, and freshness must use the browser's receive time",
  ],
  [
    /useLiveRecord<VMInfo>\(/.test(bodies.instancesPage) &&
      /instance=\{dialogInstance\}/.test(bodies.instancesPage),
    "the Instances page dialog must follow the live row, not the object captured at click time",
  ],
  [
    !/metricsSampleCount|trackedMetricsIdentity|lastMetricsTimestamp|isCpuCalculating/.test(
      bodies.dialog,
    ) &&
      /metrics\.cpu_sample_status === "calculating"/.test(bodies.metrics) &&
      /metrics\.cpu_sample_status === "ready"/.test(bodies.metrics),
    "CPU readiness must use the server contract rather than a local sample-count heuristic",
  ],
  [
    /cpu_sample_status: InstanceCpuSampleStatus/.test(bodies.types) &&
      /unavailable_sections: InstanceMetricSection\[\]/.test(bodies.types) &&
      /"ready"[\s\S]{0,100}"calculating"[\s\S]{0,100}"unavailable"[\s\S]{0,100}"not_applicable"/.test(
        bodies.types,
      ),
    "the frontend InstanceMetrics type must preserve the server readiness and availability contract",
  ],
  [
    /new Set\(metrics\.unavailable_sections\)/.test(bodies.metrics) &&
      /<PartialReadNotice>/.test(bodies.metrics) &&
      /formatSectionList\(unexplainedSections\)/.test(bodies.metrics) &&
      /cpu_effective_vcpus/.test(bodies.metrics) &&
      /<GuestAgentNotice status=\{metrics\.guest_agent\} \/>/.test(bodies.metrics) &&
      /allSectionsUnavailable/.test(bodies.metrics),
    "partial and fully unavailable metric sections must remain distinct and name the missing readings",
  ],
  [
    /const \{ isPartner \} = usePermissions\(\);/.test(bodies.metrics) &&
      /isPartner \? <DetailField label="PID" value=\{metrics\.pid\} \/> : null/.test(bodies.metrics),
    "the host PID is Partner and Admin detail; a Tenant's Metrics tab must not offer the field",
  ],
  [
    /memory_includes_cache\?: boolean;/.test(bodies.types) &&
      /instance\.memory_includes_cache === false \? "Memory in use" : "Memory, with page cache"/.test(
        bodies.consumers,
      ),
    "a Top Consumers memory figure must say whether it counts page cache, per row, as the server reports it",
  ],
  [
    /disk_io_reason\?: "guest_agent" \| "storage_driver" \| "not_reported" \| null;/.test(bodies.types) &&
      /Not measured for containers on \$\{formatLabel\(metrics\.disk_io_storage_driver\)\} storage/.test(
        bodies.metrics,
      ) &&
      /"Needs the guest agent"/.test(bodies.metrics) &&
      /"Not reported by the cluster"/.test(bodies.metrics) &&
      /Disk I\/O: \{ioMissing\}/.test(bodies.metrics),
    "unavailable disk I/O must say why (storage driver, guest agent, or not reported), never render a blank or a zero",
  ],
];

const failures = checks.filter(([passed]) => !passed).map(([, reason]) => reason);
if (failures.length > 0) {
  console.error(`instance live metrics: ${failures.length} failure(s)`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log(`instance live metrics: ${checks.length} contracts verified`);
