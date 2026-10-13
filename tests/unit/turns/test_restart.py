"""The invisible restart (spec-04 §3, §5.2; IMPL-014, IMPL-086; SEC-INJ-04 machinery): a conditioning block forces a
restart whose A2 carries it as the last user block; `restart_on_conditioning_change: false` commits A1 without it;
`spotlight: strict` final at S1 datamarks the restarted attempt's user text, not A1's."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes.config import patched_example, snapshot_from
from tests.fakes.world import build_world
from tests.golden.sse_scenarios import CODING, HARD, document, router_rules, stream

from noema.core.protocols.adapters import ModelRequest
from noema.core.values import TextBlock

from .harness import STICKY_SOL, TurnHarness, first, make_harness, run_turn, running, types_of

pytestmark = pytest.mark.unit

SOL, OPUS = "us.openai.gpt-6.1-sol", "us.anthropic.claude-opus-5-5"
LOW_INTENT = {"shipping": 0.45, "billing": 0.35, "other": 0.2}
HIGH_INJECTION = {"true": 0.6, "false": 0.4}


def _streams() -> list[dict[str, Any]]:
    return [
        stream(SOL, first_event_ms=100, first_text_ms=720, chunks=["one ", "two"]),
        stream(OPUS, first_event_ms=100, first_text_ms=400, chunks=["deep"]),
    ]


def _generation_requests(h: TurnHarness) -> list[ModelRequest]:
    adapter = h.world.adapters.adapters["bedrock-converse"]
    return [c.request for c in adapter.calls if c.op == "converse-stream"]


def _harness(rules: list[dict[str, Any]], *, restart_on_conditioning: bool = True) -> TurnHarness:
    scenario = document("r", _streams(), rules)
    if restart_on_conditioning:
        h = make_harness(scenario=scenario)
    else:
        from tests.fakes.config import FakeConfigRegistry

        patch = {"routing": {"speculation": {"restart_on_conditioning_change": False}}}
        config = FakeConfigRegistry(snapshot_from(patched_example(patch)))
        h = make_harness(world=build_world(config=config, scenario=scenario))
    h.seed_session("ses_1", last_seq=1, sticky_spec=STICKY_SOL)
    return h


async def test_conditioning_block_restarts_and_rides_last_in_a2() -> None:
    h = _harness(router_rules(intent=LOW_INTENT))
    async with running(h):
        runner = await run_turn(h)
    assert first(h, runner, "generation.cancelled")[1]["reason"] == "spec_changed"
    _, confirmed = first(h, runner, "route.confirmed")
    cond = confirmed["routing"]["conditioning"]
    assert cond["answer_mode"] == "cover_top2_and_clarify" and cond["signature"].startswith("sha256:")
    a1, a2 = _generation_requests(h)
    last_a1, last_a2 = a1.messages[-1].content[-1], a2.messages[-1].content[-1]
    assert isinstance(last_a2, TextBlock) and last_a2.text.startswith('<decision_context origin="platform" v="1">')
    assert isinstance(last_a1, TextBlock) and "<decision_context" not in last_a1.text  # A1 never carries it
    assert a1.system == a2.system
    sticky = h.world.db.store.rows("session")[0]["sticky_spec"]
    assert sticky["answer_mode"] == "cover_top2_and_clarify"


async def test_without_restart_on_conditioning_change_a1_commits_without_the_block() -> None:
    h = _harness(router_rules(intent=LOW_INTENT), restart_on_conditioning=False)
    async with running(h):
        runner = await run_turn(h)
    assert "generation.cancelled" not in types_of(h, runner)
    _, confirmed = first(h, runner, "route.confirmed")
    assert confirmed["routing"]["conditioning"] is None and confirmed["routing"]["served_spec_differs"] is False
    (a1,) = _generation_requests(h)
    assert all("<decision_context" not in b.text for m in a1.messages for b in m.content if isinstance(b, TextBlock))


async def test_strict_spotlight_applies_to_the_restarted_attempt_only() -> None:
    h = _harness(router_rules(intent=CODING, level=HARD, injection=HIGH_INJECTION, haiku_late=None))
    async with running(h):
        runner = await run_turn(h)
    assert runner.prompt is not None and runner.prompt.strict
    a1, a2 = _generation_requests(h)
    text_a1 = a1.messages[-1].content[0]
    text_a2 = a2.messages[-1].content[0]
    assert isinstance(text_a1, TextBlock) and isinstance(text_a2, TextBlock)
    assert text_a1.text == "What is the capital of France?"
    assert text_a2.text == "WhatˆisˆtheˆcapitalˆofˆFrance?"
    assert a1.system == a2.system
    assert "turn.verified" in types_of(h, runner)  # bindings[4] also sets verify_answer
