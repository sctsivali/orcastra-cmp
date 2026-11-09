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
// takes no focus, sits in its own slot inside the card's padding without ever covering text, lines
// up with the scenes beside it at one weight, paints its theme's tokens, does not move after it
// lands, and disappears from the first byte of HTML when the organization hides it, with the card's
// classic icon back and no gap. The choice lives only in Settings > Personalization and is off until
// an organization or the deployment turns it on (owner decisions 2026-11-09); the theme menu offers
// none.

const SCENE = '[data-slot="card-illustration"]';
const FALLBACK = '[data-slot="illustration-fallback"]';
const HINT = "orcastra-personalization";
// The hero face, read from illustrations.css: #46309A on light, #B7A6FF on dark.
const HERO_FACE = { light: "rgb(70, 48, 154)", dark: "rgb(183, 166, 255)" };

const themeOf = (testInfo) => (testInfo.project.name.includes("dark") ? "dark" : "light");
const isMobile = (testInfo) => testInfo.project.name.startsWith("mobile");
// Scenes are off by default, so a spec that looks at them has the organization turn them on.
const SHOWN = { organization: { illustrations: "show" } };

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
 * Every visible scene sits in its own slot at least 12px inside its card (no bleed into the edge) and
 * covers no text in it: each text run's box (a Range over the text node, so a long label is measured
 * where it actually wraps) against the scene's box.
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
      const inset = Math.min(s.left - c.left, c.right - s.right, s.top - c.top, c.bottom - s.bottom);
      if (inset < 12) out.push(`a ${Math.round(s.width)}px scene is ${Math.round(inset)}px from its card's edge`);
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

/**
 * Cards of one kind in one row hold their scenes alike: the slot at the same offset from the card's
 * top and right edge, the objects standing on one ground line, and every silhouette at a similar
 * weight. The weight is the share of the slot the objects paint (contact shadows left out), read by
 * drawing the scene with its computed paint onto a canvas.
 */
async function expectRows(page, kind) {
  const cards = await page.locator(kind).evaluateAll(async (nodes) => {
    const paint = async (svg) => {
      const clone = svg.cloneNode(true);
      const from = svg.querySelectorAll("*");
      clone.querySelectorAll("*").forEach((el, i) => {
        const cs = getComputedStyle(from[i]);
        el.setAttribute("style", `display:${cs.display};fill:${cs.fill};stroke:${cs.stroke};stroke-width:${cs.strokeWidth};stroke-dasharray:${cs.strokeDasharray};vector-effect:non-scaling-stroke`);
      });
      clone.querySelectorAll(".illo-shadow").forEach((el) => el.remove());
      const r = svg.getBoundingClientRect();
      const [w, h] = [Math.round(r.width), Math.round(r.height)];
      clone.removeAttribute("class");
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      clone.setAttribute("width", String(w));
      clone.setAttribute("height", String(h));
      const img = new Image();
      await new Promise((resolve, reject) => {
        img.onload = resolve;
        img.onerror = reject;
        img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(clone))}`;
      });
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0);
      const data = ctx.getImageData(0, 0, w, h).data;
      let painted = 0;
      for (let i = 3; i < data.length; i += 4) if (data[i] > 40) painted += 1;
      return painted / (w * h);
    };
    const out = [];
    for (const card of nodes) {
      const svg = card.querySelector('[data-slot="card-illustration"]');
      if (!svg || getComputedStyle(svg).display === "none") continue;
      const c = card.getBoundingClientRect();
      const s = svg.getBoundingClientRect();
      let ground = -Infinity;
      for (const shape of svg.querySelectorAll("polygon, polyline, path, ellipse, circle")) {
        if (shape.closest(".illo-shadow") || getComputedStyle(shape).display === "none") continue;
        ground = Math.max(ground, shape.getBoundingClientRect().bottom);
      }
      out.push({ top: Math.round(c.top), slotTop: s.top - c.top, gutter: c.right - s.right, ground: ground - c.top, weight: await paint(svg) });
    }
    return out;
  });
  const rows = new Map();
  for (const card of cards) rows.set(card.top, [...(rows.get(card.top) ?? []), card]);
  for (const row of [...rows.values()].filter((r) => r.length > 1)) {
    const [first] = row;
    const weights = row.map((card) => card.weight).sort((a, b) => a - b);
    const median = weights[Math.floor(weights.length / 2)];
    for (const card of row) {
      expect(Math.abs(card.slotTop - first.slotTop), `${kind}: slots share a top line`).toBeLessThanOrEqual(1);
      expect(Math.abs(card.gutter - first.gutter), `${kind}: slots share a right gutter`).toBeLessThanOrEqual(1);
      expect(Math.abs(card.ground - first.ground), `${kind}: objects stand on one ground line`).toBeLessThanOrEqual(2);
      expect(card.weight / median, `${kind}: a silhouette weighs about as much as its neighbours`).toBeGreaterThanOrEqual(0.65);
      expect(card.weight / median, `${kind}: a silhouette weighs about as much as its neighbours`).toBeLessThanOrEqual(1.35);
    }
  }
  return cards;
}

/**
 * KPI tiles: the small scene in the top right corner, 88px wide, 72px below a 300px tile and gone
 * below 220px; never taller than the figures beside it.
 */
async function expectKpiTiles(page) {
  const tiles = await page.locator(".illo-kpi").evaluateAll((nodes) => nodes.map((tile) => {
    const scene = tile.querySelector('[data-slot="card-illustration"]');
    const width = tile.getBoundingClientRect().width;
    const shown = getComputedStyle(scene).display !== "none";
    const s = scene.getBoundingClientRect();
    // The figures' column, past the icon that waits in the scene's place.
    let column = scene.previousElementSibling;
    while (column && column.matches('[data-slot="illustration-fallback"]')) column = column.previousElementSibling;
    const text = column.getBoundingClientRect();
    const clipped = [...tile.querySelectorAll("h3, p")].some((el) => el.scrollWidth > el.clientWidth + 1);
    // A two-line counter may let the scene run 12px past its figures; a taller one would set the tile's height.
    return { width: Math.round(width), shown, size: [Math.round(s.width), Math.round(s.height)], taller: shown && s.height > text.height + 12, topGap: Math.round(s.top - text.top), clipped };
  }));
  expect(tiles.length, "the page has KPI tiles").toBeGreaterThan(0);
  for (const tile of tiles) {
    expect(tile.shown, `a ${tile.width}px tile`).toBe(tile.width >= 220);
    if (tile.shown) {
      expect(tile.size, `a ${tile.width}px tile's scene size`).toEqual(tile.width >= 300 ? [88, 66] : [72, 54]);
      expect(tile.taller, `a ${tile.width}px tile keeps its height`).toBe(false);
      expect(tile.topGap, `a ${tile.width}px tile's scene starts on the label's line`).toBe(0);
    }
    expect(tile.clipped, `a ${tile.width}px tile clips no number or label`).toBe(false);
  }
  await expectRows(page, ".illo-kpi");
  return tiles;
}

/**
 * Launcher cards (Home features, Settings, footers): the 168px scene beside the text, 96px below a
 * 440px card, and the classic icon in its place below 300px. Data cards (Technologies): the small
 * scene beside the title down to 260px, then the icon tile.
 */
async function expectSideScenes(page, kind) {
  const cards = await page.locator(kind).evaluateAll((nodes) => nodes.map((card) => {
    const scene = card.querySelector('[data-slot="card-illustration"]');
    const fallback = card.querySelector('[data-slot="illustration-fallback"]');
    const s = scene.getBoundingClientRect();
    return {
      width: Math.round(card.getBoundingClientRect().width),
      shown: getComputedStyle(scene).display !== "none",
      icon: fallback ? getComputedStyle(fallback).display !== "none" : null,
      sceneWidth: Math.round(s.width),
      size: scene.dataset.size,
    };
  }));
  expect(cards.length, `the page has ${kind} cards`).toBeGreaterThan(0);
  const launcher = kind === ".illo-launch";
  for (const card of cards) {
    expect(card.shown, `a ${card.width}px card`).toBe(card.width >= (launcher ? 300 : 260));
    if (card.icon !== null) expect(card.icon, `a ${card.width}px card shows its icon in the scene's place`).toBe(!card.shown);
    if (card.shown && launcher && card.width < 440) expect(card.sceneWidth, `a ${card.width}px card steps its scene down`).toBe(96);
    if (card.shown && launcher && card.width >= 440) expect(card.sceneWidth).toBe({ md: 168, lg: 192 }[card.size]);
    if (card.shown && !launcher) expect(card.sceneWidth).toBe(88);
  }
  await expectRows(page, kind);
  return cards;
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
  const branding = await installBranding(page, "admin", SHOWN);

  await openHome(page);
  // Six features, the call to action and the four totals.
  expect(await visibleScenes(page)).toBe(11);
  await expectDecorative(page);
  await expectFitted(page);
  await expectKpiTiles(page);
  await expectSideScenes(page, ".illo-launch");
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
  await installBranding(page, "admin", SHOWN);

  for (const width of [1280, 1024, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await openHome(page);
    await expectFitted(page);
    await expectKpiTiles(page);
    await expectSideScenes(page, ".illo-launch");
    // Launchers two a row only from xl, where the sidebar still leaves each card room for its text.
    const lefts = await page.locator(".illo-launch").evaluateAll((nodes) => new Set(nodes.map((n) => Math.round(n.getBoundingClientRect().left))).size);
    expect(lefts, `${width}px: feature columns`).toBe(width >= 1280 ? 2 : 1);
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

test("partner: Personalization turns the organization's scenes on, and the page follows it without a reload", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", HOME);
  await answerProjects(page);
  const branding = await installBranding(page, "partner");

  await page.goto("/settings/personalization");
  const field = page.getByRole("combobox", { name: "Card Illustrations" });
  await expect(field).toBeVisible({ timeout: 20_000 });
  await expect(field, "nothing set anywhere reads as the default, off").toHaveText("Hide");
  await expect(page.getByText("Off by default.", { exact: false })).toBeVisible();

  await field.click();
  await page.getByRole("option", { name: "Show" }).click();
  await expect.poll(() => branding.writes.length).toBe(1);
  expect(branding.writes[0].settings.illustrations, "the editor writes the organization's row").toBe("show");
  await expect(page.locator("html"), "the operator's own organization previews at once").toHaveAttribute("data-illustrations", "show");
  await capture(page, testInfo, "personalization-show");

  await openHome(page);
  expect(await visibleScenes(page), "the next page reads the stored choice").toBe(11);

  await page.goto("/settings/personalization");
  await expect(field).toHaveText("Show", { timeout: 20_000 });
  await field.click();
  await page.getByRole("option", { name: "Hide" }).click();
  await expect.poll(() => branding.writes.length).toBe(2);
  expect(branding.writes[1].settings.illustrations).toBe("hide");
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "hide");
  expect(branding.foreignActor).toEqual([]);
  await expectCleanHarness(api);
});

test("partner: across laptop and tablet widths the Monitoring tiles keep every number readable", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "the phone width runs in the mobile projects");
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner", { monitoringInstanceList: true });
  await installBranding(page, "partner", SHOWN);

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
  await installBranding(page, "partner", SHOWN);

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
  await installBranding(page, "admin", SHOWN);

  await page.goto("/technologies");
  const card = page.getByRole("button", { name: "View CPU Resources details" });
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expectFitted(page);
  await expectSideScenes(page, ".illo-card");
  // The header is as tall as its text or the small scene, so the figures follow without a band.
  const bands = await page.locator(".illo-card").evaluateAll((nodes) => nodes.map((node) => {
    const description = node.querySelector("button > span > span > span.min-w-0 > span:last-child").getBoundingClientRect();
    const firstRow = node.querySelector("button > span:nth-child(2) > span").getBoundingClientRect();
    return Math.round(firstRow.top - description.bottom);
  }));
  for (const band of bands) expect(band, "description to the first figure").toBeLessThanOrEqual(40);
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

test("the sign-in error page keeps its classic icon by default, and names the situation with a scene once shown", async ({ page, context }, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/auth/error?error=AccessDenied");
  await expect(page.getByText("Access Denied", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page), "nothing chosen anywhere: off").toBe(0);
  await expect(page.locator(`${FALLBACK} svg`)).toBeVisible();

  await hint(context, "show");
  await page.reload();
  await expect(page.getByText("Access Denied", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page)).toBe(1);
  await expectDecorative(page);
  await expectFitted(page);
  await capture(page, testInfo, "auth-error-scene");
  expect(errors).toEqual([]);
});

test("with nothing chosen anywhere the cards stay classic from the first byte", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const api = await installApiFixtures(page, "admin", HOME);
  await answerProjects(page);
  await installBranding(page, "admin");

  const firstByte = await (await page.request.get("/")).text();
  expect(firstByte, "the server renders the default before any script runs").toContain('data-illustrations="hide"');
  await openHome(page);
  await expect(page.locator("html")).toHaveAttribute("data-illustrations", "hide");
  expect(await visibleScenes(page)).toBe(0);
  await expect(page.locator(`${FALLBACK} svg`)).toHaveCount(6);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "home-default");
  await expectCleanHarness(api);
});

test("partner: first-run empty states in Settings draw their scene above the text", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installSettingsFixtures(page, "partner");
  api.organization.settings = { illustrations: "show" };

  for (const [path, title] of [
    ["/settings/integrations", "No API keys"],
    ["/settings/integrations?section=organizations", "No organizations are synced yet"],
    ["/settings/user-groups?section=policies", "No access policies yet"],
  ]) {
    await page.goto(path);
    const heading = page.getByText(title, { exact: true }).first();
    await expect(heading).toBeVisible({ timeout: 20_000 });
    const scene = page.locator("div.text-center").filter({ has: page.getByText(title, { exact: true }) }).locator(SCENE);
    await expect(scene, `${title} draws its scene`).toHaveCount(1);
    const [s, h] = [await scene.boundingBox(), await heading.boundingBox()];
    expect(s.y + s.height, `${title}: the scene sits above its heading`).toBeLessThanOrEqual(h.y + 1);
    expect(s.width, `${title}: the empty-state scene is the medium size`).toBe(168);
    await expectDecorative(page);
    await expectFitted(page);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `empty-${title.replace(/\W+/g, "-").toLowerCase()}`);
  }
  await expectCleanSettingsHarness(api);
});

test("an account with no role sees the access gate with its scene", async ({ page }, testInfo) => {
  await authenticate(page.context(), "tenant", { groups: ["role_unmapped"] });
  // A roleless account reads no branding, so the page keeps what the server rendered: here a
  // hint carrying a deployment that turned scenes on.
  await hint(page.context(), "show");
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

test("partner: the API key counters carry their scenes, and their icons where a tile is too narrow", async ({ page }, testInfo) => {
  test.skip(isMobile(testInfo), "the phone width runs one tile a row; the narrow case is the 768px pass");
  await authenticate(page.context(), "partner");
  const api = await installSettingsFixtures(page, "partner");
  api.organization.settings = { illustrations: "show" };

  for (const width of [1440, 1024, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/settings/integrations");
    await expect(page.getByText("Revoked Keys", { exact: true })).toBeVisible({ timeout: 20_000 });
    const tiles = await expectKpiTiles(page);
    const icons = await page.locator(".illo-kpi").evaluateAll((nodes) => nodes.map((tile) => {
      const icon = tile.querySelector('[data-slot="illustration-fallback"]');
      return icon ? getComputedStyle(icon).display !== "none" : null;
    }));
    tiles.forEach((tile, i) => {
      if (icons[i] !== null) expect(icons[i], `${width}px: a ${tile.width}px tile shows its icon only without a scene`).toBe(!tile.shown);
    });
    await expectFitted(page);
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, `api-key-counters-${width}`);
  }
  await expectCleanSettingsHarness(api);
});

test("partner: an inventory list draws its scene on a first run, never for a filter that matched nothing", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const api = await installApiFixtures(page, "partner");
  await installBranding(page, "partner", SHOWN);
  await page.route(/\/api\/v1\/networks(\?.*)?$/, (route) => route.request().method() === "GET"
    ? route.fulfill({ json: { total: 0, networks: [], nodes: ["cluster-a"], unreachable_nodes: [] } })
    : route.fallback());

  await page.goto("/inventory/networks");
  await expect(page.getByText("No networks yet", { exact: true })).toBeVisible({ timeout: 20_000 });
  expect(await visibleScenes(page), "the first run names what will be here").toBe(1);
  await expectDecorative(page);
  await expectFitted(page);
  await capture(page, testInfo, "networks-first-run");

  await page.goto("/inventory/networks?q=no-such-network");
  await expect(page.getByText("No networks found", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("No network matches the current filters.")).toBeVisible();
  expect(await visibleScenes(page), "a filter that matched nothing keeps the icon").toBe(0);
  await expectCleanHarness(api);
});
