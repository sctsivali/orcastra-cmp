const { test, expect } = require("@playwright/test");
const {
  authenticate,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");

// A Tenant is sent Regions without hardware: GET /regions answers it with null hardware, a
// null hypervisor type and null hardware totals, the same line GET /nodes draws. The page
// shows it places, clusters and its own instances instead, and never a zero for a figure it
// was not given.

const HW = {
  cpu_total: 64, cpu_sockets: 2, cpu_architecture: "x86_64", cpu_vendor: "Intel", cpu_model: "Xeon",
  memory_total: 512 * 1024 ** 3, memory_used: 0, gpu_total: 2, gpu_cards_total: 3, gpu_integrated_total: 0,
  gpu_vendors: ["NVIDIA"], gpu_models: ["A100"], storage_total: 10 * 1024 ** 4, storage_used: 0,
  storage_types: [], storage_drivers: [],
};

function jakarta(hardware, nodeType) {
  return {
    region_id: "id-3174", region_name: "Kota Administrasi Jakarta Selatan", country_code: "ID",
    country_name: "Indonesia", city: "Jakarta Selatan", province: "Daerah Khusus Ibukota Jakarta",
    latitude: -6.2615, longitude: 106.8106, continent: "AS", facility_name: null, facility_provider: null,
    node_count: 1, instance_count: 3, instance_running: 2, hardware,
    nodes: [{ cluster_id: "nuc2", display_name: "NUC 2", status: "online", node_type: nodeType, hardware }],
  };
}

const TENANT_BODY = {
  regions: [jakarta(null, null)], total_nodes: 1, total_instances: 3,
  total_cpu: null, total_memory: null, total_storage: null, total_gpu: null,
  total_gpu_cards: null, total_gpu_integrated: null,
};

const PARTNER_BODY = {
  regions: [jakarta(HW, "microcloud")], total_nodes: 1, total_instances: 3,
  total_cpu: 64, total_memory: HW.memory_total, total_storage: HW.storage_total, total_gpu: 2,
  total_gpu_cards: 3, total_gpu_integrated: 0,
};

async function openRegions(page, role, body) {
  await authenticate(page.context(), role);
  const api = await installApiFixtures(page, role, { regionsOverview: () => body });
  await page.goto("/regions");
  await expect(page.getByText("Cluster Locations")).toBeVisible();
  return api;
}

function tile(page, label) {
  return page.locator("main").getByText(label, { exact: true });
}

test("tenant: Regions shows places and its own instances, no fleet hardware", async ({ page }) => {
  const api = await openRegions(page, "tenant", TENANT_BODY);

  for (const label of ["Regions", "Clusters", "Instances", "Running"]) {
    await expect(tile(page, label).first()).toBeVisible();
  }
  for (const label of ["CPU Threads", "Memory", "Storage", "Compute GPUs"]) {
    await expect(tile(page, label)).toHaveCount(0);
  }

  const card = page.locator("#region-card-id-3174");
  await expect(card).toContainText("3 instances");
  await expect(card).toContainText("2 running");
  await card.locator("[aria-expanded]").first().click();
  await expect(card).toContainText("Clusters in this region");
  await expect(card).toContainText("NUC 2");
  await expect(card).not.toContainText("CPU threads");
  await expect(card).not.toContainText("GPU");
  // the instance counts appear once, in the header, not again in the body
  await expect(card.getByText("3 instances")).toHaveCount(1);

  await expect(page.locator("main")).not.toContainText(/\b0 B\b|NaN|undefined|null/);
  await expectNoHorizontalOverflow(page);
  await expectCleanHarness(api);
});

test("tenant: a region popup on the map carries no hardware rows", async ({ page }) => {
  const api = await openRegions(page, "tenant", TENANT_BODY);
  await page.locator(".leaflet-container").scrollIntoViewIfNeeded();
  const pin = page.locator(".custom-marker").first();
  await expect(pin).toBeVisible();
  await pin.click();
  const popup = page.locator(".leaflet-popup-content");
  await expect(popup).toContainText("Clusters");
  await expect(popup).toContainText("Instances");
  await expect(popup).not.toContainText("CPU threads");
  await expect(popup).not.toContainText("Memory");
  await expectCleanHarness(api);
});

test("partner: Regions keeps the fleet hardware tiles and the per-cluster figures", async ({ page }) => {
  const api = await openRegions(page, "partner", PARTNER_BODY);

  for (const label of ["Regions", "Clusters", "CPU Threads", "Memory", "Storage", "Compute GPUs"]) {
    await expect(tile(page, label).first()).toBeVisible();
  }
  await expect(tile(page, "Running")).toHaveCount(0);
  await expect(page.locator("main")).toContainText("3 cards detected");

  const card = page.locator("#region-card-id-3174");
  await expect(card).toContainText("64 CPU threads");
  await card.locator("[aria-expanded]").first().click();
  await expect(card).toContainText("3 instances");
  // The node's type is shown through formatHypervisorType instead of the raw API value.
  await expect(card).toContainText("MicroCloud");
  await expectCleanHarness(api);
});

test("partner: a cluster whose hardware was not read says so instead of showing zeros", async ({ page }) => {
  const offline = {
    ...jakarta(null, "lxd"),
    region_id: "id-3674", region_name: "Kota Tangerang Selatan", city: "Tangerang Selatan",
    province: "Banten", latitude: -6.2886, longitude: 106.7179, instance_count: 0, instance_running: 0,
    nodes: [{ cluster_id: "bliss001", display_name: "BSD Lab", status: "offline", node_type: null, hardware: null }],
  };
  const api = await openRegions(page, "partner", { ...PARTNER_BODY, regions: [PARTNER_BODY.regions[0], offline], total_nodes: 2 });

  const card = page.locator("#region-card-id-3674");
  await expect(card).toContainText("Hardware not read");
  await card.locator("[aria-expanded]").first().click();
  await expect(card).toContainText("BSD Lab");
  await expect(card).toContainText("0 instances");
  await expect(card).not.toContainText("CPU threads");
  await expect(card).not.toContainText(/\b0 B\b/);
  // the region that was read keeps its figures
  await expect(page.locator("#region-card-id-3174")).toContainText("64 CPU threads");
  await expectCleanHarness(api);
});

test("partner: fleet tiles say Not read when no cluster's hardware was read", async ({ page }) => {
  const unread = {
    ...jakarta(null, "lxd"),
    nodes: [{ cluster_id: "nuc2", display_name: "NUC 2", status: "offline", node_type: "lxd", hardware: null }],
  };
  const body = {
    regions: [unread], total_nodes: 1, total_instances: 0,
    total_cpu: 0, total_memory: 0, total_storage: 0, total_gpu: 0, total_gpu_cards: 0, total_gpu_integrated: 0,
  };
  const api = await openRegions(page, "partner", body);
  for (const label of ["CPU Threads", "Memory", "Storage", "Compute GPUs"]) {
    await expect(tile(page, label).first()).toBeVisible();
  }
  await expect(page.locator("main").getByText("Not read", { exact: true })).toHaveCount(4);
  await expect(page.locator("main")).not.toContainText(/\b0 B\b/);
  await expectCleanHarness(api);
});
