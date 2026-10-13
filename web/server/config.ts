import { readFileSync } from "node:fs";
import { isIPv4 } from "node:net";
import { isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** Settings of the dev cookie proxy (spec-09 §2 "Dev login"). */
export interface ProxyConfig {
  /** Upstream Noema API origin, e.g. http://127.0.0.1:8000 (no trailing slash). */
  apiUrl: string;
  /** alias -> API key; the keys never leave this process. */
  keys: ReadonlyMap<string, string>;
  /** The built SPA (vite build output). */
  distDir: string;
  host: string;
  port: number;
  /** Add `Secure` to the cookies (needs https in front of the proxy). */
  secureCookies: boolean;
  /** Idle lifetime of a browser session (ms). */
  sessionTtlMs: number;
  /** NOEMA_UI_RECORD: append every proxied API response to this JSONL file (e2e contract check). */
  recordFile: string | null;
}

export class ConfigError extends Error {}

/** spec-09 §2 key format, test keys only (a dev-only proxy never holds a live key); anything else is refused at startup. */
const KEY_RE = /^ak_test_[0-9A-HJKMNP-TV-Z]{26}\.[A-Za-z0-9_-]{43}$/;
const ALIAS_RE = /^[a-z][a-z0-9_-]{0,31}$/;

/** `alias=key` pairs separated by commas or newlines, or a JSON object {alias: key}. */
export function parseKeys(text: string, source: string): Map<string, string> {
  const out = new Map<string, string>();
  const trimmed = text.trim();
  let pairs: [string, unknown][];
  if (trimmed.startsWith("{")) {
    pairs = Object.entries(JSON.parse(trimmed) as Record<string, unknown>);
  } else {
    pairs = trimmed
      .split(/[,\n]/)
      .map((p) => p.trim())
      .filter((p) => p !== "" && !p.startsWith("#"))
      .map((p) => {
        const eq = p.indexOf("=");
        return eq < 0 ? ["", p] : [p.slice(0, eq).trim(), p.slice(eq + 1).trim()];
      });
  }
  for (const [alias, key] of pairs) {
    if (!ALIAS_RE.test(alias)) throw new ConfigError(`${source}: invalid alias ${JSON.stringify(alias)} (expected ${ALIAS_RE})`);
    if (typeof key === "string" && key.startsWith("ak_live_")) throw new ConfigError(`${source}: the key for ${alias} is an ak_live_ key; the dev proxy accepts ak_test_ keys only`);
    if (typeof key !== "string" || !KEY_RE.test(key)) throw new ConfigError(`${source}: the key for ${alias} is not an ak_test_ API key`);
    out.set(alias, key);
  }
  return out;
}

const HERE = fileURLToPath(new URL(".", import.meta.url));

/**
 * Reads the proxy settings from the environment. Refuses to start unless NOEMA_ENV=dev (spec-09 §2).
 *   NOEMA_UI_API_URL        upstream API (default http://127.0.0.1:8000)
 *   NOEMA_UI_API_KEYS       alias=key[,alias=key...]
 *   NOEMA_UI_API_KEYS_FILE  a file with the same format or a JSON object (merged after NOEMA_UI_API_KEYS)
 *   NOEMA_UI_HOST / NOEMA_UI_PORT (default 127.0.0.1:5180; a non-loopback host needs NOEMA_UI_ALLOW_NON_LOOPBACK=1),
 *   NOEMA_UI_DIST, NOEMA_UI_SECURE_COOKIES=1,
 *   NOEMA_UI_RECORD (a JSONL file that receives every proxied API response, for the e2e contract check)
 */
export function configFromEnv(env: Readonly<Record<string, string | undefined>>): ProxyConfig {
  if (env.NOEMA_ENV !== "dev") {
    throw new ConfigError(`the dev cookie proxy runs only with NOEMA_ENV=dev (got ${JSON.stringify(env.NOEMA_ENV ?? "")})`);
  }
  const apiUrl = new URL(env.NOEMA_UI_API_URL ?? "http://127.0.0.1:8000");
  if (apiUrl.protocol !== "http:" && apiUrl.protocol !== "https:") throw new ConfigError("NOEMA_UI_API_URL must be http(s)");
  const keys = new Map<string, string>();
  if (env.NOEMA_UI_API_KEYS) for (const [a, k] of parseKeys(env.NOEMA_UI_API_KEYS, "NOEMA_UI_API_KEYS")) keys.set(a, k);
  if (env.NOEMA_UI_API_KEYS_FILE) {
    const file = env.NOEMA_UI_API_KEYS_FILE;
    for (const [a, k] of parseKeys(readFileSync(file, "utf8"), file)) keys.set(a, k);
  }
  const port = Number(env.NOEMA_UI_PORT ?? "5180");
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new ConfigError("NOEMA_UI_PORT must be a port number");
  const host = env.NOEMA_UI_HOST ?? "127.0.0.1";
  const loopback = host === "localhost" || host === "::1" || (isIPv4(host) && host.startsWith("127."));
  if (!loopback && env.NOEMA_UI_ALLOW_NON_LOOPBACK !== "1") {
    throw new ConfigError(`NOEMA_UI_HOST=${host} is not a loopback address; set NOEMA_UI_ALLOW_NON_LOOPBACK=1 to serve the dev proxy beyond this machine`);
  }
  const dist = env.NOEMA_UI_DIST ?? resolve(HERE, "../dist");
  return {
    apiUrl: apiUrl.origin,
    keys,
    distDir: isAbsolute(dist) ? dist : resolve(dist),
    host,
    port,
    secureCookies: env.NOEMA_UI_SECURE_COOKIES === "1",
    sessionTtlMs: 8 * 3600 * 1000,
    recordFile: env.NOEMA_UI_RECORD ?? null,
  };
}
