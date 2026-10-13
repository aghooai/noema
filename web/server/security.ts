import { randomBytes, timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import { CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE } from "../src/api/protocol.ts";

/** spec-09 §8, verbatim. */
export const CSP =
  "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; connect-src 'self'; " +
  "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

/** Set on every response of the proxy (static files, auth endpoints, proxied API responses). */
export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  "content-security-policy": CSP,
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
};

export function applySecurityHeaders(res: ServerResponse): void {
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.setHeader(k, v);
}

export function token(): string {
  return randomBytes(32).toString("base64url");
}

export function cookies(req: IncomingMessage): Map<string, string> {
  const out = new Map<string, string>();
  for (const part of (req.headers.cookie ?? "").split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0) out.set(part.slice(0, eq).trim(), part.slice(eq + 1).trim());
  }
  return out;
}

export function sameToken(a: string | undefined, b: string | undefined): boolean {
  if (a === undefined || b === undefined || a === "") return false;
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

/** The double-submit check: the x-csrf-token header equals the CSRF cookie (and, with a session, its token). */
export function csrfOk(req: IncomingMessage, sessionToken?: string): boolean {
  const header = req.headers[CSRF_HEADER];
  const sent = typeof header === "string" ? header : undefined;
  const cookie = cookies(req).get(CSRF_COOKIE);
  return sameToken(sent, cookie) && (sessionToken === undefined || sameToken(sent, sessionToken));
}

/** Loopback spellings of this machine accepted in Host, besides the configured NOEMA_UI_HOST. */
const LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "[::1]"];

/**
 * DNS-rebinding guard: the Host header must name this proxy, i.e. the configured host or a loopback spelling, with
 * the port the request arrived on (= NOEMA_UI_PORT, or the ephemeral port when it is 0).
 */
export function hostOk(req: IncomingMessage, configuredHost: string): boolean {
  const host = req.headers.host?.toLowerCase();
  const names = [configuredHost.includes(":") ? `[${configuredHost}]` : configuredHost, ...LOOPBACK_HOSTS];
  return host !== undefined && names.some((n) => host === `${n.toLowerCase()}:${req.socket.localPort}`);
}

/** Mutating requests must come from a page of this proxy: Origin is http(s):// plus the (checked) Host. */
export function originOk(req: IncomingMessage): boolean {
  const origin = req.headers.origin?.toLowerCase();
  const host = req.headers.host?.toLowerCase();
  return origin !== undefined && host !== undefined && (origin === `http://${host}` || origin === `https://${host}`);
}

export function sessionCookie(sid: string, secure: boolean): string {
  return `${SESSION_COOKIE}=${sid}; HttpOnly; SameSite=Strict; Path=/${secure ? "; Secure" : ""}`;
}

/** Readable by the SPA on purpose (it echoes it in x-csrf-token); SameSite=Strict like the session cookie. */
export function csrfCookie(value: string, secure: boolean): string {
  return `${CSRF_COOKIE}=${value}; SameSite=Strict; Path=/${secure ? "; Secure" : ""}`;
}

export function clearCookie(name: string, httpOnly: boolean): string {
  return `${name}=; Max-Age=0; SameSite=Strict; Path=/${httpOnly ? "; HttpOnly" : ""}`;
}

export interface BrowserSession {
  alias: string;
  csrf: string;
  lastSeen: number;
}

/** In-memory browser sessions (dev only): session id -> alias and CSRF token, with an idle TTL. */
export class Sessions {
  readonly #map = new Map<string, BrowserSession>();
  readonly #ttlMs: number;
  readonly #now: () => number;

  constructor(ttlMs: number, now: () => number = Date.now) {
    this.#ttlMs = ttlMs;
    this.#now = now;
  }

  create(alias: string): { sid: string; session: BrowserSession } {
    const sid = token();
    const session = { alias, csrf: token(), lastSeen: this.#now() };
    this.#map.set(sid, session);
    return { sid, session };
  }

  get(sid: string | undefined): BrowserSession | null {
    if (sid === undefined) return null;
    const s = this.#map.get(sid);
    if (s === undefined) return null;
    if (this.#now() - s.lastSeen > this.#ttlMs) {
      this.#map.delete(sid);
      return null;
    }
    s.lastSeen = this.#now();
    return s;
  }

  delete(sid: string | undefined): void {
    if (sid !== undefined) this.#map.delete(sid);
  }
}
