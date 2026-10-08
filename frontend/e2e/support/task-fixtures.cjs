const { expect } = require("@playwright/test");
const { expectCleanHarness, installApiFixtures } = require("./fixtures.cjs");
const { fullInstance } = require("./api-fixture-data.cjs");

const baseURL = new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025");

function task(number, overrides = {}) {
  const stamp = new Date(Date.now() - 60_000).toISOString();
  return {
    id: `00000000-0000-4000-8000-${String(number).padStart(12, "0")}`,
    seq: number,
    kind: "lxd_operation",
    cluster_id: "cluster-a",
    project: "tenant-a",
    resource_type: "instance",
    resource_name: "tenant-owned-vm",
    operation_id: `fixture-operation-${number}`,
    operation_class: "task",
    action: "instance.start",
    description: `Fixture task ${number}`,
    status: "pending",
    status_code: 105,
    progress: null,
    error: null,
    may_cancel: true,
    actor_email: null,
    actor_role: null,
    actor_kind: "external",
    api_key_id: null,
    requestor: null,
    source: "events",
    request_id: null,
    target_organization_id: null,
    target_organization_slug: null,
    location: null,
    metadata: {},
    lxd_created_at: stamp,
    lxd_updated_at: stamp,
    started_at: null,
    finished_at: null,
    created_at: stamp,
    updated_at: stamp,
    duration_ms: null,
    queue_position: number,
    ...overrides,
  };
}

async function installTaskFixtures(page, role, options = {}) {
  const state = await installApiFixtures(page, role, { instanceListPage: true });
  state.queueRows = Array.from({ length: options.openTasks ?? 120 }, (_, index) => task(index + 1));
  state.dueSchedules = Array.from({ length: options.dueSchedules ?? 0 }, (_, index) => ({
    schedule_id: index + 1,
    cluster_id: "cluster-a",
    project: "tenant-a",
    instance_name: "tenant-owned-vm",
    label: `Fixture schedule ${index + 1}`,
    next_run_at: new Date(Date.now() + 60_000).toISOString(),
    run_state: "idle",
  }));
  state.disconnected = options.degraded ? ["cluster-a"] : [];
  state.followerActive = options.followerActive ?? true;
  state.taskRequests = [];
  state.taskSockets = [];
  state.taskMutations = [];
  state.cancelDetailReads = 0;
  state.cancelTarget = null;

  await page.route(/\/api\/v1\/(?:tasks(?:\/[^/?]+)?|cluster-log|system\/settings|instances)(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    if (url.origin !== baseURL.origin || request.headers().authorization !== `Bearer e2e-${role}-access-token`) {
      state.authFailures.push({ path, method: request.method() });
      await route.abort("blockedbyclient");
      return;
    }
    if (request.method() !== "GET") {
      const mutation = { path, method: request.method() };
      state.mutations.push(mutation);
      const row = state.queueRows.find((item) => path === `/tasks/${item.id}`);
      if (options.cancel === true && role !== "tenant" && request.method() === "DELETE" && row?.may_cancel) {
        state.taskMutations.push(mutation);
        state.cancelTarget = row.id;
        await route.fulfill({ json: {
          success: true, task_id: row.id, operation_id: row.operation_id,
          message: "Cancel requested; the task closes when the cluster confirms",
        } });
        return;
      }
      state.unexpectedMutations.push(mutation);
      await route.fulfill({ status: 405, json: { detail: "Task fixtures reject mutations" } });
      return;
    }
    state.taskRequests.push({ path, limit: url.searchParams.get("limit"), offset: url.searchParams.get("offset") });
    if (path === "/instances") {
      await route.fulfill({ json: {
        total: 1, instances: [fullInstance()], nodes: ["cluster-a"], unreachable_nodes: [],
      } });
      return;
    }
    if (path === "/system/settings") {
      await route.fulfill({ json: {
        read_only_mode: false, api_version: "e2e", debug: false,
        snapshot_schedules_enabled: true, task_journal_enabled: true,
      } });
      return;
    }
    const open = state.queueRows.filter((row) => ["pending", "running"].includes(row.status));
    const limit = Number(url.searchParams.get("limit") ?? 100);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    if (path === "/tasks/queue") {
      const rows = open.slice(offset, offset + limit);
      const due = offset === 0 ? state.dueSchedules.slice(0, 100) : [];
      const clusters = rows.length || due.length ? [{
        cluster_id: "cluster-a", state: state.disconnected.length ? "disconnected" : "connected",
        running: rows.filter((row) => row.status === "running"),
        pending: rows.filter((row) => row.status === "pending"), due_schedules: due,
      }] : [];
      await route.fulfill({ json: {
        clusters, generated_at: new Date().toISOString(), disconnected_clusters: state.disconnected,
        follower_active: state.followerActive, total_open: open.length, limit, offset,
        has_more: offset + rows.length < open.length, due_schedules_total: state.dueSchedules.length,
        due_schedules_truncated: due.length < state.dueSchedules.length,
      } });
    } else if (path === "/tasks") {
      await route.fulfill({ json: {
        tasks: limit === 0 ? [] : state.queueRows.slice(offset, offset + limit), total: state.queueRows.length,
        running: open.length, failed_recent: 0, limit, offset, has_more: offset + limit < state.queueRows.length,
        disconnected_clusters: state.disconnected, follower_active: state.followerActive,
      } });
    } else if (path === "/cluster-log") {
      await route.fulfill({ json: { entries: [], total: 0, limit, offset, has_more: false } });
    } else {
      const row = state.queueRows.find((item) => path === `/tasks/${item.id}`);
      if (row && row.id === state.cancelTarget) {
        state.cancelDetailReads += 1;
        if (state.cancelDetailReads >= 3) {
          row.status = "cancelled";
          row.status_code = 401;
          row.may_cancel = false;
          row.finished_at = new Date().toISOString();
          row.queue_position = null;
        }
      }
      await route.fulfill(row ? { json: { task: row, log: [] } } : { status: 404, json: { detail: "Task not found" } });
    }
  });

  await page.routeWebSocket(/\/api\/v1\/tasks\/ws(?:\?.*)?$/, async (socket) => {
    const url = new URL(socket.url());
    // The feed opens with a single-use tasks ticket minted by installApiFixtures, never a token.
    const ticket = url.searchParams.get("ticket");
    const minted = ticket ? state.wsTickets.get(ticket) : undefined;
    const valid =
      url.hostname === baseURL.hostname &&
      !url.searchParams.has("token") &&
      minted?.kind === "tasks" &&
      ticket.startsWith(`e2e-${role}-ticket-`) &&
      !state.spentWsTickets.has(ticket);
    if (ticket) state.spentWsTickets.add(ticket);
    if (!valid) {
      state.webSocketAuthFailures.push({ path: url.pathname });
      await socket.close({ code: 1008, reason: "Invalid task fixture actor" });
      return;
    }
    state.taskSockets.push(socket);
    socket.send(JSON.stringify({ type: "hello", server_time: new Date().toISOString(), follower_active: state.followerActive }));
  });
  return state;
}

async function expectCleanTaskHarness(state) {
  expect(state.mutations, "only explicitly declared task mutations may leave the browser").toEqual(state.taskMutations);
  await expectCleanHarness({ ...state, mutations: [] });
}

module.exports = { expectCleanTaskHarness, installTaskFixtures, task };
