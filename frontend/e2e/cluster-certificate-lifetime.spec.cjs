const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// Registration offers how long the cluster's client certificate lasts. Left alone, nothing is sent
// and the deployment's default applies as before; a chosen lifetime is sent; the panel after it
// shows the date read from the issued certificate.

const in30 = new Date(Date.now() + 30 * 86400e3).toISOString();
const in365 = new Date(Date.now() + 365 * 86400e3).toISOString();
const caEnd = new Date(Date.now() + 1800 * 86400e3).toISOString();

const LIFETIMES = {
  default: "default",
  options: [
    { value: "default", label: "Default (30 days)", expires_at: in30, available: true },
    { value: "720h", label: "1 month", expires_at: in30, available: true },
    { value: "8760h", label: "1 year", expires_at: in365, available: true },
    { value: "4320h", label: "6 months", expires_at: null, available: false, reason: "The certificate authority expires on Feb 1, 2027, before this period ends." },
    { value: "longest", label: "Longest", expires_at: caEnd, available: true },
  ],
};

async function openRegister(page, context, { role = "partner", lifetimes = LIFETIMES } = {}) {
  await authenticate(context, role);
  await installApiFixtures(page, role);
  const registrations = [];
  await page.route(/\/api\/v1\/clusters\/certificate-lifetimes$/, (route) =>
    lifetimes ? route.fulfill({ json: lifetimes }) : route.fulfill({ status: 503, json: { detail: "down" } }));
  await page.route(/\/api\/v1\/clusters\/register$/, async (route) => {
    const body = route.request().postDataJSON();
    registrations.push(body);
    await route.fulfill({
      status: 201,
      json: {
        success: true, message: "Certificate generated!", cluster_id: body.cluster_id, status: "not_authorized",
        certificate_generated: true, trust_added: false,
        certificate: "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
        certificate_lifetime: body.certificate_lifetime ?? "default",
        certificate_expires_at: body.certificate_lifetime === "longest" ? caEnd : in30,
        lifetime_shortened: false,
      },
    });
  });
  await page.goto("/register-node");
  return registrations;
}

async function fillAndSubmit(page) {
  await page.getByLabel("Cluster ID").fill("qa-lifetime");
  await page.getByLabel("Display Name").fill("QA Lifetime");
  await page.getByLabel("Endpoint").fill("https://10.20.30.40:8443");
  await page.getByRole("combobox", { name: /Region/ }).click();
  await page.getByPlaceholder(/Search/).fill("Aceh Selatan");
  await page.getByRole("option", { name: /Aceh Selatan/ }).first().click();
  await page.getByRole("button", { name: /Register/ }).click();
}

test("the picker opens on the default and names each date", async ({ page, context }) => {
  await openRegister(page, context);
  const picker = page.getByRole("combobox", { name: "Certificate Validity" });
  await expect(picker).toContainText("Default (30 days) · until");

  await picker.click();
  await expect(page.getByRole("option", { name: /^Longest · until/ })).toBeVisible();
  await expect(page.getByRole("option", { name: "6 months · not available" })).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");
  await expect(page.getByText("before this period ends")).toBeVisible();
});

test("left alone, registration sends no lifetime and the default applies", async ({ page, context }) => {
  const registrations = await openRegister(page, context);
  await fillAndSubmit(page);
  await expect.poll(() => registrations.length).toBe(1);
  expect(registrations[0]).not.toHaveProperty("certificate_lifetime");
  await expect(page.getByText(/The certificate is valid until/)).toBeVisible();
});

test("a chosen lifetime is sent and the panel shows the issued date", async ({ page, context }) => {
  const registrations = await openRegister(page, context);
  await page.getByRole("combobox", { name: "Certificate Validity" }).click();
  await page.getByRole("option", { name: /^Longest · until/ }).click();
  await expect(page.getByText(/keeps access until the issuing certificate authority expires/)).toBeVisible();
  await fillAndSubmit(page);
  await expect.poll(() => registrations.length).toBe(1);
  expect(registrations[0].certificate_lifetime).toBe("longest");
  await expect(page.getByText(/The certificate is valid until/)).toBeVisible();
});

test("an unloadable list still registers with the default", async ({ page, context }) => {
  const registrations = await openRegister(page, context, { lifetimes: null });
  await expect(page.getByText(/lifetimes could not be loaded/)).toBeVisible();
  await fillAndSubmit(page);
  await expect.poll(() => registrations.length).toBe(1);
  expect(registrations[0]).not.toHaveProperty("certificate_lifetime");
});
