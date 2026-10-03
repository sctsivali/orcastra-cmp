const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  installApiFixtures,
} = require("./support/fixtures.cjs");
const {
  BASE_CREATE_BODY,
  DEFAULT_TARGET,
  UNKNOWN_RESULT_MESSAGE,
  isTargetSnapshotRead,
  openInstanceSnapshotForm,
} = require("./support/snapshot-test-helpers.cjs");

test("The per-instance entry point submits through the shared stateless form", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Per-instance flow needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
    },
  });

  await page.goto("/inventory/instances");
  await expect(page.getByRole("heading", { level: 1, name: "Instances" })).toBeVisible();
  const { detailDialog, createDialog } = await openInstanceSnapshotForm(page);

  const target = createDialog.getByRole("region", { name: "Target", exact: true });
  await expect(target).toContainText(DEFAULT_TARGET.host);
  await expect(target).toContainText(DEFAULT_TARGET.project);
  await expect(target).toContainText(DEFAULT_TARGET.instanceName);
  await createDialog.getByLabel("What is this for").fill(BASE_CREATE_BODY.label);
  await createDialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  await expect(page.getByText("Snapshot taken", { exact: true })).toBeVisible();
  await expect(createDialog).toBeHidden();
  await expect(
    detailDialog.getByText("ops-maintenance-20260920t120001z", { exact: true }),
  ).toBeVisible();
  expect(api.snapshotReadCounts.policy).toBeGreaterThanOrEqual(2);
  expect(api.snapshotMutations).toHaveLength(1);
  expect(api.snapshotMutations[0].body).toEqual(BASE_CREATE_BODY);
  await expectCleanHarness(api);
});

test("The per-instance dialog rejects a cached policy when fresh revalidation fails", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Fresh preflight needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    snapshotReadSequences: {
      policy: [null, { status: 403, body: { detail: "Fixture policy access was revoked" } }],
    },
  });

  await page.goto("/inventory/instances");
  await page.getByRole("button", { name: DEFAULT_TARGET.instanceName, exact: true }).click();
  const detailDialog = page
    .getByRole("dialog")
    .filter({
      has: page.getByRole("heading", { name: DEFAULT_TARGET.instanceName, exact: true }),
    });
  await detailDialog.getByRole("tab", { name: "Snapshots", exact: true }).click();
  const takeSnapshot = detailDialog.getByRole("button", { name: "Take Snapshot", exact: true });
  await expect(takeSnapshot).toBeEnabled();
  await takeSnapshot.click();

  const createDialog = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("heading", { name: "Take a Snapshot", exact: true }) });
  await expect(
    createDialog.getByRole("alert").filter({ hasText: "Snapshot policy could not be verified" }),
  ).toBeVisible();
  await expect(createDialog.getByLabel("What is this for")).toHaveCount(0);
  await expect(createDialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  expect(api.snapshotReadCounts.policy).toBeGreaterThanOrEqual(2);
  expect(api.snapshotMutations).toEqual([]);
  await expectCleanHarness(api);
});

test("Per-instance unknown check failure retains its draft and guard until retry succeeds", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Unknown recovery needs one profile");

  await authenticate(page.context(), "partner");
  let releaseVerificationResponse;
  const verificationResponseGate = new Promise((resolve) => {
    releaseVerificationResponse = resolve;
  });
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
      status: 504,
      body: { detail: "Fixture proxy deadline" },
    },
    snapshotReadSequences: {
      policy: [
        null,
        null,
        {
          status: 403,
          body: { detail: "Fixture verification read failed" },
          responseGate: verificationResponseGate,
        },
        null,
      ],
    },
  });

  await page.goto("/inventory/instances");
  const { createDialog } = await openInstanceSnapshotForm(page);
  const label = createDialog.getByLabel("What is this for");
  await label.fill(BASE_CREATE_BODY.label);
  await createDialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  const unknown = createDialog
    .getByRole("alert")
    .filter({ hasText: "Snapshot result unknown" });
  await expect(unknown).toContainText(UNKNOWN_RESULT_MESSAGE);
  const targetReadsBefore = api.requests.filter((entry) => isTargetSnapshotRead(entry)).length;
  try {
    await unknown.getByRole("button", { name: "Check snapshots" }).click();

    await expect(unknown.getByRole("button", { name: "Checking snapshots" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    await expect
      .poll(() => api.requests.filter((entry) => isTargetSnapshotRead(entry)).length)
      .toBeGreaterThan(targetReadsBefore);
    await expect(label).toHaveValue(BASE_CREATE_BODY.label);
    await expect(label).toBeDisabled();
    await expect(unknown).toBeVisible();
    await expect(createDialog).toBeVisible();
  } finally {
    releaseVerificationResponse();
  }

  await expect(
    createDialog.getByText(
      "The latest snapshot list could not be verified. The result is still unknown. Stay here and try again.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(unknown).toBeVisible();
  await expect(
    createDialog.getByRole("button", { name: "Take Snapshot", exact: true }),
  ).toBeDisabled();
  const policyFailure = createDialog
    .getByRole("alert")
    .filter({ hasText: "Snapshot policy could not be verified" });
  await expect(policyFailure).toBeVisible();
  expect(api.snapshotMutations).toHaveLength(1);

  await policyFailure.getByRole("button", { name: "Retry policy check" }).click();
  await expect(policyFailure).toBeHidden();
  await expect(label).toHaveValue(BASE_CREATE_BODY.label);
  await expect(unknown).toBeVisible();
  await expect(
    createDialog.getByRole("button", { name: "Take Snapshot", exact: true }),
  ).toBeDisabled();

  await unknown.getByRole("button", { name: "Check snapshots" }).click();
  await expect(createDialog).toBeHidden();
  expect(api.snapshotMutations).toHaveLength(1);
  await expectCleanHarness(api);
});

test("Per-instance reconnect revalidation cannot open a second create flight", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Single-flight race needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotInstancePage: true,
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
      delayMs: 1_500,
    },
    snapshotReadSequences: {
      policy: [null, null, { delayMs: 350 }],
    },
  });

  await page.goto("/inventory/instances");
  const { createDialog } = await openInstanceSnapshotForm(page);
  const label = createDialog.getByLabel("What is this for");
  await label.fill(BASE_CREATE_BODY.label);
  await createDialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();
  await expect.poll(() => api.snapshotMutations.length).toBe(1);
  const policyReadsBeforeReconnect = api.snapshotReadCounts.policy;

  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.waitForTimeout(50);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => api.snapshotReadCounts.policy).toBeGreaterThan(policyReadsBeforeReconnect);

  await expect(createDialog.getByRole("button", { name: "Taking snapshot" })).toHaveAttribute(
    "aria-busy",
    "true",
  );
  await expect(label).toHaveValue(BASE_CREATE_BODY.label);
  await expect(label).toBeDisabled();
  await expect(createDialog.getByRole("region", { name: "Target", exact: true })).toContainText(
    DEFAULT_TARGET.instanceName,
  );
  await createDialog.locator("form").evaluate((form) => {
    form.requestSubmit();
    form.requestSubmit();
  });
  await page.waitForTimeout(100);
  expect(api.snapshotMutations).toHaveLength(1);

  await expect(page.getByText("Snapshot taken", { exact: true })).toBeVisible();
  await expect(createDialog).toBeHidden();
  expect(api.snapshotMutations).toHaveLength(1);
  await expectCleanHarness(api);
});
