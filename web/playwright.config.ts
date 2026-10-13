import { defineConfig } from "@playwright/test";

/**
 * Playwright on the system Chrome (channel "chrome" = /opt/google/chrome, the install behind /usr/bin/google-chrome).
 *
 * The specs drive the built SPA through the dev cookie proxy (server/main.ts, NOEMA_ENV=dev). Two backends:
 * - the integrator's stack: NOEMA_UI_API_URL=http://127.0.0.1:<api port> and NOEMA_UI_API_KEYS[_FILE] with the
 *   aliases developer, tenant_admin and reviewer (override with NOEMA_UI_E2E_ALIAS_{CHAT,ADMIN,REVIEWER});
 * - otherwise a local fixture API (e2e/fixture-api.ts: contract fixtures and golden SSE streams) with fake keys.
 * Every proxied API response is recorded (NOEMA_UI_RECORD) and `make ui-e2e` validates the recording against the
 * contract with the server's ContractValidator (e2e/validate_recording.py).
 */
const stack = process.env.NOEMA_UI_API_URL !== undefined && process.env.NOEMA_UI_API_URL !== "";
const port = Number(process.env.NOEMA_UI_PORT ?? "5180");
const fixturePort = port + 1;
const fakeKey = (id: string): string => `ak_test_01JA0000000000000000000${id}.${"f".repeat(43)}`;
const proxyEnv: Record<string, string> = {
  NOEMA_ENV: "dev",
  NOEMA_UI_PORT: String(port),
  NOEMA_UI_RECORD: process.env.NOEMA_UI_RECORD ?? "test-results/api-recording.jsonl",
  NOEMA_UI_API_URL: stack ? (process.env.NOEMA_UI_API_URL as string) : `http://127.0.0.1:${fixturePort}`,
};
if (stack) {
  for (const k of ["NOEMA_UI_API_KEYS", "NOEMA_UI_API_KEYS_FILE"]) if (process.env[k]) proxyEnv[k] = process.env[k] as string;
} else {
  proxyEnv.NOEMA_UI_API_KEYS = `developer=${fakeKey("AAA")},tenant_admin=${fakeKey("BBB")},reviewer=${fakeKey("CCC")}`;
}

export default defineConfig({
  testDir: "e2e",
  testMatch: /.*\.spec\.ts$/,
  outputDir: "test-results/playwright",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel: "chrome",
    headless: true,
    trace: "retain-on-failure",
    acceptDownloads: true,
  },
  webServer: [
    ...(stack ? [] : [{ command: `node e2e/fixture-api.ts ${fixturePort}`, url: `http://127.0.0.1:${fixturePort}/v1/sessions`, reuseExistingServer: false, timeout: 20_000 }]),
    { command: "node server/main.ts", url: `http://127.0.0.1:${port}/`, env: proxyEnv, reuseExistingServer: false, timeout: 20_000 },
  ],
});
