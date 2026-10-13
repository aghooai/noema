import type { Usage, UsageGroupBy } from "@aghoo/sdk";
import { type ReactNode, useEffect, useState } from "react";
import { Inspector } from "../inspector/Inspector";
import { tokens, usd } from "../lib/format";
import { useStore } from "../lib/store";
import { LINES, pivot, type UsageStore } from "./usageStore";

const GROUPS: UsageGroupBy[] = ["line", "project", "session", "model", "day"];

function Cell({ u }: { u: Usage | undefined }): ReactNode {
  if (u === undefined) return <td className="num muted">–</td>;
  return (
    <td className="num" title={`in ${tokens(u.input_tokens)} (cached ${tokens(u.cached_input_tokens)}) · out ${tokens(u.output_tokens)} (reasoning ${tokens(u.reasoning_tokens)})${u.cost_estimated ? " · estimated" : ""}`}>
      {usd(u.cost_usd)}
    </td>
  );
}

function TurnUsage({ store }: { store: UsageStore }): ReactNode {
  const s = useStore(store);
  const t = s.turn;
  if (t === null) return null;
  return (
    <section className="turn-drill" data-testid="turn-drill">
      <h3>Turn {t.turnId}</h3>
      <table className="usage-table">
        <thead>
          <tr><th>part</th><th>model</th><th>outcome</th><th>input</th><th>output</th><th>cost</th></tr>
        </thead>
        <tbody>
          {t.decision !== null && (
            <tr><td>decision</td><td>{t.decision.provenance.profile_id}</td><td>{t.decision.status}</td><td className="num">{tokens(t.decision.usage.input_tokens)}</td><td className="num">{tokens(t.decision.usage.output_tokens)}</td><td className="num">{usd(t.decision.usage.cost_usd)}</td></tr>
          )}
          {t.attempts.map((a) => (
            <tr key={a.attemptId}>
              <td>{a.status === "committed" ? "generation" : "waste"} ({a.attemptId})</td><td>{a.model}</td><td>{a.status}</td>
              <td className="num">{a.usage === null ? "–" : tokens(a.usage.input_tokens)}</td><td className="num">{a.usage === null ? "–" : tokens(a.usage.output_tokens)}</td><td className="num">{usd(a.usage?.cost_usd)}</td>
            </tr>
          ))}
          {t.verification !== null && (
            <tr><td>verification</td><td>{t.verification.provenance.profile_id}</td><td>{t.verification.status}</td><td className="num">{tokens(t.verification.usage.input_tokens)}</td><td className="num">{tokens(t.verification.usage.output_tokens)}</td><td className="num">{usd(t.verification.usage.cost_usd)}</td></tr>
          )}
          {t.usage !== null && (
            <tr className="total"><td>turn total</td><td /><td /><td className="num">{tokens(t.usage.input_tokens)}</td><td className="num">{tokens(t.usage.output_tokens)}</td><td className="num">{usd(t.usage.cost_usd)}</td></tr>
          )}
        </tbody>
      </table>
      <details>
        <summary>Inspector</summary>
        <Inspector view={t} />
      </details>
    </section>
  );
}

/** W7: usage and cost by group, one column per line, with a drill-down from a session to its turns to one turn. */
export function UsagePage({ store }: { store: UsageStore }): ReactNode {
  const s = useStore(store);
  const [turnId, setTurnId] = useState("");
  useEffect(() => {
    void store.load();
  }, [store]);
  const table = pivot(s.rows);
  return (
    <div className="usage-page">
      <h2>Usage and cost</h2>
      <form className="usage-filter" onSubmit={(e) => { e.preventDefault(); void store.load(); }}>
        <label>group by <select value={s.groupBy} onChange={(e) => void store.load({ groupBy: e.target.value as UsageGroupBy })} data-testid="group-by">{GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}</select></label>{" "}
        <label>from <input type="date" value={s.from} onChange={(e) => store.set({ from: e.target.value })} /></label>{" "}
        <label>to <input type="date" value={s.to} onChange={(e) => store.set({ to: e.target.value })} /></label>{" "}
        <button type="submit">Apply</button>
      </form>
      {s.error !== null && <div className="error" data-testid="usage-error">{s.error}</div>}
      {s.loading ? <div className="muted">loading…</div> : (
        <table className="usage-table" data-testid="usage-table">
          <thead>
            <tr><th>{s.groupBy}</th>{LINES.map((l) => <th key={l}>{l.replace("_", " ")}</th>)}<th>billed</th></tr>
          </thead>
          <tbody>
            {table.map((r) => (
              <tr key={r.key ?? "-"} data-testid="usage-row">
                <th scope="row">
                  {s.groupBy === "session" && r.key !== null ? <button type="button" className="link-button" onClick={() => void store.openSession(r.key as string)}>{r.key}</button> : (r.key ?? "–")}
                </th>
                {LINES.map((l) => <Cell key={l} u={r.cells[l]} />)}
                <td className="num">{usd(r.billedUsd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {s.sessionId !== null && (
        <section data-testid="session-turns">
          <h3>Turns of {s.sessionId}</h3>
          <ul>
            {s.sessionTurns.map((t) => (
              <li key={t.turn_id}><button type="button" className="link-button" onClick={() => void store.openTurn(t.turn_id)}>#{t.seq} {t.turn_id}</button> {t.text.slice(0, 80)}</li>
            ))}
          </ul>
        </section>
      )}
      <form onSubmit={(e) => { e.preventDefault(); if (turnId.trim() !== "") void store.openTurn(turnId.trim()); }}>
        <label>turn id <input value={turnId} onChange={(e) => setTurnId(e.target.value)} placeholder="turn_…" data-testid="turn-id-input" /></label> <button type="submit">Open turn</button>
      </form>
      {s.drillError !== null && <div className="error">{s.drillError}</div>}
      <TurnUsage store={store} />
    </div>
  );
}
