import type { AghooClient, SessionTurn, UsageGroupBy, UsageLine, UsageRow } from "@aghoo/sdk";
import { fromTurnObject, type TurnView } from "../chat/turnState";
import { errorText } from "../lib/format";
import { Store, take } from "../lib/store";

export const LINES: readonly UsageLine[] = ["decision", "generation", "labeling", "verification", "waste", "cache_savings"];

/** Rows pivoted to one row per key with one cell per usage line (03 §10 W7: separate lines). */
export interface UsageTableRow {
  key: string | null;
  cells: Partial<Record<UsageLine, UsageRow["usage"]>>;
  /** Billed cost: the sum over every line except cache_savings, which carries the avoided cost (spec-11 §3). */
  billedUsd: number;
}

export function pivot(rows: UsageRow[]): UsageTableRow[] {
  const byKey = new Map<string, UsageTableRow>();
  for (const r of rows) {
    const id = r.key ?? "\u0000";
    let row = byKey.get(id);
    if (row === undefined) {
      row = { key: r.key, cells: {}, billedUsd: 0 };
      byKey.set(id, row);
    }
    row.cells[r.line] = r.usage;
    if (r.line !== "cache_savings") row.billedUsd += r.usage.cost_usd;
  }
  return [...byKey.values()];
}

export interface UsageState {
  groupBy: UsageGroupBy;
  from: string;
  to: string;
  rows: UsageRow[];
  loading: boolean;
  error: string | null;
  /** Drill-down: a session's turns, then one turn. */
  sessionId: string | null;
  sessionTurns: SessionTurn[];
  turn: TurnView | null;
  drillError: string | null;
}

const SHOWN = 1000;

/** W7: GET /v1/usage by group_by and date range, drilling into a session's turns and one turn (GET /v1/turns/{id}). */
export class UsageStore extends Store<UsageState> {
  readonly #client: AghooClient;
  /** Only the latest load may set the rows (a slower earlier response must not overwrite a newer grouping). */
  #gen = 0;

  constructor(client: AghooClient) {
    super({ groupBy: "line", from: "", to: "", rows: [], loading: false, error: null, sessionId: null, sessionTurns: [], turn: null, drillError: null });
    this.#client = client;
  }

  async load(patch: Partial<Pick<UsageState, "groupBy" | "from" | "to">> = {}): Promise<void> {
    this.set({ ...patch, loading: true, error: null });
    const gen = ++this.#gen;
    const { groupBy, from, to } = this.state;
    const params: { group_by: UsageGroupBy; limit: number; from?: string; to?: string } = { group_by: groupBy, limit: 200 };
    if (from !== "") params.from = from;
    if (to !== "") params.to = to;
    try {
      const rows = await take(this.#client.usage.list(params), SHOWN);
      if (gen === this.#gen) this.set({ rows, loading: false });
    } catch (e) {
      if (gen === this.#gen) this.set({ rows: [], loading: false, error: errorText(e) });
    }
  }

  async openSession(sessionId: string): Promise<void> {
    this.set({ sessionId, sessionTurns: [], turn: null, drillError: null });
    try {
      const turns = await take(this.#client.sessions.turns(sessionId, { limit: 100 }), SHOWN);
      this.set({ sessionTurns: turns.filter((t) => t.role === "assistant") });
    } catch (e) {
      this.set({ drillError: errorText(e) });
    }
  }

  async openTurn(turnId: string): Promise<void> {
    this.set({ turn: null, drillError: null });
    try {
      this.set({ turn: fromTurnObject(await this.#client.turns.retrieve(turnId)) });
    } catch (e) {
      this.set({ drillError: errorText(e) });
    }
  }
}
