import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { load } from "js-yaml";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { AghooClient, type ClientOptions } from "../src/index";
import { retryClock } from "../src/core/retry";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(HERE, "../../..");
export const BASE = "http://127.0.0.1:4010";
export const API_KEY = "ak_test_01JA00000000000000000000AA.secret";

/** One msw server per test file; any request without a handler fails the test (nothing reaches the network). */
export function useServer() {
  const server = setupServer();
  beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
  afterEach(() => server.resetHandlers());
  afterAll(() => server.close());
  return server;
}

export function client(opts: ClientOptions = {}): AghooClient {
  return new AghooClient({ apiKey: API_KEY, baseURL: BASE, ...opts });
}

/** Retry sleeps are recorded instead of waited for; `random` is pinned so full-jitter delays are deterministic. */
export function fakeRetryClock(random = 0.5): number[] {
  const sleeps: number[] = [];
  vi.spyOn(retryClock, "random").mockReturnValue(random);
  vi.spyOn(retryClock, "sleep").mockImplementation(async (ms: number) => {
    sleeps.push(ms);
  });
  return sleeps;
}

export interface Fixture {
  status: number;
  headers: Record<string, string>;
  body?: unknown;
  frames?: { id: string; event: string; data: unknown }[];
}

/** A shared response fixture (sdk/fixtures, validated against the contract by sdk/python/tests). */
export function fixture(name: string): Fixture {
  return JSON.parse(readFileSync(join(REPO, "sdk/fixtures", `${name}.json`), "utf8")) as Fixture;
}

/** A vendored contract example (src/noema/contracts/_vendor/examples). */
export function example<T = unknown>(name: string): T {
  return JSON.parse(readFileSync(join(REPO, "src/noema/contracts/_vendor/examples", `${name}.json`), "utf8")) as T;
}

export function respond(fx: Fixture): Response {
  return new Response(JSON.stringify(fx.body), { status: fx.status, headers: fx.headers });
}

export interface GoldenEvent {
  id: number;
  at: number;
  type: string;
  data: { type: string } & Record<string, unknown>;
}
export interface Golden {
  id: string;
  title: string;
  request: { headers: Record<string, string>; body: Record<string, unknown> };
  events: GoldenEvent[];
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

export function sseFrame(id: string | number, event: string, data: unknown): string {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** An SSE response whose body is the given chunks; `fail` errors the body after the chunks (a dropped connection). */
export function sseResponse(chunks: string[], init: { fail?: boolean; headers?: Record<string, string> } = {}): Response {
  const enc = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(ctl) {
      const chunk = chunks[i++];
      if (chunk !== undefined) ctl.enqueue(enc.encode(chunk));
      else if (init.fail) ctl.error(new TypeError("terminated"));
      else ctl.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream", "x-request-id": "req_1", ...init.headers },
  });
}
