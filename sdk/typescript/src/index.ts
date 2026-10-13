/** @aghoo/sdk: the hand-written client over the vendored contract surface (sdk.ts, types.ts). */
export { AghooClient } from "./client.js";
export { q } from "./builders.js";
export { AghooConnectionError, AghooError, AghooTimeoutError } from "./core/errors.js";
export type { AghooErrorInit } from "./core/errors.js";
export { API_VERSION, DEFAULT_BASE_URL, DEFAULT_TIMEOUT_MS } from "./core/transport.js";
export type { ClientOptions } from "./core/transport.js";
export { TurnStreamImpl } from "./stream.js";
export type { TurnStreamOptions } from "./resources/turns.js";
export type {
  CalibratorInfo,
  CalibratorStatus,
  DataClass,
  DerivativeKind,
  FileObject,
  FileRejectionCode,
  LabelingDroppedItem,
  LabelingRun,
  ProfileInfo,
  RequestOptions,
  Session,
  SessionTurn,
  SessionUpdate,
  TurnCreateParams,
  TurnObject,
  TurnStream,
  UsageGroupBy,
  UsageLine,
  UsageRow,
} from "./sdk.js";
export type * from "./types.js";
export type * from "./openai-compat.js";
