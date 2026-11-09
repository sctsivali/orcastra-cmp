const { test, expect } = require("@playwright/test");
const {
  BASE_URL,
  authenticate,
  capture,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");

// Card illustrations (components/illustrations): decoration that names a card's subject. These
// specs hold what a reader cannot see in review: a scene is hidden from assistive technology and
// takes no focus, stays inside its card, paints its theme's tokens, does not move after it lands,
// and disappears from the first byte of HTML when the organization or the account hides it, with
// the card's classic icon back in its place and no gap left where the scene was.

const SCENE = '[data-slot="card-illustration"]';
const FALLBACK = '[data-slot="illustration-fallback"]';
const HINT = "orcastra-personalization";
// The hero face, read from illustrations.css: #3A2482 on light, #BBABFF on dark.
const HERO_FACE = { light: "rgb(58, 36, 130)", dark: "rgb(187, 171, 255)" };

const themeOf = (testInfo) => (testInfo.project.name.includes("dark") ? "dark" : "light");
const isMobile = (testInfo) => testInfo.project.name.startsWith("mobile");

/**
 * The two preference reads, answered from state, and the personal write applied to it the way
 * routers/user_preferences.py applies it: a key sent as null clears that choice, a key left out
 * is kept. Registered after installApiFixtures, so these routes answer first.
 */
async function installPreferences(page, role, { organization = {}, personal = {} } = {}) {
  const state = { personal: { ...personal }, writes: [], foreignActor: [] };
  const bearer = `Bearer e2e-${role}-access-token`;
  const personalView = () => ({ theme: "system", lastModified: null, illustrations: null, ...state.personal });
  await page.route("**/api/v1/user-preferences", async (route) => {
    const request = route.request();
    if (request.headers().authorization !== bearer) state.foreignActor.push(request.method());
    if (request.method() === "GET") return route.fulfill({ json: personalView() });
    if (request.method() !== "PUT") return route.fallback();
    const body = request.postDataJSON();
    state.writes.push(body);
    for (const [key, value] of Object.entries(body)) {
      if (value === null) delete state.personal[key];
      else state.personal[key] = value;
    }
    return route.fulfill({ json: personalView() });
  });
  await page.route("**/api/v1/organizations/my-preferences", (route) =>
    route.fulfill({ json: { organization_id: 17, settings: organization, updated_at: null, version: "e2e-illustrations" } }));
  await page.route("**/api/v1/organizations/my-preferences/version", (route) =>
    route.fulfill({ json: { organization_id: 17, updated_at: null, version: "e2e-illustrations" } }));
  return state;
}

// Home reads the fleet's instances and projects. The shared fixtures answer the instance list
// behind monitoringInstanceList; the project list is answered here.
const HOME = { monitoringInstanceList: true };
async function answerProjects(page) {
  await page.route(/\/api\/v1\/projects(\?fresh=true)?$/, (route) => route.request().method() === "GET"
    ? route.fulfill({ json: [{ host: "cluster-a", total: 1, reachable: true, error: null, projects: [{ name: "default", description: "Default project", config: {}, used_by: [] }] }] })
    : route.fallback());
}

async function hint(context, choice) {
  await context.addCookies([{ name: HINT, value: encodeURIComponent(JSON.stringify({ il: choice })), url: BASE_URL, sameSite: "Lax" }]);
}

function visibleScenes(page) {
  return page.locator(SCENE).evaluateAll((nodes) =>
    nodes.filter((node) => getComputedStyle(node).display !== "none" && node.getBoundingClientRect().width > 0).length);
}

async function expectDecorative(page) {
  const report = await page.locator(SCENE).evaluateAll((nodes) => nodes.map((node) => ({
    ariaHidden: node.getAttribute("aria-hidden"),
    focusable: node.getAttribute("focusable"),
    tabStops: node.querySelectorAll("a, button, input, [tabindex]").length,
    text: node.textContent.trim(),
    pointer: getComputedStyle(node).pointerEvents,
  })));
  expect(report.length, "the page draws scenes").toBeGreaterThan(0);
  for (const scene of report) {
    expect(scene).toEqual({ ariaHidden: "true", focusable: "false", tabStops: 0, text: "", pointer: "none" });
  }
}

async function expectInsideCards(page) {
  const escapes = await page.locator(SCENE).evaluateAll((nodes) => nodes
    .filter((node) => getComputedStyle(node).display !== "none")
    .map((node) => {
      const card = node.closest(".rounded-xl, .rounded-lg");
      if (!card) return "a scene outside any card";
      const s = node.getBoundingClientRect();
      const c = card.getBoundingClientRect();
      const inside = s.left >= c.left - 1 && s.right <= c.right + 1 && s.top >= c.top - 1 && s.bottom <= c.bottom + 1;
      return inside ? null : `scene ${Math.round(s.left)},${Math.round(s.top)} ${Math.round(s.width)}x${Math.round(s.height)} leaves its card`;
    })
    .filter(Boolean));
  expect(escapes).toEqual([]);
}

async function heroFill(page) {
  return page.locator(`${SCENE} .illo-face.illo-ink.illo-left`).first().evaluate((node) => getComputedStyle(node).fill);
}

async function sceneBoxes(page) {
  return page.locator(SCENE).evaluateAll((nodes) => nodes
    .filter((node) => getComputedStyle(node).display !== "none")
    .map((node) => {
      const r = node.getBoundingClientRect();
      return [Math.round(r.left * 2) / 2, Math.round((r.top + window.scrollY) * 2) / 2, Math.round(r.width), Math.round(r.height)];
    }));
}

test("admin: Home draws a decorative scene in each feature card, the call to action and the totals", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", HOME);
  await answerProjects(page);
  const prefs = await installPreferences(page, "admin");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Platform Features" })).toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");

  // Six features, the call to action and the four totals.
  expect(await visibleScenes(page)).toBe(11);
  await expectDecorative(page);
  await expectInsideCards(page);
  expect(await heroFill(page), "the hero face paints the theme's token").toBe(HERO_FACE[themeOf(testInfo)]);
  const fallbacks = await page.locator(FALLBACK).evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).display));
  expect(fallbacks, "the classic icons wait behind the scenes").toEqual(Array(6).fill("none"));

  const landed = await sceneBoxes(page);
  await page.waitForTimeout(1500);
  expect(await sceneBoxes(page), "a scene does not move once it has landed").toEqual(landed);

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "home-scenes");
  expect(prefs.writes, "reading never writes").toEqual([]);
  expect(prefs.foreignActor).toEqual([]);
  await expectCleanHarness(api);
});

test("an organization that hides scenes gets the classic cards from the first byte, with no gap", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  await hint(page.context(), "hide");
  const api = await installApiFixtures(page, "partner", HOME);
  await answerProjects(page);
  await installPreferences(page, "partner", { organization: { illustrations: "hide" } });

  const firstByte = await (await page.request.get("/")).text();
  expect(firstByte, "the server renders the choice before any script runs").toContain('data-illustrations="hide"');

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Platform Features" })).toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "hide");
  expect(await visibleScenes(page)).toBe(0);
  await expect(page.locator(`${FALLBACK} svg`)).toHaveCount(6);
  for (const icon of await page.locator(`${FALLBACK} svg`).all()) await expect(icon).toBeVisible();

  // The title takes the whole header row back: nothing is reserved for the missing scene.
  const reserved = await page.locator(FALLBACK).evaluateAll((nodes) => nodes.map((node) => {
    const title = node.parentElement;
    const header = title.parentElement;
    const style = getComputedStyle(header);
    const inner = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    return Math.round(inner - title.getBoundingClientRect().width);
  }));
  for (const gap of reserved) expect(gap).toBeLessThanOrEqual(1);

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "home-hidden");
  await expectCleanHarness(api);
});

test("an account's own choice to show beats its organization's hide", async ({ page }) => {
  await authenticate(page.context(), "partner");
  await hint(page.context(), "show");
  const api = await installApiFixtures(page, "partner", HOME);
  await answerProjects(page);
  await installPreferences(page, "partner", { organization: { illustrations: "hide" }, personal: { illustrations: "show" } });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Platform Features" })).toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "show");
  expect(await visibleScenes(page)).toBe(11);
  await expectCleanHarness(api);
});

test("tenant: the theme menu hides scenes for this account, survives a reload, and returns to the default", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "the sidebar is a drawer on a phone; the desktop menu is the same component");
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", HOME);
  await answerProjects(page);
  const prefs = await installPreferences(page, "tenant");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Platform Features" })).toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  expect(await visibleScenes(page)).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Theme", exact: true }).click();
  const menu = page.getByRole("menu");
  await expect(menu.getByText("Card Illustrations", { exact: true })).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: /^Default Setting/ })).toContainText("✓");
  await menu.getByRole("menuitem", { name: /^Hide/ }).click();

  await expect.poll(() => prefs.writes).toEqual([{ illustrations: "hide" }]);
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "hide");
  expect(await visibleScenes(page)).toBe(0);
  const cookie = (await page.context().cookies()).find((c) => c.name === HINT);
  expect(JSON.parse(decodeURIComponent(cookie.value)).il, "the hint carries the choice to the next first byte").toBe("hide");

  await page.reload();
  await expect(page.getByRole("heading", { name: "Platform Features" })).toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  expect(await visibleScenes(page), "the stored choice holds across a reload").toBe(0);

  await page.getByRole("button", { name: "Theme", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: /^Hide/ })).toContainText("✓");
  await page.getByRole("menuitem", { name: /^Default Setting/ }).click();
  await expect.poll(() => prefs.writes).toEqual([{ illustrations: "hide" }, { illustrations: null }]);
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "show");
  expect(await visibleScenes(page)).toBeGreaterThan(0);
  expect(prefs.foreignActor).toEqual([]);
  await expectCleanHarness(api);
});

test("partner: a KPI tile keeps its scene only while there is room beside the number", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });
  await installPreferences(page, "partner");

  await page.goto("/inventory/monitoring");
  const tiles = page.locator(".illo-kpi");
  await expect(tiles).toHaveCount(4, { timeout: 20_000 });
  const report = await tiles.evaluateAll((nodes) => nodes.map((tile) => {
    const scene = tile.querySelector('[data-slot="card-illustration"]');
    const value = tile.querySelector(".text-2xl");
    return {
      width: Math.round(tile.getBoundingClientRect().width),
      shown: getComputedStyle(scene).display !== "none",
      clipped: value ? value.scrollWidth > value.clientWidth + 1 : false,
    };
  }));
  for (const tile of report) {
    expect(tile.shown, `a ${tile.width}px tile`).toBe(tile.width >= 240);
    expect(tile.clipped, "the number is never clipped").toBe(false);
  }
  if (isMobile(testInfo)) expect(report.every((tile) => !tile.shown), "two tiles a row on a phone leave no room").toBe(true);
  else expect(report.every((tile) => tile.shown), "four tiles a row at 1440px hold a scene").toBe(true);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "monitoring-kpi");
  await expectCleanHarness(api);
});

test("admin: a clickable card nudges its scene, and reduced motion keeps it still", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "hover is a pointer affordance");
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin");
  await installPreferences(page, "admin");

  await page.goto("/technologies");
  const card = page.getByRole("button", { name: "View CPU Resources details" });
  await expect(card).toBeVisible({ timeout: 20_000 });
  const scene = card.locator(`${SCENE} .illo-scene`);
  await card.hover();
  await expect.poll(() => scene.evaluate((node) => getComputedStyle(node).transform)).toBe("matrix(1, 0, 0, 1, 0, -2)");

  await page.mouse.move(0, 0);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await card.hover();
  await page.waitForTimeout(300);
  expect(await scene.evaluate((node) => getComputedStyle(node).transform)).toBe("none");
  await expectCleanHarness(api);
});

test("the sign-in error page names the situation with a scene, and the classic icon when hidden", async ({ page, context }, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/auth/error?error=AccessDenied");
  await expect(page.getByText("Access Denied", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page)).toBe(1);
  await expectDecorative(page);
  await capture(page, testInfo, "auth-error-scene");

  await hint(context, "hide");
  await page.reload();
  await expect(page.getByText("Access Denied", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page)).toBe(0);
  await expect(page.locator(`${FALLBACK} svg`)).toBeVisible();
  expect(errors).toEqual([]);
});
