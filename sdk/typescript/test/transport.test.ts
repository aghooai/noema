import { delay, http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { retryClock, retryDelayMs } from "../src/core/retry";
import { AghooConnectionError, AghooError, AghooTimeoutError } from "../src/index";
import { BASE, client, fakeRetryClock, fixture, respond, useServer } from "./helpers";

const server = useServer();
const PARAMS = { input: "I was charged twice.", questions: [] };

/** Answers each POST /v1/decisions with the next response; records the Idempotency-Key of every attempt. */
function sequence(...responses: (() => Response | Promise<Response>)[]) {
  const keys: (string | null)[] = [];
  server.use(
    http.post(`${BASE}/v1/decisions`, ({ request }) => {
      keys.push(request.headers.get("idempotency-key"));
      const next = responses[Math.min(keys.length - 1, responses.length - 1)]!;
      return next();
    }),
  );
  return keys;
}
const ok = () => HttpResponse.json({ model: "aghoo/x@1", answers: {}, usage: { input_tokens: 1, output_tokens: 1 }, decision_id: "dec_1" });
const jev = { ...PARAMS, output_format: "jev" as const };

describe("retries", () => {
  it("honors retry-after and reuses one Idempotency-Key across attempts", async () => {
    const sleeps = fakeRetryClock();
    const keys = sequence(() => respond(fixture("createDecision.429")), ok);
    await client().decisions.create(jev);
    expect(sleeps).toEqual([2000]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(keys[1]).toBe(keys[0]);
  });

  it("uses full jitter (base 0.5 s, cap 8 s) and gives up after maxRetries 2", async () => {
    const sleeps = fakeRetryClock(0.5);
    const keys = sequence(() => respond(fixture("createTurn.500")));
    await expect(client().decisions.create(jev)).rejects.toMatchObject({ status: 500, code: "internal_error" });
    expect(keys).toHaveLength(3);
    expect(sleeps).toEqual([250, 500]);
    expect([0, 1, 2, 3, 4, 5, 9].map((a) => retryDelayMs(a, null))).toEqual([250, 500, 1000, 2000, 4000, 4000, 4000]);
  });

  it("does not retry non-retryable codes, a retry-after above 60 s, or with maxRetries 0", async () => {
    fakeRetryClock();
    const tooLong = () => {
      const fx = fixture("createDecision.503");
      return respond({ ...fx, headers: { ...fx.headers, "retry-after": "120" } });
    };
    for (const make of [() => respond(fixture("createDecisionOpenAICompat.422")), tooLong]) {
      const keys = sequence(make);
      await expect(client().decisions.create(jev)).rejects.toBeInstanceOf(AghooError);
      expect(keys).toHaveLength(1);
    }
    const keys = sequence(() => respond(fixture("createDecision.503")));
    await expect(client().decisions.create(jev, { maxRetries: 0 })).rejects.toMatchObject({ retryAfterS: 1 });
    expect(keys).toHaveLength(1);
    const keys2 = sequence(() => respond(fixture("createDecision.503")));
    await expect(client({ maxRetries: 1 }).decisions.create(jev)).rejects.toMatchObject({ code: "all_routes_unavailable" });
    expect(keys2).toHaveLength(2);
  });

  it("retries connect errors, then throws AghooConnectionError", async () => {
    fakeRetryClock();
    const keys = sequence(() => HttpResponse.error());
    const err = await client().decisions.create(jev).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AghooConnectionError);
    expect(keys).toHaveLength(3);
  });

  it("maps a non-envelope 502 to a retryable AghooError", async () => {
    fakeRetryClock();
    const keys = sequence(() => new HttpResponse("<html>bad gateway</html>", { status: 502 }), ok);
    await client().decisions.create(jev);
    expect(keys).toHaveLength(2);
    sequence(() => new HttpResponse("<html>bad gateway</html>", { status: 502, headers: { "x-request-id": "req_p" } }));
    await expect(client().decisions.create(jev, { maxRetries: 0 })).rejects.toMatchObject({
      status: 502,
      type: "server_error",
      code: "unknown_error",
      requestId: "req_p",
      retryable: true,
    });
  });

  it("sends a caller-provided idempotencyKey", async () => {
    const keys = sequence(ok);
    await client().decisions.create(jev, { idempotencyKey: "order-17" });
    expect(keys).toEqual(["order-17"]);
  });
});

describe("timeouts and AbortSignal", () => {
  it("throws AghooTimeoutError when timeoutMs elapses", async () => {
    sequence(async () => {
      await delay("infinite");
      return ok();
    });
    const err = await client().decisions.create(jev, { timeoutMs: 20, maxRetries: 0 }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AghooTimeoutError);
    expect(err).toBeInstanceOf(AghooConnectionError);
  });

  it("rejects with the signal's reason and does not retry", async () => {
    fakeRetryClock();
    const keys = sequence(async () => {
      await delay("infinite");
      return ok();
    });
    const ctl = new AbortController();
    const reason = new Error("stop");
    const pending = client().decisions.create(jev, { signal: ctl.signal });
    await vi.waitFor(() => expect(keys).toHaveLength(1)); // the request is in flight
    ctl.abort(reason);
    await expect(pending).rejects.toBe(reason);
    expect(keys).toHaveLength(1);
  });

  it("detaches from a reused AbortSignal once each call settles", async () => {
    sequence(ok);
    const ctl = new AbortController();
    const add = vi.spyOn(ctl.signal, "addEventListener");
    const remove = vi.spyOn(ctl.signal, "removeEventListener");
    for (let i = 0; i < 3; i++) await client().decisions.create(jev, { signal: ctl.signal });
    const aborts = (calls: unknown[][]) => calls.filter((c) => c[0] === "abort").length;
    expect(aborts(add.mock.calls)).toBe(3);
    expect(aborts(remove.mock.calls)).toBe(3);
  });

  it("does not retry an error the server replayed from its idempotency store (the same key replays it again)", async () => {
    const sleeps = fakeRetryClock();
    const fx = fixture("createDecision.503");
    const keys = sequence(
      () => respond({ ...fx, headers: { ...fx.headers, "x-aghoo-idempotent-replay": "true" } }),
      ok,
    );
    const err = await client().decisions.create(jev).catch((e: unknown) => e);
    expect(err).toMatchObject({ status: 503, code: "all_routes_unavailable", retryable: true });
    expect(keys).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });

  it("an abort during the backoff sleep rejects with the reason", async () => {
    const ctl = new AbortController();
    const sleeping = retryClock.sleep(60_000, ctl.signal);
    ctl.abort(new Error("bye"));
    await expect(sleeping).rejects.toThrow("bye");
    await expect(retryClock.sleep(1, ctl.signal)).rejects.toThrow("bye");
    await expect(retryClock.sleep(1)).resolves.toBeUndefined();
  });
});

describe("path segments", () => {
  it("rejects an empty, '.' or '..' id before sending (URL parsing would resolve it to another route)", async () => {
    let seen: string | undefined;
    server.use(
      http.get(`${BASE}/v1/sessions/:id`, ({ params }) => {
        seen = String(params.id);
        return respond(fixture("getSession.200"));
      }),
    );
    const c = client();
    const calls: (() => Promise<unknown>)[] = [
      () => c.sessions.delete(".."),
      () => c.sessions.retrieve(""),
      () => c.turns.cancel("."),
      () => c.turns.retrieve(".."),
      () => c.files.delete(".."),
      () => c.files.derivative("..", "text"),
      async () => {
        for await (const _ of c.sessions.turns("..")) break;
      },
      () => c.turns.stream({ input: [] }, { lastEventId: "1", turnId: ".." }).finalText(),
    ];
    for (const call of calls) await expect(call()).rejects.toThrow(TypeError);
    await c.sessions.retrieve("..ses.1"); // dots inside an id are fine
    expect(seen).toBe("..ses.1");
  });
});
