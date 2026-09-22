const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  installApiFixtures,
} = require("./support/fixtures.cjs");

const LOCAL_FINGERPRINT = "a".repeat(64);
const INITIAL_ALLOWED_PATHS = new Set([
  "/nodes/names",
  "/nodes/availability",
  "/user-preferences",
  "/organizations/my-preferences",
  "/organizations/my-preferences/version",
  "/system/settings",
]);
const OPTIONAL_CREATE_PATHS = [
  "/images/remote",
  "/storage/volumes",
  "/networks",
  "/profiles/default",
];

function json(route, body, status = 200, headers = {}) {
  return route.fulfill({
    status,
    headers,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function projectInventory(host, names) {
  return {
    host,
    projects: names.map((name) => ({
      name,
      description: `${name} fixture`,
      config: {},
      used_by: [],
      host,
      tags: [],
    })),
    total: names.length,
    reachable: true,
    error: null,
  };
}

function poolInventory(host, names) {
  return {
    total: names.length,
    pools: names.map((name) => ({
      name,
      description: "",
      driver: "dir",
      config: {},
      used_by: [],
      status: "Created",
      node: host,
    })),
    nodes: [host],
    unreachable_nodes: [],
  };
}

function clusterAvailability(names) {
  const now = new Date().toISOString();
  return {
    clusters: names.map((name) => ({
      name,
      display_name: name === "cluster-a" ? "Cluster A" : "Cluster B",
      endpoint: `https://${name}.invalid:8443`,
      status: "online",
      reason_code: "ok",
      reason: null,
      node_type: "lxd",
      checked_at: now,
      available: true,
    })),
    online_count: names.length,
    total_count: names.length,
    source: "availability_cache",
    generated_at: now,
  };
}

function localImageInventory(host, project = "default") {
  return {
    host,
    project,
    total: 1,
    images: [{
      fingerprint: LOCAL_FINGERPRINT,
      aliases: ["local-orchestration-image"],
      description: "Local orchestration fixture",
      architecture: "x86_64",
      size: 1024,
      upload_date: null,
      public: false,
      type: "container",
      cached: true,
      auto_update: false,
      created_at: null,
      last_used_at: null,
      expires_at: null,
      profiles: [],
      project,
    }],
  };
}

function remoteImageInventory() {
  return {
    total: 1,
    images: [{
      distribution: "Debian",
      release: "bookworm",
      variant: "default",
      architecture: "amd64",
      type: "container",
      alias: "debian/bookworm",
      source: "Custom",
      server: "https://images.lxd.canonical.com",
      fingerprint: "b".repeat(64),
    }],
  };
}

function countRequests(requests, path, predicate = () => true) {
  return requests.filter((entry) => entry.path === path && predicate(entry)).length;
}

function isGpuPath(path) {
  return /^\/nodes\/[^/]+\/gpus$/.test(path);
}

function isLocalImagePath(path) {
  return /^\/images\/[^/]+$/.test(path) && path !== "/images/remote";
}

async function installCreateRoutes(page, {
  clusters = ["cluster-a"],
  projects = (host) => [host === "cluster-a" ? "tenant-a" : "tenant-b"],
  pools = (host) => [`${host}-pool`],
  onProjects,
  onPools,
  onLocalImages,
  onRemoteImages,
  onNetworks,
  onDefaultProfile,
} = {}) {
  const requests = [];

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method().toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "") || "/";
    const entry = {
      method,
      path,
      search: url.search,
      authorization: request.headers().authorization ?? null,
    };
    requests.push(entry);

    if (method !== "GET") {
      await route.fallback();
      return;
    }
    if (path === "/nodes/names") {
      await json(route, clusters);
      return;
    }
    if (path === "/nodes/availability") {
      await json(route, clusterAvailability(clusters));
      return;
    }
    const projectMatch = path.match(/^\/projects\/([^/]+)$/);
    if (projectMatch) {
      const host = decodeURIComponent(projectMatch[1]);
      if (onProjects) {
        await onProjects({ route, host, entry });
      } else {
        await json(route, projectInventory(host, projects(host)));
      }
      return;
    }
    if (path === "/storage/pools") {
      const host = url.searchParams.get("host") ?? "";
      if (onPools) {
        await onPools({ route, host, entry });
      } else {
        await json(route, poolInventory(host, pools(host)));
      }
      return;
    }
    if (path === "/instances/write-policy") {
      const host = url.searchParams.get("host") ?? "";
      await json(route, {
        host,
        mode: "full",
        can_edit_specifications: true,
        can_use_advanced_create: true,
      });
      return;
    }
    if (path === "/images/remote") {
      if (onRemoteImages) {
        await onRemoteImages({ route, entry });
      } else {
        await json(route, remoteImageInventory());
      }
      return;
    }
    if (path === "/networks" && onNetworks) {
      const host = url.searchParams.get("host") ?? "";
      const project = url.searchParams.get("project") ?? "default";
      await onNetworks({ route, host, project, entry });
      return;
    }
    if (path === "/profiles/default" && onDefaultProfile) {
      const host = url.searchParams.get("host") ?? "";
      const project = url.searchParams.get("project") ?? "default";
      await onDefaultProfile({ route, host, project, entry });
      return;
    }
    const localImageMatch = path.match(/^\/images\/([^/]+)$/);
    if (localImageMatch) {
      const host = decodeURIComponent(localImageMatch[1]);
      const project = url.searchParams.get("project") ?? "default";
      if (onLocalImages) {
        await onLocalImages({ route, host, project, entry });
      } else {
        await json(route, localImageInventory(host, project));
      }
      return;
    }

    await route.fallback();
  });

  return requests;
}

async function selectOption(page, label, option) {
  const picker = page.getByLabel(label, { exact: true });
  await expect(picker).toBeEnabled();
  const choice = page.getByRole("option", { name: option, exact: false });
  // Availability updates can replace the portalled option between pointer-down and pointer-up.
  // Reopen and resolve the current option instead of forcing a click on a detached element.
  await expect(async () => {
    if (!(await choice.isVisible())) await picker.click({ timeout: 1_000 });
    await choice.click({ timeout: 1_000 });
  }).toPass({ timeout: 10_000 });
  await expect(picker).toHaveAttribute("aria-expanded", "false");
  await expect(picker).toContainText(option);
}

async function expectNoOptionalReads(requests, { localImageReads = 0 } = {}) {
  for (const path of OPTIONAL_CREATE_PATHS) {
    expect(countRequests(requests, path), `${path} must remain intent-gated`).toBe(0);
  }
  expect(
    requests.filter((entry) => isLocalImagePath(entry.path)),
    localImageReads === 0
      ? "local image inventory must remain intent-gated"
      : "only the required local image inventory read is allowed",
  ).toHaveLength(localImageReads);
  expect(requests.filter((entry) => isGpuPath(entry.path)), "GPU inventory must remain intent-gated").toEqual([]);
}

function localImageRow(scope) {
  return scope.getByRole("row").filter({ hasText: "local-orchestration-image" });
}

test("create inventory reads are staged by cluster and picker intent", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  const requests = await installCreateRoutes(page);

  await page.goto("/inventory/instances/create");
  await expect(page.getByLabel("Cluster *", { exact: true })).toBeEnabled();
  await expect.poll(() => countRequests(requests, "/nodes/names")).toBe(1);
  await expect.poll(() => countRequests(requests, "/nodes/availability")).toBe(1);

  const unexpectedInitial = requests.filter((entry) => !INITIAL_ALLOWED_PATHS.has(entry.path));
  expect(unexpectedInitial, "the unselected form must only make global bootstrap reads").toEqual([]);
  await expectNoOptionalReads(requests);

  await selectOption(page, "Cluster *", "Cluster A");
  await expect.poll(() => countRequests(requests, "/projects/cluster-a")).toBe(1);
  await expect.poll(() => countRequests(
    requests,
    "/storage/pools",
    (entry) => entry.search === "?host=cluster-a",
  )).toBe(1);
  await expect.poll(() => countRequests(
    requests,
    "/instances/write-policy",
    (entry) => entry.search === "?host=cluster-a",
  )).toBe(1);
  await expectNoOptionalReads(requests);

  await page.getByRole("button", { name: "Select base image" }).click();
  await expect(page.getByRole("dialog", { name: "Select Base Image" })).toBeVisible();
  await expect.poll(() => countRequests(requests, "/images/cluster-a")).toBe(1);
  await expect.poll(() => countRequests(requests, "/images/remote")).toBe(1);
  await expect(localImageRow(page)).toHaveCount(1);
  await expect(localImageRow(page)).toBeVisible();
  await expect(page.getByText("debian/bookworm", { exact: true })).toBeVisible();

  expect(requests.every((entry) => entry.authorization === "Bearer e2e-admin-access-token")).toBe(true);
  await expectCleanHarness(api);
});

test("local images remain usable while the remote catalogue fails and retries", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  let releaseFirstRemote;
  const firstRemoteGate = new Promise((resolve) => { releaseFirstRemote = resolve; });
  let remoteReads = 0;
  const requests = await installCreateRoutes(page, {
    onRemoteImages: async ({ route }) => {
      remoteReads += 1;
      if (remoteReads === 1) {
        await firstRemoteGate;
      }
      if (remoteReads <= 2) {
        await json(route, { detail: "Remote catalogue unavailable" }, 503);
        return;
      }
      await json(route, remoteImageInventory());
    },
  });

  await page.goto("/inventory/instances/create");
  await selectOption(page, "Cluster *", "Cluster A");
  await page.getByRole("button", { name: "Select base image" }).click();

  const dialog = page.getByRole("dialog", { name: "Select Base Image" });
  await expect(localImageRow(dialog)).toHaveCount(1);
  await expect(localImageRow(dialog)).toBeVisible();
  await expect(dialog.getByText("Loading remote catalogues…", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Select" })).toBeEnabled();

  releaseFirstRemote();
  const retry = dialog.getByRole("button", { name: "Remote catalogues unavailable. Retry" });
  await expect(retry).toBeVisible();
  await expect.poll(() => countRequests(requests, "/images/remote")).toBe(2);
  await expect(localImageRow(dialog)).toBeVisible();
  await retry.click();

  await expect.poll(() => countRequests(requests, "/images/remote")).toBe(3);
  await expect(dialog.getByText("debian/bookworm", { exact: true })).toBeVisible();
  await expect(localImageRow(dialog)).toBeVisible();
  await expect(retry).toHaveCount(0);
  await expectCleanHarness(api);
});

test("Attach network waits for a non-eth0 inherited NIC before adding its override", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  let releaseDefaultProfile;
  const defaultProfileGate = new Promise((resolve) => { releaseDefaultProfile = resolve; });
  const requests = await installCreateRoutes(page, {
    onNetworks: async ({ route, host, project }) => {
      await json(route, {
        total: 1,
        networks: [{
          name: "tenant-net",
          description: "Tenant fixture network",
          type: "bridge",
          managed: true,
          config: { "ipv4.address": "10.42.0.1/24" },
          used_by: [],
          status: "Created",
          node: host,
          project,
        }],
        nodes: [host],
        unreachable_nodes: [],
      });
    },
    onDefaultProfile: async ({ route, host, project }) => {
      await defaultProfileGate;
      await json(route, {
        name: "default",
        description: "",
        config: {},
        devices: {
          root: { type: "disk", path: "/", pool: `${host}-pool` },
          lan0: { type: "nic", network: "tenant-net" },
        },
        used_by: [],
        node: host,
        project,
        etag: '"profile-non-eth0"',
      });
    },
  });

  await page.goto("/inventory/instances/create");
  await selectOption(page, "Cluster *", "Cluster A");
  await selectOption(page, "Project", "tenant-a");
  await expectNoOptionalReads(requests);

  await page.getByRole("button", { name: "Attach network" }).click();
  await expect.poll(() => countRequests(
    requests,
    "/networks",
    (entry) => entry.search === "?host=cluster-a&project=tenant-a",
  )).toBe(1);
  await expect.poll(() => countRequests(
    requests,
    "/profiles/default",
    (entry) => entry.search === "?host=cluster-a&project=tenant-a",
  )).toBe(1);
  await expect(page.getByLabel("Interface name", { exact: true })).toHaveCount(0);

  releaseDefaultProfile();
  const interfaceName = page.getByLabel("Interface name", { exact: true });
  await expect(interfaceName).toHaveCount(1);
  await expect(interfaceName).toHaveValue("lan0");
  await expect(page.getByLabel("Network", { exact: true })).toContainText("tenant-net");
  await expect(page.getByText("Overridden below", { exact: true })).toBeVisible();
  expect(countRequests(requests, "/networks")).toBe(1);
  expect(countRequests(requests, "/profiles/default")).toBe(1);
  await expectCleanHarness(api);
});

test("rapid cluster A to B to A changes reject stale options and selections", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  let releaseBProjects;
  let releaseBPools;
  const bProjectsGate = new Promise((resolve) => { releaseBProjects = resolve; });
  const bPoolsGate = new Promise((resolve) => { releaseBPools = resolve; });
  let aProjectReads = 0;
  let aPoolReads = 0;
  const requests = await installCreateRoutes(page, {
    clusters: ["cluster-a", "cluster-b"],
    onProjects: async ({ route, host }) => {
      if (host === "cluster-b") {
        await bProjectsGate;
        await json(route, projectInventory(host, ["b-stale-project"]));
        return;
      }
      aProjectReads += 1;
      await json(route, projectInventory(host, [
        aProjectReads === 1 ? "a-old-project" : "a-final-project",
      ]));
    },
    onPools: async ({ route, host }) => {
      if (host === "cluster-b") {
        await bPoolsGate;
        await json(route, poolInventory(host, ["b-stale-pool"]));
        return;
      }
      aPoolReads += 1;
      await json(route, poolInventory(host, [
        aPoolReads === 1 ? "a-old-pool" : "a-final-pool",
      ]));
    },
  });

  await page.goto("/inventory/instances/create");
  await selectOption(page, "Cluster *", "Cluster A");
  await selectOption(page, "Project", "a-old-project");
  await selectOption(page, "Storage Pool", "a-old-pool");
  await expect(page.getByLabel("Project", { exact: true })).toContainText("a-old-project");
  await expect(page.getByLabel("Storage Pool", { exact: true })).toContainText("a-old-pool");

  await selectOption(page, "Cluster *", "Cluster B");
  await expect.poll(() => countRequests(requests, "/projects/cluster-b")).toBe(1);
  await expect.poll(() => countRequests(
    requests,
    "/storage/pools",
    (entry) => entry.search === "?host=cluster-b",
  )).toBe(1);
  await selectOption(page, "Cluster *", "Cluster A");

  await expect.poll(() => countRequests(requests, "/projects/cluster-a")).toBe(2);
  await expect.poll(() => countRequests(
    requests,
    "/storage/pools",
    (entry) => entry.search === "?host=cluster-a",
  )).toBe(2);
  await expect(page.getByLabel("Project", { exact: true })).toContainText("default");
  await expect(page.getByLabel("Storage Pool", { exact: true })).toContainText("a-final-pool");

  releaseBProjects();
  releaseBPools();
  await expect.poll(() => countRequests(requests, "/projects/cluster-b")).toBe(1);
  await page.waitForTimeout(100);

  await expect(page.getByLabel("Project", { exact: true })).toContainText("default");
  await expect(page.getByLabel("Storage Pool", { exact: true })).toContainText("a-final-pool");
  await page.getByLabel("Project", { exact: true }).click();
  await expect(page.getByRole("option", { name: "a-final-project", exact: false })).toBeVisible();
  await expect(page.getByRole("option", { name: "a-old-project", exact: false })).toHaveCount(0);
  await expect(page.getByRole("option", { name: "b-stale-project", exact: false })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.getByLabel("Storage Pool", { exact: true }).click();
  await expect(page.getByRole("option", { name: "a-final-pool", exact: false })).toBeVisible();
  await expect(page.getByRole("option", { name: "a-old-pool", exact: false })).toHaveCount(0);
  await expect(page.getByRole("option", { name: "b-stale-pool", exact: false })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expectCleanHarness(api);
});

test("a local-image deep link wakes only the required local source", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", {
    snapshotInstancePage: true,
    instanceCreatePage: true,
  });
  const requests = await installCreateRoutes(page);

  await page.goto(
    `/inventory/instances/create?node=cluster-a&type=container&source=image&image=${LOCAL_FINGERPRINT}&project=tenant-a`,
  );
  await expect(page.getByRole("button", { name: /local-orchestration-image/ })).toBeVisible();
  await expect.poll(() => countRequests(requests, "/images/cluster-a")).toBe(1);
  expect(countRequests(requests, "/images/remote")).toBe(0);
  await expectNoOptionalReads(requests, { localImageReads: 1 });
  await expect(page.getByLabel("Cluster *", { exact: true })).toContainText("Cluster A");
  await expect(page.getByLabel("Project", { exact: true })).toContainText("tenant-a");
  await expectCleanHarness(api);
});
