import { describe, expect, it } from "vitest";
import { readSse, SseParser, type SseMessage } from "../src/core/sse";

function parseAll(chunks: string[]): SseMessage[] {
  const p = new SseParser();
  return [...chunks.flatMap((c) => p.push(c)), ...p.end()];
}

describe("SseParser", () => {
  it("parses id, event and a single data line", () => {
    expect(parseAll(['id: 1\nevent: turn.created\ndata: {"type":"turn.created"}\n\n'])).toEqual([
      { id: "1", event: "turn.created", data: '{"type":"turn.created"}' },
    ]);
  });

  it("joins multi-line data, defaults the event name and keeps the last id", () => {
    expect(parseAll(["id: 7\ndata: a\ndata: b\n\ndata: c\n\n"])).toEqual([
      { id: "7", event: "message", data: "a\nb" },
      { id: "7", event: "message", data: "c" },
    ]);
  });

  it("ignores comments (pings), retry and unknown fields; a lone blank line dispatches nothing", () => {
    expect(parseAll([": ping\n\nretry: 100\nfoo: bar\n\nid: 2\nevent: x\ndata: {}\n\n"])).toEqual([
      { id: "2", event: "x", data: "{}" },
    ]);
  });

  it("handles CRLF, CR and LF line endings, including a CRLF split across chunks", () => {
    expect(parseAll(["id: 1\r", "\nevent: e\rdata: d\r\n\r", "\n"])).toEqual([{ id: "1", event: "e", data: "d" }]);
  });

  it("strips one leading space of a value, a BOM at stream start, and accepts fields without a colon", () => {
    expect(parseAll(["\uFEFFdata:  two spaces\n\ndata\n\n"])).toEqual([
      { id: null, event: "message", data: " two spaces" },
      { id: null, event: "message", data: "" },
    ]);
  });

  it("splits events at any byte boundary", () => {
    const text = 'id: 1\nevent: a\ndata: {"x":"é…"}\n\nid: 2\nevent: b\ndata: 2\n\n';
    const expected = parseAll([text]);
    for (let size = 1; size < 9; size++) {
      const chunks = text.match(new RegExp(`[\\s\\S]{1,${size}}`, "g")) ?? [];
      expect(parseAll(chunks)).toEqual(expected);
    }
  });

  it("discards an unterminated event at end of stream and ignores ids containing NUL", () => {
    expect(parseAll(["id: 1\ndata: done\n\nid: 2\u0000\ndata: partial"])).toEqual([
      { id: "1", event: "message", data: "done" },
    ]);
  });

  it("reads a byte stream with multi-byte characters split across chunks", async () => {
    const bytes = new TextEncoder().encode("data: héllo\n\n");
    const body = new ReadableStream<Uint8Array>({
      start(ctl) {
        ctl.enqueue(bytes.slice(0, 8)); // splits the two-byte "é"
        ctl.enqueue(bytes.slice(8));
        ctl.close();
      },
    });
    const out: SseMessage[] = [];
    for await (const m of readSse(body)) out.push(m);
    expect(out).toEqual([{ id: null, event: "message", data: "héllo" }]);
  });

  it("parses a large frame in small chunks in linear time (2 MiB in 64-char chunks)", () => {
    const text = `id: 1\nevent: decision.completed\ndata: ${"x".repeat(2 << 20)}\n\n`;
    const p = new SseParser();
    const out: SseMessage[] = [];
    for (let i = 0; i < text.length; i += 64) out.push(...p.push(text.slice(i, i + 64)));
    expect(out).toHaveLength(1);
    expect(out[0]?.data.length).toBe(2 << 20);
  });
});
