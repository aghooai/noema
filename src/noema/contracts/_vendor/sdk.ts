/**
 * TypeScript SDK surface (declarations only; proposal v0.3).
 * Builders infer literal names/values so answers are typed per question.
 * The client is hand-written against this surface; its wire types are generated from the bundled OpenAPI document
 * (openapi-typescript) and must stay assignable to the types here.
 */
import type {
  AnswerMode,
  CalibrationBinding,
  ChoiceOption,
  ChoiceQuestion,
  Degradation,
  DecisionCreateParams,
  DecisionResult,
  Effort,
  ErrorCode,
  ErrorType,
  HoldoutMetrics,
  JevDecisionResponse,
  JsonValue,
  GenerationMode,
  InputPart,
  OpenEnum,
  PredicateQuestion,
  Question,
  RoutingDecision,
  ScoreQuestion,
  Strategy,
  TurnEvent,
  TurnTimings,
  TypedDecisionResult,
  Usage,
} from "./types";

export declare const q: {
  predicate<const N extends string>(
    name: N,
    opts: { instructions: string; criteria?: { true?: string; false?: string } },
  ): PredicateQuestion<N, "predicate">;
  /** Jev naming for a boolean question; identical semantics to predicate. */
  noul<const N extends string>(
    name: N,
    opts: { instructions: string; criteria?: { true?: string; false?: string } },
  ): PredicateQuestion<N, "noul">;
  /**
   * Options in declared order: the order members see, ties break by, and the facade echoes. An array (not a Record), so
   * integer-like values keep their order and each option can carry a `group` for the hierarchical method.
   */
  choice<const N extends string, const C extends readonly ChoiceOption[]>(
    name: N,
    opts: { instructions: string; choices: C },
  ): ChoiceQuestion<N, C[number]["value"]>;
  score<const N extends string, const L extends readonly { label: string; description?: string | null }[]>(
    name: N,
    opts: { instructions: string; levels: L },
  ): ScoreQuestion<N, L[number]["label"]>;
};

/** Thrown for every non-2xx HTTP response (after retries). SSE `error` events are yielded by TurnStream, not thrown. */
export declare class AghooError extends Error {
  readonly status: number;
  readonly type: OpenEnum<ErrorType>;
  readonly code: OpenEnum<ErrorCode>;
  readonly param: string | null;
  readonly requestId: string | null;
  readonly retryable: boolean;
  /** From `retry-after`, in seconds. */
  readonly retryAfterS: number | null;
  readonly details: { [k: string]: JsonValue } | undefined;
}

export interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  /** Retries apply only before any stream event is received. */
  maxRetries?: number;
  idempotencyKey?: string;
}

export interface TurnCreateParams {
  session_id?: string;
  /** Optimistic ordering: the `last_seq` / `seq` the client has seen. A different server value is 409 `session_conflict`. */
  expected_seq?: number;
  store?: boolean;
  stream?: boolean;
  wait_for_files?: boolean;
  allow_partial_files?: boolean;
  retry_of?: string;
  /** Developer instructions, appended to the platform preamble (never above it). */
  instructions?: string;
  input: { role: "user"; content: InputPart[] }[];
  decisions?: {
    profile?: string; // must be in the tenant's request_overrides allowlist
    questions?: readonly Question[]; // app questions, returned to the caller
    /** App question names the commit must wait for (router commit-critical questions are always blocking). */
    blocking?: string[];
    deadline_ms?: number;
    min_quorum?: number;
    include?: ("members" | "member_distributions" | "stages")[];
  };
  generation?: {
    /** "auto" = routing policy; or a generation profile id from the allowlist. */
    profile?: "auto" | string;
    effort?: Effort; // ignored (with a degradation) on models that don't accept it, unless the profile is pinned -> 422
    max_output_tokens?: number;
    /** Only GLM-5.3 accepts temperature among the default routes; see model controls. */
    temperature?: number;
  };
  strategy?: "auto" | Strategy;
  /** "auto" = bindings decide; "off" disables decision conditioning; or force an answer mode. */
  conditioning?: "auto" | "off" | AnswerMode;
  /** Run post-generation answer verification (default: per bindings). */
  verify?: boolean;
  history?: { mode: "auto" | "none" | "last_n"; max_tokens?: number; last_n?: number };
  metadata?: Record<string, string>;
}

export interface TurnObject {
  turn_id: string;
  session_id: string | null;
  seq: number | null; // position in the session; null when store is false
  text: string; // committed attempt only
  decision: DecisionResult | null;
  routing: RoutingDecision;
  attempts: { attempt_id: string; route_id: string; model: string; outcome: "committed" | "cancelled" | "failed" | "not_selected"; usage: Usage }[];
  verification: DecisionResult | null;
  degradations: Degradation[];
  usage: Usage;
  timings: TurnTimings;
}

export interface TurnStream extends AsyncIterable<TurnEvent> {
  /** Resolves with the final decision (including late `decision.updated` merges), or null. */
  decision(): Promise<DecisionResult | null>;
  /** Committed text only: discarded speculative attempts are excluded. */
  finalText(): Promise<string>;
  /** Explicit abort: calls POST /v1/turns/{id}/cancel (final). A dropped connection is not an abort; reattach with lastEventId. */
  abort(): void;
}

export type DataClass = "public" | "internal" | "confidential" | "restricted";

export interface Session {
  id: string;
  created_at: number;
  last_seq: number; // seq of the latest committed turn (0 = none)
  owner?: string;
  data_class: DataClass;
  retention: { policy: "default" | "ephemeral" | "custom"; expires_at: number | null };
  metadata: Record<string, string>;
}

/** PATCH semantics: present fields replace the stored value; `metadata` replaces the whole map. Phase 1: retention "custom" is 422. */
export interface SessionUpdate {
  metadata?: Record<string, string>;
  retention?: Session["retention"]["policy"];
  data_class?: DataClass;
}

/** One history message; a committed turn yields its user message, then its assistant message. */
export interface SessionTurn {
  turn_id: string;
  seq: number;
  role: "user" | "assistant";
  text: string; // redacted spans read "[redacted]"
  created_at: number;
  /** Offsets in Unicode code points of `text` (use Array.from(text) to slice); `end` is exclusive. */
  redactions: { start: number; end: number; reason: string }[];
}

export type DerivativeKind =
  | "text" | "markdown" | "page_images" | "pdf_sanitized" | "raster" | "thumbnail" | "ocr_text"
  | "transcript" | "keyframes" | "timeline" | "audio_normalized";

export interface FileObject {
  id: string;
  filename: string; // display only; never sent to models
  media_type: string; // sniffed, not client-declared
  bytes: number;
  sha256: string;
  status: "quarantined" | "processing" | "ready" | "partial" | "rejected" | "failed" | "deleted";
  derivatives: { kind: DerivativeKind; id: string; status: "pending" | "ready" | "failed"; pages?: number }[];
  rejection?: { code: FileRejectionCode; message: string };
  failure?: { code: "extraction_failed" | "ocr_failed" | "transcription_failed" | "timeout"; message: string; retryable: true };
}

export type FileRejectionCode =
  | "type_mismatch" | "not_allowlisted" | "file_too_large" | "too_many_pages" | "duration_exceeded" | "pixel_limit"
  | "encrypted_pdf" | "decompression_bomb" | "corrupt" | "unsupported_codec" | "empty_content" | "malware_detected";

export interface ProfileInfo {
  id: string; // e.g. "router-default"
  version: string; // "^[0-9]+$", same form as provenance.profile_version
  kind: "decision" | "generation";
  description: string;
  /** decision: member model ids; generation: member model ids in order. */
  models: string[];
  mode?: GenerationMode; // generation profiles
  modalities: ("text" | "image" | "document")[]; // native, union over members
  streaming?: "live" | "after_selection";
}

export type UsageLine = "decision" | "generation" | "labeling" | "verification" | "waste" | "cache_savings";
export type UsageGroupBy = "project" | "session" | "model" | "day" | "line";

/** One (key, line) row; key = project id, session id, Bedrock model id, UTC day or line name (null when it doesn't apply). */
export interface UsageRow {
  key: string | null;
  line: UsageLine;
  usage: Usage;
}

// --- Read-only W4 views (GET /v1/admin/...; roles tenant_admin, reviewer, auditor; dropped items: tenant_admin, reviewer) ---

/**
 * fitted: fitted, gate not yet evaluated. rejected: failed the acceptance gate. accepted: passed the gate, awaiting two-person
 * activation (approve by one operator, activate by another). active: applied to answers whose binding matches. downgraded:
 * drift detected (weekly ECE); never re-activated. retired: replaced, retired by hand, or its profile version left the active config.
 */
export type CalibratorStatus = "fitted" | "rejected" | "accepted" | "active" | "downgraded" | "retired";

export interface CalibratorInfo {
  id: string;
  object: "calibrator";
  /** platform: fitted on platform seed data; applies only to platform question signatures, until a tenant artifact for the same binding is active. */
  scope: "platform" | "tenant";
  question_name: string;
  status: CalibratorStatus;
  method: "temperature" | "platt"; // the pooled transform (Phase 1 methods)
  /** One artifact holds the per-member and pooled parts; they are only ever applied together (ADR-003 §1). */
  parameters: {
    pooled: Record<string, number>; // {"temperature": T} or {"a": a, "b": b}
    member_T: Record<string, number>; // member_id -> T_m (> 0)
    epsilon: number; // > 0
  };
  binding: CalibrationBinding;
  member_subset: string[];
  holdout: HoldoutMetrics | null;
  acceptance: {
    passed: boolean;
    failed_rules: (
      | "ece_max" | "nll_not_worse" | "n_min" | "flagged_classes" | "class_consensus_min" | "family_overlap_lift"
      | "label_source" | "member_subset_share"
    )[];
  };
  reliability: {
    bins: { lower: number; upper: number; n: number; mean_predicted: number | null; observed: number | null }[];
    ci: { ece?: number[]; brier?: number[]; nll?: number[] }; // each [low, high]
  };
  created_at: number;
  approved_by: string | null; // operator who approved activation (first of two)
  activated_by: string[]; // both operator ids (approve, activate); empty until active
  activated_at: number | null;
  downgraded_at: number | null;
  downgrade_reason: "drift_ece" | "drift_ci" | null;
  retired_at: number | null;
  retired_reason: "replaced" | "manual" | "profile_version_retired" | null;
}

export interface LabelingRun {
  id: string;
  object: "labeling_run";
  status: "running" | "completed" | "failed" | "cancelled";
  profile_id: string;
  profile_version: string;
  question_name: string;
  question_signature: string;
  labelers: string[];
  rule: "unanimous";
  counts: {
    sampled: number; sampled_uniform: number; sampled_uncertainty: number; kept: number;
    dropped_disagreed: number; dropped_refused: number; dropped_parse_error: number; dropped_labeler_error: number;
  };
  consensus_rate: number | null;
  consensus_rate_by_class: Record<string, number>;
  n_by_class: Record<string, number>;
  usage: Usage;
  started_at: number;
  completed_at: number | null;
}

export interface LabelingDroppedItem {
  id: string;
  object: "labeling_dropped_item";
  run_id: string;
  decision_id: string;
  question_name: string;
  question_signature: string;
  input_view_signature: string;
  reason: "disagreed" | "refused" | "parse_error" | "labeler_error";
  sample_reason: "uniform" | "uncertainty";
  labeler_answers: { model: string; status: "ok" | "refused" | "parse_error" | "error"; value: string | boolean | null }[];
  ensemble: { value: string | boolean | null; probability: number | null; band: "high" | "medium" | "low" | null; calibration_status: "calibrated" | "provider_reported" | "uncalibrated" };
  content: { excerpt: string | null; redacted: boolean };
  sampled_at: number;
}

type Page = { limit?: number };

export declare class AghooClient {
  constructor(opts: { apiKey?: string; baseURL?: string; fetch?: typeof fetch });

  decisions: {
    create<const Qs extends readonly Question[]>(
      params: DecisionCreateParams<Qs> & { output_format: "jev" },
      opts?: RequestOptions,
    ): Promise<JevDecisionResponse<Qs>>;
    create<const Qs extends readonly Question[]>(
      params: DecisionCreateParams<Qs> & { output_format?: "native" },
      opts?: RequestOptions,
    ): Promise<TypedDecisionResult<Qs>>;
    retrieve(id: string): Promise<DecisionResult>;
  };

  turns: {
    stream(params: TurnCreateParams, opts?: RequestOptions & { lastEventId?: string }): TurnStream;
    create(params: TurnCreateParams, opts?: RequestOptions): Promise<TurnObject>;
    retrieve(id: string): Promise<TurnObject>;
    /** Idempotent explicit cancel; keeps committed partial text. */
    cancel(id: string, opts?: RequestOptions): Promise<{ turn_id: string; status: "cancelling" | "cancelled" | "completed" }>;
  };

  profiles: {
    /** Profiles the caller may select (tenant allowlist applied). */
    list(params?: { kind?: "decision" | "generation" }): Promise<ProfileInfo[]>;
  };

  sessions: {
    create(params?: {
      metadata?: Record<string, string>;
      retention?: Session["retention"]["policy"];
      data_class?: DataClass;
    }): Promise<Session>;
    retrieve(id: string): Promise<Session>;
    /** Newest first; pages are fetched lazily. */
    list(params?: Page): AsyncIterable<Session>;
    update(id: string, params: SessionUpdate): Promise<Session>;
    /** Oldest first. */
    turns(id: string, params?: { limit?: number; after?: string }): AsyncIterable<SessionTurn>;
    delete(id: string): Promise<{ id: string; deleted: true; deletion_job_id: string }>;
  };

  files: {
    upload(file: Blob, params: { filename: string; purpose: "input"; session_id?: string }): Promise<FileObject>;
    retrieve(id: string): Promise<FileObject>;
    /** Server-sent status updates until ready / rejected / failed / deleted, or partial with no pending derivative. */
    watch(id: string, opts?: RequestOptions): AsyncIterable<FileObject>;
    delete(id: string): Promise<{ id: string; deleted: true }>;
    /** Derivative bytes; `page` (1-based) is required for page_images. Blob.type carries the content type. */
    derivative(id: string, kind: DerivativeKind, params?: { page?: number }): Promise<Blob>;
  };

  usage: {
    /** Rows ordered by key, then line; defaults: group_by "line", the last 30 UTC days. */
    list(params?: Page & { group_by?: UsageGroupBy; from?: string; to?: string }): AsyncIterable<UsageRow>;
  };

  /** Read-only W4 views; other roles get AghooError code permission_denied. */
  admin: {
    calibrators: {
      list(params?: Page & { profile_id?: string; question_name?: string; status?: CalibratorStatus }): AsyncIterable<CalibratorInfo>;
    };
    labeling: {
      runs(params?: Page & { profile_id?: string; question_name?: string; status?: LabelingRun["status"] }): AsyncIterable<LabelingRun>;
      dropped(params?: Page & { run_id?: string; question_name?: string; reason?: LabelingDroppedItem["reason"] }): AsyncIterable<LabelingDroppedItem>;
    };
  };
}
