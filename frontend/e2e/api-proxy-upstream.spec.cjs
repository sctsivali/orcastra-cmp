// The /api/v1 proxy speaks for the browser with the session in its cookie. Unlike every other
// spec, these requests are not answered in the browser: they go through `next start` to a fake
// backend that records what reached it, so the proxy's own decision is what is asserted.
const http = require("node:http");
const { test, expect } = require("@playwright/test");
const { BASE_URL, authenticate } = require("./support/fixtures.cjs");

const UPSTREAM_PORT = Number(process.env.ORCASTRA_E2E_UPSTREAM_PORT || 47391);
const ACTOR = "x-orcastra-actor";

let server;
const seen = [];

test.beforeAll(async ({}, testInfo) => {
  // One project owns the fake backend's port; the others would collide on it.
  if (testInfo.project.name !== "desktop-light") return;
  server = http.createServer((req, res) => {
    seen.push({ method: req.method, url: req.url, headers: req.headers });
    req.resume();
    req.on("end", () => {
      res.writeHead(200, {
        "content-type": "application/json",
        "cache-control": "public, max-age=60",
        "access-control-allow-origin": "https://evil.example",
        "access-control-allow-credentials": "true",
      });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  await new Promise((resolve) => server.listen(UPSTREAM_PORT, "127.0.0.1", resolve));
});

test.afterAll(async () => {
  if (server) await new Promise((resolve) => server.close(resolve));
});

test.beforeEach(async ({}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-light", "the fake backend runs once, on desktop-light");
  seen.length = 0;
});

function lastUpstream(path) {
  return [...seen].reverse().find((entry) => entry.url.startsWith(`/api/v1${path}`));
}

test("a page's read and write reach the backend with the session's token and nothing else", async ({ page }) => {
  await authenticate(page.context(), "partner");
  // A page that renders without the app shell, so only the requests below reach the backend.
  await page.goto("/healthz");
  const answers = await page.evaluate(async () => {
    // As the app sends them: the account it acts for, and here a bearer the page chose, which the
    // proxy replaces.
    const actor = { "x-orcastra-actor": encodeURIComponent("partner@e2e.invalid") };
    const read = await fetch("/api/v1/proxy-probe/read", { headers: { ...actor, Authorization: "Bearer page-chosen" } });
    const write = await fetch("/api/v1/proxy-probe/write", {
      method: "POST", headers: { ...actor, "Content-Type": "application/json" }, body: "{}",
    });
    const pick = (r) => ({
      status: r.status, acao: r.headers.get("access-control-allow-origin"),
      cacheControl: r.headers.get("cache-control"), vary: r.headers.get("vary"),
    });
    return { read: pick(read), write: pick(write) };
  });

  for (const path of ["/proxy-probe/read", "/proxy-probe/write"]) {
    const upstream = lastUpstream(path);
    expect(upstream, `${path} reached the backend`).toBeTruthy();
    expect(upstream.headers.authorization).toBe("Bearer e2e-partner-access-token");
    expect(upstream.headers["x-orcastra-proxy-auth"], "the backend may key this account's rate limit").toBe("session");
    expect(upstream.headers.cookie, "the refresh token in the cookie stays at the proxy").toBeUndefined();
    expect(upstream.headers.origin).toBeUndefined();
  }
  for (const answer of [answers.read, answers.write]) {
    expect(answer.status).toBe(200);
    expect(answer.acao, "no upstream CORS on a same-origin answer").toBeNull();
    expect(answer.cacheControl).toBe("private, max-age=60");
    expect(answer.vary).toContain("Cookie");
  }
});

test("a write another site starts is refused before the backend", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const response = await page.request.post(`${BASE_URL}/api/v1/proxy-probe/forged`, {
    headers: { origin: "https://evil.example", "content-type": "application/json" },
    data: {},
  });
  expect(response.status()).toBe(403);
  expect(response.headers()["x-orcastra-security-action"]).toBe("cross-site");
  expect(lastUpstream("/proxy-probe/forged")).toBeUndefined();
});

test("a script with the session cookie that names its account is the session, as the QA harness is", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const response = await page.request.post(`${BASE_URL}/api/v1/proxy-probe/script`, {
    headers: {
      authorization: "Bearer undefined", "content-type": "application/json",
      [ACTOR]: encodeURIComponent("admin@e2e.invalid"),
    },
    data: {},
  });
  expect(response.status()).toBe(200);
  expect(lastUpstream("/proxy-probe/script").headers.authorization).toBe("Bearer e2e-admin-access-token");
});

test("a navigation never gets the session, wherever the link was", async ({ page }) => {
  await authenticate(page.context(), "partner");
  // A link opened in the tab: no actor header, so no session, on http as on https.
  await page.goto("/api/v1/proxy-probe/navigated");
  const upstream = lastUpstream("/proxy-probe/navigated");
  expect(upstream, "the navigation reached the backend").toBeTruthy();
  expect(upstream.headers.authorization, "a navigation carries no session").toBeUndefined();
  expect(upstream.headers["x-orcastra-proxy-auth"]).toBeUndefined();
  expect(upstream.headers.cookie).toBeUndefined();
});

test("an integration's own key passes through when no session cookie is sent", async ({ playwright }) => {
  const client = await playwright.request.newContext();
  try {
    const response = await client.get(`${BASE_URL}/api/v1/proxy-probe/keyed`, {
      // A caller claiming the session mark must not get a rate-limit bucket per token it invents.
      headers: { authorization: "Bearer key-id:key-secret", "x-orcastra-proxy-auth": "session" },
    });
    expect(response.status()).toBe(200);
    const upstream = lastUpstream("/proxy-probe/keyed");
    expect(upstream.headers.authorization).toBe("Bearer key-id:key-secret");
    expect(upstream.headers["x-orcastra-proxy-auth"], "the caller's mark is dropped").toBeUndefined();
  } finally {
    await client.dispose();
  }
});

test("a tab acting for another account is told so and reaches nothing", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const mismatch = await page.request.get(`${BASE_URL}/api/v1/proxy-probe/actor`, {
    headers: { [ACTOR]: encodeURIComponent("admin@e2e.invalid") },
  });
  expect(mismatch.status()).toBe(401);
  expect(mismatch.headers()["x-orcastra-auth"]).toBe("actor-changed");
  expect(lastUpstream("/proxy-probe/actor")).toBeUndefined();

  const same = await page.request.get(`${BASE_URL}/api/v1/proxy-probe/actor-ok`, {
    headers: { [ACTOR]: encodeURIComponent("partner@e2e.invalid") },
  });
  expect(same.status()).toBe(200);
  expect(lastUpstream("/proxy-probe/actor-ok").headers[ACTOR], "the hint is not forwarded").toBeUndefined();
});

test("a session the identity provider ended is over at the proxy", async ({ page }) => {
  await authenticate(page.context(), "partner", { tokenFields: { error: "RefreshTokenRevoked" } });
  const response = await page.request.get(`${BASE_URL}/api/v1/proxy-probe/ended`, {
    headers: { [ACTOR]: encodeURIComponent("partner@e2e.invalid") },
  });
  expect(response.status()).toBe(401);
  expect(response.headers()["x-orcastra-auth"]).toBe("session-ended");
  expect(lastUpstream("/proxy-probe/ended")).toBeUndefined();
});
