import { http } from "msw";
import { describe, expect, it } from "vitest";
import { API_KEY, BASE, client, type Golden, goldens, sseFrame, sseResponse, useServer } from "./helpers";

const server = useServer();
const GOLDENS = goldens();

/** The fixture's frames as one SSE text, with pings and an unknown event type mixed in, cut into 7-char chunks. */
function goldenChunks(g: Golden): string[] {
  let text = ": ping\n\n";
  for (const ev of g.events) {
    text += sseFrame(ev.id, ev.type, ev.data);
    if (ev.id === 2) text += sseFrame(ev.id, "turn.future_event", { type: "turn.future_event", x: 1 }) + ": ping\n\n";
  }
  return text.match(/[\s\S]{1,7}/g) ?? [];
}

/** Oracle: Phase 1 sends deltas only for the committed attempt (P-SSE-1), so the text is every delta, in order. */
function expectedText(g: Golden & { db?: { turn?: { output_text?: string } } }): string {
  const stored = g.db?.turn?.output_text;
  if (stored !== undefined) return stored;
  return g.events
    .filter((e) => e.type === "generation.delta")
    .map((e) => e.data.text as string)
    .join("");
}

function expectedDecision(g: Golden): unknown {
  const last = g.events.filter((e) => e.type === "decision.completed" || e.type === "decision.updated").at(-1);
  return last === undefined ? null : last.data.decision;
}

describe("TurnStream against the golden SSE fixtures (spec-04 §4.2)", () => {
  it("covers every fixture", () => {
    expect(GOLDENS.size).toBeGreaterThanOrEqual(17);
  });

  for (const [id, g] of GOLDENS) {
    it(`${id} ${g.title}: events, finalText and decision`, async () => {
      const seen: { body: unknown; headers: Headers }[] = [];
      server.use(
        http.post(`${BASE}/v1/turns`, async ({ request }) => {
          seen.push({ body: await request.json(), headers: request.headers });
          return sseResponse(goldenChunks(g));
        }),
      );
      const s = client().turns.stream(g.request.body as never);
      const events: unknown[] = [];
      for await (const ev of s) events.push(ev);

      expect(events).toEqual(g.events.map((e) => e.data));
      expect(await s.finalText()).toBe(expectedText(g as never));
      expect(await s.decision()).toEqual(expectedDecision(g));
      expect(s.turnId).toBe(g.events[0]?.data.turn_id);
      expect(s.lastEventId).toBe(String(g.events.at(-1)?.id));

      expect(seen).toHaveLength(1);
      const sent = seen[0]!;
      expect(sent.body).toEqual({ ...g.request.body, stream: true });
      expect(sent.headers.get("authorization")).toBe(`Bearer ${API_KEY}`);
      expect(sent.headers.get("aghoo-version")).toBe("2026-10-01");
      expect(sent.headers.get("accept")).toBe("text/event-stream");
      expect(sent.headers.get("content-type")).toBe("application/json");
      expect(sent.headers.get("idempotency-key")).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    });
  }

  it("finalText() and decision() consume the stream when it is not iterated", async () => {
    const g = GOLDENS.get("G02")!;
    server.use(http.post(`${BASE}/v1/turns`, () => sseResponse(goldenChunks(g))));
    const s = client().turns.stream(g.request.body as never);
    const [text, decision] = await Promise.all([s.finalText(), s.decision()]);
    expect(text).toBe(expectedText(g as never));
    expect(decision).toEqual(expectedDecision(g));
    expect(() => s[Symbol.asyncIterator]()).toThrow(TypeError);
  });

  it("G10: an SSE error event is yielded, not thrown, and the partial committed text is final", async () => {
    const g = GOLDENS.get("G10")!;
    server.use(http.post(`${BASE}/v1/turns`, () => sseResponse(goldenChunks(g))));
    const s = client().turns.stream(g.request.body as never);
    const errors: unknown[] = [];
    for await (const ev of s) if (ev.type === "error") errors.push(ev);
    expect(errors).toEqual([expect.objectContaining({ code: "upstream_stalled", retryable: true })]);
    expect(await s.finalText()).toBe("The capital of France is Paris. It is also ");
  });
});
