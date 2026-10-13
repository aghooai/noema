"""Scenario documents (spec-10 §2.4) of the golden SSE fixtures and the decision results they produce.

Rules are ordered: generation streams first (op `converse-stream`), then the verifier questions, then the router
questions. Router timing D-std (spec-04 §4.2): DeepSeek and Luna answer every question by 900 ms, Haiku all but
one by 930 ms. `decide()` runs the fake decision engine on a scenario under a `FakeClock`, so the fixtures carry
exactly the results a turn sees (S1 at commit-critical closure, the final snapshot), with normalized ids.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import anyio
from tests.fakes.clock import FakeClock
from tests.fakes.ops_admission import CountingReservation
from tests.fakes.world import build_world

from noema.core.clock import Deadline
from noema.core.principal import ALL_SCOPES, Principal
from noema.core.protocols.decision import DecisionContext, DecisionRequest, DecisionSnapshot, ProfileRef, Purpose

HAIKU: Final = "us.anthropic.claude-haiku-5-5"
INTENT, COMPLEXITY = "primary intent", "How hard"
SENSITIVE, INJECTION = "sensitive personal data", "instruct the AI system"
VERIFY_ANSWERS, VERIFY_CLAIMS = "directly addresses", "not supported by the conversation"
FACTUAL: Final = {"factual_qa": 0.9, "chitchat": 0.05, "coding": 0.05}  # top 0.90, margin 0.85
CODING: Final = {"coding": 0.85, "factual_qa": 0.15}  # top 0.85, margin 0.70
MODERATE: Final = {"trivial": 0.1, "moderate": 0.8, "hard": 0.1}
HARD: Final = {"moderate": 0.1, "hard": 0.8, "expert": 0.1}
LOW: Final = {"true": 0.05, "false": 0.95}
USER_TEXT: Final = "What is the capital of France?"
TRACE_ID: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
Rule = dict[str, Any]


def answer(match: Mapping[str, Any], dist: Mapping[str, float], ms: int) -> Rule:
    return {"match": dict(match), "answer": {"dist": dict(dist)}, "latency_ms": ms}


def router_rules(
    *,
    intent: Mapping[str, float] = FACTUAL,
    level: Mapping[str, float] = MODERATE,
    haiku_late: str | None = SENSITIVE,
    injection: Mapping[str, float] = LOW,
    haiku_silent: bool = False,
    all_down: bool = False,
) -> list[Rule]:
    """The router decision (5 questions of router-default)."""
    if all_down:
        return [{"match": {}, "fault": {"kind": "http_5xx", "status": 503}, "latency_ms": 50}]
    rules: list[Rule] = []
    if haiku_silent:
        rules.append({"match": {"model": HAIKU}, "fault": {"kind": "hang_before_first_byte"}})
    elif haiku_late is not None:
        rules.append(
            {"match": {"model": HAIKU, "prompt_contains": haiku_late}, "fault": {"kind": "hang_before_first_byte"}}
        )
    for text, dist in ((INTENT, intent), (COMPLEXITY, level), (INJECTION, injection), ("", LOW)):
        match = {"prompt_contains": text} if text else {}
        rules.append(answer({"model": HAIKU, **match}, dist, 930))
        rules.append(answer(match, dist, 900))
    return rules


def verifier_rules() -> list[Rule]:
    """answer-verifier (Haiku 800 ms, Luna 700 ms): the answer addresses the question, no unsupported claims."""
    yes, no = {"true": 0.9, "false": 0.1}, {"true": 0.1, "false": 0.9}
    return [
        answer({"model": HAIKU, "prompt_contains": VERIFY_ANSWERS}, yes, 800),
        answer({"prompt_contains": VERIFY_ANSWERS}, yes, 700),
        answer({"model": HAIKU, "prompt_contains": VERIFY_CLAIMS}, no, 800),
        answer({"prompt_contains": VERIFY_CLAIMS}, no, 700),
    ]


def stream(
    model: str,
    *,
    first_event_ms: float,
    first_text_ms: float,
    chunks: Sequence[str],
    step_ms: float = 100,
    nth: int | None = None,
    fault: Mapping[str, Any] | None = None,
    reasoning: int = 0,
) -> Rule:
    """A generation stream; one chunk every `step_ms` (chunk_tokens 4)."""
    spec: dict[str, Any] = {
        "first_event_ms": first_event_ms,
        "first_text_ms": first_text_ms,
        "chunks": list(chunks),
        "chunk_tokens": 4,
        "tokens_per_s": 4000.0 / step_ms,
    }
    if reasoning:
        spec["reasoning"] = {"deltas": reasoning, "every_ms": 20}
    match: dict[str, Any] = {"model": model, "op": "converse-stream"}
    if nth is not None:
        match["nth"] = nth
    rule: Rule = {"match": match, "stream": spec}
    if fault is not None:
        rule["fault"] = dict(fault)
    return rule


def document(name: str, *rules: Sequence[Rule]) -> dict[str, Any]:
    return {
        "scenario": name,
        "seed": 7,
        "mantle_auth": "bearer_short_term",
        "defaults": {"latency": {"fixed_ms": 0}},
        "rules": [r for group in rules for r in group],
    }


@dataclass(frozen=True)
class DecisionTimeline:
    """S1 and the final snapshot with their times (ms from the decision start) and wire results."""

    s1: DecisionSnapshot
    s1_ms: int
    final: DecisionSnapshot
    final_ms: int
    s1_result: dict[str, Any]
    final_result: dict[str, Any]

    @property
    def changed(self) -> list[str]:
        return sorted(self.final.final_names - self.s1.final_names)


def _principal() -> Principal:
    return Principal("t_acme", "p1", "prn_dev", "key_1", frozenset({"developer"}), ALL_SCOPES, "req_1")


def _normalized(result: Mapping[str, Any], decision_id: str) -> dict[str, Any]:
    return {**result, "id": decision_id}


async def _decide(
    doc: Mapping[str, Any], purpose: str, text: str, hold: bool, cancel_at_ms: float | None, decision_id: str
) -> DecisionTimeline:
    clock = FakeClock()
    world = build_world(clock=clock, scenario=doc, hold_decisions_until_cancel=hold)
    snap = world.config.current()
    if purpose == "verification":
        profile = snap.decision_profile("answer-verifier")
        qs = world.compiler.compile(
            profile,
            [],
            origin="verification",
            output_format="native",
            select=["answers_question", "unsupported_claims"],
        )
        req = DecisionRequest(
            ProfileRef(profile.id, profile.version),
            qs,
            text,
            profile.deadline_ms,
            profile.min_quorum,
            "off",
            frozenset({"members"}),
            "verification",
            "native",
        )
    else:
        profile = snap.decision_profile("router-default")
        qs = world.compiler.compile(profile, [], origin="turn", output_format="native", platform_from=profile)
        req = DecisionRequest(
            ProfileRef(profile.id, profile.version),
            qs,
            text,
            profile.deadline_ms,
            profile.min_quorum,
            "profile_default",
            frozenset({"members"}),
            "turn",
            "native",
        )
    ctx = DecisionContext(
        _principal(),
        snap.tenant_ref("t_acme"),
        snap,
        Deadline(clock.monotonic() + 120, clock),
        None,
        CountingReservation(),
        cast("Purpose", purpose),
        True,
        "internal",
        "full",
        TRACE_ID,
        turn_id="turn_1",
        session_id="ses_1",
    )
    t0 = clock.monotonic()
    async with clock.autojump(), world.decisions.run(ctx, req) as run, anyio.create_task_group() as tg:
        if cancel_at_ms is not None:

            async def cancel_later() -> None:
                await clock.sleep(cancel_at_ms / 1000)
                await run.cancel()

            tg.start_soon(cancel_later)
        s1 = await run.commit_critical()
        s1_ms = round((clock.monotonic() - t0) * 1000)
        final = await run.finalize()
        final_ms = round((clock.monotonic() - t0) * 1000)
    return DecisionTimeline(
        s1, s1_ms, final, final_ms, _normalized(s1.result, decision_id), _normalized(final.result, decision_id)
    )


def decide(
    doc: Mapping[str, Any],
    *,
    purpose: str = "router",
    text: str = USER_TEXT,
    hold: bool = False,
    cancel_at_ms: float | None = None,
    decision_id: str = "dec_1",
) -> DecisionTimeline:
    """Run the fake decision engine on `doc` (synchronously, in its own event loop)."""
    return anyio.run(_decide, doc, purpose, text, hold, cancel_at_ms, decision_id)
