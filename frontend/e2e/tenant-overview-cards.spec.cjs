const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const {
  expectCleanInventoryHarness,
  installInventoryHardeningFixtures,
} = require("./support/inventory-hardening-fixtures.cjs");

// A Tenant reads availability from its own cards, and a page must not ask for what the API refuses
// that role: GET /nodes/names answers a Tenant 403, a red line in the console on a page that works.

test("tenant: a cluster card puts its id under its name, not against it", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const state = await installInventoryHardeningFixtures(page, "tenant");
  await page.goto("/inventory");

  const section = page.locator("section[aria-labelledby='tenant-clusters-heading']");
  const name = section.getByText("Core Cluster", { exact: true });
  await expect(name).toBeVisible({ timeout: 15_000 });
  const id = section.getByText("cluster-a", { exact: true });
  await expect(id).toBeVisible();

  const nameBox = await name.boundingBox();
  const idBox = await id.boundingBox();
  expect(idBox.y, "the id reads on its own line").toBeGreaterThanOrEqual(nameBox.y + nameBox.height - 1);
  expect(Math.abs(idBox.x - nameBox.x), "both lines start at the same edge").toBeLessThanOrEqual(1);
  await expectCleanInventoryHarness(state);
});

for (const [path, heading] of [["/inventory/images", "Images"], ["/inventory/iso-images", "ISO Images"]]) {
  test(`tenant: ${path} does not ask for the cluster names`, async ({ page, context }) => {
    await authenticate(context, "tenant");
    await installInventoryHardeningFixtures(page, "tenant");
    await page.route(/\/api\/v1\/images(\?|$)/, (route) => route.fulfill({ json: [
      { host: "cluster-a", project: "default", total: 0, images: [], reachable: true, error: null },
    ] }));
    await page.route(/\/api\/v1\/storage\/volumes(\?|$)/, (route) => route.fulfill({ json: {
      total: 0, volumes: [], nodes: ["cluster-a"], unreachable_nodes: [],
    } }));
    const refused = [];
    // Answered the way the API answers this role, so a page that asks still renders.
    await page.route("**/api/v1/nodes/names", (route) => {
      refused.push(route.request().url());
      return route.fulfill({ status: 403, json: { detail: "Requires partner role" } });
    });

    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, exact: true }).first()).toBeVisible({ timeout: 15_000 });
    await page.waitForLoadState("networkidle");
    expect(refused, "a route that refuses this role").toEqual([]);
  });
}
