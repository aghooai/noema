import { normalizeDecision } from "../answers.js";
import type { Transport } from "../core/transport.js";
import type { RequestOptions, TurnCreateParams, TurnObject } from "../sdk.js";
import { TurnStreamImpl } from "../stream.js";
import { type Json, seg } from "./shared.js";

/** Options of `turns.stream`: `lastEventId` with the `turnId` it belongs to resumes that turn (Phase 2). */
export type TurnStreamOptions = RequestOptions & { lastEventId?: string; turnId?: string };

function normalizeTurn(turn: TurnObject): TurnObject {
  normalizeDecision(turn.decision);
  normalizeDecision(turn.verification);
  return turn;
}

/** Smart turns: SSE streams, non-streaming turns, retrieval and explicit cancel. */
export class Turns {
  readonly #t: Transport;
  constructor(t: Transport) {
    this.#t = t;
  }

  /**
   * Streams a new turn (`stream: true` is forced). The request starts now; retries happen only before the first event.
   * Aborting `signal` cancels the turn on the server and closes the stream. With `lastEventId` (plus its `turnId`) the
   * SDK resumes that turn instead (`GET /v1/turns/{id}/events`; Phase 1 servers answer 501 `not_implemented`).
   */
  stream(params: TurnCreateParams, opts: TurnStreamOptions = {}): TurnStreamImpl {
    const { lastEventId, turnId, signal, ...rest } = opts;
    if (lastEventId !== undefined && turnId === undefined) {
      throw new TypeError("resuming with lastEventId needs the turnId it belongs to");
    }
    const resume = lastEventId !== undefined && turnId !== undefined;
    return new TurnStreamImpl({
      signal,
      turnId: resume ? turnId : undefined,
      open: (conn, stop) =>
        resume
          ? this.#t.sse(
              { method: "GET", path: `/v1/turns/${seg(turnId)}/events`, headers: { "Last-Event-ID": lastEventId } },
              { ...rest, signal: conn },
              stop,
            )
          : this.#t.sse(
              { method: "POST", path: "/v1/turns", json: { ...params, stream: true }, idempotent: true },
              { ...rest, signal: conn },
              stop,
            ),
      cancel: (turn) => this.cancel(turn),
    });
  }

  /** A non-streaming turn (`stream: false` is forced): the reconciled TurnObject. */
  async create(params: TurnCreateParams, opts: RequestOptions = {}): Promise<TurnObject> {
    const spec = { method: "POST", path: "/v1/turns", json: { ...params, stream: false }, idempotent: true } as const;
    return normalizeTurn(await this.#t.json<TurnObject>(spec, opts));
  }

  async retrieve(turnId: string, opts: RequestOptions = {}): Promise<TurnObject> {
    return normalizeTurn(await this.#t.json<TurnObject>({ method: "GET", path: `/v1/turns/${seg(turnId)}` }, opts));
  }

  /** Idempotent explicit cancel; keeps committed partial text. */
  cancel(turnId: string, opts: RequestOptions = {}): Promise<Json<"cancelTurn", 202>> {
    return this.#t.json({ method: "POST", path: `/v1/turns/${seg(turnId)}/cancel`, idempotent: true }, opts);
  }
}
