const { expect } = require("@playwright/test");
const { expectCleanHarness, installApiFixtures } = require("./fixtures.cjs");

// note: plans, jobs and settings come from a golden file the backend generates from its real
// presenters (backend/tests/test_migration_e2e_contract.py), so these fixtures cannot drift
// from the shape the server sends.
const CONTRACT = require("./migration-contract.json");

const NOW = "2026-09-22T12:00:00Z";
const FUTURE = "2099-09-22T12:00:00Z";
const EXPIRED = "2020-09-22T12:00:00Z";

const clone = (value) => JSON.parse(JSON.stringify(value));

function makePlan(overrides = {}, variant = "move_ready") {
  return { ...clone(CONTRACT.plans[variant]), ...overrides };
}

function makeJob(overrides = {}) {
  const resourceName = overrides.resourceName ?? "source-vm";
  const phase = overrides.phase ?? "queued";
  const status = overrides.status ?? "pending";
  const cleaned = { ...overrides };
  delete cleaned.resourceName;
  const base = clone(CONTRACT.jobs.source_retained);
  return {
    ...base,
    job_id: `job-${resourceName}`,
    source: { ...base.source, resource_name: resourceName },
    target: { ...base.target, resource_name: resourceName },
    phase,
    status,
    timeline: [{ sequence: 1, phase, message: `Fixture entered ${phase}.`, created_at: NOW }],
    available_actions: overrides.available_actions ?? [],
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
  // instanceListPage admits the Instances page's own event socket for a spec that starts there.
  const base = await installApiFixtures(page, role, { instanceListPage: options.instanceListPage === true });
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
      path === "/networks" ||
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
      const executable = (value) => options.executionEnabled !== false && value;
      const projectRelocation = (value) => options.projectRelocationEnabled ?? value;
      await json(route, {
        ...CONTRACT.settings,
        read_only_mode: options.readOnly === true,
        api_version: "e2e",
        snapshot_schedules_enabled: true,
        task_journal_enabled: false,
        migration_center_enabled: options.centerEnabled !== false && CONTRACT.settings.migration_center_enabled,
        migration_durable_copy_enabled: executable(CONTRACT.settings.migration_durable_copy_enabled),
        migration_instance_move_enabled: executable(CONTRACT.settings.migration_instance_move_enabled),
        migration_project_member_relocation_enabled: projectRelocation(
          CONTRACT.settings.migration_project_member_relocation_enabled,
        ),
        migration_project_cross_cluster_enabled: projectRelocation(
          CONTRACT.settings.migration_project_cross_cluster_enabled,
        ),
      });
      return;
    }
    if (method === "GET" && path === "/clusters") {
      await json(route, { clusters: [cluster("cluster-a"), cluster("cluster-b")], total: 2 });
      return;
    }
    if (method === "GET" && path === "/projects") {
      if (options.projectsGate) await options.projectsGate;
      await json(route, [
        { host: "cluster-a", projects: [{ name: "default", description: "", config: {}, used_by: [], host: "cluster-a", tags: [] }], total: 1, reachable: true, error: null },
        { host: "cluster-b", projects: [{ name: "target-project", description: "", config: {}, used_by: [], host: "cluster-b", tags: [] }], total: 1, reachable: true, error: null },
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
      const prefix = node === "cluster-a" ? "member-a" : "member-b";
      const members = [prefix, `${prefix}2`].map((serverName, index) => ({
        server_name: serverName,
        url: `https://${serverName}.invalid:8443`,
        database: true,
        status: "Online",
        message: "",
        roles: index === 0 ? ["database-leader"] : ["database"],
        architecture: "x86_64",
        failure_domain: null,
        description: null,
        groups: [],
      }));
      await json(route, { node, is_clustered: true, cluster_enabled: true, members, total: members.length });
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
    if (method === "GET" && path === "/networks") {
      const host = url.searchParams.get("host") ?? "cluster-b";
      await json(route, {
        total: 1,
        networks: [{
          name: "lxdbr1",
          description: "",
          type: "bridge",
          managed: true,
          config: {},
          used_by: [],
          status: "Created",
          node: host,
          project: url.searchParams.get("project") ?? "default",
        }],
        nodes: [host],
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
        const body = state.mutations.at(-1).body;
        state.patchBodies.push({ planId, body });
        if ((options.patchConflictPlanIds ?? []).includes(planId)) {
          // The server's own wording (repository.py), so the wizard's conflict recovery runs.
          await json(route, { detail: "The plan changed. Reload it before applying changes." }, 409);
          return;
        }
        // A PATCH re-runs preflight on the server. A fixture plan can name the variant that
        // the new revision resolves to, such as a blocked plan whose network is now mapped.
        const variant = (options.patchVariants ?? {})[planId];
        const base = variant ? { ...makePlan({}, variant), plan_id: planId } : plan;
        const updated = { ...base, ...body, revision: plan.revision + 1, updated_at: NOW };
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
    const actionMatch = path.match(/^\/migrations\/([^/]+)\/(cancel|cutover|confirm-external-cutover|rollback|restore-checkpoint|cleanup|recheck|release)$/);
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
