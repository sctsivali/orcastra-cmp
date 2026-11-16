const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// One size system on every screen, the one the account chose (decimal unless it chose binary). The
// risk: a field that shows 8.59 GB for a limit stored as 8GiB must not write 8590000000 when the
// field was left untouched, and in binary a field must write exact binary bytes.

const gib = 1024 ** 3;

async function openSpecificationDialog(page, context, fixtureOptions = {}) {
  await authenticate(context, "admin");
  // An account that chose decimal, unless a test says otherwise: binary is the default.
  await installApiFixtures(page, "admin", { snapshotInstancePage: true, byteUnits: "decimal", ...fixtureOptions });
  const previews = [];
  await page.route(/\/api\/v1\/instances\/tenant-owned-vm\/specification(?:\/preflight)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({
        headers: { ETag: '"spec-v1"' },
        json: {
          instance_name: "tenant-owned-vm", host: "cluster-a", project: "tenant-a", type: "virtual-machine",
          status: "Running", cpu: { value: 4, source: "profile" },
          memory: { value_bytes: 8 * gib, source: "instance" },
          root_disk: { value_bytes: 80 * gib, pool: "fast-pool", source: "instance", editable: true, reason: null },
          capabilities: { live_update: true, restart_supported: true }, etag: '"spec-v1"',
        },
      });
      return;
    }
    previews.push(request.postDataJSON());
    await route.fulfill({ status: 503, json: { detail: "stopped by the spec" } });
  });
  await page.goto("/inventory/instances");
  await page.getByRole("button", { name: "Actions for instance tenant-owned-vm in tenant-a on cluster-a" }).click();
  await page.getByRole("menuitem", { name: /Edit Specifications/ }).click();
  const dialog = page.getByRole("dialog", { name: "Edit Specifications" });
  await expect(dialog).toBeVisible();
  return { dialog, previews };
}

test("an account that never chose reads the disk in GiB", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await page.goto("/inventory/instances");
  const row = page.getByRole("row", { name: /tenant-owned-vm/ });
  await expect(row).toContainText("80 GiB");
  await expect(row).not.toContainText("85.9 GB");
});

test("a disk created in GiB reads in GB in the table for a decimal account", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true, byteUnits: "decimal" });
  await page.goto("/inventory/instances");
  const row = page.getByRole("row", { name: /tenant-owned-vm/ });
  await expect(row).toContainText("85.9 GB");
  await expect(row).not.toContainText("80GiB");
});

test("a size field offers the standard units and shows the stored value in them", async ({ page, context }) => {
  const { dialog } = await openSpecificationDialog(page, context);
  await expect(dialog.getByRole("spinbutton", { name: "Memory Limit", exact: true })).toHaveValue("8.59");
  await expect(dialog.getByRole("spinbutton", { name: "Root Disk Size", exact: true })).toHaveValue("85.9");

  await dialog.getByRole("combobox", { name: "Memory limit unit" }).click();
  await expect(page.getByRole("option")).toHaveText(["MB", "GB", "TB"]);
  await page.keyboard.press("Escape");
});

test("a field nobody touched is not applied, whatever it rounds to", async ({ page, context }) => {
  const { dialog, previews } = await openSpecificationDialog(page, context);
  await dialog.getByLabel("CPU count").fill("6");
  await dialog.getByRole("button", { name: "Review and apply" }).click();

  await expect.poll(() => previews.length).toBeGreaterThan(0);
  expect(previews[0]).toMatchObject({ cpu_count: 6 });
  expect(previews[0]).not.toHaveProperty("memory_bytes");
  expect(previews[0]).not.toHaveProperty("root_disk_bytes");
});

test("a field the operator typed in writes exactly what it says", async ({ page, context }) => {
  const { dialog, previews } = await openSpecificationDialog(page, context);
  await dialog.getByRole("spinbutton", { name: "Memory Limit", exact: true }).fill("4");
  await dialog.getByRole("button", { name: "Review and apply" }).click();

  await expect.poll(() => previews.length).toBeGreaterThan(0);
  expect(previews[0].memory_bytes).toBe(4_000_000_000);
});

test("typing the same figure back is still the stored value", async ({ page, context }) => {
  const { dialog } = await openSpecificationDialog(page, context);
  const memory = dialog.getByRole("spinbutton", { name: "Memory Limit", exact: true });
  await memory.fill("9");
  await expect(dialog.getByRole("button", { name: "Review and apply" })).toBeEnabled();
  await memory.fill("8.59");
  await expect(dialog.getByRole("button", { name: "Review and apply" }), "nothing is left to apply").toBeDisabled();
});

test.describe("an account that reads binary", () => {
  test("the table reads the disk in GiB", async ({ page, context }) => {
    await authenticate(context, "admin");
    await installApiFixtures(page, "admin", { snapshotInstancePage: true, byteUnits: "binary" });
    await page.goto("/inventory/instances");
    const row = page.getByRole("row", { name: /tenant-owned-vm/ });
    await expect(row).toContainText("80 GiB");
    await expect(row).not.toContainText("85.9 GB");
  });

  test("a size field offers binary units and shows a binary limit exactly", async ({ page, context }) => {
    const { dialog } = await openSpecificationDialog(page, context, { byteUnits: "binary" });
    await expect(dialog.getByRole("spinbutton", { name: "Memory Limit", exact: true })).toHaveValue("8");
    await expect(dialog.getByRole("spinbutton", { name: "Root Disk Size", exact: true })).toHaveValue("80");

    await dialog.getByRole("combobox", { name: "Memory limit unit" }).click();
    await expect(page.getByRole("option")).toHaveText(["MiB", "GiB", "TiB"]);
    await page.keyboard.press("Escape");
  });

  test("a figure typed in binary writes exact binary bytes", async ({ page, context }) => {
    const { dialog, previews } = await openSpecificationDialog(page, context, { byteUnits: "binary" });
    await dialog.getByRole("spinbutton", { name: "Memory Limit", exact: true }).fill("4");
    await dialog.getByRole("button", { name: "Review and apply" }).click();

    await expect.poll(() => previews.length).toBeGreaterThan(0);
    expect(previews[0].memory_bytes).toBe(4 * gib);
    expect(previews[0]).not.toHaveProperty("root_disk_bytes");
  });

  test("every API call tells the server which units to write", async ({ page, context }) => {
    await authenticate(context, "admin");
    await installApiFixtures(page, "admin", { snapshotInstancePage: true, byteUnits: "binary" });
    await page.goto("/inventory/instances");
    await expect(page.getByRole("row", { name: /tenant-owned-vm/ })).toContainText("80 GiB");
    // From the next load on, the hint cookie carries the choice, so even the first call says binary.
    const sent = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/v1/")) sent.push(request.headers()["x-byte-units"]);
    });
    await page.reload();
    await expect(page.getByRole("row", { name: /tenant-owned-vm/ })).toContainText("80 GiB");
    // The preference reads go out on plain fetch and carry no header: they return no size text.
    expect(sent).toContain("binary");
    expect(sent).not.toContain("decimal");
  });
});

test.describe("a binary account whose units arrive after the form", () => {
  // No hint cookie, as on a new device: the form renders before the account's choice is read.
  test("the instance create fields move to GiB until someone types in them", async ({ page, context }) => {
    await authenticate(context, "admin");
    await installApiFixtures(page, "admin", { snapshotInstancePage: true, instanceCreatePage: true, byteUnits: "binary" });
    await page.goto("/inventory/instances/create");
    const memory = page.locator("#create-memory");
    const unit = memory.locator("xpath=following-sibling::button[@role='combobox'][1]");
    await expect(memory).toHaveValue("1");
    await expect(unit).toHaveText("GiB");
  });

  test("the project create limits move to GiB, and a typed one keeps its unit", async ({ page, context }) => {
    await authenticate(context, "admin");
    await installApiFixtures(page, "admin", { byteUnits: "binary" });
    await page.route(/\/api\/v1\/nodes\/names/, (route) => route.fulfill({ json: ["cluster-a"] }));
    await page.route(/\/api\/v1\/nodes\/[^/]+\/resources/, (route) => route.fulfill({ json: {
      cpu_total: 12, cpu_available: 12, memory_total: 64 * gib, memory_available: 60 * gib,
      storage_total: 400 * gib, storage_available: 300 * gib,
    } }));
    let posted = null;
    await page.route(/\/api\/v1\/projects(\?|$)/, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      posted = route.request().postDataJSON();
      return route.fulfill({ status: 201, json: { success: true, message: "created", project: { name: posted.name } } });
    });
    await page.goto("/create");
    await page.getByLabel("Project Name").fill("qa-binary-limits");
    await page.getByRole("combobox").filter({ hasText: /Select a cluster/ }).click();
    await page.getByRole("option").first().click();
    await expect(page.getByText(/Memory: [\d.]+ GiB available/)).toBeVisible();
    await page.getByLabel("Max vCPUs *").fill("2");
    await page.getByLabel("Max Memory *").fill("8");
    await page.getByLabel("Max Storage *").fill("100");
    await page.getByRole("button", { name: "Create Project", exact: true }).click();
    await expect.poll(() => posted?.max_ram).toBe("8GiB");
    expect(posted.max_storage).toBe("100GiB");
  });
});
