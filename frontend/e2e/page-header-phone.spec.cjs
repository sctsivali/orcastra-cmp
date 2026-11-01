const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installNetworkFixtures } = require("./support/network-fixtures.cjs");

// A page header held its title and its actions on one row at every width, so on a phone the primary
// action ran off the right edge, where the page clips it and no scroll reaches it.

test("the primary action of a page stays on screen", async ({ page }) => {
  // note: a small phone on purpose. At the Pixel 7's 412 this button happens to end 1px inside.
  await page.setViewportSize({ width: 360, height: 800 });
  await authenticate(page.context(), "partner");
  await installNetworkFixtures(page, "partner", { allowCreate: true });
  await page.goto("/inventory/networks");

  const action = page.getByRole("button", { name: "Create Network", exact: true });
  await expect(action).toBeVisible({ timeout: 15_000 });
  const box = await action.boundingBox();
  const width = page.viewportSize().width;
  expect(box.x + box.width, "the button ends inside the viewport").toBeLessThanOrEqual(width);
});
