const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// LXD accepts an exec on a VM without its agent and then drops the sockets, so the proxy can say
// "ready" and close a moment later. Each such session used to reset the backoff, which meant one
// exec a second for as long as the page stayed open (2026-10-30, Proxmox and Windows guests).

const CONSOLE_URL =
  "/console/e2e?instance=vm-a&host=cluster-a&project=default&type=virtual-machine&tab=text&continuation=1";

async function openShellPage(page, context, onSocket) {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  await page.route("**/api/v1/console/**", (route) =>
    route.fulfill({
      json: {
        instance_name: "vm-a",
        instance_type: "virtual-machine",
        host: "cluster-a",
        project: "default",
        text_console: true,
        graphics_console: true,
        graphics_type: "spice",
        audio_enabled: false,
        usb_redirect: false,
        state: "Running",
        sessions: [],
      },
    }),
  );
  await page.routeWebSocket(/\/api\/v1\/terminal\/ws\//, onSocket);
  await page.goto(CONSOLE_URL, { waitUntil: "domcontentloaded" });
}

test("a shell that dies right after it opens backs off", async ({ page, context }) => {
  const opened = [];
  await openShellPage(page, context, (socket) => {
    opened.push(Date.now());
    socket.onMessage(() => {
      socket.send(JSON.stringify({ type: "ready" }));
      socket.close({ code: 1011, reason: "Lost the connection to the shell" });
    });
  });

  await expect.poll(() => opened.length, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);

  // The delays are 1 s, then 2 s, each within 20 % (lib/ws-close.ts backoffMs). A reset backoff
  // leaves at most 1.2 s between every pair; the second wait is at least 1.6 s.
  expect(opened[2] - opened[1]).toBeGreaterThanOrEqual(1_500);
});

test("a shell that ran for a while retries from the first delay", async ({ page, context }) => {
  const opened = [];
  let closedAt = 0;
  await openShellPage(page, context, (socket) => {
    opened.push(Date.now());
    socket.onMessage(() => {
      if (opened.length > 1) return;
      socket.send(JSON.stringify({ type: "ready" }));
      setTimeout(() => {
        closedAt = Date.now();
        socket.close({ code: 1011, reason: "Lost the connection to the shell" });
      }, 10_500);
    });
  });

  await expect.poll(() => opened.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(2);

  expect(opened[1] - closedAt).toBeLessThan(1_900);
});

test("a shell the server refuses is not tried again", async ({ page, context }) => {
  const opened = [];
  await openShellPage(page, context, (socket) => {
    opened.push(Date.now());
    socket.onMessage(() => {
      socket.send(JSON.stringify({ type: "error", message: "The VM agent is not running", retryable: false }));
      socket.close({ code: 4404, reason: "The VM agent is not running" });
    });
  });

  await expect.poll(() => opened.length, { timeout: 15_000 }).toBe(1);
  await page.waitForTimeout(3_000);

  expect(opened).toHaveLength(1);
});
