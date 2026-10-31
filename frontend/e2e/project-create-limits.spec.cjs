const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

const gb = 1_000_000_000;

test("a cluster with no free threads still takes a project limit", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  await page.route(/\/api\/v1\/nodes\/names/, (route) => route.fulfill({ json: ["cluster-a"] }));
  // An over-committed cluster: threads minus allocation is zero, and the cap is still legal.
  await page.route(/\/api\/v1\/nodes\/[^/]+\/resources/, (route) => route.fulfill({ json: {
    cpu_total: 12, cpu_available: 0, memory_total: 30 * gb, memory_available: 21 * gb,
    storage_total: 200 * gb, storage_available: 175 * gb,
  } }));
  let posted = null;
  await page.route(/\/api\/v1\/projects(\?|$)/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posted = route.request().postDataJSON();
    return route.fulfill({ status: 201, json: { success: true, message: "created", project: { name: posted.name } } });
  });

  await page.goto("/create");
  await page.getByLabel("Project Name").fill("qa-overcommitted");
  await page.getByRole("combobox").filter({ hasText: /Select a cluster/ }).click();
  await page.getByRole("option").first().click();
  await expect(page.getByText("Hardware threads: 0 available (of 12)")).toBeVisible();
  await page.getByLabel("Max vCPUs *").fill("1");
  await page.getByLabel("Max Memory *").fill("1");
  await page.getByLabel("Max Storage *").fill("1");

  await page.getByRole("button", { name: "Create Project", exact: true }).click();
  await expect.poll(() => posted?.max_cpu).toBe(1);
});
