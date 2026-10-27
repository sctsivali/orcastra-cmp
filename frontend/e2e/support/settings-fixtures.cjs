const { installApiFixtures, expectCleanHarness } = require("./fixtures.cjs");

async function installSettingsFixtures(page, role, options = {}) {
  const base = await installApiFixtures(page, role, options.api || {});
  const state = {
    base,
    requests: [],
    sharedClears: 0,
    sharedClearFails: options.sharedClearFails === true,
    holdNextSharedClear: null,
    organization: { organization_id: 17, settings: {}, updated_at: null },
    personal: options.personal || {},
    orgReads: 0,
    orgFailuresRemaining: 0,
    orgFailedReads: 0,
    holdNextOrgRead: null,
  };
  const reads = {
    "/organizations": [],
    "/integrations/api-keys": { total: 0, active: 0, expired: 0, revoked: 0, keys: [] },
    "/integrations/available-clusters": { clusters: [] },
    "/integrations/capabilities": { capabilities: [], application_types: [] },
    "/integrations/organizations": [],
    "/user-groups": [],
    "/user-groups/sync-status": { authentik_configured: false, authentik_reachable: false, groups_exist: false, details: "Fixture identity provider is disabled" },
    "/tenant-access/policies": { policies: [], total: 0 },
    "/clusters": { clusters: [], total: 0 },
    // An Administrator's Personalization tab edits the deployment default (2026-10-27). Any
    // other role asking for it stays an unexpected request.
    ...(role === "admin" ? { "/branding/deployment": { settings: {}, updated_by: null, updated_at: null, version: "e2e-built-in" } } : {}),
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = request.method();
    const isPreferences = ["/user-preferences", "/organizations/my-preferences", "/organizations/my-preferences/version"].includes(path);
    const declared = (method === "GET" && (isPreferences || Object.hasOwn(reads, path) || path === "/system/resource-usage")) || (method === "POST" && path === "/system/cache/clear");
    if (!declared) return route.fallback();
    const entry = { method, path, search: url.search };
    state.requests.push(entry);
    const baseURL = new URL(process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025");
    if (url.origin !== baseURL.origin || request.headers().authorization !== `Bearer e2e-${role}-access-token`) {
      base.authFailures.push(entry);
      return route.fulfill({ status: 401, json: { detail: "Unexpected fixture actor or origin" } });
    }
    if (method === "POST") {
      if (role !== "admin" || url.search || request.postData()) {
        base.unexpected.push(entry);
        return route.fulfill({ status: 403, json: { detail: "Only the declared Admin clear is allowed" } });
      }
      state.sharedClears += 1;
      const hold = state.holdNextSharedClear;
      state.holdNextSharedClear = null;
      if (hold) await hold;
      return route.fulfill(state.sharedClearFails
        ? { status: 503, json: { detail: "Fixture shared cache unavailable" } }
        : { json: { message: "Cache cleared", namespaces: ["api_responses"] } });
    }
    if (path === "/user-preferences") return route.fulfill({ json: state.personal });
    if (path === "/organizations/my-preferences/version") {
      const { organization_id, updated_at, version = null } = state.organization;
      return route.fulfill({ json: { organization_id, updated_at, version } });
    }
    if (path === "/organizations/my-preferences") {
      state.orgReads += 1;
      if (state.orgFailuresRemaining > 0) {
        state.orgFailuresRemaining -= 1;
        await route.fulfill({ status: 503, json: { detail: "Fixture preferences temporarily unavailable" } });
        state.orgFailedReads += 1;
        return;
      }
      const payload = structuredClone(state.organization);
      const hold = state.holdNextOrgRead;
      state.holdNextOrgRead = null;
      if (hold) await hold;
      return route.fulfill({ json: payload });
    }
    if (path === "/system/resource-usage") return route.fulfill({ status: 503, json: { detail: "Fixture host metrics are unavailable" } });
    return route.fulfill({ json: reads[path] });
  });
  return state;
}

async function wakePreferenceSync(page) {
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

async function expectCleanSettingsHarness(state) {
  await expectCleanHarness(state.base);
}

module.exports = { installSettingsFixtures, wakePreferenceSync, expectCleanSettingsHarness };
