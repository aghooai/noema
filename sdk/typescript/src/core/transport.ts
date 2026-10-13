import type { RequestOptions } from "../sdk.js";
import { AghooConnectionError, AghooError, AghooTimeoutError, errorFromResponse } from "./errors.js";
import { DEFAULT_MAX_RETRIES, retryClock, retryDelayMs } from "./retry.js";
import { readSse, type SseMessage } from "./sse.js";

/** The `Aghoo-Version` this SDK was built against (the contract's only supported version). */
export const API_VERSION = "2026-10-01";
/** The contract's `servers[0].url`; set `baseURL` (or `AGHOO_BASE_URL`) for a real deployment. */
export const DEFAULT_BASE_URL = "https://api.aghoo.example";
/** Per-attempt timeout; equals the largest `Aghoo-Deadline-Ms` the API accepts. */
export const DEFAULT_TIMEOUT_MS = 600_000;

/** Constructor options; a superset of the contract's `{ apiKey?, baseURL?, fetch? }`. */
export interface ClientOptions {
  /** Bearer API key (`ak_<env>_<id>.<secret>`); default `AGHOO_API_KEY`. Omitted when unset (e.g. behind a BFF). */
  apiKey?: string;
  /** Default `AGHOO_BASE_URL`, else {@link DEFAULT_BASE_URL}. */
  baseURL?: string;
  fetch?: typeof fetch;
  /** Default retries per call (2). */
  maxRetries?: number;
  /** Default per-attempt timeout in ms; for streams it covers the time to the first event. */
  timeoutMs?: number;
}

export type Query = Readonly<Record<string, string | number | boolean | undefined>>;

/** One logical API call. */
export interface RequestSpec {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  path: string;
  query?: Query;
  json?: unknown;
  form?: FormData;
  /** The operation declares `Idempotency-Key`: one key (given or UUIDv4) is sent on every attempt. */
  idempotent?: boolean;
  accept?: string;
  headers?: Readonly<Record<string, string>>;
}

function envVar(name: string): string | undefined {
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return proc?.env?.[name];
}

function describe(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Set on an error that the server replayed from its idempotency store: the same key can only replay it again. */
const REPLAY_HEADER = "x-aghoo-idempotent-replay";

/** Rejects a path with an empty, `.` or `..` segment: URL parsing would resolve it (an id must never change the route). */
function checkPath(path: string): void {
  for (const part of path.split("/").slice(1)) {
    if (part === "" || part === "." || part === "..") {
      throw new TypeError(`invalid path segment ${JSON.stringify(part)} in ${path}: ids must not be empty, "." or ".."`);
    }
  }
}

/** HTTP transport: headers, timeouts, AbortSignal, retries before the first stream event. */
export class Transport {
  readonly #fetch: typeof fetch;
  readonly #baseURL: string;
  readonly #apiKey: string | undefined;
  readonly #maxRetries: number;
  readonly #timeoutMs: number;

  constructor(opts: ClientOptions) {
    this.#fetch = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.#baseURL = (opts.baseURL ?? envVar("AGHOO_BASE_URL") ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.#apiKey = opts.apiKey ?? envVar("AGHOO_API_KEY");
    this.#maxRetries = opts.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.#timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  url(path: string, query?: Query): string {
    let url = this.#baseURL + path;
    if (query !== undefined) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) if (v !== undefined) qs.append(k, String(v));
      const s = qs.toString();
      if (s) url += `?${s}`;
    }
    return url;
  }

  #headers(spec: RequestSpec, opts: RequestOptions): Record<string, string> {
    const h: Record<string, string> = { "Aghoo-Version": API_VERSION, Accept: spec.accept ?? "application/json" };
    if (this.#apiKey) h.Authorization = `Bearer ${this.#apiKey}`;
    if (spec.json !== undefined) h["Content-Type"] = "application/json";
    if (spec.idempotent) h["Idempotency-Key"] = opts.idempotencyKey ?? crypto.randomUUID();
    return { ...h, ...spec.headers };
  }

  /**
   * Sends one logical request and returns `read(res)` of its 2xx response. Retryable failures (connect errors,
   * timeouts, statuses whose envelope says `retryable`, a stream that ends before its first event) are retried up to
   * `maxRetries` times with the same headers, including the Idempotency-Key; an error the server replayed from its
   * idempotency store (`x-aghoo-idempotent-replay: true`) is thrown at once. A non-2xx answer is thrown as AghooError.
   * With `linked`, the caller's signal stays linked to the connection after `read` (streams) and `unlink` detaches it;
   * otherwise it is detached as soon as the call settles. Aborting `stop` ends the retry loop: the backoff rejects
   * with `stop.reason` and no new attempt starts, while an attempt in flight continues.
   */
  async send<T>(
    spec: RequestSpec,
    opts: RequestOptions,
    read: (res: Response) => Promise<T>,
    linked = false,
    stop?: AbortSignal,
  ): Promise<{ value: T; unlink: () => void }> {
    checkPath(spec.path);
    const maxRetries = opts.maxRetries ?? this.#maxRetries;
    const timeoutMs = opts.timeoutMs ?? this.#timeoutMs;
    const user = opts.signal;
    const url = this.url(spec.path, spec.query);
    const init = {
      method: spec.method,
      headers: this.#headers(spec, opts),
      body: spec.form ?? (spec.json === undefined ? null : JSON.stringify(spec.json)),
    };
    for (let attempt = 0; ; attempt++) {
      if (user?.aborted) throw user.reason;
      if (stop?.aborted) throw stop.reason;
      const ctl = new AbortController();
      const onAbort = (): void => ctl.abort(user?.reason);
      user?.addEventListener("abort", onAbort, { once: true });
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        ctl.abort(new AghooTimeoutError(timeoutMs));
      }, timeoutMs);
      let failure: AghooError | AghooConnectionError;
      let replayed = false;
      try {
        const res = await this.#fetch(url, { ...init, signal: ctl.signal });
        if (res.ok) {
          const value = await read(res);
          const unlink = (): void => user?.removeEventListener("abort", onAbort);
          if (!linked) unlink();
          return { value, unlink };
        }
        replayed = res.headers.get(REPLAY_HEADER) === "true";
        failure = errorFromResponse(res.status, res.headers, await res.text().catch(() => ""));
      } catch (e) {
        if (user?.aborted) throw user.reason;
        if (timedOut) failure = new AghooTimeoutError(timeoutMs);
        else if (e instanceof AghooError || e instanceof AghooConnectionError) failure = e;
        else failure = new AghooConnectionError(`request failed: ${describe(e)}`, { cause: e });
      } finally {
        clearTimeout(timer);
      }
      user?.removeEventListener("abort", onAbort);
      const retryable = failure instanceof AghooError ? failure.retryable && !replayed : true;
      const retryAfterS = failure instanceof AghooError ? failure.retryAfterS : null;
      const delay = retryable && attempt < maxRetries ? retryDelayMs(attempt, retryAfterS) : null;
      if (delay === null) throw failure;
      const backoff = stop === undefined || user === undefined ? (stop ?? user) : AbortSignal.any([user, stop]);
      await retryClock.sleep(delay, backoff);
    }
  }

  /** JSON request and response body. */
  async json<T>(spec: RequestSpec, opts: RequestOptions = {}): Promise<T> {
    return (await this.send(spec, opts, async (res) => JSON.parse(await res.text()) as T)).value;
  }

  /** Binary response body; `Blob.type` carries the content type. */
  async blob(spec: RequestSpec, opts: RequestOptions = {}): Promise<Blob> {
    return (await this.send(spec, opts, (res) => res.blob())).value;
  }

  /**
   * SSE response as messages. The first message is awaited inside the retry loop (so a stream that fails before its
   * first event is retried); later failures are thrown by the generator. `stop` ends the retry loop (see `send`).
   */
  async sse(spec: RequestSpec, opts: RequestOptions = {}, stop?: AbortSignal): Promise<AsyncGenerator<SseMessage>> {
    const { value, unlink } = await this.send(
      { ...spec, accept: "text/event-stream" },
      opts,
      async (res) => {
        if (res.body === null) throw new AghooConnectionError("stream response has no body");
        const it = readSse(res.body);
        const head = await it.next();
        if (head.done) throw new AghooConnectionError("stream ended before its first event");
        return { first: head.value, rest: it };
      },
      true,
      stop,
    );
    const { first, rest } = value;
    const signal = opts.signal;
    return (async function* () {
      yield first;
      try {
        yield* rest;
      } catch (e) {
        if (signal?.aborted) throw signal.reason;
        if (e instanceof AghooConnectionError) throw e;
        throw new AghooConnectionError(`stream interrupted: ${describe(e)}`, { cause: e });
      } finally {
        unlink();
      }
    })();
  }
}
