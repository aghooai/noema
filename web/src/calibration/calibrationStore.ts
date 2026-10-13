import type { AghooClient, CalibratorInfo, CalibratorStatus, LabelingDroppedItem, LabelingRun } from "@aghoo/sdk";
import { errorText } from "../lib/format";
import { Store, take } from "../lib/store";

/** One read-only W4 list; each loads and fails on its own (e.g. an auditor gets 403 on dropped items only). */
export interface ListState<T> {
  rows: T[];
  loading: boolean;
  error: string | null;
}

export interface CalibrationState {
  status: CalibratorStatus | "";
  reason: LabelingDroppedItem["reason"] | "";
  calibrators: ListState<CalibratorInfo>;
  runs: ListState<LabelingRun>;
  dropped: ListState<LabelingDroppedItem>;
}

const SHOWN = 100;
const idle = <T,>(): ListState<T> => ({ rows: [], loading: false, error: null });

/** W4: GET /v1/admin/calibrators, /v1/admin/labeling/runs and /v1/admin/labeling/dropped (read-only). */
export class CalibrationStore extends Store<CalibrationState> {
  readonly #client: AghooClient;
  /** Per list, only the latest load may set its rows (filters can change while a load is in flight). */
  readonly #gen = { calibrators: 0, runs: 0, dropped: 0 };

  constructor(client: AghooClient) {
    super({ status: "", reason: "", calibrators: idle(), runs: idle(), dropped: idle() });
    this.#client = client;
  }

  async #load<K extends "calibrators" | "runs" | "dropped">(
    key: K,
    it: () => AsyncIterable<CalibrationState[K]["rows"][number]>,
  ): Promise<void> {
    const gen = ++this.#gen[key];
    this.set((s) => ({ ...s, [key]: { ...s[key], loading: true, error: null } }));
    try {
      const rows = await take(it(), SHOWN);
      if (gen === this.#gen[key]) this.set((s) => ({ ...s, [key]: { rows, loading: false, error: null } }));
    } catch (e) {
      if (gen === this.#gen[key]) this.set((s) => ({ ...s, [key]: { rows: [], loading: false, error: errorText(e) } }));
    }
  }

  async load(): Promise<void> {
    const { status, reason } = this.state;
    const admin = this.#client.admin;
    await Promise.all([
      this.#load("calibrators", () => admin.calibrators.list(status === "" ? { limit: SHOWN } : { limit: SHOWN, status })),
      this.#load("runs", () => admin.labeling.runs({ limit: SHOWN })),
      this.#load("dropped", () => admin.labeling.dropped(reason === "" ? { limit: SHOWN } : { limit: SHOWN, reason })),
    ]);
  }

  async filter(patch: { status?: CalibrationState["status"]; reason?: CalibrationState["reason"] }): Promise<void> {
    this.set(patch);
    await this.load();
  }
}
