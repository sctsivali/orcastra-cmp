const { test, expect } = require("@playwright/test");
const { authenticate } = require("./support/fixtures.cjs");
const { installInventoryHardeningFixtures } = require("./support/inventory-hardening-fixtures.cjs");

// A cluster still reading past the backend's 15 s per-cluster wait (late_nodes) is not
// an outage: the backend finishes its read in the background, so the list says it is still loading
// and asks again on its own a few seconds later, when the rows have usually landed.

const volumes = ({ late = [], role = "admin" } = {}) => ({
  total: 1,
  volumes: [
    {
      name: "vol-1", type: "custom", pool: "pool-a", node: "blitzkrieg", project: "default",
      content_type: "filesystem", size: 21474836480, used_by: [], config: {}, description: "",
    },
  ],
  nodes: ["blitzkrieg", "central-1"],
  unreachable_nodes: [],
  partial_nodes: [],
  late_nodes: late,
  node_errors:
    role === "tenant"
      ? {}
      : Object.fromEntries(late.map((node) => [node, "late: still reading after 15 s, shown when it finishes"])),
  collected_at: new Date().toISOString(),
});

async function open(page, context, role, respond) {
  await authenticate(context, role);
  await installInventoryHardeningFixtures(page, role);
  let reads = 0;
  await page.route(/\/api\/v1\/storage\/volumes(\?|$)/, (route) => {
    reads += 1;
    return route.fulfill({ json: respond(reads) });
  });
  await page.goto("/inventory/storage-volumes");
  return { banner: page.locator('[data-slot="unreachable-banner"]'), reads: () => reads };
}

for (const role of ["admin", "partner"]) {
  test(`${role}: a cluster still loading has its own sentence and row`, async ({ page, context }) => {
    const { banner } = await open(page, context, role, () => volumes({ late: ["central-1"], role }));
    await expect(banner.getByRole("status")).toHaveText(
      "Still loading 1 cluster. Its storage volumes are shown when it answers.",
    );
    await expect(banner).not.toContainText("Could not reach");
    await banner.getByRole("button", { name: "Show Details" }).click();
    await expect(banner.getByRole("listitem")).toHaveText([
      "central-1Still loading · still reading after 15 s, shown when it finishes",
    ]);
    await expect(banner).not.toContainText("late:");
  });
}

test("the list asks again on its own and the banner leaves once the rows arrive", async ({ page, context }) => {
  const { banner, reads } = await open(page, context, "admin", (n) =>
    n === 1 ? volumes({ late: ["central-1"] }) : volumes(),
  );
  await expect(banner).toBeVisible();
  // STALE_FOLLOW_UP_DELAYS_MS: the first look is 5 s after the late answer.
  await expect.poll(reads, { timeout: 12_000 }).toBeGreaterThan(1);
  await expect(banner).toHaveCount(0);
});

test("tenant: told the list is incomplete, never which cluster", async ({ page, context }) => {
  const { banner } = await open(page, context, "tenant", () => volumes({ late: ["central-1"], role: "tenant" }));
  await expect(banner.getByRole("status")).toHaveText(
    "Some storage volumes could not be loaded right now, so this list may be incomplete.",
  );
  await expect(banner).not.toContainText("central-1");
});
