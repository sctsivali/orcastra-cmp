const { test, expect } = require("@playwright/test");
const { authenticate, expectCleanHarness, expectNoHorizontalOverflow, installApiFixtures } = require("./support/fixtures.cjs");

// Rebuild erases a root disk. A Tenant reaches it only with the Rebuild grant (beside View and
// Operate), on a deployment that has not turned tenant rebuild off, and never in the empty mode it
// could not boot from. Every role confirms twice: a ticked acknowledgement and the name typed out.

const MENU = "Actions for instance tenant-owned-vm in tenant-a on cluster-a";
const GRANTED = ["view", "operate", "console", "rebuild"];

async function stubDialogReads(page, { refusal } = {}) {
  const posts = [];
  await page.route(/\/api\/v1\/instances\/tenant-owned-vm\/rebuild-preflight(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { instance_name: "tenant-owned-vm", host: "cluster-a", status: "Running", type: "virtual-machine", snapshots: [], attached_volumes: [], base_image: "ubuntu 24.04" } }));
  await page.route(/\/api\/v1\/images\/cluster-a(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { images: [{ fingerprint: "a".repeat(64), aliases: ["ubuntu/24.04"], type: "virtual-machine", size: 1024 ** 3, description: "Ubuntu 24.04" }] } }));
  await page.route(/\/api\/v1\/images\/remote(?:\?.*)?$/, (route) => route.fulfill({ json: { images: [] } }));
  await page.route(/\/api\/v1\/instances\/tenant-owned-vm\/rebuild(?:\?.*)?$/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts.push(route.request().postDataJSON());
    if (refusal) return route.fulfill({ status: refusal.status, headers: refusal.headers ?? {}, json: { detail: refusal.detail } });
    return route.fulfill({ status: 500, json: { detail: "stopped by the spec" } });
  });
  return posts;
}

async function openMenu(page) {
  await page.goto("/inventory/instances");
  await page.getByRole("button", { name: MENU }).click();
}

test("a tenant with the Rebuild grant gets the item, a hard warning and no empty mode", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true, tenantPermissions: GRANTED, tenantRebuildEnabled: true });
  const posts = await stubDialogReads(page, {
    refusal: {
      status: 429,
      headers: { "Retry-After": "2040" },
      detail: { error: "rebuild_budget", message: "This instance was rebuilt 3 times in the last hour. You can rebuild it again in 34 minutes.", retry_after_seconds: 2040 },
    },
  });
  await openMenu(page);
  await page.getByRole("menuitem", { name: /Rebuild Instance/ }).click();

  const dialog = page.getByRole("dialog", { name: /Rebuild Instance/ });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Image/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Cloud-init/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /Empty \/ ISO/ })).toHaveCount(0);
  await expect(dialog.getByText("Everything on the root disk is erased permanently")).toBeVisible();
  await expect(dialog.getByText(/new SSH host key/)).toBeVisible();

  const submit = dialog.getByRole("button", { name: /^Rebuild$/ });
  await expect(submit).toBeDisabled();
  await dialog.getByRole("combobox").first().click();
  await page.getByRole("option", { name: /ubuntu\/24\.04/ }).click();
  await dialog.getByLabel("Type tenant-owned-vm to confirm").fill("tenant-owned-vm");
  await expect(submit).toBeDisabled();
  await dialog.getByRole("checkbox", { name: /I understand that everything on the root disk/ }).check();
  await expect(submit).toBeEnabled();

  await submit.click();
  await expect(dialog.getByText(/Rebuild not started\. This instance was rebuilt 3 times in the last hour\. You can rebuild it again in 34 minutes\./)).toBeVisible();
  // Nothing started, so the form stays as filled in and the dialog can close.
  await expect(submit).toBeEnabled();
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({ mode: "image", confirm_name: "tenant-owned-vm", image: "a".repeat(64) });
  await expectNoHorizontalOverflow(page);
  await expectCleanHarness(api);
});

test("a tenant without the Rebuild grant is not offered it", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true, tenantRebuildEnabled: true });
  await openMenu(page);
  await expect(page.getByRole("menuitem", { name: /Start Instance|Stop Instance/ }).first()).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /Rebuild Instance/ })).toHaveCount(0);
  await expectCleanHarness(api);
});

test("a deployment that turned tenant rebuild off hides it from a granted tenant", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true, tenantPermissions: GRANTED, tenantRebuildEnabled: false });
  await openMenu(page);
  await expect(page.getByRole("menuitem", { name: /Start Instance|Stop Instance/ }).first()).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /Rebuild Instance/ })).toHaveCount(0);
  await expectCleanHarness(api);
});

test("a partner keeps every mode, the empty one included", async ({ page, context }) => {
  await authenticate(context, "partner");
  const api = await installApiFixtures(page, "partner", { snapshotInstancePage: true });
  await stubDialogReads(page);
  await openMenu(page);
  await page.getByRole("menuitem", { name: /Rebuild Instance/ }).click();
  const dialog = page.getByRole("dialog", { name: /Rebuild Instance/ });
  await expect(dialog.getByRole("button", { name: /Empty \/ ISO/ })).toBeVisible();
  await dialog.getByRole("button", { name: /Empty \/ ISO/ }).click();
  await expect(dialog.getByText(/The new root disk is blank/)).toBeVisible();
  await expectCleanHarness(api);
});

test("a tenant without Console is not offered cloud-init, which runs as root", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotInstancePage: true, tenantPermissions: ["view", "operate", "rebuild"], tenantRebuildEnabled: true });
  await stubDialogReads(page);
  await openMenu(page);
  await page.getByRole("menuitem", { name: /Rebuild Instance/ }).click();
  const dialog = page.getByRole("dialog", { name: /Rebuild Instance/ });
  await expect(dialog.getByRole("button", { name: /^Image/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Cloud-init/ })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: /Empty \/ ISO/ })).toHaveCount(0);
  await expectCleanHarness(api);
});
