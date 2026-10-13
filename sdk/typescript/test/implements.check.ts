/**
 * Compile-time check that the hand-written implementation satisfies the vendored declaration-only surface
 * (contracts/sdk.ts): `tsc` fails if a method, overload or field drifts from the contract.
 */
import { AghooClient, AghooError, q, type TurnStreamImpl } from "../src/index";
import type * as S from "../src/sdk";

type Assert<X extends Y, Y> = X;
export type _client = Assert<AghooClient, S.AghooClient>;
export type _constructor = Assert<typeof AghooClient, typeof S.AghooClient>;
export type _error = Assert<AghooError, S.AghooError>;
export type _builders = Assert<typeof q, typeof S.q>;
export type _stream = Assert<TurnStreamImpl, S.TurnStream>;
export type _streamReturn = Assert<ReturnType<AghooClient["turns"]["stream"]>, S.TurnStream>;
