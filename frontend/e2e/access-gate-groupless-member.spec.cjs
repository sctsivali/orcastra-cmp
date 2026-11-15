const { test, expect } = require("@playwright/test");
const {
  NO_ROLE_VERDICT,
  authenticate,
  countRequests,
  expectCleanHarness,
  installApiFixtures,
} = require("./support/fixtures.cjs");

// A partner deployment signs OrcaHub buyers in through a federated IdP that sends no groups. The
// backend admits such an account as a Tenant when an active organization lists it as a member, so
// the dashboard must ask it instead of deciding "no role" from the session's groups. Only the
// verdict differs between the two cases here: the session is groupless in both.

const BUYER_VERDICT = {
  role: "tenant",
  has_role: true,
  blocked_reason: null,
  tenant_access: {
    total_clusters: 1,
    total_rules: 1,
    phase: "read_only",
    items: [
      {
        cluster_id: "cluster-a",
        project_name: "shop",
        phase: "read_only",
        inferred: false,
        term_ended_at: "2026-11-01T00:00:00Z",
        access_ends_at: "2026-12-01T00:00:00Z",
      },
    ],
  },
};

test("a groupless member is let in as a Tenant, with branding and its term notice", async ({ page }) => {
  await authenticate(page.context(), "tenant", { groups: [] });
  const api = await installApiFixtures(page, "tenant", { accessVerdict: BUYER_VERDICT });

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("heading", { name: "No role assigned yet" })).toHaveCount(0);
  await expect(page.getByTestId("tenant-term-notice")).toContainText("Read-only access");

  expect(countRequests(api, "/me/access"), "the groupless session asked the backend").toBeGreaterThan(0);
  expect(countRequests(api, "/user-preferences"), "its branding and preferences are read").toBeGreaterThan(0);
  expect(countRequests(api, "/system/vault-status"), "the Vault notice reads for it too").toBeGreaterThan(0);
  await expectCleanHarness(api);
});

test("a deep link opened by a groupless member lands on its page, not the notice", async ({ page }) => {
  await authenticate(page.context(), "tenant", { groups: [] });
  const api = await installApiFixtures(page, "tenant", { accessVerdict: BUYER_VERDICT });

  await page.goto("/technologies?from=orcahub");
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toBeVisible({ timeout: 20_000 });
  await expect(page).toHaveURL(/\/technologies\?from=orcahub$/);
  await expectCleanHarness(api);
});

test("a groupless account the backend does not admit keeps the notice and reads nothing it would be refused", async ({ page }) => {
  await authenticate(page.context(), "tenant", { groups: [] });
  const api = await installApiFixtures(page, "tenant", { accessVerdict: NO_ROLE_VERDICT });

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { name: "No role assigned yet" })).toBeVisible({ timeout: 20_000 });
  await expect(page).toHaveURL(/\/technologies$/);
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toHaveCount(0);

  expect(countRequests(api, "/me/access")).toBeGreaterThan(0);
  expect(countRequests(api, "/user-preferences"), "no branding read for an account with no role").toBe(0);
  expect(countRequests(api, "/system/vault-status"), "no Vault poll for an account with no role").toBe(0);
});

test("a groupless account whose verdict cannot be read stays on the notice", async ({ page }) => {
  await authenticate(page.context(), "tenant", { groups: [] });
  await installApiFixtures(page, "tenant");
  // Registered after the fixtures, so it wins: the verdict route is down.
  await page.route("**/api/v1/me/access", (route) => route.fulfill({ status: 503, json: { detail: "unavailable" } }));

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { name: "No role assigned yet" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toHaveCount(0);
});
