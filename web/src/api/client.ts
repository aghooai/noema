import { AghooClient } from "@aghoo/sdk";
import { AUTH_PATHS, type AuthState, CSRF_COOKIE, CSRF_HEADER, isMutating } from "./protocol";

/** The value of one cookie from a `document.cookie` string. */
export function readCookie(name: string, cookies: string): string | null {
  for (const part of cookies.split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq).trim() === name) return decodeURIComponent(part.slice(eq + 1).trim());
  }
  return null;
}

/**
 * fetch for the SPA: same-origin cookies only (the proxy's HttpOnly session cookie authenticates), and the
 * double-submit CSRF header on every mutating request. No Authorization header is ever set in the browser.
 */
export function csrfFetch(csrfToken: () => string | null, base: typeof fetch): typeof fetch {
  return (input, init = {}) => {
    const headers = new Headers(init.headers);
    headers.delete("authorization");
    const method = init.method ?? (input instanceof Request ? input.method : "GET");
    const token = csrfToken();
    if (isMutating(method) && token !== null) headers.set(CSRF_HEADER, token);
    return base(input, { ...init, headers, credentials: "same-origin" });
  };
}

export interface Api {
  client: AghooClient;
  auth: {
    session(): Promise<AuthState>;
    login(alias: string): Promise<AuthState>;
    logout(): Promise<AuthState>;
  };
}

/**
 * The API as the SPA sees it: the SDK pointed at the proxy origin (no API key: the proxy adds it server side) and the
 * proxy's dev-login endpoints.
 */
export function createApi(opts: { origin: string; fetch: typeof fetch; cookies: () => string }): Api {
  const f = csrfFetch(() => readCookie(CSRF_COOKIE, opts.cookies()), opts.fetch);
  const client = new AghooClient({ baseURL: opts.origin, fetch: f, maxRetries: 1 });
  const call = async (path: string, method: "GET" | "POST", body?: unknown): Promise<AuthState> => {
    const init: RequestInit =
      body === undefined
        ? { method, headers: { accept: "application/json" } }
        : { method, headers: { accept: "application/json", "content-type": "application/json" }, body: JSON.stringify(body) };
    const res = await f(opts.origin + path, init);
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return (await res.json()) as AuthState;
  };
  return {
    client,
    auth: {
      session: () => call(AUTH_PATHS.session, "GET"),
      login: (alias) => call(AUTH_PATHS.login, "POST", { alias }),
      logout: () => call(AUTH_PATHS.logout, "POST", {}),
    },
  };
}

/** The browser wiring (window origin, global fetch, document.cookie). */
export function browserApi(): Api {
  return createApi({
    origin: window.location.origin,
    fetch: (input, init) => globalThis.fetch(input, init),
    cookies: () => document.cookie,
  });
}
