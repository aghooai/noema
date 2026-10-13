import { normalizeDecision } from "./answers.js";
import type { DecisionResult, TurnEvent } from "./types.js";

/** The closed set of event types this SDK knows; frames of any other type are skipped (03 §3.2). */
export const TURN_EVENT_TYPES: ReadonlySet<string> = new Set<TurnEvent["type"]>([
  "turn.created",
  "preflight.completed",
  "route.selected",
  "decision.completed",
  "decision.updated",
  "route.confirmed",
  "generation.started",
  "generation.delta",
  "generation.committed",
  "generation.cancelled",
  "generation.rerouted",
  "generation.completed",
  "turn.verified",
  "turn.completed",
  "error",
]);

interface Delta {
  seq: number;
  text: string;
}

/**
 * Client-side reconciliation of one turn's events (03 §3.2 "client obligations"): only the committed attempt's deltas
 * are text; on `generation.rerouted` the text from `discard_from_seq` onward is dropped and the new attempt continues;
 * `decision.updated` replaces the decision; `turn.completed` is final.
 */
export class TurnAccumulator {
  turnId: string | null = null;
  decision: DecisionResult | null = null;
  completed = false;
  readonly #deltas = new Map<string, Delta[]>();
  readonly #reroutes = new Map<string, { from: string; discardFromSeq: number | null }>();
  /** The attempt whose text is shown: the last committed or rerouted-to attempt. */
  #final: string | null = null;

  apply(ev: TurnEvent): void {
    switch (ev.type) {
      case "generation.delta": {
        const list = this.#deltas.get(ev.attempt_id);
        if (list === undefined) this.#deltas.set(ev.attempt_id, [{ seq: ev.seq, text: ev.text }]);
        else list.push({ seq: ev.seq, text: ev.text });
        break;
      }
      case "turn.created":
        this.turnId = ev.turn_id;
        break;
      case "decision.completed":
      case "decision.updated":
        this.decision = normalizeDecision(ev.decision);
        break;
      case "turn.verified":
        normalizeDecision(ev.verification);
        break;
      case "generation.committed":
        this.#final = ev.attempt_id;
        break;
      case "generation.rerouted":
        this.#reroutes.set(ev.to_attempt_id, { from: ev.from_attempt_id, discardFromSeq: ev.discard_from_seq });
        this.#final = ev.to_attempt_id;
        break;
      case "turn.completed":
        this.completed = true;
        break;
      default:
        break;
    }
  }

  /** Committed text only: deltas of discarded (cancelled, uncommitted) attempts are excluded. */
  get text(): string {
    return this.#final === null ? "" : this.#chain(this.#final, null, new Set());
  }

  #chain(attempt: string, before: number | null, seen: Set<string>): string {
    if (seen.has(attempt)) return "";
    seen.add(attempt);
    const reroute = this.#reroutes.get(attempt);
    const prefix = reroute === undefined ? "" : this.#chain(reroute.from, reroute.discardFromSeq, seen);
    let own = "";
    for (const d of this.#deltas.get(attempt) ?? []) if (before === null || d.seq < before) own += d.text;
    return prefix + own;
  }
}
