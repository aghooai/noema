import { TURN_EVENT_TYPES, TurnAccumulator } from "./accumulator.js";
import { AghooConnectionError } from "./core/errors.js";
import type { SseMessage } from "./core/sse.js";
import type { TurnStream } from "./sdk.js";
import type { DecisionResult, TurnEvent } from "./types.js";

/** What a TurnStream needs from the client. */
export interface TurnStreamSource {
  /**
   * Starts the HTTP request. `conn` closes the connection (a disconnect, never a cancel by itself); `stop` ends the
   * retry loop (its backoff is cut short and no new attempt starts) while an attempt in flight stays open.
   */
  open(conn: AbortSignal, stop: AbortSignal): Promise<AsyncGenerator<SseMessage>>;
  /** POST /v1/turns/{id}/cancel. */
  cancel(turnId: string): Promise<unknown>;
  /** The caller's AbortSignal: aborting it cancels the turn on the server and closes the stream. */
  signal?: AbortSignal | undefined;
  /** Known up front when resuming. */
  turnId?: string | undefined;
}

function isTurnEvent(value: unknown): value is TurnEvent {
  return (
    typeof value === "object" &&
    value !== null &&
    TURN_EVENT_TYPES.has((value as { type?: unknown }).type as string)
  );
}

/**
 * Async iterable over a turn's SSE events (iterable once). The request starts when the stream is created.
 * `decision()` and `finalText()` settle with `turn.completed`; called before any iteration they consume the stream
 * themselves. Unknown event types are skipped; SSE `error` events are yielded, not thrown. The caller's `signal` is
 * listened to only until the stream settles (or `turn.completed` arrives), so one signal can serve many streams.
 */
export class TurnStreamImpl implements TurnStream {
  readonly #acc = new TurnAccumulator();
  readonly #conn = new AbortController();
  /** Aborted before the turn id is known: the open stops retrying (no new POST for a turn the caller gave up on). */
  readonly #stop = new AbortController();
  /** Removes the listeners on the caller's signal once the stream settles. */
  readonly #detach = new AbortController();
  readonly #src: TurnStreamSource;
  readonly #messages: Promise<AsyncGenerator<SseMessage>>;
  readonly #events: AsyncGenerator<TurnEvent>;
  readonly #done: Promise<void>;
  #resolveDone!: () => void;
  #rejectDone!: (e: unknown) => void;
  #settled = false;
  #consumed = false;
  #abortRequested = false;
  /** The open was stopped before any turn existed: the stream ends empty, there is nothing to cancel. */
  #stopped = false;
  #userAbortReason: unknown = undefined;
  #userAborted: Promise<never> | null = null;
  #cancel: Promise<unknown> | null = null;
  #lastEventId: string | null = null;

  constructor(src: TurnStreamSource) {
    this.#src = src;
    this.#acc.turnId = src.turnId ?? null;
    this.#done = new Promise<void>((resolve, reject) => {
      this.#resolveDone = resolve;
      this.#rejectDone = reject;
    });
    this.#done.catch(() => undefined);
    const user = src.signal;
    if (user !== undefined) {
      const listen = { once: true, signal: this.#detach.signal } as const;
      this.#userAborted = new Promise<never>((_, reject) => {
        if (user.aborted) reject(user.reason);
        else user.addEventListener("abort", () => reject(user.reason), listen);
      });
      this.#userAborted.catch(() => undefined);
      if (!user.aborted) user.addEventListener("abort", () => this.#onUserAbort(user.reason), listen);
    }
    this.#messages = user?.aborted ? Promise.reject(user.reason) : src.open(this.#conn.signal, this.#stop.signal);
    this.#messages.catch(() => undefined);
    this.#events = this.#pull();
  }

  /** The turn id, once `turn.created` arrived (or the resumed turn's id). */
  get turnId(): string | null {
    return this.#acc.turnId;
  }

  /** The SSE `id` of the last event received (send it as `lastEventId` to resume, Phase 2). */
  get lastEventId(): string | null {
    return this.#lastEventId;
  }

  [Symbol.asyncIterator](): AsyncIterator<TurnEvent> {
    if (this.#consumed) throw new TypeError("a TurnStream can be iterated only once");
    this.#consumed = true;
    return this.#iterate();
  }

  decision(): Promise<DecisionResult | null> {
    this.#drainIfIdle();
    return this.#done.then(() => this.#acc.decision);
  }

  finalText(): Promise<string> {
    this.#drainIfIdle();
    return this.#done.then(() => this.#acc.text);
  }

  /**
   * Explicit, final abort: POST /v1/turns/{id}/cancel (once the turn id is known); the stream then ends normally.
   * Before turn.created, an attempt in flight is read until its turn id arrives (then cancelled), and a retry backoff
   * ends without a new attempt (the stream then ends empty: no turn exists).
   */
  abort(): void {
    if (this.#abortRequested || this.#acc.completed) return;
    this.#abortRequested = true;
    if (this.#acc.turnId !== null) {
      this.#sendCancel();
      return;
    }
    this.#stop.abort(); // no new attempt; the cancel goes out when turn.created arrives
    this.#drainIfIdle();
  }

  #sendCancel(): void {
    const id = this.#acc.turnId;
    if (this.#cancel !== null || id === null) return;
    this.#cancel = this.#src.cancel(id).catch(() => undefined);
  }

  #onUserAbort(reason: unknown): void {
    if (this.#acc.completed || this.#settled) return; // nothing left to cancel or close
    this.#userAbortReason = reason;
    this.#abortRequested = true;
    if (this.#acc.turnId !== null) {
      this.#sendCancel();
      this.#conn.abort(reason);
      return;
    }
    // The turn id is not known yet: start no new attempt, read the one in flight until turn.created, cancel, close.
    this.#stop.abort();
    void (async () => {
      try {
        while (this.#acc.turnId === null && !this.#conn.signal.aborted) {
          if ((await this.#events.next()).done) break;
        }
      } catch {
        // the request failed or the connection closed: there is nothing to cancel
      } finally {
        this.#conn.abort(reason);
      }
    })();
  }

  /** The single reader of the connection: parses, reconciles, and yields known events. */
  async *#pull(): AsyncGenerator<TurnEvent> {
    let messages: AsyncGenerator<SseMessage>;
    try {
      messages = await this.#messages;
    } catch (e) {
      if (!this.#stop.signal.aborted || e !== this.#stop.signal.reason) throw e;
      this.#stopped = true; // aborted before any turn existed
      return;
    }
    for await (const msg of messages) {
      this.#lastEventId = msg.id;
      let data: unknown;
      try {
        data = JSON.parse(msg.data);
      } catch (e) {
        throw new AghooConnectionError("malformed SSE event data", { cause: e });
      }
      if (!isTurnEvent(data)) continue;
      this.#acc.apply(data);
      if (data.type === "turn.completed") this.#detach.abort(); // an abort after this point has nothing to do
      if (data.type === "turn.created" && this.#abortRequested) {
        this.#sendCancel();
        if (this.#userAbortReason !== undefined) this.#conn.abort(this.#userAbortReason);
      }
      yield data;
      if (data.type === "turn.completed") return; // always the last event
    }
  }

  async *#iterate(): AsyncGenerator<TurnEvent> {
    try {
      for (;;) {
        const next = this.#events.next();
        const r = this.#userAborted === null ? await next : await Promise.race([next, this.#userAborted]);
        if (r.done) break;
        yield r.value;
      }
      if (!this.#acc.completed && !this.#stopped) throw new AghooConnectionError("stream ended before turn.completed");
      this.#settle(null);
    } catch (e) {
      this.#settle(e);
      throw e;
    } finally {
      if (!this.#settled) this.#settle(new AghooConnectionError("stream closed before turn.completed"));
      if (this.#userAbortReason === undefined) void this.#events.return(undefined).catch(() => undefined);
    }
  }

  #drainIfIdle(): void {
    if (this.#consumed) return;
    this.#consumed = true;
    void (async () => {
      for await (const _ of this.#iterate()) {
        // reconciled by #pull
      }
    })().catch(() => undefined);
  }

  #settle(error: unknown): void {
    if (this.#settled) return;
    this.#settled = true;
    this.#detach.abort();
    if (error === null) this.#resolveDone();
    else this.#rejectDone(error);
  }
}
