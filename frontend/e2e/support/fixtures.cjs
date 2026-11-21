const { expect } = require("@playwright/test");
const { webcrypto } = require("node:crypto");
const {
  fullInstance,
  instanceMetrics,
  iso,
  monitoringDashboard,
  monitoringHistory,
  platformAvailability,
  platformNodes,
  tenantSpecifications,
} = require("./api-fixture-data.cjs");
const { kernelAdvisories, kernelCveUbuntuStatus, kernelWarningRows } = require("./kernel-advisory-fixtures.cjs");
const {
  fulfillSnapshotCreate,
  fulfillSnapshotRead,
  initializeSnapshotState,
  isExactSnapshotCreate,
  parseMutation,
  snapshotReadDescriptor,
  snapshotTarget,
} = require("./snapshot-fixtures.cjs");
const { editionModule } = require("./edition.cjs");

// An edition's own endpoints (e2e/editions/<edition>/routes.cjs). Asked only for a request no CMP
// fixture below answers, so Full and every CMP endpoint behave the same on any edition.
const editionRoutes = editionModule("routes");
// Paths an edition's shell reads on every page, beside /system/settings and the others, for the specs
// that pin a page's exact reads.
const EDITION_SHELL_READS = Object.freeze([...(editionRoutes?.shellReads ?? [])]);

const BASE_URL = process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025";
const AUTH_SECRET =
  process.env.AUTH_SECRET ||
  process.env.NEXTAUTH_SECRET ||
  "orcastra-e2e-only-secret-32-characters-minimum";
const SESSION_COOKIE = "authjs.session-token";
// The browser never holds the access token: every request names its account here, and the
// /api/v1 proxy attaches the token from the session cookie (src/lib/api-proxy-auth.ts).
const ACTOR_HEADER = "x-orcastra-actor";

function actorOf(role) {
  return encodeURIComponent(`${role}@e2e.invalid`);
}

/** True when a browser request names the role's account and carries no credential of its own. */
function namesActor(request, role) {
  const headers = request.headers();
  return headers[ACTOR_HEADER] === actorOf(role) && headers.authorization === undefined;
}

// Node 18 does not expose WebCrypto globally, while Auth.js/Jose expects the
// browser-compatible API when generating the synthetic E2E session token.
if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const ROLE_GROUPS = {
  admin: ["role_admin"],
  partner: ["role_partner"],
  tenant: ["role_tenant"],
};


async function authenticate(context, role, { sessionRole = role, accessExpiresIn = 60 * 60, groups: groupsClaim, tokenFields = {} } = {}) {
  // A refreshed synthetic session can lose privileges without changing its principal or bearer.
  // accessExpiresIn below 60 s keeps the server from hydrating the session, as for a deep link
  // opened in a new tab long after the last refresh: the page then reads /api/auth/session itself.
  // `groups` replaces the role's canonical group, for an IdP that names it another way.
  // `tokenFields` adds or overrides session fields, for a session the identity provider ended.
  const groups = groupsClaim ?? ROLE_GROUPS[sessionRole];
  if (!ROLE_GROUPS[role] || !groups) throw new Error(`Unknown E2E role: ${sessionRole}`);
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
      expiresAt: now + accessExpiresIn,
      groups,
      roles: groups,
      iat: now,
      exp: now + 60 * 60,
      jti: `e2e-${role}-${now}`,
      ...tokenFields,
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
    // Single-use WebSocket tickets minted through POST /ws-tickets: ticket -> request body.
    wsTickets: new Map(),
    spentWsTickets: new Set(),
    monitoringSockets: [],
    instanceSockets: [],
    pageErrors: [],
    // What the page's Content Security Policy blocked (enforced) or would block (report-only).
    cspViolations: [],
    cspReportOnly: [],
    metricsMode: "success",
    metricsCalls: [],
    detailCalls: 0,
    // The Monitoring page's fleet instance list: `true` answers it, "unavailable"
    // answers 503. Off by default so a page that must not read the fleet list still fails here.
    monitoringInstanceList: options.monitoringInstanceList || false,
    tenantEmpty: options.tenantEmpty === true,
    nodeError: options.nodeError === true,
    instanceStatus: options.instanceStatus || "Running",
  };
  initializeSnapshotState(state, options);
  const configuredSnapshotTarget = snapshotTarget(options);

  page.on("pageerror", (error) => state.pageErrors.push(error.message));
  page.on("console", (message) => {
    const text = message.text();
    if (!/Content Security Policy/i.test(text)) return;
    (text.includes("[Report Only]") ? state.cspReportOnly : state.cspViolations).push(text.slice(0, 300));
  });

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
      (options.snapshotInstancePage === true || options.instanceListPage === true) &&
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

    // A socket proves its caller with a ticket minted for exactly this socket, once; a URL that
    // carries the access token is refused like a wrong ticket.
    const ticket = url.searchParams.get("ticket");
    const minted = ticket ? state.wsTickets.get(ticket) : undefined;
    const ticketMatches =
      minted !== undefined &&
      !state.spentWsTickets.has(ticket) &&
      ticket.startsWith(`e2e-${role}-ticket-`) &&
      (isExpectedMonitoringSocket
        ? minted.kind === "monitoring"
        : minted.kind === "events" && minted.host === url.searchParams.get("host"));
    if (ticket) state.spentWsTickets.add(ticket);
    if (url.searchParams.has("token") || !ticketMatches) {
      state.webSocketAuthFailures.push(entry);
      await webSocket.close({ code: 1008, reason: "Invalid E2E WebSocket ticket" });
      return;
    }

    (isExpectedMonitoringSocket ? state.monitoringSockets : state.instanceSockets).push(entry);
    if (isExpectedInstanceSocket) options.onInstanceSocket?.(webSocket);
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

    if (!namesActor(request, role)) {
      state.authFailures.push({
        ...entry,
        actor: request.headers()[ACTOR_HEADER] || null,
        authorization: request.headers().authorization || null,
      });
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Missing role fixture actor" }),
      });
      return;
    }

    if (method === "POST" && path === "/ws-tickets") {
      const ticket = `e2e-${role}-ticket-${state.wsTickets.size + 1}`;
      state.wsTickets.set(ticket, JSON.parse(request.postData() || "{}"));
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({ ticket, tickets: null, expires_in: 30 }),
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
      body = options.byteUnits ? { byteUnits: options.byteUnits } : {};
    } else if (path === "/organizations/my-preferences") {
      body = { organization_id: 0, settings: {}, updated_at: null, version: "e2e-built-in", source: "default", subject: "deployment" };
    } else if (path === "/organizations/my-preferences/version") {
      body = { organization_id: 0, updated_at: null, version: "e2e-built-in" };
    } else if (path === "/system/settings") {
      body = {
        read_only_mode: options.readOnly === true,
        api_version: "e2e",
        debug: false,
        snapshot_schedules_enabled: true,
        task_journal_enabled: false,
        ...(options.tenantRebuildEnabled === undefined ? {} : { tenant_rebuild_enabled: options.tenantRebuildEnabled }),
      };
    } else if (path === "/system/vault-status") {
      // The shell's Vault notice reads this on every page; a healthy Vault shows nothing.
      body = { state: "ok", checked_at: Date.now() / 1000, registry_source: "vault" };
    } else if (path === "/system/host-health") {
      // An Administrator's sidebar counts the dashboard's dependencies needing attention on every
      // page (Host Health); a healthy report shows no count.
      body = {
        checks: [],
        overall: "ok",
        attention: 0,
        collected_at: Date.now() / 1000,
        source: "cached",
        store: "shared",
        interval_seconds: 30,
      };
    } else if (path === "/system/release-status") {
      // An Administrator's shell reads what the release manifest says about this version; other
      // roles never send it. Up to date here, so nothing shows.
      body = {
        enabled: true,
        running_version: "1.0.0-RC4-hotfix3",
        level: "none",
        reasons: [],
        running_status: "latest",
        latest_version: "1.0.0-RC4-hotfix3",
        security_version: null,
        notes_url: null,
        upgrade_url: null,
        checked_at: new Date().toISOString(),
        last_success_at: new Date().toISOString(),
        source: "github",
        error: null,
        stale: false,
        ...(options.releaseStatus || {}),
      };
    } else if (path === "/me/access") {
      // Asked by a Tenant session (a fixture actor can become one mid-test by a downgrade) and by a
      // session whose groups map to no role. The default is a Tenant with access; `accessVerdict`
      // (an object, or a function called per request) answers otherwise, for example NO_ROLE_VERDICT.
      const verdict = typeof options.accessVerdict === "function" ? options.accessVerdict() : options.accessVerdict;
      body = {
        email: `${role}@e2e.invalid`,
        role: "tenant",
        has_role: true,
        blocked_reason: null,
        tenant_access: { total_clusters: 1, total_rules: 1 },
        ...(verdict || {}),
      };
    } else if (path === "/tenant-access/orphan-rules") {
      // Tenant Access lists the rules the audit found naming nothing; none here.
      body = { rules: [], total: 0 };
    } else if (path === "/my-keys/last-used") {
      // My Keys reads when each key last changed something; no key has here.
      body = { items: [] };
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
    } else if (typeof options.regionsOverview === "function" && path === "/regions") {
      body = options.regionsOverview();
    } else if (path === "/nodes/availability") {
      body = platformAvailability();
    } else if (path === "/nodes") {
      if (state.nodeError) {
        status = 503;
        body = { detail: "Fixture node inventory unavailable" };
      } else {
        body = platformNodes({
          serverVersion: options.nodeServerVersion,
          release: options.nodeRelease,
          tags: options.nodeTags,
        });
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
            permissions: options.tenantPermissions ?? ["view", "operate", "console"],
          },
        ],
        total_clusters: 1,
        total_rules: 1,
      };
    } else if (path === "/monitoring/dashboard") {
      body = typeof options.monitoringDashboard === "function" ? options.monitoringDashboard() : monitoringDashboard();
    } else if (path === "/monitoring/history") {
      if (url.searchParams.has("points")) state.unexpected.push(entry);
      body = monitoringHistory(url.searchParams.get("window"));
    } else if (path === "/warnings/system/all") {
      // options.kernel: "patched" (default), "exposed", "unconfirmed" or "two" (kernel-advisory-fixtures.cjs)
      const kernelRows = kernelWarningRows(options.kernel);
      const critical = kernelRows.filter((r) => r.severity === "critical").length;
      body = {
        total: kernelRows.length,
        critical,
        warning: kernelRows.length - critical,
        info: 0,
        lxd_warnings: [],
        certificate_warnings: [],
        node_warnings: [],
        kernel_advisories: kernelRows,
        nodes: ["cluster-a"],
      };
    } else if (path === "/security/kernel-advisories") {
      // Partner and Admin only (require_partner); a tenant asking at all is a defect.
      if (role === "tenant") {
        state.unexpected.push(entry);
        status = 403;
        body = { detail: "Partner access required" };
      } else {
        body = kernelAdvisories(options.kernel);
      }
    } else if (path === "/security/kernel-advisories/ubuntu-status") {
      // When Ubuntu's records were last read, for the Kernel CVEs tab; Partner and Admin only.
      if (role === "tenant") {
        state.unexpected.push(entry);
        status = 403;
        body = { detail: "Partner access required" };
      } else {
        body = kernelCveUbuntuStatus(options.kernelUbuntu);
      }
    } else if (path === "/warnings") {
      body = { total: 0, warnings: [], nodes: ["cluster-a"], unreachable_nodes: [] };
    } else if (path === "/instances" && state.monitoringInstanceList && url.search.replace(/^\?/, "").split("&").every((pair) => pair === "" || pair === "fresh=true")) {
      if (state.monitoringInstanceList === "unavailable") {
        status = 503;
        body = { detail: "The clusters did not answer the instance list" };
      } else {
        body = {
          total: 1,
          vms: [fullInstance(state.instanceStatus)],
          nodes: ["cluster-a"],
          lxd_count: 1,
          microcloud_count: 0,
          incus_count: 0,
          offset: 0,
          limit: 1,
          has_more: false,
          unreachable_nodes: [],
          collected_at: iso(),
        };
      }
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
        body = typeof options.instanceMetrics === "function"
          ? options.instanceMetrics(call, state.instanceStatus)
          : instanceMetrics(call, state.instanceStatus);
      }
    } else {
      const answer = editionRoutes?.answer({ method, path, url, role, options, state });
      if (answer === undefined) {
        state.unexpected.push(entry);
        status = 599;
        body = { detail: `Unexpected E2E endpoint: ${method} ${path}${url.search}` };
      } else {
        ({ status = 200, headers = {}, body } = answer);
      }
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
  expect(
    state.snapshotReconciliation.freshInstanceReads,
    "declared fresh instance reconciliation reads",
  ).toBe(state.expectedSnapshotReconciliation.freshInstanceReads);
  expect(
    state.snapshotReconciliation.operationReads,
    "declared snapshot operation reconciliation reads",
  ).toHaveLength(state.expectedSnapshotReconciliation.operationReads);
  expect(state.mutations, "only declared snapshot mutations may leave the browser").toEqual(
    state.snapshotMutations,
  );
  expect(state.authFailures, "requests without the role fixture bearer").toEqual([]);
  expect(state.externalRequests, "external requests attempted by the isolated browser test").toEqual(
    [],
  );
  expect(state.unexpectedWebSockets, "unexpected WebSocket connections").toEqual([]);
  expect(state.webSocketAuthFailures, "WebSocket connections without a valid role ticket").toEqual(
    [],
  );
  expect(state.pageErrors, "uncaught browser errors").toEqual([]);
  expect(state.cspViolations ?? [], "what the enforced Content Security Policy blocked").toEqual([]);
  // The report-only policy is the next tightening; what it would block is listed, not failed.
  for (const line of new Set(state.cspReportOnly ?? [])) console.log(`[csp-report-only] ${line}`);
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

/**
 * A deep link the server refuses (auth.ts authorized()) is answered with a redirect home before
 * any page code loads.
 *
 * The redirect is asserted and not followed: `next start`, which serves these specs, hands the
 * middleware a Host of localhost, so a followed redirect leaves the 127.0.0.1 origin and its
 * session cookie. The standalone server that ships keeps the origin the request came on.
 */
async function expectServerRedirectHome(page, path) {
  const response = await page.request.get(path, { maxRedirects: 0 });
  expect([302, 303, 307, 308], `${path} answered ${response.status()}`).toContain(response.status());
  expect(new URL(response.headers().location, BASE_URL).pathname, path).toBe("/");
}

// What /me/access answers an account with no role group and no membership.
const NO_ROLE_VERDICT = Object.freeze({ role: null, has_role: false, blocked_reason: "no_role_assigned", tenant_access: null });

module.exports = {
  ACTOR_HEADER,
  EDITION_SHELL_READS,
  NO_ROLE_VERDICT,
  BASE_URL,
  actorOf,
  authenticate,
  expectServerRedirectHome,
  capture,
  countRequests,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
  namesActor,
};
