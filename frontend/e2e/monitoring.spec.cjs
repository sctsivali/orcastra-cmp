const { test, expect } = require("@playwright/test");
const {
  authenticate,
  capture,
  countRequests,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");

async function setDocumentVisibility(page, state) {
  await page.evaluate((nextState) => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => nextState,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}

async function expectDomOrder(page, locators) {
  const handles = [];
  for (const locator of locators) handles.push(await locator.elementHandle());
  expect(handles.every(Boolean)).toBe(true);
  const ordered = await page.evaluate(
    ([first, second, third]) =>
      Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) &&
      Boolean(second.compareDocumentPosition(third) & Node.DOCUMENT_POSITION_FOLLOWING),
    handles,
  );
  expect(ordered).toBe(true);
}

test("Monitoring pause freezes automation while manual refresh and resume still work", async ({ page }, testInfo) => {
  // Partner deliberately exercises REST fallback. Admin's monitoring WebSocket is a separate
  // transport contract; pause semantics must also hold when normal polling is active.
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });

  await page.goto("/inventory/monitoring");
  await expect(page.getByRole("heading", { level: 1, name: "Monitoring" })).toBeVisible();

  const refresh = page.getByRole("button", { name: "Refresh monitoring" });
  const pause = page.getByRole("button", { name: "Pause automatic monitoring updates" });
  const filter = page.getByRole("combobox", { name: "Filter by cluster" });
  await expect(refresh).toBeVisible();
  await expect(pause).toBeVisible();
  await expect(filter).toBeVisible();
  await expectDomOrder(page, [refresh, pause, filter]);

  await pause.hover();
  await expect(page.getByRole("tooltip")).toContainText("Workloads will keep running");

  // History is read for the selected time window only: no prefetch of the other ranges, and
  // never by point count, which assumed a cadence the collector does not have.
  await expect.poll(() => countRequests(api, "/monitoring/history"), { timeout: 8_000 }).toBeGreaterThan(0);
  const windows = new Set(
    api.requests
      .filter((request) => request.path === "/monitoring/history")
      .map((request) => new URLSearchParams(request.search).get("window")),
  );
  expect([...windows]).toEqual(["300"]);
  await expect(page.getByText("Real-time resource metrics across all clusters")).toHaveCount(0);
  await expect(page.getByText(/Updated /).first()).toBeVisible();
  await expect(page.getByText("2.8%", { exact: true })).toBeVisible();
  await expect(page.getByText("0.9 of 32 CPU threads busy")).toBeVisible();

  await pause.click();
  const resume = page.getByRole("button", { name: "Resume automatic monitoring updates" });
  await expect(resume).toHaveAttribute("aria-pressed", "true");
  await page.waitForTimeout(250);
  const pausedDashboardCount = countRequests(api, "/monitoring/dashboard");
  const pausedHistoryCount = countRequests(api, "/monitoring/history");
  await page.waitForTimeout(5_500);
  expect(countRequests(api, "/monitoring/dashboard")).toBe(pausedDashboardCount);
  expect(countRequests(api, "/monitoring/history")).toBe(pausedHistoryCount);

  // A person can still ask for one fresh snapshot while automation remains paused.
  await refresh.click();
  await expect
    .poll(() => countRequests(api, "/monitoring/dashboard"), { timeout: 8_000 })
    .toBeGreaterThan(pausedDashboardCount);
  await expect(resume).toBeVisible();
  expect(
    api.requests.some(
      (request) => request.path === "/monitoring/dashboard" && request.search.includes("fresh=true"),
    ),
  ).toBe(true);

  const manualDashboardCount = countRequests(api, "/monitoring/dashboard");
  await resume.click();
  await expect(page.getByRole("button", { name: "Pause automatic monitoring updates" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await expect
    .poll(() => countRequests(api, "/monitoring/dashboard"), { timeout: 8_000 })
    .toBeGreaterThan(manualDashboardCount);

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "monitoring-controls-and-pause");
  await expectCleanHarness(api);
});

test("Admin pause closes the monitoring WebSocket and resume opens a new stream", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "The transport lifecycle only needs one browser profile");

  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { monitoringInstanceList: true, monitoringWebSocket: true });

  await page.goto("/inventory/monitoring");
  await expect(page.getByRole("heading", { level: 1, name: "Monitoring" })).toBeVisible();
  await expect.poll(() => api.monitoringSockets.length, { timeout: 8_000 }).toBe(1);
  expect(api.monitoringSockets[0].closedAt).toBeNull();

  const pause = page.getByRole("button", { name: "Pause automatic monitoring updates" });
  await pause.click();
  const resume = page.getByRole("button", { name: "Resume automatic monitoring updates" });
  await expect(resume).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => api.monitoringSockets[0].closedAt, { timeout: 8_000 }).not.toBeNull();

  // The hook's reconnect delay starts at two seconds. Waiting past it proves Pause cleared the
  // reconnect path instead of merely closing one socket and immediately replacing it.
  const pausedDashboardCount = countRequests(api, "/monitoring/dashboard");
  await page.waitForTimeout(2_500);
  expect(api.monitoringSockets).toHaveLength(1);
  expect(countRequests(api, "/monitoring/dashboard")).toBe(pausedDashboardCount);

  await page.getByRole("button", { name: "Refresh monitoring" }).click();
  await expect
    .poll(() => countRequests(api, "/monitoring/dashboard"), { timeout: 8_000 })
    .toBeGreaterThan(pausedDashboardCount);
  expect(
    api.requests.some(
      (request) => request.path === "/monitoring/dashboard" && request.search.includes("fresh=true"),
    ),
  ).toBe(true);
  expect(api.monitoringSockets).toHaveLength(1);

  await resume.click();
  await expect.poll(() => api.monitoringSockets.length, { timeout: 8_000 }).toBe(2);
  expect(api.monitoringSockets[0].closedAt).not.toBeNull();
  expect(api.monitoringSockets[1].closedAt).toBeNull();
  await expect(page.getByRole("button", { name: "Pause automatic monitoring updates" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );

  await expectCleanHarness(api);
});

// The page reads the fleet instance list itself (2026-11-03), so a consumer row opens from the
// full record in that list instead of waiting for a detail read.
function instanceListAnswered(page) {
  return page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/api/v1/instances"));
}

test("Top Consumer opens from the cached record and live Metrics preserve the last good sample", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });

  const listed = instanceListAnswered(page);
  await page.goto("/inventory/monitoring");
  await listed;
  const consumer = page.getByRole("button", {
    name: "Open resource details for tenant-owned-vm on cluster-a in project tenant-a",
  });
  await expect(consumer).toBeVisible();
  await consumer.click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "tenant-owned-vm" })).toBeVisible();
  await expect(dialog.getByRole("tab", { name: "Overview" })).toBeVisible();
  expect(api.detailCalls, "the cached list record needs no detail read").toBe(0);
  await expect(dialog.getByRole("tab", { name: "Overview" })).toHaveAttribute("data-state", "active");
  await expect(dialog.getByText("Resources", { exact: true })).toHaveCount(0);
  expect(api.metricsCalls).toHaveLength(0);

  await dialog.getByRole("tab", { name: "Metrics" }).click();
  await expect.poll(() => api.metricsCalls.length).toBe(1);
  await expect(dialog.getByText("Calculating…", { exact: true })).toBeVisible();
  // The fixture's server clock is 20 s behind; the age comes from when the reading arrived.
  await expect(dialog.getByText(/These readings are stale/)).toHaveCount(0);

  // Browser visibility is part of the activation predicate. A hidden document gets no poll.
  await setDocumentVisibility(page, "hidden");
  const hiddenCount = api.metricsCalls.length;
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(hiddenCount);

  await setDocumentVisibility(page, "visible");
  await expect.poll(() => api.metricsCalls.length, { timeout: 8_000 }).toBeGreaterThan(hiddenCount);
  await expect(dialog.getByText("22.5%", { exact: true })).toBeVisible();

  // Once visible, the next successful read follows the five-second live cadence.
  const beforeCadence = api.metricsCalls.length;
  const previousAt = api.metricsCalls.at(-1).at;
  await expect.poll(() => api.metricsCalls.length, { timeout: 8_000 }).toBeGreaterThan(beforeCadence);
  const cadenceMs = api.metricsCalls.at(-1).at - previousAt;
  expect(cadenceMs).toBeGreaterThanOrEqual(4_500);
  expect(cadenceMs).toBeLessThan(7_500);

  // A 503 leaves the successful sample visible and explains why it is stale.
  api.metricsMode = "unavailable";
  await dialog.getByRole("button", { name: "Refresh instance metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Cluster did not answer" })).toBeVisible({
    timeout: 8_000,
  });
  await expect(dialog.getByText("22.5%", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Showing the last successful readings.")).toBeVisible();

  // A rate limit waits for its Retry-After (30 s here) before polling resumes on its own, and a
  // permission failure stops the loop for good. Neither may keep probing within the wait.
  api.metricsMode = "limited";
  await dialog.getByRole("button", { name: "Refresh instance metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Metrics temporarily limited" })).toBeVisible({
    timeout: 8_000,
  });
  const limitedCount = api.metricsCalls.length;
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(limitedCount);

  api.metricsMode = "forbidden";
  await dialog.getByRole("button", { name: "Refresh instance metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Metrics access denied" })).toBeVisible({
    timeout: 8_000,
  });
  const terminalCount = api.metricsCalls.length;
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(terminalCount);

  await capture(page, testInfo, "monitoring-instance-live-metrics");
  await expectCleanHarness(api);
});

test("Top Consumer hydrates once when the instance list has not answered", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: "unavailable" });

  await page.goto("/inventory/monitoring");
  await page
    .getByRole("button", {
      name: "Open resource details for tenant-owned-vm on cluster-a in project tenant-a",
    })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "tenant-owned-vm" })).toBeVisible();
  await expect.poll(() => api.detailCalls).toBe(1);
  await expect(dialog.getByRole("tab", { name: "Overview" })).toHaveAttribute("data-state", "active");
  await expectCleanHarness(api);
});

test("stopped Top Consumer checks once, then only at the slow cadence", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true, instanceStatus: "Stopped" });

  const listed = instanceListAnswered(page);
  await page.goto("/inventory/monitoring");
  await listed;
  await page
    .getByRole("button", {
      name: "Open resource details for tenant-owned-vm on cluster-a in project tenant-a",
    })
    .click();

  const dialog = page.getByRole("dialog");
  const dialogHeading = dialog.getByRole("heading", { name: "tenant-owned-vm" });
  await expect(dialogHeading).toBeVisible();
  expect(api.detailCalls, "the cached list record carries the state").toBe(0);
  await expect(
    dialogHeading.locator("xpath=..").getByText("Stopped", { exact: true }),
  ).toBeVisible();
  // One read confirms the runtime state; while stopped, the next check is 30 s away, so a
  // start is still noticed without a five-second loop against a stopped instance.
  await dialog.getByRole("tab", { name: "Metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Instance is not running" })).toBeVisible();
  await expect.poll(() => api.metricsCalls.length, { timeout: 8_000 }).toBe(1);
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(1);
  await expect(dialog.getByRole("button", { name: "Check instance metrics again" })).toBeVisible();

  await capture(page, testInfo, "monitoring-stopped-instance-metrics");
  await expectCleanHarness(api);
});

// A Tenant monitors its own instances and the clusters' status only (owner decision
// 2026-10-26). The backend sends every host figure as null and refuses it the host history, so
// the page shows no utilization gauge, no history chart, no warnings tile and no host figure on
// a cluster card, and never asks for the history.
test("tenant: Monitoring shows its instances and cluster status, no host figure", async ({ page }) => {
  const { monitoringDashboard } = require("./support/api-fixture-data.cjs");
  const HOST_FIGURES = [
    "total_cpu", "used_cpu", "cpu_usage_percent", "realtime_cpu_percent", "cpu_busy_threads",
    "cpu_busy_percent", "cpu_reporting_threads", "total_memory_bytes", "used_memory_bytes",
    "memory_usage_percent", "realtime_memory_bytes", "realtime_memory_percent",
    "total_storage_bytes", "used_storage_bytes", "storage_usage_percent", "total_gpu",
    "total_gpu_cards", "used_gpu",
  ];
  const KEPT = new Set([
    "node_name", "is_online", "cluster_status", "collection_timed_out", "cpu_reporting_instances",
    "instance_count", "running_instances", "container_count", "vm_count",
  ]);
  const tenantBody = () => {
    const full = monitoringDashboard();
    const body = { ...full, cpu_status: "unavailable", storage_pools: [], warnings: null };
    for (const field of HOST_FIGURES) body[field] = null;
    body.nodes = full.nodes.map((node) => {
      const row = { cpu_status: "unavailable", gpu_models: [] };
      for (const [key, value] of Object.entries(node)) {
        if (KEPT.has(key)) row[key] = value;
        else if (!(key in row)) row[key] = null;
      }
      return row;
    });
    return body;
  };

  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { monitoringInstanceList: true, monitoringDashboard: tenantBody });
  await page.goto("/inventory/monitoring");
  const main = page.getByRole("main");
  await expect(main.getByText("Total Instances")).toBeVisible();
  await expect(main.getByText("Clusters Online")).toBeVisible();
  await expect(main.getByText("Cluster Status")).toBeVisible();
  await expect(main).toContainText("Your instances and the status of the clusters they run on");
  await expect(main).toContainText("Status of each cluster and your instances on it");

  for (const absent of ["Resource Utilization", "Resource Metrics", "Warnings"]) {
    await expect(main.getByText(absent, { exact: true })).toHaveCount(0);
  }
  await expect(main).not.toContainText(/CPU threads|vCPU Allocation|GB \/|NaN|undefined/);
  await expect(main.getByText("Running", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "Refresh monitoring" }).click();
  await page.waitForTimeout(1_500);
  expect(countRequests(api, "/monitoring/history"), "a tenant never asks for the host history").toBe(0);
  await expectNoHorizontalOverflow(page);
  await expectCleanHarness(api);
});

// A container without limits.cpu or limits.memory is measured against its host, so "of 64
// vCPUs" and "/ 62 GiB" read as an allocation the instance was never given. The readings say
// whether the limits are set, and every surface says "no limit" instead.
test("an instance without limits says so instead of showing the host's size", async ({ page }) => {
  const { instanceMetrics, monitoringDashboard } = require("./support/api-fixture-data.cjs");
  const gib = 1024 ** 3;
  const unlimitedDashboard = () => {
    const body = monitoringDashboard();
    const mark = (row) => ({ ...row, cpu_vcpus: 64, cpu_limit_set: false, memory_limit_set: false });
    return {
      ...body,
      consumers: (body.consumers || []).map(mark),
      top_cpu_consumers: (body.top_cpu_consumers || []).map(mark),
      top_memory_consumers: (body.top_memory_consumers || []).map(mark),
    };
  };
  const unlimitedMetrics = (call, status) => ({
    ...instanceMetrics(call === 1 ? 2 : call, status),
    cpu_effective_vcpus: 64,
    cpu_busy_vcpus: 0.9,
    cpu_usage_percent: 1.4,
    memory_used_bytes: 2 * gib,
    memory_total_bytes: 62 * gib,
    memory_usage_percent: 3.2,
    cpu_limit_set: false,
    memory_limit_set: false,
  });

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true,
    monitoringDashboard: unlimitedDashboard,
    instanceMetrics: unlimitedMetrics,
  });
  await page.goto("/inventory/monitoring");
  const consumer = page.getByRole("button", {
    name: "Open resource details for tenant-owned-vm on cluster-a in project tenant-a",
  });
  await expect(consumer).toBeVisible();
  await expect(consumer).toContainText("no vCPU limit");
  await expect(consumer).not.toContainText(/of 64 vCPUs/);

  await consumer.click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("tab", { name: "Metrics" }).click();
  await expect(dialog.getByText(/vCPUs? busy · no vCPU limit/)).toBeVisible({ timeout: 10_000 });
  await expect(dialog.getByText(/2\.15 GB used, no memory limit set/)).toBeVisible();
  await expect(dialog).not.toContainText(/of 64 vCPUs|66\.57 GB/);
  await expectCleanHarness(api);
});

// ---------------------------------------------------------------------------------------------
// Gauges, pools, names and alarms (2026-10-29)
// ---------------------------------------------------------------------------------------------

const GIB = 1024 ** 3;

// An offline row as GET /monitoring/dashboard sends it: totals zero, shares null, never 0%.
function offlineNode(name) {
  return {
    node_name: name, node_type: "LXD", is_online: false, cluster_status: "offline",
    status_message: "Cannot connect to LXD endpoint.", collection_timed_out: false,
    cpu_total: 0, cpu_used: 0, cpu_usage_percent: 0, cpu_allocated: 0, cpu_allocation_ratio: 0,
    cpu_overcommitted: false, cpu_busy_threads: null, cpu_busy_percent: null,
    cpu_status: "unavailable", cpu_reporting_instances: 0,
    memory_total_bytes: 0, memory_used_bytes: 0, memory_usage_percent: null,
    storage_total_bytes: 0, storage_used_bytes: 0, storage_usage_percent: null,
    gpu_total: 0, gpu_used: 0, gpu_available: 0, gpu_cards_total: 0, gpu_integrated_total: 0,
    gpu_models: [], gpu_utilization_supported: false,
    instance_count: 0, running_instances: 0, container_count: 0, vm_count: 0,
  };
}

// Every cluster offline. The memory and storage gauges used to draw 0.0% over "0 B / 0 B" here
// while the CPU gauge said Unavailable.
function nothingReported() {
  const { monitoringDashboard } = require("./support/api-fixture-data.cjs");
  return {
    ...monitoringDashboard(),
    online_nodes: 0, inactive_nodes: 1, clusters_reporting: 0, clusters_total: 1,
    total_instances: 0, running_instances: 0, stopped_instances: 0, container_count: 0, vm_count: 0,
    total_cpu: 0, used_cpu: 0, cpu_usage_percent: 0, realtime_cpu_percent: 0,
    cpu_busy_threads: null, cpu_busy_percent: null, cpu_status: "unavailable",
    cpu_reporting_instances: 0, cpu_reporting_threads: 0,
    total_memory_bytes: 0, used_memory_bytes: 0, memory_usage_percent: null,
    realtime_memory_bytes: 0, realtime_memory_percent: 0,
    total_storage_bytes: 0, used_storage_bytes: 0, storage_usage_percent: null,
    total_gpu: 0, total_gpu_cards: 0, used_gpu: 0,
    nodes: [offlineNode("cluster-a")],
    storage_pools: [], consumers: [], top_cpu_consumers: [], top_memory_consumers: [],
  };
}

// cluster-a reports; cluster-unreachable ("Edge Cluster" in the availability fixture) does not.
function oneOfTwoReporting() {
  const { monitoringDashboard } = require("./support/api-fixture-data.cjs");
  const body = monitoringDashboard();
  return {
    ...body,
    total_nodes: 2, inactive_nodes: 1, clusters_total: 2,
    nodes: [...body.nodes, offlineNode("cluster-unreachable")],
  };
}

test("no cluster reporting: every gauge says Unavailable under one caption", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true, monitoringDashboard: nothingReported });
  await page.goto("/inventory/monitoring");

  const card = page.locator('[data-slot="resource-utilization"]');
  await expect(card).toBeVisible();
  // CPU, memory, storage and GPU alike; the allocation line goes with the reading.
  await expect(card.getByText("Unavailable", { exact: true })).toHaveCount(4);
  await expect(card.getByText("No cluster reported usage", { exact: true })).toHaveCount(4);
  await expect(card).not.toContainText(/\d%|0 B \/ 0 B|Allocated:|No GPU on/);

  // The pools card stays and says why it is empty, so the page keeps its shape.
  const pools = page.locator('[data-slot="storage-pools"]');
  await expect(pools.getByText("No storage pools", { exact: true })).toBeVisible();
  await expect(pools.getByText("No cluster reported a storage pool.")).toBeVisible();
  await expect(pools.getByRole("button", { name: /Highest|Lowest/ })).toHaveCount(0);

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "monitoring-nothing-reported");
  await expectCleanHarness(api);
});

test("partial reporting says how many clusters, and a picked offline cluster says it is offline", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true, monitoringDashboard: oneOfTwoReporting });
  await page.goto("/inventory/monitoring");

  const card = page.locator('[data-slot="resource-utilization"]');
  await expect(card.getByText("From 1 of 2 clusters", { exact: true })).toBeVisible();
  await expect(card.getByText("2.8%", { exact: true })).toBeVisible();
  await expect(card.getByText("Unavailable", { exact: true })).toHaveCount(0);

  await page.getByRole("combobox", { name: "Filter by cluster" }).click();
  await page.getByRole("option", { name: /Edge Cluster/ }).click();
  await expect(card.getByText("Usage and allocation on Edge Cluster", { exact: true })).toBeVisible();
  await expect(card.getByText("Cluster offline", { exact: true })).toHaveCount(4);
  await expect(card.getByText("Unavailable", { exact: true })).toHaveCount(4);
  await expect(card.getByText("From 1 of 2 clusters")).toHaveCount(0);
  await expect(card).not.toContainText(/Allocated:|0 B \/ 0 B/);
  await expect(page.locator('[data-slot="storage-pools"]').getByText("Edge Cluster reported no storage pool.")).toBeVisible();

  await expectNoHorizontalOverflow(page);
  await expectCleanHarness(api);
});

test("clusters and pools read by display name, and pool members only when there are several", async ({ page }, testInfo) => {
  const { monitoringDashboard } = require("./support/api-fixture-data.cjs");
  const withSharedPool = () => {
    const body = monitoringDashboard();
    return {
      ...body,
      storage_pools: [
        ...body.storage_pools,
        {
          name: "remote", node: "cluster-a", members: ["micro1", "micro2", "micro3"], scope: "shared",
          driver: "ceph", total_bytes: 400 * GIB, used_bytes: 100 * GIB, available_bytes: 300 * GIB,
          usage_percent: 25,
        },
      ],
    };
  };
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true, monitoringDashboard: withSharedPool });
  await page.goto("/inventory/monitoring");
  const main = page.getByRole("main");

  // The cluster card leads with the name the Clusters page shows, the id beneath it.
  await expect(main.getByText("Core Cluster", { exact: true }).first()).toBeVisible();
  await expect(main.getByText("cluster-a", { exact: true }).first()).toBeVisible();

  const pools = page.locator('[data-slot="storage-pools"]');
  await expect(pools.getByText("fast-pool", { exact: true })).toBeVisible();
  await expect(pools.locator('[data-slot="badge"]', { hasText: /^ZFS$/ })).toBeVisible();
  await expect(pools.getByText("Core Cluster", { exact: true })).toHaveCount(2);
  // A standalone host lists itself as its one member: nothing to add beside its own name.
  await expect(pools).not.toContainText(/\(cluster-a|1 members/);
  const members = pools.getByRole("button", { name: "Show the 3 members holding remote on Core Cluster" });
  await expect(members).toHaveText("shared by 3 members");
  await members.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Members holding remote" })).toBeVisible();
  await expect(dialog.getByText("micro2", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  // The picker names the cluster the same way, with the id as its second line.
  await page.getByRole("combobox", { name: "Filter by cluster" }).click();
  await expect(page.getByRole("option", { name: /Core Cluster/ })).toContainText("cluster-a");
  await page.keyboard.press("Escape");

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "monitoring-names-and-pools");
  await expectCleanHarness(api);
});

test("alarm tiles speak the Warnings page's words and stay neutral at zero", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });
  await page.goto("/inventory/monitoring");

  const alarms = page.locator('[data-slot="alarm-statistics"]');
  for (const label of ["Critical", "High", "Warning", "Info"]) {
    const tile = alarms.getByRole("link", { name: new RegExp(`^0\\s*${label}$`) });
    await expect(tile).toBeVisible();
    // the number is the first <p> in the tile, which the live harness reads
    await expect(tile.locator("p").first()).toHaveText("0");
  }
  await expect(alarms).not.toContainText(/Major|Inactive|All systems operational|✓/);
  await expect(alarms.getByText("No active alarms", { exact: true })).toBeVisible();
  await expect(alarms.getByRole("link", { name: /View All/ })).toHaveCount(0);
  await expectCleanHarness(api);
});

test("Monitoring counts kernel findings in View All, as the dashboard does", async ({ page }, testInfo) => {
  const { kernelWarningRows } = require("./support/kernel-advisory-fixtures.cjs");
  const kernels = kernelWarningRows("exposed").length;
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true, kernel: "exposed" });
  await page.goto("/inventory/monitoring");

  const alarms = page.locator('[data-slot="alarm-statistics"]');
  const tile = alarms.getByRole("link", { name: new RegExp(`^${kernels}\\s*Kernel CVEs$`) });
  await expect(tile).toHaveAttribute("href", "/inventory/warnings?tab=kernel");
  await expect(alarms.getByRole("link", { name: `View All (${kernels})` })).toBeVisible();
  await expect(alarms.getByText("No active alarms")).toHaveCount(0);
  await capture(page, testInfo, "monitoring-alarms-kernel");
  await expectCleanHarness(api);
});

// The Instance Distribution list (2026-11-03): it reads the list the page already holds, never a
// forced fleet read on open, follows what it was opened for, and Refresh is the one live read.
function freshListReads(api) {
  return api.requests.filter((r) => r.path === "/instances" && /(^|[?&])fresh=true/.test(r.search)).length;
}

test("Instance Distribution opens from the page's list and refreshes only on demand", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });

  const listed = instanceListAnswered(page);
  await page.goto("/inventory/monitoring");
  await listed;
  await page.getByRole("button", { name: /^Running\s*1$/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Running Instances" })).toBeVisible();
  await expect(dialog.getByText("1 instance found", { exact: true })).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Show details for tenant-owned-vm on cluster-a in project tenant-a" }),
  ).toBeVisible();
  expect(freshListReads(api), "opening forces no fleet read").toBe(0);
  await expect(page.getByRole("tooltip")).toHaveCount(0);

  await dialog.getByRole("button", { name: "Refresh instances" }).click();
  await expect.poll(() => freshListReads(api)).toBe(1);

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await page.getByRole("button", { name: /^0\s*Containers$/ }).click();
  await expect(dialog.getByRole("heading", { name: "Containers" })).toBeVisible();
  await expect(dialog.getByText("0 instances found", { exact: true })).toBeVisible();
  await expect(dialog.getByText("No instances found", { exact: true })).toBeVisible();
  expect(freshListReads(api)).toBe(1);
  await expectCleanHarness(api);
});

test("Instance Distribution says a failed read failed, and Retry recovers", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: "unavailable" });

  await page.goto("/inventory/monitoring");
  await page.getByRole("button", { name: /^Running\s*1$/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Could not load instances" })).toBeVisible({ timeout: 15_000 });
  await expect(dialog.getByText(/instances? found/)).toHaveCount(0);
  await expect(dialog.getByText("No instances found", { exact: true })).toHaveCount(0);

  api.monitoringInstanceList = true;
  await dialog.getByRole("button", { name: "Retry" }).click();
  await expect(
    dialog.getByRole("button", { name: "Show details for tenant-owned-vm on cluster-a in project tenant-a" }),
  ).toBeVisible();
  await expect(dialog.getByText("1 instance found", { exact: true })).toBeVisible();
  await expectCleanHarness(api);
});
