const { defineConfig, devices } = require("@playwright/test");
const path = require("node:path");

const baseURL = process.env.ORCASTRA_E2E_BASE_URL || "http://127.0.0.1:2025";
const parsedBaseURL = new URL(baseURL);
const e2ePort = Number(parsedBaseURL.port || (parsedBaseURL.protocol === "https:" ? 443 : 80));
if (!Number.isInteger(e2ePort) || e2ePort < 1 || e2ePort > 65535) {
  throw new Error(`Invalid ORCASTRA_E2E_BASE_URL port: ${parsedBaseURL.port}`);
}
const artifactRoot = process.env.ORCASTRA_E2E_ARTIFACT_ROOT || "/tmp/orcastra-playwright";
const outputDir = process.env.ORCASTRA_E2E_OUTPUT_DIR || path.join(artifactRoot, "test-results");
const reportDir = process.env.ORCASTRA_E2E_REPORT_DIR || path.join(artifactRoot, "playwright-report");

module.exports = defineConfig({
  testDir: "./e2e",
  outputDir,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 1,
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  reporter: [
    ["list"],
    ["html", { outputFolder: reportDir, open: "never" }],
  ],
  use: {
    baseURL,
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    serviceWorkers: "block",
  },
  projects: [
    {
      name: "desktop-light",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
        colorScheme: "light",
      },
    },
    {
      name: "mobile-dark",
      use: {
        ...devices["Pixel 7"],
        colorScheme: "dark",
      },
    },
  ],
  webServer: {
    command: `npx next start -p ${e2ePort}`,
    url: baseURL,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    env: {
      AUTH_SECRET:
        process.env.AUTH_SECRET ||
        process.env.NEXTAUTH_SECRET ||
        "orcastra-e2e-only-secret-32-characters-minimum",
      NEXTAUTH_SECRET:
        process.env.NEXTAUTH_SECRET ||
        process.env.AUTH_SECRET ||
        "orcastra-e2e-only-secret-32-characters-minimum",
      NEXTAUTH_URL: baseURL,
      AUTHENTIK_ISSUER: "https://idp.invalid/application/o/orcastra/",
      AUTHENTIK_CLIENT_ID: "orcastra-e2e-client",
      AUTHENTIK_CLIENT_SECRET: "unused-e2e-client-secret",
      NEXT_PUBLIC_API_URL: "",
      NEXT_TELEMETRY_DISABLED: "1",
    },
  },
});
