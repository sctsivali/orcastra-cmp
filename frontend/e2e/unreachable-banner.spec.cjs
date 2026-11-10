const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installInventoryHardeningFixtures } = require("./support/inventory-hardening-fixtures.cjs");
const { fullInstance } = require("./support/api-fixture-data.cjs");

const list = (role, unreachable, reasons = {}) => ({
  total: 1, vms: [fullInstance()], nodes: ["cluster-a"], unreachable_nodes: unreachable,
  node_errors: role === "tenant" ? {} : reasons,
  lxd_count: 1, microcloud_count: 0, incus_count: 0, offset: 0, limit: 0, has_more: false,
  collected_at: new Date().toISOString(),
});

async function open(page, context, role, respond) {
  await authenticate(context, role);
  await installInventoryHardeningFixtures(page, role);
  await page.route(/\/api\/v1\/instances(\?|$)/, (route) => route.fulfill({ json: respond() }));
  await page.goto("/inventory/instances");
  const banner = page.locator('[data-slot="unreachable-banner"]');
  await expect(banner).toBeVisible();
  return banner;
}

const clusters = (count) => Array.from({ length: count }, (_, i) => `qa-down-${String(count - i).padStart(3, "0")}`);

for (const role of ["admin", "partner"]) {
  test(`${role}: the banner is one sentence until the operator asks for the clusters`, async ({ page, context }) => {
    const names = ["bliss001", "center-1", "nuc1"];
    const banner = await open(page, context, role, () => list(role, names, {
      bliss001: "unreachable: no connection to the cluster",
      "center-1": "unreachable: did not answer within 30s",
    }));

    await expect(banner.getByRole("status")).toHaveText(
      "Could not reach 3 clusters. Their instances are not shown, so this list may be incomplete.",
    );
    await expect(banner).not.toContainText("bliss001");
    const toggle = banner.getByRole("button", { name: "Show Details" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");

    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(banner.getByRole("button", { name: "Hide Details" })).toHaveAttribute("aria-expanded", "true");
    const rows = banner.getByRole("listitem");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toHaveText("bliss001no connection to the cluster");
    await expect(rows.nth(1)).toHaveText("center-1did not answer within 30s");
    await expect(rows.nth(2)).toHaveText("nuc1");
    await expect(banner).not.toContainText("no reason reported");
    await expect(banner).not.toContainText("unreachable:");
  });
}

test("a single cluster keeps the same short shape", async ({ page, context }) => {
  const banner = await open(page, context, "admin", () => list("admin", ["nuc2"], { nuc2: "unreachable: read timed out" }));
  await expect(banner.getByRole("status")).toContainText("Could not reach 1 cluster.");
  await expect(banner).not.toContainText("nuc2");
  await expect(banner.getByRole("button", { name: "Show Details" })).toBeVisible();
});

test("two hundred clusters scroll inside the banner, sorted, and stay open through a refetch", async ({ page, context }) => {
  let names = clusters(200);
  const banner = await open(page, context, "admin", () => list("admin", names));
  await banner.getByRole("button", { name: "Show Details" }).click();

  const details = banner.getByRole("list");
  await expect(banner.getByRole("listitem")).toHaveCount(200);
  await expect(banner.getByRole("listitem").first()).toHaveText("qa-down-001");
  const box = await details.boundingBox();
  expect(box.height, "the list must not push the page away").toBeLessThanOrEqual(241);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);

  await details.focus();
  await expect(details).toBeFocused();
  await page.keyboard.press("End");
  await expect.poll(() => details.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);

  names = clusters(199);
  await page.getByRole("button", { name: "Refresh instances" }).click();
  await expect(banner.getByRole("status")).toContainText("Could not reach 199 clusters.");
  await expect(banner.getByRole("listitem")).toHaveCount(199);
});

test("a reason cannot be rendered as markup", async ({ page, context }) => {
  const banner = await open(page, context, "admin", () => list("admin", ["nuc2"], {
    nuc2: 'error: <img src=x onerror="window.__owned=1"> and a very long unbroken '.padEnd(400, "x"),
  }));
  await banner.getByRole("button", { name: "Show Details" }).click();
  await expect(banner.getByRole("listitem")).toContainText("<img src=x");
  await expect(banner.locator("img")).toHaveCount(0);
  expect(await page.evaluate(() => window.__owned)).toBeUndefined();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

for (const [role, sentence] of [
  ["admin", "Could not fully read 1 cluster. Some of its networks are not shown, so this list may be incomplete."],
  ["tenant", "Some networks could not be loaded right now, so this list may be incomplete."],
]) {
  test(`${role}: a cluster that lost rows from the network list is not silent`, async ({ page, context }) => {
    await authenticate(context, role);
    await installInventoryHardeningFixtures(page, role);
    await page.route(/\/api\/v1\/networks(\?|$)/, (route) => route.fulfill({ json: {
      total: 0, networks: [], nodes: ["cluster-a"], unreachable_nodes: [], partial_nodes: ["cluster-a"],
      node_errors: role === "tenant" ? {} : { "cluster-a": "partial: the network list did not finish" },
    } }));
    await page.goto("/inventory/networks");
    await expect(page.locator('[data-slot="unreachable-banner"]').getByRole("status")).toHaveText(sentence);
  });
}

// One card per list. Two stacked banners, "2" in one and "1" in the other, would never say the
// list is short by three clusters.
test("admin: an unreachable and a partly read cluster share one banner", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installInventoryHardeningFixtures(page, "admin");
  await page.route(/\/api\/v1\/networks(\?|$)/, (route) => route.fulfill({ json: {
    total: 0, networks: [], nodes: ["cluster-a"], unreachable_nodes: ["cluster-c", "cluster-b"], partial_nodes: ["cluster-a"],
    node_errors: {
      "cluster-a": "partial: the network list did not finish",
      "cluster-b": "unreachable: no connection to the cluster",
    },
  } }));
  await page.goto("/inventory/networks");
  const banner = page.locator('[data-slot="unreachable-banner"]');
  await expect(banner).toHaveCount(1);
  await expect(banner.getByRole("status")).toHaveText(
    "Could not reach 2 clusters and could not fully read 1. Some networks are not shown, so this list may be incomplete.",
  );
  await banner.getByRole("button", { name: "Show Details" }).click();
  const rows = banner.getByRole("listitem");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toHaveText("cluster-aPartly read · the network list did not finish");
  await expect(rows.nth(1)).toHaveText("cluster-bno connection to the cluster");
  await expect(rows.nth(2)).toHaveText("cluster-c");
});

test("a tenant gets the sentence and nothing to open", async ({ page, context }) => {
  const banner = await open(page, context, "tenant", () => list("tenant", ["secret-cluster"]));
  await expect(banner.getByRole("status")).toHaveText(
    "Some instances could not be loaded right now, so this list may be incomplete.",
  );
  await expect(banner.getByRole("button")).toHaveCount(0);
  await expect(banner).not.toContainText("secret-cluster");
  await expect(page.locator("body")).not.toContainText("secret-cluster");
});
