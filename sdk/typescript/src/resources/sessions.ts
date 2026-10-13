import type { Transport } from "../core/transport.js";
import type { DataClass, RequestOptions, Session, SessionTurn, SessionUpdate } from "../sdk.js";
import { type Json, listPages, seg } from "./shared.js";

/** Sessions and their authorized history. */
export class Sessions {
  readonly #t: Transport;
  constructor(t: Transport) {
    this.#t = t;
  }

  create(
    params: { metadata?: Record<string, string>; retention?: Session["retention"]["policy"]; data_class?: DataClass } = {},
    opts: RequestOptions = {},
  ): Promise<Session> {
    return this.#t.json({ method: "POST", path: "/v1/sessions", json: params, idempotent: true }, opts);
  }

  retrieve(sessionId: string, opts: RequestOptions = {}): Promise<Session> {
    return this.#t.json({ method: "GET", path: `/v1/sessions/${seg(sessionId)}` }, opts);
  }

  /** Newest first; pages are fetched lazily. */
  list(params: { limit?: number } = {}, opts: RequestOptions = {}): AsyncIterable<Session> {
    return listPages<Session>(this.#t, "/v1/sessions", { limit: params.limit }, opts);
  }

  /** PATCH semantics: present fields replace the stored value; `metadata` replaces the whole map. */
  update(sessionId: string, params: SessionUpdate, opts: RequestOptions = {}): Promise<Session> {
    return this.#t.json({ method: "PATCH", path: `/v1/sessions/${seg(sessionId)}`, json: params }, opts);
  }

  /** Oldest first: each committed turn yields its user message, then its assistant message. */
  turns(
    sessionId: string,
    params: { limit?: number; after?: string } = {},
    opts: RequestOptions = {},
  ): AsyncIterable<SessionTurn> {
    const path = `/v1/sessions/${seg(sessionId)}/turns`;
    return listPages<SessionTurn>(this.#t, path, { limit: params.limit }, opts, params.after);
  }

  /** Cascading, asynchronous deletion. */
  delete(sessionId: string, opts: RequestOptions = {}): Promise<Json<"deleteSession", 202>> {
    return this.#t.json({ method: "DELETE", path: `/v1/sessions/${seg(sessionId)}` }, opts);
  }
}
