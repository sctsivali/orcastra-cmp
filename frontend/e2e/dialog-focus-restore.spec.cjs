const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { expectCleanNetworkFixtures, installNetworkFixtures } = require("./support/network-fixtures.cjs");

// A closing dialog hands focus back to the control that opened it without moving the page. The
// created-row focus scrolls a new row into view while its create dialog is still closing, and a
// restore that scrolled to the opener (a Create button at the top of the page) would put that row
// back off screen.
test("closing a dialog returns focus to its opener and leaves the page where it was", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const api = await installNetworkFixtures(page, "admin");
  await page.goto("/inventory/networks");
  const opener = page.getByRole("button", { name: "Create Network", exact: true });
  await opener.click();
  await expect(page.getByRole("dialog")).toBeVisible();

  // What useRowFocus does to a created row while the dialog is still on screen.
  const scrolled = await page.evaluate(() => {
    const spacer = document.createElement("div");
    spacer.style.height = "3000px";
    (document.querySelector("main") ?? document.body).append(spacer);
    spacer.scrollIntoView({ block: "end" });
    return Math.round(window.scrollY);
  });
  expect(scrolled).toBeGreaterThan(500);

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(opener).toBeFocused();
  expect(Math.round(await page.evaluate(() => window.scrollY))).toBe(scrolled);
  await expectCleanNetworkFixtures(api);
});
