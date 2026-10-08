const { test, expect } = require("@playwright/test");
const { authenticate, capture, expectNoHorizontalOverflow } = require("./support/fixtures.cjs");
const { installSettingsFixtures, expectCleanSettingsHarness } = require("./support/settings-fixtures.cjs");

// Settings > Integrations against mocked API answers: which organizations a key owns, why an
// owner key's Revoke and Delete explain instead of ending it, and the Administrator's transfer.

const SOON = new Date(Date.now() + 3 * 24 * 3600 * 1000).toISOString();

function key(id, name, over = {}) {
  return {
    id, name, app_name: "OrcaHub", app_type: "orcahub",
    capabilities: ["inventory.read", "org.sync", "tenant.provision"],
    created_at: "2026-09-01T00:00:00+00:00", expires_at: null, is_active: true, is_revoked: false,
    is_expired: false, expiry_state: "never", last_used_at: null,
    cluster_permissions: [{ cluster_id: "c1", permissions: ["admin"], projects: ["*"] }],
    user_mappings_count: 0, is_add_on_credential: false,
    owned_organization_count: 0, owned_organization_slugs: [], owned_organization_hidden_count: 0,
    ...over,
  };
}

function org(id, slug, owner, ownerName, state, over = {}) {
  return {
    id, name: slug.split("-").map((w) => w[0].toUpperCase() + w.slice(1)).join(" "), slug,
    description: null, source: "orcahub", external_id: `ext-${slug}`, api_key_id: owner,
    app_name: owner ? "OrcaHub" : null, created_at: null, updated_at: null,
    owner_key_name: ownerName, owner_key_state: state, owner_key_expires_at: null,
    owner_key_expiry_state: owner ? "never" : null, cluster_ids: ["c1"], ...over,
  };
}

const VOCABULARY = {
  capabilities: [
    { name: "inventory.read", description: "Read clusters, projects and instances.", elevated: false },
    { name: "org.sync", description: "Sync organizations and members.", elevated: true, elevated_reason: "Writes access control" },
    { name: "tenant.provision", description: "Provision buyer dashboard access.", elevated: true, elevated_reason: "Writes access control" },
  ],
  application_types: [],
};

const PROVISIONING_WOULD_STOP = {
  error: "provisioning_would_stop", action: "capabilities", capability: "tenant.provision",
  message: "This key owns 2 OrcaHub organizations (fossgo and 1 you cannot see). Withdrawing its tenant.provision "
    + "capability stops provisioning buyer dashboard access for them: an order completed in OrcaHub no longer gives "
    + "its buyer access here. Their sync and member updates continue. Confirm to withdraw it anyway.",
  consequence: "Provisioning buyer dashboard access for these organizations stops. Their sync and member updates continue.",
  api_key_id: "oak_hub_new", organization_count: 2, organization_slugs: ["fossgo"], hidden_organization_count: 1,
  proceed: "Send the same request with force set to true to withdraw it anyway.",
};

function world(role) {
  const admin = role === "admin";
  return {
    puts: [],
    capabilityPuts: [],
    rotations: [],
    keys: [
      key("oak_hub_old", "Hub Old", {
        is_active: false, is_revoked: true,
        owned_organization_count: 1, owned_organization_slugs: admin ? ["acme-cloud"] : [],
        owned_organization_hidden_count: admin ? 0 : 1,
      }),
      key("oak_hub_new", "Hub Connection", {
        owned_organization_count: 2, owned_organization_slugs: admin ? ["buyer-co", "fossgo"] : ["fossgo"],
        owned_organization_hidden_count: admin ? 0 : 1,
      }),
      key("oak_expiring", "Expiring Key", { expires_at: SOON, expiry_state: "expiring" }),
      key("oak_mcp", "Agent Key", { app_type: "mcp", capabilities: ["inventory.read", "lxd.proxy"] }),
      key("oak_addon", "backup-bot @ acme", { app_type: "automation", is_add_on_credential: true }),
    ],
    orgs: admin
      ? [
          org(1, "acme-cloud", "oak_hub_old", "Hub Old", "revoked", { cluster_ids: ["c1", "c2"] }),
          org(2, "buyer-co", "oak_hub_new", "Hub Connection", "active"),
          org(3, "fossgo", "oak_hub_new", "Hub Connection", "active"),
        ]
      : [
          org(1, "acme-cloud", "oak_hub_old", "Hub Old", "revoked"),
          org(3, "fossgo", "oak_hub_new", "Hub Connection", "active"),
          // A tenant row lists the organization; its owner key is shown only to its partners.
          org(4, "buyer-two", "oak_hub_new", null, null, { owner_key_expiry_state: null }),
        ],
  };
}

async function installOwnership(page, role) {
  const settings = await installSettingsFixtures(page, role);
  const state = world(role);
  // Registered after the settings fixtures, so it answers these paths first.
  await page.route("**/api/v1/integrations/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    if (request.headers().authorization !== `Bearer e2e-${role}-access-token`) {
      return route.fulfill({ status: 401, json: { detail: "Unexpected fixture actor" } });
    }
    if (request.method() === "GET" && path === "/integrations/api-keys") {
      const active = state.keys.filter((k) => !k.is_revoked && k.is_active).length;
      return route.fulfill({ json: { total: state.keys.length, active, expired: 0, revoked: 1, keys: state.keys } });
    }
    if (request.method() === "GET" && path === "/integrations/organizations") {
      return route.fulfill({ json: state.orgs });
    }
    if (request.method() === "GET" && path === "/integrations/capabilities") {
      return route.fulfill({ json: VOCABULARY });
    }
    const rotation = path.match(/^\/integrations\/api-keys\/([^/]+)\/rotate-secret$/);
    if (request.method() === "POST" && rotation) {
      state.rotations.push(rotation[1]);
      const target = state.keys.find((k) => k.id === rotation[1]);
      return route.fulfill({ json: { api_key: target, api_key_id: target.id, api_key_secret: "oas_e2e_rotated_secret" } });
    }
    const capabilities = path.match(/^\/integrations\/api-keys\/([^/]+)\/capabilities$/);
    if (request.method() === "PUT" && capabilities) {
      const body = JSON.parse(request.postData() || "{}");
      state.capabilityPuts.push({ keyId: capabilities[1], body });
      const target = state.keys.find((k) => k.id === capabilities[1]);
      const withdraws = target.capabilities.includes("tenant.provision") && !body.capabilities.includes("tenant.provision");
      if (withdraws && target.owned_organization_count > 0 && !body.force) {
        return route.fulfill({ status: 409, json: { detail: PROVISIONING_WOULD_STOP } });
      }
      Object.assign(target, { capabilities: body.capabilities });
      return route.fulfill({ json: target });
    }
    const transfer = path.match(/^\/integrations\/organizations\/(\d+)\/owner-key$/);
    if (request.method() === "PUT" && transfer) {
      const body = JSON.parse(request.postData() || "{}");
      state.puts.push({ orgId: Number(transfer[1]), body });
      if (role !== "admin") return route.fulfill({ status: 403, json: { detail: "Insufficient permissions" } });
      const moved = state.orgs.find((o) => o.id === Number(transfer[1]));
      const previous = moved.api_key_id;
      Object.assign(moved, { api_key_id: body.api_key_id, owner_key_name: "Hub Connection", owner_key_state: "active" });
      return route.fulfill({
        json: {
          organization_id: moved.id, organization_slug: moved.slug, external_id: moved.external_id,
          previous_api_key_id: previous, previous_key_state: "revoked", api_key_id: body.api_key_id,
          api_key_name: "Hub Connection", app_name: "OrcaHub", changed: true,
          uncovered_cluster_ids: ["c2"], claimed_organization_count: 3,
        },
      });
    }
    return route.fallback();
  });
  return { settings, state };
}

// The innermost card whose title is this key's name. A wrapper that holds every card matches the
// same filter and comes first in document order, so the last match is the card itself.
function card(page, name) {
  return page.locator("div.rounded-xl").filter({ has: page.getByRole("heading", { name, exact: true }) }).last();
}

test("an admin sees what each key owns and moves a frozen organization", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const { settings, state } = await installOwnership(page, "admin");
  await page.goto("/settings/integrations");

  const hub = card(page, "Hub Connection");
  await expect(hub.getByText("Owns 2 organizations", { exact: true })).toBeVisible();
  await expect(hub.getByText("buyer-co", { exact: true })).toBeVisible();
  await expect(card(page, "Hub Old").getByText(
    "This key no longer works, so the organizations it owns are frozen until an administrator transfers them.",
  )).toBeVisible();

  // An owner key's Revoke explains instead of ending it.
  await hub.getByRole("button", { name: "Revoke" }).click();
  const explain = page.getByRole("alertdialog");
  await expect(explain.getByRole("heading", { name: "Transfer Ownership First" })).toBeVisible();
  await expect(explain).toContainText(
    "This key owns 2 OrcaHub organizations (buyer-co, fossgo). Revoking it would leave them with an owner",
  );
  await expect(explain.getByRole("button", { name: "Revoke" })).toHaveCount(0);
  await explain.getByRole("button", { name: "Open Synced Organizations" }).click();
  await expect(page.locator("#synced-organizations")).toBeFocused();

  const panel = page.locator("#synced-organizations");
  await expect(panel.getByText(
    "1 organization has an owner key that no longer works. Their sync and provisioning are paused until you transfer them.",
  )).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "admin-synced-organizations");

  await panel.getByRole("button", { name: "Actions for acme-cloud" }).click();
  await page.getByRole("menuitem", { name: "Transfer Owner" }).click();
  const dialog = page.getByRole("dialog", { name: "Transfer Owner Key" });
  await expect(dialog.getByRole("heading", { name: "Transfer Owner Key" })).toBeVisible();
  await dialog.locator("#transfer-owner-key").click();
  const options = page.getByRole("option");
  await expect(options).toHaveCount(2);
  await expect(page.getByRole("option", { name: /Agent Key|backup-bot|Hub Old/ })).toHaveCount(0);
  await page.getByRole("option", { name: /Hub Connection/ }).click();
  await expect(dialog).toContainText("does not include c2");

  const submit = dialog.getByRole("button", { name: "Transfer Owner" });
  await dialog.getByLabel("Reason").fill("The old connection key was revoked");
  await expect(submit).toBeDisabled();
  await dialog.getByLabel("Type the organization's slug to confirm").fill("acme-cloud");
  await expect(submit).toBeEnabled();
  await capture(page, testInfo, "admin-transfer-dialog");
  await submit.evaluate((element) => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(page.getByText("Owner Key Transferred", { exact: true })).toBeVisible();
  expect(state.puts).toEqual([{ orgId: 1, body: { api_key_id: "oak_hub_new", reason: "The old connection key was revoked" } }]);
  await expect(panel.getByText("organization has an owner key that no longer works", { exact: false })).toHaveCount(0);
  await expectCleanSettingsHarness(settings);
});

test("a partner reads ownership and cannot transfer", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const { settings, state } = await installOwnership(page, "partner");
  await page.goto("/settings/integrations");

  const hub = card(page, "Hub Connection");
  await expect(hub.getByText("Owns 2 organizations", { exact: true })).toBeVisible();
  await expect(hub.getByText("+1 you cannot see", { exact: true })).toBeVisible();
  await hub.getByRole("button", { name: "Delete" }).click();
  const explain = page.getByRole("alertdialog");
  await expect(explain).toContainText("(fossgo and 1 you cannot see)");
  await expect(explain).toContainText("Ask an administrator to transfer them to another active key first");
  await expect(explain.getByRole("button", { name: "Open Synced Organizations" })).toHaveCount(0);
  await explain.getByRole("button", { name: "Close" }).click();

  const panel = page.locator("#synced-organizations");
  await expect(panel.getByText("Ask an administrator to transfer this organization to an active key.")).toBeVisible();
  const tenantOnly = panel.getByRole("row").filter({ hasText: "buyer-two" });
  await expect(tenantOnly.getByText("Not Shown", { exact: true })).toBeVisible();
  await expect(tenantOnly.getByText("Only this organization's partners see its owner key.")).toBeVisible();
  await expect(panel.getByRole("button", { name: /^Actions for/ })).toHaveCount(0);
  await expect(panel.getByText("no longer works", { exact: false })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "partner-synced-organizations");
  expect(state.puts).toEqual([]);
  await expectCleanSettingsHarness(settings);
});


test("withdrawing tenant.provision from an owner key says what stops and goes through on confirmation", async ({ page }, testInfo) => {
  await authenticate(page.context(), "partner");
  const { settings, state } = await installOwnership(page, "partner");
  await page.goto("/settings/integrations");

  const hub = card(page, "Hub Connection");
  await hub.getByRole("button", { name: "Edit Capabilities" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit Capabilities" });
  await dialog.getByRole("checkbox", { name: /tenant\.provision/ }).uncheck();
  await dialog.getByRole("button", { name: "Save Capabilities" }).click();

  const warning = dialog.getByRole("alert");
  await expect(warning).toContainText(
    "Provisioning buyer dashboard access for these organizations stops. Their sync and member updates continue.",
  );
  await expect(warning).toContainText("(fossgo and 1 you cannot see)");
  await expect(dialog.getByRole("button", { name: "Save Capabilities" })).toHaveCount(0);
  expect(state.capabilityPuts).toEqual([
    { keyId: "oak_hub_new", body: { capabilities: ["inventory.read", "org.sync"], force: false } },
  ]);
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "partner-provisioning-warning");

  const confirm = dialog.getByRole("button", { name: "Withdraw Anyway" });
  await confirm.evaluate((element) => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(page.getByText("Capabilities Updated", { exact: true })).toBeVisible();
  await expect(dialog).toHaveCount(0);
  expect(state.capabilityPuts).toEqual([
    { keyId: "oak_hub_new", body: { capabilities: ["inventory.read", "org.sync"], force: false } },
    { keyId: "oak_hub_new", body: { capabilities: ["inventory.read", "org.sync"], force: true } },
  ]);
  await expectCleanSettingsHarness(settings);
});


test("rotating a secret needs the key's name typed first", async ({ page }, testInfo) => {
  await authenticate(page.context(), "admin");
  const { settings, state } = await installOwnership(page, "admin");
  await page.goto("/settings/integrations");

  await card(page, "Hub Connection").getByRole("button", { name: "Rotate Secret" }).click();
  const confirm = page.getByRole("alertdialog");
  const rotate = confirm.getByRole("button", { name: "Rotate Secret" });
  const typed = confirm.getByLabel("Type the key's name to confirm");
  await expect(rotate).toBeDisabled();
  await typed.fill("Hub Connectio");
  await expect(rotate).toBeDisabled();
  await typed.fill("Hub Connection");
  await expect(rotate).toBeEnabled();
  await capture(page, testInfo, "rotate-secret-confirmation");
  await rotate.evaluate((element) => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(page.getByRole("dialog", { name: "Secret Rotated" })).toBeVisible();
  expect(state.rotations).toEqual(["oak_hub_new"]);
  await expectCleanSettingsHarness(settings);
});
