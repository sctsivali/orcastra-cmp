const { expect } = require("@playwright/test");

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

const gib = 1024 ** 3;

function iso(offsetMs = 0) {
  return new Date(Date.now() + offsetMs).toISOString();
}

function nodeResources() {
  return {
    cpu_total: 32,
    cpu_used: 8,
    cpu_available: 24,
    cpu_allocated: 8,
    cpu_allocation_ratio: 0.25,
    cpu_overcommitted: false,
    cpu_sockets: 1,
    memory_total: 128 * gib,
    memory_used: 32 * gib,
    memory_available: 96 * gib,
    gpu_total: 1,
    gpu_used: 1,
    gpu_available: 0,
    gpu_cards_total: 1,
    gpu_integrated_total: 0,
    gpu_display_total: 0,
    gpu_unknown_total: 0,
    gpu_vendors: ["NVIDIA"],
    gpu_models: ["NVIDIA L4"],
    gpu_utilization_supported: false,
    gpu_cards: [
      {
        index: 0,
        id: 0,
        driver: "nvidia",
        driver_version: "550.54",
        pci_address: "0000:01:00.0",
        vendor: "NVIDIA",
        vendor_id: "10de",
        product: "L4",
        product_id: "27b8",
        numa_node: 0,
        in_use: true,
        used_by: "/1.0/instances/tenant-owned-vm?project=tenant-a",
        gpu_class: "compute",
        is_compute: true,
        is_integrated: false,
        attachable: true,
        model_name: "NVIDIA L4",
        memory_total: 24 * gib,
        memory_shared: false,
        mdev_profiles: [],
      },
    ],
    storage_total: 2 * 1024 ** 4,
    storage_used: 600 * gib,
    storage_available: 2 * 1024 ** 4 - 600 * gib,
    storage_raw_total: 2 * 1024 ** 4,
    storage_raw_used: 600 * gib,
    storage_pools: [
      {
        name: "fast-pool",
        driver: "zfs",
        space_total: 2 * 1024 ** 4,
        space_used: 600 * gib,
        space_available: 2 * 1024 ** 4 - 600 * gib,
      },
    ],
    degraded: false,
    degraded_parts: [],
    hardware: {
      scope: "node",
      member_count: 1,
      detected: true,
      system: {
        vendor: "Acme Compute",
        product: "Atlas 1U",
        family: "Atlas",
        version: "1",
        type: "server",
        firmware: { vendor: "Acme", version: "1.2.3", date: "2026-09-01" },
        chassis: { vendor: "Acme", type: "rack", version: "1" },
        motherboard: { vendor: "Acme", product: "Atlas Board", version: "1" },
      },
      cpu: {
        architectures: ["x86_64"],
        total: 32,
        cores: 16,
        threads: 32,
        numa_nodes: 1,
        sockets: [
          {
            socket: 0,
            name: "Xeon Test CPU",
            vendor: "Intel",
            cores: 16,
            threads: 32,
            frequency: 2400,
            frequency_turbo: 3400,
          },
        ],
      },
      memory: {
        total: 128 * gib,
        used: 32 * gib,
        free: 96 * gib,
        hugepages_total: 0,
        hugepages_used: 0,
        hugepages_size: 0,
      },
      networks: [],
      storage_devices: [
        {
          id: "nvme0n1",
          device: "/dev/nvme0n1",
          model: "Fixture NVMe",
          type: "nvme",
          size: 2 * 1024 ** 4,
          numa_node: 0,
          removable: false,
          read_only: false,
          firmware_version: "1.0",
          block_size: 4096,
          rpm: 0,
          partitions: 2,
        },
      ],
      storage_types: ["NVMe"],
    },
    collected_at: iso(),
  };
}

function platformNodes() {
  return {
    all_connected: false,
    collected_at: iso(),
    nodes: [
      {
        name: "cluster-a",
        display_name: "Core Cluster",
        endpoint: "https://cluster-a.invalid:8443",
        connected: true,
        reachable: true,
        status: "online",
        node_type: "LXD",
        clustered: false,
        server_version: "6.9",
        api_version: "1.0",
        auth: "trusted",
        error: null,
        tags: [],
        environment: {
          architectures: ["x86_64"],
          kernel: "Linux",
          kernel_architecture: "x86_64",
          kernel_version: "6.8.0",
          os_name: "Ubuntu",
          os_version: "24.04",
          server: "lxd",
          server_version: "6.9",
          driver: "lxc",
          driver_version: "6.0",
          storage: "zfs",
          storage_version: "2.2",
          firewall: "nftables",
          instance_types: ["container", "virtual-machine"],
          storage_supported_drivers: [{ Name: "zfs", Version: "2.2", Remote: false }],
        },
        resources: nodeResources(),
      },
      {
        name: "cluster-unreachable",
        display_name: "Edge Cluster",
        endpoint: "https://edge.invalid:8443",
        connected: false,
        reachable: false,
        status: "offline",
        node_type: "LXD",
        clustered: null,
        server_version: null,
        api_version: null,
        auth: null,
        environment: null,
        error: "Connection timed out",
        resources: null,
        tags: [],
      },
    ],
  };
}

function tenantSpecifications({ empty = false } = {}) {
  return {
    total: empty ? 0 : 1,
    collected_at: iso(),
    // The UI must disclose that the result is partial without disclosing this cluster id.
    unreachable_nodes: ["customer-secret-cluster"],
    instances: empty
      ? []
      : [
          {
            name: "tenant-owned-vm",
            host: "cluster-a",
            project: "tenant-a",
            // Lowercase on purpose. The controlled display label must remain Title Case.
            status: "running",
            type: "virtual-machine",
            architecture: "x86_64",
            cpu: { value: "4", source: "explicit" },
            memory: { value: "8GiB", source: "inherited" },
            root_disk: { value: "80GiB", pool: "fast-pool", source: "explicit" },
            gpu_count: 1,
            gpu_devices: ["gpu0"],
          },
        ],
  };
}

function fullInstance(status = "Running") {
  return {
    name: "tenant-owned-vm",
    status,
    status_code: status === "Running" ? 103 : 102,
    ip_addresses: ["10.10.0.20"],
    host: "cluster-a",
    host_endpoint: "https://cluster-a.invalid:8443",
    location: "member-1",
    project: "tenant-a",
    architecture: "x86_64",
    created_at: "2026-09-18T09:00:00Z",
    last_used_at: "2026-09-19T09:00:00Z",
    description: "Tenant workload",
    type: "virtual-machine",
    resources: null,
    config: {
      "limits.cpu": "4",
      "limits.memory": "8GiB",
      "volatile.base_image": "fixture-image-fingerprint-that-remains-complete",
    },
    devices: { root: { type: "disk", path: "/", pool: "fast-pool", size: "80GiB" } },
    expanded_devices: {
      root: { type: "disk", path: "/", pool: "fast-pool", size: "80GiB" },
      eth0: { type: "nic", network: "tenant-net" },
    },
    profiles: ["default"],
    tags: ["tenant"],
  };
}

function instanceMetrics(call) {
  const calculating = call === 1;
  return {
    instance_name: "tenant-owned-vm",
    host: "cluster-a",
    status: "Running",
    cpu_usage_percent: calculating ? 0 : 22.5,
    memory_used_bytes: 3 * gib,
    memory_total_bytes: 8 * gib,
    memory_usage_percent: 37.5,
    disk_usage_bytes: { root: 24 * gib },
    network_rx_bytes: { eth0: 1024 * 1024 },
    network_tx_bytes: { eth0: 512 * 1024 },
    processes: 42,
    uptime_seconds: 7200,
    base_image: "fixture-image",
    ipv4_addresses: ["10.10.0.20"],
    ipv6_addresses: [],
    pid: 1234,
    cpu_sample_status: calculating ? "calculating" : "ready",
    unavailable_sections: [],
    // The first reading deliberately proves the 15-second stale threshold.
    timestamp: calculating ? iso(-20_000) : iso(),
  };
}

function monitoringDashboard() {
  const consumer = {
    name: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    status: "running",
    cpu_usage: 22.5,
    memory_usage_bytes: 3 * gib,
    memory_limit_bytes: 8 * gib,
    disk_usage_bytes: 24 * gib,
    network_rx_bytes: 1024 * 1024,
    network_tx_bytes: 512 * 1024,
  };
  return {
    timestamp: iso(),
    total_nodes: 1,
    online_nodes: 1,
    inactive_nodes: 0,
    total_instances: 1,
    running_instances: 1,
    stopped_instances: 0,
    other_instances: 0,
    container_count: 0,
    vm_count: 1,
    total_cpu: 32,
    used_cpu: 4,
    cpu_usage_percent: 12.5,
    realtime_cpu_percent: 22.5,
    total_memory_bytes: 128 * gib,
    used_memory_bytes: 32 * gib,
    memory_usage_percent: 25,
    realtime_memory_bytes: 3 * gib,
    realtime_memory_percent: 2.34,
    total_storage_bytes: 2 * 1024 ** 4,
    used_storage_bytes: 600 * gib,
    storage_usage_percent: 29.3,
    total_gpu: 1,
    total_gpu_cards: 1,
    used_gpu: 1,
    nodes: [
      {
        node_name: "cluster-a",
        node_type: "LXD",
        is_online: true,
        cluster_status: "online",
        status_message: null,
        cpu_total: 32,
        cpu_used: 4,
        cpu_usage_percent: 12.5,
        cpu_allocated: 4,
        cpu_allocation_ratio: 0.125,
        cpu_overcommitted: false,
        memory_total_bytes: 128 * gib,
        memory_used_bytes: 32 * gib,
        memory_usage_percent: 25,
        storage_total_bytes: 2 * 1024 ** 4,
        storage_used_bytes: 600 * gib,
        storage_usage_percent: 29.3,
        gpu_total: 1,
        gpu_used: 1,
        gpu_available: 0,
        gpu_cards_total: 1,
        gpu_integrated_total: 0,
        gpu_models: ["NVIDIA L4"],
        gpu_utilization_supported: false,
        instance_count: 1,
        running_instances: 1,
      },
    ],
    storage_pools: [
      {
        name: "fast-pool",
        node: "cluster-a",
        members: [],
        scope: null,
        driver: "zfs",
        total_bytes: 2 * 1024 ** 4,
        used_bytes: 600 * gib,
        available_bytes: 2 * 1024 ** 4 - 600 * gib,
        usage_percent: 29.3,
      },
    ],
    top_cpu_consumers: [consumer],
    top_memory_consumers: [consumer],
    warnings: { total: 0, critical: 0, warning: 0, info: 0 },
    collection_time_ms: 125,
  };
}

function monitoringHistory(points = 60) {
  const count = Math.min(Math.max(Number(points) || 12, 2), 24);
  return Array.from({ length: count }, (_, index) => ({
    timestamp: iso((index - count) * 5_000),
    cpu_usage_percent: 10 + index * 0.5,
    memory_usage_percent: 25 + index * 0.25,
    storage_usage_percent: 29.3,
    network_rx_bytes: index * 100_000,
    network_tx_bytes: index * 80_000,
    disk_io_bytes: index * 20_000,
    running_instances: 1,
    nodes: {
      "cluster-a": {
        cpu_usage_percent: 10 + index * 0.5,
        memory_usage_percent: 25 + index * 0.25,
        storage_usage_percent: 29.3,
        network_rx_bytes: index * 100_000,
        network_tx_bytes: index * 80_000,
        running_instances: 1,
      },
    },
  }));
}

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
    pageErrors: [],
    metricsMode: "success",
    metricsCalls: [],
    detailCalls: 0,
    tenantEmpty: options.tenantEmpty === true,
    nodeError: options.nodeError === true,
    instanceStatus: options.instanceStatus || "Running",
  };

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

    if (!isExpectedMonitoringSocket) {
      state.unexpectedWebSockets.push(entry);
      await webSocket.close({ code: 1008, reason: "Unexpected E2E WebSocket" });
      return;
    }

    if (url.searchParams.get("token") !== `e2e-${role}-access-token`) {
      state.webSocketAuthFailures.push(entry);
      await webSocket.close({ code: 1008, reason: "Invalid E2E WebSocket token" });
      return;
    }

    state.monitoringSockets.push(entry);
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

    if (method !== "GET") {
      state.mutations.push(entry);
      await route.fulfill({
        status: 405,
        contentType: "application/json",
        body: JSON.stringify({ detail: "E2E fixtures reject all mutations" }),
      });
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

    let status = 200;
    let headers = {};
    let body;

    if (path === "/user-preferences") {
      body = {};
    } else if (path === "/organizations/my-preferences") {
      body = { settings: {}, updated_at: null };
    } else if (path === "/organizations/my-preferences/version") {
      body = { updated_at: null };
    } else if (path === "/system/settings") {
      body = {
        read_only_mode: false,
        api_version: "e2e",
        debug: false,
        snapshot_schedules_enabled: true,
        task_journal_enabled: false,
      };
    } else if (path === "/nodes") {
      if (state.nodeError) {
        status = 503;
        body = { detail: "Fixture node inventory unavailable" };
      } else {
        body = platformNodes();
      }
    } else if (path === "/instances/specifications") {
      body = tenantSpecifications({ empty: state.tenantEmpty });
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
  expect(state.mutations, "live-style mutations attempted by a read-only browser test").toEqual([]);
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
    viewport: window.innerWidth,
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
