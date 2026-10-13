import type { CalibratorInfo, CalibratorStatus, LabelingDroppedItem, LabelingRun } from "@aghoo/sdk";
import { type ReactNode, useEffect } from "react";
import { num, pct, unixTime, usd } from "../lib/format";
import { useStore } from "../lib/store";
import type { CalibrationStore, ListState } from "./calibrationStore";

const STATUSES: CalibratorStatus[] = ["fitted", "rejected", "accepted", "active", "downgraded", "retired"];
const REASONS: LabelingDroppedItem["reason"][] = ["disagreed", "refused", "parse_error", "labeler_error"];

function ci(range: number[] | undefined): string {
  return range === undefined || range.length < 2 ? "" : ` [${num(range[0], 3)}, ${num(range[1], 3)}]`;
}

function List<T>({ state, empty, children }: { state: ListState<T>; empty: string; children: (rows: T[]) => ReactNode }): ReactNode {
  if (state.loading) return <div className="muted">loading…</div>;
  if (state.error !== null) return <div className="error" data-testid="list-error">{state.error}</div>;
  if (state.rows.length === 0) return <div className="muted">{empty}</div>;
  return children(state.rows);
}

/** Reliability diagram as a table: per bin, mean predicted vs observed (vs LLM consensus), as two meters. No SVG. */
export function Reliability({ c }: { c: CalibratorInfo }): ReactNode {
  return (
    <table className="reliability" data-testid="reliability">
      <thead>
        <tr><th>bin</th><th>n</th><th>predicted</th><th>observed (consensus)</th></tr>
      </thead>
      <tbody>
        {c.reliability.bins.map((b) => (
          <tr key={`${b.lower}-${b.upper}`}>
            <td>{num(b.lower)}–{num(b.upper)}</td>
            <td className="num">{b.n}</td>
            <td><meter min={0} max={1} value={b.mean_predicted ?? 0} /> {num(b.mean_predicted)}</td>
            <td><meter min={0} max={1} value={b.observed ?? 0} /> {num(b.observed)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function CalibratorCard({ c }: { c: CalibratorInfo }): ReactNode {
  const h = c.holdout;
  return (
    <article className="calibrator" data-testid="calibrator">
      <header>
        <strong>{c.question_name}</strong> <span className={`badge status-${c.status}`}>{c.status}</span> <span className="badge">{c.scope}</span> {c.method} · {c.binding.profile_id}@{c.binding.profile_version} · {c.binding.choice_method} · {c.id}
      </header>
      <div>members: {c.member_subset.join(", ")} · acceptance {c.acceptance.passed ? "passed" : `failed (${c.acceptance.failed_rules.join(", ")})`}</div>
      {h === null ? <div className="muted">no holdout evaluation</div> : (
        <div className="holdout" data-testid="holdout">
          vs LLM consensus ({h.labelers.join(" + ")}): n {h.n} · ECE {num(h.ece, 3)}{ci(c.reliability.ci.ece)} · Brier {num(h.brier, 3)}{ci(c.reliability.ci.brier)} · NLL {num(h.nll, 3)}{ci(c.reliability.ci.nll)} (uncalibrated {num(h.nll_uncalibrated, 3)}) · coverage {pct(h.scope.retained_share)} of uniform traffic · consensus {pct(h.consensus_rate)}
          {h.flagged_classes.length > 0 && <div>flagged classes (&lt; 30 labels): {h.flagged_classes.join(", ")}</div>}
        </div>
      )}
      <Reliability c={c} />
      <div className="muted">
        created {unixTime(c.created_at)} · approved by {c.approved_by ?? "–"} · activated by {c.activated_by.join(", ") || "–"} at {unixTime(c.activated_at)}
        {c.downgrade_reason !== null && ` · downgraded (${c.downgrade_reason}) ${unixTime(c.downgraded_at)}`}
        {c.retired_reason !== null && ` · retired (${c.retired_reason}) ${unixTime(c.retired_at)}`}
      </div>
    </article>
  );
}

export function RunsTable({ rows }: { rows: LabelingRun[] }): ReactNode {
  return (
    <table className="runs" data-testid="runs">
      <thead>
        <tr><th>run</th><th>status</th><th>question</th><th>labelers</th><th>sampled</th><th>kept</th><th>dropped</th><th>consensus by class</th><th>cost</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td title={r.id}>{r.profile_id}@{r.profile_version}</td>
            <td>{r.status}</td>
            <td>{r.question_name}</td>
            <td>{r.labelers.join(" + ")}</td>
            <td className="num">{r.counts.sampled} ({r.counts.sampled_uniform} uniform, {r.counts.sampled_uncertainty} uncertainty)</td>
            <td className="num">{r.counts.kept} ({pct(r.consensus_rate)})</td>
            <td className="num">{r.counts.dropped_disagreed + r.counts.dropped_refused + r.counts.dropped_parse_error + r.counts.dropped_labeler_error}</td>
            <td>{Object.entries(r.consensus_rate_by_class).map(([k, v]) => `${k} ${pct(v)} (n ${r.n_by_class[k] ?? 0})`).join(" · ")}</td>
            <td className="num">{usd(r.usage.cost_usd)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function DroppedList({ rows }: { rows: LabelingDroppedItem[] }): ReactNode {
  return (
    <ul className="dropped" data-testid="dropped">
      {rows.map((d) => (
        <li key={d.id}>
          <strong>{d.question_name}</strong> · {d.reason} · sampled ({d.sample_reason}) {unixTime(d.sampled_at)} · ensemble {String(d.ensemble.value)} ({pct(d.ensemble.probability)}, {d.ensemble.calibration_status})
          <ul>
            {d.labeler_answers.map((a) => (
              <li key={a.model}>{a.model}: {a.status === "ok" ? String(a.value) : a.status}</li>
            ))}
          </ul>
          <pre className="excerpt">{d.content.excerpt ?? "(no stored content)"}{d.content.redacted ? "\n[redacted]" : ""}</pre>
        </li>
      ))}
    </ul>
  );
}

/** W4 (read-only): calibrators, labeling runs (labels vs LLM consensus) and dropped items. Activation stays in the CLI. */
export function CalibrationPage({ store }: { store: CalibrationStore }): ReactNode {
  const s = useStore(store);
  useEffect(() => {
    void store.load();
  }, [store]);
  return (
    <div className="calibration-page">
      <h2>Calibration (labels are LLM consensus, not human ground truth)</h2>
      <section>
        <h3>Calibrators</h3>
        <label>
          status{" "}
          <select value={s.status} onChange={(e) => void store.filter({ status: e.target.value as CalibratorStatus | "" })}>
            <option value="">any</option>
            {STATUSES.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
        <List state={s.calibrators} empty="No calibrators.">{(rows) => rows.map((c) => <CalibratorCard key={c.id} c={c} />)}</List>
      </section>
      <section>
        <h3>Labeling runs</h3>
        <List state={s.runs} empty="No labeling runs.">{(rows) => <RunsTable rows={rows} />}</List>
      </section>
      <section>
        <h3>Dropped items (labelers disagreed or failed)</h3>
        <label>
          reason{" "}
          <select value={s.reason} onChange={(e) => void store.filter({ reason: e.target.value as LabelingDroppedItem["reason"] | "" })}>
            <option value="">any</option>
            {REASONS.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
        <List state={s.dropped} empty="No dropped items.">{(rows) => <DroppedList rows={rows} />}</List>
      </section>
    </div>
  );
}
