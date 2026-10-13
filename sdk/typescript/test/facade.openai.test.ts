/**
 * Offline client checks for gate Q2 (spec-10 §3): the unmodified `openai` npm client parses the facade's contract
 * examples and shared error fixtures (served by msw; nothing here exercises the facade itself) after changing only
 * `baseURL` (POST /openai/v1/decisions on Noema; never OpenAI, D1). The gate itself runs against a real facade in
 * facade.openai.live.test.ts (NOEMA_FACADE_BASE_URL).
 */
import OpenAI from "openai";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OADecisionCreateParams, OADecisionResponse } from "../src/index";
import { API_KEY, BASE, example, fixture, respond, useServer } from "./helpers";

const server = useServer();
const FACADE = `${BASE}/openai/v1`;

interface Seen {
  body: unknown;
  auth: string | null;
  include: string | null;
}

function facade(...responses: (() => Response)[]): Seen[] {
  const seen: Seen[] = [];
  server.use(
    http.post(`${FACADE}/decisions`, async ({ request }) => {
      seen.push({
        body: await request.json(),
        auth: request.headers.get("authorization"),
        include: request.headers.get("x-aghoo-include"),
      });
      return responses[Math.min(seen.length - 1, responses.length - 1)]!();
    }),
  );
  return seen;
}

const openai = (maxRetries = 2) => new OpenAI({ apiKey: API_KEY, baseURL: FACADE, maxRetries });
const request = (name: string) => example<OADecisionCreateParams>(name);
const response = (name: string) => example<OADecisionResponse>(name);

describe("OpenAI facade conformance with the openai npm client", () => {
  it("acceptance: the client sends the facade request unchanged, with the bearer key", async () => {
    const seen = facade(() => HttpResponse.json(response("openai-response.valid")));
    const body = request("openai-request.valid");
    await openai().decisions.create(body as never);
    expect(seen).toEqual([{ body, auth: `Bearer ${API_KEY}`, include: null }]);
  });

  for (const name of ["openai-response.valid", "openai-response.valid-non-pattern-name"]) {
    it(`parsing: ${name} is returned as typed answers in request order`, async () => {
      facade(() => HttpResponse.json(response(name)));
      const req = name.endsWith("non-pattern-name") ? "openai-request.valid-non-pattern-name" : "openai-request.valid";
      const d = await openai().decisions.create(request(req) as never);
      expect(d).toEqual(response(name));
      expect(d.answers.map((a) => a.type)).toEqual(response(name).answers.map((a) => a.type));
      expect(d.usage.total_tokens).toBe(d.usage.input_tokens + d.usage.output_tokens);
    });
  }

  it("unavailable maps to refusal, with x-aghoo-include: ensemble (x_aghoo.mapped_unavailable)", async () => {
    const seen = facade(() => HttpResponse.json(response("openai-response.valid-ensemble")));
    const d = (await openai().decisions.create(request("openai-request.valid") as never, {
      headers: { "x-aghoo-include": "ensemble" },
    })) as unknown as OADecisionResponse;
    expect(seen[0]?.include).toBe("ensemble");
    const refusal = d.answers.find((a) => a.type === "refusal");
    expect(refusal?.name).toBe("regression");
    expect(d.x_aghoo?.mapped_unavailable?.regression).toBeDefined();
  });

  it("unavailable maps to refusal without the header (no x_aghoo)", async () => {
    const { x_aghoo: _, ...plain } = response("openai-response.valid-ensemble");
    facade(() => HttpResponse.json(plain));
    const d = await openai().decisions.create(request("openai-request.valid") as never);
    expect(d.answers.find((a) => a.name === "regression")).toEqual({ type: "refusal", name: "regression" });
    expect("x_aghoo" in d).toBe(false);
  });

  it("errors: the envelope is parsed by the openai client (status, type, code, param)", async () => {
    const seen = facade(() => respond(fixture("createDecisionOpenAICompat.422")));
    const err = await openai().decisions.create(request("openai-request.valid") as never).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(OpenAI.APIError);
    expect(err).toMatchObject({ status: 422, type: "capability_error", code: "unsupported_parameter", param: "questions[0].choices" });
    expect(seen).toHaveLength(1);
  });
});

describe("openai client retries on the facade's 429 and 503 (counted on the mock)", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }));
  afterEach(() => vi.useRealTimers());

  for (const code of ["createDecisionOpenAICompat.429", "createDecisionOpenAICompat.503"]) {
    it(`${code}: retried after retry-after, then answered`, async () => {
      const seen = facade(
        () => respond(fixture(code)),
        () => HttpResponse.json(response("openai-response.valid")),
      );
      const pending = openai().decisions.create(request("openai-request.valid") as never);
      await vi.waitFor(() => expect(seen).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(1_000); // retry-after: 1
      await expect(pending).resolves.toEqual(response("openai-response.valid"));
      expect(seen).toHaveLength(2);
    });
  }

  it("stops after maxRetries and surfaces the 503 envelope", async () => {
    const seen = facade(() => respond(fixture("createDecisionOpenAICompat.503")));
    const pending = openai(1)
      .decisions.create(request("openai-request.valid") as never)
      .catch((e: unknown) => e);
    await vi.waitFor(() => expect(seen).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(1_000);
    const err = await pending;
    expect(err).toMatchObject({ status: 503, code: "all_routes_unavailable" });
    expect(seen).toHaveLength(2);
  });
});
