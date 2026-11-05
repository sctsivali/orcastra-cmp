const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installInventoryHardeningFixtures, expectCleanInventoryHarness } = require("./support/inventory-hardening-fixtures.cjs");
const { fullInstance } = require("./support/api-fixture-data.cjs");

// The Overview search finds a project through one of its instance names, and the project card
// pages its instances five at a time without filtering them. Opened from that search, the card
// starts on the page holding the instance; it used to start on page 1, where the seventh
// instance of a busy project was not shown (RC5 acceptance, 2026-11-05).

const project = (host, name) => ({ name, description: "", config: {}, used_by: [], host, tags: [] });
const names = Array.from({ length: 7 }, (_, i) => `vm-${i + 1}`);

async function installBusyProject(page, role) {
  const state = await installInventoryHardeningFixtures(page, role);
  // note: registered after the inventory fixture, so these run first (Playwright matches routes
  // in reverse registration order).
  await page.route("**/api/v1/projects", (route) => route.fulfill({ json: [
    { host: "cluster-a", projects: [project("cluster-a", "tenant-a")], total: 1, reachable: true, error: null },
  ] }));
  await page.route(/\/api\/v1\/instances(\?|$)/, (route) => {
    const vms = names.map((name) => ({ ...fullInstance(), name }));
    return route.fulfill({ json: {
      total: vms.length, vms, nodes: ["cluster-a"], unreachable_nodes: [], node_errors: {},
      lxd_count: 1, microcloud_count: 0, incus_count: 0, offset: 0, limit: 0, has_more: false,
      collected_at: new Date().toISOString(),
    } });
  });
  return state;
}

test("a project card opened from an instance search starts on the page holding it", async ({ page, context }) => {
  await authenticate(context, "admin");
  const state = await installBusyProject(page, "admin");
  await page.goto("/inventory");

  await page.getByPlaceholder("Search projects by name, cluster, description, or instance").fill("vm-7");
  const show = page.getByRole("button", { name: /Show instances in tenant-a on cluster-a/ });
  await expect(show).toBeVisible({ timeout: 15_000 });
  await show.click();

  const rows = page.locator('[data-slot="project-instance-row"]');
  await expect(rows.filter({ hasText: "vm-7" })).toHaveCount(1);
  await expect(rows.filter({ hasText: "vm-1" })).toHaveCount(0);

  // Collapsing puts the card away: reopened without a search, it starts at the top again.
  await page.getByRole("button", { name: /Hide instances in tenant-a on cluster-a/ }).click();
  await page.getByPlaceholder("Search projects by name, cluster, description, or instance").fill("");
  await page.getByRole("button", { name: /Show instances in tenant-a on cluster-a/ }).click();
  await expect(rows.filter({ hasText: "vm-1" })).toHaveCount(1);
  await expectCleanInventoryHarness(state);
});
