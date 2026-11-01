const { test, expect } = require("@playwright/test");
const { BASE_URL, authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// An OrcaHub buyer opens /terminal/<id>?instance&host&project in a new tab. The terminal used to
// refuse a stale ticket before accepting the socket, which the browser saw as a bare 1006, and a
// session the IdP had ended still read as signed in, so the tab said "disconnected" and nothing
// recovered. The socket now closes with a code the page acts on (lib/ws-close.ts), and the page
// refreshes, signs in again, or stops and says why (lib/socket-recovery.ts).

const TERMINAL_PATH = "/terminal/e2e?instance=vm-a&host=cluster-a&project=default";
const SESSION_USER = {
  id: "admin@e2e.invalid",
  name: "Admin E2E",
  email: "admin@e2e.invalid",
  image: null,
  groups: ["role_admin"],
  roles: ["role_admin"],
};

async function openTerminal(page, context, { onSocket, session, authOptions } = {}) {
  await authenticate(context, "admin", authOptions);
  const state = await installApiFixtures(page, "admin");
  state.sessionReads = [];
  state.signIns = [];
  await page.route("**/api/v1/console/**", (route) =>
    route.fulfill({
      json: {
        instance_name: "vm-a",
        instance_type: "container",
        host: "cluster-a",
        project: "default",
        text_console: true,
        graphics_console: false,
        state: "Running",
        sessions: [],
      },
    }),
  );
  // The session the page reads and the forced refresh it asks for (update({ forceRefresh })).
  await page.route("**/api/auth/session", async (route) => {
    const request = route.request();
    state.sessionReads.push({ method: request.method(), body: request.postData() || "" });
    if (session) {
      await route.fulfill({ json: session(request) });
      return;
    }
    // The real session, read with a plain GET under the same cookies, answers the forced refresh.
    const real = await page.request.get(`${BASE_URL}/api/auth/session`);
    await route.fulfill({ response: real });
  });
  // Where a sign-in would leave for the IdP; the test stops it here and keeps what it asked for.
  await page.route("**/api/auth/signin/authentik**", async (route) => {
    state.signIns.push(new URLSearchParams(route.request().postData() || ""));
    await route.fulfill({ json: { url: `${BASE_URL}/e2e-idp` } });
  });
  await page.route("**/e2e-idp", (route) => route.fulfill({ contentType: "text/html", body: "<p>Identity provider</p>" }));
  const sockets = [];
  await page.routeWebSocket(/\/api\/v1\/terminal\/ws\//, (socket) => {
    sockets.push(socket);
    onSocket?.(socket, sockets.length);
  });
  return { state, sockets };
}

const ready = (socket) =>
  socket.onMessage(() => {
    socket.send(JSON.stringify({ type: "session", session_id: "s-1" }));
    socket.send(JSON.stringify({ type: "ready" }));
  });

const mints = (state) => state.requests.filter((r) => r.method === "POST" && r.path === "/ws-tickets").length;
const forcedRefreshes = (state) => state.sessionReads.filter((r) => r.method === "POST" && r.body.includes("forceRefresh")).length;

test("a 4401 refreshes the session, mints a new ticket and connects", async ({ page, context }) => {
  const { state, sockets } = await openTerminal(page, context, {
    onSocket: (socket, n) => {
      if (n === 1) socket.onMessage(() => socket.close({ code: 4401, reason: "ticket_invalid" }));
      else ready(socket);
    },
  });
  await page.goto(TERMINAL_PATH, { waitUntil: "domcontentloaded" });

  await expect(page.getByText("Connected", { exact: true }).first()).toBeVisible({ timeout: 15_000 });
  expect(sockets).toHaveLength(2);
  expect(mints(state)).toBe(2);
  expect(forcedRefreshes(state)).toBe(1);
  expect(state.signIns).toHaveLength(0);
});

for (const [code, reason, message] of [
  [4403, "forbidden", "You do not have access to this instance."],
  [4428, "break_glass_required", "Break-glass access is required to open a session on this Cluster."],
  [4423, "migration_fenced", "Migration Center is moving this instance. Try again when the move finishes."],
]) {
  test(`a ${code} close says why and never reconnects`, async ({ page, context }) => {
    await page.clock.install();
    const { state, sockets } = await openTerminal(page, context, {
      onSocket: (socket) => socket.onMessage(() => socket.close({ code, reason })),
    });
    await page.goto(TERMINAL_PATH, { waitUntil: "domcontentloaded" });

    await expect(page.getByText(message).first()).toBeVisible({ timeout: 15_000 });
    await page.clock.runFor(120_000);
    expect(sockets).toHaveLength(1);
    expect(mints(state)).toBe(1);
    expect(forcedRefreshes(state)).toBe(0);
  });
}

test("a session that drops after ready reconnects at most five times", async ({ page, context }) => {
  await page.clock.install();
  const { sockets } = await openTerminal(page, context, {
    onSocket: (socket) =>
      socket.onMessage(() => {
        socket.send(JSON.stringify({ type: "ready" }));
        socket.close({ code: 1011, reason: "Lost the connection to the shell" });
      }),
  });
  await page.goto(TERMINAL_PATH, { waitUntil: "domcontentloaded" });

  await expect.poll(() => sockets.length, { timeout: 15_000 }).toBe(1);
  await expect(page.getByText(/Connection lost\. Reconnecting in 1 s \(attempt 1 of 5\)\./).first()).toBeVisible();
  // The waits add up to 31 s give or take 20 %; walk the clock until the page gives up.
  await expect
    .poll(async () => {
      await page.clock.runFor(5_000);
      return page.getByText("Could not reconnect. Select Reconnect to try again.").first().isVisible();
    }, { timeout: 30_000 })
    .toBe(true);
  await page.clock.runFor(120_000);
  expect(sockets, "the first session and five reconnects, no more").toHaveLength(6);
});

test("a deep link whose session the IdP ended signs in again and comes back to it", async ({ page, context }) => {
  // Access token nearly expired: the server does not hydrate it, so the page reads the session.
  const { state, sockets } = await openTerminal(page, context, {
    authOptions: { accessExpiresIn: 30 },
    session: () => ({
      user: SESSION_USER,
      expires: new Date(Date.now() + 3_600_000).toISOString(),
      accessToken: "e2e-admin-access-token",
      error: "RefreshTokenRevoked",
    }),
  });
  await page.goto(TERMINAL_PATH, { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/e2e-idp$/, { timeout: 15_000 });
  expect(state.signIns).toHaveLength(1);
  expect(state.signIns[0].get("callbackUrl")).toBe(TERMINAL_PATH);
  expect(sockets).toHaveLength(0);
});

test("a second automatic sign-in within a minute waits for the operator", async ({ page, context }) => {
  // The tab already tried once (orcastra:reauth:last); another try would bounce between hosts.
  await context.addInitScript(() => window.sessionStorage.setItem("orcastra:reauth:last", String(Date.now())));
  const { state } = await openTerminal(page, context, {
    authOptions: { accessExpiresIn: 30 },
    session: () => ({
      user: SESSION_USER,
      expires: new Date(Date.now() + 3_600_000).toISOString(),
      accessToken: "e2e-admin-access-token",
      error: "RefreshTokenRevoked",
    }),
    onSocket: (socket) => socket.onMessage(() => socket.close({ code: 4401, reason: "session_ended" })),
  });
  await page.goto(TERMINAL_PATH, { waitUntil: "domcontentloaded" });

  await expect(page.getByText("Your session ended. Sign in again to continue.")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Your session ended. Sign in again to open this terminal.").first()).toBeVisible({ timeout: 15_000 });
  expect(state.signIns).toHaveLength(0);

  await page.getByRole("button", { name: "Sign In Again" }).first().click();
  await expect(page).toHaveURL(/\/e2e-idp$/, { timeout: 15_000 });
  expect(state.signIns).toHaveLength(1);
  expect(state.signIns[0].get("callbackUrl")).toBe(TERMINAL_PATH);
});
