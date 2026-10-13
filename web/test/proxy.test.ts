import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer, type IncomingHttpHeaders, request, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createHandler, type Recorded } from "../server/app";
import { ConfigError, configFromEnv, parseKeys, type ProxyConfig } from "../server/config";
import { CSP } from "../server/security";

const KEY = `ak_test_01JA00000000000000000000AA.${"s".repeat(43)}`;
const KEY2 = `ak_test_01JA00000000000000000000BB.${"t".repeat(43)}`;

interface Seen {
  method: string;
  url: string;
  headers: IncomingHttpHeaders;
  body: string;
}

const seen: Seen[] = [];
const recorded: Recorded[] = [];
let upstream: Server;
let proxy: Server;
let base = "";
let release: () => void = () => undefined;

function listen(s: Server): Promise<string> {
  return new Promise((r) => s.listen(0, "127.0.0.1", () => r(`http://127.0.0.1:${(s.address() as AddressInfo).port}`)));
}

beforeAll(async () => {
  upstream = createServer((req, res) => {
    let body = "";
    req.on("data", (c: Buffer) => (body += c.toString()));
    req.on("end", () => {
      seen.push({ method: req.method ?? "", url: req.url ?? "", headers: req.headers, body });
      if (req.url?.endsWith("/events")) {
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.write('id: 1\nevent: file.updated\ndata: {"status":"ready"}\n\nid: 2\nevent: file.upd');
        return;
      }
      if (req.url?.startsWith("/v1/turns") && req.method === "POST") {
        res.writeHead(200, { "content-type": "text/event-stream", "set-cookie": "upstream=1" });
        res.write('id: 1\nevent: turn.created\ndata: {"type":"turn.created"}\n\n');
        new Promise<void>((r) => (release = r)).then(() => res.end('id: 2\nevent: turn.completed\ndata: {"type":"turn.completed"}\n\n'));
        return;
      }
      res.writeHead(200, { "content-type": "application/json", "x-request-id": "req_up", "content-security-policy": "default-src *" });
      res.end(JSON.stringify({ data: [], has_more: false, next_cursor: null }));
    });
  });
  const apiUrl = await listen(upstream);
  const top = mkdtempSync(join(tmpdir(), "noema-web-"));
  const dist = join(top, "dist");
  mkdirSync(join(dist, "assets"), { recursive: true });
  writeFileSync(join(top, "secret.js"), "secret");
  writeFileSync(join(dist, "index.html"), "<!doctype html><title>t</title>");
  writeFileSync(join(dist, "assets", "app.js"), "export {}");
  writeFileSync(join(dist, "assets", "x.svg"), "<svg/>");
  const config: ProxyConfig = { apiUrl, keys: new Map([["developer", KEY], ["reviewer", KEY2]]), distDir: dist, host: "127.0.0.1", port: 0, secureCookies: false, sessionTtlMs: 60_000, recordFile: null };
  proxy = createServer(createHandler(config, undefined, (r) => recorded.push(r)));
  base = await listen(proxy);
});

afterAll(() => {
  proxy.closeAllConnections();
  upstream.closeAllConnections();
  proxy.close();
  upstream.close();
});

function setCookies(res: Response): string[] {
  return res.headers.getSetCookie();
}

function jar(cookies: string[]): string {
  return cookies.map((c) => c.split(";")[0]).join("; ");
}

/** node:http request (fetch cannot set Host): the status and body of one request with the given headers. */
function raw(path: string, headers: Record<string, string>): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const r = request(`${base}${path}`, { headers }, (res) => {
      let body = "";
      res.on("data", (c: Buffer) => (body += c.toString()));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
    });
    r.on("error", reject);
    r.end();
  });
}

async function login(alias = "developer"): Promise<{ cookie: string; csrf: string }> {
  const first = await fetch(`${base}/auth/session`);
  const pre = jar(setCookies(first));
  const csrf0 = pre.split("=")[1] as string;
  const res = await fetch(`${base}/auth/login`, { method: "POST", headers: { cookie: pre, "x-csrf-token": csrf0, origin: base, "content-type": "application/json" }, body: JSON.stringify({ alias }) });
  expect(res.status).toBe(200);
  const cookie = jar(setCookies(res));
  const csrf = /noema_csrf=([^;]+)/.exec(cookie)?.[1] as string;
  return { cookie, csrf };
}

describe("dev cookie proxy (spec-09 §2, §8)", () => {
  it("refuses to start unless NOEMA_ENV=dev, and refuses malformed keys", () => {
    for (const env of [{}, { NOEMA_ENV: "test" }, { NOEMA_ENV: "prod" }, { NOEMA_ENV: "staging" }]) expect(() => configFromEnv(env)).toThrow(ConfigError);
    expect(() => configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_API_KEYS: "dev=not-a-key" })).toThrow(/not an ak_test_/);
    expect(() => configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_API_KEYS: `Bad Alias=${KEY}` })).toThrow(/invalid alias/);
    const c = configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_API_KEYS: `developer=${KEY}`, NOEMA_UI_API_URL: "http://127.0.0.1:9/ignored" });
    expect([...c.keys.keys()]).toEqual(["developer"]);
    expect(c.apiUrl).toBe("http://127.0.0.1:9");
    expect([...parseKeys(JSON.stringify({ a: KEY, b: KEY2 }), "f")]).toEqual([["a", KEY], ["b", KEY2]]);
    expect([...parseKeys(`# keys\na=${KEY}\nb=${KEY2}\n`, "f").keys()]).toEqual(["a", "b"]);
  });

  it("dev login: a readable SameSite=Strict CSRF cookie, then an HttpOnly SameSite=Strict session cookie; the key never leaves", async () => {
    const first = await fetch(`${base}/auth/session`);
    const body = await first.text();
    expect(JSON.parse(body)).toEqual({ authenticated: false, alias: null, aliases: ["developer", "reviewer"] });
    const pre = setCookies(first);
    expect(pre).toHaveLength(1);
    expect(pre[0]).toMatch(/^noema_csrf=[\w-]{43}; SameSite=Strict; Path=\/$/);
    const noCsrf = await fetch(`${base}/auth/login`, { method: "POST", headers: { cookie: jar(pre), origin: base }, body: '{"alias":"developer"}' });
    expect(noCsrf.status).toBe(403);
    const csrf0 = jar(pre).split("=")[1] as string;
    const ok = await fetch(`${base}/auth/login`, { method: "POST", headers: { cookie: jar(pre), "x-csrf-token": csrf0, origin: base }, body: '{"alias":"developer"}' });
    const text = await ok.text();
    expect(JSON.parse(text)).toMatchObject({ authenticated: true, alias: "developer" });
    const sc = setCookies(ok);
    expect(sc.find((c) => c.startsWith("noema_session="))).toMatch(/^noema_session=[\w-]{43}; HttpOnly; SameSite=Strict; Path=\/$/);
    expect(sc.find((c) => c.startsWith("noema_csrf="))).not.toContain(csrf0);
    for (const t of [body, text, ...pre, ...sc, JSON.stringify([...ok.headers])]) expect(t).not.toContain(KEY.slice(0, 20));
    const unknown = await fetch(`${base}/auth/login`, { method: "POST", headers: { cookie: jar(pre), "x-csrf-token": csrf0, origin: base }, body: '{"alias":"root"}' });
    expect(unknown.status).toBe(401);
  });

  it("/v1 without a session is 401 (typed envelope) and never reaches the API", async () => {
    const n = seen.length;
    const res = await fetch(`${base}/v1/sessions`);
    expect(res.status).toBe(401);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe("invalid_api_key");
    expect(seen.length).toBe(n);
  });

  it("forwards GETs with the session's key; strips cookies, CSRF and any browser Authorization; adds the CSP", async () => {
    const { cookie } = await login("reviewer");
    const res = await fetch(`${base}/v1/admin/calibrators?limit=5`, { headers: { cookie, authorization: "Bearer ak_live_stolen", "aghoo-version": "2026-10-01", "x-evil": "1" } });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-security-policy")).toBe(CSP);
    expect(res.headers.get("referrer-policy")).toBe("no-referrer");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    const up = seen.at(-1) as Seen;
    expect(up.url).toBe("/v1/admin/calibrators?limit=5");
    expect(up.headers.authorization).toBe(`Bearer ${KEY2}`);
    expect(up.headers["aghoo-version"]).toBe("2026-10-01");
    expect(up.headers.cookie).toBeUndefined();
    expect(up.headers["x-csrf-token"]).toBeUndefined();
    expect(up.headers["x-evil"]).toBeUndefined();
    expect(recorded.at(-1)).toMatchObject({ method: "GET", path: "/v1/admin/calibrators", status: 200 });
  });

  it("SEC-UI-03: a mutating request without (or with a wrong) CSRF token is 403 and not forwarded", async () => {
    const { cookie, csrf } = await login();
    const n = seen.length;
    for (const headers of [{ cookie, origin: base }, { cookie, origin: base, "x-csrf-token": "wrong" }, { cookie: cookie.replace(/noema_csrf=[^;]+/, "noema_csrf=wrong"), origin: base, "x-csrf-token": "wrong" }]) {
      for (const method of ["POST", "DELETE", "PATCH"]) {
        const res = await fetch(`${base}/v1/sessions/ses_1`, { method, headers });
        expect(res.status).toBe(403);
        expect(((await res.json()) as { error: { code: string } }).error.code).toBe("permission_denied");
      }
    }
    expect(seen.length).toBe(n);
    const ok = await fetch(`${base}/v1/sessions/ses_1`, { method: "DELETE", headers: { cookie, "x-csrf-token": csrf, origin: base } });
    expect(ok.status).toBe(200);
    expect(seen.length).toBe(n + 1);
  });

  it("streams SSE through unbuffered, drops upstream Set-Cookie, and forwards the request body", async () => {
    const { cookie, csrf } = await login();
    const res = await fetch(`${base}/v1/turns`, { method: "POST", headers: { cookie, "x-csrf-token": csrf, origin: base, "content-type": "application/json" }, body: '{"input":[]}' });
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.headers.get("set-cookie")).toBeNull();
    const reader = (res.body as ReadableStream<Uint8Array>).getReader();
    const first = new TextDecoder().decode((await reader.read()).value);
    expect(first).toContain("turn.created");
    release();
    let rest = "";
    for (;;) {
      const r = await reader.read();
      if (r.done) break;
      rest += new TextDecoder().decode(r.value);
    }
    expect(rest).toContain("turn.completed");
    expect(seen.at(-1)?.body).toBe('{"input":[]}');
    expect(Buffer.from(recorded.at(-1)?.body_b64 ?? "", "base64").toString()).toContain("turn.completed");
  });

  it("records a stream the browser closes early up to its last complete frame", async () => {
    const { cookie } = await login();
    const ctl = new AbortController();
    const res = await fetch(`${base}/v1/files/file_1/events`, { headers: { cookie }, signal: ctl.signal });
    const reader = (res.body as ReadableStream<Uint8Array>).getReader();
    await reader.read();
    const n = recorded.length;
    ctl.abort();
    await expect.poll(() => recorded.length).toBe(n + 1);
    expect(recorded.at(-1)?.path).toBe("/v1/files/file_1/events");
    expect(Buffer.from(recorded.at(-1)?.body_b64 ?? "", "base64").toString()).toBe('id: 1\nevent: file.updated\ndata: {"status":"ready"}\n\n');
  });

  it("logout ends the session", async () => {
    const { cookie, csrf } = await login();
    const out = await fetch(`${base}/auth/logout`, { method: "POST", headers: { cookie, "x-csrf-token": csrf, origin: base } });
    expect(out.status).toBe(200);
    expect(setCookies(out).some((c) => c.startsWith("noema_session=;") && c.includes("Max-Age=0"))).toBe(true);
    expect((await fetch(`${base}/v1/sessions`, { headers: { cookie } })).status).toBe(401);
  });

  it("serves only the built SPA: index with the CSP, assets, no traversal, no SVG, no other methods", async () => {
    const index = await fetch(`${base}/`);
    expect(index.status).toBe(200);
    expect(index.headers.get("content-security-policy")).toBe(CSP);
    expect(index.headers.get("cache-control")).toBe("no-store");
    expect((await fetch(`${base}/assets/app.js`)).headers.get("content-type")).toBe("text/javascript; charset=utf-8");
    expect((await fetch(`${base}/assets/x.svg`)).status).toBe(404);
    expect((await fetch(`${base}/missing.js`)).status).toBe(404);
    expect((await fetch(`${base}/%E0%A4%A`)).status).toBe(404);
    expect((await fetch(`${base}/`, { method: "POST" })).status).toBe(405);
    for (const path of ["/..%2Fsecret.js", "/assets/..%2F..%2Fsecret.js", "/../secret.js", "/assets/%2e%2e/%2e%2e/secret.js"]) {
      const raw = await new Promise<number>((resolve) => {
        request(`${base}${path}`, (r) => resolve(r.statusCode ?? 0)).end();
      });
      expect(raw, path).toBe(404);
    }
  });

  it("DNS-rebinding guard: a Host other than this proxy's host:port is 403 on every path and never reaches the API", async () => {
    const { cookie } = await login();
    const port = new URL(base).port;
    const n = seen.length;
    for (const host of [`attacker.example:${port}`, `127.0.0.1.attacker.example:${port}`, "127.0.0.1:1", "127.0.0.1"]) {
      for (const path of ["/", "/auth/session", "/v1/sessions"]) {
        const r = await raw(path, { host, cookie });
        expect(r.status, `${host} ${path}`).toBe(403);
        expect(r.body).not.toContain("developer");
        expect(r.body).toContain("permission_denied");
      }
    }
    expect(seen.length).toBe(n);
    for (const host of [`127.0.0.1:${port}`, `localhost:${port}`, `LOCALHOST:${port}`, `[::1]:${port}`]) {
      expect((await raw("/v1/sessions", { host, cookie })).status, host).toBe(200);
    }
    expect(seen.length).toBe(n + 4);
  });

  it("POST /auth/* and mutating /v1 calls need this proxy's Origin (403 otherwise, not forwarded)", async () => {
    const pre = jar(setCookies(await fetch(`${base}/auth/session`)));
    const csrf0 = pre.split("=")[1] as string;
    const bad: (string | undefined)[] = [undefined, "null", `http://attacker.example:${new URL(base).port}`, "http://127.0.0.1:1", `${base}.attacker.example`];
    for (const origin of bad) {
      const headers: Record<string, string> = { cookie: pre, "x-csrf-token": csrf0, ...(origin === undefined ? {} : { origin }) };
      const res = await fetch(`${base}/auth/login`, { method: "POST", headers, body: '{"alias":"developer"}' });
      expect(res.status, String(origin)).toBe(403);
      expect(setCookies(res).some((c) => c.startsWith("noema_session="))).toBe(false);
    }
    const { cookie, csrf } = await login();
    const n = seen.length;
    for (const origin of bad) {
      const headers: Record<string, string> = { cookie, "x-csrf-token": csrf, ...(origin === undefined ? {} : { origin }) };
      expect((await fetch(`${base}/v1/sessions/ses_1`, { method: "DELETE", headers })).status, String(origin)).toBe(403);
      expect((await fetch(`${base}/auth/logout`, { method: "POST", headers })).status, String(origin)).toBe(403);
    }
    expect(seen.length).toBe(n);
    expect((await fetch(`${base}/v1/sessions`, { headers: { cookie } })).status).toBe(200); // still logged in; GET needs no Origin
    const ok = await fetch(`${base}/v1/sessions/ses_1`, { method: "DELETE", headers: { cookie, "x-csrf-token": csrf, origin: base.replace("127.0.0.1", "localhost") } });
    expect(ok.status).toBe(403); // Origin must name the Host the request was sent to
    expect((await fetch(`${base}/v1/sessions/ses_1`, { method: "DELETE", headers: { cookie, "x-csrf-token": csrf, origin: base } })).status).toBe(200);
  });

  it("refuses a non-loopback NOEMA_UI_HOST unless NOEMA_UI_ALLOW_NON_LOOPBACK=1", () => {
    for (const host of ["127.0.0.1", "127.0.0.2", "localhost", "::1"]) expect(configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_HOST: host }).host).toBe(host);
    expect(configFromEnv({ NOEMA_ENV: "dev" }).host).toBe("127.0.0.1");
    for (const host of ["0.0.0.0", "::", "192.168.1.5", "dev.example.com", "127.0.0.1.example.com"]) {
      expect(() => configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_HOST: host }), host).toThrow(/not a loopback address/);
      expect(configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_HOST: host, NOEMA_UI_ALLOW_NON_LOOPBACK: "1" }).host).toBe(host);
    }
  });

  it("refuses ak_live_ keys: the dev proxy holds ak_test_ keys only", () => {
    const live = KEY.replace("ak_test_", "ak_live_");
    expect(() => configFromEnv({ NOEMA_ENV: "dev", NOEMA_UI_API_KEYS: `developer=${live}` })).toThrow(/ak_live_ key/);
    expect(() => parseKeys(JSON.stringify({ developer: live }), "f")).toThrow(ConfigError);
    expect([...parseKeys(`developer=${KEY}`, "f").keys()]).toEqual(["developer"]);
  });

  it("an unreachable API is a typed 502", async () => {
    const dead: ProxyConfig = { apiUrl: "http://127.0.0.1:9", keys: new Map([["developer", KEY]]), distDir: "/nonexistent", host: "127.0.0.1", port: 0, secureCookies: false, sessionTtlMs: 60_000, recordFile: null };
    const s = createServer(createHandler(dead));
    const url = await listen(s);
    const first = await fetch(`${url}/auth/session`);
    const pre = jar(setCookies(first));
    const login2 = await fetch(`${url}/auth/login`, { method: "POST", headers: { cookie: pre, "x-csrf-token": pre.split("=")[1] as string, origin: url }, body: '{"alias":"developer"}' });
    const res = await fetch(`${url}/v1/sessions`, { headers: { cookie: jar(setCookies(login2)) } });
    expect(res.status).toBe(502);
    expect(((await res.json()) as { error: { retryable: boolean } }).error.retryable).toBe(true);
    s.close();
  });
});
