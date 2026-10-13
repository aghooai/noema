import { getEventListeners } from "node:events";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { retryClock } from "../src/core/retry";
import { AghooConnectionError, AghooError, type TurnEvent } from "../src/index";
import { BASE, client, fakeRetryClock, fixture, goldens, respond, sseFrame, sseResponse, useServer } from "./helpers";

const server = useServer();
const G01 = goldens().get("G01")!;
const G07 = goldens().get("G07")!;
const INPUT = { input: [{ role: "user" as const, content: [{ type: "input_text" as const, text: "Hi" }] }] };
const frames = (events: { id: number; type: string; data: unknown }[]) => events.map((e) => sseFrame(e.id, e.type, e.data));

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((r) => {
    release = r;
  });
  return { promise, release };
}

/**
 * A client whose requests are recorded when `fetch` is called ("POST /v1/turns/turn_1/cancel"). The SDK calls `fetch`
 * synchronously inside `abort()` and the abort listeners, so the list is exact without waiting for msw.
 */
function recordingClient() {
  const sent: string[] = [];
  const c = client({
    fetch: (input, init) => {
      sent.push(`${init?.method ?? "GET"} ${new URL(String(input)).pathname}`);
      return globalThis.fetch(input, init);
    },
  });
  return { c, sent, cancels: () => sent.filter((r) => r.endsWith("/cancel")) };
}

/** Retry backoffs that last until their signal aborts; `sleeping` resolves when the first one starts. */
function blockingBackoff() {
  const sleeping = deferred();
  vi.spyOn(retryClock, "sleep").mockImplementation(
    (_ms: number, signal?: AbortSignal) =>
      new Promise<void>((_, reject) => {
        sleeping.release();
        signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
      }),
  );
  return sleeping.promise;
}

/** An SSE body that waits for `gate` before sending anything. */
function gatedSse(gate: Promise<void>, chunks: string[]): Response {
  const enc = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    async pull(ctl) {
      if (i === 0) await gate;
      const c = chunks[i++];
      if (c === undefined) ctl.close();
      else ctl.enqueue(enc.encode(c));
    },
  });
  return new Response(body, { headers: { "content-type": "text/event-stream" } });
}

describe("TurnStream reconciliation", () => {
  it("renders only the committed attempt and honors generation.rerouted discard_from_seq", async () => {
    const ev: TurnEvent[] = [
      { type: "turn.created", turn_id: "turn_9", session_id: null, created_at: 1 },
      { type: "generation.delta", attempt_id: "att_1", seq: 0, text: "spec " },
      { type: "generation.committed", attempt_id: "att_2", reason: "spec_agrees" },
      { type: "generation.delta", attempt_id: "att_2", seq: 0, text: "Hello " },
      { type: "generation.delta", attempt_id: "att_2", seq: 1, text: "wrong " },
      { type: "generation.rerouted", from_attempt_id: "att_2", to_attempt_id: "att_3", reason: "x", discard_from_seq: 1 },
      { type: "generation.delta", attempt_id: "att_3", seq: 1, text: "world" },
      { type: "turn.completed", turn_id: "turn_9", usage: G01.events.at(-1)!.data.usage as never, timings: G01.events.at(-1)!.data.timings as never },
    ];
    server.use(http.post(`${BASE}/v1/turns`, () => sseResponse(ev.map((e, i) => sseFrame(i + 1, e.type, e)))));
    expect(await client().turns.stream(INPUT).finalText()).toBe("Hello world");
  });

  it("normalizes unknown answer types in streamed decisions to UnknownAnswer", async () => {
    const decision = structuredClone(G01.events.find((e) => e.type === "decision.completed")!.data.decision) as {
      answers: Record<string, unknown>[];
    };
    decision.answers[0] = { type: "ranking", name: "intent", order: ["a", "b"] };
    const events = G01.events.map((e) => (e.type === "decision.completed" ? { ...e, data: { ...e.data, decision } } : e));
    const late = events.filter((e) => e.type !== "decision.updated");
    server.use(http.post(`${BASE}/v1/turns`, () => sseResponse(frames(late))));
    const d = await client().turns.stream(INPUT).decision();
    expect(d?.answers[0]).toEqual({
      type: "unknown",
      name: "intent",
      raw_type: "ranking",
      raw: { type: "ranking", name: "intent", order: ["a", "b"] },
    });
  });

  it("throws AghooConnectionError on malformed data and on a stream without turn.completed", async () => {
    server.use(http.post(`${BASE}/v1/turns`, () => sseResponse([sseFrame(1, "turn.created", G01.events[0]!.data), "data: {nope\n\n"])));
    await expect(client().turns.stream(INPUT).finalText()).rejects.toThrow(/malformed SSE event data/);
    server.use(http.post(`${BASE}/v1/turns`, () => sseResponse(frames(G01.events.slice(0, 5)))));
    await expect(client().turns.stream(INPUT).decision()).rejects.toBeInstanceOf(AghooConnectionError);
  });
});

describe("TurnStream retries (only before the first event)", () => {
  it("retries a retryable status and a stream that drops before its first event, with one Idempotency-Key", async () => {
    const sleeps = fakeRetryClock(0.5);
    const keys: (string | null)[] = [];
    let n = 0;
    server.use(
      http.post(`${BASE}/v1/turns`, ({ request }) => {
        keys.push(request.headers.get("idempotency-key"));
        n++;
        if (n === 1) return respond(fixture("createDecision.503"));
        if (n === 2) return sseResponse([], { fail: true });
        return sseResponse(frames(G01.events));
      }),
    );
    const s = client().turns.stream(INPUT);
    expect(await s.finalText()).toContain("The capital of France");
    expect(n).toBe(3);
    expect(new Set(keys).size).toBe(1);
    expect(sleeps).toEqual([1000, 500]); // retry-after: 1, then full jitter 0.5 * min(8 s, 0.5 s * 2^1)
  });

  it("does not retry after the first event: the drop surfaces as AghooConnectionError", async () => {
    fakeRetryClock();
    let n = 0;
    server.use(
      http.post(`${BASE}/v1/turns`, () => {
        n++;
        return sseResponse(frames(G01.events.slice(0, 3))); // the connection ends before turn.completed
      }),
    );
    const s = client().turns.stream(INPUT);
    const got: string[] = [];
    await expect(
      (async () => {
        for await (const ev of s) got.push(ev.type);
      })(),
    ).rejects.toBeInstanceOf(AghooConnectionError);
    expect(got).toEqual(["turn.created", "preflight.completed", "route.selected"]);
    expect(n).toBe(1);
    await expect(s.finalText()).rejects.toBeInstanceOf(AghooConnectionError);
  });

  it("throws a non-retryable AghooError from iteration without retrying", async () => {
    fakeRetryClock();
    let n = 0;
    server.use(
      http.post(`${BASE}/v1/turns`, () => {
        n++;
        return respond(fixture("createTurn.422"));
      }),
    );
    const s = client().turns.stream({ ...INPUT, strategy: "hedge" });
    const err = await s.finalText().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AghooError);
    expect(err).toMatchObject({ status: 422, code: "feature_not_available", param: "strategy", retryable: false });
    expect(n).toBe(1);
  });
});

describe("TurnStream abort and cancel", () => {
  it("abort() calls POST /v1/turns/{id}/cancel once and the stream ends with turn.completed", async () => {
    const cancels: (string | null)[] = [];
    server.use(
      http.post(`${BASE}/v1/turns`, () => sseResponse(frames(G07.events))),
      http.post(`${BASE}/v1/turns/turn_1/cancel`, ({ request }) => {
        cancels.push(request.headers.get("idempotency-key"));
        return respond(fixture("cancelTurn.202"));
      }),
    );
    const rec = recordingClient();
    const s = rec.c.turns.stream(INPUT);
    const types: string[] = [];
    for await (const ev of s) {
      types.push(ev.type);
      if (ev.type === "generation.delta") {
        s.abort();
        s.abort();
      }
    }
    expect(types.at(-1)).toBe("turn.completed");
    await vi.waitFor(() => expect(cancels).toHaveLength(1));
    expect(cancels[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(await s.finalText()).toBe("The capital of France is Paris. ");
    s.abort(); // after turn.completed: nothing left to cancel
    expect(rec.cancels()).toEqual(["POST /v1/turns/turn_1/cancel"]);
  });

  it("abort() before turn.created sends the cancel as soon as the turn id is known", async () => {
    const gate = deferred();
    const cancelled = deferred();
    server.use(
      http.post(`${BASE}/v1/turns`, () => gatedSse(gate.promise, frames(G07.events))),
      http.post(`${BASE}/v1/turns/turn_1/cancel`, () => {
        cancelled.release();
        return HttpResponse.json(fixture("cancelTurn.202").body as never, { status: 202 });
      }),
    );
    const s = client().turns.stream(INPUT);
    s.abort();
    gate.release();
    await cancelled.promise;
    expect(await s.finalText()).toBe("The capital of France is Paris. ");
  });

  it("an aborted AbortSignal cancels the turn on the server and rejects iteration with its reason", async () => {
    server.use(
      http.post(`${BASE}/v1/turns`, () => sseResponse(frames(G07.events))),
      http.post(`${BASE}/v1/turns/turn_1/cancel`, () => respond(fixture("cancelTurn.202"))),
    );
    const rec = recordingClient();
    const ctl = new AbortController();
    const s = rec.c.turns.stream(INPUT, { signal: ctl.signal });
    const reason = new Error("user left");
    const seen: string[] = [];
    const run = (async () => {
      for await (const ev of s) {
        seen.push(ev.type);
        if (ev.type === "route.selected") ctl.abort(reason);
      }
    })();
    await expect(run).rejects.toBe(reason);
    await expect(s.decision()).rejects.toBe(reason);
    expect(rec.cancels()).toEqual(["POST /v1/turns/turn_1/cancel"]);
    expect(seen).toEqual(["turn.created", "preflight.completed", "route.selected"]);
  });

  it("a signal aborted before turn.created still cancels once the turn id arrives", async () => {
    const gate = deferred();
    const received = deferred();
    const cancelled = deferred();
    server.use(
      http.post(`${BASE}/v1/turns`, () => {
        received.release();
        return gatedSse(gate.promise, frames(G07.events));
      }),
      http.post(`${BASE}/v1/turns/turn_1/cancel`, () => {
        cancelled.release();
        return respond(fixture("cancelTurn.202"));
      }),
    );
    const ctl = new AbortController();
    const s = client().turns.stream(INPUT, { signal: ctl.signal });
    const pending = s.finalText();
    await received.promise; // the attempt is in flight: the server may already have created the turn
    ctl.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    gate.release();
    await cancelled.promise;
  });

  it("a signal reused across streams is detached when each settles; aborting it later cancels nothing", async () => {
    let turns = 0;
    server.use(
      http.post(`${BASE}/v1/turns`, () => {
        const id = `turn_${++turns}`;
        const events = G07.events.map((e) => ("turn_id" in e.data ? { ...e, data: { ...e.data, turn_id: id } } : e));
        return sseResponse(frames(turns === 3 ? events.slice(0, 2) : events)); // the third stream drops early
      }),
    );
    const rec = recordingClient();
    const app = new AbortController(); // one app-wide signal
    const listeners = () => getEventListeners(app.signal, "abort").length;
    const first = rec.c.turns.stream(INPUT, { signal: app.signal });
    expect(listeners()).toBe(2);
    for await (const _ of first) {
      // consumed to turn.completed
    }
    expect(listeners()).toBe(0);
    expect(await rec.c.turns.stream(INPUT, { signal: app.signal }).finalText()).toBe("The capital of France is Paris. ");
    expect(listeners()).toBe(0);
    await expect(rec.c.turns.stream(INPUT, { signal: app.signal }).finalText()).rejects.toBeInstanceOf(
      AghooConnectionError,
    );
    expect(listeners()).toBe(0); // no settled stream stays reachable from the signal
    app.abort();
    expect(rec.cancels()).toEqual([]); // turns that completed (or were dropped) are not cancelled
    expect(rec.sent).toEqual(["POST /v1/turns", "POST /v1/turns", "POST /v1/turns"]);
  });

  it("a signal aborted during the retry backoff starts no new attempt and creates no turn", async () => {
    const sleeping = blockingBackoff();
    server.use(http.post(`${BASE}/v1/turns`, () => respond(fixture("createDecision.429"))));
    const rec = recordingClient();
    const ctl = new AbortController();
    const s = rec.c.turns.stream(INPUT, { signal: ctl.signal });
    const pending = s.finalText();
    await sleeping; // the first attempt got 429 with retry-after; no request is in flight and no turn exists
    ctl.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(rec.sent).toEqual(["POST /v1/turns"]); // no second POST, so nothing to cancel
    expect(getEventListeners(ctl.signal, "abort")).toHaveLength(0);
  });

  it("abort() during the retry backoff ends the stream empty without a new attempt", async () => {
    const sleeping = blockingBackoff();
    server.use(http.post(`${BASE}/v1/turns`, () => respond(fixture("createDecision.429"))));
    const rec = recordingClient();
    const s = rec.c.turns.stream(INPUT);
    const text = s.finalText();
    await sleeping;
    s.abort();
    expect(await text).toBe("");
    expect(await s.decision()).toBeNull();
    expect(rec.sent).toEqual(["POST /v1/turns"]);
  });

  it("an already aborted signal sends no request", async () => {
    const ctl = new AbortController();
    ctl.abort();
    const s = client().turns.stream(INPUT, { signal: ctl.signal });
    await expect(s.finalText()).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("turns: create, retrieve, cancel, resume", () => {
  it("create forces stream:false; retrieve and cancel use the turn id", async () => {
    let body: unknown;
    server.use(
      http.post(`${BASE}/v1/turns`, async ({ request }) => {
        body = await request.json();
        return respond(fixture("createTurn.200"));
      }),
      http.get(`${BASE}/v1/turns/turn_1`, () => respond(fixture("getTurn.200"))),
      http.post(`${BASE}/v1/turns/turn_1/cancel`, () => respond(fixture("cancelTurn.202"))),
    );
    const c = client();
    const t = await c.turns.create({ ...INPUT, stream: true });
    expect(body).toEqual({ ...INPUT, stream: false });
    expect(t.text).toBe("Paris is the capital of France.");
    expect((await c.turns.retrieve("turn_1")).routing.route_id).toBe("sol-fast");
    expect(await c.turns.cancel("turn_1")).toEqual({ turn_id: "turn_1", status: "cancelling" });
  });

  it("lastEventId resumes via GET /v1/turns/{id}/events (Phase 1: 501 not_implemented)", async () => {
    let lastEventId: string | null = null;
    server.use(
      http.get(`${BASE}/v1/turns/turn_1/events`, ({ request }) => {
        lastEventId = request.headers.get("last-event-id");
        return respond(fixture("resumeTurnEvents.501"));
      }),
    );
    const s = client().turns.stream(INPUT, { lastEventId: "7", turnId: "turn_1" });
    expect(s.turnId).toBe("turn_1");
    await expect(s.finalText()).rejects.toMatchObject({ status: 501, code: "not_implemented" });
    expect(lastEventId).toBe("7");
    expect(() => client().turns.stream(INPUT, { lastEventId: "7" })).toThrow(TypeError);
  });
});
