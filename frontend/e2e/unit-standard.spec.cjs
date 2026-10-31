const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// One size standard on every screen, and the thing that makes it risky: a field that shows 8.59 GB
// for a limit stored as 8GiB must not write 8590000000 when nobody touched it.

const gib = 1024 ** 3;

async function openSpecificationDialog(page, context) {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
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

test("a disk created in GiB reads in GB in the table", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await page.goto("/inventory/instances");
  const row = page.getByRole("row", { name: /tenant-owned-vm/ });
  await expect(row).toContainText("85.9 GB");
  await expect(row).not.toContainText("80GiB");
});

test("a size field offers the standard units and shows the stored value in them", async ({ page, context }) => {
  const { dialog } = await openSpecificationDialog(page, context);
  await expect(dialog.getByRole("spinbutton", { name: "Memory limit", exact: true })).toHaveValue("8.59");
  await expect(dialog.getByRole("spinbutton", { name: "Root disk size", exact: true })).toHaveValue("85.9");

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
  await dialog.getByRole("spinbutton", { name: "Memory limit", exact: true }).fill("4");
  await dialog.getByRole("button", { name: "Review and apply" }).click();

  await expect.poll(() => previews.length).toBeGreaterThan(0);
  expect(previews[0].memory_bytes).toBe(4_000_000_000);
});

test("typing the same figure back is still the stored value", async ({ page, context }) => {
  const { dialog } = await openSpecificationDialog(page, context);
  const memory = dialog.getByRole("spinbutton", { name: "Memory limit", exact: true });
  await memory.fill("9");
  await expect(dialog.getByRole("button", { name: "Review and apply" })).toBeEnabled();
  await memory.fill("8.59");
  await expect(dialog.getByRole("button", { name: "Review and apply" }), "nothing is left to apply").toBeDisabled();
});
