import { CSP } from "../server/security.ts";
import { ALIAS, expect, expectNoKeyInStorage, login, test } from "./support.ts";

test.describe("UI security (spec-09 §8)", () => {
  test("the document and API responses carry the CSP, Referrer-Policy and nosniff", async ({ page, request }) => {
    const doc = await request.get("/");
    expect(doc.headers()["content-security-policy"]).toBe(CSP);
    expect(doc.headers()["referrer-policy"]).toBe("no-referrer");
    expect(doc.headers()["x-content-type-options"]).toBe("nosniff");
    await login(page, ALIAS.chat);
    const api = await page.request.get("/v1/sessions?limit=1");
    expect(api.status()).toBe(200);
    expect(api.headers()["content-security-policy"]).toBe(CSP);
  });

  test("SEC-UI-03: a mutating request without the CSRF token is refused with 403", async ({ page }) => {
    await login(page, ALIAS.chat);
    const statuses = await page.evaluate(async () => {
      const csrf = /(?:^|; )noema_csrf=([^;]+)/.exec(document.cookie)?.[1] ?? "";
      const post = (headers: Record<string, string>) =>
        fetch("/v1/sessions", { method: "POST", headers: { "content-type": "application/json", "aghoo-version": "2026-10-01", ...headers }, body: "{}" }).then((r) => r.status);
      return { without: await post({}), wrong: await post({ "x-csrf-token": "forged" }), with: await post({ "x-csrf-token": csrf }) };
    });
    expect(statuses).toEqual({ without: 403, wrong: 403, with: 201 });
  });

  test("the session cookie is HttpOnly and SameSite=Strict; no API key reaches browser storage", async ({ page, context }) => {
    await login(page, ALIAS.chat);
    const cookies = await context.cookies();
    const session = cookies.find((c) => c.name === "noema_session");
    expect(session).toMatchObject({ httpOnly: true, sameSite: "Strict", path: "/" });
    expect(cookies.find((c) => c.name === "noema_csrf")).toMatchObject({ httpOnly: false, sameSite: "Strict" });
    expect(JSON.stringify(cookies)).not.toMatch(/ak_(live|test)_/);
    await expectNoKeyInStorage(page);
  });
});
