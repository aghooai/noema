import type { Degradation, Session, SessionTurn } from "@aghoo/sdk";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { ChatStore } from "../src/chat/chatStore";
import { BASE, CSRF, expectedText, fixture, golden, goldenSse, respond, sseResponse, testApi, useServer } from "./helpers";

const server = useServer();

function sessionsHandlers(): void {
  const p1 = fixture("listSessions.page1");
  const p2 = fixture("listSessions.page2");
  const t1 = fixture("listSessionTurns.page1");
  const t2 = fixture("listSessionTurns.page2");
  server.use(
    http.get(`${BASE}/v1/sessions`, ({ request }) => respond(new URL(request.url).searchParams.has("after") ? p2 : p1)),
    http.get(`${BASE}/v1/sessions/:id/turns`, ({ request }) => respond(new URL(request.url).searchParams.has("after") ? t2 : t1)),
  );
}

describe("ChatStore (W1) over the SDK", () => {
  it("lists sessions across pages and loads a session's history oldest first", async () => {
    sessionsHandlers();
    const chat = new ChatStore(testApi().client);
    await chat.loadSessions();
    const all = [...((fixture("listSessions.page1").body as { data: Session[] }).data), ...((fixture("listSessions.page2").body as { data: Session[] }).data)];
    expect(chat.state.sessions.map((s) => s.id)).toEqual(all.map((s) => s.id));
    await chat.selectSession(all[0]!.id);
    const turns = [...((fixture("listSessionTurns.page1").body as { data: SessionTurn[] }).data), ...((fixture("listSessionTurns.page2").body as { data: SessionTurn[] }).data)];
    expect(chat.state.messages.map((m) => [m.role, m.text, m.turnId])).toEqual(turns.map((t) => [t.role, t.text, t.turn_id]));
  });

  it("streams a turn (G02 invisible restart): CSRF header, no Authorization, committed text, inspector follows", async () => {
    const g = golden("G02");
    const seen: { body: unknown; headers: Headers }[] = [];
    const refining: boolean[] = [];
    server.use(
      http.post(`${BASE}/v1/turns`, async ({ request }) => {
        seen.push({ body: await request.json(), headers: request.headers });
        return sseResponse(goldenSse(g));
      }),
    );
    const noted: Degradation[][] = [];
    const chat = new ChatStore(testApi().client, (d) => noted.push(d));
    chat.set({ sessionId: "ses_1" });
    chat.subscribe(() => refining.push(chat.state.live?.refining ?? false));
    await chat.send("What is the capital of France?");

    expect(seen).toHaveLength(1);
    expect(seen[0]?.body).toEqual({ session_id: "ses_1", stream: true, input: [{ role: "user", content: [{ type: "input_text", text: "What is the capital of France?" }] }] });
    expect(seen[0]?.headers.get("x-csrf-token")).toBe(CSRF);
    expect(seen[0]?.headers.get("authorization")).toBeNull();
    expect(chat.state.messages.map((m) => [m.role, m.text])).toEqual([
      ["user", "What is the capital of France?"],
      ["assistant", expectedText(g)],
    ]);
    expect(chat.state.live).toBeNull();
    expect(chat.state.sending).toBe(false);
    expect(chat.state.inspectedTurnId).toBe("turn_1");
    expect(chat.inspected()?.specRestarts).toBe(1);
    expect(refining).toContain(true);
    expect(noted.length).toBeGreaterThanOrEqual(3);
  });

  it("attachments become input parts (image -> input_image, else input_file) with wait_for_files", async () => {
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/v1/turns`, async ({ request }) => {
        body = await request.json();
        return sseResponse(goldenSse(golden("G01")));
      }),
    );
    const chat = new ChatStore(testApi().client);
    chat.set({ sessionId: "ses_1" });
    await chat.send("Compare", [
      { fileId: "file_1", filename: "a.pdf", mediaType: "application/pdf" },
      { fileId: "file_2", filename: "b.png", mediaType: "image/png" },
    ]);
    expect(body).toMatchObject({
      wait_for_files: true,
      input: [{ role: "user", content: [{ type: "input_text", text: "Compare" }, { type: "input_file", file_id: "file_1" }, { type: "input_image", file_id: "file_2" }] }],
    });
  });

  it("a typed error before the stream (409 session_conflict fixture) is shown and no assistant message is added", async () => {
    const fx = fixture("createTurn.409");
    server.use(http.post(`${BASE}/v1/turns`, () => respond(fx)));
    const chat = new ChatStore(testApi().client);
    chat.set({ sessionId: "ses_1" });
    await chat.send("hi");
    const code = (fx.body as { error: { code: string } }).error.code;
    expect(chat.state.sendError).toContain(code);
    expect(chat.state.messages.map((m) => m.role)).toEqual(["user"]);
  });

  it("inspect() of a stored turn reads GET /v1/turns/{id} (getTurn fixture) once", async () => {
    let calls = 0;
    server.use(
      http.get(`${BASE}/v1/turns/:id`, () => {
        calls += 1;
        return respond(fixture("getTurn.200"));
      }),
    );
    const chat = new ChatStore(testApi().client);
    await chat.inspect("turn_1");
    await chat.inspect("turn_1");
    expect(calls).toBe(1);
    expect(chat.inspected()?.routing?.route_id).toBe("sol-fast");
    expect(chat.state.inspectorLoading).toBe(false);
  });

  it("cancel() sends POST /v1/turns/{id}/cancel with the CSRF header", async () => {
    const g = golden("G07");
    const cancels: Headers[] = [];
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const head = g.events.slice(0, 10);
    const tail = g.events.slice(10);
    server.use(
      http.post(`${BASE}/v1/turns`, () => {
        const enc = new TextEncoder();
        const body = new ReadableStream<Uint8Array>({
          async start(ctl) {
            ctl.enqueue(enc.encode(goldenSse({ ...g, events: head })));
            await gate;
            ctl.enqueue(enc.encode(tail.map((e) => `id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify(e.data)}\n\n`).join("")));
            ctl.close();
          },
        });
        return new Response(body, { headers: { "content-type": "text/event-stream" } });
      }),
      http.post(`${BASE}/v1/turns/:id/cancel`, ({ request }) => {
        cancels.push(request.headers);
        release();
        return HttpResponse.json(fixture("cancelTurn.202").body as object, { status: 202 });
      }),
    );
    const chat = new ChatStore(testApi().client);
    chat.set({ sessionId: "ses_1" });
    const done = chat.send("hi");
    await new Promise<void>((r) => {
      const off = chat.subscribe(() => {
        if (chat.state.live?.text !== undefined && chat.state.live.text !== "") {
          off();
          r();
        }
      });
    });
    chat.cancel();
    await done;
    expect(cancels).toHaveLength(1);
    expect(cancels[0]?.get("x-csrf-token")).toBe(CSRF);
    expect(chat.state.messages.at(-1)?.text).toBe(expectedText(g));
  });
});
