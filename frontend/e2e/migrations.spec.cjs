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

test("paused admission keeps existing migration recovery visible", async ({ page }, testInfo) => {
  const unresolved = makeJob({
    resourceName: "needs-recheck",
    phase: "manual_recovery",
    status: "manual_recovery",
    available_actions: ["recheck"],
  });
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    centerEnabled: false,
    jobs: [unresolved],
  });

  await page.goto("/inventory/migrations");

  await expect(page.getByRole("heading", { name: "New migration admission is paused" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New Migration" })).toBeDisabled();
  await expect(page.getByText("needs-recheck", { exact: true })).toBeVisible();
  expect(api.requests.some((request) => request.path === "/migrations")).toBe(true);

  await page.getByRole("button", { name: "needs-recheck", exact: true }).click();
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

  await choose(page, "Source cluster", "Cluster A");
  await choose(page, "Source project", "default");
  await choose(page, "Instance name", "source-vm");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await choose(page, "Destination cluster", "Cluster B");
  await expect(page.getByLabel("Destination member (optional)")).toBeVisible();
  await page.getByLabel("Destination project", { exact: true }).fill("target-project");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page).toHaveURL(/\bplan=plan-resume\b/);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Review the strategy" })).toBeVisible();
  expect(api.createdBodies).toHaveLength(1);
  expect(api.createdBodies[0]).toMatchObject({
    scope: "instance",
    intent: "move",
    source: { cluster_id: "cluster-a", project: "default", resource_name: "source-vm" },
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
    "Resolve mappings",
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

test("preflight shows blockers, expiry, revision conflict, and rollout execution gate", async ({ page }, testInfo) => {
  const blocked = makePlan({
    plan_id: "plan-blocked",
    status: "blocked",
    blockers: [{
      code: "TARGET_NOT_EMPTY",
      severity: "blocker",
      message: "Target project is not empty.",
      remediation: "Choose a new or empty target project.",
      resource_key: null,
    }],
  });
  const expired = makePlan({ plan_id: "plan-expired", status: "expired", expires_at: EXPIRED });
  const ready = makePlan({ plan_id: "plan-gated" });

  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner", {
    executionEnabled: false,
    plans: [blocked, expired, ready],
    patchConflictPlanIds: [blocked.plan_id],
  });

  await page.goto(`/inventory/migrations?plan=${blocked.plan_id}`);
  await expect(page.getByText("Target project is not empty.", { exact: true })).toBeVisible();
  await expect(page.getByText("Resolution: Choose a new or empty target project.", { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Continue" })).toBeDisabled();
  await page.getByRole("dialog").getByRole("button", { name: "Recheck" }).click();
  await expect(page.getByText("Plan revision changed; reload and review again.", { exact: true })).toBeVisible();

  await page.goto(`/inventory/migrations?plan=${expired.plan_id}`);
  await expect(page.getByRole("heading", { name: "Preflight expired" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Continue" })).toBeDisabled();

  await page.goto(`/inventory/migrations?plan=${ready.plan_id}`);
  await expect(page.getByRole("heading", { name: "Preflight Passed" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "This execution path is not enabled" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Set recovery policy" })).toBeVisible();
  await expect(page.getByLabel("Require external traffic confirmation")).toBeDisabled();
  await expect(page.getByText(/Signed cutover webhooks are not enabled/)).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Approve execution" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Start migration" })).toBeDisabled();
  expect(api.executions).toEqual([]);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("copy recovery stays independent and a failed draft read can retry in place", async ({ page }, testInfo) => {
  const copy = makePlan({
    plan_id: "plan-copy",
    intent: "copy",
    checkpoint_requested: false,
    rollback_contract: { summary: "Copy has no rollback window." },
  });
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
  await expect(page.getByText("Create a pre-cutover checkpoint", { exact: true })).toHaveCount(0);
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the copy outcome" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "The source remains active" })).toBeVisible();
  await expect(page.getByLabel("Source retention in hours")).toHaveCount(0);
  await expect(page.getByLabel("Require external traffic confirmation")).toHaveCount(0);

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});

test("durable copy does not offer destination member placement", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installMigrationFixtures(page, "partner");

  await page.goto("/inventory/migrations?new=1");
  await choose(page, "Intent", "Durable copy");
  await choose(page, "Source cluster", "Cluster A");
  await choose(page, "Source project", "default");
  await choose(page, "Instance name", "source-vm");
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();

  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await choose(page, "Destination cluster", "Cluster B");
  await expect(page.getByLabel("Destination member (optional)")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Copy placement is automatic" })).toBeVisible();
  await expect(
    page.getByText(
      "Durable copy cannot target a specific destination member in this release. The destination cluster selects placement.",
      { exact: true },
    ),
  ).toBeVisible();
  await expectCleanMigrationHarness(api);
});

test("project migration actions stay hidden until capability admission is known", async ({ page }) => {
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
  await expect(page.getByRole("menuitem", { name: "Relocate Workloads" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "Relocate Project" })).toHaveCount(0);

  releaseSettings();
  await expect(page.getByRole("menuitem", { name: "Relocate Workloads" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Relocate Project" })).toBeVisible();

  await page.getByRole("menuitem", { name: "Relocate Workloads" }).click();
  await expect(page).toHaveURL(/\bmode=member\b/);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the source" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Choose the destination" })).toBeVisible();
  await choose(page, "Destination member (optional)", "member-b");
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

  await page.getByLabel("Reason for this migration").fill("Approved change CHG-1024");
  await page.getByLabel("I reviewed the downtime class and cutover sequence.").check();
  await page.getByLabel("I understand the retained source stays stopped and fenced.").check();
  await page.getByLabel("I reviewed snapshot schedule and billing impact for every source workload.").check();

  await page.getByRole("dialog").getByRole("button", { name: "Start migration" }).click();
  await expect(page.getByText("Fixture lost the execute response after acceptance.", { exact: true })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Start migration" }).click();

  await expect(page).toHaveURL(/\bjob=job-executed\b/);
  expect(api.executions).toHaveLength(2);
  expect(api.executions[0].headers["idempotency-key"]).toBeTruthy();
  expect(api.executions[1].headers["idempotency-key"]).toBe(
    api.executions[0].headers["idempotency-key"],
  );
  await expectCleanMigrationHarness(api);
});

test("job detail preserves stale state and filters release-disabled destructive actions", async ({ page }, testInfo) => {
  const jobs = [
    makeJob({ resourceName: "unknown-vm", phase: "reconciling", status: "unknown", available_actions: ["recheck"] }),
    makeJob({ resourceName: "manual-vm", phase: "manual_recovery", status: "manual_recovery", available_actions: ["recheck"] }),
    makeJob({ resourceName: "cleanup-vm", phase: "cleanup_due", status: "waiting", available_actions: ["cleanup"] }),
    makeJob({ resourceName: "rollback-vm", phase: "rollback_available", status: "waiting", available_actions: ["rollback", "restore_checkpoint"] }),
    makeJob({ resourceName: "abort-vm", phase: "cutover_pending", status: "waiting", available_actions: ["cancel", "cutover"] }),
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
  await closeDialog(page);

  await page.getByRole("button", { name: "cleanup-vm", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Source cleanup is due" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Delete Source" })).toHaveCount(0);
  await closeDialog(page);

  await page.getByRole("button", { name: "rollback-vm", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Rollback" })).toHaveCount(0);
  await expect(page.getByRole("dialog").getByRole("button", { name: "Restore Checkpoint" })).toHaveCount(0);
  await closeDialog(page);

  expect(
    api.mutations.filter((request) =>
      ["/rollback", "/restore-checkpoint", "/cleanup"].some((suffix) => request.path.endsWith(suffix)),
    ),
  ).toEqual([]);

  await page.getByRole("button", { name: "abort-vm", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Abort before cutover" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Abort before cutover" }).click();
  await expect(page.getByRole("heading", { name: "Abort Before Cutover" })).toBeVisible();
  await expect(page.getByText(/cutover approval is requested before target staging/)).toBeVisible();

  await expectProjectTheme(page, testInfo);
  await expectNoHorizontalOverflow(page);
  await expectCleanMigrationHarness(api);
});
