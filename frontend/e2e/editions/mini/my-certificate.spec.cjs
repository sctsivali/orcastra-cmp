const { test, expect } = require("@playwright/test");
const { authenticate, countRequests, expectCleanHarness, installApiFixtures } = require("../../support/fixtures.cjs");

// The certificate an operator signs in with stays reachable: from the user menu, from the expiry
// notice on every page, and from the access gate that replaces the shell once a Tenant's access
// ends (it could neither see nor renew its certificate there before EDITION_GATE_EXEMPT_PATHS).

const ENDED_ACCESS = {
  role: "tenant",
  has_role: true,
  blocked_reason: "no_tenant_access",
  tenant_access: { total_clusters: 0, total_rules: 0, phase: "ended", items: [] },
};

const IN_RENEWAL_WINDOW = { not_after: new Date(Date.now() + 9 * 86_400_000).toISOString(), days_left: 9, expiring: true, renew_available: true };

test("a Tenant whose access ended reaches and renews its certificate from the gate", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { accessVerdict: ENDED_ACCESS, mini: { myCertificate: IN_RENEWAL_WINDOW } });

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { level: 1, name: "No active access" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/Your certificate expires on/)).toBeVisible();
  await page.getByRole("link", { name: "Renew it" }).click();

  await expect(page).toHaveURL(/\/my-certificate$/);
  await expect(page.getByRole("heading", { level: 1, name: "My Certificate" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Renew Certificate" })).toBeVisible();
  // Without the shell: no sidebar navigation, and a way back.
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Back" })).toBeVisible();
  await page.getByRole("link", { name: "Back" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "No active access" })).toBeVisible();

  await expectCleanHarness(api);
});

test("every page warns about a certificate in its renewal window and links to renewal", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { mini: { myCertificate: IN_RENEWAL_WINDOW } });

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/Your certificate expires on .* \(9 days left\)/)).toBeVisible();
  await page.getByRole("link", { name: "Renew it now" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "My Certificate" })).toBeVisible();
  await expect(page.getByText("partner@e2e.invalid").first()).toBeVisible();

  expect(countRequests(api, "/me/certificate")).toBeGreaterThan(0);
  await expectCleanHarness(api);
});

test("a certificate far from expiry shows no notice and says when renewal opens", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin");

  await page.goto("/my-certificate");
  await expect(page.getByRole("heading", { level: 1, name: "My Certificate" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/Renewal opens on/)).toBeVisible();
  await expect(page.getByText(/Your certificate expires on/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Renew Certificate" })).toHaveCount(0);

  await expectCleanHarness(api);
});
