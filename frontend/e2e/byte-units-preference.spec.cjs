const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// Settings > General: each account picks how sizes read, binary (GiB, the default) or decimal (GB).
// The choice is saved on the server, painted from the first byte through the hint cookie, and a
// failed save never leaves the screen claiming a choice that was not stored.

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
      return route.fulfill({ json: { byteUnits: state.personal.byteUnits ?? "binary" } });
    }
    if (readDelayMs) await new Promise((resolve) => setTimeout(resolve, readDelayMs));
    return route.fulfill({ json: state.personal });
  });
  return state;
}

const radio = (page, name) => page.getByRole("radiogroup", { name: "Units" }).getByRole("radio", { name: new RegExp(`^${name}`) });

for (const role of ["admin", "partner", "tenant"]) {
  test(`${role} finds Units in Settings > General with binary chosen by default`, async ({ page, context }) => {
    await authenticate(context, role);
    await installApiFixtures(page, role);
    await withPreferences(page);
    await page.goto("/settings/general");
    await expect(page.getByRole("radiogroup", { name: "Units" })).toBeVisible();
    await expect(radio(page, "Binary")).toBeChecked();
    await expect(radio(page, "Decimal")).not.toBeChecked();
    const order = await page.getByRole("radiogroup", { name: "Units" }).getByRole("radio").evaluateAll(
      (radios) => radios.map((radio) => radio.value),
    );
    expect(order).toEqual(["binary", "decimal"]);
    await expect(page.getByText("1 GB = 1,000,000,000 bytes")).toBeVisible();
    await expect(page.getByText("1 GiB = 1,073,741,824 bytes")).toBeVisible();
  });
}

test("an account that never chose reads every size in GiB", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  await withPreferences(page);
  await page.goto("/inventory/instances");
  await expect(page.getByRole("row", { name: /tenant-owned-vm/ })).toContainText("80 GiB");
});

test("choosing decimal saves only that choice and every size follows", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin", { snapshotInstancePage: true });
  const prefs = await withPreferences(page);
  await page.goto("/settings/general");

  await radio(page, "Decimal").check();
  await expect.poll(() => prefs.puts.length).toBe(1);
  expect(prefs.puts[0]).toEqual({ byteUnits: "decimal" });
  await expect(radio(page, "Decimal")).toBeChecked();

  const hint = (await context.cookies()).find((cookie) => cookie.name === HINT);
  expect(JSON.parse(decodeURIComponent(hint.value)).bu).toBe("decimal");

  await page.goto("/inventory/instances");
  await expect(page.getByRole("row", { name: /tenant-owned-vm/ })).toContainText("85.9 GB");
});

test("an account that chose decimal keeps it", async ({ page, context }) => {
  await authenticate(context, "partner");
  await installApiFixtures(page, "partner");
  await withPreferences(page, { initial: { byteUnits: "decimal" } });
  await page.goto("/settings/general");
  await expect(radio(page, "Decimal")).toBeChecked();
  await expect(radio(page, "Binary")).not.toBeChecked();
});

test("the keyboard moves the choice like any radio group", async ({ page, context }) => {
  await authenticate(context, "partner");
  await installApiFixtures(page, "partner");
  const prefs = await withPreferences(page);
  await page.goto("/settings/general");
  await radio(page, "Binary").focus();
  await page.keyboard.press("ArrowRight");
  await expect(radio(page, "Decimal")).toBeChecked();
  await expect.poll(() => prefs.puts.at(-1)).toEqual({ byteUnits: "decimal" });
});

test("a failed save puts the choice back and says so", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  const prefs = await withPreferences(page, { failPut: true });
  await page.goto("/settings/general");

  // click, not check: the refused save puts Binary back at once, which check() reads as a click
  // that changed nothing.
  await radio(page, "Decimal").click();
  await expect.poll(() => prefs.puts.length).toBe(1);
  await expect(page.getByText("Could not save units").first()).toBeVisible();
  await expect(radio(page, "Binary")).toBeChecked();
});

test("the hint paints the account's units before its preferences arrive", async ({ page, context }) => {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  // The saved choice answers late: what shows before it is the hint cookie's.
  await withPreferences(page, { initial: { byteUnits: "decimal" }, readDelayMs: 8000 });
  const origin = new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025");
  await context.addCookies([{
    name: HINT, value: encodeURIComponent(JSON.stringify({ bu: "decimal" })), domain: origin.hostname, path: "/",
  }]);
  const problems = [];
  page.on("console", (message) => {
    if (/hydrat|did not match|server rendered/i.test(message.text())) problems.push(message.text());
  });

  const started = Date.now();
  await page.goto("/settings/general");
  await expect(radio(page, "Decimal")).toBeChecked({ timeout: 7000 });
  expect(Date.now() - started).toBeLessThan(8000);
  expect(problems).toEqual([]);
});

for (const tampered of ['{"bu":"<img src=x onerror=alert(1)>"}', '{"bu":["decimal"]}', "%7Bnot-json", '{"bu":"DECIMAL"}']) {
  test(`a tampered hint ${JSON.stringify(tampered).slice(0, 24)} reads binary`, async ({ page, context }) => {
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
    await expect(radio(page, "Binary")).toBeChecked();
    expect(errors).toEqual([]);
  });
}
