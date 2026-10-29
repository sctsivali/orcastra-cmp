const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectNoHorizontalOverflow,
} = require("./support/fixtures.cjs");
const {
  EXPIRED,
  expectCleanMigrationHarness,
  installMigrationFixtures,
  makeJob,
  makePlan,
} = require("./support/migration-fixtures.cjs");

async function expectProjectTheme(page, testInfo) {
  const expected = testInfo.project.name.includes("dark") ? "dark" : "light";
  await expect(page.locator("html")).toHaveClass(new RegExp(`(?:^|\\s)${expected}(?:\\s|$)`));
}

async function choose(page, label, option) {
  const trigger = page.getByLabel(label, { exact: true });
  await expect(trigger).toBeEnabled();
  await trigger.click();
  await page.getByRole("option", { name: new RegExp(option, "i") }).click();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(trigger).toBeFocused();
}

async function closeDialog(page) {
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Close", exact: true })
    .first()
    .click();
}

test("Admin can open Migration Center in desktop-light and mobile-dark", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installMigrationFixtures(page, "admin");

  await page.goto("/inventory/migrations");
  await expect(page.getByRole("heading", { level: 1, name: "Migration Center" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New Migration" })).toBeEnabled();
  await expect(page.getByText("No active migrations", { exact: true })).toBeVisible();

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

for (const scenario of [
  {
    name: "identity binding failure",
    status: 403,
    error: "identity_binding_required",
    reason:
      "this sign-in could not be bound to a current identity; sign out, sign in again, then retry",
  },
  {
    name: "current role denial",
    status: 403,
    error: "current_role_denied",
    reason:
      "your current role does not allow Migration Center access; ask an administrator to assign the Admin or Partner role",
  },
  {
    name: "authority service outage",
    status: 503,
    error: "authority_unavailable",
    reason:
      "current access could not be verified; retry shortly or ask an administrator to check the identity service",
  },
]) {
  test(`Migration Center explains ${scenario.name} without exposing server diagnostics`, async ({ page }) => {
    const serverDiagnostic = "sensitive upstream identity diagnostic";
    await authenticate(page.context(), "partner");
    const api = await installMigrationFixtures(page, "partner", {
      authorityListError: {
        status: scenario.status,
        detail: {
          error: scenario.error,
          message: serverDiagnostic,
        },
      },
    });

    await page.goto("/inventory/migrations");

    await expect(page.getByRole("heading", { level: 1, name: "Migration Center" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Could not load migrations" })).toBeVisible();
    await expect(page.getByText(new RegExp(scenario.reason))).toBeVisible();
    await expect(page.getByText(serverDiagnostic, { exact: false })).toHaveCount(0);
    await expectCleanMigrationHarness(api);
  });
}

test("an initial migration queue failure leaves saved drafts usable", async ({ page }) => {
  const draft = makePlan({ plan_id: "plan-draft-while-jobs-unavailable" });
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    plans: [draft],
    failInitialJobList: true,
  });

  await page.goto("/inventory/migrations");
  await expect(page.getByRole("heading", { name: "Migration queue is unavailable" })).toBeVisible();
  await page.getByRole("tab", { name: "Drafts (1)" }).click();
  await page.getByRole("button", { name: "Resume migration draft for source-vm" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`plan=${draft.plan_id}`));

  await expectCleanMigrationHarness(api);
});

test("load older uses offset pagination and exposes the 101st job and draft", async ({ page }) => {
  const jobs = Array.from({ length: 101 }, (_, index) =>
    makeJob({ resourceName: `job-page-${String(index + 1).padStart(3, "0")}` }),
  );
  const template = makePlan();
  const plans = Array.from({ length: 101 }, (_, index) => {
    const suffix = String(index + 1).padStart(3, "0");
    const resourceName = `draft-page-${suffix}`;
    return makePlan({
      plan_id: `plan-page-${suffix}`,
      source: { ...template.source, resource_name: resourceName },
      target: { ...template.target, resource_name: resourceName },
    });
  });

  await authenticate(page.context(), "admin");
  const api = await installMigrationFixtures(page, "admin", { jobs, plans });
  await page.goto("/inventory/migrations");

  await expect(page.getByRole("button", { name: "job-page-101", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Load older jobs" }).click();
  await expect(page.getByRole("button", { name: "job-page-101", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Load older drafts" }).click();
  await page.getByRole("tab", { name: "Drafts (101)" }).click();
  await expect(
    page.getByRole("button", { name: "Resume migration draft for draft-page-101" }),
  ).toBeVisible();

  expect(
    api.requests.some(
      (request) => request.path === "/migrations" && request.search.includes("offset=100"),
    ),
  ).toBe(true);
  expect(
    api.requests.some(
      (request) => request.path === "/migrations/plans" && request.search.includes("offset=100"),
    ),
  ).toBe(true);
  await expectCleanMigrationHarness(api);
});

test("turned-off admission keeps existing migration recovery visible", async ({ page }, testInfo) => {
  const unresolved = makeJob({
    resourceName: "needs-recheck",
    phase: "manual_recovery",
    status: "manual_recovery",
    available_actions: ["recheck", "release"],
  });
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    centerEnabled: false,
    jobs: [unresolved],
  });

  await page.goto("/inventory/migrations");

  await expect(page.getByRole("heading", { name: "New migrations are turned off on this deployment" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New Migration" })).toBeDisabled();
  await expect(page.getByText("needs-recheck", { exact: true })).toBeVisible();
  expect(api.requests.some((request) => request.path === "/migrations")).toBe(true);

  await page.getByRole("button", { name: "needs-recheck", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Manual recovery required" })).toBeVisible();
  // Release is Admin only. The server offers it, the console hides it from a Partner.
  await expect(page.getByRole("dialog").getByRole("button", { name: "Release" })).toHaveCount(0);
  await page.getByRole("dialog").getByRole("button", { name: "Recheck" }).click();
  await expect(page.getByRole("heading", { name: "Recheck Migration" })).toBeVisible();
  await page.getByLabel("Reason").fill("Reconcile the uncertain daemon operation");
  await page.getByRole("button", { name: "Run recheck" }).click();
  await expect(page.getByRole("heading", { name: "Recheck Migration" })).toHaveCount(0);

  const recheck = api.mutations.find(
    (request) => request.method === "POST" && request.path === "/migrations/job-needs-recheck/recheck",
  );
  expect(recheck?.body).toEqual({
    expected_revision: 1,
    reason: "Reconcile the uncertain daemon operation",
  });

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("Partner creates a durable draft and resumes it after reload", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", { createdPlanId: "plan-resume" });

  await page.goto("/inventory/migrations");
  await page.getByRole("button", { name: "New Migration" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the source" })).toBeVisible();
  if (testInfo.project.name.startsWith("mobile")) {
    await expect(page.getByText("Step 1 of 7: Choose the source", { exact: true })).toBeVisible();
  }

  await choose(page, "Source cluster", "Cluster A");
  await choose(page, "Source project", "default");
  await choose(page, "Instance name", "source-vm");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await choose(page, "Destination cluster", "Cluster B");
  await choose(page, "Destination project", "target-project");
  await expect(page.getByLabel("Destination member (optional)")).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page).toHaveURL(/\bplan=plan-resume\b/);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review the strategy" })).toBeVisible();
  await expect(page).toHaveURL(/\bstep=strategy\b/);
  expect(api.createdBodies).toHaveLength(1);
  expect(api.createdBodies[0]).toMatchObject({
    scope: "instance",
    intent: "move",
    source: { cluster_id: "cluster-a", project: "default", resource_name: "source-vm", member: "member-a" },
    target: { cluster_id: "cluster-b", project: "target-project", resource_name: "source-vm" },
  });
  const createRequest = api.mutations.find(
    (request) => request.method === "POST" && request.path === "/migrations/plans",
  );
  expect(createRequest.headers).not.toHaveProperty("idempotency-key");

  // A refresh keeps the step in view; a plan link that names no step opens at Preflight.
  await page.reload();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review the strategy" })).toBeVisible();
  await page.goto("/inventory/migrations?plan=plan-resume");
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();
  await expect(page.getByText("Plan revision 1", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Preflight Passed" })).toBeVisible();
  await expect(page).toHaveURL(/\bstep=preflight\b/);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("Back closes a wizard opened from the Migration Center, and Close leaves no extra entry", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner");
  const dialog = page.getByRole("dialog");

  await page.goto("/inventory/migrations");
  await page.getByRole("button", { name: "New Migration" }).click();
  await expect(dialog.getByRole("heading", { name: "Choose the source" })).toBeVisible();
  await page.goBack();
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(/\/inventory\/migrations$/);
  await expect(page.getByRole("heading", { level: 1, name: "Migration Center" })).toBeVisible();

  // Close steps back over the entry the wizard opened, so the next Back leaves the page at once.
  await page.getByRole("button", { name: "New Migration" }).click();
  await expect(dialog.getByRole("heading", { name: "Choose the source" })).toBeVisible();
  await closeDialog(page);
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(/\/inventory\/migrations$/);
  await page.goBack();
  await expect(page).not.toHaveURL(/\/inventory\/migrations/);
  await expectCleanMigrationHarness(api);
});

test("editing a resumed source detaches to a new plan without reusing the stale instance", async ({ page }) => {
  const resumed = makePlan({ plan_id: "plan-edit-source" });
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    plans: [resumed],
    createdPlanId: "plan-detached-source",
  });

  await page.goto(`/inventory/migrations?plan=${resumed.plan_id}`);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();

  for (const expectedHeading of [
    "Review mappings",
    "Review the strategy",
    "Choose the destination",
    "Choose the source",
  ]) {
    await page.getByRole("dialog").getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: expectedHeading })).toBeVisible();
  }

  await choose(page, "Source cluster", "Cluster B");
  await expect(page).not.toHaveURL(new RegExp(`plan=${resumed.plan_id}`));
  await expect(page).toHaveURL(/\bnew=1\b/);
  await expect(page.getByLabel("Instance name", { exact: true })).toHaveText("Choose an instance");
  await expect(page.getByRole("dialog").getByRole("button", { name: "Continue" })).toBeDisabled();

  await choose(page, "Source cluster", "Cluster A");
  await choose(page, "Source project", "default");
  await choose(page, "Instance name", "source-vm");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page).toHaveURL(/\bplan=plan-detached-source\b/);
  expect(api.createdBodies).toHaveLength(1);
  expect(api.patchBodies).toEqual([]);
  expect(api.createdBodies[0].source).toMatchObject({
    cluster_id: "cluster-a",
    project: "default",
    resource_name: "source-vm",
    member: "member-a",
  });
  await expectCleanMigrationHarness(api);
});

test("Tenant is refused before Migration Center data is requested", async ({ page }, testInfo) => {
  await authenticate(page.context(), "tenant");
  const api = await installMigrationFixtures(page, "tenant");

  await page.goto("/inventory/migrations");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { level: 1, name: "Migration Center" })).toHaveCount(0);
  expect(api.requests.filter((request) => request.path.startsWith("/migrations"))).toEqual([]);

  await expectProjectTheme(page, testInfo);
  await expectCleanMigrationHarness(api);
});

test("a network blocker is resolved by mapping, and a stale revision reloads instead of failing", async ({ page }, testInfo) => {
  const blocked = makePlan({}, "move_network_mapping");
  const stale = makePlan({ plan_id: "plan-stale" });
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    plans: [blocked, stale],
    patchVariants: { [blocked.plan_id]: "move_ready" },
    patchConflictPlanIds: [stale.plan_id],
  });

  await page.goto(`/inventory/migrations?plan=${blocked.plan_id}`);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();
  await expect(
    page.getByText(
      "NIC 'eth0' uses 'lxdbr0', which the target project cannot use. Available networks: lxdbr1.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Resolution: Map the network to one of the available target networks.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Continue" })).toBeDisabled();

  await page.getByRole("dialog").getByRole("button", { name: "Back", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review mappings" })).toBeVisible();
  await choose(page, "Target network for eth0 on source-vm", "lxdbr1");
  await expect(page.getByText("Your mapping, applied on Continue", { exact: true })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Preflight Passed" })).toBeVisible();
  await expect(page.getByText("Plan revision 2", { exact: true })).toBeVisible();
  expect(api.patchBodies.at(-1)).toMatchObject({
    planId: blocked.plan_id,
    body: { mappings: { networks: { lxdbr0: "lxdbr1" } } },
  });

  await page.goto(`/inventory/migrations?plan=${stale.plan_id}`);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Recheck" }).click();
  await expect(page.getByRole("heading", { name: "The plan was updated" })).toBeVisible();
  await expect(page.getByText(/Preflight changed since you reviewed it and is now revision 1/)).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "The plan changed" })).toHaveCount(0);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("expired preflight and a turned-off move keep the plan reviewable but not executable", async ({ page }, testInfo) => {
  const expired = makePlan({ plan_id: "plan-expired", status: "expired", expires_at: EXPIRED });
  const ready = makePlan({ plan_id: "plan-gated" });

  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    executionEnabled: false,
    plans: [expired, ready],
  });

  await page.goto(`/inventory/migrations?plan=${expired.plan_id}`);
  await expect(page.getByRole("heading", { name: "Preflight expired" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Continue" })).toBeDisabled();

  await page.goto(`/inventory/migrations?plan=${ready.plan_id}`);
  await expect(page.getByRole("heading", { name: "Preflight Passed" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "This migration cannot be started here" })).toBeVisible();
  await expect(
    page.getByText("Move Instance is turned off on this deployment. The plan can still be reviewed.", { exact: true }),
  ).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Set recovery policy" })).toBeVisible();
  await expect(page.getByLabel("Keep the stopped source for (hours)")).toHaveValue("24");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Approve execution" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Start move" })).toBeDisabled();
  expect(api.executions).toEqual([]);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("copy of a running source explains the stop and a failed draft read can retry in place", async ({ page }, testInfo) => {
  const copy = makePlan({}, "copy_running");
  const retry = makePlan({ plan_id: "plan-retry" });

  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    plans: [copy, retry],
    failPlanReadOnceIds: [retry.plan_id],
  });

  await page.goto(`/inventory/migrations?plan=${retry.plan_id}`);
  await expect(page.getByRole("heading", { name: "Draft could not be restored" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`plan=${retry.plan_id}`));
  await page.getByRole("button", { name: "Retry draft" }).click();
  await expect(page.getByRole("heading", { name: "Review preflight" })).toBeVisible();

  await page.goto(`/inventory/migrations?plan=${copy.plan_id}`);
  await expect(page.getByRole("heading", { name: "The source is running and will be stopped" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the copy outcome" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "The source is not changed" })).toBeVisible();
  await expect(page.getByLabel("Keep the stopped source for (hours)")).toHaveCount(0);
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Approve execution" })).toBeVisible();
  // A running copy needs the downtime acknowledgement on top of the copy and billing ones.
  for (const label of [
    "I reviewed the transfer impact. The copy is a new, independent instance.",
    "I reviewed snapshot schedule and billing impact for every source workload.",
    "I reviewed the downtime. A running source is stopped for the transfer.",
  ]) {
    await expect(page.getByLabel(label)).toBeVisible();
  }
  await expect(page.getByRole("dialog").getByRole("button", { name: "Start copy" })).toBeDisabled();

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("a dir-pool source the target cannot bound waits for the operator to accept its size", async ({ page }) => {
  // Owner decision 2026-10-29: Pandora's dir pool holds far more than any target has free, so a copy
  // there is not refused; Start waits for an explicit acceptance of the unmeasured size.
  const plan = makePlan(
    {
      plan_id: "plan-unmeasured",
      warnings: [
        {
          code: "source_size_unmeasured",
          severity: "warning",
          message:
            "The size of web cannot be measured on its dir pool, and target pool 'fast' has less free space than that pool holds in total. The transfer may fill the target pool.",
          remediation:
            "Check the target pool's free space, then confirm the unmeasured size at approval, or choose a pool with more room.",
        },
      ],
    },
    "copy_running",
  );
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", { plans: [plan] });
  const dialog = page.getByRole("dialog");

  await page.goto(`/inventory/migrations?plan=${plan.plan_id}`);
  await expect(dialog.getByText(/cannot be measured on its dir pool/).first()).toBeVisible();
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the copy outcome" })).toBeVisible();
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Approve execution" })).toBeVisible();

  await page.getByLabel("Reason for this migration").fill("Copy within Pandora's pool");
  for (const label of [
    "I reviewed the transfer impact. The copy is a new, independent instance.",
    "I reviewed snapshot schedule and billing impact for every source workload.",
    "I reviewed the downtime. A running source is stopped for the transfer.",
  ]) {
    await page.getByLabel(label).check();
  }
  const start = dialog.getByRole("button", { name: "Start copy" });
  await expect(start).toBeDisabled();
  await page
    .getByLabel("I accept that this transfer's size cannot be measured on a dir pool and may fill the target pool.")
    .check();
  await expect(start).toBeEnabled();
  await expectCleanMigrationHarness(api);
});

async function openNewMigration(page) {
  await page.goto("/inventory/migrations");
  const newMigration = page.getByRole("button", { name: "New Migration" });
  await expect(newMigration).toBeEnabled();
  await newMigration.click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the source" })).toBeVisible();
}

test("a failed settings poll does not take away a good write verdict", async ({ page }) => {
  // The owner saw "Execution is unavailable" while the deployment was fine: the first settings
  // read answered, a later poll failed, and the wizard read that one failure as the verdict.
  test.setTimeout(120_000);
  await page.clock.install();
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    settingsGoodReads: 1,
    settingsFailures: 1000,
  });
  const dialog = page.getByRole("dialog");
  let flashed = false;
  await page.exposeFunction("reportUnavailableNotice", () => {
    flashed = true;
  });

  await openNewMigration(page);
  await page.evaluate(() => {
    // A MutationObserver catches a notice that appears and leaves between two assertions.
    const observer = new MutationObserver(() => {
      if (document.body.innerText.includes("Execution is unavailable")) {
        window.reportUnavailableNotice();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  const readsBefore = api.settingsReads;
  // Two minutes: the minute's poll and all of its retries fail, while the verdict is under five.
  await page.clock.runFor("02:00");
  expect(api.settingsReads, "the poll and its retries ran").toBeGreaterThan(readsBefore + 2);
  expect(flashed, "the unavailable notice appeared for a failed poll").toBe(false);
  await expect(dialog.getByText("Execution is unavailable", { exact: true })).toHaveCount(0);
  expect(api.mutations).toEqual([]);
});

test("a verdict that went stale says why, offers Check Again, and recovers", async ({ page }) => {
  test.setTimeout(120_000);
  await page.clock.install();
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    settingsGoodReads: 1,
    settingsFailures: 1000,
  });
  const dialog = page.getByRole("dialog");

  await openNewMigration(page);
  // Six minutes of failed polls: the last good verdict is older than five and stops answering.
  await page.clock.runFor("06:00");
  await expect(dialog.getByText("Execution is unavailable", { exact: true })).toBeVisible();
  await expect(dialog.getByText(/The server did not answer the settings check\./)).toBeVisible();

  api.settingsFailuresLeft = 0;
  await dialog.getByRole("button", { name: "Check Again", exact: true }).click();
  await expect(dialog.getByText("Execution is unavailable", { exact: true })).toHaveCount(0);
  expect(api.mutations).toEqual([]);
});

test("read-only mode still opens the wizard from the row menu and the center, and refuses only Start", async ({ page }) => {
  // Planning is not a write. The server refuses execute in read-only mode, so the entry points
  // answer to permission and rollout only, and the wizard says why Start is unavailable.
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", { readOnly: true, instanceListPage: true });
  const dialog = page.getByRole("dialog");

  await page.goto("/inventory/instances");
  await page.getByRole("button", { name: "Actions for instance source-vm in default on cluster-a" }).click();
  const move = page.getByRole("menuitem", { name: /^Move Instance/ });
  await expect(move).toBeEnabled();
  await move.click();
  await expect(page).toHaveURL(/\/inventory\/migrations\?.*\bnew=1\b/);
  await expect(dialog.getByRole("heading", { name: "Choose the source" })).toBeVisible();
  await expect(page.getByLabel("Instance name", { exact: true })).toContainText("source-vm");
  await expect(dialog.getByText("Execution is unavailable", { exact: true })).toBeVisible();
  await expect(dialog.getByText(/Disabled in read-only mode\. A saved draft can still be reviewed/)).toBeVisible();
  await closeDialog(page);

  await page.goto("/inventory/migrations");
  const newMigration = page.getByRole("button", { name: "New Migration" });
  await expect(newMigration).toBeEnabled();
  await newMigration.click();
  await expect(dialog.getByRole("heading", { name: "Choose the source" })).toBeVisible();
  expect(api.mutations).toEqual([]);
  await expectCleanMigrationHarness(api);
});

test("the source cluster picker does not wait for the project inventory", async ({ page }) => {
  // The all-projects read fans out to every cluster. A slow one must hold only the project
  // pickers, never the cluster picker in front of them.
  await authenticate(page.context(), "partner");
  let releaseProjects;
  const projectsGate = new Promise((resolve) => {
    releaseProjects = resolve;
  });
  const api = await installMigrationFixtures(page, "partner", { projectsGate });

  await page.goto("/inventory/migrations?new=1");
  await choose(page, "Source cluster", "Cluster A");
  const project = page.getByLabel("Source project", { exact: true });
  await expect(project).toBeDisabled();
  await expect(project).toContainText("Loading projects");

  releaseProjects();
  await choose(page, "Source project", "default");
  await expectCleanMigrationHarness(api);
});

test("copy can target a specific destination member under a suggested new name", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", { createdPlanId: "plan-copy-member" });

  await page.goto("/inventory/migrations?new=1");
  await choose(page, "Intent", "Copy");
  await choose(page, "Source cluster", "Cluster A");
  await choose(page, "Source project", "default");
  await choose(page, "Instance name", "source-vm");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await choose(page, "Destination cluster", "Cluster B");
  await choose(page, "Destination project", "target-project");
  await expect(page.getByLabel("Destination instance name")).toHaveValue("source-vm-copy");
  await choose(page, "Destination member (optional)", "member-b2");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page).toHaveURL(/\bplan=plan-copy-member\b/);
  expect(api.createdBodies[0]).toMatchObject({
    intent: "copy",
    target: {
      cluster_id: "cluster-b",
      project: "target-project",
      resource_name: "source-vm-copy",
      member: "member-b2",
    },
  });
  await expectCleanMigrationHarness(api);
});

test("project relocation actions explain why they are unavailable", async ({ page }) => {
  let releaseSettings;
  const settingsGate = new Promise((resolve) => {
    releaseSettings = resolve;
  });

  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", { settingsGate, projectRelocationEnabled: false });

  await page.goto("/inventory");
  const projectCard = page.locator('[data-slot="project-card"]').filter({ hasText: "default" });
  await expect(projectCard).toBeVisible();
  await projectCard.getByRole("button", { name: "Project actions for default" }).click();
  const workloads = page.getByRole("menuitem", { name: /Relocate Workloads/ });
  const project = page.getByRole("menuitem", { name: /Relocate Project/ });
  await expect(workloads).toHaveAttribute("aria-disabled", "true");
  await expect(workloads).toContainText("Checking migration availability");

  releaseSettings();
  await expect(workloads).toContainText("Relocate Workloads is turned off on this deployment");
  await expect(project).toContainText("Relocate Project is turned off on this deployment");
  await expect(project).toHaveAttribute("aria-disabled", "true");

  // The item stays focusable so its reason is reachable, but selecting it does nothing.
  await workloads.focus();
  await page.keyboard.press("Enter");
  await expect(workloads).toBeVisible();
  await expect(page).toHaveURL(/\/inventory$/);
  expect(api.requests.filter((request) => request.path.startsWith("/migrations"))).toEqual([]);
  await expectCleanMigrationHarness(api);
});

test("an enabled project relocation opens the wizard at the project source", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", { projectRelocationEnabled: true });

  await page.goto("/inventory");
  const projectCard = page.locator('[data-slot="project-card"]').filter({ hasText: "default" });
  await projectCard.getByRole("button", { name: "Project actions for default" }).click();
  const workloads = page.getByRole("menuitem", { name: "Relocate Workloads", exact: true });
  await expect(workloads).not.toHaveAttribute("aria-disabled", "true");
  await workloads.click();

  await expect(page).toHaveURL(/\bmode=member\b/);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Choose the source" })).toBeVisible();
  await expect(dialog.getByLabel("Relocation", { exact: true })).toContainText("Move Workloads to Another Member");
  // A project is relocated, never copied.
  await dialog.getByLabel("Intent", { exact: true }).click();
  await expect(page.getByRole("option", { name: "Copy" })).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  // The member is the whole destination here, so Continue waits for it.
  await expect(dialog.getByRole("button", { name: "Continue" })).toBeDisabled();
  await choose(page, "Destination member", "member-a2");
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect.poll(() => api.createdBodies.length).toBe(1);
  expect(api.createdBodies[0]).toMatchObject({
    scope: "project",
    intent: "move",
    source: { cluster_id: "cluster-a", project: "default" },
    target: { cluster_id: "cluster-a", project: "default", member: "member-a2" },
  });
  await expectCleanMigrationHarness(api);
});

test("relocating a project to another cluster names a new project and refuses an existing one", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner");

  await page.goto("/inventory/migrations?new=1&scope=project&source_cluster=cluster-a&source_project=default&mode=cluster");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Relocation", { exact: true })).toContainText("Move Project to Another Cluster");
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  // The source cluster is offered but cannot be chosen: renaming covers that case.
  await dialog.getByLabel("Destination cluster", { exact: true }).click();
  await expect(page.getByRole("option", { name: /cluster-a/i })).toHaveAttribute("aria-disabled", "true");
  await page.getByRole("option", { name: /cluster-b/i }).click();
  const name = dialog.getByLabel("Project name on the destination", { exact: true });
  await expect(name).toHaveValue("default");
  await name.fill("target-project");
  await expect(dialog.getByText("already exists there", { exact: false })).toBeVisible();
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByRole("button", { name: "Continue" })).toBeDisabled();
  await name.fill("web-relocated");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("project-relocation-destination.png"), fullPage: true });
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect.poll(() => api.createdBodies.length).toBe(1);
  expect(api.createdBodies[0]).toMatchObject({
    scope: "project",
    source: { cluster_id: "cluster-a", project: "default" },
    target: { cluster_id: "cluster-b", project: "web-relocated" },
  });
  await expectCleanMigrationHarness(api);
});

test("renaming a project stays on its cluster and needs a different name", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner");

  await page.goto("/inventory/migrations?new=1&scope=project&source_cluster=cluster-a&source_project=default&mode=rename");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Relocation", { exact: true })).toContainText("Rename Project on This Cluster");
  await dialog.getByRole("button", { name: "Continue" }).click();
  const name = dialog.getByLabel("New project name", { exact: true });
  await name.fill("default");
  await expect(dialog.getByText("Choose a name other than the current one.")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Continue" })).toBeDisabled();
  await name.fill("default-v2");
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect.poll(() => api.createdBodies.length).toBe(1);
  expect(api.createdBodies[0].target).toMatchObject({ cluster_id: "cluster-a", project: "default-v2" });
  await expectCleanMigrationHarness(api);
});

test("execution retry reuses the idempotency key for the same reviewed payload", async ({ page }) => {
  const ready = makePlan({ plan_id: "plan-idempotent" });
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    plans: [ready],
    failExecuteOnce: true,
  });

  await page.goto(`/inventory/migrations?plan=${ready.plan_id}`);
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Set recovery policy" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Approve execution" })).toBeVisible();

  const acknowledgements = [
    "I reviewed the downtime. A running source is stopped for the transfer.",
    "I understand the source is kept stopped for the retention window and then deleted.",
    "I reviewed snapshot schedule and billing impact for every source workload.",
  ];
  await page.getByLabel("Reason for this migration").fill("Approved change CHG-1024");
  for (const label of acknowledgements) await page.getByLabel(label).check();

  await page.getByRole("dialog").getByRole("button", { name: "Start move" }).click();
  await expect(page.getByText("Fixture lost the execute response after acceptance.", { exact: true })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Start move" }).click();

  await expect(page).toHaveURL(/\bjob=job-executed\b/);
  expect(api.executions).toHaveLength(2);
  expect(api.executions[0].headers["idempotency-key"]).toBeTruthy();
  expect(api.executions[1].headers["idempotency-key"]).toBe(
    api.executions[0].headers["idempotency-key"],
  );
  expect([...api.executions[0].body.acknowledgements].sort()).toEqual([
    "billing-impact-reviewed",
    "downtime-reviewed",
    "source-retention-understood",
  ]);
  await expectCleanMigrationHarness(api);
});

test("job detail shows retention, offers Finalize and Roll Back, and keeps stale state", async ({ page }, testInfo) => {
  const jobs = [
    makeJob({ resourceName: "unknown-vm", phase: "reconciling", status: "unknown", available_actions: ["recheck"] }),
    makeJob({ resourceName: "manual-vm", phase: "manual_recovery", status: "manual_recovery", available_actions: ["recheck", "release"] }),
    makeJob({ resourceName: "retained-vm", phase: "source_retained", status: "waiting", available_actions: ["rollback", "cleanup"] }),
    makeJob({ resourceName: "due-vm", phase: "cleanup_due", status: "waiting", available_actions: ["rollback", "cleanup"] }),
    makeJob({ resourceName: "blocked-vm", phase: "cleanup_blocked", status: "waiting", available_actions: ["rollback", "cleanup", "release"] }),
    makeJob({ resourceName: "abort-vm", scope: "project", phase: "cutover_pending", status: "waiting", available_actions: ["cancel", "cutover"] }),
  ];

  await authenticate(page.context(), "admin");
  const api = await installMigrationFixtures(page, "admin", { jobs, failJobListAfterFirst: true });
  await page.goto("/inventory/migrations");

  await page.getByRole("button", { name: "Refresh migrations" }).click();
  await expect(page.getByRole("heading", { name: "Migration updates are reconnecting" })).toBeVisible();
  await expect(page.getByRole("button", { name: "unknown-vm", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "unknown-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Operation outcome is being reconciled" })).toBeVisible();
  await closeDialog(page);

  await page.getByRole("button", { name: "manual-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Manual recovery required" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Release" })).toBeVisible();
  await closeDialog(page);

  await page.getByRole("button", { name: "due-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Retention ended" })).toBeVisible();
  await closeDialog(page);

  await page.getByRole("button", { name: "blocked-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Cleanup or rollback is blocked" })).toBeVisible();
  await closeDialog(page);

  await page.getByRole("button", { name: "retained-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Source Retention" })).toBeVisible();
  // The transfer method in operator words, never the raw enum.
  await expect(page.getByRole("dialog").getByText("Transfer between clusters", { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Roll Back" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Finalize" }).click();
  await expect(page.getByRole("heading", { name: "Finalize This Move" })).toBeVisible();
  const submit = page.getByRole("button", { name: "Finalize and delete source" });
  await page.getByLabel("Reason").fill("Target verified by the application owner");
  await expect(submit).toBeDisabled();
  await page.getByLabel(/to confirm/).fill("cleanup");
  await expect(submit).toBeDisabled();
  await page.getByLabel(/to confirm/).fill("CLEANUP");
  await submit.click();
  await expect(page.getByRole("heading", { name: "Finalize This Move" })).toHaveCount(0);
  const finalize = api.mutations.find(
    (request) => request.method === "POST" && request.path === "/migrations/job-retained-vm/cleanup",
  );
  expect(finalize?.body).toEqual({
    expected_revision: 1,
    reason: "Target verified by the application owner",
    confirmation: "CLEANUP",
  });
  expect(api.mutations.filter((request) => request.path.endsWith("/rollback"))).toEqual([]);
  await closeDialog(page);

  await page.getByRole("button", { name: "abort-vm", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Abort before cutover" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Abort before cutover" }).click();
  await expect(page.getByRole("heading", { name: "Abort Before Cutover" })).toBeVisible();

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});
