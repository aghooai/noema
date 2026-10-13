/**
 * OpenAI Decisions-compatible facade: POST /openai/v1/decisions (proposal v0.2).
 *
 * Mirrors the request/response shapes of POST the OpenAI Decisions endpoint (/v1/decisions) [OA-R] so existing clients only
 * change base_url and model (model = a Noema decision profile, e.g. "aghoo/lightweight-model-decide" or "aghoo/router-default").
 * Noema never calls OpenAI (D1): every request is answered by a Bedrock decision profile (default lightweight-model-decide,
 * gpt-6-luna alone) and mapped back to these shapes. Compatibility is of shapes only: outputs and confidence values
 * are not equivalent to OpenAI's. Limits: at most 32 questions, 2-255 choices, 2-16 score levels.
 */

export type OAInputPart =
  | { type: "input_text"; text: string }
  | { type: "input_image"; image_url: string; detail?: "low" | "high" | "auto" | "original" | null };

export interface OAInputMessage {
  role: "user";
  type?: "message";
  content: string | OAInputPart[];
}

export type OAQuestion =
  | { type: "predicate"; instructions: string; name?: string | null }
  /** Explicit names must be unique and must not match q<digits> (reserved for generated names). `true` and "true" (or false/"false") collide: 400. */
  | { type: "choice"; instructions: string; name?: string | null; choices: { value: string | boolean; description?: string | null }[] }
  | { type: "score"; instructions: string; name?: string | null; levels: { label: string; description?: string | null }[] }; // 2-16 levels

export interface OADecisionCreateParams {
  model: string; // "aghoo/<profile>[@version]"; other values -> 400 model_not_available
  input: string | OAInputMessage[];
  questions: OAQuestion[]; // 1-32; a question that needs the hierarchical choice method is 422 unsupported_parameter
  safety_identifier?: string; // <= 128 chars; stored for abuse tracking, never sent to models
}

export type OAAnswer =
  | { type: "predicate"; name: string | null; probability: number }
  | { type: "choice"; name: string | null; choice: string | boolean; confidence: number;
      /** In declared choice order (not sorted). */
      probabilities: { value: string | boolean; probability: number }[] }
  | { type: "score"; name: string | null; score: number; confidence: number;
      /** In level index order. */
      probabilities: { value: number; label: string; probability: number }[] }
  | { type: "refusal"; name: string | null };

export interface OAUsage {
  input_tokens: number;
  input_tokens_details: { cached_tokens: number; cache_write_tokens: number };
  output_tokens: number;
  output_tokens_details: { reasoning_tokens: number };
  total_tokens: number;
}

export interface OADecisionResponse {
  /** Resolved profile, version-pinned: "aghoo/router-default@1". */
  model: string;
  /** Exactly the requested questions, in request order (router questions are never added). */
  answers: OAAnswer[];
  usage: OAUsage;
  /** Present only with request header `x-aghoo-include: ensemble`. */
  x_aghoo?: {
    decision_id: string;
    calibration: Record<string, "calibrated" | "uncalibrated">;
    /** Native-only answer types mapped to refusal: name -> original reason. */
    mapped_unavailable?: Record<string, "deadline_exceeded" | "no_quorum" | "all_members_failed" | "no_capable_member">;
  };
}
