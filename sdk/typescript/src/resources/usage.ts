import type { Transport } from "../core/transport.js";
import type {
  CalibratorInfo,
  CalibratorStatus,
  LabelingDroppedItem,
  LabelingRun,
  RequestOptions,
  UsageGroupBy,
  UsageRow,
} from "../sdk.js";
import { listPages } from "./shared.js";

/** Usage and cost rows. */
export class Usage {
  readonly #t: Transport;
  constructor(t: Transport) {
    this.#t = t;
  }

  /** Rows ordered by key, then line; defaults: group_by "line", the last 30 UTC days (`from`/`to` are YYYY-MM-DD). */
  list(
    params: { limit?: number; group_by?: UsageGroupBy; from?: string; to?: string } = {},
    opts: RequestOptions = {},
  ): AsyncIterable<UsageRow> {
    const { limit, group_by, from, to } = params;
    return listPages<UsageRow>(this.#t, "/v1/usage", { limit, group_by, from, to }, opts);
  }
}

/** Read-only W4 views (`/v1/admin/...`); other roles get AghooError code permission_denied. */
export class Admin {
  readonly calibrators: {
    list(
      params?: { limit?: number; profile_id?: string; question_name?: string; status?: CalibratorStatus },
      opts?: RequestOptions,
    ): AsyncIterable<CalibratorInfo>;
  };
  readonly labeling: {
    runs(
      params?: { limit?: number; profile_id?: string; question_name?: string; status?: LabelingRun["status"] },
      opts?: RequestOptions,
    ): AsyncIterable<LabelingRun>;
    dropped(
      params?: { limit?: number; run_id?: string; question_name?: string; reason?: LabelingDroppedItem["reason"] },
      opts?: RequestOptions,
    ): AsyncIterable<LabelingDroppedItem>;
  };

  constructor(t: Transport) {
    this.calibrators = {
      list: (params = {}, opts = {}) => listPages<CalibratorInfo>(t, "/v1/admin/calibrators", { ...params }, opts),
    };
    this.labeling = {
      runs: (params = {}, opts = {}) => listPages<LabelingRun>(t, "/v1/admin/labeling/runs", { ...params }, opts),
      dropped: (params = {}, opts = {}) =>
        listPages<LabelingDroppedItem>(t, "/v1/admin/labeling/dropped", { ...params }, opts),
    };
  }
}
