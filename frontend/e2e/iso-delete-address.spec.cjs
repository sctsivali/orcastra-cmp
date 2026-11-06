const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// The ISO page deleted by (pool, type, name, host) alone, so an ISO in another project or on one
// member of a per-member pool was addressed as `default`'s copy of that name: a 404, the wrong
// volume, or the view-only refusal on a shared cluster (RC5 follow-up F-02, 2026-11-06). The
// request now carries the row's own project and member.

const ISO = {
  name: "ubuntu-26.04-live.iso",
  type: "custom",
  pool: "local",
  description: "",
  config: { size: "6GiB" },
  used_by: [],
  content_type: "iso",
  node: "cluster-a",
  project: "acme",
  location: "member-2",
};

test("deleting an ISO names the project and member of its row", async ({ page, context }) => {
  await authenticate(context, "partner");
  await installApiFixtures(page, "partner");
  const deletes = [];
  await page.route(/\/api\/v1\/storage\/volumes(\?|$)/, (route) =>
    route.fulfill({ json: { total: 1, volumes: [ISO], nodes: ["cluster-a"], unreachable_nodes: [] } }),
  );
  await page.route(/\/api\/v1\/storage\/volumes\/custom\//, (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deletes.push(new URL(route.request().url()));
    return route.fulfill({ json: { success: true, message: "deleted" } });
  });

  await page.goto("/inventory/iso-images");
  await page.getByRole("button", { name: `Actions for ISO ${ISO.name}`, exact: true }).click();
  await page.getByRole("menuitem", { name: "Delete ISO" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Delete ISO", exact: true }).click();

  await expect.poll(() => deletes.length).toBe(1);
  const [request] = deletes;
  expect(request.pathname).toBe(`/api/v1/storage/volumes/custom/${ISO.name}`);
  expect(Object.fromEntries(request.searchParams)).toEqual({
    host: "cluster-a",
    pool: "local",
    project: "acme",
    location: "member-2",
  });
});
