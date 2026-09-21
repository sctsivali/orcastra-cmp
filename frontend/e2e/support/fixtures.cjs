const { expect } = require("@playwright/test");
const {
  fullInstance,
  instanceMetrics,
  iso,
  monitoringDashboard,
  monitoringHistory,
  platformNodes,
  tenantSpecifications,
} = require("./api-fixture-data.cjs");
const {
  fulfillSnapshotCreate,
  fulfillSnapshotRead,
  initializeSnapshotState,
  isExactSnapshotCreate,
  parseMutation,
  snapshotReadDescriptor,
  snapshotTarget,
} = require("./snapshot-fixtures.cjs");

const BASE_URL = process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025";
const AUTH_SECRET =
  process.env.AUTH_SECRET ||
  process.env.NEXTAUTH_SECRET ||
  "orcastra-e2e-only-secret-32-characters-minimum";
const SESSION_COOKIE = "authjs.session-token";

const ROLE_GROUPS = {
  admin: ["role_admin"],
  partner: ["role_partner"],
  tenant: ["role_tenant"],
};


async function authenticate(context, role) {
  const groups = ROLE_GROUPS[role];
  if (!groups) throw new Error(`Unknown E2E role: ${role}`);
  const { encode } = await import("next-auth/jwt");
  const now = Math.floor(Date.now() / 1000);
  const email = `${role}@e2e.invalid`;
  const value = await encode({
    secret: AUTH_SECRET,
    salt: SESSION_COOKIE,
    maxAge: 60 * 60,
    token: {
      sub: email,
      authentikId: email,
      name: `${role[0].toUpperCase()}${role.slice(1)} E2E`,
      email,
      accessToken: `e2e-${role}-access-token`,
      refreshToken: "unused-e2e-refresh-token",
      expiresAt: now + 60 * 60,
      groups,
      roles: groups,
      iat: now,
      exp: now + 60 * 60,
      jti: `e2e-${role}-${now}`,
    },
  });

  await context.addCookies([
    {
      name: SESSION_COOKIE,
      value,
      url: BASE_URL,
      httpOnly: true,
      sameSite: "Lax",
      secure: false,
      expires: now + 60 * 60,
    },
  ]);
}

async function installApiFixtures(page, role, options = {}) {
  const state = {
    requests: [],
    unexpected: [],
    mutations: [],
    authFailures: [],
    externalRequests: [],
    unexpectedWebSockets: [],
    webSocketAuthFailures: [],
    monitoringSockets: [],
    instanceSockets: [],
    pageErrors: [],
    metricsMode: "success",
    metricsCalls: [],
    detailCalls: 0,
    tenantEmpty: options.tenantEmpty === true,
    nodeError: options.nodeError === true,
    instanceStatus: options.instanceStatus || "Running",
  };
  initializeSnapshotState(state, options);
  const configuredSnapshotTarget = snapshotTarget(options);

  page.on("pageerror", (error) => state.pageErrors.push(error.message));

  const baseUrl = new URL(BASE_URL);
  const allowedOrigin = baseUrl.origin;
  const recordExternalRequest = (request) => {
    state.externalRequests.push({
      method: request.method(),
      url: request.url(),
    });
  };

  await page.routeWebSocket(/^wss?:\/\//, async (webSocket) => {
    const url = new URL(webSocket.url());
    const entry = {
      path: url.pathname,
      url: webSocket.url(),
      openedAt: Date.now(),
      closedAt: null,
      closeCode: null,
    };
    const isExpectedMonitoringSocket =
      options.monitoringWebSocket === true &&
      role === "admin" &&
      url.pathname === "/api/v1/monitoring/ws" &&
      url.hostname === baseUrl.hostname &&
      url.port === "4050" &&
      url.protocol === (baseUrl.protocol === "https:" ? "wss:" : "ws:");
    const isExpectedInstanceSocket =
      options.snapshotInstancePage === true &&
      ["admin", "partner", "tenant"].includes(role) &&
      url.pathname === "/api/v1/events/ws" &&
      url.searchParams.get("host") === configuredSnapshotTarget.host &&
      url.hostname === baseUrl.hostname &&
      url.port === "4050" &&
      url.protocol === (baseUrl.protocol === "https:" ? "wss:" : "ws:");

    if (!isExpectedMonitoringSocket && !isExpectedInstanceSocket) {
      state.unexpectedWebSockets.push(entry);
      await webSocket.close({ code: 1008, reason: "Unexpected E2E WebSocket" });
      return;
    }

    if (url.searchParams.get("token") !== `e2e-${role}-access-token`) {
      state.webSocketAuthFailures.push(entry);
      await webSocket.close({ code: 1008, reason: "Invalid E2E WebSocket token" });
      return;
    }

    (isExpectedMonitoringSocket ? state.monitoringSockets : state.instanceSockets).push(entry);
    webSocket.onClose(async (code) => {
      entry.closedAt = Date.now();
      entry.closeCode = code ?? null;
      try {
        // onClose disables Playwright's default close forwarding. Complete the mocked server
        // side of the handshake so the page observes a cleanly closed connection.
        await webSocket.close({ code: code ?? 1000 });
      } catch {
        // The browser may finish tearing down the page before the mock sends its close frame.
      }
    });
    if (isExpectedInstanceSocket && options.instanceWebSocketDisconnect === true) {
      await webSocket.close({ code: 1012, reason: "Simulated event stream loss" });
    }
  });

  await page.route(
    (url) => ["http:", "https:"].includes(url.protocol) && url.origin !== allowedOrigin,
    async (route) => {
      recordExternalRequest(route.request());
      await route.abort("blockedbyclient");
    },
  );

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "") || "/";
    const method = request.method().toUpperCase();
    const entry = { method, path, search: url.search, at: Date.now() };
    state.requests.push(entry);

    // The more-specific API route wins over the general external-network route in Playwright.
    // Recheck the origin here so an absolute external /api/v1 URL remains fail-closed.
    if (url.origin !== allowedOrigin) {
      recordExternalRequest(request);
      await route.abort("blockedbyclient");
      return;
    }

    const expectedBearer = `Bearer e2e-${role}-access-token`;
    if (request.headers().authorization !== expectedBearer) {
      state.authFailures.push({ ...entry, authorization: request.headers().authorization || null });
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Missing role fixture bearer" }),
      });
      return;
    }

    if (method !== "GET") {
      const { mutation, bodyParseError } = parseMutation(request, entry);
      state.mutations.push(mutation);

      if (
        isExactSnapshotCreate({
          options,
          method,
          path,
          url,
          mutation,
          bodyParseError,
        })
      ) {
        await fulfillSnapshotCreate(route, state, options, mutation);
        return;
      }

      state.unexpectedMutations.push({
        ...mutation,
        reason: bodyParseError ?? "method, path, query, content type, or body did not match",
      });
      await route.fulfill({
        status: 405,
        contentType: "application/json",
        body: JSON.stringify({ detail: "E2E fixtures reject undeclared mutations" }),
      });
      return;
    }

    let status = 200;
    let headers = {};
    let body;

    const snapshotRead = snapshotReadDescriptor({
      path,
      url,
      entry,
      role,
      options,
      state,
      iso,
      fullInstance,
    });
    if (snapshotRead) {
      await fulfillSnapshotRead(route, snapshotRead);
      return;
    }

    if (path === "/user-preferences") {
      body = {};
    } else if (path === "/organizations/my-preferences") {
      body = { settings: {}, updated_at: null };
    } else if (path === "/organizations/my-preferences/version") {
      body = { updated_at: null };
    } else if (path === "/system/settings") {
      body = {
        read_only_mode: options.readOnly === true,
        api_version: "e2e",
        debug: false,
        snapshot_schedules_enabled: true,
        task_journal_enabled: false,
      };
    } else if (path === "/instances/write-policy") {
      const host = url.searchParams.get("host");
      if (host !== configuredSnapshotTarget.host) {
        state.unexpected.push(entry);
        status = 599;
        body = { detail: `Unexpected write-policy host: ${host}` };
      } else {
        const mode = options.instanceWritePolicyMode ??
          (role === "admin" ? "full" : role === "partner" ? "safe" : "view_only");
        body = {
          host,
          mode,
          can_edit_specifications: mode !== "view_only",
          can_use_advanced_create: mode === "full",
        };
      }
    } else if (options.instanceCreatePage === true && path === "/storage/pools") {
      body = {
        total: 1,
        pools: [{
          name: "default",
          description: "",
          driver: "dir",
          config: {},
          used_by: [],
          status: "Created",
          node: configuredSnapshotTarget.host,
        }],
        nodes: [configuredSnapshotTarget.host],
        unreachable_nodes: [],
      };
    } else if (options.instanceCreatePage === true && path === "/storage/volumes") {
      body = { total: 0, volumes: [], nodes: [configuredSnapshotTarget.host], unreachable_nodes: [] };
    } else if (options.instanceCreatePage === true && path === "/networks") {
      body = { total: 0, networks: [], nodes: [configuredSnapshotTarget.host], unreachable_nodes: [] };
    } else if (options.instanceCreatePage === true && path === "/profiles/default") {
      body = {
        name: "default",
        description: "",
        config: {},
        devices: { root: { type: "disk", path: "/", pool: "default" } },
        used_by: [],
        node: configuredSnapshotTarget.host,
        project: configuredSnapshotTarget.project,
        etag: '"profile-v1"',
      };
    } else if (options.instanceCreatePage === true && path === `/nodes/${configuredSnapshotTarget.host}/gpus`) {
      body = { host: configuredSnapshotTarget.host, gpus: [], total: 0, compute_total: 0 };
    } else if (options.instanceCreatePage === true && path === `/images/${configuredSnapshotTarget.host}`) {
      const images = options.instanceCreateImage ? [options.instanceCreateImage] : [];
      body = {
        host: configuredSnapshotTarget.host,
        project: configuredSnapshotTarget.project,
        total: images.length,
        images,
      };
    } else if (options.instanceCreatePage === true && path === "/images/remote") {
      body = { images: [], total: 0 };
    } else if (path === "/nodes") {
      if (state.nodeError) {
        status = 503;
        body = { detail: "Fixture node inventory unavailable" };
      } else {
        body = platformNodes();
      }
    } else if (path === "/instances/specifications") {
      body = tenantSpecifications({ empty: state.tenantEmpty });
    } else if (path === "/tenant-access/my-access") {
      body = {
        access: [
          {
            cluster_id: "cluster-a",
            project_name: "tenant-a",
            instance_name: "tenant-owned-vm",
            permissions: ["view", "operate", "console"],
          },
        ],
        total_clusters: 1,
        total_rules: 1,
      };
    } else if (path === "/monitoring/dashboard") {
      body = monitoringDashboard();
    } else if (path === "/monitoring/history") {
      body = monitoringHistory(url.searchParams.get("points"));
    } else if (path === "/warnings/system/all") {
      body = {
        total: 0,
        critical: 0,
        warning: 0,
        info: 0,
        lxd_warnings: [],
        certificate_warnings: [],
        node_warnings: [],
        nodes: ["cluster-a"],
      };
    } else if (path === "/warnings") {
      body = { total: 0, warnings: [], nodes: ["cluster-a"], unreachable_nodes: [] };
    } else if (path === "/instances/tenant-owned-vm") {
      state.detailCalls += 1;
      body = fullInstance(state.instanceStatus);
    } else if (path === "/vms/cluster-a/tenant-owned-vm/metrics") {
      const call = state.metricsCalls.length + 1;
      state.metricsCalls.push(entry);
      if (state.metricsMode === "unavailable") {
        status = 503;
        headers = { "Retry-After": "1" };
        body = { detail: "The cluster did not answer before the metrics deadline" };
      } else if (state.metricsMode === "forbidden") {
        status = 403;
        body = { detail: "Instance metrics are outside this actor scope" };
      } else if (state.metricsMode === "limited") {
        status = 429;
        headers = { "Retry-After": "30" };
        body = { detail: "Request budget reached" };
      } else {
        body = instanceMetrics(call);
      }
    } else {
      state.unexpected.push(entry);
      status = 599;
      body = { detail: `Unexpected E2E endpoint: ${method} ${path}${url.search}` };
    }

    await route.fulfill({
      status,
      headers,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });

  return state;
}

async function expectCleanHarness(state) {
  expect(state.unexpected, "unexpected API requests").toEqual([]);
  expect(state.unexpectedMutations, "undeclared or malformed mutations").toEqual([]);
  expect(state.snapshotMutations, "declared snapshot mutation count").toHaveLength(
    state.expectedSnapshotMutationCount,
  );
  expect(state.mutations, "only declared snapshot mutations may leave the browser").toEqual(
    state.snapshotMutations,
  );
  expect(state.authFailures, "requests without the role fixture bearer").toEqual([]);
  expect(state.externalRequests, "external requests attempted by the isolated browser test").toEqual(
    [],
  );
  expect(state.unexpectedWebSockets, "unexpected WebSocket connections").toEqual([]);
  expect(state.webSocketAuthFailures, "WebSocket connections with the wrong role token").toEqual(
    [],
  );
  expect(state.pageErrors, "uncaught browser errors").toEqual([]);
}

async function expectNoHorizontalOverflow(page) {
  const dimensions = await page.evaluate(() => ({
    viewport: Math.min(
      window.innerWidth,
      document.documentElement.clientWidth,
      window.visualViewport?.width ?? Number.POSITIVE_INFINITY,
    ),
    document: document.documentElement.scrollWidth,
  }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport + 1);
}

async function capture(page, testInfo, name) {
  await page.screenshot({
    path: testInfo.outputPath(`${name}.png`),
    fullPage: true,
    animations: "disabled",
  });
}

function countRequests(state, path) {
  return state.requests.filter((entry) => entry.path === path).length;
}

module.exports = {
  BASE_URL,
  authenticate,
  capture,
  countRequests,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
};
