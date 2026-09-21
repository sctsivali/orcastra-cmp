const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");

function pendingStorageKey(role) {
  const scope = `${role}@e2e.invalid:role_${role}`;
  let hash = 2166136261;
  for (let index = 0; index < scope.length; index += 1) {
    hash ^= scope.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `orcastra.pending-creates.v1.${(hash >>> 0).toString(16)}`;
}

async function seedPending(page, role, entry) {
  await page.addInitScript(({ key, value }) => {
    sessionStorage.setItem(key, JSON.stringify([value]));
  }, { key: pendingStorageKey(role), value: entry });
}

function emptyInventory() {
  return {
    total: 0,
    vms: [],
    nodes: ["cluster-a"],
    lxd_count: 0,
    microcloud_count: 0,
    incus_count: 0,
    offset: 0,
    limit: 100,
    has_more: false,
    unreachable_nodes: [],
  };
}

test("Partner safe policy keeps typed create fields and hides host-level controls", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
    instanceWritePolicyMode: "safe",
  });
  await page.goto("/inventory/instances/create?node=cluster-a&type=container&project=tenant-a");

  await expect(page.getByLabel("CPU Cores")).toBeVisible();
  await expect(page.getByLabel("Memory")).toBeVisible();
  await expect(page.getByLabel("Storage", { exact: true })).toBeVisible();
  await expect(page.getByText("GPU Passthrough")).toHaveCount(0);
  await expect(page.getByText("Security Policies")).toHaveCount(0);
  await page.getByText("Cloud-Init", { exact: true }).first().click();
  await expect(page.getByText("Advanced host controls are unavailable")).toBeVisible();
  await expect(page.getByText("User data", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Add disk" }).click();
  await page.getByLabel("Disk kind").click();
  await expect(page.getByRole("option", { name: "Custom volume" })).toBeVisible();
  await expect(page.getByRole("option", { name: "Host path" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expectCleanHarness(api);
});

test("Admin full policy exposes the existing advanced create surface", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
    instanceWritePolicyMode: "full",
  });
  await page.goto("/inventory/instances/create?node=cluster-a&type=container&project=tenant-a");

  await expect(page.getByText("GPU Passthrough")).toBeVisible();
  await page.getByText("Advanced Options", { exact: true }).click();
  await expect(page.getByText("Security Policies")).toBeVisible();
  await expect(page.getByText("Migration", { exact: true })).toBeVisible();
  await expect(page.getByText("Boot", { exact: true })).toBeVisible();
  await expectCleanHarness(api);
});

test("advanced create controls fail closed when policy cannot be read", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  await page.route("**/api/v1/instances/write-policy?host=cluster-a", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Policy service unavailable" }),
    });
  });
  await page.goto("/inventory/instances/create?node=cluster-a&type=container&project=tenant-a");

  await expect(page.getByLabel("CPU Cores")).toBeVisible();
  await expect(page.getByText("GPU Passthrough")).toHaveCount(0);
  await page.getByText("Cloud-Init", { exact: true }).first().click();
  await expect(page.getByText("Advanced controls are unavailable because the server policy could not be verified.")).toBeVisible();
  await expectCleanHarness(api);
});

test("cached full policy cannot authorize advanced create after a refetch error", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  let policyReads = 0;
  await page.route("**/api/v1/instances/write-policy?host=cluster-a", async (route) => {
    policyReads += 1;
    if (policyReads === 1) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          host: "cluster-a",
          mode: "full",
          can_edit_specifications: true,
          can_use_advanced_create: true,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Policy service unavailable" }),
    });
  });

  await page.goto("/inventory/instances/create?node=cluster-a&type=container&project=tenant-a");
  await expect(page.getByText("GPU Passthrough")).toBeVisible();
  await page.locator('a[href="/inventory/instances"]').first().click();
  await expect(page).toHaveURL(/\/inventory\/instances$/);
  await page.goBack();
  await expect.poll(() => policyReads).toBeGreaterThan(1);
  await expect(page.getByText("GPU Passthrough")).toHaveCount(0);
  await expect(page.getByText("previously granted advanced write policy could not be verified", {
    exact: false,
  })).toBeVisible();
  await expectCleanHarness(api);
});

test("Partner create preflight keeps long effective identifiers inside a mobile dialog", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await authenticate(page.context(), "partner");
  const fingerprint = "c".repeat(64);
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
    instanceWritePolicyMode: "safe",
    instanceCreateImage: {
      fingerprint,
      aliases: ["long-name-image"],
      description: "Long identifier fixture",
      architecture: "x86_64",
      size: 1024,
      upload_date: null,
      public: false,
      type: "container",
      cached: true,
      auto_update: false,
      created_at: null,
      last_used_at: null,
      expires_at: null,
      profiles: [],
      project: "tenant-a",
    },
  });
  await page.route("**/api/v1/instances/preflight", async (route) => {
    const payload = route.request().postDataJSON();
    expect(payload.config).not.toHaveProperty("limits.memory.swap");
    expect(Object.keys(payload.config).some((key) => /^(security|boot)\./.test(key))).toBe(false);
    expect(Object.values(payload.devices)).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "gpu" }),
    ]));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ready: true,
        plan_revision: "mobile-plan",
        blockers: [],
        warnings: [{
          code: "NETWORKLESS",
          field: "devices",
          message: "No effective network interface is configured.",
          acknowledgement_required: true,
        }],
        required_acknowledgements: ["networkless"],
        effective: {
          profiles: [`profile-${"p".repeat(90)}`],
          nic_names: [`interface-${"n".repeat(90)}`],
          root_device: `root-${"r".repeat(90)}`,
        },
      }),
    });
  });

  await page.goto(
    `/inventory/instances/create?node=cluster-a&type=container&source=image&image=${fingerprint}&project=tenant-a`,
  );
  await page.getByLabel("Instance Name *").fill("partner-mobile-create");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("alertdialog", { name: "Create without network access?" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectCleanHarness(api);
});

test("read-only mode disables direct and empty-state create actions", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    readOnly: true,
    snapshotInstancePage: true,
    instanceCreatePage: true,
    snapshotReadSequences: { instancePage: [{ body: emptyInventory() }] },
  });

  await page.goto("/inventory/instances");
  await expect(page.getByRole("button", { name: "New Instance" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Create Instance" })).toBeDisabled();

  await page.goto("/inventory/instances/create?node=cluster-a&type=container&project=tenant-a");
  await expect(page.getByText("Creating instances is disabled while the system is in read-only mode.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Create & Start" })).toBeDisabled();
  await expectCleanHarness(api);
});

test("direct create stays disabled while global write availability is loading", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  let releaseSettings;
  const settingsGate = new Promise((resolve) => { releaseSettings = resolve; });
  await page.route("**/api/v1/system/settings", async (route) => {
    await settingsGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        read_only_mode: false,
        api_version: "e2e",
        debug: false,
        snapshot_schedules_enabled: true,
        task_journal_enabled: false,
      }),
    });
  });

  await page.goto("/inventory/instances/create?node=cluster-a&type=container&project=tenant-a");
  await expect(page.getByText("Creating instances is disabled while system write availability is being checked.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Create & Start" })).toBeDisabled();
  releaseSettings();
  await expect(page.getByText("Creating instances is disabled while system write availability is being checked.")).toHaveCount(0);
  await expectCleanHarness(api);
});

test("specification edit stays disabled when global write availability cannot be verified", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await page.route("**/api/v1/system/settings", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Settings unavailable" }),
    });
  });
  await page.goto("/inventory/instances");
  await page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  }).click();
  const edit = page.getByRole("menuitem", { name: /Edit Specifications/ });
  await expect(edit).toHaveAttribute("aria-disabled", "true");
  await expect(edit).toContainText("System write availability could not be verified", {
    timeout: 15_000,
  });
  await expectCleanHarness(api);
});

test("read-only edit actions explain their disabled state", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    readOnly: true,
    snapshotInstancePage: true,
  });
  await page.goto("/inventory/instances");

  await page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  }).click();
  const menuEdit = page.getByRole("menuitem", { name: /Edit Specifications/ });
  await expect(menuEdit).toHaveAttribute("aria-disabled", "true");
  await expect(menuEdit).toContainText("Disabled in read-only mode");
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "tenant-owned-vm", exact: true }).click();
  const detail = page.getByRole("dialog", { name: "tenant-owned-vm" });
  await expect(detail.getByRole("button", { name: "Edit specifications" })).toBeDisabled();
  await expectCleanHarness(api);
});

test("Tenant direct create is redirected and detail stays view-only", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true });
  await page.goto("/inventory/instances/create");
  await expect(page).toHaveURL(/\/inventory\/instances$/);
  await expect(page.getByText("Create New Instance")).toHaveCount(0);

  await page.getByRole("button", { name: "tenant-owned-vm", exact: true }).click();
  const detail = page.getByRole("dialog", { name: "tenant-owned-vm" });
  await expect(detail.getByRole("button", { name: "Edit specifications" })).toHaveCount(0);
  await expectCleanHarness(api);
});

test("a mismatched durable job is halted and Dismiss removes its safe identity", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const entry = {
    id: "cluster-a/tenant-a/pending-vm",
    name: "pending-vm",
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    jobId: "mismatched-job",
    createdAt: Date.now(),
  };
  await seedPending(page, "admin", entry);
  let reads = 0;
  await page.route("**/api/v1/instances/operations/mismatched-job", async (route) => {
    reads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        job_id: "mismatched-job",
        kind: "create",
        instance_name: "another-tenant-instance",
        host: "cluster-a",
        project: "tenant-a",
        phase: "verifying",
        status: "running",
        operation_id: "daemon-operation",
        error: null,
        result: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }),
    });
  });

  await page.goto("/inventory/instances");
  await expect(page.getByText("The operation identity did not match this pending instance.", { exact: false })).toBeVisible();
  const readsAfterHalt = reads;
  await page.waitForTimeout(3_000);
  expect(reads).toBe(readsAfterHalt);
  await page.getByRole("button", { name: "Dismiss create operation for pending-vm" }).click();
  await expect(page.getByText("pending-vm", { exact: true })).toHaveCount(0);
  expect(await page.evaluate((key) => sessionStorage.getItem(key), pendingStorageKey("admin"))).toBeNull();
  await expectCleanHarness(api);
});

test("pending create recovery never crosses authenticated actor storage", async ({ page }) => {
  await seedPending(page, "admin", {
    id: "cluster-a/tenant-a/admin-only-pending",
    name: "admin-only-pending",
    host: "cluster-a",
    project: "tenant-a",
    type: "container",
    jobId: "admin-only-job",
    createdAt: Date.now(),
  });
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true });

  await page.goto("/inventory/instances");
  await expect(page.getByText("admin-only-pending", { exact: true })).toHaveCount(0);
  expect(api.requests.some((entry) => entry.path === "/instances/operations/admin-only-job")).toBe(false);
  await expectCleanHarness(api);
});
