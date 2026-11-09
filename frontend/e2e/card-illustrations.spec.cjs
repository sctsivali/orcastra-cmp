const { test, expect } = require("@playwright/test");
const {
  BASE_URL,
  authenticate,
  capture,
  expectCleanHarness,
  expectNoHorizontalOverflow,
  installApiFixtures,
} = require("./support/fixtures.cjs");
const { installSettingsFixtures, expectCleanSettingsHarness } = require("./support/settings-fixtures.cjs");

// Card illustrations (components/illustrations): decoration that names a card's subject. These
// specs hold what a reader cannot see in review: a scene is hidden from assistive technology and
// takes no focus, fills its slot without ever covering text, stays inside its card, paints its
// theme's tokens, does not move after it lands, and disappears from the first byte of HTML when the
// organization hides it, with the card's classic icon back and no gap. The choice lives only in
// Settings > Personalization (owner decision 2026-11-09); the theme menu offers none.

const SCENE = '[data-slot="card-illustration"]';
const FALLBACK = '[data-slot="illustration-fallback"]';
const HINT = "orcastra-personalization";
// The hero face, read from illustrations.css: #3A2482 on light, #BBABFF on dark.
const HERO_FACE = { light: "rgb(58, 36, 130)", dark: "rgb(187, 171, 255)" };

const themeOf = (testInfo) => (testInfo.project.name.includes("dark") ? "dark" : "light");
const isMobile = (testInfo) => testInfo.project.name.startsWith("mobile");

/**
 * The preference reads, and the organization's branding row with its write, answered from state the
 * way services/branding applies them: a PUT replaces the organization's settings and moves the
 * version, so the sidebar and the hint re-read. Registered after installApiFixtures, so these routes
 * answer first.
 */
async function installBranding(page, role, { organization = {} } = {}) {
  const state = { organization: { ...organization }, revision: 0, writes: [], foreignActor: [] };
  const bearer = `Bearer e2e-${role}-access-token`;
  const version = () => `e2e-illustrations-${state.revision}`;
  const orgRow = () => ({
    organization_id: 17,
    settings: state.organization,
    updated_by: null,
    updated_at: null,
    effective: state.organization,
    inherits_from: "default",
    provider: null,
    followers_count: 0,
    version: version(),
  });
  const watch = (route) => {
    if (route.request().headers().authorization !== bearer) state.foreignActor.push(route.request().url());
  };
  await page.route("**/api/v1/user-preferences", (route) => {
    watch(route);
    return route.request().method() === "GET"
      ? route.fulfill({ json: { theme: "system", lastModified: null } })
      : route.fallback();
  });
  await page.route("**/api/v1/organizations/my-preferences", (route) => {
    watch(route);
    return route.fulfill({ json: { organization_id: 17, subject: "organization", source: "organization", settings: state.organization, updated_at: null, version: version() } });
  });
  await page.route("**/api/v1/organizations/my-preferences/version", (route) => {
    watch(route);
    return route.fulfill({ json: { organization_id: 17, updated_at: null, version: version() } });
  });
  await page.route(/\/api\/v1\/organizations$/, (route) => {
    watch(route);
    return route.request().method() === "GET"
      ? route.fulfill({ json: [{ id: 17, name: "QA organization", is_member: true }] })
      : route.fallback();
  });
  await page.route("**/api/v1/organizations/17/preferences", (route) => {
    watch(route);
    const method = route.request().method();
    if (method === "GET") return route.fulfill({ json: orgRow() });
    if (method !== "PUT") return route.fallback();
    const body = route.request().postDataJSON();
    state.writes.push(body);
    state.organization = { ...body.settings };
    state.revision += 1;
    return route.fulfill({ json: orgRow() });
  });
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

async function openHome(page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Platform Features" })).toBeVisible({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
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

/**
 * Every visible scene stays inside its card and covers no text in it: each text run's box (a Range
 * over the text node, so a long label is measured where it actually wraps) against the scene's box.
 */
async function expectFitted(page) {
  const problems = await page.locator(SCENE).evaluateAll((nodes) => nodes
    .filter((node) => getComputedStyle(node).display !== "none" && node.getBoundingClientRect().width > 0)
    .flatMap((node) => {
      const card = node.closest(".rounded-xl, .rounded-lg");
      if (!card) return ["a scene outside any card"];
      const s = node.getBoundingClientRect();
      const c = card.getBoundingClientRect();
      const out = [];
      if (s.left < c.left - 1 || s.right > c.right + 1 || s.top < c.top - 1 || s.bottom > c.bottom + 1) {
        out.push(`a ${Math.round(s.width)}px scene leaves its card`);
      }
      const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
      for (let text = walker.nextNode(); text; text = walker.nextNode()) {
        if (!text.textContent.trim() || node.contains(text)) continue;
        const range = document.createRange();
        range.selectNodeContents(text);
        for (const r of range.getClientRects()) {
          const overlap = Math.min(r.right, s.right) - Math.max(r.left, s.left) > 1 && Math.min(r.bottom, s.bottom) - Math.max(r.top, s.top) > 1;
          if (overlap) out.push(`"${text.textContent.trim().slice(0, 30)}" sits under a scene`);
        }
      }
      return out;
    }));
  expect(problems).toEqual([]);
}

/** KPI tiles: shown exactly while the tile is at least 220px, then at least 35% of the tile, numbers unclipped. */
async function expectKpiTiles(page) {
  const tiles = await page.locator(".illo-kpi").evaluateAll((nodes) => nodes.map((tile) => {
    const scene = tile.querySelector('[data-slot="card-illustration"]');
    const width = tile.getBoundingClientRect().width;
    const shown = getComputedStyle(scene).display !== "none";
    const clipped = [...tile.querySelectorAll("h3, p")].some((el) => el.scrollWidth > el.clientWidth + 1);
    return { width: Math.round(width), shown, share: shown ? scene.getBoundingClientRect().width / width : 0, clipped };
  }));
  expect(tiles.length, "the page has KPI tiles").toBeGreaterThan(0);
  for (const tile of tiles) {
    expect(tile.shown, `a ${tile.width}px tile`).toBe(tile.width >= 220);
    if (tile.shown) expect(tile.share, `a ${tile.width}px tile's scene fills its slot`).toBeGreaterThanOrEqual(0.35);
    expect(tile.clipped, `a ${tile.width}px tile clips no number or label`).toBe(false);
  }
  return tiles;
}

/** Feature and summary cards: the scene shows while the card is at least 260px, otherwise the classic icon returns. */
async function expectFitCards(page) {
  const cards = await page.locator(".illo-card").evaluateAll((nodes) => nodes.map((card) => {
    const scene = card.querySelector('[data-slot="card-illustration"]');
    const fallback = card.querySelector('[data-slot="illustration-fallback"]');
    const width = card.getBoundingClientRect().width;
    return {
      width: Math.round(width),
      shown: getComputedStyle(scene).display !== "none",
      icon: fallback ? getComputedStyle(fallback).display !== "none" : null,
      share: scene.getBoundingClientRect().width / width,
    };
  }));
  expect(cards.length, "the page has feature or summary cards").toBeGreaterThan(0);
  for (const card of cards) {
    expect(card.shown, `a ${card.width}px card`).toBe(card.width >= 260);
    if (card.icon !== null) expect(card.icon, `a ${card.width}px card shows its icon in the scene's place`).toBe(!card.shown);
    if (card.shown) expect(card.share, `a ${card.width}px card's scene fills its corner`).toBeGreaterThanOrEqual(0.3);
  }
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

test("admin: Home fills each feature card, the call to action and the totals with a scene that covers no text", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", HOME);
  await answerProjects(page);
  const branding = await installBranding(page, "admin");

  await openHome(page);
  // Six features, the call to action and the four totals.
  expect(await visibleScenes(page)).toBe(11);
  await expectDecorative(page);
  await expectFitted(page);
  await expectKpiTiles(page);
  await expectFitCards(page);
  expect(await heroFill(page), "the hero face paints the theme's token").toBe(HERO_FACE[themeOf(testInfo)]);
  const fallbacks = await page.locator(FALLBACK).evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).display));
  expect(fallbacks, "the classic icons wait behind the scenes").toEqual(Array(6).fill("none"));

  const landed = await sceneBoxes(page);
  await page.waitForTimeout(1500);
  expect(await sceneBoxes(page), "a scene does not move once it has landed").toEqual(landed);

  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "home-scenes");
  expect(branding.writes, "reading never writes").toEqual([]);
  expect(branding.foreignActor).toEqual([]);
  await expectCleanHarness(api);
});

test("admin: across laptop and tablet widths a scene never covers text and a KPI tile keeps its number first", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "the phone width runs in the mobile projects");
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", HOME);
  await answerProjects(page);
  await installBranding(page, "admin");

  for (const width of [1280, 1024, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await openHome(page);
    await expectFitted(page);
    await expectKpiTiles(page);
    await expectFitCards(page);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `home-${width}`);
  }
  await expectCleanHarness(api);
});

test("an organization that hides scenes gets the classic cards from the first byte, with no gap", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  await hint(page.context(), "hide");
  const api = await installApiFixtures(page, "partner", HOME);
  await answerProjects(page);
  await installBranding(page, "partner", { organization: { illustrations: "hide" } });

  const firstByte = await (await page.request.get("/")).text();
  expect(firstByte, "the server renders the choice before any script runs").toContain('data-illustrations="hide"');

  await openHome(page);
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

test("tenant: the theme menu offers no illustrations choice; the organization decides", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "the sidebar is a drawer on a phone; the desktop menu is the same component");
  await authenticate(page.context(), "tenant");
  const api = await installApiFixtures(page, "tenant", HOME);
  await answerProjects(page);
  await installBranding(page, "tenant", { organization: { illustrations: "hide" } });

  await openHome(page);
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "hide");
  await page.getByRole("button", { name: "Theme", exact: true }).click();
  const menu = page.getByRole("menu");
  await expect(menu.getByRole("menuitem")).toHaveText([/Light Mode/, /Dark Mode/, /System/, /Default Theme/]);
  await expect(menu.getByText("Card Illustrations")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expectCleanHarness(api);
});

test("partner: Personalization sets the organization's choice, and the page follows it without a reload", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", HOME);
  await answerProjects(page);
  const branding = await installBranding(page, "partner");

  await page.goto("/settings/personalization");
  const field = page.getByRole("combobox", { name: "Card Illustrations" });
  await expect(field).toBeVisible({ timeout: 20_000 });
  await expect(field).toHaveText("Show");
  await expect(page.getByText("Each person can still choose")).toHaveCount(0);

  await field.click();
  await page.getByRole("option", { name: "Hide" }).click();
  await expect.poll(() => branding.writes.length).toBe(1);
  expect(branding.writes[0].settings.illustrations, "the editor writes the organization's row").toBe("hide");
  await expect(page.locator("html"), "the operator's own organization previews at once").toHaveAttribute("data-illustrations", "hide");
  await capture(page, testInfo, "personalization-hide");

  await openHome(page);
  expect(await visibleScenes(page), "the next page reads the stored choice").toBe(0);

  await page.goto("/settings/personalization");
  await expect(field).toHaveText("Hide", { timeout: 20_000 });
  await field.click();
  await page.getByRole("option", { name: "Show" }).click();
  await expect.poll(() => branding.writes.length).toBe(2);
  expect(branding.writes[1].settings.illustrations).toBe("show");
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "show");
  expect(branding.foreignActor).toEqual([]);
  await expectCleanHarness(api);
});

test("partner: across laptop and tablet widths the Monitoring tiles keep every number readable", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "the phone width runs in the mobile projects");
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });
  await installBranding(page, "partner");

  for (const width of [1280, 1024, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/inventory/monitoring");
    await expect(page.locator(".illo-kpi")).toHaveCount(4, { timeout: 20_000 });
    await page.waitForLoadState("networkidle");
    await expectKpiTiles(page);
    await expectFitted(page);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `monitoring-${width}`);
  }
  await expectCleanHarness(api);
});

test("partner: Monitoring KPI tiles fill their slot while there is room and keep every number readable", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });
  await installBranding(page, "partner");

  await page.goto("/inventory/monitoring");
  await expect(page.locator(".illo-kpi")).toHaveCount(4, { timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  const tiles = await expectKpiTiles(page);
  await expectFitted(page);
  if (isMobile(testInfo)) expect(tiles.every((tile) => !tile.shown), "two tiles a row on a phone leave no room").toBe(true);
  else expect(tiles.every((tile) => tile.shown), "four tiles a row at 1440px hold a scene").toBe(true);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "monitoring-kpi");
  await expectCleanHarness(api);
});

test("admin: a clickable card nudges its scene, and reduced motion keeps it still", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "hover is a pointer affordance");
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin");
  await installBranding(page, "admin");

  await page.goto("/technologies");
  const card = page.getByRole("button", { name: "View CPU Resources details" });
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expectFitted(page);
  await expectFitCards(page);
  const scene = card.locator(`${SCENE} .illo-scene`);
  await card.hover();
  await expect.poll(() => scene.evaluate((node) => getComputedStyle(node).transform)).toBe("matrix(1, 0, 0, 1, 0, -2)");

  await page.mouse.move(0, 0);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await card.hover();
  await page.waitForTimeout(300);
  expect(await scene.evaluate((node) => getComputedStyle(node).transform)).toBe("none");
  await capture(page, testInfo, "technologies");
  await expectCleanHarness(api);
});

test("the sign-in error page names the situation with a scene, and the classic icon when hidden", async ({ page, context }, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/auth/error?error=AccessDenied");
  await expect(page.getByText("Access Denied", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page)).toBe(1);
  await expectDecorative(page);
  await expectFitted(page);
  await capture(page, testInfo, "auth-error-scene");

  await hint(context, "hide");
  await page.reload();
  await expect(page.getByText("Access Denied", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page)).toBe(0);
  await expect(page.locator(`${FALLBACK} svg`)).toBeVisible();
  expect(errors).toEqual([]);
});

test("partner: first-run empty states in Settings draw their scene above the text", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installSettingsFixtures(page, "partner");

  for (const [path, title] of [
    ["/settings/integrations", "No API keys"],
    ["/settings/integrations?section=organizations", "No organizations are synced yet"],
    ["/settings/user-groups?section=policies", "No access policies yet"],
  ]) {
    await page.goto(path);
    const heading = page.getByText(title, { exact: true }).first();
    await expect(heading).toBeVisible({ timeout: 20_000 });
    const scene = page.locator(SCENE).filter({ visible: true });
    await expect(scene, `${title} draws its scene`).toHaveCount(1);
    const [s, h] = [await scene.boundingBox(), await heading.boundingBox()];
    expect(s.y + s.height, `${title}: the scene sits above its heading`).toBeLessThanOrEqual(h.y + 1);
    expect(s.width, `${title}: the empty-state scene is the large size`).toBeGreaterThanOrEqual(190);
    await expectDecorative(page);
    await expectFitted(page);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `empty-${title.replace(/\W+/g, "-").toLowerCase()}`);
  }
  await expectCleanSettingsHarness(api);
});

test("an account with no role sees the access gate with its scene", async ({ page }, testInfo) => {
  await authenticate(page.context(), "tenant", { groups: ["role_unmapped"] });
  await installApiFixtures(page, "tenant");
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "No role assigned yet" })).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page)).toBe(1);
  await expectDecorative(page);
  await expectFitted(page);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "access-gate");
  expect(errors).toEqual([]);
});
