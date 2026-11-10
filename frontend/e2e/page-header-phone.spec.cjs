const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installNetworkFixtures } = require("./support/network-fixtures.cjs");

// On a phone a page header's primary action must stay inside the viewport. Past the right edge the
// page clips it and no scroll reaches it.

test("the primary action of a page stays on screen", async ({ page }) => {
  // A small phone: at the Pixel 7's 412 px this button ends 1 px inside and would pass anyway.
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
