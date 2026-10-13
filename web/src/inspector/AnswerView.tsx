import type { Answer, Calibration, HeuristicConfidence, InputView, MemberVote, QuorumInfo } from "@aghoo/sdk";
import type { ReactNode } from "react";
import { ms, num, pct, shortModel } from "../lib/format";

/** "calibrated vs LLM consensus" / "uncalibrated" (an unknown status is treated as uncalibrated, types.ts OpenEnum). */
export function calibrationLabel(c: Calibration): { text: string; cls: string } {
  if (c.status === "calibrated") return { text: "calibrated vs LLM consensus", cls: "badge badge-calibrated" };
  if (c.status === "provider_reported") return { text: "provider reported", cls: "badge badge-provider" };
  return { text: "uncalibrated", cls: "badge badge-uncalibrated" };
}

const VIEW_TEXT: Record<InputView, string> = {
  native: "native",
  thumbnail: "saw a thumbnail",
  ocr_text: "saw OCR text",
  derived_text: "saw extracted text",
  keyframes_transcript: "saw keyframes + transcript",
  metadata_only: "saw metadata only",
  skipped: "skipped",
};

export function inputViewText(v: InputView): string {
  return VIEW_TEXT[v] ?? v;
}

function statusGlyph(status: string): string {
  if (status === "ok") return "✓";
  if (status === "error") return "✗";
  return status;
}

/** The member's own vote: the argmax of its (pre-calibration) distribution, ties to the first listed. */
export function memberVote(m: MemberVote): { value: string; p: number } | null {
  let best: { value: string; p: number } | null = null;
  for (const [value, p] of Object.entries(m.calibrated_distribution ?? m.distribution ?? {})) if (best === null || p > best.p) best = { value, p };
  return best;
}

export function Members({ members }: { members: MemberVote[] }): ReactNode {
  return (
    <ul className="members">
      {members.map((m) => (
        <li key={m.member_id} className={`member member-${m.status}`} data-testid="member">
          <span className="member-model" title={m.model}>
            {shortModel(m.model)}
          </span>{" "}
          <span className="member-status">{statusGlyph(m.status)}</span>{" "}
          {memberVote(m) !== null && <span className="member-vote" data-testid="member-vote">{memberVote(m)?.value} {pct(memberVote(m)?.p)} · </span>}
          <span className="member-latency">{ms(m.latency_ms)}</span>{" "}
          <span className="member-view" title={`input view: ${m.input_view}`}>
            ({inputViewText(m.input_view)})
          </span>{" "}
          <span className="member-elicitation">{m.elicitation}</span>
          {m.skip_reason !== undefined && <span className="member-skip"> · skip: {m.skip_reason}</span>}
          {m.error !== undefined && <span className="member-error"> · {m.error.code}</span>}
        </li>
      ))}
    </ul>
  );
}

function Confidence({ c }: { c: HeuristicConfidence }): ReactNode {
  return (
    <span className="confidence" title={`heuristic (${c.method}); not a probability. margin ${num(c.margin)}, entropy ${num(c.normalized_entropy)}, agreement ${pct(c.agreement)}`}>
      confidence: <span className={`band band-${c.band}`}>{c.band}</span> ({num(c.value)})
    </span>
  );
}

function Quorum({ q }: { q: QuorumInfo }): ReactNode {
  return (
    <span className="quorum">
      quorum {q.responded}/{q.total} (needs {q.required}
      {q.refused > 0 ? `, ${q.refused} refused` : ""})
    </span>
  );
}

function Candidates({ rows }: { rows: { label: string; probability: number; votes: number }[] }): ReactNode {
  return (
    <table className="candidates">
      <tbody>
        {rows.map((r) => (
          <tr key={r.label} data-testid="candidate">
            <th scope="row">{r.label}</th>
            <td>
              <meter min={0} max={1} value={r.probability} aria-label={`${r.label} ${pct(r.probability)}`} />
            </td>
            <td className="num">≈{pct(r.probability)}</td>
            <td className="num votes" title="members whose top answer this is">
              {r.votes} {r.votes === 1 ? "vote" : "votes"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** One answer: candidate bars, calibration badge, heuristic confidence and band, quorum, per-member votes. */
export function AnswerView({ answer }: { answer: Answer }): ReactNode {
  const head = (extra: ReactNode): ReactNode => (
    <header className="answer-head">
      <span className="answer-name">{answer.name ?? "?"}</span> <span className="answer-type">{answer.type}</span> {extra}
    </header>
  );
  if (answer.type === "refusal" || answer.type === "unavailable") {
    return (
      <section className={`answer answer-${answer.type}`} data-testid="answer">
        {head(answer.type === "unavailable" ? <span className="badge badge-unavailable">unavailable: {answer.reason}</span> : <span className="badge badge-refusal">refused</span>)}
        {answer.quorum !== undefined && <Quorum q={answer.quorum} />}
        {answer.members !== undefined && <Members members={answer.members} />}
      </section>
    );
  }
  if (answer.type === "unknown") {
    return <section className="answer answer-unknown" data-testid="answer">{head(<span className="badge">unknown answer type {answer.raw_type}</span>)}</section>;
  }
  const label = calibrationLabel(answer.calibration);
  const rows = answer.candidates.map((c) => ({ label: String(c.value), probability: c.probability, votes: c.votes }));
  const top = answer.type === "choice" ? answer.choice : answer.type === "score" ? `${answer.level} (score ${num(answer.score)})` : `P(true) ${pct(answer.probability)}`;
  return (
    <section className="answer" data-testid="answer">
      {head(<span className="answer-top">{top}</span>)}
      <div className="answer-meta">
        <span className={label.cls} data-testid="calibration-badge">
          {label.text}
        </span>{" "}
        <Confidence c={answer.confidence} /> <Quorum q={answer.quorum} />
      </div>
      <Candidates rows={rows} />
      {(answer.unresolved_mass ?? 0) > 0 && <div className="unresolved">unresolved mass ≈{pct(answer.unresolved_mass)}</div>}
      {answer.type === "choice" && answer.groups !== undefined && (
        <ul className="groups">
          {answer.groups.map((g) => (
            <li key={g.id}>
              {g.label}: ≈{pct(g.probability)} {g.expanded ? "(expanded)" : "(not expanded)"}
            </li>
          ))}
        </ul>
      )}
      {answer.members !== undefined && <Members members={answer.members} />}
    </section>
  );
}
