const { test, expect } = require("@playwright/test");
const { authenticate, capture, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const { installSettingsFixtures, expectCleanSettingsHarness } = require("./support/settings-fixtures.cjs");

async function sessionIdentity(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/auth/session", { credentials: "include", cache: "no-store" });
    const session = await response.json();
    return { id: session.user?.id, groups: session.user?.groups };
  });
}

function isPrivilegedSettingsRead({ method, path }) {
  return method === "GET" && (path.startsWith("/integrations") || path.startsWith("/user-groups")
    || path === "/tenant-access/policies" || path === "/system/resource-usage");
}

for (const role of ["admin", "partner"]) {
  for (const slug of ["integrations", "user-groups", "personalization", "dashboard-host"]) {
    test(`${role} mounted ${slug} retires when the same actor is downgraded to Tenant`, async ({ page }, testInfo) => {
      await authenticate(page.context(), role);
      const api = await installSettingsFixtures(page, role);
      await page.goto(`/settings/${slug}`);
      const title = { integrations: "Integrations", "user-groups": "User Groups",
        personalization: "Personalization", "dashboard-host": "Dashboard Host" }[slug];
      await expect(page).toHaveURL(new RegExp(`/settings/${slug}$`));
      await expect(page.getByRole("tab", { name: title, exact: true })).toHaveAttribute("aria-selected", "true");
      const panel = page.getByRole("tabpanel");
      await expect(panel).toBeVisible();
      const previousPanel = await panel.elementHandle();
      const previousDocument = await page.evaluateHandle(() => document);
      const actorBefore = await sessionIdentity(page);
      expect(actorBefore).toEqual({ id: `${role}@e2e.invalid`, groups: [`role_${role}`] });
      await page.waitForLoadState("networkidle");
      const transitionBoundary = api.requests.length;

      await authenticate(page.context(), role, { sessionRole: "tenant" });
      // Auth.js' native cross-tab signal refreshes the mounted SessionProvider from the real
      // synthetic cookie. No reload or production-only state setter supplies the new groups.
      await page.evaluate(() => {
        const channel = new BroadcastChannel("next-auth");
        channel.postMessage({ event: "session", data: { trigger: "getSession" } });
        channel.close();
      });
      await expect(page).toHaveURL(/\/settings$/);
      await expect(page.getByText("Settings access restricted", { exact: true })).toBeVisible();
      await expect(page.getByRole("tab", { name: "General", exact: true })).toHaveAttribute("aria-selected", "true");
      await expect(page.getByRole("tab")).toHaveCount(1);
      expect(await previousPanel.evaluate((element) => element.isConnected)).toBe(false);
      // Requests issued while Auth.js is still resolving the new groups belong to the
      // transition. Start the strict window immediately after the privileged panel retires.
      const retirementBoundary = api.requests.length;
      const transitionReads = api.requests.slice(transitionBoundary, retirementBoundary);
      expect(await page.evaluate((original) => original === document, previousDocument)).toBe(true);
      expect(await sessionIdentity(page)).toEqual({ id: actorBefore.id, groups: ["role_tenant"] });
      await expect(page.getByRole("button", { name: "Clear Shared Backend Cache", exact: true })).toHaveCount(0);

      // Cache refetch after the downgrade must not revive queries owned by the old panel.
      await page.getByRole("button", { name: "Clear Browser Cache", exact: true }).click();
      await expect(page.getByRole("status").filter({ hasText: "Browser cache cleared." })).toBeVisible();
      // Observe beyond the 1s query retry and Dashboard Host's 5s poll. Neither may
      // restart a retired privileged query after the new role is recognized.
      const observationMs = slug === "dashboard-host" ? 5500 : 1100;
      await page.waitForTimeout(observationMs);
      const privilegedReads = api.requests.slice(retirementBoundary).filter(isPrivilegedSettingsRead);
      expect(privilegedReads).toEqual([]);
      expect(api.sharedClears).toBe(0);
      await testInfo.attach("same-actor-downgrade", {
        body: JSON.stringify({ actorBefore, actorAfter: await sessionIdentity(page), slug,
          transitionReads, retirementBoundary, postRetirementObservationMs: observationMs, privilegedReads }),
        contentType: "application/json",
      });
      await expectCleanSettingsHarness(api);
    });
  }
}

for (const role of ["admin", "partner"]) {
  test(`${role} settings tabs have reload-safe URLs and browser history`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const api = await installSettingsFixtures(page, role);
    await page.goto("/settings");
    await expect(page.getByRole("tab", { name: "General", exact: true })).toHaveAttribute("aria-selected", "true");
    for (const [title, slug] of [["Integrations", "integrations"], ["User Groups", "user-groups"], ["Personalization", "personalization"], ["Dashboard Host", "dashboard-host"]]) {
      await page.getByRole("tab", { name: title, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`/settings/${slug}$`));
      await expect(page.getByRole("tab", { name: title, exact: true })).toHaveAttribute("aria-selected", "true");
      await page.reload();
      await expect(page.getByRole("tab", { name: title, exact: true })).toHaveAttribute("aria-selected", "true");
    }
    await page.goBack();
    await expect(page).toHaveURL(/\/settings\/personalization$/);
    await expect(page.getByRole("tab", { name: "Personalization", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.goForward();
    await expect(page).toHaveURL(/\/settings\/dashboard-host$/);
    await expect(page.getByRole("heading", { name: "Could not load dashboard host figures" })).toBeVisible();
    await page.getByRole("tab", { name: "General", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/settings$/);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-settings-history`);
    await expectCleanSettingsHarness(api);
  });
}

for (const role of ["admin", "partner", "tenant"]) {
  test(`${role} can clear browser reads while retaining authentication and preferences`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const api = await installSettingsFixtures(page, role);
    await page.goto("/settings");
    await page.evaluate(() => localStorage.setItem("qa-preference-sentinel", "keep"));
    const actorBefore = await sessionIdentity(page);
    const button = page.getByRole("button", { name: "Clear Browser Cache", exact: true });
    await button.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("status").filter({ hasText: "Browser cache cleared." })).toBeVisible();
    expect(api.sharedClears).toBe(0);
    expect(await page.evaluate(() => localStorage.getItem("qa-preference-sentinel"))).toBe("keep");
    // Auth.js may rotate its encrypted cookie during a read. Actor identity must stay intact.
    expect(await sessionIdentity(page)).toEqual(actorBefore);
    expect(actorBefore).toEqual({ id: `${role}@e2e.invalid`, groups: [`role_${role}`] });
    expect((await page.context().cookies()).some((cookie) => cookie.name === "authjs.session-token")).toBe(true);
    await expect(page.getByRole("button", { name: "Clear Shared Backend Cache", exact: true })).toHaveCount(role === "admin" ? 1 : 0);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-browser-cache`);
    await expectCleanSettingsHarness(api);
  });
}

for (const slug of ["integrations", "user-groups", "personalization", "dashboard-host"]) {
  test(`tenant direct /settings/${slug} returns to General without privileged reads`, async ({ page }) => {
    await authenticate(page.context(), "tenant");
    const api = await installSettingsFixtures(page, "tenant");
    await page.goto(`/settings/${slug}`);
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByText("Settings access restricted", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Clear Browser Cache", exact: true })).toBeVisible();
    const forbidden = api.requests.filter(({ path }) => path.startsWith("/integrations") || path.startsWith("/user-groups") || path === "/tenant-access/policies" || path === "/system/resource-usage");
    expect(forbidden).toEqual([]);
    await page.getByRole("button", { name: "Dismiss settings notice" }).click();
    await expect(page.getByText("Settings access restricted", { exact: true })).toHaveCount(0);
    await expectCleanSettingsHarness(api);
  });
}

test("General alias redirects to canonical URL and unknown settings slug is 404", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin");
  await page.goto("/settings/general");
  await expect(page).toHaveURL(/\/settings$/);
  const response = await page.goto("/settings/not-a-real-tab");
  expect(response.status()).toBe(404);
  await expect(page.getByRole("button", { name: "Clear Shared Backend Cache", exact: true })).toHaveCount(0);
  await expectCleanSettingsHarness(api);
});

test("nested settings paths are rejected before streaming", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin");
  const response = await page.goto("/settings/integrations/unknown");
  expect(response.status()).toBe(404);
  expect(api.requests.filter(({ path }) => path.startsWith("/integrations"))).toEqual([]);
  await expectCleanSettingsHarness(api);
});

test("Admin shared cache failure is visible and retry requires another explicit click", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin", { sharedClearFails: true });
  await page.goto("/settings");
  await page.getByRole("button", { name: "Clear Shared Backend Cache", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Fixture shared cache unavailable" })).toBeVisible();
  expect(api.sharedClears).toBe(1);
  await expect(page.getByText("Shared backend cache cleared.", { exact: true })).toHaveCount(0);
  api.sharedClearFails = false;
  await page.getByRole("button", { name: "Clear Shared Backend Cache", exact: true }).click();
  await expect(page.getByText("Shared backend cache cleared.", { exact: true })).toBeVisible();
  expect(api.sharedClears).toBe(2);
  await expectCleanSettingsHarness(api);
});

test("duplicate Admin clear clicks dispatch one pending request", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installSettingsFixtures(page, "admin");
  let release;
  api.holdNextSharedClear = new Promise((resolve) => { release = resolve; });
  await page.goto("/settings");
  const button = page.getByRole("button", { name: "Clear Shared Backend Cache", exact: true });
  await button.evaluate((element) => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(page.getByRole("button", { name: "Clearing Shared Cache…", exact: true })).toBeDisabled();
  await expect.poll(() => api.sharedClears).toBe(1);
  release();
  await expect(page.getByText("Shared backend cache cleared.", { exact: true })).toBeVisible();
  expect(api.sharedClears).toBe(1);
  await expectCleanSettingsHarness(api);
});
