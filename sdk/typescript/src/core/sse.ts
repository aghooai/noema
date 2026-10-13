/** One dispatched server-sent event (WHATWG HTML §9.2 parsing rules). */
export interface SseMessage {
  /** The stream's last event id when this event was dispatched (it persists across events), or null. */
  id: string | null;
  /** `event:` field; "message" when absent. */
  event: string;
  /** `data:` lines joined with "\n". */
  data: string;
}

/**
 * Incremental SSE parser. `push` takes decoded text chunks of any size (a CRLF may be split across chunks) and returns
 * the events completed by that chunk. Comments (`: ping`) and `retry:` are ignored; an event without a trailing blank
 * line at end of stream is discarded, as the spec requires. Each character is scanned once, so a large frame that
 * arrives in many small chunks stays linear.
 */
export class SseParser {
  /** The current line's text so far. */
  #pending: string[] = [];
  /** The previous chunk ended with a CR: a leading LF completes that CRLF. */
  #skipLf = false;
  #started = false;
  #data: string[] = [];
  #event = "";
  #lastId: string | null = null;

  push(chunk: string): SseMessage[] {
    let text = chunk;
    if (text.length === 0) return [];
    if (!this.#started) {
      this.#started = true;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    }
    if (this.#skipLf) {
      this.#skipLf = false;
      if (text.charCodeAt(0) === 10) text = text.slice(1);
    }
    const out: SseMessage[] = [];
    let start = 0;
    const n = text.length;
    for (let i = 0; i < n; i++) {
      const c = text.charCodeAt(i);
      if (c !== 10 && c !== 13) continue;
      let line = text.slice(start, i);
      if (this.#pending.length > 0) {
        this.#pending.push(line);
        line = this.#pending.join("");
        this.#pending = [];
      }
      this.#line(line, out);
      if (c === 13 && i + 1 < n && text.charCodeAt(i + 1) === 10) i++;
      start = i + 1;
    }
    if (start < n) this.#pending.push(text.slice(start));
    this.#skipLf = text.charCodeAt(n - 1) === 13;
    return out;
  }

  /** End of stream: an unterminated line or event is dropped. */
  end(): SseMessage[] {
    this.#pending = [];
    this.#data = [];
    return [];
  }

  get lastEventId(): string | null {
    return this.#lastId;
  }

  #line(line: string, out: SseMessage[]): void {
    if (line.length === 0) {
      if (this.#data.length > 0) {
        out.push({ id: this.#lastId, event: this.#event || "message", data: this.#data.join("\n") });
      }
      this.#data = [];
      this.#event = "";
      return;
    }
    if (line.charCodeAt(0) === 58) return; // ":" comment, e.g. ": ping"
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.charCodeAt(0) === 32) value = value.slice(1);
    switch (field) {
      case "data":
        this.#data.push(value);
        break;
      case "event":
        this.#event = value;
        break;
      case "id":
        if (!value.includes("\u0000")) this.#lastId = value;
        break;
      default:
        break; // "retry" and unknown fields are ignored
    }
  }
}

/** Reads a response body as SSE messages. Throws whatever the body reader throws (an abort, a network reset). */
export async function* readSse(body: ReadableStream<Uint8Array>, parser = new SseParser()): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
    yield* parser.push(decoder.decode());
    yield* parser.end();
  } finally {
    reader.releaseLock();
    await body.cancel().catch(() => undefined);
  }
}
