const { test, expect } = require("@playwright/test");

// The sign-in page reads GET /auth/cert-status (no session, answered by the backend through nginx)
// and shows one state per answer. Signing in itself goes through the backend's cert-login and is
// covered by the backend tests and the live Mini suite, not here.

const FINGERPRINT = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89";

function certStatus(fields) {
  return {
    certificate_presented: true,
    enrolled: true,
    active: true,
    fingerprint: FINGERPRINT,
    common_name: "ops-laptop",
    expires_at: new Date(Date.now() + 200 * 86_400_000).toISOString(),
    bootstrap_available: false,
    ...fields,
  };
}

// Only the status read may leave the page: any other API call fails the test.
async function openSignIn(page, answer, query = "") {
  const unexpected = [];
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/v1/auth/cert-status") {
      await route.fulfill({ status: answer.status ?? 200, contentType: "application/json", body: JSON.stringify(answer.body) });
      return;
    }
    unexpected.push(`${request.method()} ${url.pathname}`);
    await route.fulfill({ status: 599, contentType: "application/json", body: "{}" });
  });
  await page.goto(`/auth/signin${query}`);
  await expect(page.getByText("Sign in with your client certificate")).toBeVisible({ timeout: 20_000 });
  return { unexpected, pageErrors };
}

async function expectClean(harness) {
  expect(harness.unexpected, "unexpected API requests").toEqual([]);
  expect(harness.pageErrors, "uncaught browser errors").toEqual([]);
}

test("a browser that offered no certificate is told how to import one", async ({ page }) => {
  const harness = await openSignIn(page, { body: certStatus({ certificate_presented: false, enrolled: false, active: false, fingerprint: null, common_name: null }) });
  await expect(page.getByText(/Your browser did not offer a certificate/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign In with Certificate" })).toHaveCount(0);
  await expectClean(harness);
});

test("an unknown certificate shows its fingerprint to report", async ({ page }) => {
  const harness = await openSignIn(page, { body: certStatus({ enrolled: false, active: false }) });
  await expect(page.getByText(/This certificate is not enrolled/)).toBeVisible();
  await expect(page.getByText(FINGERPRINT)).toBeVisible();
  await expect(page.getByLabel("Bootstrap Token")).toHaveCount(0);
  await expectClean(harness);
});

test("while no Administrator exists, the first one enrolls with the bootstrap token", async ({ page }) => {
  const harness = await openSignIn(page, { body: certStatus({ enrolled: false, active: false, bootstrap_available: true }) });
  await expect(page.getByLabel("Bootstrap Token")).toBeVisible();
  await expect(page.getByLabel("Email (Optional)")).toBeVisible();
  await expect(page.getByText(FINGERPRINT)).toBeVisible();
  await expectClean(harness);
});

test("a revoked or expired certificate cannot sign in", async ({ page }) => {
  const harness = await openSignIn(page, { body: certStatus({ active: false }) });
  await expect(page.getByText(/This certificate was revoked or has expired/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign In with Certificate" })).toHaveCount(0);
  await expectClean(harness);
});

test("a signed-out visit waits for the operator instead of signing in again", async ({ page }) => {
  const harness = await openSignIn(page, { body: certStatus({}) });
  await expect(page.getByText(/You are signed out/)).toBeVisible();
  await expect(page.getByText("ops-laptop")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign In with Certificate" })).toBeEnabled();
  await expectClean(harness);
});

test("a failed sign-in says so, and a status read that fails offers a retry", async ({ page }) => {
  const failed = await openSignIn(page, { body: certStatus({}) }, "?error=CredentialsSignin");
  await expect(page.getByText(/The certificate sign-in did not finish/)).toBeVisible();
  await expectClean(failed);

  await page.unrouteAll({ behavior: "ignoreErrors" });
  const unreadable = await openSignIn(page, { status: 503, body: { detail: "Sign-in is unavailable while the identity store is unreachable." } });
  await expect(page.getByText("Sign-in is unavailable while the identity store is unreachable.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Try Again" })).toBeVisible();
  await expectClean(unreadable);
});
