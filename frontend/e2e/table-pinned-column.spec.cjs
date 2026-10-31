const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installInventoryHardeningFixtures } = require("./support/inventory-hardening-fixtures.cjs");
const { installTaskFixtures } = require("./support/task-fixtures.cjs");
const { fullInstance } = require("./support/api-fixture-data.cjs");

// Headless Chromium hides scrollbars unless told otherwise, which is how a table with no bounded
// height shipped owning a 10 px vertical scrollbar: one pixel of the pinned cell's ground.
test.use({
  launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] },
  viewport: { width: 1100, height: 800 },
});

function instanceRows(count) {
  return Array.from({ length: count }, (_, index) => ({
    ...fullInstance(),
    name: `qa-pinned-${String(index + 1).padStart(2, "0")}`,
  }));
}

async function openInstances(page, context) {
  await authenticate(context, "admin");
  await installInventoryHardeningFixtures(page, "admin");
  await page.route(/\/api\/v1\/instances(\?|$)/, (route) => {
    const vms = instanceRows(6);
    return route.fulfill({ json: {
      total: vms.length, vms, nodes: ["cluster-a"], unreachable_nodes: [], node_errors: {},
      lxd_count: 1, microcloud_count: 0, incus_count: 0, offset: 0, limit: 0, has_more: false,
      collected_at: new Date().toISOString(),
    } });
  });
  await page.goto("/inventory/instances");
  await expect(page.getByText("qa-pinned-01", { exact: true })).toBeVisible();
  return page.locator('[data-slot="table-container"]').first();
}

test("a table with no bounded height never owns a vertical scrollbar", async ({ page, context }) => {
  const container = await openInstances(page, context);
  await expect(container).toHaveAttribute("data-scroll-x", "start");
  for (const zoom of ["1", "1.1", "1.25", "1.5"]) {
    await page.evaluate((value) => { document.body.style.zoom = value; }, zoom);
    await page.waitForFunction(() => true);
    const box = await container.evaluate((el) => ({
      overflowHeight: el.scrollHeight - el.clientHeight,
      scrollbarWidth: el.offsetWidth - el.clientWidth - el.clientLeft * 2,
    }));
    expect(box.overflowHeight, `content taller than the box at zoom ${zoom}`).toBeLessThanOrEqual(0);
    expect(box.scrollbarWidth, `vertical scrollbar at zoom ${zoom}`).toBe(0);
  }
});

test("the pinned column casts its shadow outward onto the rows under it", async ({ page, context }) => {
  const container = await openInstances(page, context);
  const cell = container.locator('tbody td[data-pinned="end"]').first();
  const shadow = () => cell.evaluate((el) => getComputedStyle(el, "::after").boxShadow);

  const atStart = await shadow();
  expect(atStart, "rows slide under the column, so it says so").not.toBe("none");
  expect(atStart, "an inset shadow paints inside the column and reads as recessed").not.toContain("inset");

  // note: inside the poll, because the table can still grow a few pixels after the first paint,
  // and then a single scroll stops short of the end and "middle" is the correct answer.
  await expect.poll(async () => {
    await container.evaluate((el) => { el.scrollLeft = el.scrollWidth; });
    return shadow();
  }, { message: "nothing left under the column at the end" }).toBe("none");
});

test("the pinned column paints the ground of the surface it sits on", async ({ page, context }) => {
  const container = await openInstances(page, context);
  const card = await container.evaluate((el) => {
    const surface = el.closest('[class*="bg-card"]');
    return surface ? getComputedStyle(surface).backgroundColor : null;
  });
  expect(card).not.toBeNull();
  const cell = container.locator('tbody td[data-pinned="end"]').first();
  await expect.poll(() => cell.evaluate((el) => getComputedStyle(el, "::before").backgroundColor)).toBe(card);
});

test("the task dock's pinned column paints the dock's ground, not a card's", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installTaskFixtures(page, "admin", { openTasks: 1, cancel: true });
  await page.goto("/inventory");
  await page.getByRole("tab", { name: /^Tasks/ }).click();
  const tasks = page.getByRole("tabpanel", { name: /^Tasks/ });
  const cell = tasks.locator('tbody td[data-pinned="end"]').first();
  await expect(cell).toBeVisible();
  const colours = await cell.evaluate((el) => {
    let surface = el.parentElement;
    while (surface && getComputedStyle(surface).backgroundColor === "rgba(0, 0, 0, 0)") surface = surface.parentElement;
    return { ground: getComputedStyle(el, "::before").backgroundColor, surface: getComputedStyle(surface).backgroundColor };
  });
  expect(colours.ground).toBe(colours.surface);
});
