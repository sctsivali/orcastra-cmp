const { test, expect } = require("@playwright/test");
const { authenticate, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const {
  expectCleanInventoryHarness,
  installInventoryHardeningFixtures,
} = require("./support/inventory-hardening-fixtures.cjs");
const { hypervisorRelease, platformNodes } = require("./support/api-fixture-data.cjs");

// The version verdict on a cluster card comes from the backend, per release track (ex; LXD 5.21.5
// is not "Latest" while 5.21/stable is at 5.21.9). The card also writes host capacity as CPU
// threads, explains vCPU allocation and storage behind an (i) that opens on hover, click, tap and
// the keyboard, and draws vCPU, Memory and Storage on the one usage ramp.
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
  await expect(page.getByRole("button", { name: "About cluster storage" })).toHaveCount(0);
  await expect(clusters).not.toContainText("CPU threads");
  await expect(clusters.getByRole("meter")).toHaveCount(0);
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
  // Measured against the 4x ceiling: 2.1x is about half full and still green, not a full bar.
  const meter = card.getByRole("meter", { name: /^vCPU allocation:/ });
  await expect(meter).toHaveAttribute("aria-valuenow", "52");
  await expect(meter).toHaveAttribute("data-level", "normal");
  await expect(meter.locator("> div")).toHaveClass(/bg-success/);
  await expect(card).not.toContainText("· High");

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
  await expect(panel).toContainText("The bar fills at 4x, the usual ceiling for general workloads. It turns amber from 3x and red above 4x.");
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(info).toBeFocused();
  await expectCleanInventoryHarness(state);
});

// One case per band edge on each meter. `fill` is the bar's class, `caption` the number's.
const BANDS = [
  { level: "normal", fill: /bg-success/, caption: /text-muted-foreground/, word: null },
  { level: "high", fill: /bg-status-pending/, caption: /text-status-pending/, word: "· High" },
  { level: "critical", fill: /bg-destructive/, caption: /text-status-offline/, word: "· Critical" },
];

for (const [label, share, band] of [
  ["just under amber", 0.749, BANDS[0]],
  ["at amber", 0.75, BANDS[1]],
  ["just under red", 0.899, BANDS[1]],
  ["at red", 0.9, BANDS[2]],
  ["thin-provisioned past full", 1.3, BANDS[2]],
]) {
  test(`admin: storage ${label} (${share * 100}%) takes the ${band.level} band on the card`, async ({ page }) => {
    await authenticate(page.context(), "admin");
    const total = 2 * 1024 ** 4;
    const used = Math.round(total * share);
    const state = await install(page, "admin", {
      resources: { storage_total: total, storage_used: used, storage_available: Math.max(0, total - used) },
    });
    await page.goto("/inventory");

    const card = coreCard(page);
    await expect(card).toContainText("Storage");
    await expect(card).not.toContainText(/No GPU for instances|display card|compute GPU/);
    await expect(card.getByRole("button", { name: "About GPUs for instances" })).toHaveCount(0);
    const meter = card.getByRole("meter", { name: /^Storage:/ });
    const percent = Math.min(100, share * 100);
    await expect(meter).toHaveAttribute("aria-valuenow", String(Math.round(percent)));
    await expect(meter).toHaveAttribute("data-level", band.level);
    await expect(meter.locator("> div")).toHaveClass(band.fill);
    const caption = card.locator('[data-level]:not([role="meter"])', { hasText: `${percent.toFixed(1)}% used` }).last();
    await expect(caption).toHaveClass(band.caption);
    if (band.word) await expect(caption).toContainText(band.word);
    else await expect(caption).not.toContainText(/· (High|Critical)/);
    await expect(card).not.toContainText(/NaN|Infinity|undefined/);
    await expectCleanInventoryHarness(state);
  });
}

for (const [label, ratio, band, valuenow] of [
  ["within threads", 0.9, BANDS[0], 23],
  ["a normal over-commit", 1.4, BANDS[0], 35],
  ["just under amber", 2.99, BANDS[0], 75],
  ["at amber", 3, BANDS[1], 75],
  ["at the ceiling", 4, BANDS[1], 100],
  ["past the ceiling", 4.01, BANDS[2], 100],
]) {
  test(`admin: vCPU ${label} (${ratio}x) takes the ${band.level} band on the card`, async ({ page }) => {
    await authenticate(page.context(), "admin");
    const threads = 100;
    const allocated = Math.round(threads * ratio);
    const state = await install(page, "admin", {
      resources: {
        cpu_total: threads,
        cpu_used: allocated,
        cpu_allocated: allocated,
        cpu_available: Math.max(0, threads - allocated),
        cpu_allocation_ratio: ratio,
      },
    });
    await page.goto("/inventory");

    const card = coreCard(page);
    const meter = card.getByRole("meter", { name: /^vCPU allocation:/ });
    // 2.99x is 74.75% of the ceiling: rounded for the value, still green for the band.
    await expect(meter).toHaveAttribute("aria-valuenow", String(valuenow));
    await expect(meter).toHaveAttribute("data-level", band.level);
    await expect(meter.locator("> div")).toHaveClass(band.fill);
    const caption = card.locator('[data-level]:not([role="meter"])', { hasText: `${ratio.toFixed(1)}x` }).first();
    await expect(caption).toHaveClass(band.caption);
    if (band.level === "high") await expect(caption).toContainText("· High");
    // Past the ceiling the words already name the state; the band is not repeated after them.
    if (band.level === "critical") {
      await expect(caption).toContainText("heavily over-committed");
      await expect(caption).not.toContainText("· Critical");
    }
    if (band.level === "normal") await expect(caption).not.toContainText(/· (High|Critical)/);
    await expectCleanInventoryHarness(state);
  });
}

test("admin: storage that could not be read says so instead of drawing an empty disk", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", {
    resources: {
      storage_total: 0,
      storage_used: 0,
      storage_available: 0,
      storage_pools: [],
      degraded: true,
      degraded_parts: ["storage"],
    },
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card).toContainText("Storage unknown");
  await expect(card).toContainText("Usage unavailable");
  await expect(card.getByRole("meter", { name: /^Storage:/ })).toHaveCount(0);
  await expect(card).not.toContainText(/0 B \/ 0 B|0\.0% used/);
  // The other two meters still draw: one lost part does not blank the card.
  await expect(card.getByRole("meter", { name: /^Memory:/ })).toHaveCount(1);
  await expect(card.getByRole("meter", { name: /^vCPU allocation:/ })).toHaveCount(1);
  await expectCleanInventoryHarness(state);
});

test("admin: storage with a pool that did not answer is a floor and says so", async ({ page, isMobile }) => {
  await authenticate(page.context(), "admin");
  const state = await install(page, "admin", {
    resources: { degraded: true, degraded_parts: ["storage:slow-pool"] },
  });
  await page.goto("/inventory");

  const card = coreCard(page);
  await expect(card).toContainText("Some pools did not answer");
  const meter = card.getByRole("meter", { name: /^Storage:/ });
  await expect(meter).toHaveAttribute("aria-label", /some pools did not answer/);
  const info = card.getByRole("button", { name: "About cluster storage", exact: true });
  if (isMobile) await info.tap();
  else await info.click();
  await expect(page.getByRole("dialog")).toContainText("counted once rather than once per cluster member");
  await page.keyboard.press("Escape");
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
