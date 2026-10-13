/**
 * The dev cookie proxy's browser-facing protocol (spec-09 §2 "Dev login", §8), shared by server/ and the SPA.
 * The session cookie is HttpOnly (the SPA never sees it); the CSRF cookie is readable on purpose (double submit).
 */
export const SESSION_COOKIE = "noema_session";
export const CSRF_COOKIE = "noema_csrf";
export const CSRF_HEADER = "x-csrf-token";

/** GET /auth/session, POST /auth/login {alias}, POST /auth/logout. */
export const AUTH_PATHS = { session: "/auth/session", login: "/auth/login", logout: "/auth/logout" } as const;

export interface AuthState {
  authenticated: boolean;
  /** The server-side key alias this browser session uses (the key itself never leaves the proxy). */
  alias: string | null;
  /** Aliases a developer may log in as (configured server side). */
  aliases: string[];
}

/** Requests that need the CSRF header (everything that can change state). */
export function isMutating(method: string): boolean {
  const m = method.toUpperCase();
  return m !== "GET" && m !== "HEAD" && m !== "OPTIONS";
}
