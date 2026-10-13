"""Builders of the golden SSE fixtures G01–G12 (spec-04 §4.2): scenario + request + expected events + DB.

Times are ms from t_d0 (t_in = t_d0 − 6). Generation text arrives one chunk every 100 ms from the first text; a
held attempt's buffered chunks are flushed at the commit point. Decision payloads come from running the fake
decision engine on the fixture's own scenario (`sse_scenarios.decide`). `regenerate.py` writes the YAML files;
`tests/contract_internal/test_golden_fixtures.py` checks them against these builders and the contract.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

from noema.core.errors import ErrorCode

from . import sse_events as se
from .sse_scenarios import (
    CODING,
    FACTUAL,
    HARD,
    INJECTION,
    MODERATE,
    USER_TEXT,
    DecisionTimeline,
    decide,
    document,
    router_rules,
    stream,
    verifier_rules,
)

T_IN: Final = -6
STEP: Final = 100
CHUNKS: Final = ("The capital ", "of France ", "is Paris. ", "It is also ", "the largest ", "city in ", "the country.")
SOL, OPUS, GLM = "us.openai.gpt-6.1-sol", "us.anthropic.claude-opus-5-5", "us.zai.glm-5.3"
STICKY_SOL: Final = {
    "generation_profile": "sol-fast",
    "effort": None,
    "max_output_tokens": None,
    "answer_mode": "direct",
    "modalities": ["text"],
}
STICKY_OPUS: Final = {
    "generation_profile": "opus-deep",
    "effort": "high",
    "max_output_tokens": 4000,
    "answer_mode": "direct",
    "modalities": ["text"],
}
OPUS_PARAMS: Final = {"effort": "high", "max_output_tokens": 4000}
B0, B2, B4 = "bindings[0]", "bindings[2]", "bindings[4]"
Fixture = dict[str, Any]


def request(session_id: str | None = "ses_1", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": USER_TEXT}]}],
        "stream": True,
    }
    if session_id is not None:
        body["session_id"] = session_id
    return {"headers": {"Aghoo-Version": "2026-10-01"}, "body": {**body, **extra}}


class Timeline:
    """Expected events in emission order (stable for equal times: append order)."""

    def __init__(self) -> None:
        self.events: list[se.Ev] = []

    def add(self, at: float, data: se.Json, *exact: str, note: str | None = None) -> se.Ev:
        ev = se.Ev(at, data, tuple(exact), note)
        self.events.append(ev)
        return ev

    def deltas(
        self,
        attempt: str,
        first_text: float,
        commit_at: float,
        chunks: Sequence[str] = CHUNKS,
        *,
        until: float | None = None,
        insert: Sequence[se.Ev] = (),
    ) -> float:
        """Committed deltas (buffer flushed at `commit_at`, then live); `insert`ed events go in by time. Returns
        the time of the last delta."""
        pending = sorted(insert, key=lambda e: e.at)
        last = commit_at
        for seq, text in enumerate(chunks):
            at = max(commit_at, first_text + seq * STEP)
            if until is not None and at > until:
                break
            while pending and pending[0].at < at:
                self.events.append(pending.pop(0))
            self.add(at, se.delta(attempt, seq, text), "attempt_id", "seq")
            last = at
        self.events.extend(pending)
        return last

    def wire(self) -> list[dict[str, Any]]:
        return [
            {
                "id": i + 1,
                "at": _ms(ev.at),
                "type": ev.type,
                "data": ev.data,
                "exact": list(ev.exact),
                **({"note": ev.note} if ev.note else {}),
            }
            for i, ev in enumerate(self.events)
        ]


def _ms(at: float) -> int | float:
    return int(at) if float(at).is_integer() else round(at, 3)


def prefix(
    t: Timeline,
    route_id: str,
    alias: str,
    selection: str,
    *,
    session_id: str | None = "ses_1",
    strategy: str = "holdback",
) -> None:
    """Events 1-4 (spec-04 §4.2 prefix P)."""
    t.add(T_IN + 1, se.turn_created(session_id), "session_id")
    t.add(0, se.preflight(se.tokens(USER_TEXT)), "modalities", "degradations")
    routing = se.Routing(route_id, selection, strategy=strategy, reason=f"spec0 from {selection}")
    t.add(
        0,
        se.route_selected("att_1", routing),
        "attempt_id",
        "speculative",
        "routing.route_id",
        "routing.generation_profile",
        "routing.selection",
        "routing.strategy",
        "routing.decision_id",
        "routing.matched_bindings",
        "routing.basis",
        "routing.conditioning",
        "routing.served_spec_differs",
    )
    t.add(0, se.started("att_1", route_id, alias, True), "attempt_id", "route_id", "model", "speculative")


def gen_usage(text: str, *, estimated: bool = False) -> se.Json:
    return se.usage(se.tokens(USER_TEXT) + 40, se.tokens(text), estimated=estimated)


def fixture(
    fid: str,
    title: str,
    scenario: dict[str, Any],
    t: Timeline,
    *,
    req: dict[str, Any] | None = None,
    setup: dict[str, Any] | None = None,
    db: dict[str, Any] | None = None,
    **extra: Any,
) -> Fixture:
    return {
        "id": fid,
        "title": title,
        "spec": f"spec-04 §4.2 {fid.rstrip('b')}",
        "t_in_ms": T_IN,
        "scenario": scenario,
        "setup": setup or {},
        "request": req or request(),
        "events": t.wire(),
        "db": db or {},
        **extra,
    }


def timings(
    *,
    first_delta: float | None,
    decision_ms: int | None,
    lead: int | None,
    holdback: int | None,
    end: float,
    restarts: int = 0,
    wasted: int = 0,
) -> dict[str, Any]:
    return {
        "ttft_ms": None if first_delta is None else round(first_delta - T_IN),
        "decision_ms": decision_ms,
        "decision_lead_ms": lead,
        "holdback_ms": holdback,
        "e2e_ms": round(end - T_IN),
        "spec_restarts": restarts,
        "wasted_output_tokens": wasted,
    }


def basis_intent(timeline: DecisionTimeline) -> se.Json:
    a = timeline.s1.answers["intent"]
    return se.basis(
        "intent", a.value, round(float(a.probability or 0.0), 4), round(float(a.margin or 0.0), 4), a.band or "high"
    )


def _finish(
    t: Timeline,
    attempt: str,
    end: float,
    text: str,
    decisions: Sequence[DecisionTimeline],
    first_delta: float | None,
    *,
    decision_ms: int | None,
    lead: int | None,
    holdback: int | None,
    restarts: int = 0,
    wasted: int = 0,
    wasted_exact: bool = True,
    completed: bool = True,
) -> None:
    """`wasted_exact=False` when a cancelled attempt was cut mid-stream: the fake engines estimate its output as
    chars/4, the real engine adds `platform.cost.cancel_overrun_tokens` (IMPL-020; generation.md item 2)."""
    gen = gen_usage(text)
    if completed:
        t.add(end, se.completed(attempt, "stop", gen), "attempt_id", "finish_reason")
    total = se.add_usage(gen, *(d.final_result["usage"] for d in decisions))
    tm = timings(
        first_delta=first_delta,
        decision_ms=decision_ms,
        lead=lead,
        holdback=holdback,
        end=end,
        restarts=restarts,
        wasted=wasted,
    )
    inexact = {"e2e_ms"} if wasted_exact else {"e2e_ms", "wasted_output_tokens"}
    t.add(end, se.turn_completed(total, **tm), "turn_id", *(f"timings.{k}" for k in tm if k not in inexact))


def g01(fid: str = "G01", title: str = "agree", req: dict[str, Any] | None = None, **extra: Any) -> Fixture:
    """Sticky sol-fast; Sol first text @720; D-std: S1 @930 (quorum) agrees; final @1000."""
    doc = document(fid, [stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS)], router_rules())
    d = decide(doc)
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.id", "decision.status")
    confirmed = se.Routing(
        "sol-fast",
        "rule",
        "dec_1",
        [B0],
        [basis_intent(d)],
        served_spec_differs=False,
        reason="bindings[0]: intent factual_qa",
    )
    t.add(
        930,
        se.route_confirmed("att_1", confirmed),
        "attempt_id",
        "routing.selection",
        "routing.decision_id",
        "routing.matched_bindings",
        "routing.served_spec_differs",
        "routing.conditioning",
    )
    t.add(930, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    upd = se.Ev(1000, se.decision_updated(d.final_result, d.changed), ("changed", "decision.status"))
    end = t.deltas("att_1", 720, 930, insert=[upd])
    _finish(t, "att_1", end, "".join(CHUNKS), [d], 930, decision_ms=930, lead=-210, holdback=210)
    db = {
        "turn": {
            "status": "completed",
            "routing.served_spec_differs": False,
            "routing.decision_id": "dec_1",
            "routing.matched_bindings": [B0],
        },
        "attempts": [{"id": "att_1", "outcome": "committed", "finish_reason": "stop"}],
        "session": {"last_seq": 4, "sticky_spec": STICKY_SOL},
    }
    return fixture(
        fid,
        title,
        doc,
        t,
        req=req,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL}},
        db=db,
        **extra,
    )


def g02() -> Fixture:
    """Intent coding + hard -> decided (opus-deep, high, 4000): A1 cancelled (spec_changed), A2 on Opus."""
    doc = document(
        "G02",
        [
            stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS),
            stream(OPUS, first_event_ms=400, first_text_ms=1970, chunks=CHUNKS, reasoning=40),
        ],
        router_rules(intent=CODING, level=HARD),
    )
    d = decide(doc)
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.status")
    t.add(930, se.cancelled("att_1", "spec_changed"), "attempt_id", "reason")
    t.add(930, se.started("att_2", "opus-deep", "opus-5.5", False), "attempt_id", "route_id", "model", "speculative")
    t.add(1000, se.decision_updated(d.final_result, d.changed), "changed")
    confirmed = se.Routing(
        "opus-deep",
        "rule",
        "dec_1",
        [B0, B2],
        [basis_intent(d)],
        OPUS_PARAMS,
        served_spec_differs=False,
        reason="bindings[0]: intent coding; bindings[2]: hard",
    )
    t.add(
        2900,
        se.route_confirmed("att_2", confirmed),
        "attempt_id",
        "routing.route_id",
        "routing.mode",
        "routing.selection",
        "routing.matched_bindings",
        "routing.params",
        "routing.served_spec_differs",
    )
    t.add(2900, se.committed("att_2", "spec_agrees"), "attempt_id", "reason")
    end = t.deltas("att_2", 2900, 2900)
    wasted = se.tokens("".join(CHUNKS[:3]))
    _finish(
        t,
        "att_2",
        end,
        "".join(CHUNKS),
        [d],
        2900,
        decision_ms=930,
        lead=-210,
        holdback=0,
        restarts=1,
        wasted=wasted,
        wasted_exact=False,  # att_1 was cut after 3 chunks (real engine: 72)
    )
    db = {
        "turn": {"status": "completed"},
        "session": {"sticky_spec": STICKY_OPUS},
        "attempts": [
            {"id": "att_1", "outcome": "cancelled", "cancel_reason": "spec_changed"},
            {"id": "att_2", "outcome": "committed"},
        ],
    }
    return fixture(
        "G02",
        "disagree + restart",
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL}},
        db=db,
    )


def g03(variant: str = "") -> Fixture:
    """Sol first text @300: the hold cap (max_hold_ms 400) commits at 700; S1 arrives after_commit @930."""
    intent = CODING if variant else FACTUAL
    level = HARD if variant else MODERATE
    sol = stream(SOL, first_event_ms=100, first_text_ms=300, chunks=CHUNKS, step_ms=150)
    doc = document(f"G03{variant}", [sol], router_rules(intent=intent, level=level))
    d = decide(doc)
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    held = se.Routing("sol-fast", "sticky", None, served_spec_differs=None, reason="holdback_deadline")
    t.add(
        700,
        se.route_confirmed("att_1", held),
        "attempt_id",
        "routing.selection",
        "routing.decision_id",
        "routing.matched_bindings",
        "routing.served_spec_differs",
    )
    t.add(700, se.committed("att_1", "holdback_deadline"), "attempt_id", "reason")
    late = [
        se.Ev(930, se.decision_completed(d.s1_result, "after_commit"), ("arrived", "decision.status")),
        se.Ev(1000, se.decision_updated(d.final_result, d.changed), ("changed",)),
    ]
    end = 700.0
    for seq, text in enumerate(CHUNKS):
        at = max(700.0, 300 + seq * 150)
        while late and late[0].at < at:
            t.events.append(late.pop(0))
        t.add(at, se.delta("att_1", seq, text), "attempt_id", "seq")
        end = at
    _finish(t, "att_1", end, "".join(CHUNKS), [d], 700, decision_ms=930, lead=-630, holdback=400)
    differs = bool(variant)
    db = {
        "turn": {"routing.served_spec_differs": differs, "routing.decision_id": "dec_1"},
        "session": {"sticky_spec": STICKY_OPUS if differs else STICKY_SOL},
    }
    title = "holdback_deadline, then after_commit" + (" (decided spec differs: no restart)" if variant else "")
    return fixture(
        f"G03{variant}",
        title,
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL}},
        db=db,
    )


def g04(variant: str = "") -> Fixture:
    """Sticky opus-deep; Haiku silent: every answer closes @1000 with DeepSeek + Luna (S1 final); Opus reasons
    from 400 to its first text @2400. G04b: S1 never resolves by itself; the turn cancels it at W = 1050."""
    opus = stream(OPUS, first_event_ms=400, first_text_ms=2400, chunks=CHUNKS, reasoning=100)
    doc = document(f"G04{variant}", [opus], router_rules(intent=CODING, level=HARD, haiku_silent=True))
    hold = variant == "b"
    d = decide(doc, hold=hold, cancel_at_ms=1050 if hold else None)
    t = Timeline()
    prefix(t, "opus-deep", "opus-5.5", "sticky")
    t.add(
        d.s1_ms,
        se.decision_completed(d.s1_result, "during_holdback"),
        "arrived",
        "decision.status",
        note="the closed snapshot of drun.cancel() at W" if hold else None,
    )
    confirmed = se.Routing(
        "opus-deep",
        "rule",
        "dec_1",
        [B0, B2],
        [basis_intent(d)],
        OPUS_PARAMS,
        served_spec_differs=False,
        reason="bindings[0]: intent coding; bindings[2]: hard",
    )
    t.add(
        2400, se.route_confirmed("att_1", confirmed), "attempt_id", "routing.selection", "routing.served_spec_differs"
    )
    t.add(2400, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    end = t.deltas("att_1", 2400, 2400)
    _finish(t, "att_1", end, "".join(CHUNKS), [d], 2400, decision_ms=d.s1_ms, lead=2400 - d.s1_ms, holdback=0)
    setup: dict[str, Any] = {"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_OPUS}}
    if hold:
        setup["decision_engine"] = {"hold_until_cancel": True, "cancel_at_ms": 1050}
    title = "reasoning route, wait to the decision deadline" + (" (S1 never resolves; cancel at W)" if hold else "")
    return fixture(f"G04{variant}", title, doc, t, setup=setup, db={"turn": {"status": "completed"}})


def g05() -> Fixture:
    """Sticky opus-deep; Opus 503 @150 (no event): fallback to Sol (high), first text @1400."""
    doc = document(
        "G05",
        [
            {
                "match": {"model": OPUS, "op": "converse-stream"},
                "fault": {"kind": "http_5xx", "status": 503},
                "latency_ms": 150,
            },
            stream(SOL, first_event_ms=200, first_text_ms=1250, chunks=CHUNKS),
        ],
        router_rules(intent=CODING, level=HARD),
    )
    d = decide(doc)
    t = Timeline()
    prefix(t, "opus-deep", "opus-5.5", "sticky")
    t.add(150, se.cancelled("att_1", "failed"), "attempt_id", "reason")
    t.add(150, se.started("att_2", "opus-deep", "sol-6.1", True), "attempt_id", "route_id", "model", "speculative")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.status")
    t.add(1000, se.decision_updated(d.final_result, d.changed), "changed")
    confirmed = se.Routing(
        "opus-deep",
        "rule",
        "dec_1",
        [B0, B2],
        [basis_intent(d)],
        OPUS_PARAMS,
        served_spec_differs=False,
        reason="bindings[0]: intent coding; bindings[2]: hard",
    )
    t.add(
        1400,
        se.route_confirmed("att_2", confirmed),
        "attempt_id",
        "routing.route_id",
        "routing.selection",
        "routing.served_spec_differs",
    )
    t.add(1400, se.committed("att_2", "spec_agrees"), "attempt_id", "reason")
    end = t.deltas("att_2", 1400, 1400)
    _finish(t, "att_2", end, "".join(CHUNKS), [d], 1400, decision_ms=930, lead=1400 - 930, holdback=0)
    db = {
        "attempts": [
            {"id": "att_1", "outcome": "failed", "line": "waste", "usage.tokens_estimated": True},
            {"id": "att_2", "outcome": "committed", "line": "generation"},
        ]
    }
    return fixture(
        "G05",
        "pre-commit failure -> fallback",
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_OPUS}},
        db=db,
    )


def g05b() -> Fixture:
    """As G05, but Sol fails too (@300), then the default profile sol-fast (@450): the budget of 2 is spent."""
    fail = {"kind": "http_5xx", "status": 503}
    gen = [
        {"match": {"model": OPUS, "op": "converse-stream"}, "fault": fail, "latency_ms": 150},
        {"match": {"model": SOL, "op": "converse-stream"}, "fault": fail, "latency_ms": 150},
    ]
    doc = document("G05b", gen, router_rules(intent=CODING, level=HARD))
    d = decide(doc)
    t = Timeline()
    prefix(t, "opus-deep", "opus-5.5", "sticky")
    t.add(150, se.cancelled("att_1", "failed"), "attempt_id", "reason")
    t.add(150, se.started("att_2", "opus-deep", "sol-6.1", True), "attempt_id", "route_id", "model")
    t.add(300, se.cancelled("att_2", "failed"), "attempt_id", "reason")
    t.add(300, se.started("att_3", "sol-fast", "sol-6.1", True), "attempt_id", "route_id", "model")
    t.add(450, se.cancelled("att_3", "failed"), "attempt_id", "reason")
    t.add(450, se.error(ErrorCode.ALL_ROUTES_UNAVAILABLE, "No route could answer."), "code", "retryable")
    t.add(930, se.decision_completed(d.s1_result, "before_generation"), "arrived", "decision.status")
    t.add(1000, se.decision_updated(d.final_result, d.changed), "changed")
    _finish(t, "att_3", 1000, "", [d], None, decision_ms=930, lead=None, holdback=None, completed=False)
    db = {
        "turn": {"status": "failed", "routing.selection": "fallback", "routing.served_spec_differs": True},
        "attempts": [{"id": a, "outcome": "failed"} for a in ("att_1", "att_2", "att_3")],
    }
    return fixture(
        "G05b",
        "pre-commit failures exhaust the budget",
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_OPUS}},
        db=db,
    )


def g06() -> Fixture:
    """First turn pinned to race-sol-glm: Sol (first text @720) beats GLM (@1200)."""
    doc = document(
        "G06",
        [
            stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS),
            stream(GLM, first_event_ms=300, first_text_ms=1200, chunks=["GLM answer."]),
        ],
        router_rules(),
    )
    d = decide(doc)
    t = Timeline()
    t.add(T_IN + 1, se.turn_created("ses_1"), "session_id")
    t.add(0, se.preflight(se.tokens(USER_TEXT)), "modalities")
    pinned = se.Routing("race-sol-glm", "pinned", reason="request pin")
    t.add(
        0,
        se.route_selected("att_1", pinned),
        "attempt_id",
        "speculative",
        "routing.route_id",
        "routing.mode",
        "routing.selection",
    )
    t.add(0, se.started("att_1", "race-sol-glm", "sol-6.1", True), "attempt_id", "model")
    t.add(0, se.started("att_2", "race-sol-glm", "glm-5.3", True), "attempt_id", "model", "speculative")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.status")
    confirmed = se.Routing(
        "race-sol-glm", "pinned", "dec_1", [B0], [basis_intent(d)], served_spec_differs=False, reason="request pin"
    )
    t.add(
        930,
        se.route_confirmed("att_1", confirmed),
        "attempt_id",
        "routing.selection",
        "routing.matched_bindings",
        "routing.served_spec_differs",
    )
    t.add(930, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    t.add(930, se.cancelled("att_2", "lost_race"), "attempt_id", "reason")
    upd = se.Ev(1000, se.decision_updated(d.final_result, d.changed), ("changed",))
    end = t.deltas("att_1", 720, 930, insert=[upd])
    _finish(t, "att_1", end, "".join(CHUNKS), [d], 930, decision_ms=930, lead=-210, holdback=210, wasted_exact=False)
    return fixture(
        "G06",
        "race",
        doc,
        t,
        req=request(generation={"profile": "race-sol-glm"}),
        setup={"session": {"id": "ses_1", "last_seq": 0, "sticky_spec": None}},
        db={
            "attempts": [
                {"id": "att_1", "outcome": "committed"},
                {"id": "att_2", "outcome": "cancelled", "cancel_reason": "lost_race"},
            ]
        },
    )


def g07() -> Fixture:
    """G01, then POST /v1/turns/turn_1/cancel @950 (after the commit, before the final snapshot)."""
    doc = document("G07", [stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS)], router_rules())
    d = decide(doc, cancel_at_ms=950)
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.status")
    confirmed = se.Routing(
        "sol-fast",
        "rule",
        "dec_1",
        [B0],
        [basis_intent(d)],
        served_spec_differs=False,
        reason="bindings[0]: intent factual_qa",
    )
    t.add(930, se.route_confirmed("att_1", confirmed), "attempt_id", "routing.selection")
    t.add(930, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    t.deltas("att_1", 720, 930, until=950)
    t.add(950, se.cancelled("att_1", "client_abort"), "attempt_id", "reason")
    t.add(
        950,
        se.decision_updated(d.final_result, d.changed),
        "changed",
        "decision.status",
        note="after drun.cancel(): Haiku late",
    )
    sent = "".join(CHUNKS[:3])
    total = se.add_usage(gen_usage(sent, estimated=True), d.final_result["usage"])
    tm = timings(first_delta=930, decision_ms=930, lead=-210, holdback=210, end=950)
    t.add(950, se.turn_completed(total, **tm), "turn_id")
    setup = {
        "session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL},
        "cancel": {
            "at_ms": 950,
            "request": "POST /v1/turns/turn_1/cancel",
            "response": {"status": 202, "body": {"turn_id": "turn_1", "status": "cancelling"}},
            "repeat_response": {"status": 202, "body": {"turn_id": "turn_1", "status": "cancelled"}},
        },
    }
    db = {
        "turn": {"status": "cancelled", "output_text": sent},
        "attempts": [{"id": "att_1", "outcome": "committed", "finish_reason": "cancelled"}],
        "absent_events": ["generation.completed", "turn.verified"],
    }
    return fixture("G07", "explicit cancel mid-stream", doc, t, setup=setup, db=db)


def g08() -> Fixture:
    """G01, but Haiku is late on injection_suspected (P(true) 0.35): bindings[4] matches at the final snapshot,
    so verification runs after generation.completed."""
    doc = document(
        "G08",
        [stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS)],
        verifier_rules(),
        router_rules(haiku_late=INJECTION, injection={"true": 0.35, "false": 0.65}),
    )
    d = decide(doc)
    answer = "".join(CHUNKS)
    v = decide(doc, purpose="verification", text=f"{USER_TEXT}\n{answer}", decision_id="dec_2")
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.status")
    confirmed = se.Routing(
        "sol-fast",
        "rule",
        "dec_1",
        [B0],
        [basis_intent(d)],
        served_spec_differs=False,
        reason="bindings[0]: intent factual_qa",
    )
    t.add(930, se.route_confirmed("att_1", confirmed), "attempt_id", "routing.matched_bindings")
    t.add(930, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    upd = se.Ev(1000, se.decision_updated(d.final_result, d.changed), ("changed",))
    end = t.deltas("att_1", 720, 930, insert=[upd])
    gen = gen_usage(answer)
    t.add(end, se.completed("att_1", "stop", gen), "attempt_id", "finish_reason")
    verified_at = end + v.final_ms
    t.add(verified_at, se.turn_verified(v.final_result), "verification.id", "verification.status")
    total = se.add_usage(gen, d.final_result["usage"], v.final_result["usage"])
    tm = timings(first_delta=930, decision_ms=930, lead=-210, holdback=210, end=verified_at)
    t.add(verified_at, se.turn_completed(total, **tm), "turn_id")
    db = {"turn": {"routing.matched_bindings": [B0, B4], "verification_decision_id": "dec_2"}}
    notes = ["spotlight: strict has no later attempt", "disable_tools is a no-op"]
    return fixture(
        "G08",
        "verification",
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL}},
        db=db,
        notes=notes,
    )


def g09() -> Fixture:
    """Every decision member call fails (503 @50): S1 is final `partial`; decided = spec0, so A1 commits."""
    doc = document(
        "G09", [stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS)], router_rules(all_down=True)
    )
    d = decide(doc)
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    t.add(50, se.decision_completed(d.s1_result, "before_generation"), "arrived", "decision.status")
    confirmed = se.Routing(
        "sol-fast", "sticky", "dec_1", served_spec_differs=False, reason="decision unavailable: all_members_failed"
    )
    t.add(
        720,
        se.route_confirmed("att_1", confirmed),
        "attempt_id",
        "routing.selection",
        "routing.decision_id",
        "routing.matched_bindings",
        "routing.basis",
        "routing.served_spec_differs",
        "routing.reason",
    )
    t.add(720, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    end = t.deltas("att_1", 720, 720)
    _finish(t, "att_1", end, "".join(CHUNKS), [d], 720, decision_ms=50, lead=720 - 50, holdback=0)
    return fixture(
        "G09",
        "all decision members down",
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL}},
        db={"absent_events": ["decision.updated"]},
    )


def g10(variant: str = "") -> Fixture:
    """G01; the provider stalls after delta seq 3 (sol-fast idle 30 s) -> error{upstream_stalled}. G10b: a stream
    exception event after delta seq 3 -> error{all_routes_unavailable}."""
    fault = (
        {"kind": "exception_event", "type": "modelStreamErrorException", "after": 4}
        if variant
        else {"kind": "stall_after", "n": 4}
    )
    doc = document(
        f"G10{variant}",
        [stream(SOL, first_event_ms=200, first_text_ms=720, chunks=CHUNKS, fault=fault)],
        router_rules(),
    )
    d = decide(doc)
    t = Timeline()
    prefix(t, "sol-fast", "sol-6.1", "sticky")
    t.add(930, se.decision_completed(d.s1_result, "during_holdback"), "arrived", "decision.status")
    confirmed = se.Routing(
        "sol-fast",
        "rule",
        "dec_1",
        [B0],
        [basis_intent(d)],
        served_spec_differs=False,
        reason="bindings[0]: intent factual_qa",
    )
    t.add(930, se.route_confirmed("att_1", confirmed), "attempt_id", "routing.selection")
    t.add(930, se.committed("att_1", "spec_agrees"), "attempt_id", "reason")
    upd = se.Ev(1000, se.decision_updated(d.final_result, d.changed), ("changed",))
    last = t.deltas("att_1", 720, 930, CHUNKS[:4], insert=[upd])
    code = ErrorCode.ALL_ROUTES_UNAVAILABLE if variant else ErrorCode.UPSTREAM_STALLED
    at = last if variant else last + 30_000
    t.add(at, se.error(code, "The answer stream failed."), "code", "retryable")
    sent = "".join(CHUNKS[:4])
    total = se.add_usage(gen_usage(sent, estimated=True), d.final_result["usage"])
    t.add(
        at,
        se.turn_completed(total, **timings(first_delta=930, decision_ms=930, lead=-210, holdback=210, end=at)),
        "turn_id",
    )
    db = {
        "turn": {"status": "failed", "output_text": sent},
        "attempts": [{"id": "att_1", "outcome": "committed", "error_code": code.value}],
    }
    title = "error after commit" + (" (stream exception event)" if variant else "")
    return fixture(
        f"G10{variant}",
        title,
        doc,
        t,
        setup={"session": {"id": "ses_1", "last_seq": 3, "sticky_spec": STICKY_SOL}},
        db=db,
    )


def g11() -> Fixture:
    db = {
        "rows": {"turn": 0, "attempt": 0, "decision": 0},
        "session": {"last_seq": 3, "sticky_spec": STICKY_SOL},
        "usage_lines": "recorded with turn_id turn_1",
        "labeling_offer": False,
        "get_turn_status": 404,
        "session_queue_entry": False,
    }
    return g01("G11", "store: false", req=request(store=False)) | {"db": db}


def g12(variant: str = "") -> Fixture:
    """R1 = G01 with Idempotency-Key k1; duplicates attach to the same stream (IMPL-077)."""
    replays = [
        {
            "name": "R2",
            "at_ms": 800,
            "headers": {"Idempotency-Key": "k1"},
            "body": "same",
            **({"process": "other"} if variant else {}),
            "expect": {
                "status": 200,
                "content_type": "text/event-stream",
                "headers": {"x-aghoo-idempotent-replay": "true"},
                "frames": "R1 from id 1 to turn.completed",
                "new_turn": False,
                "new_mock_requests": 0,
                "new_usage_lines": 0,
            },
        },
        {
            "name": "R3",
            "after": "turn.completed, inside resume_window_s",
            "headers": {"Idempotency-Key": "k1"},
            "body": "same",
            "expect": {"status": 200, "frames": "R1, then close"},
        },
        {
            "name": "R4",
            "after": "resume_window_s",
            "headers": {"Idempotency-Key": "k1"},
            "body": "same",
            "expect": {"status": 410, "code": "resume_window_expired", "details": {"turn_id": "turn_1"}},
        },
        {
            "name": "R5",
            "headers": {"Idempotency-Key": "k1"},
            "body": "other",
            "expect": {"status": 409, "code": "idempotency_key_reused"},
        },
    ]
    req = request()
    req["headers"]["Idempotency-Key"] = "k1"
    title = "idempotent duplicate attach" + (" (other API process: attach_remote)" if variant else "")
    return g01(f"G12{variant}", title, req=req) | {"replays": replays}


BUILDERS: Final[tuple[Callable[[], Fixture], ...]] = (
    g01,
    g02,
    g03,
    lambda: g03("b"),
    g04,
    lambda: g04("b"),
    g05,
    g05b,
    g06,
    g07,
    g08,
    g09,
    g10,
    lambda: g10("b"),
    g11,
    g12,
    lambda: g12("b"),
)


def build_all() -> list[Fixture]:
    """Every fixture, in id order."""
    return [b() for b in BUILDERS]
