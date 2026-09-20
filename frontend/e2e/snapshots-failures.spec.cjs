const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");
const {
  BASE_CREATE_BODY,
  DEFAULT_TARGET,
  UNKNOWN_RESULT_MESSAGE,
  expectElementNoHorizontalOverflow,
  expectProjectTheme,
  expectSnapshotUrlScope,
  isTargetFleetRead,
  isTargetSnapshotRead,
  isUnscopedFleetRead,
  openTakeSnapshotDialog,
  prepareCreate,
} = require("./support/snapshot-test-helpers.cjs");

for (const scenario of [
  {
    status: 403,
    detail: "Fixture permission changed",
    message: "You no longer have permission to take a snapshot of this instance.",
  },
  {
    status: 409,
    detail: "Snapshot capacity changed on the cluster.",
    message: "Snapshot capacity changed on the cluster.",
  },
  {
    status: 422,
    detail: "Fixture target changed",
    message: "The target or snapshot details are no longer valid. Review them and try again.",
  },
  {
    status: 502,
    detail: "Internal fixture daemon metadata that must not render",
    message: "The cluster could not complete the snapshot. Refresh the target before trying again.",
    error: "snapshot_terminal_failure",
  },
]) {
  test(`Snapshot HTTP ${scenario.status} keeps the reviewed target and shows a safe failure`, async (
    { page },
    testInfo,
  ) => {
    test.skip(testInfo.project.name !== "desktop-light", "Failure mapping needs one profile");

    await authenticate(page.context(), "partner");
    const api = await installApiFixtures(page, "partner", {
      snapshotCreate: {
        instanceName: DEFAULT_TARGET.instanceName,
        expectedBody: BASE_CREATE_BODY,
        status: scenario.status,
        body: {
          detail: scenario.error
            ? { error: scenario.error, message: scenario.detail }
            : scenario.detail,
        },
      },
    });

    await page.goto("/inventory/snapshots");
    const { dialog } = await openTakeSnapshotDialog(page);
    await prepareCreate(page, dialog);
    await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

    const failure = dialog.getByRole("alert").filter({ hasText: "Snapshot not taken" });
    await expect(failure).toContainText(scenario.message);
    await expect(dialog.getByLabel("What is this for")).toHaveValue(BASE_CREATE_BODY.label);
    if (scenario.status === 502) {
      await expect(dialog.getByText(scenario.detail, { exact: true })).toHaveCount(0);
    }
    await expectCleanHarness(api);
  });
}

for (const scenario of [
  {
    title: "A gateway 502 after forwarding the create is an unknown result",
    response: { status: 502, body: { error: "Backend unavailable" } },
  },
  {
    title: "An unreadable success response is an unknown result",
    response: { status: 201, rawBody: '{"success":true' },
  },
]) {
  test(scenario.title, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-light", "Transport boundary needs one profile");

    await authenticate(page.context(), "partner");
    const api = await installApiFixtures(page, "partner", {
      snapshotCreate: {
        instanceName: DEFAULT_TARGET.instanceName,
        expectedBody: BASE_CREATE_BODY,
        ...scenario.response,
      },
    });

    await page.goto("/inventory/snapshots");
    const { dialog } = await openTakeSnapshotDialog(page);
    await prepareCreate(page, dialog);
    await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

    const unknown = dialog.getByRole("alert").filter({ hasText: "Snapshot result unknown" });
    await expect(unknown).toContainText(UNKNOWN_RESULT_MESSAGE);
    await expect(dialog.getByLabel("What is this for")).toHaveValue(BASE_CREATE_BODY.label);
    await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
    expect(api.snapshotMutations).toHaveLength(1);
    await expectCleanHarness(api);
  });
}

test("A snapshot 401 completes auth refresh without retrying the mutation", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Auth retry boundary needs one profile");

  const safeMessage = "Your session could not be confirmed. Sign in again before retrying.";
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
      status: 401,
      body: { detail: safeMessage },
    },
  });

  let authSessionRequests = 0;
  let authSessionResponses = 0;
  await page.route("**/api/auth/session", async (route) => {
    authSessionRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "partner@e2e.invalid",
          authentikId: "partner@e2e.invalid",
          name: "Partner E2E",
          email: "partner@e2e.invalid",
          groups: ["role_partner"],
          roles: ["role_partner"],
        },
        accessToken: "e2e-partner-access-token",
        expires: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      }),
    });
    authSessionResponses += 1;
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);
  const authRequestsBeforeSubmit = authSessionRequests;
  const authResponsesBeforeSubmit = authSessionResponses;
  await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  const failure = dialog.getByRole("alert").filter({ hasText: "Snapshot not taken" });
  await expect(failure).toContainText(safeMessage);
  await expect(dialog.getByLabel("What is this for")).toHaveValue(BASE_CREATE_BODY.label);
  await expect.poll(() => authSessionRequests).toBeGreaterThan(authRequestsBeforeSubmit);
  await expect.poll(() => authSessionResponses).toBeGreaterThan(authResponsesBeforeSubmit);
  await page.waitForTimeout(250);
  expect(api.snapshotMutations).toHaveLength(1);
  await expectCleanHarness(api);
});

test("A structured unknown result verifies the exact target before applying list filters", async (
  { page },
  testInfo,
) => {
  test.skip(
    !["desktop-light", "mobile-dark"].includes(testInfo.project.name),
    "Unknown outcome and mobile layout need two profiles",
  );

  const rawDetail = "Fixture daemon metadata that must never be shown";
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
      status: 502,
      body: {
        detail: { error: "snapshot_outcome_unknown", message: rawDetail },
      },
    },
    snapshotReadSequences: { policy: [null, { delayMs: 350 }] },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);
    await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  const unknown = dialog.getByRole("alert").filter({ hasText: "Snapshot result unknown" });
  await expect(unknown).toContainText(UNKNOWN_RESULT_MESSAGE);
  await expect(dialog.getByText(rawDetail, { exact: true })).toHaveCount(0);
  await expect(dialog.getByLabel("What is this for")).toHaveValue(BASE_CREATE_BODY.label);
    await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  if (testInfo.project.name === "mobile-dark") {
    await expectProjectTheme(page, testInfo);
    await expectNoHorizontalOverflow(page);
    await expectElementNoHorizontalOverflow(dialog);
    await expectElementNoHorizontalOverflow(unknown);
  }

  const requestCheckpoint = api.requests.length;
  const targetReadsBefore = api.requests.filter((entry) => isTargetSnapshotRead(entry)).length;
  await unknown.getByRole("button", { name: "Check snapshots" }).click();
  await expect(unknown.getByRole("button", { name: "Checking snapshots" })).toHaveAttribute(
    "aria-busy",
    "true",
  );
  await expect
    .poll(() => api.requests.filter((entry) => isTargetSnapshotRead(entry)).length)
    .toBeGreaterThan(targetReadsBefore);
  await expect(dialog).toBeVisible();
  expect(new URL(page.url()).searchParams.get("host")).toBeNull();
  expect(new URL(page.url()).searchParams.get("project")).toBeNull();

  await expect(dialog).toBeHidden();
  await expectSnapshotUrlScope(page);

  const postCheckRequests = api.requests.slice(requestCheckpoint);
  const targetIndex = postCheckRequests.findIndex((entry) => isTargetSnapshotRead(entry));
  const narrowedIndex = postCheckRequests.findIndex((entry) => isTargetFleetRead(entry));
  expect(targetIndex).toBeGreaterThan(-1);
  expect(postCheckRequests.some(isUnscopedFleetRead)).toBe(false);
  if (narrowedIndex >= 0) expect(narrowedIndex).toBeGreaterThan(targetIndex);
  expect(api.snapshotMutations).toHaveLength(1);
  await expectCleanHarness(api);
});

test("HTTP 504 is unknown and a failed exact-target check retains the draft", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Unknown outcome needs one profile");

  const rawDetail = "An unrelated reverse proxy deadline string";
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
      status: 504,
      body: { detail: rawDetail },
    },
    snapshotReadSequences: {
      policy: [
        null,
        { status: 403, body: { detail: "Fixture verification read failed" } },
        null,
      ],
    },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);
  await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  const unknown = dialog.getByRole("alert").filter({ hasText: "Snapshot result unknown" });
  await expect(unknown).toContainText(UNKNOWN_RESULT_MESSAGE);
  await expect(dialog.getByText(rawDetail, { exact: true })).toHaveCount(0);
  const targetReadsBefore = api.requests.filter((entry) => isTargetSnapshotRead(entry)).length;
  await unknown.getByRole("button", { name: "Check snapshots" }).click();
  await expect(
    dialog.getByText(
      "The latest snapshot list could not be verified. The result is still unknown. Stay here and try again.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("What is this for")).toHaveValue(BASE_CREATE_BODY.label);
  expect(api.requests.filter((entry) => isTargetSnapshotRead(entry)).length).toBeGreaterThan(
    targetReadsBefore,
  );
  expect(api.requests.some((entry) => isTargetFleetRead(entry))).toBe(false);
  expect(api.snapshotMutations).toHaveLength(1);

  const readsAfterFailedCheck = api.requests.filter((entry) => isTargetSnapshotRead(entry)).length;
  await unknown.getByRole("button", { name: "Check snapshots" }).click();
  await expect(dialog).toBeHidden();
  expect(api.requests.filter((entry) => isTargetSnapshotRead(entry)).length).toBeGreaterThan(
    readsAfterFailedCheck,
  );
  expect(api.snapshotMutations).toHaveLength(1);
  await expectCleanHarness(api);
});

test("An interrupted create reports an unknown result and cannot be retried blindly", async (
  { page },
  testInfo,
) => {
  test.skip(testInfo.project.name !== "desktop-light", "Unknown outcome needs one profile");

  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", {
    snapshotCreate: {
      instanceName: DEFAULT_TARGET.instanceName,
      expectedBody: BASE_CREATE_BODY,
      abort: true,
    },
  });

  await page.goto("/inventory/snapshots");
  const { dialog } = await openTakeSnapshotDialog(page);
  await prepareCreate(page, dialog);
    await dialog.getByRole("button", { name: "Take Snapshot", exact: true }).click();

  const unknown = dialog.getByRole("alert").filter({ hasText: "Snapshot result unknown" });
  await expect(unknown).toContainText(UNKNOWN_RESULT_MESSAGE);
  await expect(unknown.getByRole("button", { name: "Check snapshots" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Take Snapshot", exact: true })).toBeDisabled();
  await expect(dialog.getByLabel("What is this for")).toHaveValue(BASE_CREATE_BODY.label);
  await expectCleanHarness(api);
});
