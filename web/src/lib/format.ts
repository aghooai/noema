import { AghooError } from "@aghoo/sdk";

export function pct(p: number | null | undefined, digits = 0): string {
  return p === null || p === undefined ? "–" : `${(p * 100).toFixed(digits)}%`;
}

export function num(x: number | null | undefined, digits = 2): string {
  return x === null || x === undefined ? "–" : x.toFixed(digits);
}

export function ms(x: number | null | undefined): string {
  return x === null || x === undefined ? "–" : `${Math.round(x)} ms`;
}

export function usd(x: number | null | undefined): string {
  if (x === null || x === undefined) return "–";
  return `$${x < 0.01 && x !== 0 ? x.toFixed(6) : x.toFixed(4)}`;
}

export function tokens(x: number): string {
  return x.toLocaleString("en-US");
}

/** Unix seconds to an ISO-like UTC string (no locale or time-zone dependence: deterministic in tests). */
export function unixTime(s: number | null | undefined): string {
  return s === null || s === undefined ? "–" : new Date(s * 1000).toISOString().replace("T", " ").slice(0, 19);
}

/** A short model name for dense views: "us.anthropic.claude-haiku-5-5" -> "claude-haiku-5-5", "deepseek.v3.2" -> "deepseek-v3.2". */
export function shortModel(model: string): string {
  const m = model.replace(/^(us|eu|apac|global)\./, "");
  const dot = m.indexOf(".");
  if (dot < 0) return m;
  const vendor = m.slice(0, dot);
  const rest = m.slice(dot + 1);
  return /^v\d/.test(rest) ? `${vendor}-${rest}` : rest;
}

/** A user-facing message for any thrown value; typed API errors keep their code. */
export function errorText(e: unknown): string {
  if (e instanceof AghooError) return `${e.code}: ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function errorCode(e: unknown): string | null {
  return e instanceof AghooError ? String(e.code) : null;
}
