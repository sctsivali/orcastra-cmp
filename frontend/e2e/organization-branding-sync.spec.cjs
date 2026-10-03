const { test, expect } = require("@playwright/test");
const { authenticate, capture, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const { installSettingsFixtures, wakePreferenceSync, expectCleanSettingsHarness } = require("./support/settings-fixtures.cjs");

function branding(organization_id, revision, name, accentColor = "#1e40af") {
  return {
    organization_id,
    updated_at: revision,
    settings: {
      branding: { appName: name, footerText: `${name} footer` },
      colors: { accentColor, sidebarBgColor: "#1e293b" },
      theme: "light",
    },
  };
}

async function accent(page) {
  return page.evaluate(() => document.documentElement.style.getPropertyValue("--personalization-accent"));
}

for (const role of ["partner", "tenant"]) {
  test(`${role} inherits the organization's first customization without losing a personal theme`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const api = await installSettingsFixtures(page, role, { personal: { theme: "dark", lastModified: "2026-01-01T00:00:00Z" } });
    await page.goto("/settings");
    await expect.poll(() => api.requests.filter(({ path }) => path === "/organizations/my-preferences/version").length).toBeGreaterThan(0);
    await expect(page.locator("html")).toHaveClass(/(?:^|\s)dark(?:\s|$)/);
    api.organization = branding(17, "2026-09-30T12:00:00Z", "QA organization");
    await wakePreferenceSync(page);
    await expect(page.getByText("QA organization footer", { exact: true })).toBeVisible();
    await expect.poll(() => accent(page)).toBe("#1e40af");
    await expect(page.locator("html")).toHaveClass(/(?:^|\s)dark(?:\s|$)/);
    await page.getByRole("button", { name: "Clear Browser Cache", exact: true }).click();
    await expect(page.getByText("QA organization footer", { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByText("QA organization footer", { exact: true })).toBeVisible();
    await expect(page.locator("html")).toHaveClass(/(?:^|\s)dark(?:\s|$)/);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-inherited-branding`);
    await expectCleanSettingsHarness(api);
  });
}

test("failed customization refresh retries the same version and organization identity participates in the version", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installSettingsFixtures(page, "tenant");
  api.organization = branding(17, "2026-09-30T12:00:00Z", "First QA organization");
  await page.goto("/settings");
  await expect(page.getByText("First QA organization footer", { exact: true })).toBeVisible();
  api.organization = branding(18, "2026-09-30T12:00:00Z", "Second QA organization", "#0f766e");
  api.orgFailuresRemaining = 3;
  await wakePreferenceSync(page);
  await expect.poll(() => api.orgFailedReads).toBe(3);
  await expect(page.getByText("First QA organization footer", { exact: true })).toBeVisible();
  await expect.poll(async () => {
    await wakePreferenceSync(page);
    return accent(page);
  }).toBe("#0f766e");
  await expect(page.getByText("Second QA organization footer", { exact: true })).toBeVisible();
  await expectCleanSettingsHarness(api);
});

test("a response from an old organization is ignored when membership changes during the load", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installSettingsFixtures(page, "tenant");
  api.organization = branding(17, "2026-09-30T12:00:00Z", "Retired QA organization");
  let release;
  api.holdNextOrgRead = new Promise((resolve) => { release = resolve; });
  await page.goto("/settings");
  await expect.poll(() => api.orgReads).toBe(1);
  api.organization = branding(18, "2026-09-30T12:01:00Z", "Current QA organization", "#0f766e");
  release();
  await expect.poll(() => api.requests.filter(({ path }) => path === "/organizations/my-preferences/version").length).toBeGreaterThan(0);
  await expect(page.getByText("Retired QA organization footer", { exact: true })).toHaveCount(0);
  await wakePreferenceSync(page);
  await expect(page.getByText("Current QA organization footer", { exact: true })).toBeVisible();
  await expectCleanSettingsHarness(api);
});

test("Admin without Partner continues to use personal branding", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { personal: { branding: { appName: "QA admin", footerText: "QA admin footer" } } });
  api.organization = branding(17, "2026-09-30T12:00:00Z", "Unused QA organization");
  await page.goto("/settings");
  await expect(page.getByText("QA admin footer", { exact: true })).toBeVisible();
  expect(api.orgReads).toBe(0);
  expect(api.requests.filter(({ path }) => path.startsWith("/organizations/my-preferences"))).toEqual([]);
  await expectCleanSettingsHarness(api);
});

test("the 45-second preferences poll applies new organization branding", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installSettingsFixtures(page, "tenant");
  api.organization = branding(17, "2026-09-30T12:00:00Z", "Before polling");
  await page.clock.install();
  await page.goto("/settings");
  await expect(page.getByText("Before polling footer", { exact: true })).toBeVisible();
  api.organization = branding(17, "2026-09-30T12:01:00Z", "After polling");
  await page.clock.fastForward(45_000);
  await expect(page.getByText("After polling footer", { exact: true })).toBeVisible();
  await expectCleanSettingsHarness(api);
});
