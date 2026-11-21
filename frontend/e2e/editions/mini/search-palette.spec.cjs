const { test, expect } = require("@playwright/test");
const { authenticate, countRequests, expectCleanHarness, installApiFixtures } = require("../../support/fixtures.cjs");

// The header search and Ctrl+K palette find pages and the resources the caller's role can list,
// ranked by name. Each group reads the list page's own endpoint, so a group the role cannot open
// (Clusters, Operators for a Tenant) must not appear and must not read anything.

async function openTechnologies(page, role) {
  await authenticate(page.context(), role);
  const api = await installApiFixtures(page, role, { mini: { pages: true } });
  await page.goto("/technologies");
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toBeVisible({ timeout: 20_000 });
  return api;
}

function paletteOf(page) {
  return page.getByRole("dialog", { name: "Search" });
}

async function search(page, text) {
  const input = paletteOf(page).getByRole("combobox", { name: "Search" });
  await input.fill(text);
}

test("an Administrator finds pages, instances, projects, clusters and operators", async ({ page }) => {
  const api = await openTechnologies(page, "admin");
  await page.keyboard.press("Control+k");
  const palette = paletteOf(page);
  await expect(palette).toBeVisible();

  const goTo = palette.getByRole("group", { name: "Go to" });
  await expect(goTo.getByRole("option", { name: /Operators/ })).toBeVisible();
  await expect(goTo.getByRole("option", { name: /Audit Log/ })).toBeVisible();

  await search(page, "s");
  await expect(palette.getByText("Type one more character to search resources.")).toBeVisible();

  await search(page, "shop");
  await expect(palette.getByRole("group", { name: "Instances" }).getByRole("option", { name: /shop-web-01/ })).toBeVisible();
  await expect(palette.getByRole("group", { name: "Projects" }).getByRole("option", { name: /^shop/ })).toBeVisible();

  await search(page, "tina");
  await expect(palette.getByRole("group", { name: "Operators" }).getByRole("option", { name: /Tina Tenant/ })).toBeVisible();

  await search(page, "cluster-a");
  await expect(palette.getByRole("group", { name: "Clusters" }).getByRole("option")).toHaveCount(1);

  await search(page, "audit");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/audit-log$/);
  await expect(page.getByRole("heading", { level: 1, name: "Audit Log" })).toBeVisible();
  await expect(palette).toBeHidden();

  await expectCleanHarness(api);
});

test("a Partner sees Operators and Clusters but not the Administrator-only audit log", async ({ page }) => {
  const api = await openTechnologies(page, "partner");
  await page.keyboard.press("Control+k");
  const palette = paletteOf(page);

  const goTo = palette.getByRole("group", { name: "Go to" });
  await expect(goTo.getByRole("option", { name: /Operators/ })).toBeVisible();
  await expect(goTo.getByRole("option", { name: /Audit Log/ })).toHaveCount(0);

  await search(page, "tina");
  await expect(palette.getByRole("group", { name: "Operators" }).getByRole("option", { name: /Tina Tenant/ })).toBeVisible();
  await search(page, "cluster-a");
  await expect(palette.getByRole("group", { name: "Clusters" })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expectCleanHarness(api);
});

test("a Tenant finds its instances and projects and never reads clusters or operators", async ({ page }) => {
  const api = await openTechnologies(page, "tenant");
  const nodeReads = countRequests(api, "/nodes");
  await page.getByRole("button", { name: "Search pages and resources" }).click();
  const palette = paletteOf(page);
  await expect(palette).toBeVisible();

  const goTo = palette.getByRole("group", { name: "Go to" });
  await expect(goTo.getByRole("option", { name: /Operators/ })).toHaveCount(0);
  await expect(goTo.getByRole("option", { name: /Audit Log/ })).toHaveCount(0);

  await search(page, "shop");
  await expect(palette.getByRole("group", { name: "Instances" }).getByRole("option", { name: /shop-web-01/ })).toBeVisible();
  await search(page, "tina");
  await search(page, "cluster-a");
  await expect(palette.getByText(/Nothing you can see matches/)).toBeVisible();
  await expect(palette.getByRole("group", { name: "Clusters" })).toHaveCount(0);
  await expect(palette.getByRole("group", { name: "Operators" })).toHaveCount(0);

  expect(countRequests(api, "/nodes"), "the palette read no cluster list for a Tenant").toBe(nodeReads);
  expect(countRequests(api, "/operators"), "nor the operator list").toBe(0);
  await expectCleanHarness(api);
});
