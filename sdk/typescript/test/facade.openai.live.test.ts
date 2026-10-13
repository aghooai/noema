/**
 * Gate Q2 (spec-10 §3), TS half, against a real facade: the unmodified `openai` npm client with
 * `baseURL = <server>/openai/v1` (never OpenAI, D1). Skipped unless a launcher serves the decision routes over the
 * wire mock (the scenarios of tests/integration/decision/test_facade_openai_sdk.py, on a fresh server) and sets:
 * - NOEMA_FACADE_BASE_URL: the server, e.g. http://127.0.0.1:<port>;
 * - NOEMA_FACADE_API_KEY: a key of a project with the router-default profile;
 * - NOEMA_FACADE_THROTTLED_API_KEY: a key whose first reservation is rejected with 429 (retry-after 1);
 * - NOEMA_FACADE_UNAVAILABLE_INSTRUCTIONS: question instructions for which every model call fails (`unavailable`;
 *   it is asked next to an answerable "DOWN check" question, since a decision with only unavailable answers is a 503).
 * Prompts with "FLAKY" fail each model's first two calls (one 503). Attempts are counted on the client's fetch.
 */
import OpenAI from "openai";
import { describe, expect, it } from "vitest";
import type { OADecisionCreateParams, OADecisionResponse } from "../src/index";
import { example } from "./helpers";

const env = {
  base: process.env.NOEMA_FACADE_BASE_URL,
  key: process.env.NOEMA_FACADE_API_KEY,
  throttledKey: process.env.NOEMA_FACADE_THROTTLED_API_KEY,
  unavailable: process.env.NOEMA_FACADE_UNAVAILABLE_INSTRUCTIONS,
};
const enabled = Object.values(env).every((v) => !!v);

/** An openai client on the real facade; `statuses` records the HTTP status of every attempt it makes. */
function facade(key: string) {
  const statuses: number[] = [];
  const oa = new OpenAI({
    apiKey: key,
    baseURL: `${env.base!.replace(/\/+$/, "")}/openai/v1`,
    maxRetries: 2,
    fetch: async (input, init) => {
      const res = await fetch(input, init);
      statuses.push(res.status);
      return res;
    },
  });
  return { oa, statuses };
}

const valid = () => example<OADecisionCreateParams>("openai-request.valid");
const ensemble = { headers: { "x-aghoo-include": "ensemble" } };
const create = (oa: OpenAI, body: unknown, opts?: typeof ensemble) =>
  oa.decisions.create(body as never, opts) as unknown as Promise<OADecisionResponse>;

describe.skipIf(!enabled)("Q2: the openai client against the real facade (NOEMA_FACADE_BASE_URL)", () => {
  it("acceptance and parsing: typed answers in request order, usage totals", async () => {
    const { oa, statuses } = facade(env.key!);
    const d = await create(oa, valid());
    expect(statuses).toEqual([200]);
    expect(d.model).toBe("aghoo/router-default@1");
    expect(d.answers).toEqual(example<OADecisionResponse>("openai-response.valid").answers);
    expect(d.usage.total_tokens).toBe(d.usage.input_tokens + d.usage.output_tokens);
    expect("x_aghoo" in d).toBe(false);
  });

  it("x-aghoo-include: ensemble adds x_aghoo (calibration per question, decision id)", async () => {
    const { oa } = facade(env.key!);
    const d = await create(oa, valid(), ensemble);
    expect(Object.keys(d.x_aghoo?.calibration ?? {}).sort()).toEqual(["q0", "regression"]);
    expect(d.x_aghoo?.decision_id).toMatch(/^dec_/);
  });

  it("unavailable maps to refusal, with and without x-aghoo-include: ensemble", async () => {
    const { oa } = facade(env.key!);
    const body = {
      model: "aghoo/router-default",
      input: "x",
      questions: [
        { type: "predicate", name: "flag", instructions: "DOWN check" },
        { type: "predicate", name: "gone", instructions: env.unavailable },
      ],
    };
    const plain = await create(oa, body);
    expect(plain.answers[0]?.type).toBe("predicate");
    expect(plain.answers[1]).toEqual({ type: "refusal", name: "gone" });
    expect("x_aghoo" in plain).toBe(false);
    const full = await create(oa, body, ensemble);
    expect(full.answers[1]).toEqual({ type: "refusal", name: "gone" });
    expect(full.x_aghoo?.mapped_unavailable?.gone).toBeDefined();
    expect(full.x_aghoo?.mapped_unavailable?.flag).toBeUndefined();
  });

  it("retries a 503 and a 429 (retry-after honored), then answers", async () => {
    const flaky = [{ type: "predicate", name: "flag", instructions: "FLAKY check" }];
    const a = facade(env.key!);
    const retried = await create(a.oa, { model: "aghoo/router-default", input: "x", questions: flaky });
    expect(a.statuses).toEqual([503, 200]);
    expect(retried.answers[0]?.type).toBe("predicate");
    const b = facade(env.throttledKey!);
    const down = [{ type: "predicate", name: "flag", instructions: "DOWN check" }];
    const throttled = await create(b.oa, { model: "aghoo/router-default", input: "x", questions: down });
    expect(b.statuses).toEqual([429, 200]);
    expect(throttled.answers[0]?.type).toBe("predicate");
  });

  it("errors: the facade's envelope is parsed by the openai client", async () => {
    const { oa, statuses } = facade(env.key!);
    const err = await create(oa, { model: "router-default", input: "x", questions: [] }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(OpenAI.BadRequestError);
    expect(err).toMatchObject({ status: 400 });
    expect(statuses).toEqual([400]);
  });
});
