import type * as Contract from "./sdk.js";
import type { ChoiceOption, ChoiceQuestion, PredicateQuestion, ScoreQuestion } from "./types.js";

type Criteria = { true?: string; false?: string };

function boolean<T extends "predicate" | "noul">(type: T, name: string, opts: { instructions: string; criteria?: Criteria }) {
  const q: PredicateQuestion<string, T> = { type, name, instructions: opts.instructions };
  if (opts.criteria !== undefined) q.criteria = opts.criteria;
  return q;
}

/**
 * Question builders. Names and option values are inferred as literal types, so `decisions.create` returns answers typed
 * per question (`byName`). The server validates the questions; the builders only shape them.
 */
export const q: typeof Contract.q = {
  predicate: (name, opts) => boolean("predicate", name, opts) as PredicateQuestion<typeof name, "predicate">,
  noul: (name, opts) => boolean("noul", name, opts) as PredicateQuestion<typeof name, "noul">,
  choice: <const N extends string, const C extends readonly ChoiceOption[]>(
    name: N,
    opts: { instructions: string; choices: C },
  ): ChoiceQuestion<N, C[number]["value"]> => ({
    type: "choice",
    name,
    instructions: opts.instructions,
    choices: opts.choices as readonly ChoiceOption<C[number]["value"]>[],
  }),
  score: (name, opts) => ({ type: "score", name, instructions: opts.instructions, levels: opts.levels }),
};
