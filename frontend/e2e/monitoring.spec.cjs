const { test, expect } = require("@playwright/test");
const {
  authenticate,
  capture,
  countRequests,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");

async function setDocumentVisibility(page, state) {
  await page.evaluate((nextState) => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => nextState,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}

async function expectDomOrder(page, locators) {
  const handles = [];
  for (const locator of locators) handles.push(await locator.elementHandle());
  expect(handles.every(Boolean)).toBe(true);
  const ordered = await page.evaluate(
    ([first, second, third]) =>
      Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) &&
      Boolean(second.compareDocumentPosition(third) & Node.DOCUMENT_POSITION_FOLLOWING),
    handles,
  );
  expect(ordered).toBe(true);
}

test("Monitoring pause freezes automation while manual refresh and resume still work", async ({ page }, testInfo) => {
  // Partner deliberately exercises REST fallback. Admin's monitoring WebSocket is a separate
  // transport contract; pause semantics must also hold when normal polling is active.
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner");

  await page.goto("/inventory/monitoring");
  await expect(page.getByRole("heading", { level: 1, name: "Monitoring" })).toBeVisible();

  const refresh = page.getByRole("button", { name: "Refresh monitoring" });
  const pause = page.getByRole("button", { name: "Pause automatic monitoring updates" });
  const filter = page.getByRole("combobox", { name: "Filter by cluster" });
  await expect(refresh).toBeVisible();
  await expect(pause).toBeVisible();
  await expect(filter).toBeVisible();
  await expectDomOrder(page, [refresh, pause, filter]);

  await pause.hover();
  await expect(page.getByRole("tooltip")).toContainText("Workloads will keep running");

  // History is read for the selected time window only: no prefetch of the other ranges, and
  // never by point count, which assumed a cadence the collector does not have.
  await expect.poll(() => countRequests(api, "/monitoring/history"), { timeout: 8_000 }).toBeGreaterThan(0);
  const windows = new Set(
    api.requests
      .filter((request) => request.path === "/monitoring/history")
      .map((request) => new URLSearchParams(request.search).get("window")),
  );
  expect([...windows]).toEqual(["300"]);
  await expect(page.getByText("Real-time resource metrics across all clusters")).toHaveCount(0);
  await expect(page.getByText(/Updated /).first()).toBeVisible();
  await expect(page.getByText("2.8%", { exact: true })).toBeVisible();
  await expect(page.getByText("0.9 of 32 hardware threads busy")).toBeVisible();

  await pause.click();
  const resume = page.getByRole("button", { name: "Resume automatic monitoring updates" });
  await expect(resume).toHaveAttribute("aria-pressed", "true");
  await page.waitForTimeout(250);
  const pausedDashboardCount = countRequests(api, "/monitoring/dashboard");
  const pausedHistoryCount = countRequests(api, "/monitoring/history");
  await page.waitForTimeout(5_500);
  expect(countRequests(api, "/monitoring/dashboard")).toBe(pausedDashboardCount);
  expect(countRequests(api, "/monitoring/history")).toBe(pausedHistoryCount);

  // A person can still ask for one fresh snapshot while automation remains paused.
  await refresh.click();
  await expect
    .poll(() => countRequests(api, "/monitoring/dashboard"), { timeout: 8_000 })
    .toBeGreaterThan(pausedDashboardCount);
  await expect(resume).toBeVisible();
  expect(
    api.requests.some(
      (request) => request.path === "/monitoring/dashboard" && request.search.includes("fresh=true"),
    ),
  ).toBe(true);

  const manualDashboardCount = countRequests(api, "/monitoring/dashboard");
  await resume.click();
  await expect(page.getByRole("button", { name: "Pause automatic monitoring updates" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await expect
    .poll(() => countRequests(api, "/monitoring/dashboard"), { timeout: 8_000 })
    .toBeGreaterThan(manualDashboardCount);

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "monitoring-controls-and-pause");
  await expectCleanHarness(api);
});

test("Admin pause closes the monitoring WebSocket and resume opens a new stream", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "The transport lifecycle only needs one browser profile");

  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { monitoringWebSocket: true });

  await page.goto("/inventory/monitoring");
  await expect(page.getByRole("heading", { level: 1, name: "Monitoring" })).toBeVisible();
  await expect.poll(() => api.monitoringSockets.length, { timeout: 8_000 }).toBe(1);
  expect(api.monitoringSockets[0].closedAt).toBeNull();

  const pause = page.getByRole("button", { name: "Pause automatic monitoring updates" });
  await pause.click();
  const resume = page.getByRole("button", { name: "Resume automatic monitoring updates" });
  await expect(resume).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => api.monitoringSockets[0].closedAt, { timeout: 8_000 }).not.toBeNull();

  // The hook's reconnect delay starts at two seconds. Waiting past it proves Pause cleared the
  // reconnect path instead of merely closing one socket and immediately replacing it.
  const pausedDashboardCount = countRequests(api, "/monitoring/dashboard");
  await page.waitForTimeout(2_500);
  expect(api.monitoringSockets).toHaveLength(1);
  expect(countRequests(api, "/monitoring/dashboard")).toBe(pausedDashboardCount);

  await page.getByRole("button", { name: "Refresh monitoring" }).click();
  await expect
    .poll(() => countRequests(api, "/monitoring/dashboard"), { timeout: 8_000 })
    .toBeGreaterThan(pausedDashboardCount);
  expect(
    api.requests.some(
      (request) => request.path === "/monitoring/dashboard" && request.search.includes("fresh=true"),
    ),
  ).toBe(true);
  expect(api.monitoringSockets).toHaveLength(1);

  await resume.click();
  await expect.poll(() => api.monitoringSockets.length, { timeout: 8_000 }).toBe(2);
  expect(api.monitoringSockets[0].closedAt).not.toBeNull();
  expect(api.monitoringSockets[1].closedAt).toBeNull();
  await expect(page.getByRole("button", { name: "Pause automatic monitoring updates" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );

  await expectCleanHarness(api);
});

test("Top Consumer hydrates once and live Metrics preserve the last good sample", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner");

  await page.goto("/inventory/monitoring");
  const consumer = page.getByRole("button", {
    name: "Open resource details for tenant-owned-vm on cluster-a in project tenant-a",
  });
  await expect(consumer).toBeVisible();
  await consumer.click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "tenant-owned-vm" })).toBeVisible();
  await expect.poll(() => api.detailCalls).toBe(1);
  await expect(dialog.getByRole("tab", { name: "Overview" })).toHaveAttribute("data-state", "active");
  await expect(dialog.getByText("Resources", { exact: true })).toHaveCount(0);
  expect(api.metricsCalls).toHaveLength(0);

  await dialog.getByRole("tab", { name: "Metrics" }).click();
  await expect.poll(() => api.metricsCalls.length).toBe(1);
  await expect(dialog.getByText("Calculating…", { exact: true })).toBeVisible();
  // The fixture's server clock is 20 s behind; the age comes from when the reading arrived.
  await expect(dialog.getByText(/These readings are stale/)).toHaveCount(0);

  // Browser visibility is part of the activation predicate. A hidden document gets no poll.
  await setDocumentVisibility(page, "hidden");
  const hiddenCount = api.metricsCalls.length;
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(hiddenCount);

  await setDocumentVisibility(page, "visible");
  await expect.poll(() => api.metricsCalls.length, { timeout: 8_000 }).toBeGreaterThan(hiddenCount);
  await expect(dialog.getByText("22.5%", { exact: true })).toBeVisible();

  // Once visible, the next successful read follows the five-second live cadence.
  const beforeCadence = api.metricsCalls.length;
  const previousAt = api.metricsCalls.at(-1).at;
  await expect.poll(() => api.metricsCalls.length, { timeout: 8_000 }).toBeGreaterThan(beforeCadence);
  const cadenceMs = api.metricsCalls.at(-1).at - previousAt;
  expect(cadenceMs).toBeGreaterThanOrEqual(4_500);
  expect(cadenceMs).toBeLessThan(7_500);

  // A 503 leaves the successful sample visible and explains why it is stale.
  api.metricsMode = "unavailable";
  await dialog.getByRole("button", { name: "Refresh instance metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Cluster did not answer" })).toBeVisible({
    timeout: 8_000,
  });
  await expect(dialog.getByText("22.5%", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Showing the last successful readings.")).toBeVisible();

  // A rate limit waits for its Retry-After (30 s here) before polling resumes on its own, and a
  // permission failure stops the loop for good. Neither may keep probing within the wait.
  api.metricsMode = "limited";
  await dialog.getByRole("button", { name: "Refresh instance metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Metrics temporarily limited" })).toBeVisible({
    timeout: 8_000,
  });
  const limitedCount = api.metricsCalls.length;
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(limitedCount);

  api.metricsMode = "forbidden";
  await dialog.getByRole("button", { name: "Refresh instance metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Metrics access denied" })).toBeVisible({
    timeout: 8_000,
  });
  const terminalCount = api.metricsCalls.length;
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(terminalCount);

  await capture(page, testInfo, "monitoring-instance-live-metrics");
  await expectCleanHarness(api);
});

test("stopped Top Consumer checks once, then only at the slow cadence", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { instanceStatus: "Stopped" });

  await page.goto("/inventory/monitoring");
  await page
    .getByRole("button", {
      name: "Open resource details for tenant-owned-vm on cluster-a in project tenant-a",
    })
    .click();

  const dialog = page.getByRole("dialog");
  await expect.poll(() => api.detailCalls).toBe(1);
  const dialogHeading = dialog.getByRole("heading", { name: "tenant-owned-vm" });
  await expect(
    dialogHeading.locator("xpath=..").getByText("Stopped", { exact: true }),
  ).toBeVisible();
  // One read confirms the runtime state; while stopped, the next check is 30 s away, so a
  // start is still noticed without a five-second loop against a stopped instance.
  await dialog.getByRole("tab", { name: "Metrics" }).click();
  await expect(dialog.getByRole("heading", { name: "Instance is not running" })).toBeVisible();
  await expect.poll(() => api.metricsCalls.length, { timeout: 8_000 }).toBe(1);
  await page.waitForTimeout(5_500);
  expect(api.metricsCalls).toHaveLength(1);
  await expect(dialog.getByRole("button", { name: "Check instance metrics again" })).toBeVisible();

  await capture(page, testInfo, "monitoring-stopped-instance-metrics");
  await expectCleanHarness(api);
});
