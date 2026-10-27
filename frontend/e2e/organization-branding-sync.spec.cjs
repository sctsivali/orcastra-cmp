const { test, expect } = require("@playwright/test");
const { authenticate, capture, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const { installSettingsFixtures, wakePreferenceSync, expectCleanSettingsHarness } = require("./support/settings-fixtures.cjs");

// The server's version token covers every layer under the account; any change moves it.
function branding(organization_id, revision, name, accentColor = "#1e40af", { subject = "organization" } = {}) {
  return {
    organization_id,
    updated_at: revision,
    version: `${organization_id}:${revision}:${name}`,
    source: subject === "deployment" ? "deployment" : "organization",
    subject,
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

// An Administrator without the partner role used to read its personal row as its whole branding.
// The server now answers my-preferences with the deployment default for it, and a personal row
// carries only the theme, so branding left in an old personal row is ignored.
test("an Administrator without Partner sees the deployment default, not a personal copy", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { personal: { branding: { appName: "QA admin", footerText: "QA admin footer" } } });
  api.organization = branding(0, "2026-09-30T12:00:00Z", "QA deployment", "#1e40af", { subject: "deployment" });
  await page.goto("/settings");
  await expect(page.getByText("QA deployment footer", { exact: true })).toBeVisible();
  await expect(page.getByText("QA admin footer", { exact: true })).toHaveCount(0);
  expect(api.orgReads).toBeGreaterThan(0);
  await expectCleanSettingsHarness(api);
});

test("a change that keeps the timestamp but moves the version is applied", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installSettingsFixtures(page, "tenant");
  api.organization = branding(17, "2026-09-30T12:00:00Z", "Seller look");
  await page.goto("/settings");
  await expect(page.getByText("Seller look footer", { exact: true })).toBeVisible();
  // A reset of the buyer's own row, or a new provider, can leave an older row on top.
  api.organization = branding(17, "2026-09-30T12:00:00Z", "Provider look", "#0f766e");
  await wakePreferenceSync(page);
  await expect(page.getByText("Provider look footer", { exact: true })).toBeVisible();
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
