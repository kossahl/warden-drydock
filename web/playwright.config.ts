import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  globalTeardown: "./tests/browser/global-teardown.ts",
  use: { baseURL: "http://127.0.0.1:4173", trace: "retain-on-failure" },
  webServer: {
    command: "python tests/browser/static_server.py",
    url: "http://127.0.0.1:4173/health/live",
    reuseExistingServer: false,
  },
});
