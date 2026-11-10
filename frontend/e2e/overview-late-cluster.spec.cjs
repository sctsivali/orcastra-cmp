const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installInventoryHardeningFixtures, expectCleanInventoryHarness } = require("./support/inventory-hardening-fixtures.cjs");
const { fullInstance } = require("./support/api-fixture-data.cjs");

// One cluster past the backend's 30 s inventory wall. The instance list answers with the other
// clusters and names the late one; the Overview does not wait for it.

const LATE = "cluster-b";
const REASON = "unreachable: did not answer within 30s";
const SHOWN_REASON = "did not answer within 30s";

const project = (host, name) => ({ name, description: "", config: {}, used_by: [], host, tags: [] });

function lateList(role) {
  const vms = [fullInstance()];
  return {
    total: vms.length,
    vms,
    nodes: ["cluster-a"],
    unreachable_nodes: [LATE],
    // The server leaves the reason out for a Tenant, as every fan-out list does.
    node_errors: role === "tenant" ? {} : { [LATE]: REASON },
    lxd_count: 1,
    microcloud_count: 0,
    incus_count: 0,
    offset: 0,
    limit: 0,
    has_more: false,
    collected_at: new Date().toISOString(),
  };
}

async function installLateCluster(page, role, options = {}) {
  const state = await installInventoryHardeningFixtures(page, role, options);
  let release = () => {};
  const held = new Promise((resolve) => { release = resolve; });
  const control = { held: true, release: () => { control.held = false; release(); }, instanceReads: 0 };
  // Registered after the inventory fixture, so these run first (Playwright matches routes
  // in reverse registration order).
  await page.route("**/api/v1/projects", (route) => route.fulfill({ json: [
    { host: "cluster-a", projects: [project("cluster-a", "tenant-a")], total: 1, reachable: true, error: null },
    { host: LATE, projects: [project(LATE, "web")], total: 1, reachable: true, error: null },
  ] }));
  // A regex, because the page's refetch carries a query string a glob would not match.
  await page.route(/\/api\/v1\/instances(\?|$)/, async (route) => {
    control.instanceReads += 1;
    if (control.held) await held;
    return route.fulfill({ json: lateList(role) });
  });
  return { state, control };
}

const projectCard = (page, name) =>
  page.locator('[data-row-key]').filter({ has: page.getByText(name, { exact: true }) }).first();

for (const role of ["admin", "partner"]) {
  test(`${role}: the Overview renders before the instance list and names the late cluster`, async ({ page, context }) => {
    await authenticate(context, role);
    const { state, control } = await installLateCluster(page, role);
    await page.goto("/inventory");

    // Before the instance list answers: clusters and projects are on the page, the instance
    // figures and the project counts say they are loading.
    await expect(page.getByText("Registered Clusters", { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => control.instanceReads).toBeGreaterThan(0);
    await expect(page.getByText("Loading Total Instances", { exact: true })).toBeAttached();
    await expect(projectCard(page, "web").getByText("Loading the instance count", { exact: true })).toBeAttached();
    await expect(page.getByText("This is taking longer than usual")).toHaveCount(0);

    control.release();

    await expect(page.getByText("Loading Total Instances", { exact: true })).toHaveCount(0);
    // Its projects answered and its instances did not, so the Overview's one banner counts it as
    // read in part rather than unreachable.
    const banner = page.locator('[data-slot="unreachable-banner"]');
    await expect(banner).toHaveCount(1);
    await expect(banner.getByRole("status")).toHaveText(
      "Could not fully read 1 cluster. Some of its instances are not shown, so this list may be incomplete.",
    );
    await banner.getByRole("button", { name: "Show Details" }).click();
    await expect(banner).toContainText(LATE);
    await expect(banner).toContainText("Partly read");
    await expect(banner).toContainText(SHOWN_REASON);
    // The late cluster's project has an unknown count, never 0, and cannot be deleted from here.
    await expect(projectCard(page, "web").getByLabel(/^Instance count not read/)).toBeVisible();
    await expect(projectCard(page, "tenant-a").getByText("1", { exact: true })).toBeVisible();

    await projectCard(page, "web").getByRole("button", { name: "Project actions for web" }).click();
    await page.getByRole("menuitem", { name: "Delete Project" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("could not be counted");
    await expect(dialog.getByRole("button", { name: /^Delete/ })).toBeDisabled();
    await expectCleanInventoryHarness(state);
  });
}

test("admin: the Instances page gives the late cluster's reason", async ({ page, context }) => {
  await authenticate(context, "admin");
  // The Instances page follows its clusters' events, which instanceListPage admits.
  const { state, control } = await installLateCluster(page, "admin", { instanceListPage: true });
  control.release();
  await page.goto("/inventory/instances");
  const banner = page.locator('[data-slot="unreachable-banner"]').filter({ hasText: "Its instances are not shown" });
  await expect(banner).toContainText("Could not reach 1 cluster.");
  await banner.getByRole("button", { name: "Show Details" }).click();
  await expect(banner).toContainText(SHOWN_REASON);
  await expectCleanInventoryHarness(state);
});

test("tenant: the Overview says instances are missing without the reason", async ({ page, context }) => {
  await authenticate(context, "tenant");
  const { state, control } = await installLateCluster(page, "tenant");
  control.release();
  await page.goto("/inventory");
  await expect(page.getByText("Some projects and instances could not be loaded right now, so this list may be incomplete.")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/did not answer within/)).toHaveCount(0);
  await expectCleanInventoryHarness(state);
});
