const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
  namesActor,
} = require("./support/fixtures.cjs");

const gib = 1024 ** 3;

function editableSpecification(overrides = {}) {
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
    ...overrides,
  };
}

async function selectDuringHeldMenuExit(page, actionName) {
  // Pause only the exit animation so a real backend read/event can update the row before
  // Radix releases its focus scope. Release below exercises the actual close-autofocus path.
  await page.addStyleTag({ content: `
    @keyframes qa-row-action-exit { from { opacity: 1; } to { opacity: 0; } }
    [role="menu"][data-state="closed"] {
      animation: qa-row-action-exit 650ms linear paused !important;
    }
  ` });
  const opener = page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  });
  const previousOpener = await opener.elementHandle();
  await opener.press("Enter");
  const action = page.getByRole("menuitem", { name: actionName });
  await expect(action).not.toHaveAttribute("aria-disabled", "true");
  await action.focus();
  await action.press("Enter");
  const closing = page.locator('[role="menu"][data-state="closed"]');
  await expect(closing).toHaveCount(1);
  return { opener, closing, previousOpener };
}

for (const role of ["admin", "partner"]) {
  for (const change of ["write availability", "action removal", "opener unmount"]) {
    test(`${role} row handoff cancels obsolete selection after ${change}`, async ({ page }, testInfo) => {
      await authenticate(page.context(), role);
      const streams = [];
      const options = { snapshotInstancePage: true, onInstanceSocket: (socket) => streams.push(socket) };
      if (change === "write availability") await page.clock.install();
      const api = await installApiFixtures(page, role, options);
      await page.goto("/inventory/instances");
      await expect.poll(() => streams.length).toBe(1);
      await expect.poll(() => api.requests.filter(({ path }) => path === "/system/settings").length).toBe(1);
      const { opener, closing, previousOpener } = await selectDuringHeldMenuExit(page,
        change === "action removal" ? /Stop Instance/ : /Edit Specifications/);
      expect(await previousOpener.evaluate((element) => element.isConnected)).toBe(true);

      if (change === "write availability") {
        options.readOnly = true;
        await page.clock.fastForward(60_001);
        await expect.poll(() => api.requests.filter(({ path }) => path === "/system/settings").length).toBe(2);
        await expect(closing.locator('[data-action="edit-specification"]')).toContainText("Disabled in read-only mode");
      } else {
        options.instanceStatus = "Stopped";
        streams[0].send(JSON.stringify({ type: "lifecycle", action: change === "action removal"
          ? "instance-stopped" : "instance-deleted", instance: "tenant-owned-vm", project: "tenant-a" }));
        if (change === "action removal") {
          await expect(closing.locator('[data-action="stop"]')).toHaveCount(0);
          await expect(closing.locator('[data-action="start"]')).toHaveCount(1);
        } else {
          await expect(opener).toHaveCount(0);
          await expect.poll(() => previousOpener.evaluate((element) => element.isConnected)).toBe(false);
        }
      }
      if (change !== "opener unmount") {
        await closing.evaluate((element) => element.style.setProperty("animation-play-state", "running", "important"));
        await expect(closing).toHaveCount(0);
        await expect(opener).toBeFocused();
      } else {
        await expect(closing).toHaveCount(0);
        await page.keyboard.press("Tab");
        await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.isConnected
          && document.activeElement !== document.body && !document.activeElement.closest('[role="menu"], [role="dialog"], [role="alertdialog"]')))).toBe(true);
      }
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(page.getByRole("alertdialog")).toHaveCount(0);
      const specificationReads = api.requests.filter(({ path }) => path.includes("/specification"));
      expect(specificationReads).toEqual([]);
      expect(api.mutations).toEqual([]);
      await testInfo.attach("obsolete-row-selection", {
        body: JSON.stringify({ role, change, specificationReads, mutations: api.mutations,
          openerConnected: await previousOpener.evaluate((element) => element.isConnected) }),
        contentType: "application/json",
      });
      await expectCleanHarness(api);
    });
  }
}

function job(status, phase, overrides = {}) {
  const base = {
    job_id: "spec-job-1",
    kind: "specification",
    instance_name: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    phase,
    status,
    operation_id: "daemon-operation-1",
    error: null,
    result: status === "success"
      ? { specification: null, guest_action_required: false }
      : null,
    verified_readback: ["success", "partial"].includes(status)
      ? {
          status: status === "partial" ? "Stopped" : "Running",
          cpu: { value: 6, source: "instance" },
          memory: { value_bytes: 8 * gib, source: "instance" },
          root_disk: { value_bytes: 80 * gib, pool: "fast-pool", source: "instance" },
        }
      : null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
  return { ...base, ...overrides };
}

for (const role of ["admin", "partner"]) {
  test(`${role} explicitly confirms a restart-required specification edit`, async ({ page }) => {
    await authenticate(page.context(), role);
    const api = await installApiFixtures(page, role, { snapshotInstancePage: true });
    const calls = [];
    let operationReads = 0;

    await page.route(
      /\/api\/v1\/instances\/tenant-owned-vm\/specification(?:\/preflight)?(?:\?.*)?$/,
      async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const method = request.method();
      calls.push({
        method,
        path: url.pathname,
        headers: request.headers(),
        body: request.postDataJSON?.() ?? null,
      });

      if (method === "GET" && url.pathname.endsWith("/specification")) {
        await route.fulfill({
          status: 200,
          headers: { ETag: '"spec-v1"' },
          contentType: "application/json",
          body: JSON.stringify({
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
          }),
        });
        return;
      }

      if (method === "POST" && url.pathname.endsWith("/specification/preflight")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            instance_name: "tenant-owned-vm",
            host: "cluster-a",
            project: "tenant-a",
            status: "Running",
            type: "virtual-machine",
            etag: '"spec-v1"',
            plan_revision: "plan-v1",
            execution: "restart_required",
            changes: [{ field: "cpu_count", before: 4, after: 6, source: "profile" }],
            warnings: [{
              code: "RESTART_REQUIRED",
              message: "This VM requires a graceful restart to apply the CPU change.",
              requires_acknowledgement: true,
            }],
            blockers: [],
            required_acknowledgements: ["restart_instance"],
          }),
        });
        return;
      }

      if (method === "PATCH" && url.pathname.endsWith("/specification")) {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify(job("pending", "pending")),
        });
        return;
      }

        await route.fallback();
      },
    );

    await page.route("**/api/v1/instances/operations/spec-job-1", async (route) => {
      operationReads += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          operationReads === 1 ? job("running", "applying") : job("success", "verifying"),
        ),
      });
    });

    await page.goto("/inventory/instances");
    await page.getByRole("button", {
      name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
    }).click();
    await page.getByRole("menuitem", { name: /Edit Specifications/ }).click();

    const dialog = page.getByRole("dialog", { name: "Edit Specifications" });
    await expect(dialog).toBeVisible();
    const cpu = dialog.getByLabel("CPU count");
    await expect(cpu).toHaveValue("4");
    await cpu.fill("6");
    await dialog.getByRole("button", { name: "Review and apply" }).click();

    await expect(dialog.getByText("Review Changes")).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Apply and restart" })).toBeVisible();
    await dialog.getByRole("button", { name: "Apply and restart" }).click();

    await expect(dialog.getByText("Specifications applied and verified")).toBeVisible({ timeout: 15_000 });
    await expect(dialog.getByText("Verified cluster readback")).toBeVisible();
    await expect(dialog.getByText("6", { exact: true })).toBeVisible();
    await dialog.getByRole("button", { name: "Done" }).click();
    await expect(dialog).toBeHidden();
    await expect(
      page.getByRole("region", { name: "Notifications (F8)" })
        .getByText("Specifications updated", { exact: true }),
    ).toBeVisible();

    const preflight = calls.find((call) => call.method === "POST");
    expect(preflight.body).toEqual({ host: "cluster-a", project: "tenant-a", cpu_count: 6 });
    const patches = calls.filter((call) => call.method === "PATCH");
    expect(patches).toHaveLength(1);
    expect(patches[0].headers["if-match"]).toBe('"spec-v1"');
    expect(patches[0].headers["idempotency-key"]).toBeTruthy();
    expect(patches[0].body).toMatchObject({
      host: "cluster-a",
      project: "tenant-a",
      cpu_count: 6,
      plan_revision: "plan-v1",
      apply_mode: "graceful_restart",
      acknowledgements: ["restart_instance"],
    });
    expect(operationReads).toBeGreaterThanOrEqual(2);
    await expectNoHorizontalOverflow(page);
    await expectCleanHarness(api);
  });
}

test("tenant cannot see the specification edit action", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true });

  await page.goto("/inventory/instances");
  await page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  }).click();
  await expect(page.getByRole("menuitem", { name: /Edit Specifications/ })).toHaveCount(0);

  await expectNoHorizontalOverflow(page);
  await expectCleanHarness(api);
});

test("validation explains root shrink, memory pressure, and irreversible disk growth", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await page.route(
    /\/api\/v1\/instances\/tenant-owned-vm\/specification(?:\?.*)?$/,
    async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname.endsWith("/specification")) {
      await route.fulfill({
        status: 200,
        headers: { ETag: '"spec-v1"' },
        contentType: "application/json",
        body: JSON.stringify(editableSpecification()),
      });
      return;
    }
      await route.fallback();
    },
  );

  await page.goto("/inventory/instances");
  const actions = page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  });
  await actions.focus();
  await actions.press("Enter");
  const edit = page.getByRole("menuitem", { name: /Edit Specifications/ });
  await edit.focus();
  await edit.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Edit Specifications" });

  const cpu = dialog.getByLabel("CPU count");
  await cpu.focus();
  await cpu.fill("1.5");
  await expect(cpu).toBeFocused();
  await expect(cpu).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByText("Enter a positive whole number.")).toBeVisible();
  await cpu.fill("4");

  const rootDisk = dialog.getByRole("spinbutton", { name: "Root Disk Size", exact: true });
  await rootDisk.fill("70");
  await expect(rootDisk).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByText("Root disk size can only be increased.")).toBeVisible();
  await rootDisk.fill("100");
  await expect(dialog.getByText("Root disk growth cannot be rolled back by shrinking the disk.")).toBeVisible();

  await dialog.getByRole("spinbutton", { name: "Memory Limit", exact: true }).fill("4");
  await expect(dialog.getByText("Lowering memory can pressure or terminate workloads.", { exact: false })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expectCleanHarness(api);
});

test("tenant API denial remains enforced when the hidden edit route is called directly", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true });
  await page.route("**/api/v1/instances/tenant-owned-vm/specification**", async (route) => {
    expect(namesActor(route.request(), "tenant")).toBe(true);
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Tenant access is view-only." }),
    });
  });

  await page.goto("/inventory/instances");
  const response = await page.evaluate(async () => {
    const result = await fetch(
      "/api/v1/instances/tenant-owned-vm/specification?host=cluster-a&project=tenant-a",
      // As the app sends it: the account it acts for, never a bearer (the proxy holds the token).
      { headers: { "x-orcastra-actor": encodeURIComponent("tenant@e2e.invalid") } },
    );
    return { status: result.status, body: await result.json() };
  });
  expect(response).toEqual({ status: 403, body: { detail: "Tenant access is view-only." } });
  await expectCleanHarness(api);
});

for (const role of ["admin", "partner"]) {
test(`${role} detail and row action handoffs restore keyboard focus to a connected opener`, async ({ page }) => {
  await authenticate(page.context(), role);
  const api = await installApiFixtures(page, role, { snapshotInstancePage: true });
  await page.route("**/api/v1/instances/tenant-owned-vm/specification**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET" && url.pathname.endsWith("/specification")) {
      await route.fulfill({
        status: 200,
        headers: { ETag: '"spec-v1"' },
        contentType: "application/json",
        body: JSON.stringify(editableSpecification()),
      });
      return;
    }
    await route.fallback();
  });

  await page.goto("/inventory/instances");
  const nameLink = page.getByRole("button", { name: "tenant-owned-vm", exact: true });
  await nameLink.focus();
  await nameLink.press("Enter");
  const detail = page.getByRole("dialog", { name: "tenant-owned-vm" });
  const detailEdit = detail.getByRole("button", { name: "Edit specifications" });
  await detailEdit.click();

  const specification = page.getByRole("dialog", { name: "Edit Specifications" });
  await expect(specification).toBeVisible();
  await page.keyboard.press("Shift+Tab");
  await expect.poll(() => page.evaluate(() =>
    document.activeElement?.closest('[role="dialog"]')?.textContent?.includes("Edit Specifications") ?? false,
  )).toBe(true);
  await page.keyboard.press("Escape");
  await expect(specification).toBeHidden();
  await expect(detail).toBeVisible();
  await expect(detailEdit).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(detail).toBeHidden();
  await expect(nameLink).toBeFocused();

  const actions = page.getByRole("button", {
    name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a",
  });
  // Exercise a slow menu exit as well as keyboard selection; the dialog must inherit the
  // connected row trigger after the menu releases its focus scope.
  await page.addStyleTag({ content: '[role="menu"][data-state="closed"] { animation-duration: 650ms !important; }' });
  for (const input of ["pointer", "keyboard"]) {
    if (input === "pointer") await actions.click();
    else await actions.press("Enter");
    const item = page.getByRole("menuitem", { name: /Edit Specifications/ });
    if (input === "pointer") await item.click();
    else { await item.focus(); await item.press("Enter"); }
    await expect(specification).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(specification).toBeHidden();
    await expect(actions).toBeFocused();
  }
  await actions.press("Enter");
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toBeHidden();
  await expect(actions).toBeFocused();
  await expectCleanHarness(api);
});
}
