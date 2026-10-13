import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { load } from "js-yaml";
import { setupServer } from "msw/node";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterAll, afterEach, beforeAll } from "vitest";
import type { TurnEvent } from "@aghoo/sdk";
import { type Api, createApi } from "../src/api/client";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(HERE, "../..");
export const BASE = "http://127.0.0.1:4010";
export const CSRF = "csrf-token-for-tests";

/** One msw server per test file; any request without a handler fails the test (nothing reaches the network). */
export function useServer() {
  const server = setupServer();
  beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
  afterEach(() => server.resetHandlers());
  afterAll(() => server.close());
  return server;
}

/** The SPA's API wiring against msw: proxy origin BASE, the CSRF cookie set. */
export function testApi(): Api {
  return createApi({ origin: BASE, fetch: (i, init) => globalThis.fetch(i, init), cookies: () => `other=1; noema_csrf=${CSRF}` });
}

export interface Fixture {
  status: number;
  headers: Record<string, string>;
  body?: unknown;
  frames?: { id: string; event: string; data: unknown }[];
}

/** A shared response fixture (sdk/fixtures; every one is validated by the server's ContractValidator in sdk/python). */
export function fixture(name: string): Fixture {
  return JSON.parse(readFileSync(join(REPO, "sdk/fixtures", `${name}.json`), "utf8")) as Fixture;
}

/** A vendored contract example (src/noema/contracts/_vendor/examples). */
export function example<T = unknown>(name: string): T {
  return JSON.parse(readFileSync(join(REPO, "src/noema/contracts/_vendor/examples", `${name}.json`), "utf8")) as T;
}

export function respond(fx: Fixture, body: unknown = fx.body): Response {
  return new Response(JSON.stringify(body), { status: fx.status, headers: fx.headers });
}

export interface GoldenEvent {
  id: number;
  at: number;
  type: string;
  data: TurnEvent;
}
export interface Golden {
  id: string;
  title: string;
  request: { headers: Record<string, string>; body: Record<string, unknown> };
  events: GoldenEvent[];
  db?: { turn?: { output_text?: string } };
}

/** Golden SSE fixtures of spec-04 §4.2 (tests/golden/sse/G*.yaml), keyed by id ("G01", "G10b", ...). */
export function goldens(): Map<string, Golden> {
  const dir = join(REPO, "tests/golden/sse");
  const out = new Map<string, Golden>();
  for (const name of readdirSync(dir).filter((n) => /^G\w+\.yaml$/.test(n)).sort()) {
    const g = load(readFileSync(join(dir, name), "utf8")) as Golden;
    out.set(g.id, g);
  }
  return out;
}

export function golden(id: string): Golden {
  const g = goldens().get(id);
  if (g === undefined) throw new Error(`no golden ${id}`);
  return g;
}

/** Oracle (as in the SDK suite): the stored output_text, else every delta in order (only committed deltas are sent). */
export function expectedText(g: Golden): string {
  const stored = g.db?.turn?.output_text;
  if (stored !== undefined) return stored;
  return g.events.flatMap((e) => (e.data.type === "generation.delta" ? [e.data.text] : [])).join("");
}

export function sseFrame(id: string | number, event: string, data: unknown): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** An SSE response whose body is the given text cut into small chunks. */
export function sseResponse(text: string, chunk = 11): Response {
  const enc = new TextEncoder();
  const parts = text.match(new RegExp(`[\\s\\S]{1,${chunk}}`, "g")) ?? [];
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(ctl) {
      const p = parts[i++];
      if (p === undefined) ctl.close();
      else ctl.enqueue(enc.encode(p));
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream", "x-request-id": "req_1" } });
}

export function goldenSse(g: Golden): string {
  return ": ping\n\n" + g.events.map((e) => sseFrame(e.id, e.type, e.data)).join("");
}

export function render(el: ReactElement): string {
  return renderToStaticMarkup(el);
}
