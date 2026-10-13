import type { TurnObject } from "@aghoo/sdk";
import { describe, expect, it } from "vitest";
import { applyEvent, emptyTurn, fromTurnObject, type TurnView } from "../src/chat/turnState";
import { expectedText, fixture, type Golden, golden, goldens } from "./helpers";

function run(g: Golden, upTo = Number.POSITIVE_INFINITY): TurnView {
  let v = emptyTurn();
  for (const e of g.events) if (e.id <= upTo) v = applyEvent(v, e.data);
  return v;
}

function idOf(g: Golden, pred: (e: Golden["events"][number]) => boolean): number {
  const e = g.events.find(pred);
  if (e === undefined) throw new Error("event not found");
  return e.id;
}

describe("turn view over the golden SSE fixtures (spec-04 §4.2)", () => {
  const all = goldens();

  it("covers every fixture", () => {
    expect(all.size).toBeGreaterThanOrEqual(17);
  });

  for (const [id, g] of all) {
    it(`${id} ${g.title}: committed text, decision, timings and attempts`, () => {
      const v = run(g);
      const last = g.events.at(-1)?.data;
      expect(last?.type).toBe("turn.completed");
      expect(v.status).toBe("completed");
      expect(v.refining).toBe(false);
      expect(v.text).toBe(expectedText(g));
      const decisions = g.events.filter((e) => e.data.type === "decision.completed" || e.data.type === "decision.updated");
      const lastDecision = decisions.at(-1)?.data;
      expect(v.decision).toEqual(lastDecision !== undefined && "decision" in lastDecision ? lastDecision.decision : null);
      if (last?.type === "turn.completed") {
        expect(v.timings).toEqual(last.timings);
        expect(v.specRestarts).toBe(last.timings.spec_restarts);
      }
      expect(v.turnId).toBe(g.events[0]?.data.type === "turn.created" ? g.events[0].data.turn_id : null);
      const started = g.events.filter((e) => e.data.type === "generation.started").length;
      expect(v.attempts).toHaveLength(started);
      expect(v.attempts.every((a) => a.status !== "running" || v.errors.length > 0)).toBe(true);
      expect(v.routing).not.toBeNull();
    });
  }

  it("G02: 'Refining…' from the invisible restart (spec_changed) until the new attempt commits", () => {
    const g = golden("G02");
    const cancelled = idOf(g, (e) => e.data.type === "generation.cancelled");
    const committed = idOf(g, (e) => e.data.type === "generation.committed");
    expect(run(g, cancelled - 1).refining).toBe(false);
    for (let i = cancelled; i < committed; i++) expect(run(g, i).refining).toBe(true);
    const after = run(g, committed);
    expect(after.refining).toBe(false);
    expect(after.specRestarts).toBe(1);
    expect(after.attempts.map((a) => [a.attemptId, a.status, a.detail])).toEqual([
      ["att_1", "cancelled", "spec_changed"],
      ["att_2", "committed", "spec_agrees"],
    ]);
  });

  it("G05/G06: a failed attempt and a lost race are not restarts and never show 'Refining…'", () => {
    for (const id of ["G05", "G06"]) {
      const g = golden(id);
      for (const e of g.events) expect(run(g, e.id).refining).toBe(false);
    }
    expect(run(golden("G05")).attempts[0]).toMatchObject({ status: "failed", detail: "failed" });
    expect(run(golden("G06")).attempts[1]).toMatchObject({ status: "cancelled", detail: "lost_race" });
  });

  it("route.selected routing is speculative until route.confirmed replaces it", () => {
    const g = golden("G01");
    expect(run(g, idOf(g, (e) => e.data.type === "route.selected"))).toMatchObject({ routingConfirmed: false });
    const confirmed = g.events.find((e) => e.data.type === "route.confirmed")?.data;
    const v = run(g, idOf(g, (e) => e.data.type === "route.confirmed"));
    expect(v.routingConfirmed).toBe(true);
    expect(v.routing).toEqual(confirmed !== undefined && "routing" in confirmed ? confirmed.routing : null);
  });

  it("G08: turn.verified sets the verification; G10: the SSE error is kept with the partial committed text", () => {
    expect(run(golden("G08")).verification?.answers[0]?.name).toBe("answers_question");
    const g10 = run(golden("G10"));
    expect(g10.errors).toEqual([{ code: "upstream_stalled", message: "The answer stream failed.", retryable: true }]);
    expect(g10.text).toBe("The capital of France is Paris. It is also ");
  });

  it("generation.rerouted keeps the text before discard_from_seq and continues with the new attempt", () => {
    let v = emptyTurn();
    const ev = (e: Parameters<typeof applyEvent>[1]): void => {
      v = applyEvent(v, e);
    };
    ev({ type: "generation.committed", attempt_id: "a1", reason: "spec_agrees" });
    ev({ type: "generation.delta", attempt_id: "a1", seq: 0, text: "Hello " });
    ev({ type: "generation.delta", attempt_id: "a1", seq: 1, text: "wrold" });
    ev({ type: "generation.rerouted", from_attempt_id: "a1", to_attempt_id: "a2", reason: "upstream_stalled", discard_from_seq: 1 });
    ev({ type: "generation.delta", attempt_id: "a2", seq: 0, text: "world" });
    expect(v.text).toBe("Hello world");
  });

  it("degradations from preflight, route.selected and generation.started are merged without duplicates", () => {
    const d = { file_id: "file_1", from: "application/pdf", to: "markdown", reason: "active_content_removed" as const };
    let v = applyEvent(emptyTurn(), { type: "preflight.completed", modalities: ["text", "document"], est_input_tokens: 10, degradations: [d] });
    v = applyEvent(v, { type: "generation.started", attempt_id: "a", route_id: "r", model: "m", speculative: true, degradations: [d] });
    expect(v.degradations).toEqual([d]);
    expect(v.modalities).toEqual(["text", "document"]);
  });

  it("a stored turn (getTurn fixture) becomes the same view", () => {
    const o = fixture("getTurn.200").body as TurnObject;
    const v = fromTurnObject(o);
    expect(v).toMatchObject({ turnId: o.turn_id, status: "completed", text: o.text, routing: o.routing, usage: o.usage });
    expect(v.attempts[0]).toMatchObject({ attemptId: "att_1", status: "committed", usage: o.attempts[0]?.usage });
  });
});
