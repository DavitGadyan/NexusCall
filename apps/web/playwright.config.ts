import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end specs against a RUNNING local stack (`make up`); they never start one.
 * Env: E2E_BASE_URL (default http://localhost:47000), KC_DEV_USER_PASSWORD, MAILPIT_URL (default http://localhost:47025).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:47000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
