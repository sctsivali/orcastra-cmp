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

  await page.reload();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review preflight" })).toBeVisible();
  await expect(page.getByText("Plan revision 1", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Preflight Passed" })).toBeVisible();

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
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
  const api = await installMigrationFixtures(page, "partner", { settingsGate });

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
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the source" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await choose(page, "Destination member (optional)", "member-a2");
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
  await expect(page.getByRole("heading", { name: "Source cleanup is blocked" })).toBeVisible();
  await closeDialog(page);

  await page.getByRole("button", { name: "retained-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Source Retention" })).toBeVisible();
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
