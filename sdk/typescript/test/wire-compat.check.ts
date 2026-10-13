/** Compile-time wire-compat check (spec-08 §10.3, verbatim): generated wire types vs the vendored public types. */
import type { components } from "../src/generated/openapi";
import type * as T from "../src/types";            // vendored contracts/types.ts
import type * as S from "../src/sdk";              // vendored contracts/sdk.ts
import type * as O from "../src/openai-compat";    // vendored contracts/openai-compat.ts
type W = components["schemas"];
type Assert<X extends Y, Y> = X;
/** openapi-typescript drops allOf refinements of the answer union (e.g. boolean predicate candidates); answers are
 *  covered by the runtime client-model checks instead, so they are compared everywhere except inside DecisionResult. */
type NoAnswers<X> = X extends readonly (infer E)[] ? NoAnswers<E>[] : X extends { object: "decision"; answers: unknown } ? { [K in Exclude<keyof X, "answers">]: NoAnswers<X[K]> } : X extends object ? { [K in keyof X]: NoAnswers<X[K]> } : X;
type DeepMutable<X> = X extends readonly (infer E)[] ? DeepMutable<E>[] : X extends object ? { -readonly [K in keyof X]: DeepMutable<X[K]> } : X;
export type _1 = Assert<NoAnswers<W["TurnEvent"]>, NoAnswers<T.TurnEvent>>;
export type _2 = Assert<NoAnswers<W["TurnObject"]>, NoAnswers<S.TurnObject>>;
export type _3 = Assert<NoAnswers<W["DecisionResult"]>, NoAnswers<T.DecisionResult>>;
export type _4 = Assert<W["JevDecisionResponse"], T.JevDecisionResponse>;
export type _5 = Assert<W["Session"], S.Session>;
export type _6 = Assert<W["FileObject"], S.FileObject>;
export type _7 = Assert<W["ProfileInfo"], S.ProfileInfo>;
export type _8 = Assert<W["SessionTurn"], S.SessionTurn>;
export type _9 = Assert<W["UsageRow"], S.UsageRow>;
export type _10 = Assert<W["CalibratorInfo"], S.CalibratorInfo>;
export type _11 = Assert<W["LabelingRun"], S.LabelingRun>;
export type _12 = Assert<W["LabelingDroppedItem"], S.LabelingDroppedItem>;
export type _13 = Assert<W["Error"], T.ErrorEnvelope>;
export type _14 = Assert<W["OADecisionResponse"], O.OADecisionResponse>;
export type _15 = Assert<DeepMutable<S.TurnCreateParams>, W["TurnCreateParams"]>;
export type _16 = Assert<DeepMutable<T.DecisionCreateParams>, W["DecisionCreateParams"]>;
export type _17 = Assert<S.SessionUpdate, W["SessionUpdate"]>;
export type _18 = Assert<O.OADecisionCreateParams, W["OADecisionCreateParams"]>;
