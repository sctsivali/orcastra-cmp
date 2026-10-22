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
// serves; kernel answers come from the base fixture underneath it (options.kernel).
async function install(page, role, options = {}) {
  const state = await installInventoryHardeningFixtures(page, role, options);
  return state;
}

// Open kernel CVEs are a Partner and Admin finding. The card chip counts them, hover or focus
// previews the list, a click, tap or Enter opens every member's detail, and the alarm tile and
// the Kernel CVEs tab count the same rows. A tenant never asks.
//
// note: every negative below has a positive control with the same locator in another test, so
// none of them can pass because the thing it names stopped rendering for everyone.

const cveChip = (scope, cluster) =>
  scope.getByRole("button", { name: new RegExp(`: show kernel CVE details for ${cluster}$`) });
// The painted list, not getByRole("tooltip"): Radix keeps a visually hidden copy of the content
// under role="tooltip" for aria-describedby, and that copy is what the role locator resolves.
const cveTip = (page) => page.locator('[data-slot="kernel-cve-tooltip"] > [data-slot="kernel-cve-list"]');
const coreCard = (page) => page.locator('[data-slot="cluster-card"]').filter({ hasText: "Core Cluster" });

// Records whether the preview was ever painted, so a flash that opens and fades between two reads
// still counts. A count taken after the dialog lands misses it: toHaveCount(0) polls until true.
async function watchTip(page) {
  await page.evaluate(() => {
    window.__cveTipSeen = false;
    window.__cveTipObserver?.disconnect();
    window.__cveTipObserver = new MutationObserver(() => {
      if (document.querySelector('[data-slot="kernel-cve-tooltip"]')) window.__cveTipSeen = true;
    });
    window.__cveTipObserver.observe(document.body, { childList: true, subtree: true });
  });
}
const tipSeen = (page) => page.evaluate(() => window.__cveTipSeen);

async function settle(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  // the dialog's exit animation hands focus back after it ends; wait past it before reading
  await page.waitForTimeout(400);
}

for (const role of ["admin", "partner"]) {
  test(`${role}: an affected kernel is counted on its cluster card and listed in Warnings`, async ({ page, isMobile }, testInfo) => {
    await authenticate(page.context(), role);
    const fixture = await install(page, role, { kernel: "exposed" });
    const api = fixture.base;

    await page.goto("/inventory");
    const chip = page.getByRole("button", {
      name: "1 CVE, affected: show kernel CVE details for Core Cluster",
      exact: true,
    });
    await expect(chip).toBeVisible();
    await expect(chip).toHaveText("1 CVE");
    await expect(chip).toHaveAttribute("data-slot", "badge");
    // an outline chip: nothing painted behind the words at rest
    await expect(chip).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");

    // the card keeps one solid chip, its status; "Latest" is text beside the version
    const card = coreCard(page);
    await expect(card.locator('[data-slot="badge"]', { hasText: /^Online$/ })).toHaveCount(1);
    await expect(card.getByText("Latest", { exact: true })).toBeVisible();
    await expect(card.locator('[data-slot="badge"]', { hasText: /^Latest$/ })).toHaveCount(0);

    if (!isMobile) {
      await chip.hover();
      await expect(cveTip(page)).toBeVisible();
      await expect(cveTip(page).getByText("CVE-2026-99999 (PwnEscape)", { exact: true })).toBeVisible();
      await expect(cveTip(page).getByText("Affected · CVSS 9.3 Critical · 1 of 1 member", { exact: true })).toBeVisible();
      // portalled: a clipping, transformed card cannot cut it off
      await expect(card.locator('[data-slot="kernel-cve-tooltip"]')).toHaveCount(0);
      await capture(page, testInfo, `${role}-kernel-tooltip`);
      await page.mouse.move(0, 0, { steps: 12 });
      await expect(cveTip(page)).toHaveCount(0);

      // keyboard reaches the same preview
      await chip.focus();
      await expect(cveTip(page)).toBeVisible();
      await page.keyboard.press("Enter");
    } else {
      // touch has no hover: a tap goes straight to the detail and never paints the preview, not
      // even for the frame before the dialog covers it (a tap focuses the chip after pointerup)
      await watchTip(page);
      await chip.tap();
    }

    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("heading", { name: "Kernel CVEs" })).toBeVisible();
    await expect(dialog.getByText("Affected", { exact: true }).first()).toBeVisible();
    await expect(dialog.getByText("CVE-2026-99999 (PwnEscape)", { exact: true })).toBeVisible();
    await expect(dialog.getByText("5.15.0-185-generic", { exact: true })).toBeVisible();
    await expect(dialog.getByText(/Linux CNA: 9\.3 Critical, Red Hat: 8\.8 Important, Ubuntu: Medium/)).toBeVisible();
    await expect(dialog.getByText(/linux-modules-\$\(uname -r\)\/changelog\.Debian\.gz/).first()).toBeVisible();
    const reference = dialog.getByRole("link", { name: /Kernel CVE announcement/ });
    await expect(reference).toHaveAttribute("href", "https://lists.openwall.net/linux-cve-announce/2026/10/15/2");
    await expect(reference).toHaveAttribute("target", "_blank");
    await expect(cveTip(page)).toHaveCount(0);
    if (isMobile) expect(await tipSeen(page), "a tap flashed the preview").toBe(false);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-kernel-dialog`);

    await watchTip(page);
    if (!isMobile) {
      await page.keyboard.press("Escape");
    } else {
      await dialog.getByRole("button", { name: "Close", exact: true }).first().tap();
    }
    await expect(dialog).toHaveCount(0);
    await expect(chip).toBeFocused();
    // focus came back to the chip; the preview of what was just closed stays shut
    await settle(page);
    expect(await tipSeen(page), "closing the dialog reopened the preview").toBe(false);

    if (!isMobile) {
      // the quiet window is one focus long: leaving and coming back previews again
      await page.keyboard.press("Tab");
      await page.keyboard.press("Shift+Tab");
      await expect(chip).toBeFocused();
      await expect(cveTip(page)).toBeVisible();
      await page.keyboard.press("Escape");
    }

    // one request however many cards ask: they share a query key
    expect(countRequests(api, "/security/kernel-advisories")).toBe(1);

    const tile = page.getByRole("link", { name: /1\s*Kernel CVEs/ });
    await expect(tile).toHaveAttribute("href", "/inventory/warnings?tab=kernel");
    await tile.click();
    await expect(page).toHaveURL(/\/inventory\/warnings\?tab=kernel$/);
    await expect(page.getByRole("tab", { name: /CVEs/, selected: true })).toBeVisible();
    // the CVE column: its sort control, and the id in the row
    await expect(page.getByRole("button", { name: "CVE", exact: true })).toBeVisible();
    await expect(page.locator("tbody tr td").getByText("CVE-2026-99999", { exact: true })).toBeVisible();
    const row = page.getByRole("button", { name: "Show CVE-2026-99999 for cluster-a on Core Cluster", exact: true });
    await expect(row).toBeVisible();
    await row.click();
    const detail = page.getByRole("dialog");
    await expect(detail.getByRole("heading", { name: "cluster-a on Core Cluster" })).toBeVisible();
    await expect(detail.getByText("Affected", { exact: true }).first()).toBeVisible();
    await expect(detail.getByText("CVE", { exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-kernel-tab`);

    await expectCleanInventoryHarness(fixture);
  });
}

test("admin: an unconfirmed vendor kernel reads as unconfirmed, not affected", async ({ page, isMobile }) => {
  await authenticate(page.context(), "admin");
  const fixture = await install(page, "admin", { kernel: "unconfirmed" });

  await page.goto("/inventory");
  const chip = page.getByRole("button", {
    name: "1 CVE, unconfirmed: show kernel CVE details for Core Cluster",
    exact: true,
  });
  await expect(chip).toBeVisible();
  if (!isMobile) {
    await chip.hover();
    await expect(cveTip(page).getByText("Unconfirmed · CVSS 9.3 Critical · 1 of 1 member", { exact: true })).toBeVisible();
    await page.mouse.move(0, 0, { steps: 12 });
  }
  await chip.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Unconfirmed", { exact: true }).first()).toBeVisible();
  await expect(dialog.getByText("Affected", { exact: true })).toHaveCount(0);
  await expect(dialog.getByText("5.15.148-tegra", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto("/inventory/warnings?tab=kernel");
  await expect(page.locator("tbody tr td").getByText("Unconfirmed", { exact: true })).toBeVisible();

  await expectCleanInventoryHarness(fixture);
});

test("admin: two CVEs on a two-member cluster are counted, ordered and filterable", async ({ page, isMobile }, testInfo) => {
  await authenticate(page.context(), "admin");
  const fixture = await install(page, "admin", { kernel: "two" });

  await page.goto("/inventory");
  const chip = page.getByRole("button", {
    name: "2 CVEs, affected: show kernel CVE details for Core Cluster",
    exact: true,
  });
  await expect(chip).toHaveText("2 CVEs");
  if (!isMobile) {
    await chip.hover();
    const items = cveTip(page).locator("li");
    await expect(items).toHaveCount(2);
    // affected first, then unconfirmed; a CVE with no record shows its bare id and no score
    await expect(items.nth(0)).toContainText("CVE-2026-99999 (PwnEscape)");
    await expect(items.nth(0)).toContainText("Affected · CVSS 9.3 Critical · 1 of 2 members");
    await expect(items.nth(1)).toContainText("CVE-2026-88888");
    await expect(items.nth(1)).toContainText("Unconfirmed · 2 of 2 members");
    await expect(items.nth(1)).not.toContainText("CVSS");
    await capture(page, testInfo, "admin-kernel-two-tooltip");
    await page.mouse.move(0, 0, { steps: 12 });
  }
  await chip.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "CVE-2026-88888", exact: true })).toBeVisible();
  await expect(dialog.getByText("No advisory details were returned for this CVE.")).toBeVisible();
  await expect(dialog.getByText("cluster-a-2", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto("/inventory/warnings?tab=kernel");
  await expect(page.getByRole("tab", { name: /CVEs.*\(3\)/, selected: true })).toBeVisible();
  await expect(page.getByText("3 kernel CVE findings", { exact: true })).toBeVisible();

  await page.goto("/inventory/warnings?tab=kernel&kernel.cve=CVE-2026-88888");
  await expect(page.getByText("2 of 3 kernel CVE findings", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Show CVE-2026-88888 for / })).toHaveCount(2);
  await expect(page.getByRole("button", { name: /^Show CVE-2026-99999 for / })).toHaveCount(0);

  await page.goto("/inventory/warnings?tab=kernel&kernel.q=88888");
  await expect(page.getByText("2 of 3 kernel CVE findings", { exact: true })).toBeVisible();

  await expectCleanInventoryHarness(fixture);
});

test("admin: a patched cluster grows no chip on its card but reads as patched in System", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const fixture = await install(page, "admin");
  const api = fixture.base;

  await page.goto("/inventory");
  await expect(page.getByRole("heading", { name: "Core Cluster" })).toBeVisible();
  await expect.poll(() => countRequests(api, "/security/kernel-advisories")).toBe(1);
  await expect(cveChip(coreCard(page), "Core Cluster")).toHaveCount(0);
  await expect(page.getByRole("link", { name: /Kernel CVEs/ })).toHaveCount(0);

  // the System panel is where an operator reads kernels, so it states patched too, through the
  // same locator that found nothing on the card
  await coreCard(page).getByRole("button", { name: "View Details", exact: true }).click();
  const details = page.getByRole("dialog");
  await expect(details.getByText("Kernel CVEs", { exact: true })).toBeVisible();
  const patched = cveChip(details, "Core Cluster");
  await expect(patched).toHaveAccessibleName("Patched: show kernel CVE details for Core Cluster");
  await expect(patched).toHaveText("Patched");
  expect(countRequests(api, "/security/kernel-advisories")).toBe(1);

  await expectCleanInventoryHarness(fixture);
});

test("admin: an outdated LXD offers its update as an outline chip that opens the steps", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const fixture = await install(page, "admin", { nodeServerVersion: "5.0.2" });

  await page.goto("/inventory");
  const update = coreCard(page).getByRole("button", { name: "Update Available", exact: true });
  await expect(update).toBeVisible();
  await expect(update).toHaveAttribute("data-slot", "badge");
  await expect(update).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(coreCard(page).getByText("Latest", { exact: true })).toHaveCount(0);

  await update.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "LXD Update Available" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(update).toBeFocused();

  await expectCleanInventoryHarness(fixture);
});

test("tenant: no kernel CVE is requested or shown, and no version check either", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const fixture = await install(page, "tenant", { kernel: "exposed" });
  const api = fixture.base;

  await page.goto("/inventory");
  const clusters = page.getByRole("region", { name: "Clusters", exact: true });
  await expect(clusters.getByText("Core Cluster", { exact: true })).toBeVisible();
  // the tenant's card has painted (its status chip is there), so the absences below are about the gate
  await expect(clusters.locator('[data-slot="badge"]', { hasText: /^(Online|Unavailable)$/ }).first()).toBeVisible();
  await expect(cveChip(page, "Core Cluster")).toHaveCount(0);
  await expect(page.getByText(/^(Latest|Update Available)$/)).toHaveCount(0);
  expect(countRequests(api, "/security/kernel-advisories")).toBe(0);

  await page.goto("/inventory/warnings?tab=kernel");
  await expect(page).not.toHaveURL(/\/inventory\/warnings/);
  expect(countRequests(api, "/security/kernel-advisories")).toBe(0);

  await expectCleanInventoryHarness(fixture);
});
