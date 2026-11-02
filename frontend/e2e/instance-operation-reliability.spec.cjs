const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  installApiFixtures,
} = require("./support/fixtures.cjs");

const gib = 1024 ** 3;

function specification() {
  return {
    instance_name: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    status: "Running",
    cpu: { value: 4, source: "profile" },
    memory: { value_bytes: 8 * gib, source: "instance" },
    root_disk: {
      value_bytes: 80 * gib,
      pool: "fast-pool",
      source: "instance",
      editable: true,
      reason: null,
    },
    capabilities: { live_update: true, restart_supported: true },
    etag: '"spec-v1"',
  };
}

function preflight() {
  return {
    instance_name: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    status: "Running",
    type: "virtual-machine",
    etag: '"spec-v1"',
    plan_revision: "a".repeat(64),
    execution: "live",
    changes: [{ field: "cpu_count", before: 4, after: 6, source: "profile" }],
    warnings: [],
    blockers: [],
    required_acknowledgements: [],
  };
}

function operation(status, phase, overrides = {}) {
  return {
    job_id: "spec-reliability-job",
    kind: "specification",
    instance_name: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    phase,
    status,
    operation_id: "daemon-spec-operation",
    operation_ids: ["daemon-spec-operation"],
    error: null,
    outcome: ["success", "failure", "partial"].includes(status) ? status : null,
    result: null,
    verified_readback: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

async function openEditor(page) {
  await page.goto("/inventory/instances");
  await page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  }).click();
  await page.getByRole("menuitem", { name: /Edit Specifications/ }).click();
  const dialog = page.getByRole("dialog", { name: "Edit Specifications" });
  await expect(dialog).toBeVisible();
  return dialog;
}

async function routeSpecification(page, onPatch) {
  await page.route(
    /\/api\/v1\/instances\/tenant-owned-vm\/specification(?:\/preflight)?(?:\?.*)?$/,
    async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname.endsWith("/specification")) {
      await route.fulfill({
        status: 200,
        headers: { ETag: '"spec-v1"' },
        contentType: "application/json",
        body: JSON.stringify(specification()),
      });
      return;
    }
    if (request.method() === "POST" && url.pathname.endsWith("/preflight")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(preflight()),
      });
      return;
    }
    if (request.method() === "PATCH" && url.pathname.endsWith("/specification")) {
      await onPatch(route);
      return;
    }
    await route.fallback();
    },
  );
}

test("an interrupted PATCH replays the exact key once and renders partial readback", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const writes = [];
  await routeSpecification(page, async (route) => {
    writes.push({
      key: route.request().headers()["idempotency-key"],
      body: route.request().postData(),
    });
    if (writes.length === 1) {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: "{",
      });
      return;
    }
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(operation("pending", "pending")),
    });
  });
  await page.route("**/api/v1/instances/operations/spec-reliability-job", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(operation("partial", "partial", {
        error: "The limits were applied, but the VM could not be restarted.",
        result: { instance_status: "Stopped" },
        verified_readback: {
          status: "Stopped",
          cpu: { value: 6, source: "instance" },
          memory: { value_bytes: 8 * gib, source: "instance" },
          root_disk: { value_bytes: 80 * gib, pool: "fast-pool", source: "instance" },
        },
      })),
    });
  });

  const dialog = await openEditor(page);
  await dialog.getByLabel("CPU count").fill("6");
  const review = dialog.getByRole("button", { name: "Review and apply" });
  await review.evaluate((button) => {
    button.click();
    button.click();
  });
  await expect(dialog.getByRole("button", { name: "Retry same request" })).toBeVisible();
  const ambiguousStorage = await page.evaluate(() =>
    Object.keys(sessionStorage).filter((key) =>
      key.startsWith("orcastra.specification-job.v2."),
    ),
  );
  expect(ambiguousStorage).toEqual([]);
  await dialog.getByRole("button", { name: "Retry same request" }).click();

  await expect(dialog.getByText("Specifications partially applied")).toBeVisible();
  await expect(dialog.getByText("Verified cluster readback")).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
  await expectCleanHarness(api);
});

test("a specification job resumes after reload and shows VM guest guidance", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { snapshotInstancePage: true });
  let complete = false;
  let writes = 0;
  await routeSpecification(page, async (route) => {
    writes += 1;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(operation("pending", "pending")),
    });
  });
  await page.route("**/api/v1/instances/operations/spec-reliability-job", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(complete
        ? operation("success", "success", {
            outcome: "success",
            result: { guest_action_required: true },
            verified_readback: {
              status: "Running",
              cpu: { value: 6, source: "instance" },
              memory: { value_bytes: 8 * gib, source: "instance" },
              root_disk: { value_bytes: 80 * gib, pool: "fast-pool", source: "instance" },
            },
          })
        : operation("running", "applying")),
    });
  });

  let dialog = await openEditor(page);
  await dialog.getByLabel("CPU count").fill("6");
  await dialog.getByRole("button", { name: "Review and apply" }).click();
  await expect(dialog.getByText("Applying specifications")).toBeVisible();
  const persisted = await page.evaluate(() =>
    Object.entries(sessionStorage).find(([key]) =>
      key.startsWith("orcastra.specification-job.v2."),
    ) ?? null,
  );
  expect(persisted).not.toBeNull();
  expect(JSON.parse(persisted[1])).toMatchObject({
    instanceName: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    jobId: "spec-reliability-job",
  });
  expect(Object.keys(JSON.parse(persisted[1])).sort()).toEqual([
    "host", "instanceName", "jobId", "project",
  ]);
  await dialog.getByRole("button", { name: "Close" }).filter({
    hasNot: page.locator("svg"),
  }).click();

  await page.reload();
  complete = true;
  dialog = await openEditor(page);
  await expect(dialog.getByText("Specifications applied and verified")).toBeVisible();
  await expect(dialog.getByText("Guest verification required")).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
  expect(writes).toBe(1);
  const retained = await page.evaluate(() =>
    Object.keys(sessionStorage).some((key) =>
      key.startsWith("orcastra.specification-job.v2."),
    ),
  );
  expect(retained).toBe(false);
  await expectCleanHarness(api);
});

test("a missing durable job stops polling and keeps recovery actions visible", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await routeSpecification(page, async (route) => {
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(operation("pending", "pending")),
    });
  });
  let reads = 0;
  await page.route("**/api/v1/instances/operations/spec-reliability-job", async (route) => {
    reads += 1;
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Instance operation not found" }),
    });
  });

  const dialog = await openEditor(page);
  await dialog.getByLabel("CPU count").fill("6");
  await dialog.getByRole("button", { name: "Review and apply" }).click();
  await expect(dialog.getByText("Operation status unavailable")).toBeVisible({ timeout: 15_000 });
  const readsAfterHalt = reads;
  await page.waitForTimeout(3_000);
  expect(reads).toBe(readsAfterHalt);
  await expect(dialog.getByRole("button", { name: "Reload current state" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Close" }).filter({
    hasNot: page.locator("svg"),
  })).toBeVisible();
  await expectCleanHarness(api);
});

test("a mismatched specification job in the PATCH response is never persisted or projected", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  let operationReads = 0;
  await routeSpecification(page, async (route) => {
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(operation("pending", "pending", {
        instance_name: "different-instance",
      })),
    });
  });
  await page.route("**/api/v1/instances/operations/spec-reliability-job", async (route) => {
    operationReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(operation("success", "verifying")),
    });
  });

  const dialog = await openEditor(page);
  await dialog.getByLabel("CPU count").fill("6");
  await dialog.getByRole("button", { name: "Review and apply" }).click();
  await expect(dialog.getByText("Operation identity conflict")).toBeVisible();
  await expect(dialog.getByText("No status was saved or applied", { exact: false })).toBeVisible();
  await expect(dialog.getByText("Specifications applied and verified")).toHaveCount(0);
  expect(operationReads).toBe(0);
  expect(await page.evaluate(() => Object.keys(sessionStorage).some((key) =>
    key.startsWith("orcastra.specification-job.v2."),
  ))).toBe(false);
  await expectCleanHarness(api);
});

test("a mismatched specification poll halts before projecting its terminal result", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await routeSpecification(page, async (route) => {
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(operation("pending", "pending")),
    });
  });
  let operationReads = 0;
  await page.route("**/api/v1/instances/operations/spec-reliability-job", async (route) => {
    operationReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(operation("success", "verifying", {
        project: "different-project",
      })),
    });
  });

  const dialog = await openEditor(page);
  await dialog.getByLabel("CPU count").fill("6");
  await dialog.getByRole("button", { name: "Review and apply" }).click();
  await expect(dialog.getByText("Operation identity conflict")).toBeVisible();
  await expect(dialog.getByText("Its status was not applied", { exact: false })).toBeVisible();
  await expect(dialog.getByText("Specifications applied and verified")).toHaveCount(0);
  const readsAfterConflict = operationReads;
  await page.waitForTimeout(3_000);
  expect(operationReads).toBe(readsAfterConflict);
  await expect(dialog.getByRole("button", { name: "Reload current state" })).toBeVisible();
  await expectCleanHarness(api);
});

for (const conflict of [
  {
    status: 412,
    detail: { code: "STALE_ETAG", message: "The instance changed." },
    copy: "This instance changed after the dialog loaded.",
  },
  {
    status: 409,
    detail: { code: "STALE_PLAN", message: "The reviewed plan changed." },
    copy: "This plan is no longer valid or another operation is active.",
  },
]) {
  test(`a ${conflict.status} conflict requires fresh state and never auto-replays`, async ({ page }) => {
    await authenticate(page.context(), "admin");
    const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
    let writes = 0;
    await routeSpecification(page, async (route) => {
      writes += 1;
      await route.fulfill({
        status: conflict.status,
        contentType: "application/json",
        body: JSON.stringify({ detail: conflict.detail }),
      });
    });

    const dialog = await openEditor(page);
    await dialog.getByLabel("CPU count").fill("6");
    await dialog.getByRole("button", { name: "Review and apply" }).click();
    await expect(dialog.getByText(conflict.copy, { exact: false })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Reload current state" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Retry same request" })).toHaveCount(0);
    await page.waitForTimeout(1_000);
    expect(writes).toBe(1);
    await expectCleanHarness(api);
  });
}

test("an unknown specification result stays terminal and requires actual-state review", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { snapshotInstancePage: true });
  let writes = 0;
  await routeSpecification(page, async (route) => {
    writes += 1;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(operation("pending", "pending")),
    });
  });
  await page.route("**/api/v1/instances/operations/spec-reliability-job", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(operation("unknown", "verifying", {
        error: "The daemon timed out before readback could prove the result.",
      })),
    });
  });

  const dialog = await openEditor(page);
  await dialog.getByLabel("CPU count").fill("6");
  await dialog.getByRole("button", { name: "Review and apply" }).click();
  await expect(dialog.getByText("Outcome needs verification")).toBeVisible();
  await expect(dialog.getByText("The daemon timed out before readback could prove the result.")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Reload current state" })).toBeVisible();
  await page.waitForTimeout(1_000);
  expect(writes).toBe(1);
  await expectCleanHarness(api);
});
