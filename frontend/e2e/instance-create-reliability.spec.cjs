const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  installApiFixtures,
} = require("./support/fixtures.cjs");
const { fullInstance } = require("./support/api-fixture-data.cjs");

const fingerprint = "b".repeat(64);

function json(route, body, status = 200, headers = {}) {
  return route.fulfill({
    status,
    headers,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function actorPendingStorageKey(role) {
  const scope = `${role}@e2e.invalid:role_${role}`;
  let hash = 2166136261;
  for (let index = 0; index < scope.length; index += 1) {
    hash ^= scope.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `orcastra.pending-creates.v1.${(hash >>> 0).toString(16)}`;
}

async function seedPendingJob(page, role, entry) {
  await page.addInitScript(({ key, value }) => {
    if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, JSON.stringify([value]));
  }, { key: actorPendingStorageKey(role), value: entry });
}

function createJob(entry, status, overrides = {}) {
  return {
    job_id: entry.jobId,
    kind: "create",
    instance_name: entry.name,
    host: entry.host,
    project: entry.project,
    phase: status,
    status,
    operation_id: "create-operation",
    operation_ids: ["create-operation", "start-operation"],
    error: null,
    outcome: ["success", "failure", "partial"].includes(status) ? status : null,
    result: null,
    verified_readback: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

function instanceInventory(vms) {
  return {
    total: vms.length,
    vms,
    nodes: ["cluster-a"],
    lxd_count: vms.length,
    microcloud_count: 0,
    incus_count: 0,
    offset: 0,
    limit: 100,
    has_more: false,
    unreachable_nodes: [],
  };
}

test("NETWORKLESS acceptance tracks the exact create job across reload", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const creates = [];
  const preflights = [];
  const tagWrites = [];
  let operationReads = 0;

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "") || "/";
    const method = request.method();

    if (method === "GET" && path === "/nodes/names") {
      await json(route, ["cluster-a"]);
      return;
    }
    if (method === "GET" && path === "/nodes/availability") {
      await json(route, {
        clusters: [{
          name: "cluster-a",
          display_name: "Cluster A",
          endpoint: "https://cluster-a.invalid:8443",
          status: "online",
          reason_code: "ok",
          reason: null,
          node_type: "lxd",
          checked_at: new Date().toISOString(),
          available: true,
        }],
        online_count: 1,
        total_count: 1,
        source: "availability_cache",
        generated_at: new Date().toISOString(),
      });
      return;
    }
    if (method === "GET" && path === "/projects/cluster-a") {
      await json(route, {
        host: "cluster-a",
        projects: [{
          name: "tenant-a",
          description: "Tenant A",
          config: {},
          used_by: [],
          host: "cluster-a",
          tags: [],
        }],
        total: 1,
        reachable: true,
        error: null,
      });
      return;
    }
    if (method === "GET" && path === "/storage/pools") {
      await json(route, {
        total: 1,
        pools: [{
          name: "default",
          description: "",
          driver: "dir",
          config: {},
          used_by: [],
          status: "Created",
          node: "cluster-a",
        }],
        nodes: ["cluster-a"],
        unreachable_nodes: [],
      });
      return;
    }
    if (method === "GET" && path === "/storage/volumes") {
      await json(route, { total: 0, volumes: [], nodes: ["cluster-a"], unreachable_nodes: [] });
      return;
    }
    if (method === "GET" && path === "/networks") {
      await json(route, { total: 0, networks: [], nodes: ["cluster-a"], unreachable_nodes: [] });
      return;
    }
    if (method === "GET" && path === "/profiles/default") {
      await json(route, {
        name: "default",
        description: "",
        config: {},
        devices: { root: { type: "disk", path: "/", pool: "default" } },
        used_by: [],
        node: "cluster-a",
        project: "tenant-a",
        etag: '"profile-v1"',
      });
      return;
    }
    if (method === "GET" && path === "/nodes/cluster-a/gpus") {
      await json(route, { host: "cluster-a", gpus: [], total: 0, compute_total: 0 });
      return;
    }
    if (method === "GET" && path === "/images/cluster-a") {
      await json(route, {
        host: "cluster-a",
        project: "tenant-a",
        total: 1,
        images: [{
          fingerprint,
          aliases: ["ubuntu-24.04"],
          description: "Ubuntu 24.04",
          architecture: "x86_64",
          size: 1024,
          upload_date: null,
          public: false,
          type: "virtual-machine",
          cached: true,
          auto_update: false,
          created_at: null,
          last_used_at: null,
          expires_at: null,
          profiles: [],
          project: "tenant-a",
        }],
      });
      return;
    }
    if (method === "GET" && path === "/images/remote") {
      await json(route, { images: [], total: 0 });
      return;
    }
    if (method === "POST" && path === "/instances/preflight") {
      preflights.push(request.postDataJSON());
      if (preflights.length === 1) {
        await json(route, {
          ready: false,
          plan_revision: "blocked-create-plan",
          blockers: [{
            code: "DUPLICATE_NAME",
            field: "name",
            message: "An instance with this name already exists in the selected project.",
            acknowledgement_required: false,
          }],
          warnings: [],
          required_acknowledgements: [],
          effective: { profiles: ["default"], nic_names: [], root_device: "root" },
        });
        return;
      }
      await json(route, {
        ready: true,
        plan_revision: "create-plan-v1",
        blockers: [],
        warnings: [{
          code: "NETWORKLESS",
          field: "devices",
          message: "No effective network interface is configured.",
          acknowledgement_required: true,
        }],
        required_acknowledgements: ["networkless"],
        effective: { profiles: ["default"], nic_names: [], root_device: "root" },
      });
      return;
    }
    if (method === "POST" && path === "/instances") {
      creates.push({
        body: request.postDataJSON(),
        idempotencyKey: request.headers()["idempotency-key"],
      });
      if (creates.length === 1) {
        await json(route, { detail: "Upstream result is not yet available" }, 503);
        return;
      }
      await json(route, {
        success: true,
        message: "Create accepted",
        vm: null,
        operation_id: "create-daemon-operation",
        job_id: "create-job-networkless",
        status: "accepted",
      }, 202);
      return;
    }
    if (method === "PUT" && path === "/tags/instances/cluster-a/networkless-vm") {
      tagWrites.push({ body: request.postDataJSON(), project: url.searchParams.get("project") });
      await json(route, { detail: "The create flow must not perform a second tag write" }, 500);
      return;
    }
    if (method === "GET" && path === "/instances/operations/create-job-networkless") {
      operationReads += 1;
      await json(route, {
        job_id: "create-job-networkless",
        kind: "create",
        instance_name: "networkless-vm",
        host: "cluster-a",
        project: "tenant-a",
        phase: "verifying",
        status: "running",
        operation_id: "create-daemon-operation",
        operation_ids: ["create-daemon-operation"],
        error: null,
        outcome: null,
        result: null,
        verified_readback: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
      return;
    }
    await route.fallback();
  });

  await page.goto(
    `/inventory/instances/create?node=cluster-a&type=vm&source=image&image=${fingerprint}&project=tenant-a`,
  );
  const name = page.getByLabel("Name *");
  await name.fill("duplicate-vm");
  await page.getByLabel("Tags").fill("original-tag");
  await page.getByLabel("Tags").press("Enter");
  await expect(page.getByRole("button", { name: "Create & Start" })).toBeEnabled();
  await page.getByRole("button", { name: "Create & Start" }).click();
  await expect(page.locator("#create-name-error")).toHaveText(
    "An instance with this name already exists in the selected project.",
  );
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await expect(name).toBeFocused();
  expect(creates).toHaveLength(0);
  await name.fill("networkless-vm");
  await expect(name).not.toHaveAttribute("aria-invalid", "true");
  await page.getByRole("button", { name: "Create & Start" }).click();

  const warning = page.getByRole("alertdialog", { name: "Create without network access?" });
  await expect(warning).toBeVisible();
  await warning.getByRole("button", { name: "Create isolated instance" }).click();
  await expect(page.getByRole("button", { name: "Retry same request" })).toBeVisible();

  await page.getByLabel("Name *").fill("edited-after-timeout");
  await page.getByRole("button", { name: "Remove tag original-tag" }).click();
  await page.getByLabel("Tags").fill("edited-tag");
  await page.getByLabel("Tags").press("Enter");
  await page.getByRole("button", { name: "Retry same request" }).click();

  await expect(page).toHaveURL(/\/inventory\/instances$/, { timeout: 2_000 });
  await expect(page.getByText("networkless-vm", { exact: true })).toBeVisible();
  await expect(page.getByText("edited-after-timeout", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Verifying created instance")).toBeVisible();

  expect(preflights).toHaveLength(2);
  expect(creates).toHaveLength(2);
  expect(creates[0].idempotencyKey).toBeTruthy();
  expect(creates[1]).toEqual(creates[0]);
  expect(creates[0].body).toMatchObject({
    name: "networkless-vm",
    plan_revision: "create-plan-v1",
    acknowledgements: ["networkless"],
    config: { "user.tags": "original-tag" },
  });
  expect(tagWrites).toEqual([]);
  await page.evaluate(() => {
    const pair = Object.entries(sessionStorage).find(([key]) =>
      key.startsWith("orcastra.pending-creates.v1."),
    );
    if (!pair) throw new Error("pending create identity was not persisted");
    const entries = JSON.parse(pair[1]);
    entries[0].createdAt = Date.now() - (11 * 60 * 1000);
    sessionStorage.setItem(pair[0], JSON.stringify(entries));
  });
  const readsBeforeReload = operationReads;
  await page.reload();
  await expect(page.getByText("networkless-vm", { exact: true })).toBeVisible();
  await expect.poll(() => operationReads).toBeGreaterThan(readsBeforeReload);
  await expect(page.getByText("No longer reporting")).toHaveCount(0);
  await expect(page.getByText("Verifying created instance")).toBeVisible();
  const pendingStorage = await page.evaluate(() =>
    Object.entries(sessionStorage).find(([key]) => key.startsWith("orcastra.pending-creates.v1.")) ?? null,
  );
  expect(pendingStorage).not.toBeNull();
  expect(pendingStorage[1]).toContain("create-job-networkless");
  expect(pendingStorage[1]).not.toContain("cloud_init");
  expect(pendingStorage[1]).not.toContain("devices");
  await expectCleanHarness(api);
});

test("an async create failure keeps Review configuration routed to the original scope", async ({ page }) => {
  await authenticate(page.context(), "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const entry = {
    id: "cluster-a/tenant-a/failed-create",
    name: "failed-create",
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    jobId: "failed-create-job",
    createdAt: Date.now(),
  };
  await seedPendingJob(page, "admin", entry);
  await page.route("**/api/v1/instances/operations/failed-create-job", async (route) => {
    await json(route, createJob(entry, "failure", { error: "The daemon rejected the image." }));
  });

  await page.goto("/inventory/instances");
  const failedJobRow = page.locator('tr[data-state="pending"]').filter({
    has: page.getByText("failed-create", { exact: true }),
  });
  await expect(failedJobRow).toHaveCount(1);
  await expect(failedJobRow.getByText("The daemon rejected the image.", { exact: true })).toBeVisible();
  await expect(failedJobRow.getByRole("button", { name: "Review configuration" })).toBeVisible();
  await expect(failedJobRow.getByRole("button", { name: "View instance" })).toHaveCount(0);
  await failedJobRow.getByRole("button", { name: "Review configuration" }).click();
  await expect(page).toHaveURL(
    /\/inventory\/instances\/create\?node=cluster-a&type=vm&project=tenant-a/,
  );
});

test("a create-success then start-failure keeps the partial job beside the real instance", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const existing = fullInstance("Running");
  const created = {
    ...fullInstance("Stopped"),
    name: "start-failed-vm",
    host: "cluster-a",
    project: "tenant-a",
    ip_addresses: [],
  };
  const initialInventory = instanceInventory([existing]);
  const refreshedInventory = instanceInventory([existing, created]);
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceWebSocketDisconnect: true,
    snapshotReadSequences: {
      instancePage: [
        { body: initialInventory },
        ...Array.from({ length: 5 }, () => ({ body: refreshedInventory })),
      ],
    },
  });
  const entry = {
    id: "cluster-a/tenant-a/start-failed-vm",
    name: "start-failed-vm",
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    jobId: "start-failed-job",
    createdAt: Date.now(),
  };
  await seedPendingJob(page, "admin", entry);
  let releasePartial;
  const partialGate = new Promise((resolve) => { releasePartial = resolve; });
  await page.route("**/api/v1/instances/operations/start-failed-job", async (route) => {
    await partialGate;
    await json(route, createJob(entry, "partial", {
      error: "The instance was created, but it did not start.",
      result: { instance_status: "Stopped" },
      verified_readback: { status: "Stopped" },
    }));
  });
  // The verdict reads the instance on its own cluster before the fleet catches up.
  const exactReads = [];
  await page.route((url) => url.pathname === "/api/v1/instances/start-failed-vm", async (route) => {
    exactReads.push(new URL(route.request().url()).search);
    await json(route, created);
  });

  await page.goto("/inventory/instances");
  await expect(page.locator(
    '[data-row-key="cluster-a/tenant-a/start-failed-vm"]',
  )).toHaveCount(0);
  releasePartial();
  const partialJobRow = page.locator('tr[data-state="pending"]').filter({
    has: page.getByText("start-failed-vm", { exact: true }),
  });
  await expect(partialJobRow).toHaveCount(1);
  await expect(partialJobRow.getByText("Partially created", { exact: true })).toBeVisible();
  await expect(partialJobRow.getByText(
    "The instance was created, but it did not start.",
    { exact: true },
  )).toBeVisible();
  await expect(partialJobRow.getByRole("button", { name: "Review configuration" })).toHaveCount(0);
  await expect(page.locator(
    '[data-row-key="cluster-a/tenant-a/start-failed-vm"]',
  )).toBeVisible();
  await partialJobRow.getByRole("button", { name: "View instance" }).click();
  await expect(page.locator(
    '[data-row-key="cluster-a/tenant-a/start-failed-vm"][data-row-focus="true"]',
  )).toBeVisible();
  await expect(partialJobRow.getByText("Partially created", { exact: true })).toBeVisible();
  expect(exactReads).toEqual(["?host=cluster-a&project=tenant-a"]);
  await expect.poll(() => api.instanceSockets.some((socket) => socket.closedAt !== null)).toBe(true);
  await expectCleanHarness(api);
});

test("an unknown create keeps exact reconciliation and offers actual-state refresh", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { snapshotInstancePage: true });
  const entry = {
    id: "cluster-a/tenant-a/unknown-create",
    name: "unknown-create",
    host: "cluster-a",
    project: "tenant-a",
    type: "container",
    jobId: "unknown-create-job",
    createdAt: Date.now(),
  };
  let reads = 0;
  await seedPendingJob(page, "partner", entry);
  await page.route("**/api/v1/instances/operations/unknown-create-job", async (route) => {
    reads += 1;
    await json(route, createJob(entry, "unknown", {
      phase: "verifying",
      error: "The daemon result is not yet conclusive.",
    }));
  });

  await page.goto("/inventory/instances");
  await expect(page.getByText("Outcome unknown")).toBeVisible();
  await expect(page.getByRole("button", { name: "Review configuration" })).toHaveCount(0);
  const readsBeforeRefresh = reads;
  const freshListReads = [];
  page.on("request", (request) => {
    if (/\/api\/v1\/instances\?(.*&)?fresh=true/.test(request.url())) freshListReads.push(request.url());
  });
  // A document reload would clear this marker, so it proves the row refreshed in place.
  await page.evaluate(() => { window.__sameDocument = true; });
  await page.getByRole("button", { name: "Reload current state" }).click();
  await expect.poll(() => reads).toBeGreaterThan(readsBeforeRefresh);
  await expect.poll(() => freshListReads.length).toBeGreaterThan(0);
  await expect(page.getByText("Outcome unknown")).toBeVisible();
  expect(await page.evaluate(() => window.__sameDocument)).toBe(true);
  await expectCleanHarness(api);
});

// The row is refreshed in place, not rebuilt from sessionStorage (which holds no verdict), so the
// refresh itself has to put a halted row back on its job.
test("reloading the current state of a halted create follows its job again", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { snapshotInstancePage: true });
  const entry = {
    id: "cluster-a/tenant-a/halted-create",
    name: "halted-create",
    host: "cluster-a",
    project: "tenant-a",
    type: "container",
    jobId: "halted-create-job",
    createdAt: Date.now(),
  };
  let available = false;
  await seedPendingJob(page, "partner", entry);
  await page.route("**/api/v1/instances/operations/halted-create-job", async (route) => {
    if (available) await json(route, createJob(entry, "running"));
    else await json(route, { detail: "Not found" }, 404);
  });

  await page.goto("/inventory/instances");
  await expect(page.getByText("Status unavailable")).toBeVisible();
  available = true;
  await page.getByRole("button", { name: "Reload current state" }).click();
  await expect(page.getByText("Status unavailable")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reload current state" })).toHaveCount(0);
  await expectCleanHarness(api);
});

// A cluster timing out at the 30 s inventory wall holds every fleet read for that long. The rows
// below stand for it: a fleet read made after the verdict answers only after the test has looked.
const SLOW_FLEET_MS = 40_000;

function slowFleetPendingEntry(name, jobId) {
  return {
    id: `cluster-a/tenant-a/${name}`,
    name,
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    jobId,
    createdAt: Date.now(),
  };
}

test("a created instance is listed from its own cluster while the fleet read is slow", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const existing = fullInstance("Running");
  const created = { ...fullInstance("Running"), name: "slow-fleet-vm" };
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const entry = slowFleetPendingEntry("slow-fleet-vm", "slow-fleet-job");
  await seedPendingJob(page, "admin", entry);
  let succeeded = false;
  const exactReads = [];
  await page.route("**/api/v1/instances/operations/slow-fleet-job", async (route) => {
    await json(route, createJob(entry, succeeded ? "success" : "running"));
  });
  await page.route((url) => url.pathname === "/api/v1/instances", async (route) => {
    const afterVerdict = succeeded;
    if (afterVerdict) await new Promise((resolve) => setTimeout(resolve, SLOW_FLEET_MS));
    await json(route, instanceInventory(afterVerdict ? [existing, created] : [existing])).catch(() => {});
  });
  await page.route((url) => url.pathname === "/api/v1/instances/slow-fleet-vm", async (route) => {
    exactReads.push(new URL(route.request().url()).search);
    await json(route, created);
  });

  await page.goto("/inventory/instances");
  const pendingRow = page.locator('tr[data-state="pending"]').filter({
    has: page.getByText("slow-fleet-vm", { exact: true }),
  });
  await expect(page.locator('[data-row-key="cluster-a/tenant-a/tenant-owned-vm"]')).toBeVisible();
  await expect(pendingRow).toHaveCount(1);
  succeeded = true;
  await expect(page.locator('[data-row-key="cluster-a/tenant-a/slow-fleet-vm"]')).toBeVisible({ timeout: 10_000 });
  await expect(pendingRow).toHaveCount(0);
  expect(exactReads.length).toBeGreaterThan(0);
  expect(new URLSearchParams(exactReads[0]).get("host")).toBe("cluster-a");
  expect(new URLSearchParams(exactReads[0]).get("project")).toBe("tenant-a");
  await expectCleanHarness(api);
});

test("a slow fresh fleet read does not cancel the list's first answer", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  // A create still running subscribes its cluster's event stream before the list has answered,
  // and that subscription asks the fleet for a fresh read.
  const entry = slowFleetPendingEntry("still-creating-vm", "still-creating-job");
  await seedPendingJob(page, "admin", entry);
  await page.route("**/api/v1/instances/operations/still-creating-job", async (route) => {
    await json(route, createJob(entry, "running"));
  });
  const reads = [];
  await page.route((url) => url.pathname === "/api/v1/instances", async (route) => {
    const fresh = new URL(route.request().url()).searchParams.get("fresh") === "true";
    reads.push(fresh ? "fresh" : "cached");
    // A cached answer still takes a moment, long enough for the stream to open beside it.
    await new Promise((resolve) => setTimeout(resolve, fresh ? SLOW_FLEET_MS : 1_500));
    await json(route, instanceInventory([fullInstance("Running")])).catch(() => {});
  });

  await page.goto("/inventory/instances");
  await expect(page.locator('[data-row-key="cluster-a/tenant-a/tenant-owned-vm"]')).toBeVisible({ timeout: 10_000 });
  expect(reads).toContain("cached");
  await expectCleanHarness(api);
});
