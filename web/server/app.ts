import { request as httpRequest, type IncomingMessage, type ServerResponse } from "node:http";
import { request as httpsRequest } from "node:https";
import { AUTH_PATHS, type AuthState, CSRF_COOKIE, isMutating, SESSION_COOKIE } from "../src/api/protocol.ts";
import type { ProxyConfig } from "./config.ts";
import { applySecurityHeaders, clearCookie, cookies, csrfCookie, csrfOk, hostOk, originOk, SECURITY_HEADERS, Sessions, sessionCookie, token } from "./security.ts";
import { serveStatic } from "./static.ts";

/** Request headers passed to the API (an allowlist: cookies, CSRF tokens and any browser Authorization are dropped). */
const FORWARD = [
  "accept", "content-type", "content-length", "transfer-encoding", "idempotency-key", "aghoo-version",
  "aghoo-deadline-ms", "last-event-id", "x-request-id", "traceparent", "tracestate", "x-aghoo-end-user", "x-aghoo-include",
];
/** Response headers never passed back to the browser. */
const DROP_RESPONSE = new Set(["connection", "keep-alive", "transfer-encoding", "set-cookie", "upgrade", "proxy-authenticate"]);

function json(res: ServerResponse, status: number, body: unknown, headers: Record<string, string | string[]> = {}): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...headers });
  res.end(text);
}

/** Errors the proxy itself answers, shaped like the API's envelope so the SDK parses them as typed errors. */
function envelope(res: ServerResponse, status: number, type: string, code: string, message: string, retryable = false): void {
  const request_id = `req_proxy_${token().slice(0, 12)}`;
  json(res, status, { error: { type, code, message, param: null, request_id, retryable } }, { "x-request-id": request_id });
}

async function readJson(req: IncomingMessage, max: number): Promise<unknown> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const c of req as AsyncIterable<Buffer>) {
    size += c.length;
    if (size > max) throw new Error("body too large");
    chunks.push(c);
  }
  return chunks.length === 0 ? {} : JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

/** One proxied API response, for the e2e contract check (web/e2e/validate_recording.py). */
export interface Recorded {
  method: string;
  path: string;
  status: number;
  headers: Record<string, string>;
  /** Base64 body for JSON and SSE responses (binary derivatives are not kept: only their content type is checked). */
  body_b64: string;
}

const RECORDED_TYPES = /^(application\/json|text\/event-stream)/;

/** The dev cookie proxy: static SPA, dev login, and /v1 forwarded with the session's API key added server side. */
export function createHandler(
  config: ProxyConfig,
  sessions = new Sessions(config.sessionTtlMs),
  record: ((r: Recorded) => void) | null = null,
) {
  const aliases = [...config.keys.keys()].sort();
  const state = (alias: string | null): AuthState => ({ authenticated: alias !== null, alias, aliases });

  async function auth(req: IncomingMessage, res: ServerResponse, path: string): Promise<void> {
    const jar = cookies(req);
    const sid = jar.get(SESSION_COOKIE);
    if (path === AUTH_PATHS.session && req.method === "GET") {
      const s = sessions.get(sid);
      const headers: Record<string, string> = {};
      if (s === null && jar.get(CSRF_COOKIE) === undefined) headers["set-cookie"] = csrfCookie(token(), config.secureCookies);
      json(res, 200, state(s?.alias ?? null), headers);
      return;
    }
    if (req.method !== "POST" || (path !== AUTH_PATHS.login && path !== AUTH_PATHS.logout)) {
      envelope(res, 404, "not_found_error", "not_found", "Unknown auth endpoint.");
      return;
    }
    if (!originOk(req)) {
      envelope(res, 403, "permission_error", "permission_denied", "Origin missing or not this proxy.");
      return;
    }
    if (!csrfOk(req)) {
      envelope(res, 403, "permission_error", "permission_denied", "CSRF token missing or invalid.");
      return;
    }
    if (path === AUTH_PATHS.logout) {
      sessions.delete(sid);
      json(res, 200, state(null), { "set-cookie": [clearCookie(SESSION_COOKIE, true), csrfCookie(token(), config.secureCookies)] });
      return;
    }
    let alias: unknown;
    try {
      alias = ((await readJson(req, 1024)) as { alias?: unknown }).alias;
    } catch {
      envelope(res, 400, "invalid_request_error", "schema_violation", "Expected a JSON body {\"alias\": ...}.");
      return;
    }
    if (typeof alias !== "string" || !config.keys.has(alias)) {
      envelope(res, 401, "authentication_error", "invalid_api_key", "Unknown key alias.");
      return;
    }
    sessions.delete(sid);
    const created = sessions.create(alias);
    json(res, 200, state(alias), {
      "set-cookie": [sessionCookie(created.sid, config.secureCookies), csrfCookie(created.session.csrf, config.secureCookies)],
    });
  }

  function proxy(req: IncomingMessage, res: ServerResponse): void {
    const session = sessions.get(cookies(req).get(SESSION_COOKIE));
    if (session === null) {
      envelope(res, 401, "authentication_error", "invalid_api_key", "Not logged in to the dev proxy.");
      return;
    }
    if (isMutating(req.method ?? "GET") && !originOk(req)) {
      envelope(res, 403, "permission_error", "permission_denied", "Origin missing or not this proxy.");
      return;
    }
    if (isMutating(req.method ?? "GET") && !csrfOk(req, session.csrf)) {
      envelope(res, 403, "permission_error", "permission_denied", "CSRF token missing or invalid.");
      return;
    }
    const headers: Record<string, string | string[]> = { authorization: `Bearer ${config.keys.get(session.alias) as string}` };
    for (const h of FORWARD) {
      const v = req.headers[h];
      if (v !== undefined) headers[h] = v;
    }
    const send = config.apiUrl.startsWith("https:") ? httpsRequest : httpRequest;
    const upstream = send(config.apiUrl + (req.url as string), { method: req.method, headers }, (up) => {
      const out: Record<string, string | string[]> = {};
      // The proxy's spec-09 §8 headers (set before) always win over upstream values of the same name.
      for (const [k, v] of Object.entries(up.headers)) if (v !== undefined && !DROP_RESPONSE.has(k) && !(k in SECURITY_HEADERS)) out[k] = v;
      res.writeHead(up.statusCode ?? 502, out);
      res.flushHeaders();
      if (record !== null) {
        const keep = RECORDED_TYPES.test(String(up.headers["content-type"] ?? ""));
        const chunks: Buffer[] = [];
        if (keep) up.on("data", (c: Buffer) => chunks.push(c));
        let done = false;
        // A stream the browser closes early (the SDK stops reading a file watch at its terminal snapshot) is recorded
        // up to its last complete SSE frame.
        const flush = (complete: boolean): void => {
          if (done) return;
          done = true;
          const headers: Record<string, string> = {};
          for (const [k, v] of Object.entries(up.headers)) if (typeof v === "string") headers[k] = v;
          let body = Buffer.concat(chunks);
          if (!complete) {
            if (!String(up.headers["content-type"]).startsWith("text/event-stream")) return; // a cut JSON body proves nothing
            const cut = body.lastIndexOf("\n\n");
            body = cut < 0 ? Buffer.alloc(0) : body.subarray(0, cut + 2);
          }
          const path = (req.url as string).split("?")[0] as string;
          record({ method: req.method ?? "GET", path, status: up.statusCode ?? 0, headers, body_b64: body.toString("base64") });
        };
        up.on("end", () => flush(true));
        up.on("close", () => flush(false));
      }
      up.pipe(res);
    });
    upstream.on("error", () => {
      if (!res.headersSent) envelope(res, 502, "server_error", "internal_error", "The Noema API is not reachable from the dev proxy.", true);
      else res.destroy();
    });
    res.on("close", () => upstream.destroy());
    req.pipe(upstream);
  }

  return (req: IncomingMessage, res: ServerResponse): void => {
    applySecurityHeaders(res);
    if (!hostOk(req, config.host)) {
      envelope(res, 403, "permission_error", "permission_denied", "Host not allowed (the dev proxy answers only its own host:port).");
      return;
    }
    const url = req.url ?? "/";
    const path = url.split("?")[0] as string;
    if (path === "/v1" || path.startsWith("/v1/")) {
      proxy(req, res);
      return;
    }
    const done = path.startsWith("/auth/") ? auth(req, res, path) : serveStatic(config.distDir, req, res, path);
    done.catch(() => {
      if (!res.headersSent) envelope(res, 500, "server_error", "internal_error", "Dev proxy error.");
      else res.destroy();
    });
  };
}
