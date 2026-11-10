const { test, expect } = require("@playwright/test");
const { authenticate, capture, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const { installSettingsFixtures, expectCleanSettingsHarness } = require("./support/settings-fixtures.cjs");

// Settings > Integrations and Settings > User Groups open on a section switcher (?section=)
// instead of stacking every panel, which would bury Synced Organizations, Desktop Control and
// Assigned Users under a long list.

const USERS = [
  { user_email: "alice@example.com", role: "tenant", organizations: [{ id: 3, name: "Acme", slug: "acme", role: "tenant" }] },
  { user_email: "bob@example.com", role: "partner", organizations: [
    { id: 3, name: "Acme", slug: "acme", role: "partner" }, { id: 4, name: "Beta", slug: "beta", role: "tenant" },
  ] },
  { user_email: "carol@example.com", role: "partner", organizations: [{ id: 3, name: "Acme", slug: "acme", role: "partner" }] },
];
const reads = { "/user-groups": USERS };

const sections = (page, name) => page.getByRole("navigation", { name });
const sectionLink = (page, nav, label) => sections(page, nav).getByRole("link", { name: new RegExp(`^${label}`) });

for (const role of ["admin", "partner"]) {
  test(`${role} integrations sections deep-link, switch and walk history`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const api = await installSettingsFixtures(page, role);
    await page.goto("/settings/integrations?section=desktop-control");

    const nav = "Integrations sections";
    await expect(sectionLink(page, nav, "Desktop Control")).toHaveAttribute("aria-current", "true");
    // Every section is on screen at every width; a horizontal scroller would hide the third one
    // on a phone.
    for (const label of ["API Keys", "Synced Organizations", "Desktop Control"]) {
      await expect(sectionLink(page, nav, label)).toBeInViewport({ ratio: 1 });
    }
    await expect(page.getByRole("heading", { name: "Desktop Control", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Create API Key" })).toHaveCount(0);
    // one tabpanel, the Settings one: the sections are links, not a nested tablist
    await expect(page.getByRole("tabpanel")).toHaveCount(1);

    await sectionLink(page, nav, "API Keys").click();
    await expect(page).toHaveURL(/\/settings\/integrations$/);
    await expect(page.getByRole("heading", { name: "API Keys", exact: true })).toBeVisible();
    await sectionLink(page, nav, "Synced Organizations").click();
    await expect(page).toHaveURL(/\/settings\/integrations\?section=organizations$/);
    await expect(page.locator("#synced-organizations")).toBeVisible();
    // a same-path link must not leave the page-loading status behind
    await expect(page.getByRole("status").filter({ hasText: "Loading page" })).toHaveCount(0);

    await page.goBack();
    await expect(page).toHaveURL(/\/settings\/integrations$/);
    await expect(sectionLink(page, nav, "API Keys")).toHaveAttribute("aria-current", "true");
    await page.goBack();
    await expect(page).toHaveURL(/\/settings\/integrations\?section=desktop-control$/);
    await page.goForward();
    await expect(page).toHaveURL(/\/settings\/integrations$/);
    await page.reload();
    await expect(sectionLink(page, nav, "API Keys")).toHaveAttribute("aria-current", "true");
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-integrations-sections`);
    await expectCleanSettingsHarness(api);
  });
}

test("an unknown or hostile section falls back to the default and is not reflected", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin");
  const hostile = "<img src=x onerror=alert(1)>";
  const dialogs = [];
  page.on("dialog", (dialog) => { dialogs.push(dialog.message()); void dialog.dismiss(); });
  for (const value of [hostile, "", "keys&section=organizations", "x".repeat(4000)]) {
    await page.goto(`/settings/integrations?section=${encodeURIComponent(value)}`);
    await expect(sectionLink(page, "Integrations sections", "API Keys")).toHaveAttribute("aria-current", "true");
    expect(await page.content()).not.toContain(hostile);
  }
  // repeated keys: the first value wins, and only a known one is honoured
  await page.goto("/settings/integrations?section=organizations&section=desktop-control");
  await expect(sectionLink(page, "Integrations sections", "Synced Organizations")).toHaveAttribute("aria-current", "true");
  expect(dialogs).toEqual([]);
  await expectCleanSettingsHarness(api);
});

test("a section keeps its search when the operator switches away and back", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { reads });
  await page.goto("/settings/user-groups");
  const nav = "User Groups sections";
  await expect(sectionLink(page, nav, "Assigned Users")).toHaveAttribute("aria-current", "true");
  await expect(sectionLink(page, nav, "Assigned Users")).toContainText("3");
  await expect(page.getByText("Showing all 3 users", { exact: true })).toBeVisible();
  await expect(page.getByText("Identity provider sync is not configured", { exact: false })).toBeVisible();

  const search = page.getByRole("searchbox", { name: /Search users/ });
  await search.fill("bob");
  await expect(page.getByText("Showing 1 of 3 users", { exact: true })).toBeVisible();
  await sectionLink(page, nav, "Access Policies").click();
  await expect(page.getByRole("heading", { name: "Access Policies", exact: true })).toBeVisible();
  await expect(search).toBeHidden();
  await sectionLink(page, nav, "Assigned Users").click();
  await expect(search).toHaveValue("bob");
  await expect(page.getByText("Showing 1 of 3 users", { exact: true })).toBeVisible();

  // Access follows any tenant membership: bob is a partner of Acme and a tenant of Beta.
  const bob = page.getByRole("row").filter({ hasText: "bob@example.com" });
  await expect(bob.getByRole("cell").nth(1)).toHaveText("Partner");
  await expect(bob.getByRole("button", { name: /^Access/ })).toBeVisible();
  await search.fill("carol");
  const carol = page.getByRole("row").filter({ hasText: "carol@example.com" });
  await expect(carol.getByRole("button", { name: /^Access/ })).toHaveCount(0);
  await expect(carol.getByRole("button", { name: "Remove carol@example.com" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "admin-user-groups-sections");
  await expectCleanSettingsHarness(api);
});

test("Back closes a dialog that the hidden section opened", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { reads });
  await page.goto("/settings/user-groups");
  const nav = "User Groups sections";
  await sectionLink(page, nav, "Access Policies").click();
  await sectionLink(page, nav, "Assigned Users").click();
  await page.getByRole("button", { name: "Assign User" }).click();
  await expect(page.getByRole("dialog", { name: "Assign User Role" })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/settings\/user-groups\?section=policies$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Access Policies", exact: true })).toBeVisible();
  await expectCleanSettingsHarness(api);
});

test("an administrator can assign a role in a local organization", async ({ page }) => {
  // The organization list's default leaves local organizations out. The dialog asks for both and
  // marks local ones, so an organization made in the console can get a partner row.
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { reads });
  const local = { id: 480, name: "QA Console Lab", slug: "qa-console-lab", description: null, source: "local",
    external_id: null, api_key_id: null, app_name: null, created_at: null, updated_at: null };
  await page.route(
    (url) => url.pathname.endsWith("/api/v1/integrations/organizations") && url.searchParams.get("source") === "local",
    (route) => route.fulfill({ json: [local] }),
  );
  await page.goto("/settings/user-groups");
  await page.getByRole("button", { name: "Assign User" }).click();
  const dialog = page.getByRole("dialog", { name: "Assign User Role" });
  await dialog.getByRole("combobox", { name: /Organization/ }).click();
  await expect(page.getByRole("option", { name: /QA Console Lab\s*local/ })).toBeVisible();
  await expectCleanSettingsHarness(api);
});

test("a partner is offered only the tenant role", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installSettingsFixtures(page, "partner", { reads });
  await page.goto("/settings/user-groups");
  await page.getByRole("button", { name: "Assign User" }).click();
  const dialog = page.getByRole("dialog", { name: "Assign User Role" });
  const roleSelect = dialog.getByRole("combobox", { name: "Role" });
  await expect(roleSelect).toBeDisabled();
  await expect(roleSelect).toHaveText("Tenant");
  await expect(dialog).toContainText("Partners add tenants. An administrator assigns the other roles.");
  await expectCleanSettingsHarness(api);
});

test("a refused removal stays in the dialog and says why", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { reads });
  const deletes = [];
  await page.route("**/api/v1/user-groups/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deletes.push(new URL(route.request().url()).pathname);
    return route.fulfill({ status: 502, json: { detail: "Authentik role removal was not confirmed; retry this operation" } });
  });
  await page.goto("/settings/user-groups");
  await page.getByRole("button", { name: "Remove alice@example.com" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Remove User" });
  const remove = dialog.getByRole("button", { name: "Remove", exact: true });
  await remove.evaluate((element) => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(dialog.getByRole("alert")).toContainText("Authentik role removal was not confirmed");
  await expect(dialog).toBeVisible();
  expect(deletes).toEqual(["/api/v1/user-groups/alice%40example.com"]);
  await expectCleanSettingsHarness(api);
});

for (const query of ["?section=desktop-control", "?section=policies"]) {
  test(`tenant deep link with ${query} returns to General without privileged reads`, async ({ page }) => {
    await authenticate(page.context(), "tenant");
    const api = await installSettingsFixtures(page, "tenant");
    const slug = query.includes("policies") ? "user-groups" : "integrations";
    await page.goto(`/settings/${slug}${query}`);
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByText("Settings access restricted", { exact: true })).toBeVisible();
    const forbidden = api.requests.filter(({ path }) => path.startsWith("/integrations") || path.startsWith("/user-groups") || path === "/tenant-access/policies");
    expect(forbidden).toEqual([]);
    await expectCleanSettingsHarness(api);
  });
}
