import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "*.e2e.ts",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // One shared backend (single user, one SQLite DB): run tests in file order on
  // one worker so portfolio state is predictable. No retries — this is a gate.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:8000",
    trace: "retain-on-failure",
    viewport: { width: 1600, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1600, height: 900 } },
    },
  ],
});
