const { expect } = require("@playwright/test");
const { expectCleanHarness, installApiFixtures } = require("./fixtures.cjs");

const NOW = "2026-09-22T12:00:00Z";
const FUTURE = "2099-09-22T12:00:00Z";
const EXPIRED = "2020-09-22T12:00:00Z";

function endpoint(clusterId, project, resourceName) {
  return {
    cluster_id: clusterId,
    project,
    resource_name: resourceName ?? null,
    member: null,
    cluster_fingerprint: "a".repeat(64),
  };
}

function mappings() {
  return {
    storage_pools: { root: "default" },
    networks: {},
    profiles: {},
    volumes: {},
    members: {},
    external_dependencies: {},
  };
}

function makePlan(overrides = {}) {
  return {
    plan_id: "plan-ready",
    revision: 1,
    scope: "instance",
    intent: "move",
    status: "ready",
    source: endpoint("cluster-a", "default", "source-vm"),
    target: endpoint("cluster-b", "target-project", "source-vm"),
    strategy: "cold",
    downtime_class: "cold",
    retention_hours: 24,
    checkpoint_requested: false,
    external_cutover_required: false,
    capabilities: [
      { code: "cold_cutover", supported: true, reason: "Cold cutover is available." },
      { code: "mapping_storage_root", supported: true, reason: "Root storage mapping is supported." },
      { code: "mapping_networks", supported: false, reason: "Network mappings are not applied." },
      { code: "mapping_profiles", supported: false, reason: "Profile mappings are not applied." },
      { code: "mapping_volumes", supported: false, reason: "Volume mappings are not applied." },
      { code: "mapping_members", supported: false, reason: "Member mappings are not applied." },
      { code: "mapping_external_dependencies", supported: false, reason: "External mappings are not applied." },
      { code: "external_cutover_webhook", supported: false, reason: "Signed cutover webhooks are not enabled." },
    ],
    resources: [{
      resource_key: "instance:source-vm",
      resource_type: "instance",
      source: { name: "source-vm", cluster_id: "cluster-a", project: "default" },
      target: { name: "source-vm", cluster_id: "cluster-b", project: "target-project" },
      dependency_keys: [],
    }],
    mappings: mappings(),
    blockers: [],
    warnings: [],
    rollback_contract: { summary: "Eligibility is checked before rollback." },
    expires_at: FUTURE,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeJob(overrides = {}) {
  const resourceName = overrides.resourceName ?? "source-vm";
  const phase = overrides.phase ?? "queued";
  const status = overrides.status ?? "pending";
  const availableActions = overrides.available_actions ?? [];
  const cleaned = { ...overrides };
  delete cleaned.resourceName;
  return {
    job_id: `job-${resourceName}`,
    plan_id: "plan-ready",
    plan_revision: 1,
    revision: 1,
    scope: "instance",
    intent: "move",
    source: endpoint("cluster-a", "default", resourceName),
    target: endpoint("cluster-b", "target-project", resourceName),
    strategy: "cold",
    downtime_class: "cold",
    phase,
    status,
    retention_hours: 24,
    retention_deadline: FUTURE,
    external_cutover_required: false,
    external_cutover_confirmed_at: null,
    resources: [],
    timeline: [{ sequence: 1, phase, message: `Fixture entered ${phase}.`, created_at: NOW }],
    available_actions: availableActions,
    safe_result: null,
    error_code: null,
    error_message: null,
    created_at: NOW,
    updated_at: NOW,
    started_at: NOW,
    finished_at: null,
    ...cleaned,
  };
}

function json(route, body, status = 200, headers = {}) {
  return route.fulfill({
    status,
    headers,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function list(items, limit = 100, offset = 0) {
  const pageItems = items.slice(offset, offset + limit);
  return {
    items: pageItems,
    total: items.length,
    limit,
    offset,
    has_more: offset + pageItems.length < items.length,
  };
}

function pagination(url) {
  const requestedLimit = Number.parseInt(url.searchParams.get("limit") ?? "100", 10);
  const requestedOffset = Number.parseInt(url.searchParams.get("offset") ?? "0", 10);
  return {
    limit: Number.isFinite(requestedLimit) && requestedLimit > 0 ? requestedLimit : 100,
    offset: Number.isFinite(requestedOffset) && requestedOffset >= 0 ? requestedOffset : 0,
  };
}

function cluster(clusterId) {
  return {
    cluster_id: clusterId,
    display_name: clusterId === "cluster-a" ? "Cluster A" : "Cluster B",
    endpoint: `https://${clusterId}.invalid:8443`,
    cluster_type: "lxd",
    clustered: true,
    status: "online",
    has_certificate: true,
    labels: {},
    error_message: null,
  };
}

async function installMigrationFixtures(page, role, options = {}) {
  const base = await installApiFixtures(page, role);
  const plans = new Map((options.plans ?? []).map((plan) => [plan.plan_id, plan]));
  const jobs = new Map((options.jobs ?? []).map((job) => [job.job_id, job]));
  const state = {
    base,
    requests: [],
    mutations: [],
    authFailures: [],
    unexpected: [],
    plans,
    jobs,
    jobListReads: 0,
    planReads: new Map(),
    createdBodies: [],
    patchBodies: [],
    executions: [],
    executeAttempts: 0,
  };
  const expectedBearer = `Bearer e2e-${role}-access-token`;

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "") || "/";
    const method = request.method().toUpperCase();
    const entry = {
      method,
      path,
      search: url.search,
      authorization: request.headers().authorization ?? null,
    };
    const handled = path === "/system/settings" ||
      path === "/clusters" ||
      path === "/projects" ||
      path === "/instances" ||
      path === "/cluster-members" ||
      path === "/storage/pools" ||
      path === "/migrations" ||
      path.startsWith("/migrations/");

    if (!handled) {
      await route.fallback();
      return;
    }

    state.requests.push(entry);
    if (entry.authorization !== expectedBearer) state.authFailures.push(entry);
    if (method !== "GET") {
      let body = null;
      try {
        body = request.postDataJSON();
      } catch {
        body = request.postData();
      }
      state.mutations.push({ ...entry, body, headers: request.headers() });
    }

    if (method === "GET" && path === "/system/settings") {
      if (options.settingsGate) await options.settingsGate;
      await json(route, {
        read_only_mode: options.readOnly === true,
        api_version: "e2e",
        debug: false,
        snapshot_schedules_enabled: true,
        task_journal_enabled: false,
        migration_center_enabled: options.centerEnabled !== false,
        migration_durable_copy_enabled: options.executionEnabled !== false,
        migration_instance_move_enabled: options.executionEnabled !== false,
        migration_project_member_relocation_enabled: options.executionEnabled !== false,
        migration_project_cross_cluster_enabled: options.executionEnabled !== false,
      });
      return;
    }
    if (method === "GET" && path === "/clusters") {
      await json(route, { clusters: [cluster("cluster-a"), cluster("cluster-b")], total: 2 });
      return;
    }
    if (method === "GET" && path === "/projects") {
      await json(route, [
        { host: "cluster-a", projects: [{ name: "default", description: "", config: {}, used_by: [], host: "cluster-a", tags: [] }], total: 1, reachable: true, error: null },
        { host: "cluster-b", projects: [], total: 0, reachable: true, error: null },
      ]);
      return;
    }
    if (method === "GET" && path === "/instances") {
      await json(route, {
        total: 1,
        vms: [{
          name: "source-vm",
          host: "cluster-a",
          project: "default",
          status: "Running",
          type: "container",
          architecture: "x86_64",
          location: "member-a",
          config: {},
          devices: {},
          expanded_devices: {},
          profiles: ["default"],
          tags: [],
        }],
        nodes: ["cluster-a"],
        lxd_count: 1,
        microcloud_count: 0,
        incus_count: 0,
        offset: 0,
        limit: 100,
        has_more: false,
        unreachable_nodes: [],
      });
      return;
    }
    if (method === "GET" && path === "/cluster-members") {
      const node = url.searchParams.get("node") ?? "cluster-b";
      await json(route, {
        node,
        is_clustered: true,
        cluster_enabled: true,
        members: [{
          server_name: "member-b",
          url: "https://member-b.invalid:8443",
          database: true,
          status: "Online",
          message: "",
          roles: ["database-leader"],
          architecture: "x86_64",
          failure_domain: null,
          description: null,
          groups: [],
        }],
        total: 1,
      });
      return;
    }
    if (method === "GET" && path === "/storage/pools") {
      const node = url.searchParams.get("host") ?? "cluster-b";
      await json(route, {
        total: 1,
        pools: [{ name: "default", description: "", driver: "dir", config: {}, used_by: [], status: "Created", node }],
        nodes: [node],
        unreachable_nodes: [],
      });
      return;
    }
    if (method === "GET" && path === "/migrations/plans") {
      if (options.authorityListError) {
        await json(route, { detail: options.authorityListError.detail }, options.authorityListError.status);
        return;
      }
      const { limit, offset } = pagination(url);
      await json(route, list([...plans.values()], limit, offset));
      return;
    }
    if (method === "POST" && path === "/migrations/plans") {
      const body = state.mutations.at(-1).body;
      state.createdBodies.push(body);
      const created = makePlan({
        plan_id: options.createdPlanId ?? "plan-created",
        scope: body.scope,
        intent: body.intent,
        source: body.source,
        target: body.target,
        mappings: body.mappings,
        retention_hours: body.retention_hours,
        checkpoint_requested: body.checkpoint_requested,
        external_cutover_required: body.external_cutover_required,
      });
      plans.set(created.plan_id, created);
      await json(route, created, 201);
      return;
    }
    const executeMatch = path.match(/^\/migrations\/plans\/([^/]+)\/execute$/);
    if (method === "POST" && executeMatch) {
      state.executeAttempts += 1;
      state.executions.push(state.mutations.at(-1));
      if (options.failExecuteOnce && state.executeAttempts === 1) {
        await json(route, { detail: "Fixture lost the execute response after acceptance." }, 504);
        return;
      }
      const plan = plans.get(decodeURIComponent(executeMatch[1]));
      const executed = makeJob({
        job_id: "job-executed",
        plan_id: plan?.plan_id ?? "plan-ready",
        resourceName: plan?.source.resource_name ?? "source-vm",
      });
      jobs.set(executed.job_id, executed);
      await json(route, executed, 202);
      return;
    }
    const planMatch = path.match(/^\/migrations\/plans\/([^/]+)$/);
    if (planMatch) {
      const planId = decodeURIComponent(planMatch[1]);
      const plan = plans.get(planId);
      if (!plan) {
        await json(route, { detail: "Migration plan not found" }, 404);
        return;
      }
      if (method === "GET") {
        const reads = (state.planReads.get(planId) ?? 0) + 1;
        state.planReads.set(planId, reads);
        if ((options.failPlanReadOnceIds ?? []).includes(planId) && reads === 1) {
          await json(route, { detail: "Fixture draft read interrupted" }, 429, { "Retry-After": "1" });
          return;
        }
        await json(route, plan);
        return;
      }
      if (method === "PATCH") {
        if ((options.patchConflictPlanIds ?? []).includes(planId)) {
          await json(route, { detail: "Plan revision changed; reload and review again." }, 409);
          return;
        }
        const body = state.mutations.at(-1).body;
        state.patchBodies.push({ planId, body });
        const updated = { ...plan, ...body, revision: plan.revision + 1, updated_at: NOW };
        plans.set(planId, updated);
        await json(route, updated);
        return;
      }
    }
    if (method === "GET" && path === "/migrations") {
      state.jobListReads += 1;
      if (options.authorityListError) {
        await json(route, { detail: options.authorityListError.detail }, options.authorityListError.status);
      } else if (options.failInitialJobList && state.jobListReads === 1) {
        await json(route, { detail: "Fixture migration queue unavailable" }, 429, { "Retry-After": "1" });
      } else if (options.failJobListAfterFirst && state.jobListReads > 1) {
        await json(route, { detail: "Fixture migration stream unavailable" }, 429, { "Retry-After": "1" });
      } else {
        const { limit, offset } = pagination(url);
        await json(route, list([...jobs.values()], limit, offset));
      }
      return;
    }
    const jobMatch = path.match(/^\/migrations\/([^/]+)$/);
    if (method === "GET" && jobMatch) {
      const job = jobs.get(decodeURIComponent(jobMatch[1]));
      await json(route, job ?? { detail: "Migration job not found" }, job ? 200 : 404);
      return;
    }
    const actionMatch = path.match(/^\/migrations\/([^/]+)\/(cancel|cutover|confirm-external-cutover|rollback|restore-checkpoint|cleanup|recheck)$/);
    if (method === "POST" && actionMatch) {
      const job = jobs.get(decodeURIComponent(actionMatch[1]));
      const updated = job ? { ...job, revision: job.revision + 1 } : null;
      if (updated) jobs.set(updated.job_id, updated);
      await json(route, updated ?? { detail: "Migration job not found" }, updated ? 202 : 404);
      return;
    }

    state.unexpected.push(entry);
    await json(route, { detail: `Unexpected migration fixture request: ${method} ${path}` }, 599);
  });

  return state;
}

async function expectCleanMigrationHarness(state) {
  await expectCleanHarness(state.base);
  expect(state.authFailures, "migration requests without role fixture bearer").toEqual([]);
  expect(state.unexpected, "unexpected migration fixture requests").toEqual([]);
}

module.exports = {
  EXPIRED,
  FUTURE,
  expectCleanMigrationHarness,
  installMigrationFixtures,
  makeJob,
  makePlan,
};
