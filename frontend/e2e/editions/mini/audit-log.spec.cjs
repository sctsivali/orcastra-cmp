const { test, expect } = require("@playwright/test");
const { authenticate, countRequests, expectCleanHarness, expectServerRedirectHome, installApiFixtures } = require("../../support/fixtures.cjs");
const { AUDIT_ROWS } = require("./routes.cjs");

test("an Administrator reads the audit log, its retention and a verified chain", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { mini: { pages: true } });
  await page.goto("/audit-log");
  await expect(page.getByRole("heading", { level: 1, name: "Audit Log" })).toBeVisible({ timeout: 20_000 });

  await expect(page.getByText(`${AUDIT_ROWS.length} rows kept; none are removed.`)).toBeVisible();
  for (const row of AUDIT_ROWS) {
    await expect(page.getByRole("cell", { name: row.action }).first()).toBeVisible();
  }
  // A refused sign-in reads as Refused, next to the ones that succeeded.
  await expect(page.getByRole("cell", { name: "Refused", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Succeeded", exact: true })).toHaveCount(2);

  await page.getByRole("button", { name: "Verify Integrity" }).click();
  await expect(page.getByText("The chain is intact: 3 rows verified through id 3.")).toBeVisible();
  expect(countRequests(api, "/audit-log/verify")).toBe(1);

  await expectCleanHarness(api);
});

test("a Partner deep link to the audit log is redirected home by the server", async ({ page }) => {
  await authenticate(page.context(), "partner");
  await expectServerRedirectHome(page, "/audit-log");
});
