const { test, expect } = require("@playwright/test");
const { authenticate, capture, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const {
  expectCleanInventoryHarness,
  installInventoryHardeningFixtures,
} = require("./support/inventory-hardening-fixtures.cjs");

// Tag chips are neutral category chips: the muted fill and text, the tag's category hue only in a
// square swatch. The Tags facet and the add-tag prefixes are toggle buttons a keyboard reaches.

const TAGS = ["loc_jakarta", "env_production", "plain-tag"];
const coreCard = (page) => page.locator('[data-slot="cluster-card"]').filter({ hasText: "Core Cluster" });

/** The computed colour a utility class resolves to in this theme, read from a probe. */
async function tokenColour(page, className, property) {
  return page.evaluate(
    ([cls, prop]) => {
      const probe = document.createElement("div");
      probe.className = cls;
      document.body.appendChild(probe);
      const value = getComputedStyle(probe)[prop];
      probe.remove();
      return value;
    },
    [className, property],
  );
}

test("admin: tag chips are neutral and keep their category in a square swatch", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const fixture = await installInventoryHardeningFixtures(page, "admin", { nodeTags: TAGS });

  await page.goto("/inventory");
  const card = coreCard(page);
  const chip = card.locator('[data-slot="badge"]', { hasText: /^Jakarta$/ });
  await expect(chip).toBeVisible();

  const look = await chip.evaluate((el) => {
    const swatch = el.querySelector('span[aria-hidden="true"]');
    const s = getComputedStyle(swatch);
    return {
      bg: getComputedStyle(el).backgroundColor,
      color: getComputedStyle(el).color,
      swatch: s.backgroundColor,
      radius: s.borderTopLeftRadius,
      width: swatch.getBoundingClientRect().width,
    };
  });
  // the same solid neutral as the Clustered chip, never a tint of the tag's own hue
  expect(look.bg).toBe(await tokenColour(page, "bg-muted", "backgroundColor"));
  expect(look.color).toBe(await tokenColour(page, "text-muted-foreground", "color"));
  // the category (loc_ is #3b82f6) survives as data, in a square rather than a status-like dot
  expect(look.swatch).toBe("rgb(59, 130, 246)");
  expect(look.radius).toBe("2px");
  expect(look.width).toBe(8);

  // the commas between chips are for a screen reader only
  await expect(card.locator(".sr-only", { hasText: "," })).toHaveCount(TAGS.length);
  await expect(card.locator("span:not(.sr-only)", { hasText: /^,$/ })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "admin-tag-chips");

  await expectCleanInventoryHarness(fixture);
});

test("admin: the Tags facet toggles from the keyboard and says which tags are on", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const fixture = await installInventoryHardeningFixtures(page, "admin", { nodeTags: TAGS });

  await page.goto("/inventory");
  const clusters = page.getByRole("region", { name: "Clusters", exact: true });
  const trigger = clusters.getByRole("button", { name: /Filter by Tags/ });
  await trigger.focus();
  await page.keyboard.press("Enter");

  const toggle = page.getByRole("button", { name: "production", exact: true });
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(toggle).toHaveAttribute("data-slot", "badge");
  await toggle.focus();
  await page.keyboard.press("Space");
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(clusters.getByRole("button", { name: /^Remove filter Tags / })).toHaveCount(1);
  await expect(coreCard(page)).toBeVisible();

  await page.keyboard.press("Space");
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(clusters.getByRole("button", { name: /^Remove filter Tags / })).toHaveCount(0);

  await expectCleanInventoryHarness(fixture);
});

test("admin: the add-tag dialog picks a prefix by toggle and names every remove control", async ({ page }) => {
  await authenticate(page.context(), "admin");
  const fixture = await installInventoryHardeningFixtures(page, "admin", { nodeTags: TAGS });

  await page.goto("/inventory");
  await coreCard(page).getByRole("button", { name: /add tag/i }).click();
  const dialog = page.getByRole("dialog");

  for (const name of ["Jakarta", "Production", "Plain Tag"]) {
    await expect(dialog.getByRole("button", { name: `Remove tag ${name}`, exact: true })).toBeVisible();
  }
  const location = dialog.getByRole("button", { name: "Location", exact: true });
  await expect(location).toHaveAttribute("aria-pressed", "false");
  await location.focus();
  await page.keyboard.press("Enter");
  await expect(location).toHaveAttribute("aria-pressed", "true");
  // the chosen prefix moves into the input, which now asks only for the value
  await expect(dialog.getByPlaceholder("value", { exact: true })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(location).toHaveAttribute("aria-pressed", "false");

  // nothing was saved: leaving the dialog sends no tag update
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toHaveCount(0);

  await expectCleanInventoryHarness(fixture);
});
