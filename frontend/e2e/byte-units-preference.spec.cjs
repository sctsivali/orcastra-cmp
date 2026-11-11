const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// Settings > General: each account picks how sizes read, decimal (GB) or binary (GiB). The choice
// is saved on the server, painted from the first byte through the hint cookie, and a failed save
// never leaves the screen claiming a choice that was not stored.

const HINT = "orcastra-personalization";

async function withPreferences(page, { initial = {}, failPut = false, readDelayMs = 0 } = {}) {
  const state = { personal: { ...initial }, puts: [] };
  await page.route(/\/api\/v1\/user-preferences(?:\?.*)?$/, async (route) => {
    const request = route.request();
    if (request.method() === "PUT") {
      const body = request.postDataJSON();
      state.puts.push(body);
      if (failPut) return route.fulfill({ status: 500, json: { detail: "Could not save preferences" } });
      if ("byteUnits" in body) state.personal.byteUnits = body.byteUnits ?? undefined;
      return route.fulfill({ json: { byteUnits: state.personal.byteUnits ?? "decimal" } });
    }
    if (readDelayMs) await new Promise((resolve) => setTimeout(resolve, readDelayMs));
    return route.fulfill({ json: state.personal });
  });
  return state;
}

const radio = (page, name) => page.getByRole("radiogroup", { name: "Units" }).getByRole("radio", { name: new RegExp(`^${name}`) });

for (const role of ["admin", "partner", "tenant"]) {
  test(`${role} finds Units in Settings > General with decimal chosen`, async ({ page, context }) => {
    await authenticate(context, role);
    await installApiFixtures(page, role);
    await withPreferences(page);
    await page.goto("/settings/general");
    await expect(page.getByRole("radiogroup", { name: "Units" })).toBeVisible();
    await expect(radio(page, "Decimal")).toBeChecked();
    await expect(radio(page, "Binary")).not.toBeChecked();
    await expect(page.getByText("1 GB = 1,000,000,000 bytes")).toBeVisible();
    await expect(page.getByText("1 GiB = 1,073,741,824 bytes")).toBeVisible();
  });
}

test("choosing binary saves only that choice and every size follows", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const prefs = await withPreferences(page);
  await page.goto("/settings/general");

  await radio(page, "Binary").check();
  await expect.poll(() => prefs.puts.length).toBe(1);
  expect(prefs.puts[0]).toEqual({ byteUnits: "binary" });
  await expect(radio(page, "Binary")).toBeChecked();

  const hint = (await context.cookies()).find((cookie) => cookie.name === HINT);
  expect(JSON.parse(decodeURIComponent(hint.value)).bu).toBe("binary");

  await page.goto("/inventory/instances");
  await expect(page.getByRole("row", { name: /tenant-owned-vm/ })).toContainText("80 GiB");
});

test("the keyboard moves the choice like any radio group", async ({ page, context }) => {
  await authenticate(context, "partner");
  await installApiFixtures(page, "partner");
  const prefs = await withPreferences(page);
  await page.goto("/settings/general");
  await radio(page, "Decimal").focus();
  await page.keyboard.press("ArrowRight");
  await expect(radio(page, "Binary")).toBeChecked();
  await expect.poll(() => prefs.puts.at(-1)).toEqual({ byteUnits: "binary" });
});

test("a failed save puts the choice back and says so", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  const prefs = await withPreferences(page, { failPut: true });
  await page.goto("/settings/general");

  await radio(page, "Binary").check();
  await expect.poll(() => prefs.puts.length).toBe(1);
  await expect(page.getByText("Could not save units").first()).toBeVisible();
  await expect(radio(page, "Decimal")).toBeChecked();
});

test("the hint paints the account's units before its preferences arrive", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  // The saved choice answers late: what shows before it is the hint cookie's.
  await withPreferences(page, { initial: { byteUnits: "binary" }, readDelayMs: 8000 });
  const origin = new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025");
  await context.addCookies([{
    name: HINT, value: encodeURIComponent(JSON.stringify({ bu: "binary" })), domain: origin.hostname, path: "/",
  }]);
  const problems = [];
  page.on("console", (message) => {
    if (/hydrat|did not match|server rendered/i.test(message.text())) problems.push(message.text());
  });

  const started = Date.now();
  await page.goto("/settings/general");
  await expect(radio(page, "Binary")).toBeChecked({ timeout: 7000 });
  expect(Date.now() - started).toBeLessThan(8000);
  expect(problems).toEqual([]);
});

for (const tampered of ['{"bu":"<img src=x onerror=alert(1)>"}', '{"bu":["binary"]}', "%7Bnot-json", '{"bu":"BINARY"}']) {
  test(`a tampered hint ${JSON.stringify(tampered).slice(0, 24)} reads decimal`, async ({ page, context }) => {
    await authenticate(context, "admin");
    await installApiFixtures(page, "admin");
    await withPreferences(page);
    const origin = new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025");
    await context.addCookies([{
      name: HINT, value: tampered.startsWith("%") ? tampered : encodeURIComponent(tampered), domain: origin.hostname, path: "/",
    }]);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/settings/general");
    await expect(radio(page, "Decimal")).toBeChecked();
    expect(errors).toEqual([]);
  });
}
