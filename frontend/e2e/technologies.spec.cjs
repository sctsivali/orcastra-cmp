const { test, expect } = require("@playwright/test");
const {
  authenticate,
  capture,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");

async function expectProjectTheme(page, testInfo) {
  const expected = testInfo.project.name.includes("dark") ? "dark" : "light";
  await expect(page.locator("html")).toHaveClass(new RegExp(`(?:^|\\s)${expected}(?:\\s|$)`));
}

for (const role of ["admin", "partner"]) {
  test(`${role} sees platform inventory and keyboard-safe category detail`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const api = await installApiFixtures(page, role);

    await page.goto("/technologies");
    await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Hardware" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Software" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Fleet Totals" })).toBeVisible();
    await expect(page.getByText("Partial totals from 1 of 2 nodes.")).toBeVisible();
    await expect(page.getByText("Hardware by Node", { exact: true })).toHaveCount(0);

    const expectedCards = ["CPU Resources", "Memory", "Storage", "GPU Resources"];
    for (const title of expectedCards) {
      await expect(page.getByRole("button", { name: `View ${title} details` })).toBeVisible();
    }

    const cpuCard = page.getByRole("button", { name: "View CPU Resources details" });
    await cpuCard.focus();
    await expect(cpuCard).toBeFocused();
    await page.keyboard.press("Enter");

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "CPU Resources" })).toBeVisible();
    await expect(dialog.getByRole("region", { name: "Core Cluster hardware" })).toBeVisible();
    await expect(dialog.getByText("Total Hardware Threads")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(cpuCard).toBeFocused();

    await page.getByRole("tab", { name: "Software" }).click();
    await expect(page.getByRole("heading", { name: "Software by Node" })).toBeVisible();
    await expect(page.getByText("Hardware by Node", { exact: true })).toHaveCount(0);

    await expectProjectTheme(page, testInfo);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `${role}-technologies-platform`);
    await expectCleanHarness(api);
  });
}

test("tenant sees only scoped instance specifications", async ({ page }, testInfo) => {
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant");

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { level: 1, name: "Technologies" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Your Instances" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Hardware" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Software" })).toHaveCount(0);
  await expect(page.getByText("Some instance specifications could not be loaded right now")).toBeVisible();

  const cpuCard = page.getByRole("button", { name: "View CPU details" });
  await cpuCard.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "CPU by Instance" })).toBeVisible();
  await expect(dialog.getByRole("region", { name: "tenant-owned-vm CPU specification" })).toBeVisible();
  await expect(dialog.getByText("Running", { exact: true })).toBeVisible();
  await expect(dialog.getByText("running", { exact: true })).toHaveCount(0);

  // Foreign tenant data and platform capacity are planted as sentinels. Neither may reach DOM.
  await expect(page.getByText("foreign-customer-vm", { exact: true })).toHaveCount(0);
  await expect(page.getByText("customer-secret-cluster", { exact: true })).toHaveCount(0);
  await expect(page.getByText("987654321 GiB", { exact: true })).toHaveCount(0);

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(cpuCard).toBeFocused();

  const nodeInventoryRequests = api.requests.filter(
    (request) => request.path === "/nodes" || request.path.startsWith("/nodes/"),
  );
  expect(nodeInventoryRequests, "Tenant must never request node inventory endpoints").toEqual([]);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "tenant-technologies-specifications");
  await expectCleanHarness(api);
});

test("platform inventory error remains distinct from an empty fleet", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { nodeError: true });

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { name: "Could not load node data" })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(page.getByText("No node data yet", { exact: true })).toHaveCount(0);

  await expectProjectTheme(page, testInfo);
  await capture(page, testInfo, "technologies-platform-error");
  await expectCleanHarness(api);
});

test("tenant empty state does not fall back to platform inventory", async ({ page }, testInfo) => {
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { tenantEmpty: true });

  await page.goto("/technologies");
  await expect(page.getByRole("heading", { name: "No instances assigned" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Fleet Totals" })).toHaveCount(0);
  await expect(page.getByText("Some instance specifications could not be loaded right now")).toBeVisible();
  expect(api.requests.filter((request) => request.path.startsWith("/nodes"))).toEqual([]);

  await expectProjectTheme(page, testInfo);
  await capture(page, testInfo, "tenant-technologies-empty");
  await expectCleanHarness(api);
});
