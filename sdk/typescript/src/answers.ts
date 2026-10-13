import type { Answer, DecisionResult, JsonValue, Question, TypedDecisionResult, UnknownAnswer } from "./types.js";

const KNOWN_ANSWER_TYPES: ReadonlySet<string> = new Set([
  "predicate",
  "noul",
  "choice",
  "score",
  "refusal",
  "unavailable",
]);

/**
 * An answer whose `type` this SDK version doesn't know (a later additive change) becomes an UnknownAnswer that keeps
 * the wire type in `raw_type` and the payload in `raw`; known answers are returned unchanged.
 */
export function normalizeAnswer(raw: { [k: string]: JsonValue }): Answer {
  const type = raw.type;
  if (typeof type === "string" && KNOWN_ANSWER_TYPES.has(type)) return raw as unknown as Answer;
  const unknown: UnknownAnswer = {
    type: "unknown",
    name: typeof raw.name === "string" ? raw.name : null,
    raw_type: String(type),
    raw,
  };
  return unknown;
}

/** Normalizes the answers of a decision as parsed from the wire (in place; the object is the SDK's own copy). */
export function normalizeDecision<D extends DecisionResult | null>(decision: D): D {
  if (decision !== null && Array.isArray(decision.answers)) {
    decision.answers = (decision.answers as unknown as { [k: string]: JsonValue }[]).map(normalizeAnswer);
  }
  return decision;
}

/** Adds the non-enumerable `byName` index (answers keyed by question name) to a normalized decision. */
export function withByName<Qs extends readonly Question[]>(decision: DecisionResult): TypedDecisionResult<Qs> {
  const byName: Record<string, Answer> = {};
  for (const answer of decision.answers) {
    if (answer.name !== null) byName[answer.name] = answer;
  }
  Object.defineProperty(decision, "byName", { value: byName, enumerable: false, writable: false });
  return decision as TypedDecisionResult<Qs>;
}
