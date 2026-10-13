import type { ErrorCode, ErrorType, JsonValue, OpenEnum } from "../types.js";

/** Fields of an {@link AghooError}; the HTTP status plus the `Error` envelope (03 §1) and the `retry-after` header. */
export interface AghooErrorInit {
  status: number;
  type: OpenEnum<ErrorType>;
  code: OpenEnum<ErrorCode>;
  message: string;
  param: string | null;
  requestId: string | null;
  retryable: boolean;
  retryAfterS: number | null;
  details: { [k: string]: JsonValue } | undefined;
}

/** Thrown for every non-2xx HTTP response (after retries). SSE `error` events are yielded by TurnStream, not thrown. */
export class AghooError extends Error {
  readonly status: number;
  readonly type: OpenEnum<ErrorType>;
  readonly code: OpenEnum<ErrorCode>;
  readonly param: string | null;
  readonly requestId: string | null;
  readonly retryable: boolean;
  /** From `retry-after`, in seconds. */
  readonly retryAfterS: number | null;
  readonly details: { [k: string]: JsonValue } | undefined;

  constructor(init: AghooErrorInit) {
    super(init.message);
    this.name = "AghooError";
    this.status = init.status;
    this.type = init.type;
    this.code = init.code;
    this.param = init.param;
    this.requestId = init.requestId;
    this.retryable = init.retryable;
    this.retryAfterS = init.retryAfterS;
    this.details = init.details;
  }
}

/**
 * The request never produced a usable HTTP response (DNS, connect or TLS failure, a reset), or a stream ended before
 * its final event or carried a frame that is not valid JSON. Retried like a retryable status before the first stream
 * event; thrown after that. A dropped turn stream does not cancel the turn.
 */
export class AghooConnectionError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "AghooConnectionError";
  }
}

/** `timeoutMs` elapsed before the response (for streams: before the first event) arrived. */
export class AghooTimeoutError extends AghooConnectionError {
  constructor(timeoutMs: number) {
    super(`request timed out after ${timeoutMs} ms`);
    this.name = "AghooTimeoutError";
  }
}

const TYPE_BY_STATUS: Readonly<Record<number, ErrorType>> = {
  401: "authentication_error",
  403: "permission_error",
  404: "not_found_error",
  409: "conflict_error",
  422: "capability_error",
  429: "rate_limit_error",
  503: "upstream_error",
  504: "upstream_error",
};

/** Status-only fallback for bodies that are not a Noema `Error` envelope (e.g. a proxy page). */
function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 429 || (status >= 500 && status !== 501);
}

/** `retry-after` as integer seconds (the contract never sends the HTTP-date form); null when absent or invalid. */
export function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\s*\d+\s*$/.test(value)) return null;
  return Number.parseInt(value, 10);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Builds the AghooError of a non-2xx response from its body text and headers. */
export function errorFromResponse(status: number, headers: Headers, bodyText: string): AghooError {
  const retryAfterS = parseRetryAfter(headers.get("retry-after"));
  const headerRequestId = headers.get("x-request-id");
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    parsed = undefined;
  }
  const env = isRecord(parsed) && isRecord(parsed.error) ? parsed.error : undefined;
  if (env !== undefined && typeof env.code === "string" && typeof env.type === "string") {
    return new AghooError({
      status,
      type: env.type,
      code: env.code,
      message: typeof env.message === "string" ? env.message : `HTTP ${status}`,
      param: typeof env.param === "string" ? env.param : null,
      requestId: typeof env.request_id === "string" ? env.request_id : headerRequestId,
      retryable: typeof env.retryable === "boolean" ? env.retryable : isRetryableStatus(status),
      retryAfterS,
      details: isRecord(env.details) ? (env.details as { [k: string]: JsonValue }) : undefined,
    });
  }
  return new AghooError({
    status,
    type: TYPE_BY_STATUS[status] ?? (status >= 500 ? "server_error" : "invalid_request_error"),
    code: "unknown_error",
    message: `HTTP ${status}${bodyText ? `: ${bodyText.slice(0, 200)}` : ""}`,
    param: null,
    requestId: headerRequestId,
    retryable: isRetryableStatus(status),
    retryAfterS,
    details: undefined,
  });
}
