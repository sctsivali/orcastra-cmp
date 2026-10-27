const { test, expect } = require("@playwright/test");
const { authenticate, capture, countRequests, expectCleanHarness, expectNoHorizontalOverflow, installApiFixtures } = require("./support/fixtures.cjs");

// The release notice (2026-10-27): an Administrator is told when the running version is out of
// date. End of life and a missing security fix cannot be dismissed; deprecated comes back after
// seven days; a newer release stays hidden until the next one. Nobody else is ever asked.

const DOCS = "https://docs.orcastra.io/en/latest/operations";
const verdict = (level, reasons, extra = {}) => ({
  level,
  reasons,
  running_version: "1.0.0-RC4-hotfix1",
  running_status: level === "info" ? "supported" : "deprecated",
  latest_version: "1.0.0-RC4-hotfix3",
  security_version: reasons.includes("security_fix") ? "1.0.0-RC4-hotfix2" : null,
  notes_url: `${DOCS}/releases/#1-0-0-rc4-hotfix3`,
  upgrade_url: `${DOCS}/upgrading/`,
  ...extra,
});

test("admin: a missing security fix is said on every page and cannot be dismissed", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { releaseStatus: verdict("critical", ["security_fix", "deprecated", "newer_release"]) });
  await page.goto("/settings");
  const notice = page.getByTestId("release-notice");
  await expect(notice).toHaveAttribute("data-level", "critical");
  await expect(notice).toContainText("Security fixes are waiting");
  await expect(notice).toContainText("missing the security fixes released in 1.0.0-RC4-hotfix2");
  await expect(notice.getByRole("link", { name: /How to Upgrade/ })).toHaveAttribute("href", `${DOCS}/upgrading/`);
  await expect(notice.getByRole("link", { name: /How to Upgrade/ })).toHaveAttribute("rel", /noopener/);
  await expect(notice.getByRole("button", { name: /Dismiss|Remind/ })).toHaveCount(0);
  await expect(page.getByTestId("footer-release-hint")).toHaveText("Upgrade needed");
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "admin-release-critical");
  await expectCleanHarness(api);
});

test("admin: a deprecated version can be put off for seven days", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { releaseStatus: verdict("warning", ["deprecated", "newer_release"], { running_version: "1.0.0-RC4-hotfix2" }) });
  await page.goto("/settings");
  const notice = page.getByTestId("release-notice");
  await expect(notice).toContainText("Orcastra 1.0.0-RC4-hotfix2 is deprecated");
  await notice.getByRole("button", { name: "Remind Me in 7 Days" }).click();
  await expect(notice).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("footer-release-hint")).toHaveText("Upgrade soon");
  await expect(page.getByTestId("release-notice")).toHaveCount(0);
  await expectCleanHarness(api);
});

test("admin: a newer release is announced once and the card says where it was read", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { releaseStatus: verdict("info", ["newer_release"], { running_version: "1.0.0-RC4-hotfix2" }) });
  await page.goto("/settings/dashboard-host");
  const notice = page.getByTestId("release-notice");
  await expect(notice).toContainText("Orcastra 1.0.0-RC4-hotfix3 is available");
  const card = page.getByTestId("release-status-card");
  await expect(card).toContainText("Update available");
  await expect(card).toContainText("GitHub (orcastra-docs)");
  await notice.getByRole("button", { name: "Dismiss" }).click();
  await expect(notice).toHaveCount(0);
  await expectCleanHarness(api);
});

for (const role of ["partner", "tenant"]) {
  test(`${role}: never asks for the release status`, async ({ page }) => {
    await authenticate(page.context(), role);
    const api = await installApiFixtures(page, role);
    await page.goto("/");
    await expect(page.locator("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(countRequests(api, "/system/release-status")).toBe(0);
    await expect(page.getByTestId("release-notice")).toHaveCount(0);
    await expectCleanHarness(api);
  });
}
