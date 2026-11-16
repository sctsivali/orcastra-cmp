const { test, expect } = require("@playwright/test");
const { authenticate, expectNoHorizontalOverflow, installApiFixtures } = require("./support/fixtures.cjs");

// Three cards side by side leave a phone about 90px each, less than a rate and its icon need. Every
// figure is the house stat value in the body face, with its icon in the label row above it, so it
// has the whole card width, stays on one line and never meets the icon.

const LONG_AUDIT = {
  logs: [
    {
      id: 1,
      session_id: "e2e-long",
      event_type: "disconnect",
      instance_name: "e2e-long",
      host: "e2e-node",
      project: "default",
      protocol: "websocket-terminal",
      user_id: "e2e",
      user_email: "partner@example.test",
      user_role: "partner",
      duration_seconds: 4_330_000,
      download_bytes: 999_990_000_000,
      upload_bytes: 999_990_000,
      is_break_glass: false,
      created_at: "2026-11-08T10:00:00Z",
    },
  ],
  total: 1,
  has_more: false,
};

async function expectFigure(page, label, text) {
  const value = page.locator("p", { hasText: new RegExp(`^${label}$`) }).first().locator("xpath=following-sibling::p[1]");
  await expect(value, `${label} figure`).toHaveText(text, { timeout: 15_000 });
  const m = await value.evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const lines = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
    const icon = el.previousElementSibling?.querySelector("svg")?.getBoundingClientRect();
    const card = el.closest("[class*='rounded']")?.getBoundingClientRect();
    const box = range.getBoundingClientRect();
    return {
      lines: lines.size,
      right: box.right,
      top: box.top,
      cardRight: card ? card.right : Number.POSITIVE_INFINITY,
      iconBottom: icon ? icon.bottom : Number.NEGATIVE_INFINITY,
      family: getComputedStyle(el).fontFamily,
    };
  });
  expect(m.lines, `${label} reads on one line`).toBe(1);
  expect(m.top, `${label} stays clear of its icon`).toBeGreaterThanOrEqual(m.iconBottom);
  expect(m.right, `${label} stays inside its card`).toBeLessThanOrEqual(m.cardRight);
  expect(m.family, `${label} is set in the body face, not monospace`).not.toMatch(/mono/i);
}

test("the sessions summary keeps every figure on one line, clear of its icon", async ({ page, context }) => {
  await authenticate(context, "partner");
  // Decimal prints the longest figures (999.99 GB), which is what this layout check needs.
  await installApiFixtures(page, "partner", { byteUnits: "decimal" });
  await page.route(/\/api\/v1\/console\/sessions\/active/, (route) => route.fulfill({ json: [] }));
  await page.route(/\/api\/v1\/console\/sessions\/audit/, (route) => route.fulfill({ json: LONG_AUDIT }));
  await page.goto("/inventory/sessions");

  for (const [label, figure] of [["Active", "0"], ["Download", "0 B/s"], ["Upload", "0 B/s"]]) {
    await expectFigure(page, label, figure);
  }
  await expectNoHorizontalOverflow(page);

  await page.getByRole("tab", { name: /History/ }).click();
  for (const [label, figure] of [
    ["Past Sessions", "1"],
    ["Total Time", "50d 2h"],
    ["Downloaded", "999.99 GB"],
    ["Uploaded", "999.99 MB"],
  ]) {
    await expectFigure(page, label, figure);
  }
  await expectNoHorizontalOverflow(page);
});
