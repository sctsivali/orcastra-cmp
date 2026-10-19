const { installApiFixtures, expectCleanHarness } = require("./fixtures.cjs");
const { fullInstance } = require("./api-fixture-data.cjs");

function serverRows(count) {
  return Array.from({ length: count }, (_, index) => ({
    node: `qa-server-${String(index + 1).padStart(3, "0")}`,
    node_type: ["LXD", "Incus", "MicroCloud"][index % 3],
    api_version: "1.0",
    auth: "trusted",
    public: false,
    environment: { server: "fixture", server_version: "qa", certificate: "redacted-fixture-value" },
    config: {},
  }));
}

async function installInventoryHardeningFixtures(page, role, options = {}) {
  const base = await installApiFixtures(page, role, options);
  const state = {
    base,
    requests: [],
    servers: serverRows(options.serverCount ?? 25),
    serverFailed: false,
    capacityFailed: false,
  };
  const readings = {
    healthy: { space: { used: 500, total: 1000 }, inodes: { used: 2, total: 10 } },
    zero: { space: { used: 0, total: 0 } },
    fractional: { space: { used: 0.5, total: 1 } },
    object: { space: {} },
    missing: {},
    negative: { space: { used: -1, total: 1000 } },
    invalid: { space: { used: "Infinity", total: "NaN" } },
  };
  const pools = Object.keys(readings).map((name) => ({
    name,
    driver: name === "object" ? "cephobject" : "dir",
    node: "cluster-a",
    status: "Created",
    description: "Synthetic capacity fixture",
    config: {},
    used_by: [],
  }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    if (!["/server", "/storage/pools", "/storage/pools/resources-all", "/instances", "/projects", "/clusters"].includes(path)) {
      return route.fallback();
    }
    const entry = { path, method: request.method(), search: url.search };
    state.requests.push(entry);
    const baseURL = new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025");
    if (url.origin !== baseURL.origin || request.headers().authorization !== `Bearer e2e-${role}-access-token`) {
      base.authFailures.push(entry);
      return route.fulfill({ status: 401, json: { detail: "Unexpected fixture actor or origin" } });
    }
    if (entry.method !== "GET" || (role === "tenant" && path === "/server")) {
      base.unexpected.push(entry);
      return route.fulfill({ status: 403, json: { detail: "Read is outside fixture role scope" } });
    }
    if (path === "/instances") {
      const vms = [fullInstance()];
      return route.fulfill({ json: {
        total: vms.length,
        vms,
        nodes: ["cluster-a"],
        unreachable_nodes: [],
        lxd_count: 1,
        microcloud_count: 0,
        incus_count: 0,
        offset: 0,
        limit: 0,
        has_more: false,
      } });
    }
    if (path === "/projects") return route.fulfill({ json: [] });
    if (path === "/clusters") {
      const clusters = [{
        cluster_id: "cluster-a",
        display_name: "Core Cluster",
        endpoint: "https://cluster-a.invalid:8443",
        zone: null,
        cluster_type: "LXD",
        clustered: false,
        merchant_id: null,
        status: "online",
        has_certificate: true,
        location: null,
        certificate_common_name: null,
        created_at: null,
        last_checked: null,
        error_message: null,
      }];
      return route.fulfill({ json: { clusters, total: clusters.length } });
    }
    if (path === "/server") {
      if (state.serverFailed) return route.fulfill({ status: 503, json: { detail: "Synthetic server read failed" } });
      return route.fulfill({ json: { total: state.servers.length, servers: state.servers, unreachable_nodes: ["qa-offline"], partial_nodes: [] } });
    }
    if (path === "/storage/pools") {
      return route.fulfill({ json: { total: pools.length, pools, nodes: ["cluster-a"], unreachable_nodes: [] } });
    }
    if (state.capacityFailed) return route.fulfill({ status: 503, json: { detail: "Synthetic capacity read failed" } });
    return route.fulfill({ json: {
      resources: Object.fromEntries(Object.entries(readings).map(([name, reading]) => [`cluster-a-${name}`, reading])),
      unreachable_nodes: [],
    } });
  });
  return state;
}

async function expectCleanInventoryHarness(state) {
  await expectCleanHarness(state.base);
}

module.exports = { installInventoryHardeningFixtures, expectCleanInventoryHarness, serverRows };
