const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { expectCleanTaskHarness, installTaskFixtures } = require("./support/task-fixtures.cjs");

async function openQueue(page) {
  await page.goto("/inventory/instances");
  await page.getByRole("tab", { name: /^Queue/ }).click();
  return page.getByRole("tabpanel", { name: /^Queue/ });
}

for (const role of ["admin", "partner", "tenant"]) {
  test(`${role}: queue pages retain server pending ranks and clamp after shrink`, async ({ page, context }) => {
    await authenticate(context, role);
    const state = await installTaskFixtures(page, role);
    const queue = await openQueue(page);
    await expect(queue.locator("li[data-task-id]")).toHaveCount(100);
    await expect(queue.getByRole("group", { name: "task queue" })).toBeVisible();
    await expect(queue).toContainText("1 to 100 of 120 open tasks");
    await queue.getByRole("button", { name: "Next page, task queue" }).click();
    await expect(queue.locator("li[data-task-id]")).toHaveCount(20);
    await expect(queue.locator("li[data-task-id]").first().locator("span").first()).toHaveText("101");
    await expect(queue).toContainText("101 to 120 of 120 open tasks");
    expect(state.taskRequests.some((request) => request.path === "/tasks/queue" && request.limit === "100" && request.offset === "100")).toBe(true);
    state.queueRows = state.queueRows.slice(0, 5);
    await page.getByRole("button", { name: "Refresh queue", exact: true }).click();
    await expect(queue.locator("li[data-task-id]")).toHaveCount(5);
    await expect(queue.getByRole("group", { name: "task queue" })).toHaveCount(0);
    await expect(queue).toContainText("The hypervisor schedules execution independently.");
    await expectCleanTaskHarness(state);
  });

  test(`${role}: interrupted sources produce Degraded and an honest empty queue`, async ({ page, context }) => {
    await authenticate(context, role);
    const state = await installTaskFixtures(page, role, { openTasks: 0 });
    const queue = await openQueue(page);
    await expect(page.locator('[data-live-state="connected"]')).toBeVisible();
    state.disconnected = ["cluster-a"];
    await expect(page.locator('[data-live-state="degraded"]')).toBeVisible({ timeout: 15_000 });
    await expect(queue).toContainText("No open tasks recorded");
    await expect(queue).toContainText("Unavailable hypervisor sources may have tasks that are not recorded here yet.");
    state.disconnected = [];
    await page.getByRole("button", { name: "Refresh queue", exact: true }).click();
    await expect(queue).toContainText("Nothing is waiting");
    await page.getByRole("button", { name: "Collapse task panel" }).click();
    await page.getByRole("button", { name: "Refresh tasks", exact: true }).click();
    await expect(page.locator('[data-live-state="connected"]')).toBeVisible();
    await expectCleanTaskHarness(state);
  });

  test(`${role}: due schedules are capped and only appear on the first queue page`, async ({ page, context }) => {
    await authenticate(context, role);
    const state = await installTaskFixtures(page, role, { dueSchedules: 105 });
    const queue = await openQueue(page);
    await expect(queue).toContainText("Showing the first 100 of 105 due schedules.");
    await expect(queue.getByText("Fixture schedule 100 on tenant-owned-vm", { exact: true })).toHaveCount(1);
    await expect(queue.getByText("Fixture schedule 101 on tenant-owned-vm", { exact: true })).toHaveCount(0);
    await expect(queue.getByRole("link", { name: "View schedules" })).toHaveCount(role === "tenant" ? 0 : 1);
    await queue.getByRole("button", { name: "Next page, task queue" }).click();
    await expect(queue).toContainText("Due schedules appear on the first queue page.");
    await expect(queue.getByText("Fixture schedule 1 on tenant-owned-vm", { exact: true })).toHaveCount(0);
    await expectCleanTaskHarness(state);
  });

  test(`${role}: manual refresh observes collector recovery after an inactive hello`, async ({ page, context }) => {
    await authenticate(context, role);
    const state = await installTaskFixtures(page, role, { openTasks: 0, followerActive: false });
    const queue = await openQueue(page);
    await expect(page.locator('[data-live-state="journal-paused"]')).toBeVisible();
    await expect(queue).toContainText("No open tasks recorded");
    const summariesBefore = state.taskRequests.filter((entry) => entry.path === "/tasks" && entry.limit === "0").length;
    state.followerActive = true;
    await page.getByRole("button", { name: "Refresh queue", exact: true }).click();
    await expect(page.locator('[data-live-state="connected"]')).toBeVisible();
    await expect(queue).toContainText("Nothing is waiting");
    expect(state.taskRequests.filter((entry) => entry.path === "/tasks" && entry.limit === "0").length).toBeGreaterThan(summariesBefore);
    await expectCleanTaskHarness(state);
  });

  test(`${role}: cancellation is role-gated and acceptance waits for terminal readback`, async ({ page, context }) => {
    await authenticate(context, role);
    const state = await installTaskFixtures(page, role, { openTasks: 1, cancel: true });
    await page.goto("/inventory/instances");
    await page.getByRole("tab", { name: /^Tasks/ }).click();
    const tasks = page.getByRole("tabpanel", { name: /^Tasks/ });
    const id = state.queueRows[0].id;
    const actions = tasks.getByRole("button", { name: `Actions for task ${id}` });
    await actions.focus();
    await actions.press("Enter");
    if (role === "tenant") {
      await expect(page.getByRole("menuitem", { name: "Cancel Task" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      expect(state.taskMutations).toEqual([]);
    } else {
      await page.getByRole("menuitem", { name: "Cancel Task" }).click();
      const dialog = page.getByRole("alertdialog", { name: "Cancel Task" });
      await expect(dialog).toContainText("The task may continue until the hypervisor confirms it.");
      await dialog.getByRole("button", { name: "Cancel Task", exact: true }).evaluate((button) => {
        button.click();
        button.click();
      });
      await expect(dialog).toBeHidden();
      await expect.poll(() => state.taskMutations.length).toBe(1);
      expect(state.queueRows[0].status).toBe("pending");
      await expect(tasks).toContainText("Cancelled");
      expect(state.cancelDetailReads).toBeGreaterThanOrEqual(3);
      expect(state.taskMutations).toEqual([{ path: `/tasks/${id}`, method: "DELETE" }]);
    }
    await expectCleanTaskHarness(state);
  });
}
