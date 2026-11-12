// Each page response carries its own nonce: the enforced script-src names it, the layout's inline
// scripts and Next's scripts carry it, and a second load gets another. The report endpoint takes
// only what a browser sends and refuses the rest. (Every other spec fails on an enforced
// violation through expectCleanHarness.)
const { test, expect } = require("@playwright/test");
const { BASE_URL, authenticate, installApiFixtures } = require("./support/fixtures.cjs");
const { installSettingsFixtures } = require("./support/settings-fixtures.cjs");

function scriptNonce(policy) {
  return /script-src [^;]*'nonce-([A-Za-z0-9+/=]+)'/.exec(policy ?? "")?.[1] ?? null;
}

for (const path of ["/inventory/iso-images", "/settings/integrations"]) {
  test(`${path} enforces the nonce its scripts carry, a new one each load`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-light", "headers are the same on every project");
    await authenticate(page.context(), "admin");
    const settings = path.startsWith("/settings");
    const api = settings ? await installSettingsFixtures(page, "admin") : await installApiFixtures(page, "admin");
    const nonces = [];
    for (let load = 0; load < 2; load += 1) {
      const response = await page.goto(path, { waitUntil: "domcontentloaded" });
      const policies = (await response.headersArray()).filter((h) => h.name.toLowerCase() === "content-security-policy").map((h) => h.value);
      const enforced = policies.map(scriptNonce).filter(Boolean);
      expect(enforced, `${path}: one enforced script-src with a nonce`).toHaveLength(1);
      const reportOnly = (await response.headersArray()).find((h) => h.name.toLowerCase() === "content-security-policy-report-only")?.value;
      expect(scriptNonce(reportOnly), "the report-only policy trusts the same nonce").toBe(enforced[0]);
      const carried = await page.evaluate(() => Array.from(document.scripts, (s) => ({ nonce: s.nonce, inline: !s.src })));
      expect(carried.length).toBeGreaterThan(0);
      for (const script of carried) expect(script.nonce, "every server-rendered script carries the nonce").toBe(enforced[0]);
      expect(carried.some((s) => s.inline), "the layout's inline scripts are among them").toBe(true);
      await expect(page.locator("main")).toBeVisible();
      nonces.push(enforced[0]);
    }
    expect(nonces[0]).not.toBe(nonces[1]);
    // This spec is about headers and nonces; the page specs own their API coverage.
    const state = settings ? api.base : api;
    expect(state.cspViolations, "what the enforced policy blocked").toEqual([]);
    expect(state.pageErrors, "uncaught browser errors").toEqual([]);
  });
}

test("a page middleware does not match runs no script", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "headers are the same on every project");
  await authenticate(page.context(), "admin");
  for (const path of ["/api/not-a-route", "/not-a-file.json", "/spice-html5/not-a-file"]) {
    const response = await page.goto(path, { waitUntil: "domcontentloaded" });
    const policy = (await response.headersArray()).filter((h) => h.name.toLowerCase() === "content-security-policy").map((h) => h.value).join(" | ");
    expect(policy, `${path}: no nonce policy reaches it, so it must forbid script`).toContain("script-src 'none'");
    // The app's 404 page is in the HTML; none of its scripts may have run.
    const ran = await page.evaluate(() => Boolean(window.next || window.__next_f?.length > 0 && document.querySelector("[data-nextjs-scroll-focus-boundary]")));
    expect(ran, `${path}: a script ran`).toBe(false);
  }
});

test("the report endpoint takes a browser's report and refuses anything else", async ({ playwright }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "server behaviour, once");
  const client = await playwright.request.newContext({ baseURL: BASE_URL });
  try {
    const report = { "csp-report": { "document-uri": `${BASE_URL}/inventory?x=1`, "effective-directive": "script-src-elem", "blocked-uri": "inline", disposition: "enforce" } };
    const ok = await client.post("/api/csp-report", { headers: { "content-type": "application/csp-report" }, data: JSON.stringify(report) });
    expect(ok.status()).toBe(204);
    const modern = await client.post("/api/csp-report", { headers: { "content-type": "application/reports+json" }, data: JSON.stringify([{ type: "csp-violation", body: { effectiveDirective: "img-src" } }]) });
    expect(modern.status()).toBe(204);
    const json = await client.post("/api/csp-report", { headers: { "content-type": "application/json" }, data: JSON.stringify(report) });
    expect(json.status(), "only the two report types").toBe(415);
    const big = await client.post("/api/csp-report", { headers: { "content-type": "application/csp-report" }, data: "x".repeat(9 * 1024) });
    expect(big.status(), "a body past 8 KB").toBe(413);
    const read = await client.get("/api/csp-report");
    expect(read.status(), "reports are POSTed").toBe(405);
  } finally {
    await client.dispose();
  }
});
