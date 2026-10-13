import { join } from "node:path";
import { test as base, expect, type Page } from "@playwright/test";
import { SECURITY_HEADERS } from "../server/security.ts";
import { MEDIA_DIR } from "./global-setup.ts";

/** Running against the integrator's stack (else the local fixture API). */
export const STACK = (process.env.NOEMA_UI_API_URL ?? "") !== "";
export const ALIAS = {
  chat: process.env.NOEMA_UI_E2E_ALIAS_CHAT ?? "developer",
  admin: process.env.NOEMA_UI_E2E_ALIAS_ADMIN ?? "tenant_admin",
  reviewer: process.env.NOEMA_UI_E2E_ALIAS_REVIEWER ?? "reviewer",
};
export const media = (name: string): string => join(MEDIA_DIR, name);
/** The stack's mockbedrock (state file `endpoints.mockbedrock`), for specs that load a scenario: set it only on your own stack. */
export const MOCK_URL = process.env.NOEMA_UI_E2E_MOCK_URL ?? "";
/** A prompt unique to this run on a stack (else the L2 decision cache answers a repeated input without member calls). */
export const unique = (text: string): string => (STACK ? `${text} [ui-e2e ${Date.now().toString(36)}]` : text);

interface Guard {
  cspViolations: string[];
  foreignRequests: string[];
  /** Same-origin responses (SPA files, /auth, proxied /v1) missing a spec-09 §8 header value. */
  missingHeaders: string[];
}

declare global {
  interface Window {
    __csp?: string[];
    __pwned?: unknown;
  }
}

/** Fails the test when the API key is visible to page scripts or stored anywhere in the browser. */
export async function expectNoKeyInStorage(page: Page): Promise<void> {
  const dump = await page.evaluate(async () => ({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
    idb: (await indexedDB.databases()).map((d) => d.name ?? ""),
    cookie: document.cookie,
  }));
  expect(dump.local).toEqual([]);
  expect(dump.session).toEqual([]);
  expect(dump.idb).toEqual([]);
  expect(dump.cookie).not.toContain("noema_session");
  expect(JSON.stringify(dump)).not.toMatch(/ak_(live|test)_/);
}

/**
 * Every test: SEC-UI-01 (0 `securitypolicyviolation` events), no request outside 'self', the spec-09 §8 headers on every
 * same-origin response (SPA files, /auth, proxied /v1 JSON, SSE and derivatives), and no API key in
 * localStorage, sessionStorage, IndexedDB or a script-visible cookie at the end.
 */
export const test = base.extend<{ guard: Guard }>({
  guard: [
    async ({ page, baseURL }, use) => {
      const g: Guard = { cspViolations: [], foreignRequests: [], missingHeaders: [] };
      const self = new URL(baseURL as string).origin;
      await page.addInitScript(() => {
        window.__csp = [];
        document.addEventListener("securitypolicyviolation", (e) => window.__csp?.push(`${e.violatedDirective} ${e.blockedURI}`));
      });
      page.on("console", (m) => {
        if (/Content.Security.Policy/i.test(m.text())) g.cspViolations.push(m.text());
      });
      page.on("request", (r) => {
        const u = new URL(r.url());
        if (u.protocol !== "blob:" && u.protocol !== "data:" && u.origin !== self) g.foreignRequests.push(r.url());
      });
      page.on("response", (r) => {
        const u = new URL(r.url());
        if (u.protocol !== "http:" || u.origin !== self) return; // blob: previews are local (and report the page origin)
        const h = r.headers();
        const bad = Object.entries(SECURITY_HEADERS).filter(([k, v]) => h[k] !== v).map(([k]) => k);
        if (bad.length > 0) g.missingHeaders.push(`${r.request().method()} ${r.url()} ${r.status()}: ${bad.join(", ")}`);
      });
      await use(g);
      const inPage = page.isClosed() ? [] : await page.evaluate(() => window.__csp ?? []);
      expect([...g.cspViolations, ...inPage], "CSP violations (SEC-UI-01)").toEqual([]);
      expect(g.foreignRequests, "requests outside 'self'").toEqual([]);
      expect(g.missingHeaders, "responses without the spec-09 §8 CSP, Referrer-Policy and nosniff").toEqual([]);
      if (!page.isClosed() && page.url().startsWith(self)) await expectNoKeyInStorage(page);
    },
    { auto: true },
  ],
});

export { expect };

export async function login(page: Page, alias: string): Promise<void> {
  await page.goto("/");
  await page.getByTestId(`login-${alias}`).click();
  await expect(page.getByTestId("who")).toContainText(alias);
}

/** Creates a session and streams one turn in it; resolves when the turn is complete (Send is back). */
export async function chatTurn(page: Page, text: string): Promise<void> {
  await page.goto("/#/chat");
  await page.getByTestId("new-session").click();
  await expect(page.getByTestId("message-input")).toBeEnabled();
  await page.getByTestId("message-input").fill(text);
  await page.getByTestId("send").click();
  await expect(page.getByTestId("cancel")).toBeHidden({ timeout: 60_000 });
  await expect(page.getByTestId("message-assistant").last()).toBeVisible();
}
