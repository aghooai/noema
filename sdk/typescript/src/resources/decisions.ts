import { normalizeDecision, withByName } from "../answers.js";
import type { Transport } from "../core/transport.js";
import type { RequestOptions } from "../sdk.js";
import type { DecisionCreateParams, DecisionResult, JevDecisionResponse, Question, TypedDecisionResult } from "../types.js";
import { seg } from "./shared.js";

/** Native and Jev decisions (`POST /v1/decisions`, `GET /v1/decisions/{id}`). */
export class Decisions {
  readonly #t: Transport;
  constructor(t: Transport) {
    this.#t = t;
  }

  /** Jev-shaped output: answers keyed by name, 2-decimal values. */
  create<const Qs extends readonly Question[]>(
    params: DecisionCreateParams<Qs> & { output_format: "jev" },
    opts?: RequestOptions,
  ): Promise<JevDecisionResponse<Qs>>;
  /** Native DecisionResult plus the non-enumerable `byName`, typed per question. */
  create<const Qs extends readonly Question[]>(
    params: DecisionCreateParams<Qs> & { output_format?: "native" },
    opts?: RequestOptions,
  ): Promise<TypedDecisionResult<Qs>>;
  async create(
    params: DecisionCreateParams,
    opts: RequestOptions = {},
  ): Promise<JevDecisionResponse | TypedDecisionResult<readonly Question[]>> {
    const body = await this.#t.json<DecisionResult | JevDecisionResponse>(
      { method: "POST", path: "/v1/decisions", json: params, idempotent: true },
      opts,
    );
    if (params.output_format === "jev") return body as JevDecisionResponse;
    return withByName(normalizeDecision(body as DecisionResult));
  }

  /** A stored decision, including late member merges. */
  async retrieve(decisionId: string, opts: RequestOptions = {}): Promise<DecisionResult> {
    const path = `/v1/decisions/${seg(decisionId)}`;
    return normalizeDecision(await this.#t.json<DecisionResult>({ method: "GET", path }, opts));
  }
}
