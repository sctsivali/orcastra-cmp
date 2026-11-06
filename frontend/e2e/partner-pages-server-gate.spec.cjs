const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");

// Every page that gates itself to Partner in the browser is also refused on the server, so a
// tenant deep link is redirected home before the page shell loads. The production RC4 hotfix line
// redirected seven of these on the server and the dev line only two (found in the RC5 acceptance,
// 2026-11-05). scripts/checks/check-role-gates.mjs keeps auth.ts and the pages in step; this
// proves the running middleware does what the list says.
const PARTNER_PAGES = [
  "/inventory/identities",
  "/inventory/groups",
  "/inventory/clustering/members",
  "/inventory/clusters",
  "/inventory/clusters/certificates",
  "/inventory/operations",
  "/inventory/server",
  "/inventory/snapshots",
  "/inventory/migrations",
  "/inventory/warnings",
  "/my-keys",
  "/my-keys/activity",
  "/extensions",
];

const REDIRECT = [302, 303, 307, 308];

test("a tenant deep link to a partner page is redirected home by the server", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  for (const path of PARTNER_PAGES) {
    const response = await page.request.get(path, { maxRedirects: 0 });
    expect(REDIRECT, `${path} answered ${response.status()}`).toContain(response.status());
    expect(new URL(response.headers().location, "http://e2e.invalid").pathname, path).toBe("/");
  }
});

test("a partner still gets every partner page", async ({ page }) => {
  await authenticate(page.context(), "partner");
  for (const path of PARTNER_PAGES) {
    const response = await page.request.get(path, { maxRedirects: 0 });
    expect(response.status(), path).toBe(200);
  }
});

test("a Partner known by a role_ alias group gets every partner page too", async ({ page }) => {
  // The server reads groups the way the page and the API do (types/rbac.ts): role_orcastra-partners
  // is a Partner there. A local group list on the server called it a Tenant and sent it home.
  await authenticate(page.context(), "partner", { groups: ["role_orcastra-partners"] });
  for (const path of PARTNER_PAGES) {
    const response = await page.request.get(path, { maxRedirects: 0 });
    expect(response.status(), path).toBe(200);
  }
});

test("a session with no role keeps its URL on a partner page, for the access gate to answer", async ({ page }) => {
  // The access gate shows the no-role notice on any page and keeps the address, so Check again
  // lands where the person was going once a role is granted. The partner redirect is for a role
  // below Partner, not for no role at all (found by the gate's own live spec, 2026-11-06).
  await authenticate(page.context(), "tenant", { groups: [] });
  for (const path of PARTNER_PAGES) {
    const response = await page.request.get(path, { maxRedirects: 0 });
    expect(response.status(), path).toBe(200);
  }
  const hostHealth = await page.request.get("/inventory/warnings/host-health", { maxRedirects: 0 });
  expect(REDIRECT, "Host Health still redirects anyone who is not an Admin").toContain(hostHealth.status());
});

test("Host Health stays an admin page on the server", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const refused = await page.request.get("/inventory/warnings/host-health", { maxRedirects: 0 });
  expect(REDIRECT).toContain(refused.status());

  await authenticate(page.context(), "admin");
  const allowed = await page.request.get("/inventory/warnings/host-health", { maxRedirects: 0 });
  expect(allowed.status()).toBe(200);
});
