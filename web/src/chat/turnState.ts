import type {
  DecisionResult,
  Degradation,
  Modality,
  RoutingDecision,
  TurnEvent,
  TurnObject,
  TurnTimings,
  Usage,
} from "@aghoo/sdk";

export type AttemptStatus = "running" | "committed" | "cancelled" | "completed" | "failed" | "not_selected";

export interface AttemptView {
  attemptId: string;
  routeId: string;
  model: string;
  speculative: boolean;
  status: AttemptStatus;
  /** generation.cancelled or generation.committed reason (stored turns: the outcome). */
  detail: string | null;
  /** generation.completed finish_reason. */
  finishReason: string | null;
  usage: Usage | null;
}

export interface TurnError {
  code: string;
  message: string;
  retryable: boolean;
}

/** Everything W1 shows for one assistant turn: the streamed text and the inspector's inputs. */
export interface TurnView {
  turnId: string | null;
  sessionId: string | null;
  /** pending: nothing yet; running: events arriving; completed: turn.completed (or a stored turn). */
  status: "pending" | "running" | "completed";
  /** Committed text only (03 §3.2 client obligations). */
  text: string;
  /** "Refining…": the speculative attempt was replaced invisibly (spec_changed) and the new one has not committed yet. */
  refining: boolean;
  /** Invisible restarts seen so far (final value: timings.spec_restarts). */
  specRestarts: number;
  modalities: Modality[];
  estInputTokens: number | null;
  degradations: Degradation[];
  /** route.confirmed routing when it arrived, else route.selected (speculative) routing. */
  routing: RoutingDecision | null;
  routingConfirmed: boolean;
  decision: DecisionResult | null;
  decisionArrived: string | null;
  /** decision.updated `changed` (late members merged). */
  decisionChanged: string[];
  verification: DecisionResult | null;
  attempts: AttemptView[];
  reroutes: { from: string; to: string; reason: string; discardFromSeq: number | null }[];
  errors: TurnError[];
  usage: Usage | null;
  timings: TurnTimings | null;
  /** Internal: deltas per attempt and the attempt whose text is shown. */
  deltas: Record<string, { seq: number; text: string }[]>;
  finalAttempt: string | null;
}

export function emptyTurn(): TurnView {
  return {
    turnId: null,
    sessionId: null,
    status: "pending",
    text: "",
    refining: false,
    specRestarts: 0,
    modalities: [],
    estInputTokens: null,
    degradations: [],
    routing: null,
    routingConfirmed: false,
    decision: null,
    decisionArrived: null,
    decisionChanged: [],
    verification: null,
    attempts: [],
    reroutes: [],
    errors: [],
    usage: null,
    timings: null,
    deltas: {},
    finalAttempt: null,
  };
}

function sameDegradation(a: Degradation, b: Degradation): boolean {
  return (
    a.reason === b.reason &&
    a.from === b.from &&
    a.to === b.to &&
    a.file_id === b.file_id &&
    a.part_index === b.part_index &&
    a.model === b.model
  );
}

function addDegradations(list: Degradation[], more: Degradation[]): Degradation[] {
  const out = [...list];
  for (const d of more) if (!out.some((x) => sameDegradation(x, d))) out.push(d);
  return out;
}

function setAttempt(t: TurnView, id: string, patch: Partial<AttemptView>): AttemptView[] {
  return t.attempts.map((a) => (a.attemptId === id ? { ...a, ...patch } : a));
}

/** The committed chain's text: a rerouted-to attempt continues its source's text before `discard_from_seq`. */
function committedText(t: TurnView): string {
  const seen = new Set<string>();
  const chain = (attempt: string, before: number | null): string => {
    if (seen.has(attempt)) return "";
    seen.add(attempt);
    const r = t.reroutes.find((x) => x.to === attempt);
    const prefix = r === undefined ? "" : chain(r.from, r.discardFromSeq);
    let own = "";
    for (const d of t.deltas[attempt] ?? []) if (before === null || d.seq < before) own += d.text;
    return prefix + own;
  };
  return t.finalAttempt === null ? "" : chain(t.finalAttempt, null);
}

/** Applies one SSE event (unknown types are ignored by the SDK before they get here). */
export function applyEvent(prev: TurnView, ev: TurnEvent): TurnView {
  let t: TurnView = prev.status === "pending" ? { ...prev, status: "running" } : { ...prev };
  switch (ev.type) {
    case "turn.created":
      t = { ...t, turnId: ev.turn_id, sessionId: ev.session_id };
      break;
    case "preflight.completed":
      t = {
        ...t,
        modalities: ev.modalities,
        estInputTokens: ev.est_input_tokens,
        degradations: addDegradations(t.degradations, ev.degradations),
      };
      break;
    case "route.selected":
      t = { ...t, routing: ev.routing, degradations: addDegradations(t.degradations, ev.degradations) };
      break;
    case "route.confirmed":
      t = { ...t, routing: ev.routing, routingConfirmed: true };
      break;
    case "decision.completed":
      t = { ...t, decision: ev.decision, decisionArrived: ev.arrived };
      break;
    case "decision.updated":
      t = { ...t, decision: ev.decision, decisionChanged: ev.changed };
      break;
    case "generation.started":
      t = {
        ...t,
        degradations: addDegradations(t.degradations, ev.degradations),
        attempts: [
          ...t.attempts,
          {
            attemptId: ev.attempt_id,
            routeId: ev.route_id,
            model: ev.model,
            speculative: ev.speculative,
            status: "running",
            detail: null,
            finishReason: null,
            usage: null,
          },
        ],
      };
      break;
    case "generation.committed":
      t = {
        ...t,
        finalAttempt: ev.attempt_id,
        refining: false,
        attempts: setAttempt(t, ev.attempt_id, { status: "committed", detail: ev.reason }),
      };
      break;
    case "generation.cancelled": {
      const invisible = ev.reason === "spec_changed" && t.finalAttempt === null;
      t = {
        ...t,
        refining: t.refining || invisible,
        specRestarts: t.specRestarts + (ev.reason === "spec_changed" ? 1 : 0),
        attempts: setAttempt(t, ev.attempt_id, { status: ev.reason === "failed" ? "failed" : "cancelled", detail: ev.reason }),
      };
      break;
    }
    case "generation.rerouted":
      t = {
        ...t,
        finalAttempt: ev.to_attempt_id,
        reroutes: [
          ...t.reroutes,
          { from: ev.from_attempt_id, to: ev.to_attempt_id, reason: ev.reason, discardFromSeq: ev.discard_from_seq },
        ],
      };
      break;
    case "generation.delta":
      t = {
        ...t,
        refining: t.refining && ev.attempt_id !== t.finalAttempt,
        deltas: { ...t.deltas, [ev.attempt_id]: [...(t.deltas[ev.attempt_id] ?? []), { seq: ev.seq, text: ev.text }] },
      };
      break;
    case "generation.completed":
      t = {
        ...t,
        attempts: t.attempts.map((a) =>
          a.attemptId === ev.attempt_id
            ? { ...a, status: a.status === "running" ? "completed" : a.status, finishReason: ev.finish_reason, usage: ev.usage }
            : a,
        ),
      };
      break;
    case "turn.verified":
      t = { ...t, verification: ev.verification };
      break;
    case "error":
      t = { ...t, errors: [...t.errors, { code: String(ev.code), message: ev.message, retryable: ev.retryable }] };
      break;
    case "turn.completed":
      t = {
        ...t,
        status: "completed",
        refining: false,
        usage: ev.usage,
        timings: ev.timings,
        specRestarts: ev.timings.spec_restarts,
      };
      break;
  }
  return { ...t, text: committedText(t) };
}

/** A stored turn (GET /v1/turns/{id}) as the same view, so the inspector renders history and live turns alike. */
export function fromTurnObject(o: TurnObject): TurnView {
  const status = (outcome: string): AttemptStatus =>
    outcome === "committed" ? "committed" : outcome === "failed" ? "failed" : outcome === "not_selected" ? "not_selected" : "cancelled";
  return {
    ...emptyTurn(),
    turnId: o.turn_id,
    sessionId: o.session_id,
    status: "completed",
    text: o.text,
    specRestarts: o.timings.spec_restarts,
    degradations: o.degradations,
    routing: o.routing,
    routingConfirmed: true,
    decision: o.decision,
    verification: o.verification,
    attempts: o.attempts.map((a) => ({
      attemptId: a.attempt_id,
      routeId: a.route_id,
      model: a.model,
      speculative: false,
      status: status(a.outcome),
      detail: a.outcome,
      finishReason: null,
      usage: a.usage,
    })),
    usage: o.usage,
    timings: o.timings,
  };
}
