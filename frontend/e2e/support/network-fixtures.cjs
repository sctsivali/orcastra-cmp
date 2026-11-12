const { expect } = require("@playwright/test");
const { expectCleanHarness, installApiFixtures, namesActor } = require("./fixtures.cjs");

const COMMON_KEYS = ["ipv4.address", "ipv6.address", "ipv4.nat", "ipv6.nat", "ipv4.dhcp", "ipv6.dhcp",
  "ipv4.dhcp.ranges", "ipv6.dhcp.stateful", "dns.domain", "security.acls"];
const CAPABILITIES = {
  known: true,
  types: ["bridge", "ovn"],
  config_keys: {
    bridge: [...COMMON_KEYS, "bridge.mtu", "dns.mode", "ipv6.dhcp.ranges"],
    ovn: [...COMMON_KEYS, "network"],
  },
};

async function installNetworkFixtures(page, role, options = {}) {
  const base = await installApiFixtures(page, role, { readOnly: options.readOnly === true });
  const state = {
    base, reads: [], creates: [], refused: [], capabilitiesUnavailable: options.capabilitiesUnavailable === true,
    networks: [], createStatus: options.createStatus || 200,
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const owned = ["/nodes/names", "/projects", "/projects/cluster-a", "/networks", "/network-acls", "/networks/creation/capabilities"];
    if (!owned.includes(path)) return route.fallback();
    expect(namesActor(request, role), `${request.url()} names ${role} and carries no bearer`).toBe(true);
    expect(url.origin).toBe(new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025").origin);
    const method = request.method();
    const send = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (method !== "GET") {
      let body;
      try { body = request.postDataJSON(); } catch { body = null; }
      const entry = { method, path, host: url.searchParams.get("host"), project: url.searchParams.get("project"), body };
      if (path !== "/networks" || method !== "POST" || role === "tenant" || !options.allowCreate ||
          entry.host !== "cluster-a" || !["default", "tenant-a"].includes(entry.project) ||
          !body || !["bridge", "ovn"].includes(body.type) || body.name !== "qa-network") {
        state.refused.push(entry);
        return send({ detail: "Undeclared network mutation" }, 405);
      }
      state.creates.push(entry);
      if (options.createDelay) await new Promise((resolve) => setTimeout(resolve, options.createDelay));
      if (state.createStatus !== 200) {
        return send({ detail: "Network creation timed out and may be partially applied; re-check the network list before retrying" }, state.createStatus);
      }
      const network = { ...body, config: body.config || {}, description: body.description || "", managed: true,
        used_by: [], status: "Created", node: entry.host, project: entry.project };
      state.networks.push(network);
      return send({ success: true, network });
    }
    state.reads.push({ path, host: url.searchParams.get("host"), project: url.searchParams.get("project") });
    if (path === "/nodes/names") return send(["cluster-a", "cluster-b"]);
    if (path === "/networks/creation/capabilities") {
      expect(url.searchParams.get("host")).toBe("cluster-a");
      if (state.capabilitiesUnavailable) return send({ detail: "Capabilities unavailable" }, 503);
      return send(options.capabilities || CAPABILITIES);
    }
    if (path === "/projects" || path === "/projects/cluster-a") {
      const projects = { host: "cluster-a", projects: [
        { name: "default", description: "Shared network catalogue", config: {} },
        { name: "tenant-a", description: "Scoped QA project", config: { "features.networks": "true" } },
      ], total: 2 };
      return send(path === "/projects" ? [projects] : projects);
    }
    if (path === "/network-acls") {
      expect(url.searchParams.get("host")).toBe("cluster-a");
      const project = url.searchParams.get("project");
      expect(["default", "tenant-a"]).toContain(project);
      return send({ total: 1, acls: [{ name: project === "tenant-a" ? "tenant-allow" : "qa-allow", node: "cluster-a", project }],
        nodes: ["cluster-a"], unreachable_nodes: [], partial_nodes: options.aclPartial ? ["cluster-a"] : [] });
    }
    if (path === "/networks") {
      const uplink = { name: "br-uplink", description: "QA uplink", type: "bridge", managed: true,
        node: "cluster-a", project: "default", config: { "ipv4.address": "10.40.0.1/24" }, used_by: [], status: "Created" };
      const rows = role === "tenant" ? [] : [uplink, ...state.networks];
      return send({ total: rows.length, networks: rows, nodes: ["cluster-a"], unreachable_nodes: [], partial_nodes: [] });
    }
  });
  return state;
}

async function expectCleanNetworkFixtures(state) {
  expect(state.refused, "undeclared network mutation").toEqual([]);
  await expectCleanHarness(state.base);
}

async function choose(page, label, option) {
  const trigger = page.getByLabel(label, { exact: true });
  await expect(trigger).toBeEnabled();
  await trigger.click();
  await page.getByRole("option", { name: option, exact: true }).click();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(trigger).toBeFocused();
}

async function openNetworkDialog(page) {
  await page.goto("/inventory/networks");
  await page.getByRole("button", { name: "Create Network", exact: true }).click();
  await choose(page, "Cluster *", /Core Cluster/);
  await expect(page.getByLabel("Bridge MTU", { exact: true })).toBeEnabled();
  await page.getByLabel("Name *", { exact: true }).fill("qa-network");
}

module.exports = { CAPABILITIES, choose, expectCleanNetworkFixtures, installNetworkFixtures, openNetworkDialog };
