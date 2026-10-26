const { test, expect } = require("@playwright/test");
const { authenticate, expectNoHorizontalOverflow, capture } = require("./support/fixtures.cjs");
const { installInventoryHardeningFixtures, expectCleanInventoryHarness, serverRows } = require("./support/inventory-hardening-fixtures.cjs");

for (const role of ["admin", "partner"]) {
  test(`${role}: server pagination handles boundaries, page sizes, shrink, and unreachable sources`, async ({ page, context }, testInfo) => {
    await authenticate(context, role);
    const state = await installInventoryHardeningFixtures(page, role);
    await page.goto("/inventory/server");
    const cards = page.getByRole("heading", { name: /^qa-server-/ });
    await expect(cards).toHaveCount(12);
    await expect(page.getByText(/Could not reach 1/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Go to previous page" })).toBeDisabled();
    await page.getByRole("button", { name: "Go to last page" }).click();
    await expect(cards).toHaveCount(1);
    await expect(cards.first()).toHaveText("qa-server-025");
    await expect(page.getByRole("button", { name: "Go to next page" })).toBeDisabled();

    // A refresh which removes the last page must clamp immediately to the last valid page.
    state.servers = serverRows(13);
    await page.getByRole("button", { name: "Refresh server information", exact: true }).click();
    await expect(cards.first()).toHaveText("qa-server-013");
    await expect(page.getByRole("spinbutton", { name: "Page number", exact: true })).toHaveValue("2");
    await page.getByRole("combobox", { name: "Rows per page" }).click();
    for (const size of [12, 24, 48, 96]) {
      await expect(page.getByRole("option", { name: String(size), exact: true })).toBeVisible();
    }
    await page.getByRole("option", { name: "24", exact: true }).click();
    await expect(cards).toHaveCount(13);
    await expect(page.getByRole("button", { name: "Go to next page" })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-server-pagination`);

    state.servers = [];
    await page.getByRole("button", { name: "Refresh server information", exact: true }).click();
    await expect(page.getByText("No server details to show", { exact: true })).toBeVisible();
    await expect(cards).toHaveCount(0);
    await expectCleanInventoryHarness(state);
  });

  test(`${role}: a failed server read has a retryable error rather than an empty fleet`, async ({ page, context }) => {
    await authenticate(context, role);
    const state = await installInventoryHardeningFixtures(page, role);
    state.serverFailed = true;
    await page.goto("/inventory/server");
    await expect(page.getByText("Could not load server details", { exact: true })).toBeVisible();
    await expect(page.getByText("No server details to show", { exact: true })).toHaveCount(0);
    state.serverFailed = false;
    await page.getByRole("button", { name: "Refresh server information", exact: true }).click();
    await expect(page.getByRole("heading", { name: /^qa-server-/ })).toHaveCount(12);
    await expectCleanInventoryHarness(state);
  });
}

test("tenant: forbidden server deep link never issues a privileged server read", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const state = await installInventoryHardeningFixtures(page, "tenant");
  await page.goto("/inventory/server");
  await expect(page).toHaveURL(new URL("/", process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025").href);
  await expect(page.getByRole("heading", { name: "Server Information", exact: true })).toHaveCount(0);
  expect(state.requests.filter((entry) => entry.path === "/server")).toEqual([]);
  await expectCleanInventoryHarness(state);
});

test("tenant: overview shows assigned cluster information without rename or tag controls", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const state = await installInventoryHardeningFixtures(page, "tenant");
  await page.goto("/inventory");
  await expect(
    page.getByRole("region", { name: "Clusters", exact: true })
      .getByText("Core Cluster", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Actions for cluster cluster-a", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /add tag/i })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "Edit Name", exact: true })).toHaveCount(0);
  await expectCleanInventoryHarness(state);
});

for (const role of ["admin", "partner"]) {
  test(`${role}: storage capacity distinguishes zero, missing, fractional, and invalid readings`, async ({ page, context }, testInfo) => {
    await authenticate(context, role);
    const state = await installInventoryHardeningFixtures(page, role);
    await page.goto("/inventory/storage-pools");
    await expect(page.getByRole("heading", { name: "Storage Pools", exact: true })).toBeVisible();
    const poolRow = (name) => page.getByRole("row").filter({ has: page.getByRole("cell", { name, exact: true }) });
    for (const name of ["object", "missing", "negative", "invalid"]) {
      await expect(poolRow(name).getByRole("cell", { name: "Usage unavailable", exact: true })).toBeVisible();
      await expect(poolRow(name).getByRole("button", { name: /capacity breakdown/ })).toHaveCount(0);
    }
    await expect(poolRow("zero").getByText("0 B / 0 B", { exact: true })).toBeVisible();
    await expect(poolRow("fractional").getByText("0.5 B / 1 B", { exact: true })).toBeVisible();
    await expect(page.locator("main")).not.toContainText(/NaN|undefined|Infinity/);
    await poolRow("healthy").getByRole("button", { name: /capacity breakdown/ }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Space", { exact: true })).toBeVisible();
    await expect(dialog.getByText("Inodes", { exact: true })).toBeVisible();
    await expect(dialog.getByText("50%", { exact: true })).toBeVisible();
    await expect(dialog).not.toContainText(/NaN|undefined|Infinity/);
    await capture(page, testInfo, `${role}-storage-capacity`);
    await page.keyboard.press("Escape");
    await expectNoHorizontalOverflow(page);
    await expectCleanInventoryHarness(state);
  });
}

// A pool is shared by every party on its cluster, so its driver, state and capacity are the
// provider's (owner decision 2026-10-26). A Tenant sees which pools exist and what uses them,
// and its page never asks for the capacity read the backend refuses it.
test("tenant: storage pools list names and usage, no driver, status or capacity", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const state = await installInventoryHardeningFixtures(page, "tenant");
  await page.goto("/inventory/storage-pools");
  await expect(page.getByRole("heading", { name: "Storage Pools", exact: true })).toBeVisible();
  await expect(page.getByText("Storage pools your instances use")).toBeVisible();
  await expect(page.getByRole("cell", { name: "healthy", exact: true })).toBeVisible();
  const head = page.locator("main thead");
  await expect(head).toContainText("Used By");
  for (const header of ["Driver", "Size (Used / Total)", "Status"]) {
    await expect(head, `${header} is operator detail`).not.toContainText(header);
  }
  await expect(page.locator("main")).not.toContainText(/Usage unavailable|capacity breakdown|NaN|undefined/);
  await page.waitForTimeout(1_000);
  expect(
    state.requests.filter((entry) => entry.path === "/storage/pools/resources-all"),
    "a tenant never asks for pool capacity",
  ).toEqual([]);
  await expectNoHorizontalOverflow(page);
  await expectCleanInventoryHarness(state);
});
