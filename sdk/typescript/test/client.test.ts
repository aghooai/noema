import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { AghooClient, AghooError, type DecisionResult, type JevDecisionResponse, q } from "../src/index";
import { BASE, client, example, fixture, respond, useServer } from "./helpers";

const server = useServer();

const questions = [
  q.choice("department", {
    instructions: "Which team should handle this message?",
    choices: [{ value: "billing" }, { value: "technical" }, { value: "shipping" }, { value: "other" }],
  }),
  q.predicate("needs_human", { instructions: "The customer explicitly asks for a human." }),
  q.score("urgency", { instructions: "How urgent?", levels: [{ label: "low" }, { label: "medium" }, { label: "high" }] }),
] as const;

describe("q builders", () => {
  it("build the wire question shapes (array choices keep order and groups; criteria only when given)", () => {
    expect(q.predicate("a", { instructions: "i" })).toEqual({ type: "predicate", name: "a", instructions: "i" });
    expect(q.noul("b", { instructions: "i", criteria: { true: "t" } })).toEqual({
      type: "noul",
      name: "b",
      instructions: "i",
      criteria: { true: "t" },
    });
    const sizes = q.choice("size", { instructions: "i", choices: [{ value: "10", group: "s" }, { value: "2", group: "s" }] });
    expect(sizes.choices.map((c) => c.value)).toEqual(["10", "2"]);
    expect(q.score("u", { instructions: "i", levels: [{ label: "lo", description: null }, { label: "hi" }] })).toEqual({
      type: "score",
      name: "u",
      instructions: "i",
      levels: [{ label: "lo", description: null }, { label: "hi" }],
    });
  });
});

describe("decisions", () => {
  it("create returns the native result with a non-enumerable byName index", async () => {
    const request = example<Record<string, unknown>>("decision-request.valid");
    let sent: unknown;
    server.use(
      http.post(`${BASE}/v1/decisions`, async ({ request: r }) => {
        sent = await r.json();
        return HttpResponse.json(example("decision-result.valid"));
      }),
    );
    const d = await client().decisions.create({ ...request, questions } as never);
    expect(sent).toEqual({ ...request, questions });
    const typed = d as unknown as { byName: Record<string, { type: string; choice?: string }> };
    expect(typed.byName.department?.type).toBe("choice");
    expect(Object.keys(typed.byName)).toEqual(["department", "needs_human", "urgency"]);
    expect(Object.keys(d)).not.toContain("byName");
    expect(JSON.parse(JSON.stringify(d))).toEqual(example("decision-result.valid"));
  });

  it("create with output_format jev returns the Jev shape unchanged", async () => {
    server.use(http.post(`${BASE}/v1/decisions`, () => HttpResponse.json(example("decision-jev-response.valid"))));
    const r: JevDecisionResponse = await client().decisions.create({
      ...(example("decision-request.valid-jev") as object),
      output_format: "jev",
    } as never);
    expect(r).toEqual(example("decision-jev-response.valid"));
  });

  it("retrieve normalizes answers of unknown types", async () => {
    const stored = example<DecisionResult>("decision-result.valid");
    (stored.answers as unknown as Record<string, unknown>[]).push({ type: "ranking", name: "order" });
    server.use(http.get(`${BASE}/v1/decisions/dec_1`, () => HttpResponse.json(stored)));
    const d = await client().decisions.retrieve("dec_1");
    expect(d.answers.at(-1)).toEqual({ type: "unknown", name: "order", raw_type: "ranking", raw: { type: "ranking", name: "order" } });
  });

  it("maps an error envelope to AghooError (status, type, code, param, requestId, retryable, retryAfterS)", async () => {
    server.use(http.post(`${BASE}/v1/decisions`, () => respond(fixture("createDecision.400"))));
    const err = (await client()
      .decisions.create({ input: "x", questions })
      .catch((e: unknown) => e)) as AghooError;
    expect(err).toBeInstanceOf(AghooError);
    expect(err).toMatchObject({
      name: "AghooError",
      status: 400,
      type: "invalid_request_error",
      code: "schema_violation",
      param: "questions[0].choices",
      requestId: "req_01JA0000000000000000000000",
      retryable: false,
      retryAfterS: null,
      details: undefined,
      message: "questions[0].choices: too short",
    });
  });
});

describe("profiles and sessions", () => {
  it("profiles.list sends kind and returns data", async () => {
    let url = "";
    server.use(
      http.get(`${BASE}/v1/profiles`, ({ request }) => {
        url = request.url;
        return respond(fixture("listProfiles.200"));
      }),
    );
    const profiles = await client().profiles.list({ kind: "generation" });
    expect(new URL(url).searchParams.get("kind")).toBe("generation");
    expect(profiles.map((p) => p.version)).toEqual(["1", "7"]);
  });

  it("sessions: create, retrieve, update (PATCH, no Idempotency-Key), delete", async () => {
    const seen: { method: string; key: string | null; body: unknown }[] = [];
    const record = async (request: Request) =>
      seen.push({ method: request.method, key: request.headers.get("idempotency-key"), body: await request.text() });
    server.use(
      http.post(`${BASE}/v1/sessions`, async ({ request }) => (await record(request), respond(fixture("createSession.201")))),
      http.get(`${BASE}/v1/sessions/ses_1`, () => respond(fixture("getSession.200"))),
      http.patch(`${BASE}/v1/sessions/ses_1`, async ({ request }) => (await record(request), respond(fixture("updateSession.200")))),
      http.delete(`${BASE}/v1/sessions/ses_1`, () => respond(fixture("deleteSession.202"))),
    );
    const c = client();
    expect((await c.sessions.create({ metadata: { app: "support" } })).id).toBe("ses_1");
    expect((await c.sessions.retrieve("ses_1")).last_seq).toBe(4);
    expect((await c.sessions.update("ses_1", { data_class: "confidential" })).data_class).toBe("confidential");
    expect(await c.sessions.delete("ses_1")).toEqual({ id: "ses_1", deleted: true, deletion_job_id: "job_01JA" });
    expect(seen[0]).toMatchObject({ method: "POST", body: '{"metadata":{"app":"support"}}' });
    expect(seen[0]!.key).toMatch(/^[0-9a-f-]{36}$/);
    expect(seen[1]).toEqual({ method: "PATCH", key: null, body: '{"data_class":"confidential"}' });
  });

  it("sessions.list and sessions.turns page lazily via next_cursor", async () => {
    const urls: URL[] = [];
    server.use(
      http.get(`${BASE}/v1/sessions`, ({ request }) => {
        const u = new URL(request.url);
        urls.push(u);
        return respond(fixture(u.searchParams.get("after") === "c2" ? "listSessions.page2" : "listSessions.page1"));
      }),
      http.get(`${BASE}/v1/sessions/ses_1/turns`, ({ request }) => {
        const u = new URL(request.url);
        urls.push(u);
        return respond(fixture(u.searchParams.get("after") === "t2" ? "listSessionTurns.page2" : "listSessionTurns.page1"));
      }),
    );
    const c = client();
    const ids: string[] = [];
    for await (const s of c.sessions.list({ limit: 2 })) {
      ids.push(s.id);
      if (ids.length === 1) expect(urls).toHaveLength(1); // the second page is not fetched yet
    }
    expect(ids).toEqual(["ses_3", "ses_2", "ses_1"]);
    expect(urls.map((u) => [u.searchParams.get("limit"), u.searchParams.get("after")])).toEqual([
      ["2", null],
      ["2", "c2"],
    ]);
    const turns: string[] = [];
    for await (const t of c.sessions.turns("ses_1", { after: "t2" })) turns.push(`${t.seq}:${t.role}`);
    expect(turns).toEqual(["2:user", "2:assistant"]);
  });

  it("a 404 is thrown as AghooError and is not retried", async () => {
    let n = 0;
    server.use(http.get(`${BASE}/v1/sessions/ses_x`, () => (n++, respond(fixture("getSession.404")))));
    await expect(client().sessions.retrieve("ses_x")).rejects.toMatchObject({ status: 404, code: "session_not_found" });
    expect(n).toBe(1);
  });

  it("omits Authorization without an API key and reads AGHOO_API_KEY / AGHOO_BASE_URL", async () => {
    const auth: (string | null)[] = [];
    server.use(http.get(`${BASE}/v1/profiles`, ({ request }) => (auth.push(request.headers.get("authorization")), respond(fixture("listProfiles.200")))));
    const saved = { key: process.env.AGHOO_API_KEY, url: process.env.AGHOO_BASE_URL };
    try {
      delete process.env.AGHOO_API_KEY;
      process.env.AGHOO_BASE_URL = `${BASE}/`;
      await new AghooClient({}).profiles.list();
      process.env.AGHOO_API_KEY = "ak_test_env.secret";
      await new AghooClient().profiles.list();
    } finally {
      if (saved.key === undefined) delete process.env.AGHOO_API_KEY;
      else process.env.AGHOO_API_KEY = saved.key;
      if (saved.url === undefined) delete process.env.AGHOO_BASE_URL;
      else process.env.AGHOO_BASE_URL = saved.url;
    }
    expect(auth).toEqual([null, "Bearer ak_test_env.secret"]);
  });
});
