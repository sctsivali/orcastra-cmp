const { test, expect } = require("@playwright/test");
const { authenticate, capture } = require("./support/fixtures.cjs");
const { CAPABILITIES, choose, expectCleanNetworkFixtures, installNetworkFixtures, openNetworkDialog } = require("./support/network-fixtures.cjs");

for (const role of ["admin", "partner"]) {
  test(`${role} can create a rich Bridge once while the request is pending`, async ({ page }, testInfo) => {
    await authenticate(page.context(), role);
    const api = await installNetworkFixtures(page, role, { allowCreate: true, createDelay: 300 });
    await openNetworkDialog(page);
    await choose(page, "IPv4 Address", "Custom");
    await page.getByLabel("IPv4 custom CIDR").fill("10.42.0.1/24");
    await choose(page, "IPv4 NAT", "Enabled");
    await choose(page, "IPv4 DHCP", "Enabled");
    await page.getByLabel("IPv4 DHCP Ranges", { exact: true }).fill("10.42.0.10-10.42.0.200");
    await choose(page, "IPv6 Address", "Custom");
    await page.getByLabel("IPv6 custom CIDR").fill("::/64");
    await choose(page, "IPv6 Stateful DHCP", "Enabled");
    await page.getByLabel("IPv6 DHCP Ranges", { exact: true }).fill("::10-::100");
    await page.getByLabel("Bridge MTU").fill("1500");
    await page.getByLabel("DNS Domain").fill("qa.example");
    await choose(page, "DNS Mode", "Managed");
    await page.getByRole("checkbox", { name: "qa-allow", exact: true }).check();
    await capture(page, testInfo, `network-create-${role}`);
    const submit = page.getByRole("button", { name: "Create", exact: true });
    await expect(submit).toBeEnabled();
    await submit.evaluate((button) => { button.click(); button.click(); });
    await expect(submit).toBeDisabled();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(api.creates).toHaveLength(1);
    expect(api.creates[0]).toMatchObject({ host: "cluster-a", project: "default", body: {
      name: "qa-network", type: "bridge", config: { "ipv4.address": "10.42.0.1/24", "ipv4.nat": "true",
        "ipv4.dhcp": "true", "ipv4.dhcp.ranges": "10.42.0.10-10.42.0.200", "ipv6.address": "::/64",
        "ipv6.dhcp.stateful": "true", "ipv6.dhcp.ranges": "::10-::100", "bridge.mtu": "1500",
        "dns.domain": "qa.example", "dns.mode": "managed", "security.acls": "qa-allow" },
    } });
    await expect(page.getByRole("row").filter({ hasText: "qa-network" })).toBeVisible();
    await expectCleanNetworkFixtures(api);
  });
}

test("OVN creation keeps project and uplink scope and excludes Bridge fields", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installNetworkFixtures(page, "partner", { allowCreate: true });
  await openNetworkDialog(page);
  await choose(page, "Type *", "OVN");
  await choose(page, "Project", "tenant-a");
  await choose(page, "Uplink Network *", "br-uplink (bridge)");
  await expect(page.getByLabel("Bridge MTU")).toHaveCount(0);
  await expect(page.getByLabel("DNS Mode")).toHaveCount(0);
  await choose(page, "IPv6 Stateful DHCP", "Enabled");
  await expect(page.getByLabel("IPv6 DHCP Ranges")).toHaveCount(0);
  await page.getByRole("checkbox", { name: "tenant-allow", exact: true }).check();
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(api.creates).toHaveLength(1);
  expect(api.creates[0].project).toBe("tenant-a");
  expect(api.creates[0].body.config).toEqual({ network: "br-uplink", "ipv6.dhcp.stateful": "true", "security.acls": "tenant-allow" });
  expect(api.reads.filter((read) => read.path === "/network-acls").map((read) => read.project)).toContain("tenant-a");
  await expectCleanNetworkFixtures(api);
});

test("invalid CIDR and DHCP ranges prevent a request and recover after correction", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installNetworkFixtures(page, "partner");
  await openNetworkDialog(page);
  await choose(page, "IPv6 Address", "Custom");
  await page.getByLabel("IPv6 custom CIDR").fill("1:2/64");
  await expect(page.getByText("Invalid IPv6 CIDR", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  await page.getByLabel("IPv6 custom CIDR").fill("::/64");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();
  await page.getByLabel("Bridge MTU").fill("1200");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  await page.getByLabel("Bridge MTU").fill("1500");
  await choose(page, "IPv4 Address", "Custom");
  await page.getByLabel("IPv4 custom CIDR").fill("10.42.0.1/24");
  await page.getByLabel("IPv4 DHCP Ranges", { exact: true }).fill("10.43.0.10-10.43.0.20");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  await page.getByLabel("IPv4 DHCP Ranges", { exact: true }).fill("10.42.0.10-10.42.0.20");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();
  expect(api.creates).toHaveLength(0);
  await expectCleanNetworkFixtures(api);
});

test("capability failure is visible and must recover before creation", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installNetworkFixtures(page, "partner", { capabilitiesUnavailable: true, allowCreate: true });
  await page.goto("/inventory/networks");
  await page.getByRole("button", { name: "Create Network", exact: true }).click();
  await choose(page, "Cluster *", /Core Cluster/);
  await page.getByLabel("Name *", { exact: true }).fill("qa-network");
  await expect(page.getByRole("button", { name: "Retry capabilities" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  api.capabilitiesUnavailable = false;
  await page.getByRole("button", { name: "Retry capabilities" }).click();
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(api.creates[0].body.config).toEqual({});
  await expectCleanNetworkFixtures(api);
});

test("unsupported OVN is disabled without trusting a platform name", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installNetworkFixtures(page, "partner", { capabilities: {
    known: true, types: ["bridge"], config_keys: { bridge: CAPABILITIES.config_keys.bridge },
  } });
  await openNetworkDialog(page);
  await page.getByLabel("Type *", { exact: true }).click();
  await expect(page.getByRole("option", { name: "OVN", exact: true })).toHaveAttribute("data-disabled", "");
  await page.keyboard.press("Escape");
  expect(api.creates).toHaveLength(0);
  await expectCleanNetworkFixtures(api);
});

test("a daemon timeout remains an error without a created-row success", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installNetworkFixtures(page, "partner", { allowCreate: true, createStatus: 504 });
  await openNetworkDialog(page);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "may be partially applied" })).toBeVisible();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "qa-network" })).toHaveCount(0);
  expect(api.creates).toHaveLength(1);
  await expectCleanNetworkFixtures(api);
});

test("Tenant sees no infrastructure creation control or privileged discovery", async ({ page }) => {
  await authenticate(page.context(), "tenant");
  const api = await installNetworkFixtures(page, "tenant");
  await page.goto("/inventory/networks");
  await expect(page.getByRole("heading", { name: "Networks", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Create Network", exact: true })).toHaveCount(0);
  expect(api.reads.filter((read) => ["/network-acls", "/networks/creation/capabilities"].includes(read.path))).toEqual([]);
  expect(api.creates).toHaveLength(0);
  await expectCleanNetworkFixtures(api);
});

test("read-only mode disables network creation", async ({ page }) => {
  await authenticate(page.context(), "partner");
  const api = await installNetworkFixtures(page, "partner", { readOnly: true });
  await page.goto("/inventory/networks");
  await expect(page.getByRole("button", { name: "Create Network", exact: true })).toBeDisabled();
  expect(api.creates).toHaveLength(0);
  await expectCleanNetworkFixtures(api);
});
