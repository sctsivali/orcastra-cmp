const { test, expect } = require("@playwright/test");
const { authenticate, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const {
  expectCleanInventoryHarness,
  installInventoryHardeningFixtures,
} = require("./support/inventory-hardening-fixtures.cjs");
const { hypervisorRelease, platformNodes } = require("./support/api-fixture-data.cjs");

// The version verdict on a cluster card comes from the backend, per release track (ex; LXD 5.21.5
// is not "Latest" while 5.21/stable is at 5.21.9). The card also writes host capacity as CPU
// threads and explains vCPU allocation and GPUs behind an (i) that opens on hover, click, tap and
// the keyboard.
//
// Every negative below has a positive control with the same locator in another test.

const coreCard = (page) => page.locator('[data-slot="cluster-card"]').filter({ hasText: "Core Cluster" });

// cluster-a with the verdict, version and resources each test needs. Registered after the
// inventory fixture, so it answers first (Playwright matches routes in reverse order).
async function install(page, role, { release, serverVersion, clustered = false, resources } = {}) {
  const state = await installInventoryHardeningFixtures(page, role);
  await page.route(/\/api\/v1\/nodes(\?|$)/, (route) => {
    const body = platformNodes({ serverVersion, release });
    body.nodes[0].clustered = clustered;
    if (resources) body.nodes[0].resources = { ...body.nodes[0].resources, ...resources };
    return route.fulfill({ json: body });
  });
  return state;
}

const lts5219 = hypervisorRelease("latest", {
  track: "5.21 LTS",
  lts: true,
  latest_version: "5.21.9",
  channel: "5.21/stable",
  newer_track: { track: "6.x", version: "6.10.1", channel: "6/stable", lts: false },
});

const behind5215 = hypervisorRelease("update_available", {
  track: "5.21 LTS",
  lts: true,
  latest_version: "5.21.9",
  released_at: "2026-11-01T05:10:05+00:00",
  channel: "5.21/stable",
  newer_track: { track: "6.x", version: "6.10.1", channel: "6/stable", lts: false },
});

for (const role of ["admin", "partner"]) {
  test(`${role}: an LTS cluster on its newest release reads Latest LTS and names the feature track`, async ({ page, isMobile }) => {
    await authenticate(page.context(), role);
    const state = await install(page, role, { serverVersion: "5.21.9", release: lts5219 });
    await page.goto("/inventory");

    const card = coreCard(page);
    await expect(card.getByText("Latest LTS", { exact: true })).toBeVisible();
    await expect(card.getByText(/^Latest$/)).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Update Available" })).toHaveCount(0);

    const info = card.getByRole("button", { name: "About LXD 5.21.9", exact: true });
    if (isMobile) {
      await info.tap();
    } else {
      await info.click();
    }
    const panel = page.getByRole("dialog").filter({ hasText: "newest release on the 5.21 LTS track" });
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("(channel 5.21/stable)");
    await expect(panel).toContainText("LXD 6.10.1 is out on the 6.x feature track (channel 6/stable)");
    await expect(panel).toContainText("The 5.21 LTS track stays supported");
    await expect(panel).toContainText(/Checked \w{3} \d{1,2}, \d{4}/);
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await expectCleanInventoryHarness(state);
  });
}

test("admin: a cluster behind its track offers the update, with the track's real numbers", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", { serverVersion: "5.21.5", release: behind5215, clustered: true });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card.getByText(/^Latest( LTS)?$/)).toHaveCount(0);
  const update = card.getByRole("button", { name: "Update Available: LXD 5.21.9", exact: true });
  await expect(update).toHaveText("Update Available");
  await update.focus();
  await page.keyboard.press("Enter");

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "LXD Update Available" })).toBeVisible();
  await expect(dialog).toContainText("A newer release is out on the 5.21 LTS track, the track this Cluster follows.");
  await expect(dialog.locator("dd").filter({ hasText: "5.21.5" })).toHaveCount(1);
  await expect(dialog.locator("dd").filter({ hasText: "5.21.9" })).toHaveCount(1);
  await expect(dialog.locator("dd").filter({ hasText: "5.21/stable" })).toHaveCount(1);
  await expect(dialog.locator("dd").filter({ hasText: "Nov 1, 2026" })).toHaveCount(1);
  // a clustered LXD refreshes in a cohort so every member lands on the same revision
  await expect(dialog).toContainText('sudo snap refresh lxd --channel=5.21/stable --cohort="+"');
  await expect(dialog).toContainText("Run it on every cluster member, and only while all of them are online.");
  await expect(dialog.getByRole("link", { name: "Release Notes" })).toHaveAttribute(
    "href",
    "https://github.com/canonical/lxd/releases",
  );
  // no hand-typed release date appears
  await expect(dialog).not.toContainText("December 2025");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(update).toBeFocused();
  await expectCleanInventoryHarness(state);
});

test("admin: a standalone server's update command has no cohort and no member note", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", { serverVersion: "5.21.5", release: behind5215, clustered: false });
  await page.goto("/inventory");

  await coreCard(page).getByRole("button", { name: "Update Available: LXD 5.21.9", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("sudo snap refresh lxd --channel=5.21/stable");
  await expect(dialog).not.toContainText("--cohort");
  await expect(dialog).not.toContainText("every cluster member");
  await expectCleanInventoryHarness(state);
});

test("admin: a feed that could not be read says Not checked with the reason", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", {
    serverVersion: "5.21.5",
    release: hypervisorRelease("not_checked", { attempted_at: "2026-11-07T09:05:00+00:00" }),
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card.getByText("Not checked", { exact: true })).toBeVisible();
  await expect(card.getByText(/^(Latest|Latest LTS|Update Available)$/)).toHaveCount(0);
  await card.getByRole("button", { name: "Why the version was not checked", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(
    /The LXD release feed could not be read \(last tried Nov 7, 2026, .+\), so no verdict is shown\./,
  );
  await expectCleanInventoryHarness(state);
});

test("admin: a build ahead of stable says so instead of Latest", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", {
    serverVersion: "5.21.10",
    release: hypervisorRelease("ahead", { track: "5.21 LTS", lts: true, latest_version: "5.21.9", channel: "5.21/stable" }),
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card.getByText("Newer than stable", { exact: true })).toBeVisible();
  await expect(card.getByText(/^Latest( LTS)?$/)).toHaveCount(0);
  await expectCleanInventoryHarness(state);
});

test("admin: a check turned off by config shows nothing beside the version", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", { serverVersion: "5.21.5", release: null });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card).toContainText("Hypervisor Version: 5.21.5");
  await expect(card.getByText(/^(Latest|Latest LTS|Update Available|Not checked|Newer than stable)$/)).toHaveCount(0);
  await expectCleanInventoryHarness(state);
});

test("tenant: no version verdict and no host capacity reach the tenant's cards", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const state = await install(page, "tenant", { serverVersion: "5.21.5", release: behind5215 });
  await page.goto("/inventory");

  const clusters = page.getByRole("region", { name: "Clusters", exact: true });
  await expect(clusters.getByText("Core Cluster", { exact: true })).toBeVisible();
  await expect(clusters.locator('[data-slot="badge"]', { hasText: /^(Online|Unavailable)$/ }).first()).toBeVisible();
  await expect(page.getByText(/^(Latest|Latest LTS|Update Available|Not checked|Newer than stable)$/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "About vCPU allocation" })).toHaveCount(0);
  await expect(clusters).not.toContainText("CPU threads");
  await expectCleanInventoryHarness(state);
});

test("admin: an over-committed card says CPU threads and explains the ratio behind its (i)", async ({ page, isMobile }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", {
    resources: { cpu_used: 25, cpu_allocated: 25, cpu_total: 12, cpu_available: 0, cpu_allocation_ratio: 25 / 12 },
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card).toContainText("25 vCPUs / 12 CPU threads");
  await expect(card).toContainText("2.1x over-committed");
  await expect(card).not.toContainText(/hardware thread|allocated \(over-committed\)/i);

  const info = card.getByRole("button", { name: "About vCPU allocation", exact: true });
  if (!isMobile) {
    // a mouse preview opens on hover and leaves focus where it was
    await info.hover();
    const preview = page.getByRole("dialog").filter({ hasText: "Running instances on this cluster" });
    await expect(preview).toBeVisible();
    await expect(info).not.toBeFocused();
    await page.mouse.move(0, 0);
    await expect(preview).toHaveCount(0);
  }
  // keyboard: Enter opens it, Escape closes it and gives focus back
  await info.focus();
  await page.keyboard.press("Enter");
  const panel = page.getByRole("dialog").filter({ hasText: "Running instances on this cluster" });
  await expect(panel).toContainText("are given 25 vCPUs in total. Its hosts have 12 CPU threads, so the allocation is 2.1x.");
  await expect(panel).toContainText("cores × threads per core");
  await expect(panel).toContainText("Above 4x is flagged.");
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(info).toBeFocused();
  await expectCleanInventoryHarness(state);
});

test("admin: a cluster with only a display card says no GPU is for instances", async ({ page, isMobile }) => {
  await authenticate(page.context(), "admin");
  const display = {
    index: 0,
    id: 0,
    driver: "ast",
    vendor: "ASPEED Technology, Inc.",
    pci_address: "0000:02:00.0",
    gpu_class: "display",
    is_compute: false,
    in_use: false,
  };
  const state = await install(page, "admin", {
    resources: { gpu_total: 0, gpu_used: 0, gpu_available: 0, gpu_cards_total: 1, gpu_display_total: 1, gpu_cards: [display] },
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card).toContainText("No GPU for instances");
  await expect(card).toContainText("1 display card only");
  await expect(card).not.toContainText(/No compute GPU|card detected/);
  const info = card.getByRole("button", { name: "About GPUs for instances", exact: true });
  if (isMobile) {
    await info.tap();
  } else {
    await info.click();
  }
  await expect(page.getByRole("dialog")).toContainText("Display cards, such as a server's onboard VGA, are detected but cannot be allocated.");
  await expectCleanInventoryHarness(state);
});

test("admin: a rolling upgrade shows the version range and is judged by its oldest member", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", {
    serverVersion: "5.21.9",
    clustered: true,
    release: hypervisorRelease("update_available", {
      track: "5.21 LTS",
      lts: true,
      latest_version: "5.21.9",
      channel: "5.21/stable",
      mixed_versions: true,
      members: [
        { member: "mc2", version: "5.21.5" },
        { member: "mc1", version: "5.21.9" },
      ],
    }),
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card).toContainText("Hypervisor Version: 5.21.5 to 5.21.9");
  await card.getByRole("button", { name: "Update Available: LXD 5.21.9", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.locator("dt").filter({ hasText: "Oldest Member" })).toHaveCount(1);
  await expect(dialog.locator("dd").filter({ hasText: "5.21.5" })).toHaveCount(1);
  await expect(dialog).toContainText("The members run different versions (mc2 on 5.21.5, mc1 on 5.21.9), so this is for the oldest.");
  await expectCleanInventoryHarness(state);
});
