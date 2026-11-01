const { test, expect } = require("@playwright/test");
const { authenticate, expectNoHorizontalOverflow, installApiFixtures } = require("./support/fixtures.cjs");

// Three cards side by side leave a phone about 90px each, less than a rate and its icon need, so the
// figure ran under the icon and broke onto two lines.

test("the sessions summary keeps every figure on one line, clear of its icon", async ({ page, context }) => {
  await authenticate(context, "partner");
  await installApiFixtures(page, "partner");
  await page.route(/\/api\/v1\/console\/sessions\/active/, (route) => route.fulfill({ json: [] }));
  await page.goto("/inventory/sessions");

  for (const [label, figure] of [["Download", "↓0 B/s"], ["Upload", "↑0 B/s"]]) {
    const value = page.getByText(figure, { exact: true });
    await expect(value).toBeVisible({ timeout: 15_000 });
    const card = page.getByText(label, { exact: true }).locator("xpath=ancestor::div[contains(@class, 'justify-between')][1]");
    const valueRange = await value.evaluate((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const lines = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
      return { right: range.getBoundingClientRect().right, lines: lines.size };
    });
    const icon = await card.locator("svg").first().boundingBox();
    expect(valueRange.lines, `${label} reads on one line`).toBe(1);
    expect(valueRange.right, `${label} stays clear of its icon`).toBeLessThanOrEqual(icon.x);
  }
  await expectNoHorizontalOverflow(page);
});
