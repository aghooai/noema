import type { DecisionResult, Degradation, RoutingDecision } from "@aghoo/sdk";
import type { ReactNode } from "react";
import type { TurnView } from "../chat/turnState";
import { ms, pct, shortModel, tokens, usd } from "../lib/format";
import { AnswerView, calibrationLabel } from "./AnswerView";

function Section({ title, testId, children }: { title: string; testId: string; children: ReactNode }): ReactNode {
  return (
    <section className="inspector-section" data-testid={testId}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

export function degradationText(d: Degradation): string {
  const who = d.model === undefined ? "" : ` for ${shortModel(d.model)}`;
  const what = d.file_id !== undefined ? d.file_id : d.part_index !== undefined ? `part ${d.part_index}` : "input";
  return `${what}: ${d.from} → ${d.to}${who} (${d.reason})`;
}

/** The answer check badge (03 §10 W1): each verification predicate passes when P(true) >= 0.5. */
export function VerificationBadge({ verification }: { verification: DecisionResult }): ReactNode {
  return (
    <span className="verification" data-testid="verification-badge">
      {verification.answers.map((a) => {
        if (a.type === "predicate" || a.type === "noul") {
          const pass = a.probability >= 0.5;
          return (
            <span key={a.name} className={pass ? "check check-pass" : "check check-fail"} title={`${calibrationLabel(a.calibration).text}, P(true) ${pct(a.probability)}`}>
              ⓘ answer check: {a.name.replaceAll("_", " ")} {pass ? "✓" : "✗"}
            </span>
          );
        }
        return (
          <span key={a.name ?? "?"} className="check check-unavailable">
            ⓘ answer check: {a.name ?? "?"} {a.type}
          </span>
        );
      })}
    </span>
  );
}

function Routing({ r, confirmed }: { r: RoutingDecision; confirmed: boolean }): ReactNode {
  return (
    <>
      <div data-testid="route">
        route: <strong>{r.route_id}</strong> ({r.mode}) · {r.selection} · {r.strategy} {confirmed ? "" : <em>(speculative)</em>}
      </div>
      <div className="route-reason">reason: {r.reason}</div>
      <div className="route-bindings">matched bindings: {r.matched_bindings.length === 0 ? "none" : r.matched_bindings.join(", ")}</div>
      {r.basis.length > 0 && (
        <ul className="route-basis">
          {r.basis.map((b) => (
            <li key={b.name}>
              {b.name} = {String(b.value)} (≈{pct(b.probability)}, {b.band}, {b.status})
            </li>
          ))}
        </ul>
      )}
      {r.fallbacks.length > 0 && <div>fallbacks: {r.fallbacks.join(" → ")}</div>}
      {r.served_spec_differs !== null && <div>served spec differs from the decided one: {r.served_spec_differs ? "yes" : "no"}</div>}
    </>
  );
}

function Conditioning({ r }: { r: RoutingDecision }): ReactNode {
  return (
    <>
      <div data-testid="effort">
        effort: {r.params.effort ?? "model default"}
        {r.params.max_output_tokens !== undefined ? ` · max_output_tokens ${r.params.max_output_tokens}` : ""}
      </div>
      <div data-testid="conditioning">
        conditioning: {r.conditioning === null ? "none" : `${r.conditioning.answer_mode}: ${r.conditioning.summary}`}
      </div>
    </>
  );
}

function Decision({ d, arrived, changed }: { d: DecisionResult; arrived: string | null; changed: string[] }): ReactNode {
  const p = d.provenance;
  return (
    <>
      <div className="decision-meta">
        {p.profile_id}@{p.profile_version} · status {d.status}
        {arrived !== null ? ` · arrived ${arrived.replaceAll("_", " ")}` : ""}
        {p.cache.hit ? " · cache hit" : ""} · {usd(d.usage.cost_usd)}
      </div>
      {changed.length > 0 && <div className="decision-changed">late members merged: {changed.join(", ")}</div>}
      {d.answers.map((a, i) => (
        <AnswerView key={`${a.name ?? "?"}-${i}`} answer={a} />
      ))}
    </>
  );
}

/** W1 decision inspector for one turn (live or stored). */
export function Inspector({ view }: { view: TurnView }): ReactNode {
  const d = view.decision;
  const stages = d?.provenance.stages ?? [];
  return (
    <div className="inspector" data-testid="inspector">
      {view.turnId !== null && (
        <div className="muted turn-id" data-testid="turn-id">
          {view.turnId}
        </div>
      )}
      {view.verification !== null && (
        <Section title="Verification" testId="inspector-verification">
          <VerificationBadge verification={view.verification} />
        </Section>
      )}
      <Section title="Decision" testId="inspector-decision">
        {d === null ? <div className="muted">{view.status === "completed" ? "no decision" : "waiting for the decision…"}</div> : <Decision d={d} arrived={view.decisionArrived} changed={view.decisionChanged} />}
      </Section>
      {stages.length > 0 && (
        <Section title="Escalation stages" testId="inspector-stages">
          <ol>
            {stages.map((s, i) => (
              <li key={i}>
                {s.profile_id}@{s.profile_version}: {s.questions.join(", ")} (triggered by {s.triggered_by.join(", ")}) · {usd(s.usage.cost_usd)}
              </li>
            ))}
          </ol>
        </Section>
      )}
      <Section title="Route" testId="inspector-route">
        {view.routing === null ? <div className="muted">no route yet</div> : <Routing r={view.routing} confirmed={view.routingConfirmed} />}
      </Section>
      {view.routing !== null && (
        <Section title="Effort and conditioning" testId="inspector-conditioning">
          <Conditioning r={view.routing} />
        </Section>
      )}
      <Section title="Attempts" testId="inspector-attempts">
        <div data-testid="spec-restarts">spec restarts: {view.specRestarts}</div>
        <ul>
          {view.attempts.map((a) => (
            <li key={a.attemptId}>
              {a.attemptId}: {a.routeId} · {shortModel(a.model)}
              {a.speculative ? " (speculative)" : ""} · {a.status}
              {a.detail !== null && a.detail !== a.status ? ` (${a.detail})` : ""}
              {a.finishReason !== null ? ` · finished: ${a.finishReason}` : ""}
            </li>
          ))}
        </ul>
        {view.reroutes.map((r) => (
          <div key={r.to}>rerouted {r.from} → {r.to}: {r.reason}</div>
        ))}
        {view.timings !== null && (
          <div className="timings">
            ttft {ms(view.timings.ttft_ms)} · decision {ms(view.timings.decision_ms)} · holdback {ms(view.timings.holdback_ms)} · e2e {ms(view.timings.e2e_ms)} · wasted {tokens(view.timings.wasted_output_tokens)} tokens
          </div>
        )}
        {view.usage !== null && <div className="turn-usage">turn cost {usd(view.usage.cost_usd)}{view.usage.cost_estimated ? " (estimated)" : ""}</div>}
      </Section>
      <Section title="Degradations" testId="inspector-degradations">
        {view.degradations.length === 0 ? <div className="muted">none</div> : (
          <ul>
            {view.degradations.map((g, i) => (
              <li key={i} className={`degradation degradation-${g.reason}`}>{degradationText(g)}</li>
            ))}
          </ul>
        )}
      </Section>
      {view.errors.length > 0 && (
        <Section title="Errors" testId="inspector-errors">
          {view.errors.map((e, i) => (
            <div key={i} className="error">{e.code}: {e.message}{e.retryable ? " (retryable)" : ""}</div>
          ))}
        </Section>
      )}
    </div>
  );
}
