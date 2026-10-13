/**
 * Wire types for Noema (proposal v0.2).
 *
 * Question/answer shapes follow the OpenAI Decisions API (predicate | choice | score; answers carry `type` + `name`;
 * refusals are a distinct answer type). JEV-style additions: JSON state input, predicate criteria, nullable
 * descriptions, decision escalation. Ensemble, calibration, provenance, routing and conditioning are Noema-specific.
 * Every model call behind these types runs on Amazon Bedrock (D1). See ../03-contracts.md for semantics and invariants.
 * The OpenAI-compatible facade has its own types in ./openai-compat.ts.
 */

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };

/**
 * Response enums are open: the server may add values without a version bump. Known values keep autocomplete; any
 * other string is preserved verbatim. Branch on the known values and treat the rest conservatively (an unknown
 * calibration status is `uncalibrated`; an unknown answer type is `UnknownAnswer`, i.e. unavailable).
 */
export type OpenEnum<T extends string> = T | (string & {});

// ---------------------------------------------------------------------------------------------
// Questions (request side). `predicate` = JEV "noul"/Boolean; `choice` = JEV "Choice"; `score` = JEV "Score".
// ---------------------------------------------------------------------------------------------

/** "predicate" (OpenAI Decisions naming) and "noul" (Jev naming) are synonyms; answers echo the requested name. */
export type BooleanType = "predicate" | "noul";

export interface PredicateQuestion<N extends string = string, T extends BooleanType = BooleanType> {
  type: T;
  name: N;
  instructions: string;
  /** JEV-style descriptions of what true / false mean (folded into the member prompt). */
  criteria?: { true?: string; false?: string };
}

export interface ChoiceOption<V extends string = string> {
  value: V;
  description?: string | null;
  /**
   * Optional group label for the hierarchical method (every option carries one, or none does). Used when the option count
   * exceeds a member's template max_choices; without it the server splits the declared order into contiguous groups.
   */
  group?: string;
}

export interface ChoiceQuestion<N extends string = string, V extends string = string> {
  type: "choice";
  name: N;
  instructions: string;
  /**
   * 2–255 distinct values; include a fallback such as "other" (Decisions API guidance). More options than a member's template
   * max_choices (16 for logprob codes, 26 for verbalized letters) are answered hierarchically: stage 1 over groups, stage 2
   * inside the top groups, with the unexpanded mass reported as `unresolved_mass`.
   */
  choices: readonly ChoiceOption<V>[];
}

export interface ScoreLevel<L extends string = string> {
  label: L;
  description?: string | null;
}

export interface ScoreQuestion<N extends string = string, L extends string = string> {
  type: "score";
  name: N;
  instructions: string;
  /** 2–16 levels, ordered lowest to highest; level index = array position (0-based). */
  levels: readonly ScoreLevel<L>[];
}

export type Question = PredicateQuestion | ChoiceQuestion | ScoreQuestion;

// ---------------------------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------------------------

export type InputPart =
  | { type: "input_text"; text: string }
  /** Inline images become ephemeral files and go through the same sandbox path as uploads. */
  | { type: "input_image"; image_url: string; detail?: "low" | "high" | "auto" | "original" }
  | { type: "input_image"; file_id: string }
  | { type: "input_file"; file_id: string }
  /** JEV-style structured state; serialized as canonical JSON for members. */
  | { type: "input_json"; json: JsonValue };

export interface UserMessage {
  role: "user";
  content: InputPart[];
}

/** string = plain text; array = user messages; object = JSON state (JEV-style). */
export type DecisionInput = string | UserMessage[] | { [k: string]: JsonValue };

export interface DecisionCreateParams<Qs extends readonly Question[] = readonly Question[]> {
  /**
   * Decision profile id, optionally pinned to a version: "router-default" or "router-default@1". Default: the tenant's
   * `defaults.decision_profile` (`lightweight-model-decide` unless the tenant configures another, D6).
   */
  profile?: string;
  input: DecisionInput;
  /** 1-32 questions. Names are unique, match ^[a-z][a-z0-9_]{0,63}$, and q<digits> is reserved for the facade's generated names. */
  questions: Qs;
  /** Overrides the profile deadline (clamped to the tenant maximum). */
  deadline_ms?: number;
  /** Minimum members that must answer each question; default from the profile. */
  min_quorum?: number;
  /** Escalate weak answers to the profile's escalation target (default: per profile config). */
  escalation?: "profile_default" | "off";
  /** Optional session for authorized history context (summary only by default). */
  session_id?: string;
  include?: ("members" | "member_distributions" | "stages")[];
  /** "native" (default): DecisionResult. "jev": Jev-shaped JevDecisionResponse (answers keyed by name, 2-decimal values). */
  output_format?: "native" | "jev";
  /** Opaque end-user identifier for abuse tracking (OpenAI-compatible; stored, never sent to models). */
  safety_identifier?: string;
}

// ---------------------------------------------------------------------------------------------
// Probability semantics
// ---------------------------------------------------------------------------------------------

/**
 * - calibrated:        a calibrator was fitted for exactly this binding (profile version of the stage that produced the
 *                      answer, question signature, input-view signature, member subset, choice method) and passed holdout
 *                      acceptance; `binding` and `holdout` are present. Escalated answers and `lightweight-model-decide` answers are
 *                      `uncalibrated` until a calibrator fitted for their own stage passes.
 * - provider_reported: probabilities produced by a native decision API and passed through. Reserved: no native
 *                      decision API is available on Bedrock today (the Mantle Decisions route rejects Luna).
 * - uncalibrated:      pooled member probabilities (token logprobs, verbalized distributions or sample frequencies)
 *                      without an accepted calibrator. Ranking is usable; absolute values are not.
 */
export type CalibrationStatus = "calibrated" | "provider_reported" | "uncalibrated";

export interface HoldoutMetrics {
  dataset_id: string;
  /**
   * D4: labels are kept only when every labeler model agreed (gpt-6.1-sol + claude-sonnet-5-5 on Bedrock).
   * Calibration is therefore relative to consensus labels, on the subset where the labelers agreed.
   */
  label_source: "llm_consensus";
  labelers: string[]; // >= 2 distinct model ids
  consensus_rate: number; // overall share of sampled items kept
  /** Every class >= 0.6 for acceptance (and therefore > 0). */
  consensus_rate_by_class: Record<string, number>;
  n: number;
  /** Holdout labels per class; sums to n. */
  n_by_class: Record<string, number>;
  ece: number; // expected calibration error (adaptive bins); acceptance <= 0.05
  brier: number;
  nll: number;
  /** NLL of the same holdout before calibration; acceptance: nll <= nll_uncalibrated. */
  nll_uncalibrated: number;
  /** Classes with fewer than 30 holdout labels: classwise calibration is not evidenced. */
  flagged_classes: string[];
  /** Which population the evidence covers: agreement with consensus on the retained share of uniform traffic. */
  scope: { target: "llm_consensus_agreement"; retained_share: number; slices: { name: string; n: number }[] };
  /** Labeler-family check: largest member-labeler agreement lift over a random-label baseline; <= threshold to calibrate. */
  family_overlap: { max_member_lift: number; threshold: number };
  evaluated_at: string; // ISO 8601
}

/** What a calibrator was fitted for; must match the answering stage, question, views, member subset and choice method. */
export interface CalibrationBinding {
  profile_id: string;
  profile_version: string;
  /** sha256 of this question's canonical definition (not the whole set). */
  question_signature: string;
  input_view_signature: string;
  choice_method: "flat" | "hierarchical";
}

export interface Calibration {
  status: OpenEnum<CalibrationStatus>;
  /** What the pooled distribution was built from. */
  source?: "logprob" | "verbalized" | "mixed";
  method?: "temperature" | "platt" | "isotonic" | "dirichlet";
  calibrator_id?: string;
  /** Members whose answers were pooled; the calibrator is bound to this exact subset. */
  member_subset?: string[];
  binding?: CalibrationBinding;
  holdout?: HoldoutMetrics;
}

/**
 * Heuristic confidence. Never a probability; do not threshold it as if it were one.
 * Formulas follow TypeSafe's published definitions (TS-C):
 *   normalized_max     (choice, predicate): (p_max - 1/n) / (1 - 1/n); with unresolved_mass > 0, n counts the unresolved
 *                                           bucket as one extra category and p_max is the top candidate
 *   ordinal_dispersion (score):             max(0, 1 - sum_i p_i*|i - m| / MAD(uniform, m_u)),
 *                                           m = most likely level (ties -> lower index), m_u = uniform's centre
 * They reproduce the two OpenAI guide examples (0.93, 0.55); OpenAI's own formula is undocumented.
 */
export interface HeuristicConfidence {
  kind: "heuristic";
  value: number; // [0,1]
  method: "normalized_max" | "ordinal_dispersion" | "agreement" | "provider";
  band: "high" | "medium" | "low"; // from routing.confidence_bands
  margin: number; // p(top1) - p(top2)
  /**
   * H/ln(n) over the candidates, plus one unresolved bucket when unresolved_mass > 0 (n as for normalized_max); H(p)/ln(K)
   * when there is none. 0 = certain, 1 = uniform.
   */
  normalized_entropy: number;
  agreement: number; // fraction of answering members whose argmax equals candidates[0]
  member_divergence: number | null; // mean base-2 Jensen–Shannon divergence member → linear pool (raw_score); null if 1 member
}

export interface Candidate<V> {
  value: V;
  /** Ensemble probability after aggregation (and calibration when status = calibrated). */
  probability: number;
  /** Pooled pre-calibration score (linear-pool mass or log-pool logit); comparable within one answer only. */
  raw_score: number;
  /** Number of answering members whose argmax is this value. */
  votes: number;
}

// ---------------------------------------------------------------------------------------------
// Members and provenance
// ---------------------------------------------------------------------------------------------

export type Elicitation =
  | "token_logprobs" // single-letter option codes, top_logprobs renormalized over options (DeepSeek via Mantle)
  | "verbalized" // model states a distribution as JSON (Haiku, Luna on Bedrock: no logprobs, no temperature)
  | "sampling" // k samples, smoothed frequencies (only for models whose outputs vary)
  | "native_decision"; // reserved: a decision API returns probabilities (none on Bedrock today)

/** What this member actually saw for the turn's media (multimodal ensembles). */
export type InputView = "native" | "thumbnail" | "ocr_text" | "derived_text" | "keyframes_transcript" | "metadata_only" | "skipped";

export interface MemberVote {
  member_id: string;
  provider: "bedrock-converse" | "bedrock-mantle-chat";
  model: string; // Bedrock model / inference-profile id, e.g. "us.anthropic.claude-haiku-5-5"
  /** Provider-reported revision when the API returns one (none of the retained probes did); profile versions pin ids, not revisions. */
  model_revision?: string;
  region?: string;
  elicitation: Elicitation;
  input_view: InputView;
  status: OpenEnum<"ok" | "refused" | "timeout" | "error" | "late" | "skipped">;
  /** Required when status = skipped: the member cannot see the media, or cannot carry the question's option count. */
  skip_reason?: "input_view" | "too_many_choices";
  /**
   * Raw per-option distribution from this member (pre-calibration). Present when status = ok. Sums to 1 - unresolved_mass.
   * Options in `censored_options` (and options of unexpanded groups) have no entry: no floor is invented.
   */
  distribution?: Record<string, number>;
  /** Declared options this member could not measure (absent from the returned top logprobs). */
  censored_options?: string[];
  /** This member's mass not attributed to any option (unlisted logprob tail, unexpanded groups). Default 0. */
  unresolved_mass?: number;
  /** After the per-member calibrator (when one is active). */
  calibrated_distribution?: Record<string, number>;
  calibrator_id?: string;
  weight: number;
  latency_ms: number;
  samples?: number; // elicitation = sampling
  usage?: Usage;
  error?: { code: string; message: string };
}

/** Disjoint categories: cost = uncached input + cache reads + cache writes + output (reasoning is billed as output). */
export interface Usage {
  /** Total input including cache reads and writes. */
  input_tokens: number;
  /** Includes reasoning_tokens. */
  output_tokens: number;
  /** Part of input_tokens served from the provider prompt cache. */
  cached_input_tokens: number;
  /** Part of input_tokens written to the provider prompt cache. */
  cache_write_input_tokens: number;
  /** Part of output_tokens spent on reasoning. */
  reasoning_tokens: number;
  cost_usd: number;
  /** True when cost comes from the price catalog rather than AWS billing data. */
  cost_estimated: boolean;
  /** True when the provider returned no usage (a cancelled call) and the token counts are Noema's estimate; not zero usage. */
  tokens_estimated: boolean;
}

export interface QuorumInfo {
  required: number;
  responded: number;
  refused: number;
  total: number;
}

export interface EscalationStage {
  profile_id: string;
  profile_version: string;
  triggered_by: ("confidence_below" | "margin_below" | "refused" | "unavailable")[];
  questions: string[];
  usage: Usage;
  /** sha256 over this stage's per-member input views; calibrator bindings of answers from this stage use it. */
  input_view_signature?: string;
  /** member_id -> template version for this stage's members (member ids are positional within the stage). */
  prompt_templates?: Record<string, string>;
}

export interface Provenance {
  trace_id: string;
  profile_id: string;
  profile_version: string;
  /** sha256 of the canonical question set (names, types, options, criteria, instructions). */
  question_signature: string;
  /** HMAC-SHA256 of canonical input under a per-tenant key (no raw content). */
  input_digest: string;
  /** sha256 over the per-member input views (e.g. "m1:ocr_text,m2:native,m3:native"). */
  input_view_signature: string;
  /** Seed of the per-member option-order permutation (part of the calibrated pipeline). */
  option_order_seed: number;
  prompt_templates: Record<string, string>; // member_id -> template version
  aggregation: { method: "linear_pool" | "log_pool" | "stacked"; weights_version?: string };
  deadline_ms: number;
  quorum: QuorumInfo; // decision-level summary (minimum across answers)
  cache: { hit: boolean };
  /** Escalation stages after the first (empty when no escalation fired). */
  stages: EscalationStage[];
  started_at: string;
  completed_at: string;
}

// ---------------------------------------------------------------------------------------------
// Answers
// ---------------------------------------------------------------------------------------------

interface ScoredAnswerBase<N extends string> {
  /** Request name, or q<digits> for unnamed facade questions (stored facade decisions). */
  name: N;
  calibration: Calibration;
  confidence: HeuristicConfidence;
  quorum: QuorumInfo;
  members?: MemberVote[];
  /** Pooled mass not attributed to any candidate; candidates sum to 1 - unresolved_mass. Never spread over options. Default 0. */
  unresolved_mass?: number;
}

/** Stage-1 group of a hierarchical choice answer. Options of unexpanded groups are not emitted as candidates. */
export interface AnswerGroup {
  id: string; // "g1", "g2", …
  label: string; // request-supplied group name, or "options a-b" (1-based declared positions) for a server split
  options: string[];
  probability: number; // pooled stage-1 mass; the group's candidates sum to it
  expanded: boolean; // the top groups covering >= 0.9 of the stage-1 mass and holding >= 2 options (at most 3) are expanded
}

export interface PredicateAnswer<N extends string = string, T extends BooleanType = BooleanType> extends ScoredAnswerBase<N> {
  type: T;
  /** P(statement is true). */
  probability: number;
  candidates: Candidate<boolean>[];
}

export interface ChoiceAnswer<N extends string = string, V extends string = string> extends ScoredAnswerBase<N> {
  type: "choice";
  /** Argmax of candidates (ties -> declared order); always a declared value. Thresholding is the caller's policy. */
  choice: V;
  /**
   * Sorted by probability descending (ties -> declared order). Flat answers cover every declared value; hierarchical answers
   * cover the options of the expanded groups. Candidates sum to 1 - unresolved_mass.
   */
  candidates: Candidate<V>[];
  /** flat (default) or hierarchical (08 §3.4). Calibrators bind to the method. */
  choice_method?: "flat" | "hierarchical";
  /** Present exactly when choice_method = hierarchical. */
  groups?: AnswerGroup[];
}

export interface ScoreAnswer<N extends string = string, L extends string = string> extends ScoredAnswerBase<N> {
  type: "score";
  /** Probability-weighted mean level index (Decisions API semantics); may fall between levels. */
  score: number;
  /** Label of the most probable level. */
  level: L;
  candidates: (Candidate<L> & { index: number })[];
}

export interface RefusalAnswer<N extends string = string> {
  type: "refusal";
  name: N;
  quorum?: QuorumInfo;
  members?: MemberVote[];
}

export interface UnavailableAnswer<N extends string = string> {
  type: "unavailable";
  name: N;
  reason: OpenEnum<"deadline_exceeded" | "no_quorum" | "all_members_failed" | "no_capable_member">;
  quorum?: QuorumInfo;
  members?: MemberVote[];
}

/**
 * SDK stand-in for an answer `type` this client version doesn't know (a later additive change). The wire type is kept
 * in `raw_type` and the payload in `raw`. Treat it like `unavailable`; never read `raw` values as a decision.
 */
export interface UnknownAnswer<N extends string = string> {
  type: "unknown";
  name: N | null;
  raw_type: string;
  raw: { [k: string]: JsonValue };
}

export type Answer = PredicateAnswer | ChoiceAnswer | ScoreAnswer | RefusalAnswer | UnavailableAnswer | UnknownAnswer;

export interface DecisionResult {
  id: string; // "dec_..."
  object: "decision";
  created_at: number; // unix seconds
  /**
   * completed: every answer is final and none is `unavailable`. Includes "the deadline passed with quorum met"; members
   *            that missed the deadline are `timeout`.
   * quorum:    the in-turn snapshot sent when every commit-critical answer is final but a non-blocking answer is still open
   *            (provisional, from the members that answered so far; pending members `late`). Exactly one `decision.updated`
   *            follows with the final result (`completed` or `partial`). `/v1/decisions`, the facade and stored rows never
   *            carry `quorum`. This is not early commit.
   * partial:   every answer is final and at least one is `unavailable`, for any reason.
   */
  status: OpenEnum<"completed" | "quorum" | "partial">;
  answers: Answer[];
  provenance: Provenance;
  usage: Usage;
}

// ---------------------------------------------------------------------------------------------
// Type-level mapping from a question tuple to answers keyed by name (SDK convenience).
// ---------------------------------------------------------------------------------------------

export type AnswerFor<Q> =
  Q extends PredicateQuestion<infer N, infer T> ? PredicateAnswer<N, T>
  : Q extends ChoiceQuestion<infer N, infer V> ? ChoiceAnswer<N, V>
  : Q extends ScoreQuestion<infer N, infer L> ? ScoreAnswer<N, L>
  : never;

export type AnswersByName<Qs extends readonly Question[]> = {
  [Q in Qs[number] as Q["name"]]: AnswerFor<Q> | RefusalAnswer<Q["name"]> | UnavailableAnswer<Q["name"]> | UnknownAnswer<Q["name"]>;
};

export interface TypedDecisionResult<Qs extends readonly Question[]> extends DecisionResult {
  byName: AnswersByName<Qs>;
}

// ---------------------------------------------------------------------------------------------
// Jev-shaped output (output_format: "jev"). Mirrors TypeSafe Jev's answer shapes [TS-API]: answers keyed by
// question name; noul = P(true); choice/score probabilities as maps; score legend keyed "0".."n-1"; values rounded
// to 2 decimals (sums may be 0.99-1.01). Produced by our Bedrock profiles (e.g. lightweight-model-decide = gpt-6-luna); Jev is
// not called. refusal / unavailable are extensions (Jev documents no refusal type).
// ---------------------------------------------------------------------------------------------

export type JevAnswer =
  | { type: "noul"; noul: number }
  | { type: "choice"; choice: string; probabilities: Record<string, number>; confidence: number }
  | { type: "score"; score: number; legend: Record<string, string>; probabilities: Record<string, number>; confidence: number }
  | { type: "refusal" }
  | { type: "unavailable"; reason: UnavailableAnswer["reason"] };

export type JevAnswerFor<Q> =
  Q extends PredicateQuestion ? { type: "noul"; noul: number }
  : Q extends ChoiceQuestion<string, infer V> ? { type: "choice"; choice: V; probabilities: Record<V, number>; confidence: number }
  : Q extends ScoreQuestion ? { type: "score"; score: number; legend: Record<string, string>; probabilities: Record<string, number>; confidence: number }
  : never;

export interface JevDecisionResponse<Qs extends readonly Question[] = readonly Question[]> {
  /** Resolved, version-pinned profile, e.g. "aghoo/lightweight-model-decide@1" (cf. Jev's versioned "jev-1.13.0"). */
  model: string;
  answers: { [Q in Qs[number] as Q["name"]]: JevAnswerFor<Q> | { type: "refusal" } | { type: "unavailable"; reason: UnavailableAnswer["reason"] } };
  usage: { input_tokens: number; output_tokens: number };
  /** Full native result is retrievable at GET /v1/decisions/{decision_id}. */
  decision_id: string;
}

// ---------------------------------------------------------------------------------------------
// Routing, conditioning, generation profiles, turn streaming
// ---------------------------------------------------------------------------------------------

export type Strategy = "gate" | "holdback" | "speculative" | "hedge";
export type GenerationMode = "single" | "fallback" | "race" | "best_of_n" | "fuse";
export type Effort = "none" | "low" | "medium" | "high" | "xhigh" | "max";
export type AnswerMode = "direct" | "cover_top2_and_clarify" | "clarify_first";

/** Noema-authored decision-context block appended to the generation prompt (template decision-conditioning@1). */
export interface Conditioning {
  answer_mode: AnswerMode;
  /** sha256 over the template id and the rendered block; part of the attempt spec compared at commit. */
  signature: string;
  /** Human-readable summary shown in the decision inspector, e.g. "intent billing≈40% vs shipping≈40%, low confidence". */
  summary: string;
}

export interface RoutingDecision {
  route_id: string; // generation profile id actually used
  generation_profile: string;
  mode: GenerationMode;
  /** How the route was chosen. */
  selection: "rule" | "expected_utility" | "pinned" | "sticky" | "default" | "fallback";
  strategy: Strategy;
  decision_id: string | null;
  policy_version: string;
  /** Bindings (by index/id) whose `when` matched, in evaluation order. */
  matched_bindings: string[];
  /** Answers that determined the route and parameters. */
  basis: {
    name: string;
    value: string | boolean | number;
    probability: number;
    margin: number;
    band: "high" | "medium" | "low";
    status: CalibrationStatus;
  }[];
  /** Parameters applied to the attempt(s); only values the model accepts (see model controls). */
  params: { effort?: Effort; max_output_tokens?: number };
  conditioning: Conditioning | null;
  /**
   * true when the served spec differs from the decided one: the answer committed on spec0 at the holdback deadline, or a
   * pre-commit fallback to `routing.default_profile` served the turn; false when they are equal; null before a decision arrives.
   */
  served_spec_differs: boolean | null;
  /** Human-readable explanation surfaced in the decision inspector. */
  reason: string;
  fallbacks: string[]; // ordered generation-profile ids
}

export type Modality = "text" | "image" | "audio" | "video" | "document";

export interface Degradation {
  /** Either the uploaded file or the inline part (index into the turn input) that was degraded. */
  file_id?: string;
  part_index?: number;
  attempt_id?: string; // route-specific degradations carry the attempt
  model?: string;
  from: string; // e.g. "application/pdf"
  to: string; // e.g. "markdown+page_images(4)"
  reason:
    | "model_capability"
    | "size_limit"
    | "media_budget"
    | "policy"
    | "param_unsupported"
    | "active_content_removed"
    | "not_ready"
    | "extraction_failed"
    | "rejected";
}

// ---------------------------------------------------------------------------------------------
// Errors. Each code has one HTTP status, `error.type` and `retryable` value (03 §1).
// ---------------------------------------------------------------------------------------------

export type ErrorCode =
  | "schema_violation" | "unsupported_question_type" | "reserved_question_name" | "unsupported_parameter" | "model_not_available"
  | "profile_not_allowed" | "invalid_api_key" | "session_forbidden" | "session_not_found" | "idempotency_key_reused"
  | "file_not_ready" | "file_rejected" | "file_too_large" | "image_too_large" | "unsupported_media_type"
  | "no_route_satisfies_constraints" | "no_capable_member" | "rpm_exceeded" | "tpm_exceeded" | "budget_exhausted" | "queue_timeout"
  | "all_routes_unavailable" | "deadline_exceeded" | "tenant_access_misconfigured" | "upstream_stalled" | "client_too_slow"
  | "resume_window_expired" | "session_conflict" | "session_busy" | "not_found" | "permission_denied" | "invalid_end_user_token"
  | "request_too_large" | "feature_not_available" | "internal_error" | "not_implemented";

export type ErrorType =
  | "invalid_request_error" | "authentication_error" | "permission_error" | "not_found_error" | "conflict_error"
  | "capability_error" | "rate_limit_error" | "upstream_error" | "server_error";

/** HTTP error body (OpenAI-shaped). Codes are an open enum: a later server may add codes without a version bump. */
export interface ErrorEnvelope {
  error: {
    type: OpenEnum<ErrorType>;
    code: OpenEnum<ErrorCode>;
    message: string;
    /** Offending field path (e.g. "questions[1].choices") or header name; null when no single field is at fault. */
    param: string | null;
    request_id: string;
    retryable: boolean;
    details?: { [k: string]: JsonValue };
  };
}

export type TurnEvent =
  | { type: "turn.created"; turn_id: string; session_id: string | null; created_at: number }
  | { type: "preflight.completed"; modalities: Modality[]; est_input_tokens: number; degradations: Degradation[] }
  | { type: "route.selected"; attempt_id: string; routing: RoutingDecision; speculative: boolean; degradations: Degradation[] }
  | { type: "decision.completed"; decision: DecisionResult; arrived: "before_generation" | "during_holdback" | "after_commit" | "after_completion" }
  | { type: "decision.updated"; decision: DecisionResult; changed: string[] } // late members merged (informational)
  | { type: "route.confirmed"; attempt_id: string; routing: RoutingDecision }
  | { type: "generation.started"; attempt_id: string; route_id: string; model: string; speculative: boolean; degradations: Degradation[] }
  | { type: "generation.delta"; attempt_id: string; seq: number; text: string }
  | { type: "generation.committed"; attempt_id: string; reason: "spec_agrees" | "holdback_deadline" | "gate" | "speculative" | "selected" }
  | { type: "generation.cancelled"; attempt_id: string; reason: "spec_changed" | "lost_race" | "not_selected" | "client_abort" | "failed" }
  | { type: "generation.rerouted"; from_attempt_id: string; to_attempt_id: string; reason: string; discard_from_seq: number | null }
  | { type: "generation.completed"; attempt_id: string; finish_reason: "stop" | "length" | "content_filter" | "cancelled"; usage: Usage }
  | { type: "turn.verified"; verification: DecisionResult } // post-generation answer checks by the decision ensemble
  /** Always the last event: late decision members, verification and usage have settled before it. */
  | { type: "turn.completed"; turn_id: string; usage: Usage; timings: TurnTimings }
  | { type: "error"; code: OpenEnum<ErrorCode>; message: string; retryable: boolean };

export interface TurnTimings {
  ttft_ms: number | null; // first committed delta delivered to the client
  decision_ms: number | null;
  decision_lead_ms: number | null; // first answer text of r0 minus decision ready; > 0 means holdback was free
  holdback_ms: number | null; // time buffered output waited for the decision
  e2e_ms: number;
  spec_restarts: number; // invisible restarts because the decided attempt spec differed
  wasted_output_tokens: number; // tokens from cancelled attempts (estimated)
}
