const { test, expect } = require("@playwright/test");
const {
  authenticate,
  capture,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");
const catalog = require("../src/lib/regions/catalog.json");

// A cluster registered in a country abroad is shown as the whole country, because nobody knows
// the site inside it at registration. It used to be a pin on the capital, which claimed a place.
// Indonesian regencies and an API client's own site keep their pins.

const OUTLINES = "/geo/country-areas.v1.json";
const byId = new Map(catalog.regions.map((entry) => [entry.id, entry]));

const HW = {
  cpu_total: 8, cpu_sockets: 1, cpu_architecture: "x86_64", cpu_vendor: "Intel", cpu_model: "QA",
  memory_total: 0, memory_used: 0, gpu_total: 0, gpu_cards_total: 0, gpu_integrated_total: 0,
  gpu_vendors: [], gpu_models: [], storage_total: 0, storage_used: 0, storage_types: [], storage_drivers: [],
};

function region(id, statuses, over = {}) {
  const entry = byId.get(id);
  const base = entry
    ? {
        region_id: entry.id, region_name: entry.name, country_code: entry.country_code,
        country_name: entry.country_name, city: entry.city, latitude: entry.latitude,
        longitude: entry.longitude, continent: entry.continent,
        ...(entry.province ? { province: entry.province } : {}),
      }
    : {};
  const merged = { ...base, ...over };
  return {
    facility_name: null, facility_provider: null, instance_count: 0, instance_running: 0, hardware: HW,
    ...merged,
    node_count: statuses.length,
    nodes: statuses.map((status, i) => ({
      cluster_id: `${merged.region_id}-${i + 1}`, display_name: `${merged.region_name} ${i + 1}`,
      status, node_type: "LXD", hardware: HW,
    })),
  };
}

function overview(regions) {
  return {
    regions, total_nodes: regions.length, total_instances: 0, total_cpu: 0, total_memory: 0,
    total_storage: 0, total_gpu: 0, total_gpu_cards: 0, total_gpu_integrated: 0,
  };
}

const JAKARTA = region("id-3174", ["online"]);
const JAPAN = region("c-jp", ["online", "online"]);
const FLEET = [
  JAKARTA,
  JAPAN,
  region("c-us", ["online", "offline"]),
  region("c-sg", ["offline"]),
  region("c-ru", ["online"]),
  region("c-va", ["online"]),
  // saved while country entries still named the capital, before the backfill reaches it
  region("c-gb", ["online"], { city: "London", latitude: 51.5074, longitude: -0.1278 }),
  // an API client's own site abroad is a real place, so it stays a pin
  region("us-east-dc1", ["online"], {
    region_id: "us-east-dc1", region_name: "US East", country_code: "US", country_name: "United States",
    city: "Ashburn", latitude: 39.0438, longitude: -77.4874, continent: "NA",
  }),
  region("unknown", ["offline"], {
    region_id: "unknown", region_name: "Unknown", country_code: "XX", country_name: "Unknown",
    city: "Unknown", latitude: 0, longitude: 0, continent: "XX",
  }),
];
const SHADED = ["jp", "us", "sg", "ru", "va", "gb"];

async function openRegions(page, role, regions) {
  await authenticate(page.context(), role);
  const api = await installApiFixtures(page, role, { regionsOverview: () => overview(regions) });
  const outlineRequests = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === OUTLINES) outlineRequests.push(request.url());
  });
  await page.goto("/regions");
  await expect(page.getByText("Cluster Locations")).toBeVisible();
  await page.locator(".leaflet-container").waitFor({ state: "visible" });
  return { api, outlineRequests };
}

/** Regions the markers in the DOM stand for: a bubble counts its number. */
function markedRegions(page) {
  return page.evaluate(() => {
    let total = 0;
    for (const el of document.querySelectorAll(".custom-marker, .country-chip, .custom-cluster-icon")) {
      total += el.classList.contains("custom-cluster-icon") ? Number(el.textContent.match(/\d+/)?.[0] ?? 1) : 1;
    }
    return total;
  });
}

/**
 * A screen point that lands on the outline itself: inside its fill, inside the map frame, and
 * not under a chip or a pin. A bounding-box centre is often sea for a coastline like Japan's.
 */
async function pointOnOutline(page, code) {
  // elementFromPoint only sees the viewport, and on a phone the map sits below the stat tiles
  await page.locator(".leaflet-container").scrollIntoViewIfNeeded();
  const point = await page.evaluate((cls) => {
    const path = document.querySelector(`path.country-area-${cls}`);
    const frame = document.querySelector(".leaflet-container").getBoundingClientRect();
    if (!path) return null;
    const box = path.getBoundingClientRect();
    const toUser = path.getScreenCTM().inverse();
    for (let fy = 0.5; fy < 1; fy += 0.04) {
      for (const sy of [1, -1]) {
        for (let fx = 0.05; fx < 1; fx += 0.05) {
          const x = box.left + box.width * fx;
          const y = box.top + box.height * (0.5 + sy * (fy - 0.5));
          if (x < frame.left + 4 || x > frame.right - 4 || y < frame.top + 4 || y > frame.bottom - 4) continue;
          const user = new DOMPoint(x, y).matrixTransform(toUser);
          if (path.isPointInFill(user) && document.elementFromPoint(x, y) === path) return { x, y };
        }
      }
    }
    return null;
  }, code);
  expect(point, `no reachable point on the ${code} outline`).not.toBeNull();
  return point;
}

function outlineWidth(page, code) {
  return page.locator(`path.country-area-${code}`).evaluate((path) => path.getBoundingClientRect().width);
}

/** The outline's width once the map has stopped moving: a fly or a zoom animates for a while. */
async function settledWidth(page, code) {
  let previous = -1;
  for (let i = 0; i < 40; i += 1) {
    const width = await outlineWidth(page, code);
    if (Math.abs(width - previous) < 0.5) return width;
    previous = width;
    await page.waitForTimeout(250);
  }
  throw new Error(`the map kept moving around ${code}`);
}

for (const role of ["admin", "partner", "tenant"]) {
  test(`${role}: a country abroad is shaded whole and never named by its capital`, async ({ page }, testInfo) => {
    const { api, outlineRequests } = await openRegions(page, role, FLEET);

    const outlines = page.locator("path.country-area");
    await expect(outlines).toHaveCount(SHADED.length);
    for (const code of SHADED) await expect(page.locator(`path.country-area-${code}`)).toHaveCount(1);
    expect(outlineRequests).toHaveLength(1);

    // the status colour is the product token, read back from the rendered path
    const colours = await page.evaluate(() => {
      const token = (name) => {
        const probe = document.createElement("div");
        probe.style.color = `hsl(var(${name}))`;
        document.body.appendChild(probe);
        const value = getComputedStyle(probe).color;
        probe.remove();
        return value;
      };
      const path = (code) => getComputedStyle(document.querySelector(`path.country-area-${code}`));
      return {
        jp: path("jp").fill, us: path("us").fill, sg: path("sg").fill,
        online: token("--status-online"), pending: token("--status-pending"), offline: token("--status-offline"),
        opacity: path("jp").fillOpacity, dark: document.documentElement.classList.contains("dark"),
      };
    });
    expect(colours.jp).toBe(colours.online);
    expect(colours.us).toBe(colours.pending);
    expect(colours.sg).toBe(colours.offline);
    expect(colours.opacity).toBe(colours.dark ? "0.3" : "0.2");

    // every placeable region is marked once: six chips, Jakarta's pin and the API client's pin
    if (testInfo.project.name.startsWith("desktop")) {
      await expect.poll(() => markedRegions(page)).toBe(8);
    } else {
      await expect.poll(() => markedRegions(page)).toBeGreaterThan(0);
    }

    await expect(page.getByText("Pin: exact regency or city")).toBeVisible();
    await expect(page.getByText("Shaded: whole country, exact site not recorded")).toBeVisible();
    await expect(page.getByText("1 region has no location data and is not shown on the map.")).toBeVisible();

    await expect(page.locator("#region-card-c-gb")).toContainText("Whole country");
    await expect(page.locator("#region-card-c-jp")).toContainText("Whole country");
    await expect(page.locator("#region-card-us-east-dc1")).toContainText("Ashburn, United States");
    await expect(page.locator("#region-card-id-3174")).toContainText("Jakarta Selatan, Daerah Khusus Ibukota Jakarta");
    await expect(page.locator("body")).not.toContainText("London");

    await expectNoHorizontalOverflow(page);
    await page.locator(".leaflet-container").screenshot({ path: testInfo.outputPath(`map-${role}.png`) });
    await capture(page, testInfo, `regions-${role}`);
    await expectCleanHarness(api);
  });
}

test("clicking a shaded country selects it and opens its region", async ({ page }) => {
  const { api } = await openRegions(page, "admin", FLEET);
  await expect(page.locator("path.country-area-ru")).toHaveCount(1);

  const point = await pointOnOutline(page, "ru");
  await page.mouse.move(point.x, point.y);
  await expect(page.locator(".country-area-tooltip")).toContainText("Russia, 1 cluster");

  await page.mouse.click(point.x, point.y);
  await expect(page.locator(".leaflet-popup-content")).toContainText("Russia");
  await expect(page.locator(".leaflet-popup-content")).toContainText("Whole country");
  const card = page.locator("#region-card-c-ru");
  await expect(card).toBeInViewport();
  await expect(card).toContainText("Clusters in this region");
  await expectCleanHarness(api);
});

test("a country chip is reachable and selectable from the keyboard", async ({ page }) => {
  const { api } = await openRegions(page, "admin", [JAKARTA, JAPAN]);
  const chip = page.locator('.country-chip[title="Japan, whole country"]');
  await expect(chip).toHaveText(/JP\s*2/);
  await chip.focus();
  await page.keyboard.press("Enter");
  const card = page.locator("#region-card-c-jp");
  await expect(card).toBeInViewport();
  await expect(card).toContainText("Clusters in this region");
  await expectCleanHarness(api);
});

test("View on Map frames the country, again when it is already selected", async ({ page }) => {
  const { api } = await openRegions(page, "admin", [JAKARTA, JAPAN]);
  await expect(page.locator("path.country-area-jp")).toHaveCount(1);
  const card = page.locator("#region-card-c-jp");
  await card.getByText("Whole country").click();
  const viewOnMap = card.getByRole("button", { name: "View on Map" });

  const initial = await settledWidth(page, "jp");
  await viewOnMap.click();
  await expect.poll(() => outlineWidth(page, "jp"), { timeout: 8_000 }).toBeGreaterThan(initial * 1.5);
  const framed = await settledWidth(page, "jp");

  const zoomOut = page.getByRole("button", { name: "Zoom out" });
  await zoomOut.click();
  await settledWidth(page, "jp");
  await zoomOut.click();
  expect(await settledWidth(page, "jp")).toBeLessThan(framed / 2);

  // the selection did not change, so only the focus signal can move the map back
  await viewOnMap.click();
  await expect.poll(() => outlineWidth(page, "jp"), { timeout: 8_000 }).toBeGreaterThan(framed * 0.8);
  expect(Math.abs((await settledWidth(page, "jp")) - framed)).toBeLessThan(framed * 0.05);
  await expectCleanHarness(api);
});

test("hostile text in a stored location stays text on the chip, outline and popup", async ({ page }) => {
  // a legacy record read back without the input pattern, and an API client's own region abroad
  const hostile = "<img src=x onerror=\"window.__xss=1\">";
  const { api } = await openRegions(page, "admin", [
    JAKARTA,
    region("c-jp", ["online"], { region_name: hostile, country_name: hostile }),
    region("c-<x", ["online"], {
      region_id: "c-<x", region_name: hostile, country_code: "<X", country_name: hostile,
      city: hostile, latitude: 10, longitude: 20, continent: "AF",
    }),
  ]);
  await expect(page.locator("path.country-area-jp")).toHaveCount(1);
  await expect(page.locator(".country-chip").filter({ hasText: "<X" })).toHaveCount(1);

  const point = await pointOnOutline(page, "jp");
  await page.mouse.move(point.x, point.y);
  await expect(page.locator(".country-area-tooltip")).toContainText(hostile);
  await page.mouse.click(point.x, point.y);
  await expect(page.locator(".leaflet-popup-content")).toContainText(hostile);

  const injected = await page.evaluate(() => ({
    images: document.querySelectorAll(".leaflet-container img[src='x']").length,
    flagged: window.__xss ?? null,
  }));
  expect(injected).toEqual({ images: 0, flagged: null });
  await expectCleanHarness(api);
});

test("an Indonesia-only fleet never downloads the country outlines", async ({ page }) => {
  const { api, outlineRequests } = await openRegions(page, "admin", [JAKARTA]);
  await expect(page.locator(".custom-marker")).toHaveCount(1);
  await expect(page.getByText("Pin: exact regency or city")).toHaveCount(0);
  await expect(page.locator("path.country-area")).toHaveCount(0);
  expect(outlineRequests).toEqual([]);
  await expectCleanHarness(api);
});

test("missing country outlines leave the chips and say so", async ({ page }, testInfo) => {
  await page.route(`**${OUTLINES}`, (route) => route.fulfill({ status: 404, body: "not found" }));
  const { api } = await openRegions(page, "admin", [JAKARTA, JAPAN]);
  await expect(
    page.getByText("Country outlines could not be loaded, so each country is marked by its chip only."),
  ).toBeVisible();
  await expect(page.locator('.country-chip[title="Japan, whole country"]')).toHaveCount(1);
  await expect(page.locator("path.country-area")).toHaveCount(0);
  await capture(page, testInfo, "outlines-missing");
  await expectCleanHarness(api);
});
