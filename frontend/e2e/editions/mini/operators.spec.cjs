const { test, expect } = require("@playwright/test");
const { authenticate, expectCleanHarness, expectServerRedirectHome, installApiFixtures } = require("../../support/fixtures.cjs");
const { OPERATORS } = require("./routes.cjs");

// The Operators page renders its table, and every filter on it opens. A column that declared both a
// facet and a date filter once made defineTable throw at render, so Administrators and Partners saw
// the error screen instead of the page (check-table-filters now refuses that at build time).

function rowsOf(page) {
  return page.getByTestId("mini-operators-table").locator("tbody tr[data-row-key]");
}

async function openOperators(page, role) {
  await authenticate(page.context(), role);
  const api = await installApiFixtures(page, role, { mini: { pages: true } });
  await page.goto("/operators");
  await expect(page.getByRole("heading", { level: 1, name: "Operators" })).toBeVisible({ timeout: 20_000 });
  return api;
}

test("an Administrator sees every operator and can narrow them by search, status and expiry", async ({ page }) => {
  const api = await openOperators(page, "admin");
  await expect(page.getByText("Everyone who signs in, their roles and their certificates.")).toBeVisible();
  await expect(rowsOf(page)).toHaveCount(OPERATORS.length);

  await page.getByPlaceholder("Search operators").fill("tina");
  await expect(rowsOf(page)).toHaveCount(1);
  await expect(rowsOf(page).first()).toContainText("Tina Tenant");
  await page.getByPlaceholder("Search operators").fill("");
  await expect(rowsOf(page)).toHaveCount(OPERATORS.length);

  await page.locator('[data-filter-field="Status"]').click();
  await page.getByRole("option", { name: /Revoked/ }).click();
  await page.keyboard.press("Escape");
  await expect(rowsOf(page)).toHaveCount(1);
  await expect(rowsOf(page).first()).toContainText("rex@e2e.invalid");
  await page.locator('[data-filter-field="Status"]').click();
  await page.getByRole("option", { name: /Revoked/ }).click();
  await page.keyboard.press("Escape");
  await expect(rowsOf(page)).toHaveCount(OPERATORS.length);

  // The expiry column is a date range only: the certificate expiring in 12 days is the one left.
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("option", { name: "Certificate Expires" }).click();
  await page.locator('[data-filter-field="Certificate Expires"]').click();
  await page.getByRole("radiogroup", { name: "Certificate Expires presets" }).getByRole("radio", { name: "Next 30 days" }).click();
  await page.keyboard.press("Escape");
  await expect(rowsOf(page)).toHaveCount(1);
  await expect(rowsOf(page).first()).toContainText("Tina Tenant");
  await expect(rowsOf(page).first()).toContainText("Expiring soon");

  await expectCleanHarness(api);
});

test("an Administrator can change any role and issue a certificate for an organization", async ({ page }) => {
  const api = await openOperators(page, "admin");

  await page.getByRole("button", { name: "Actions for tina@e2e.invalid" }).click();
  await expect(page.getByRole("menuitem", { name: "Make Partner" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Make Administrator" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Show certificates of tina@e2e.invalid" }).click();
  await expect(page.getByRole("list", { name: "Certificates of tina@e2e.invalid" })).toContainText("Active");

  await page.getByRole("button", { name: "Issue Certificate" }).click();
  const dialog = page.getByRole("dialog", { name: "Issue Certificate" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByPlaceholder("name@example.com…")).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();

  await expectCleanHarness(api);
});

test("a Partner sees only its organization's Tenants and cannot change a role", async ({ page }) => {
  const api = await openOperators(page, "partner");
  await expect(page.getByText("Tenants of the organizations you run, and their certificates.")).toBeVisible();
  await expect(rowsOf(page)).toHaveCount(OPERATORS.filter((o) => o.role === "tenant").length);
  await expect(rowsOf(page).filter({ hasText: "partner@e2e.invalid" })).toHaveCount(0);

  await page.getByRole("button", { name: "Actions for tina@e2e.invalid" }).click();
  await expect(page.getByRole("menuitem", { name: "Issue Another Certificate" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /^Make / })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await expectCleanHarness(api);
});

test("a Tenant deep link to Operators or the Audit Log is redirected home by the server", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  await expectServerRedirectHome(page, "/operators");
  await expectServerRedirectHome(page, "/audit-log");
});
