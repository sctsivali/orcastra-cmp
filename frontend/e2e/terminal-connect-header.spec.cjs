const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// The connect header is a box the operator asked to keep, and it used to go crooked: its width came
// from string length, it was written into the stream, and xterm re-wraps old lines on narrowing.
// So it is a DOM box over blank rows now, and these scenarios are the ways that could still break.

const SESSION = "f33efef3-75a2-4cdf-80b4-c27d64222ad0";
const LONG_NAME = "a".repeat(63);

function consoleUrl(name, tab = "text") {
  return `/console/e2e?instance=${name}&host=cluster-a&project=default&type=virtual-machine&tab=${tab}&continuation=1`;
}

// The client sends its size first and a resize after the fit. The server answers the first.
function answerOnce(socket, answer) {
  let answered = false;
  socket.onMessage(() => {
    if (answered) return;
    answered = true;
    answer();
  });
}

async function openTerminal(page, context, { name = "vm-a", tab = "text", onSocket } = {}) {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  await page.route("**/api/v1/console/**", (route) =>
    route.fulfill({
      json: {
        instance_name: name, instance_type: "virtual-machine", host: "cluster-a", project: "default",
        text_console: true, graphics_console: true, graphics_type: "spice", audio_enabled: false,
        usb_redirect: false, state: "Running", sessions: [],
      },
    }),
  );
  const sockets = [];
  const connect = (socket) => {
    sockets.push(socket);
    if (onSocket) return onSocket(socket, sockets.length);
    answerOnce(socket, () => {
      socket.send(JSON.stringify({ type: "session", session_id: SESSION }));
      socket.send(JSON.stringify({ type: "ready" }));
      socket.send(Buffer.from("root@vm-a:~# "));
    });
    return undefined;
  };
  await page.routeWebSocket(/\/api\/v1\/terminal\/ws\//, connect);
  await page.routeWebSocket(/\/api\/v1\/console\/.*ws/, connect);
  await page.goto(consoleUrl(name, tab), { waitUntil: "domcontentloaded" });
  return { sockets };
}

const box = (page) => page.locator('[data-slot="terminal-connect-box"]');

// The terminal is on a canvas, so read its buffer. xterm-setup.ts leaves the handle for this.
function bufferLines(page) {
  return page.evaluate(() => {
    const host = [...document.querySelectorAll("div")].find((el) => el.__xterm);
    if (!host) return [];
    const { buffer } = host.__xterm;
    const lines = [];
    for (let i = 0; i < buffer.active.length; i += 1) lines.push(buffer.active.getLine(i).translateToString(true));
    return lines;
  });
}

const write = (page, data) =>
  page.evaluate((text) => new Promise((resolve) => {
    [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm.write(text, resolve);
  }), data);

// Holds whatever the width: the box is inside the screen, four cells tall, on the first four rows.
async function expectBoxInPlace(page) {
  const fit = await box(page).evaluate((el) => {
    const screen = el.closest(".xterm-screen").getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    const term = [...document.querySelectorAll("div")].find((node) => node.__xterm).__xterm;
    const cell = screen.height / term.rows;
    return { left: rect.left - screen.left, right: screen.right - rect.right, top: rect.top - screen.top, height: rect.height, cell };
  });
  expect(fit.left, "starts on the first column").toBeGreaterThanOrEqual(-1);
  expect(fit.right, "stays inside the terminal").toBeGreaterThanOrEqual(-1);
  expect(fit.top, "starts on the first row").toBeLessThanOrEqual(1);
  expect(Math.abs(fit.height - fit.cell * 4), "four rows tall").toBeLessThanOrEqual(1);
}

test("the box stands above the session lines and the shell", async ({ page, context }) => {
  await openTerminal(page, context);

  await expect(box(page)).toBeVisible();
  await expect(box(page)).toContainText("Orcastra Terminal");
  await expect(box(page)).toContainText("Connecting to vm-a…");
  await expectBoxInPlace(page);

  await expect.poll(async () => (await bufferLines(page)).slice(0, 7)).toEqual([
    "", "", "", "",
    `✓ Session established: ${SESSION}`,
    "✓ Connected",
    "root@vm-a:~# ",
  ]);
});

test("a 63 character name is clipped inside the box, not pushed through it", async ({ page, context }) => {
  await openTerminal(page, context, { name: LONG_NAME });
  await expect(box(page)).toBeVisible();
  await page.setViewportSize({ width: 420, height: 800 });
  await expectBoxInPlace(page);
  const line = box(page).getByText(/Connecting to/);
  await expect(line).toHaveText(`Connecting to ${LONG_NAME}…`);
  expect(await line.evaluate((el) => getComputedStyle(el).textOverflow)).toBe("ellipsis");
});

test("narrowing the window after connecting does not bend the box", async ({ page, context }) => {
  await openTerminal(page, context);
  await expect(box(page)).toBeVisible();
  await expect.poll(async () => (await bufferLines(page))[5]).toBe("✓ Connected");

  for (const width of [900, 480, 320, 700, 1280]) {
    await page.setViewportSize({ width, height: 800 });
    await page.waitForFunction(() => {
      const term = [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm;
      return term.cols > 0;
    });
    await expect(box(page)).toBeVisible();
    await expectBoxInPlace(page);
  }
});

test("zooming the font keeps the box on its four rows", async ({ page, context }) => {
  await openTerminal(page, context);
  await expect(box(page)).toBeVisible();
  for (const size of [8, 22, 32, 14]) {
    await page.evaluate((px) => {
      [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm.options.fontSize = px;
    }, size);
    await expect.poll(() => box(page).evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBe(size);
    await expectBoxInPlace(page);
  }
});

test("lost GPU context falls back without losing the box", async ({ page, context }) => {
  await openTerminal(page, context);
  await expect(box(page)).toBeVisible();
  await page.evaluate(() => {
    for (const canvas of document.querySelectorAll("canvas")) {
      canvas.getContext("webgl2")?.getExtension("WEBGL_lose_context")?.loseContext();
    }
  });
  await write(page, "after the loss\r\n");
  await expect(box(page)).toBeVisible();
  await expectBoxInPlace(page);
});

test("one scrolled line does not take the whole box away", async ({ page, context }) => {
  await openTerminal(page, context);
  await expect.poll(async () => (await bufferLines(page))[5]).toBe("✓ Connected");
  await page.evaluate(() => {
    const term = [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm;
    term.options.scrollback = 1000;
  });
  await write(page, "\r\n".repeat(60));
  await page.evaluate(() => [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm.scrollToTop());
  await expect(box(page)).toBeVisible();
  await page.evaluate(() => [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm.scrollLines(2));
  await expect(box(page), "two rows of it are still on screen").toBeVisible();
  const overlap = await box(page).evaluate((el) => {
    const visible = el.closest(".xterm-screen").parentElement.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    return { top: rect.top, clipTop: visible.top, hit: document.elementFromPoint(rect.left + 5, visible.top - 2)?.closest(".xterm") ?? null };
  });
  expect(overlap.hit, "what sticks out above the terminal must not cover the toolbar").toBeNull();
});

test("a program that writes over those rows removes the box instead of hiding behind it", async ({ page, context }) => {
  await openTerminal(page, context);
  await expect.poll(async () => (await bufferLines(page))[5]).toBe("✓ Connected");
  await expect(box(page)).toBeVisible();
  // What `clear` sends on a serial console: home, then erase below.
  await write(page, "\x1b[H\x1b[Jroot@vm-a:~# ");
  await expect(box(page)).toHaveCount(0);
});

test("a full screen program covers the box and hands it back", async ({ page, context }) => {
  await openTerminal(page, context);
  await expect(box(page)).toBeVisible();
  await write(page, "\x1b[?1049h");
  await expect(box(page)).toBeHidden();
  await write(page, "\x1b[?1049l");
  await expect(box(page)).toBeVisible();
});

test("a reconnect keeps the cursor where it is, so the box survives", async ({ page, context }) => {
  await openTerminal(page, context, {
    onSocket: (socket, count) => {
      answerOnce(socket, () => {
        socket.send(JSON.stringify({ type: "session", session_id: SESSION }));
        socket.send(JSON.stringify({ type: "ready" }));
        if (count === 1) setTimeout(() => socket.close({ code: 1011, reason: "Lost the connection" }), 100);
      });
    },
  });
  await expect.poll(async () => (await bufferLines(page)).filter((line) => line === "✓ Connected").length, { timeout: 15_000 }).toBe(2);
  await expect(box(page)).toBeVisible();
  expect((await bufferLines(page)).slice(0, 4)).toEqual(["", "", "", ""]);
});

test("reconnecting draws no second box", async ({ page, context }) => {
  const { sockets } = await openTerminal(page, context, {
    onSocket: (socket, count) => {
      answerOnce(socket, () => {
        socket.send(JSON.stringify({ type: "session", session_id: SESSION }));
        socket.send(JSON.stringify({ type: "ready" }));
        if (count < 3) setTimeout(() => socket.close({ code: 1011, reason: "Lost the connection" }), 50);
      });
    },
  });
  await expect.poll(() => sockets.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(3);
  await expect(box(page)).toHaveCount(1);
});

test("a new shell after a drop leaves the dead program's screen and mouse mode", async ({ page, context }) => {
  await openTerminal(page, context, {
    onSocket: (socket, count) => {
      answerOnce(socket, () => {
        socket.send(JSON.stringify({ type: "session", session_id: SESSION }));
        socket.send(JSON.stringify({ type: "ready" }));
        if (count === 1) {
          socket.send(Buffer.from("\x1b[?1049h\x1b[?1002h"));
          setTimeout(() => socket.close({ code: 1011, reason: "Lost the connection" }), 200);
        }
      });
    },
  });
  await expect.poll(() => page.evaluate(() => {
    const term = [...document.querySelectorAll("div")].find((el) => el.__xterm)?.__xterm;
    return term ? { buffer: term.buffer.active.type, mouse: term.modes.mouseTrackingMode } : null;
  }), { timeout: 15_000 }).toEqual({ buffer: "normal", mouse: "none" });
});

test("a serial console says when to press Enter, after it connects", async ({ page, context }) => {
  await openTerminal(page, context, { tab: "serial" });
  await expect(box(page)).toContainText("Orcastra Serial Console");
  await expect.poll(async () => (await bufferLines(page)).slice(4, 7).join("|")).toContain("✓ Connected|Press Enter for a prompt.");
});

for (const [label, value] of [
  ["CSI", "%1B%5B2J%1B%5BHroot"],
  ["8-bit CSI", "%C2%9B2Jroot"],
  ["an OSC 8 link", "%1B%5D8%3B%3Bhttp%3A%2F%2Fevil.invalid%07click%1B%5D8%3B%3B%07"],
  ["a bidi override", "vm%E2%80%AEroot"],
  ["a path", "..%2F..%2Fetc"],
  ["4 KB of name", "a".repeat(4096)],
]) {
  test(`a link carrying ${label} is refused before anything reaches the terminal`, async ({ page, context }) => {
    const { sockets } = await openTerminal(page, context, { name: value });
    await expect(page.getByText("This console link is not valid.")).toBeVisible();
    expect(await page.evaluate(() => [...document.querySelectorAll("div")].some((el) => el.__xterm))).toBe(false);
    expect(sockets).toHaveLength(0);
  });
}

test("a close reason cannot drive the terminal", async ({ page, context }) => {
  await openTerminal(page, context, {
    onSocket: (socket) => {
      socket.onMessage(() => {
        socket.send(JSON.stringify({ type: "error", message: "gone\x1b[2J\x1b]8;;http://evil.invalid\x07link\u202etext" }));
        socket.close({ code: 4404, reason: "refused" });
      });
    },
  });
  await expect.poll(async () => (await bufferLines(page)).join("\n")).toContain("gone");
  const text = (await bufferLines(page)).join("\n");
  expect(text).toContain("[2J");
  expect(text).not.toContain("\u202e");
  expect(await page.evaluate(() => {
    const term = [...document.querySelectorAll("div")].find((el) => el.__xterm).__xterm;
    return term.buffer.active.getLine(0).translateToString(true) === "" && term.buffer.active.length > 4;
  }), "the screen was not cleared").toBe(true);
});
