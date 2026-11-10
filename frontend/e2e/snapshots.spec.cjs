const { test, expect } = require("@playwright/test");
const {
  authenticate,
  capture,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  expectServerRedirectHome,
  installApiFixtures,
} = require("./support/fixtures.cjs");
const {
  BASE_CREATE_BODY,
  expectElementNoHorizontalOverflow,
  expectFocusWithin,
  expectPendingTouchTargets,
  expectProjectTheme,
  expectSnapshotUrlScope,
  isFreshTargetFleetRead,
  isTargetSnapshotRead,
  openTakeSnapshotDialog,
  prepareCreate,
  selectFixtureTarget,
  selectOption,
} = require("./support/snapshot-test-helpers.cjs");

test("Partner takes one stateless snapshot through the explicit target cascade", async (
  { page },
  testInfo,
) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotCreate: {
      instanceName: "tenant-owned-vm",
      expectedBody: BASE_CREATE_BODY,
      delayMs: 1_200,
    },
    snapshotReconciliation: { freshInstanceReads: 1 },
  });

  await page.goto("/inventory/snapshots");
  await expect(page.getByRole("heading", { level: 1, name: "Snapshots" })).toBeVisible();

  const headerRefresh = page.getByRole("button", { name: "Refresh snapshots" }).first();
  const headerCreate = page.getByRole("button", { name: "Take Snapshot", exact: true }).first();
  await expect(headerRefresh).toBeVisible();
  await expect(headerCreate).toBeVisible();
  const refreshPrecedesCreate = await headerRefresh.evaluate((refresh, create) => {
    return Boolean(refresh.compareDocumentPosition(create) & Node.DOCUMENT_POSITION_FOLLOWING);
  }, await headerCreate.elementHandle());
  expect(refreshPrecedesCreate).toBe(true);
  if ((page.viewportSize()?.width ?? 0) >= 640) {
    const refreshBox = await headerRefresh.boundingBox();
    const createBox = await headerCreate.boundingBox();
    expect(createBox?.x ?? 0).toBeGreaterThan((refreshBox?.x ?? 0) + (refreshBox?.width ?? 0));
  }

  const { dialog, trigger } = await openTakeSnapshotDialog(page);
  await expect(dialog).toHaveCSS("position", "fixed");
  await expect(dialog.getByRole("combobox", { name: "Project" })).toBeDisabled();
  await expect(dialog.getByRole("combobox", { name: "Instance" })).toBeDisabled();
  const cluster = dialog.getByRole("combobox", { name: "Cluster" });
  await cluster.focus();
  await cluster.press("Enter");
  await expect(page.getByRole("option").filter({ hasText: "Edge Cluster" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await expect(
    page.getByRole("option").filter({ hasText: "Untrusted Cluster" }),
  ).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");
  await selectFixtureTarget(page, dialog);

  const label = dialog.getByLabel("What is this for");
  const expiry = dialog.getByLabel("Keep for (days)");
  await expect(label).toHaveAttribute("maxlength", "48");
  await expect(expiry).toHaveAttribute("min", "1");
  await expect(expiry).toHaveAttribute("max", "7");
  await label.fill(BASE_CREATE_BODY.label);
  await expect(dialog.getByText(/does not capture guest memory/i)).toBeVisible();
  await expect(dialog.getByText(/not an off-host backup/i)).toBeVisible();
  await expectNoHorizontalOverflow(page);

  const submit = dialog.getByRole("button", { name: "Take Snapshot", exact: true });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(dialog.getByRole("button", { name: "Taking snapshot" })).toHaveAttribute(
    "aria-busy",
    "true",
  );

  for (const name of ["Cluster", "Project", "Instance"]) {
    await expect(dialog.getByRole("combobox", { name })).toBeDisabled();
  }
  const projectDuringSubmit = dialog.getByRole("combobox", { name: "Project" });
  await projectDuringSubmit.evaluate((button) => {
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await dialog.locator("form").evaluate((form) => {
    form.requestSubmit();
    form.requestSubmit();
  });
  await expect(projectDuringSubmit).toContainText("tenant-a");
  await expect(page.getByRole("option").filter({ hasText: "operations" })).toHaveCount(0);
  await expect.poll(() => api.snapshotMutations.length).toBe(1);

  await expect(page.getByText("Snapshot taken", { exact: true })).toBeVisible();
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  await expect(page.getByText("ops-maintenance-20260920t120001z", { exact: true })).toBeVisible();

  expect(api.snapshotMutations).toHaveLength(1);
  expect(api.snapshotMutations[0]).toMatchObject({
    method: "POST",
    path: "/instances/tenant-owned-vm/snapshots",
    search: "",
    body: BASE_CREATE_BODY,
  });
  expect(
    api.requests.some((entry) => entry.path === "/projects/cluster-a" && entry.search === ""),
  ).toBe(true);
  expect(
    api.requests.some((entry) => {
      if (entry.path !== "/instances") return false;
      const params = new URLSearchParams(entry.search);
      return params.get("host") === "cluster-a" && params.get("project") === "tenant-a";
    }),
  ).toBe(true);
  expect(
    api.requests.some(
      (entry) =>
        entry.path === "/instances/tenant-owned-vm" &&
        new URLSearchParams(entry.search).get("project") === "tenant-a",
    ),
  ).toBe(true);
  expect(
    api.requests.some(
      (entry) =>
        entry.path === "/instances/tenant-owned-vm/snapshots" &&
        new URLSearchParams(entry.search).get("project") === "tenant-a",
    ),
  ).toBe(true);
  await expect.poll(() => api.snapshotReconciliation.freshInstanceReads).toBe(1);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "fleet-snapshot-created");
  await expectCleanHarness(api);
});

test("Admin sees a disabled create entry point in read-only mode", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "Role and read-only state need one profile");

  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", { snapshotPage: true, readOnly: true });

  await page.goto("/inventory/snapshots");
  const create = page.getByRole("button", { name: "Take Snapshot", exact: true }).first();
  await expect(create).toBeVisible();
  await expect(create).toBeDisabled();
  await expect(create).toHaveAttribute("title", "Action disabled in read-only mode");
  expect(api.snapshotMutations).toHaveLength(0);

  await capture(page, testInfo, "fleet-snapshot-read-only");
  await expectCleanHarness(api);
});

test("Snapshot dialog traps focus and restores its opener", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "Keyboard focus needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { snapshotPage: true });

  await page.goto("/inventory/snapshots");
  const firstOpen = await openTakeSnapshotDialog(page);
  await expectFocusWithin(firstOpen.dialog);
  const cluster = firstOpen.dialog.getByRole("combobox", { name: "Cluster" });
  const close = firstOpen.dialog.getByRole("button", { name: "Close" });
  await expect(cluster).toBeEnabled();
  await cluster.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(close).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(cluster).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(firstOpen.dialog).toBeHidden();
  await expect(firstOpen.trigger).toBeFocused();

  await firstOpen.trigger.press("Enter");
  const reopened = page.getByRole("dialog");
  await expect(reopened).toBeVisible();
  await expectFocusWithin(reopened);
  await reopened.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(reopened).toBeHidden();
  await expect(firstOpen.trigger).toBeFocused();
  await expectCleanHarness(api);
});

test("Project and instance candidates stay interactive while authoritative checks finish", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Concurrency contract needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotReadSequences: { projects: [{ delayMs: 2_500 }] },
  });

  await page.goto("/inventory/snapshots");
  await expect.poll(() => api.snapshotReadCounts.fleetProjects ?? 0).toBeGreaterThan(0);
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Cluster", "Core Cluster");

  const project = dialog.getByRole("combobox", { name: "Project" });
  const verifying = dialog.getByText("Verifying projects on this cluster", { exact: true });
  await expect(verifying).toBeVisible();
  await expect(project).toBeEnabled();
  await selectOption(page, dialog, "Project", "tenant-a");

  await expect.poll(() => api.snapshotReadCounts.instances ?? 0).toBeGreaterThan(0);
  await expect(verifying).toBeVisible();
  const instance = dialog.getByRole("combobox", { name: "Instance" });
  await expect(instance).toBeEnabled();
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");

  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  expect(api.snapshotReadCounts.detail ?? 0).toBe(0);
  expect(api.snapshotReadCounts.policy ?? 0).toBe(0);
  await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();

  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toContainText(
    "tenant-owned-vm",
  );
  expect(api.snapshotReadCounts.detail ?? 0).toBeGreaterThan(0);
  expect(api.snapshotReadCounts.policy ?? 0).toBeGreaterThan(0);
  await expectCleanHarness(api);
});

test("Authoritative project removal clears a provisional target", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "Fail-closed target proof needs one profile");

  let releaseAuthoritativeProjects;
  const authoritativeProjectsGate = new Promise((resolve) => {
    releaseAuthoritativeProjects = resolve;
  });
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotReadSequences: {
      projects: [
        {
          responseGate: authoritativeProjectsGate,
          body: {
            host: "cluster-a",
            projects: [
              {
                name: "operations",
                description: "Authoritative project",
                config: {},
                used_by: [],
                host: "cluster-a",
                tags: [],
              },
            ],
            total: 1,
            reachable: true,
            error: null,
          },
        },
      ],
    },
  });

  await page.goto("/inventory/snapshots");
  await expect.poll(() => api.snapshotReadCounts.fleetProjects ?? 0).toBeGreaterThan(0);
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Cluster", "Core Cluster");
  await selectOption(page, dialog, "Project", "tenant-a");
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  // The authoritative list lands only once the provisional target is complete. A fixed delay
  // would race the three picks, and on a slow runner the instance list re-renders under the click.
  releaseAuthoritativeProjects();

  await expect(
    dialog.getByText("The filtered project is no longer available. Choose another project."),
  ).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "Project" })).not.toContainText("tenant-a");
  await expect(dialog.getByRole("combobox", { name: "Instance" })).toBeDisabled();
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  expect(api.snapshotReadCounts.detail ?? 0).toBe(0);
  expect(api.snapshotReadCounts.policy ?? 0).toBe(0);
  expect(api.snapshotMutations).toEqual([]);
  await expectCleanHarness(api);
});

test("A cold scoped worker retries without a manual target reset", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "Cold-worker retry needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotReadSequences: {
      instances: [
        {
          status: 503,
          body: { detail: "Instance inventory is temporarily unavailable" },
        },
        {
          status: 503,
          body: { detail: "Instance inventory is temporarily unavailable" },
        },
        {
          status: 503,
          body: { detail: "Instance inventory is temporarily unavailable" },
        },
      ],
    },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Cluster", "Core Cluster");
  await selectOption(page, dialog, "Project", "tenant-a");

  await expect.poll(() => api.snapshotReadCounts.instances ?? 0).toBe(4);
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toContainText(
    "tenant-owned-vm",
  );
  expect(api.snapshotMutations).toEqual([]);
  await expectCleanHarness(api);
});

test("Changing project clears the instance and its snapshot draft", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "Cascade behavior needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotBillingLinked: true,
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);
  await dialog.getByLabel("Reason").fill("Fixture reason that must not cross targets");
  await dialog
    .getByLabel("I accept the effect on the schedule this instance is billed under")
    .click();
  await selectOption(page, dialog, "Project", "operations");

  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("combobox", { name: "Instance" })).not.toHaveText(
    "tenant-owned-vm",
  );
  await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();

  await selectOption(page, dialog, "Project", "tenant-a");
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  await expect(dialog.getByLabel("What is this for")).toHaveValue("");
  await expect(dialog.getByLabel("Reason")).toHaveValue("");
  await expect(
    dialog.getByLabel("I accept the effect on the schedule this instance is billed under"),
  ).not.toBeChecked();
  await expectCleanHarness(api);
});

test("A late policy response cannot revive a target from the previous project", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Stale response guard needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotReadSequences: { policy: [{ delayMs: 350 }] },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Cluster", "Core Cluster");
  await selectOption(page, dialog, "Project", "tenant-a");
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  await expect.poll(() => api.snapshotReadCounts.policy ?? 0).toBeGreaterThan(0);

  await selectOption(page, dialog, "Project", "operations");
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  await page.waitForTimeout(400);
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("combobox", { name: "Instance" })).not.toHaveText(
    "tenant-owned-vm",
  );
  await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  expect(api.snapshotMutations).toEqual([]);
  await expectCleanHarness(api);
});

test("A late instance detail cannot revive a target from the previous project", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Stale response guard needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotReadSequences: { detail: [{ delayMs: 1000 }] },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Cluster", "Core Cluster");
  await selectOption(page, dialog, "Project", "tenant-a");
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  await expect.poll(() => api.snapshotReadCounts.detail ?? 0).toBeGreaterThan(0);

  await selectOption(page, dialog, "Project", "operations");
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  await page.waitForTimeout(1050);
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("combobox", { name: "Instance" })).not.toHaveText(
    "tenant-owned-vm",
  );
  await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  expect(api.snapshotReadCounts.policy ?? 0).toBeGreaterThan(0);
  expect(api.snapshotMutations).toEqual([]);
  await expectCleanHarness(api);
});

test("Retry target check recovers after the selected cluster misses one instance read", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Reachability recovery needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotInstancesUnreachableReads: 1,
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Cluster", "Core Cluster");
  await selectOption(page, dialog, "Project", "tenant-a");

  const readFailure = dialog.getByText(
    "The selected cluster did not answer the instance read.",
    { exact: false },
  );
  await expect(readFailure).toBeVisible();
  const instance = dialog.getByRole("combobox", { name: "Instance" });
  await expect(instance).toBeDisabled();
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toHaveCount(0);
  const targetFailure = dialog
    .getByRole("alert")
    .filter({ hasText: "Target could not be verified" });
  await expect(targetFailure).toBeVisible();
  const readsBeforeRetry = api.snapshotReadCounts.instances;
  const detailReadsBeforeRetry = api.snapshotReadCounts.detail ?? 0;
  const policyReadsBeforeRetry = api.snapshotReadCounts.policy ?? 0;
  const requestCheckpoint = api.requests.length;

  await targetFailure.getByRole("button", { name: "Retry target check" }).click();
  await expect.poll(() => api.snapshotReadCounts.instances).toBeGreaterThan(readsBeforeRetry);
  await expect(readFailure).toBeHidden();
  const recoveryRead = api.requests.slice(requestCheckpoint).find((entry) => {
    if (entry.path !== "/instances") return false;
    const params = new URLSearchParams(entry.search);
    return (
      params.get("host") === "cluster-a" &&
      params.get("project") === "tenant-a" &&
      params.get("fresh") === "true"
    );
  });
  expect(recoveryRead).toBeDefined();
  expect(api.snapshotReadCounts.detail ?? 0).toBe(detailReadsBeforeRetry);
  expect(api.snapshotReadCounts.policy ?? 0).toBe(policyReadsBeforeRetry);
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toContainText(
    "tenant-owned-vm",
  );
  await expect(dialog.getByLabel("What is this for")).toBeVisible();
  expect(api.snapshotMutations).toEqual([]);
  await expectCleanHarness(api);
});

test("Billing-linked target requires a reason and explicit acknowledgement", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Billing gate needs one profile");

  const expectedBody = {
    ...BASE_CREATE_BODY,
    reason: "Emergency maintenance window",
    acknowledge_billing_impact: true,
  };
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotBillingLinked: true,
    snapshotCreate: { instanceName: "tenant-owned-vm", expectedBody },
    snapshotReconciliation: { freshInstanceReads: 1 },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);

  const submit = dialog.getByRole("button", { name: "Snapshot anyway" });
  await expect(dialog.getByText(/managed and billed by another product/i)).toBeVisible();
  await expect(submit).toBeDisabled();
  await dialog.getByLabel("Reason").fill(expectedBody.reason);
  await expect(submit).toBeDisabled();
  const acknowledgement = dialog.getByLabel(
    "I accept the effect on the schedule this instance is billed under",
  );
  await acknowledgement.click();
  await expect(acknowledgement).toBeChecked();
  await expect(submit).toBeEnabled();
  await submit.click();

  await expect(page.getByText("Snapshot taken", { exact: true })).toBeVisible();
  expect(api.snapshotMutations[0].body).toEqual(expectedBody);
  await expect.poll(() => api.snapshotReconciliation.freshInstanceReads).toBe(1);
  await expectCleanHarness(api);
});

test("A full operator snapshot set blocks create and points to the existing list", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Capacity state needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotCount: 5,
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);

  await expect(dialog.getByRole("alert").filter({ hasText: "Snapshot limit reached" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "View existing snapshots" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  await expectCleanHarness(api);
});

test("Pending refresh uses its create target, not page scope", async ({ page }, testInfo) => {
  test.skip(
    !["desktop-light", "mobile-dark"].includes(testInfo.project.name),
    "Pending scope and mobile layout need two profiles",
  );

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotFreshFleetDelayMs: 350,
    snapshotCreate: {
      instanceName: "tenant-owned-vm",
      expectedBody: BASE_CREATE_BODY,
      status: 202,
    },
    snapshotReconciliation: {
      freshInstanceReads: 2,
      operation: {
        id: "fixture-operation-1",
        host: "cluster-a",
        body: { status: "Success", status_code: 200, err: "" },
      },
    },
  });

  await page.goto("/inventory/snapshots?host=cluster-a&project=operations");
  const { dialog } = await openTakeSnapshotDialog(page);
  await selectOption(page, dialog, "Project", "tenant-a");
  await selectOption(page, dialog, "Instance", "tenant-owned-vm");
  await dialog.getByLabel("What is this for").fill(BASE_CREATE_BODY.label);
  await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  await expect(dialog).toBeHidden();
  const pending = page
    .getByRole("status")
    .filter({ hasText: "ops-maintenance-20260920t120000z" });
  await expect(pending).toContainText("Snapshot creation is in progress");
  await expect(pending).toContainText("ops-maintenance-20260920t120000z");
  await expect(pending).toContainText("tenant-owned-vm");
  await expect(pending).toContainText("tenant-a");
  await expect(page.getByText("Snapshot taken", { exact: true })).toHaveCount(0);
  expect(new URL(page.url()).searchParams.get("project")).toBe("operations");

  const requestCheckpoint = api.requests.length;
  const refresh = pending.getByRole("button", { name: "Refresh snapshots" });
  await refresh.click();
  await expect(refresh).toHaveAttribute("aria-busy", "true");
  await expect
    .poll(() => api.requests.slice(requestCheckpoint).some((entry) => isTargetSnapshotRead(entry)))
    .toBe(true);
  await expect
    .poll(() =>
      api.requests.slice(requestCheckpoint).some((entry) => isFreshTargetFleetRead(entry)),
    )
    .toBe(true);

  const refreshRequests = api.requests.slice(requestCheckpoint);
  const exactIndex = refreshRequests.findIndex((entry) => isTargetSnapshotRead(entry));
  const scopedFleetIndex = refreshRequests.findIndex((entry) => isFreshTargetFleetRead(entry));
  expect(exactIndex).toBeGreaterThan(-1);
  expect(scopedFleetIndex).toBeGreaterThan(exactIndex);
  expect(
    refreshRequests.some((entry) => {
      if (entry.path !== "/snapshots") return false;
      const params = new URLSearchParams(entry.search);
      return params.get("host") !== "cluster-a" || params.get("project") !== "tenant-a";
    }),
  ).toBe(false);
  await expectSnapshotUrlScope(page);
  await expect(refresh).toHaveAttribute("aria-busy", "false");
  await expect(pending).toBeVisible();
  await expect.poll(() => api.snapshotReconciliation.operationReads.length).toBe(1);
  await expect.poll(() => api.snapshotReconciliation.freshInstanceReads).toBe(2);

  if (testInfo.project.name === "mobile-dark") {
    await expectProjectTheme(page, testInfo);
    await expectNoHorizontalOverflow(page);
    await expectElementNoHorizontalOverflow(pending);
    await expectPendingTouchTargets(page, pending, refresh);
  }
  await expectCleanHarness(api);
});

test("Long target identifiers remain readable without mobile dark-mode overflow", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "mobile-dark", "This is the mobile dark visual boundary");

  const longTarget = {
    host: "cluster-production-west-authoritative-name-with-long-id-000001",
    hostLabel: "cluster-production-west-authoritative-name-with-long-id-000001",
    project: "project-enterprise-billing-scope-with-long-name-0000000001",
    instanceName: "instance-customer-workload-with-long-name-000000000000001",
  };
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotPage: true,
    snapshotTarget: longTarget,
    snapshotBillingLinked: true,
    snapshotCount: 5,
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await expect(dialog).toHaveCSS("position", "fixed");
  await selectFixtureTarget(page, dialog, longTarget);

  const target = dialog.getByRole("region", { name: "Target", exact: true });
  await expect(target).toContainText(longTarget.host);
  await expect(target).toContainText(longTarget.project);
  await expect(target).toContainText(longTarget.instanceName);
  await expect(dialog.getByText(/managed and billed by another product/i)).toBeVisible();
  const capacity = dialog.getByRole("alert").filter({ hasText: "Snapshot limit reached" });
  await expect(capacity).toBeVisible();
  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectElementNoHorizontalOverflow(dialog);
  await expectElementNoHorizontalOverflow(target);
  await expectElementNoHorizontalOverflow(capacity);
  await capture(page, testInfo, "fleet-snapshot-long-target-mobile-dark");
  await expectCleanHarness(api);
});

test("Tenant deep link never mounts the fleet snapshot reads or create action", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "The role boundary needs one profile");

  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", { snapshotPage: true });

  // The server refuses the page before it loads (auth.ts); the tenant lands home.
  await expectServerRedirectHome(page, "/inventory/snapshots");
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Take Snapshot", exact: true })).toHaveCount(0);
  expect(
    api.requests.filter(
      (entry) =>
        entry.path === "/snapshots" ||
        entry.path === "/snapshot-schedules" ||
        entry.path.endsWith("/snapshots"),
    ),
  ).toEqual([]);
  expect(api.mutations).toEqual([]);
  await expectCleanHarness(api);
});
