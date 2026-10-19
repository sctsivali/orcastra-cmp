const { test, expect } = require("@playwright/test");
const {
  authenticate,
  capture,
  countRequests,
  expectNoHorizontalOverflow,
} = require("./support/fixtures.cjs");
const {
  expectCleanInventoryHarness,
  installInventoryHardeningFixtures,
} = require("./support/inventory-hardening-fixtures.cjs");

// The overview also reads /clusters, /instances and /projects, which the inventory fixture
// serves; kernel answers come from the base fixture underneath it.
async function install(page, role, options = {}) {
  const state = await installInventoryHardeningFixtures(page, role, options);
  return state;
}

// Host kernel advisories are a Partner and Admin finding. The card badge opens every member's
// verdict, the alarm tile and the Kernel tab count the same rows, and a tenant never asks.

for (const role of ["admin", "partner"]) {
  test(`${role}: an exposed host kernel is flagged on its cluster card and in Warnings`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const fixture = await install(page, role, { kernelExposed: true });
    const api = fixture.base;

    await page.goto("/inventory");
    const badge = page.getByRole("button", {
      name: "Kernel Exposed: show host kernel advisory for Core Cluster",
      exact: true,
    });
    await expect(badge).toBeVisible();

    await badge.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("heading", { name: "Host Kernel Advisory" })).toBeVisible();
    await expect(dialog.getByText("CVE-2026-99999 (PwnEscape)", { exact: true })).toBeVisible();
    await expect(dialog.getByText("5.15.0-185-generic", { exact: true })).toBeVisible();
    await expect(dialog.getByText(/Linux CNA: 9\.3 Critical, Red Hat: 8\.8 Important, Ubuntu: Medium/)).toBeVisible();
    await expect(dialog.getByText(/linux-modules-\$\(uname -r\)\/changelog\.Debian\.gz/).first()).toBeVisible();
    const reference = dialog.getByRole("link", { name: /Kernel CVE announcement/ });
    await expect(reference).toHaveAttribute("href", "https://lists.openwall.net/linux-cve-announce/2026/10/15/2");
    await expect(reference).toHaveAttribute("target", "_blank");
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-kernel-dialog`);

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(badge).toBeFocused();

    // one request however many cards ask: they share a query key
    expect(countRequests(api, "/security/kernel-advisories")).toBe(1);

    const tile = page.getByRole("link", { name: /1\s*Host Kernels/ });
    await expect(tile).toHaveAttribute("href", "/inventory/warnings?tab=kernel");
    await tile.click();
    await expect(page).toHaveURL(/\/inventory\/warnings\?tab=kernel$/);
    await expect(page.getByRole("tab", { name: /Kernels/, selected: true })).toBeVisible();
    const row = page.getByRole("button", { name: "Show kernel advisory for cluster-a on Core Cluster", exact: true });
    await expect(row).toBeVisible();
    await row.click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: "cluster-a on Core Cluster" })).toBeVisible();
    await expect(page.getByRole("dialog").getByText("Exposed", { exact: true }).first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-kernel-tab`);

    await expectCleanInventoryHarness(fixture);
  });
}

test("admin: a patched cluster grows no badge on its card but reads as patched in System", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const fixture = await install(page, "admin");
  const api = fixture.base;

  await page.goto("/inventory");
  await expect(page.getByRole("heading", { name: "Core Cluster" })).toBeVisible();
  await expect.poll(() => countRequests(api, "/security/kernel-advisories")).toBe(1);
  await expect(page.getByRole("button", { name: /Kernel Exposed|Kernel Unverified/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /Host Kernels/ })).toHaveCount(0);

  // the System panel is where an operator reads kernels, so it states patched too
  await page.locator('[data-slot="cluster-card"]').filter({ hasText: "Core Cluster" })
    .getByRole("button", { name: "View Details", exact: true }).click();
  const details = page.getByRole("dialog");
  await expect(details.getByText("Kernel Advisory", { exact: true })).toBeVisible();
  await expect(details.getByRole("button", {
    name: "Kernel Patched: show host kernel advisory for Core Cluster",
    exact: true,
  })).toBeVisible();
  expect(countRequests(api, "/security/kernel-advisories")).toBe(1);

  await expectCleanInventoryHarness(fixture);
});

test("tenant: no kernel advisory is requested or shown", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const fixture = await install(page, "tenant", { kernelExposed: true });
  const api = fixture.base;

  await page.goto("/inventory");
  await expect(page.getByRole("region", { name: "Clusters", exact: true }).getByText("Core Cluster", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Kernel Exposed/ })).toHaveCount(0);
  expect(countRequests(api, "/security/kernel-advisories")).toBe(0);

  await page.goto("/inventory/warnings?tab=kernel");
  await expect(page).not.toHaveURL(/\/inventory\/warnings/);
  expect(countRequests(api, "/security/kernel-advisories")).toBe(0);

  await expectCleanInventoryHarness(fixture);
});
