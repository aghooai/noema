/**
 * Compile-time contract tests: `tsc --noEmit` must pass, and every @ts-expect-error must be needed.
 */
import { AghooClient, AghooError, q } from "./sdk";
import type { OADecisionCreateParams } from "./openai-compat";

declare const client: AghooClient;

const questions = [
  q.choice("department", {
    instructions: "Which team should handle this message?",
    choices: [
      { value: "billing", description: "Payments, refunds, invoices" },
      { value: "technical", description: "Bugs and errors" },
      { value: "other", description: null },
    ],
  }),
  q.predicate("needs_human", {
    instructions: "The customer explicitly asks for a human.",
    criteria: { true: "Asks for a person, agent or call back", false: "No such request" },
  }),
  q.score("urgency", {
    instructions: "How urgent is the request?",
    levels: [
      { label: "low", description: "Can wait days" },
      { label: "medium" },
      { label: "high", description: "Blocking now" },
    ],
  }),
] as const;

export async function routeExample(): Promise<string> {
  const d = await client.decisions.create({ profile: "router-default@1", input: "I was charged twice.", questions });

  const dept = d.byName.department;
  if (dept.type !== "choice") return "fallback"; // refusal, unavailable or an answer type this SDK doesn't know

  // Narrowed to ChoiceAnswer<"department", "billing" | "technical" | "other">.
  const value: "billing" | "technical" | "other" = dept.choice;
  // @ts-expect-error "shipping" is not a declared choice
  const bad: typeof dept.choice = "shipping";

  // Calibrated thresholds only make sense when status === "calibrated".
  const top = dept.candidates[0];
  const trusted = dept.calibration.status === "calibrated" && top !== undefined && top.probability >= 0.8;
  const lowConfidence = dept.confidence.band === "low"; // heuristic band, not a probability

  const urgency = d.byName.urgency;
  if (urgency.type === "score") {
    const level: "low" | "medium" | "high" = urgency.level;
    void level;
  }

  const human = d.byName.needs_human;
  if (human.type === "predicate" && human.probability > 0.5) return "human";

  // @ts-expect-error no question named "sentiment"
  void d.byName.sentiment;

  void bad;
  void lowConfidence;
  return trusted ? value : "generalist";
}

export async function jevExample(): Promise<string> {
  // Jev-like decision from gpt-6-luna (single-model profile) behind the Decisions-style API.
  const r = await client.decisions.create({
    profile: "lightweight-model-decide@1",
    output_format: "jev",
    input: "My package says delivered but I was also charged twice.",
    questions: [
      q.choice("department", { instructions: "Which team?", choices: [{ value: "billing" }, { value: "shipping" }, { value: "other" }] }),
      q.score("urgency", { instructions: "How urgent?", levels: [{ label: "low" }, { label: "medium" }, { label: "high" }] }),
      q.noul("needs_human", { instructions: "The customer explicitly asks for a human." }),
    ] as const,
  });
  const dept = r.answers.department;
  const human = r.answers.needs_human;
  if (human.type === "noul" && human.noul > 0.5) return "human";
  if (dept.type === "choice") {
    const v: "billing" | "shipping" | "other" = dept.choice;
    const p: number = dept.probabilities.shipping;
    // @ts-expect-error "technical" is not a declared choice
    void dept.probabilities.technical;
    void p;
    return v;
  }
  const u = r.answers.urgency;
  if (u.type === "score") void u.legend["0"];
  return "unknown";
}

export async function nativeNoulExample(): Promise<void> {
  const d = await client.decisions.create({
    profile: "router-default",
    input: "Please call me back.",
    questions: [q.noul("needs_human", { instructions: "Asks for a human." })] as const,
  });
  const a = d.byName.needs_human;
  if (a.type === "noul") void a.probability; // native answers echo the requested type name
}

export async function defaultProfileExample(): Promise<void> {
  // No profile: the tenant's defaults.decision_profile answers (lightweight-model-decide, D6).
  const d = await client.decisions.create({ input: "Where is my parcel?", questions });
  void d.provenance.profile_id;
}

export function choiceBuilderExample(): void {
  // Integer-like values keep their declared order, and options can carry hierarchical-method groups.
  const sizes = q.choice("size", {
    instructions: "Which size?",
    choices: [{ value: "10", group: "small" }, { value: "2", group: "small" }, { value: "xl", group: "large" }, { value: "xxl", group: "large" }],
  });
  const v: "10" | "2" | "xl" | "xxl" = sizes.choices[0]!.value;
  void v;
  // @ts-expect-error the Record form was removed: object keys can't keep integer-like order or carry groups
  q.choice("bad", { instructions: "Pick one", choices: { a: null, b: null } });
}

export async function resourcesExample(): Promise<void> {
  const profiles = await client.profiles.list();
  const version: string = profiles[0]!.version; // "1", same form as provenance.profile_version
  // @ts-expect-error profile versions are strings
  const n: number = profiles[0]!.version;
  void version;
  void n;
  for await (const s of client.sessions.list({ limit: 20 })) void s.last_seq;
  await client.sessions.update("ses_123", { metadata: { app: "support" }, data_class: "confidential" });
  for await (const t of client.sessions.turns("ses_123")) void t.redactions;
  const page = await client.files.derivative("file_1", "page_images", { page: 1 });
  void page.type;
  for await (const row of client.usage.list({ group_by: "model" })) void row.usage.cost_usd;
  for await (const c of client.admin.calibrators.list({ status: "active" })) void c.holdout?.ece;
  for await (const r of client.admin.labeling.runs()) void r.consensus_rate_by_class;
  for await (const item of client.admin.labeling.dropped({ reason: "disagreed" })) void item.labeler_answers;
}

export async function errorExample(): Promise<string> {
  try {
    await client.turns.create({ input: [{ role: "user", content: [{ type: "input_text", text: "Hi" }] }], strategy: "hedge" });
    return "ok";
  } catch (e) {
    if (e instanceof AghooError && e.code === "feature_not_available") return `not in this phase: ${e.param ?? ""}`;
    throw e;
  }
}

export async function jsonStateExample(): Promise<void> {
  // JEV-style structured state.
  await client.decisions.create({
    profile: "router-default",
    input: { order_id: "A-17", events: ["delivered", "charged", "charged"] },
    questions,
  });
}

export async function streamExample(): Promise<void> {
  const profiles = await client.profiles.list({ kind: "generation" });
  const quality = profiles.find((p) => p.mode === "best_of_n");
  const s = client.turns.stream({
    session_id: "ses_123",
    input: [{ role: "user", content: [{ type: "input_text", text: "Summarize the attached PDF" }, { type: "input_file", file_id: "file_1" }] }],
    generation: { profile: quality ? quality.id : "auto", effort: "medium" },
    conditioning: "auto",
    decisions: { blocking: ["department"] },
  });
  for await (const ev of s) {
    switch (ev.type) {
      case "generation.delta":
        process.stdout.write(ev.text);
        break;
      case "generation.rerouted":
        // UI clears text from discard_from_seq and shows the new attempt.
        break;
      case "route.confirmed":
        void ev.routing.conditioning?.summary;
        break;
      case "turn.verified":
        void ev.verification.answers;
        break;
      case "error":
        if (ev.code === "upstream_stalled") void ev.retryable; // keep the partial text; retry with retry_of
        break;
    }
  }
}

export const compatRequest: OADecisionCreateParams = {
  model: "aghoo/router-default",
  input: [{ role: "user", content: "Export fails in Safari but works in Chrome." }],
  questions: [
    { type: "score", instructions: "How severe is this?", levels: [{ label: "Cosmetic" }, { label: "Workaround available" }, { label: "Fully blocked" }] },
    { type: "choice", instructions: "Is it a regression?", choices: [{ value: true }, { value: false }] },
  ],
};

declare const process: { stdout: { write(s: string): void } };
