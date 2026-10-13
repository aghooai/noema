import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { dump, load } from "js-yaml";
import { REPO } from "./fixture-data.ts";

/** The compose stack's default tools/mockbedrock scenario (restored after a spec that loaded its own). */
const DEFAULT_SCENARIO = resolve(REPO, "deploy/compose/mockbedrock/default.yaml");
/** opus-deep's model (platform.example.yaml `intent_routes.coding`); the speculative default route is sol-fast. */
const OPUS = "us.anthropic.claude-opus-5-5";

async function post(mockUrl: string, yaml: string): Promise<void> {
  const r = await fetch(`${mockUrl}/_mock/scenario`, { method: "POST", headers: { "content-type": "application/yaml" }, body: yaml });
  if (!r.ok) throw new Error(`POST ${mockUrl}/_mock/scenario: ${r.status} ${await r.text()}`);
}

/**
 * An invisible restart on the stack (NOEMA_UI_E2E_MOCK_URL = the stack's mockbedrock): the default scenario with three
 * rules in front for one unique marker (so the L2 decision cache and other tests are not involved). The router answers
 * intent `coding` (binding 0 routes opus-deep, so the speculative sol-fast attempt is cancelled with `spec_changed`),
 * and the restarted opus-deep attempt's first text (its commit) comes after 2.5 s, which keeps "Refining…" visible.
 * `restore` loads the default scenario again.
 */
export async function restartScenario(mockUrl: string): Promise<{ prompt: string; restore: () => Promise<void> }> {
  const marker = `ui-refine-${Date.now().toString(36)}`;
  const base = readFileSync(DEFAULT_SCENARIO, "utf8");
  const scenario = load(base) as { scenario: string; rules: unknown[] };
  const answer = {
    // Values outside a question's options get no mass (tests/fakes/scenario.py letter_dist), so one dist answers the
    // intent and complexity questions and every predicate.
    dist: { coding: 0.9, factual_qa: 0.05, chitchat: 0.05, trivial: 0.1, moderate: 0.8, hard: 0.1, true: 0.05, false: 0.95 },
  };
  scenario.scenario = "ui-e2e-restart";
  scenario.rules = [
    {
      match: { op: "converse-stream", model: OPUS, prompt_contains: marker },
      stream: { first_event_ms: 60, first_text_ms: 2500, chunks: ["Refined ", "answer."], chunk_tokens: 4, tokens_per_s: 200 },
    },
    { match: { op: "converse", prompt_contains: marker }, answer, latency_ms: 60 },
    { match: { op: "chat", prompt_contains: marker }, answer, latency_ms: 60 },
    ...scenario.rules,
  ];
  await post(mockUrl, dump(scenario));
  return { prompt: `Write a sorting function (${marker}).`, restore: () => post(mockUrl, base) };
}
