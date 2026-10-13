"""Escalation (spec-06 §9, §17 T-ESC-1…3, T-CLS-1 refusal trigger; IMPL-005, IMPL-025, IMPL-061, IMPL-068)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from tests.contract_internal.kits import principal
from tests.unit.decision.checks import assert_jev, assert_valid, wire_request
from tests.unit.decision.helpers import DEPARTMENT, FixedCost
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, engine_world, tel

import noema.contracts
from noema.core.protocols.decision import DecisionRequest, Msg, ProfileRef
from noema.core.protocols.media import ContentPart
from noema.decision.jev import jev_response

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
OPTS = ["billing", "technical", "shipping", "other"]


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def rule(
    model: str, contains: str, dist: Mapping[str, float], ms: float, *, nth: int | None = None, **extra: Any
) -> dict[str, Any]:
    match: dict[str, Any] = {"model": model, "prompt_contains": contains}
    if nth is not None:
        match["nth"] = nth
    return {"match": match, "answer": {"dist": dict(dist)}, "latency_ms": ms, **extra}


def luna_stage0() -> list[dict[str, Any]]:
    dept = {"billing": 0.48, "technical": 0.02, "shipping": 0.48, "other": 0.02}
    return [
        rule(LUNA, "Which team", dept, 700, nth=1),
        rule(LUNA, "How urgent", {"low": 0.02, "medium": 0.88, "high": 0.1}, 700),
        rule(LUNA, "speak with a human", {"true": 0.01, "false": 0.99}, 700),
    ]


def ensemble_department() -> list[dict[str, Any]]:
    confident = {"billing": 0.9, "technical": 0.02, "shipping": 0.06, "other": 0.02}
    return [rule(m, "Which team", confident, 400) for m in (DEEPSEEK, LUNA, HAIKU)]


async def test_t_esc_1_lightweight_then_router_default() -> None:
    ew = engine_world()
    ew.use(luna_stage0() + ensemble_department())
    jev_req = example("decision-request.valid-jev.json")
    req = ew.request(
        jev_req["questions"], profile="lightweight-model-decide", value=jev_req["input"], escalation="profile_default"
    )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: Any = snap.result
    (stage,) = result["provenance"]["stages"]
    assert (stage["profile_id"], stage["profile_version"]) == ("router-default", "1")
    assert (stage["triggered_by"], stage["questions"]) == (["margin_below"], ["department"])
    assert stage["prompt_templates"] == {"m1": "logprob-codes@2", "m2": "verbalized-dist@1", "m3": "verbalized-dist@1"}
    dept, urgency, human = result["answers"]
    assert [m["member_id"] for m in dept["members"]] == ["m1", "m2", "m3"] and dept["choice"] == "billing"
    assert len(urgency["members"]) == 1 and len(human["members"]) == 1
    assert_valid(result, wire_request(req, jev_req["input"]))
    assert tel(ew).counter("aghoo_escalations_total", trigger="margin_below", outcome="ran") == 1


async def test_t_esc_1_escalation_off_gives_the_jev_example() -> None:
    ew = engine_world()
    ew.use(luna_stage0())
    jev_req = example("decision-request.valid-jev.json")
    req = ew.request(
        jev_req["questions"], profile="lightweight-model-decide", value=jev_req["input"], output_format="jev"
    )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    jev = jev_response(snap.result, req.questions.questions, model="aghoo/lightweight-model-decide@1")
    expected = example("decision-jev-response.valid.json")
    assert jev["model"] == expected["model"] and jev["answers"] == expected["answers"]
    assert jev["decision_id"] == snap.decision_id
    assert_jev(jev, jev_req)


async def _image_input(ew: Any) -> tuple[Msg, ...]:
    async def body() -> AsyncIterator[bytes]:
        yield b"\x89PNG fake screenshot bytes"

    ref = await ew.world.files.upload(
        principal(), body(), declared_type="image/png", filename="shot.png", session_id=None
    )
    return (
        Msg(
            "user",
            (
                ContentPart(0, "text", None, text="Which team handles this?"),
                ContentPart(1, "image", "image-1", file=ref),
            ),
        ),
    )


async def test_t_esc_2_escalated_router_plus_reproduces_the_example_answer() -> None:
    cost = FixedCost()
    ew = engine_world(cost=cost)
    flat = {"billing": 0.3, "technical": 0.25, "shipping": 0.25, "other": 0.2}
    plus = {
        DEEPSEEK: ((0.55, 0.05, 0.30, 0.10), 488, (60, 2)),
        LUNA: ((0.70, 0.02, 0.25, 0.03), 883, (120, 29)),
        HAIKU: ((0.62, 0.03, 0.30, 0.05), 938, (130, 35)),
    }
    rules = [rule(m, "Which team", flat, 200, nth=1) for m in plus]
    for model, (dist, ms, (i, o)) in plus.items():
        rules.append(
            rule(
                model,
                "Which team",
                dict(zip(OPTS, dist, strict=True)),
                ms,
                stream={"usage": {"input_tokens": i, "output_tokens": o}},
            )
        )
    ew.use(rules)
    req_json = example("decision-request.valid-image.json")
    value = await _image_input(ew)
    p = ew.snapshot.decision_profile("router-default")
    qs = ew.compiler.compile(p, req_json["questions"], origin="native", output_format="native")
    req = DecisionRequest(
        ProfileRef("router-default", 1),
        qs,
        value,
        1000,
        2,
        "profile_default",
        frozenset({"members", "stages"}),
        "native",
        "native",
    )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: Any = snap.result
    expected = example("decision-result.valid-escalated.json")
    assert result["answers"] == expected["answers"]
    (stage,) = result["provenance"]["stages"]
    exp_stage = expected["provenance"]["stages"][0]
    assert {k: v for k, v in stage.items() if k != "usage"} == {k: v for k, v in exp_stage.items() if k != "usage"}
    assert result["provenance"]["input_view_signature"] == expected["provenance"]["input_view_signature"]
    assert_valid(result, req_json)


async def test_t_esc_3_turn_budget_skips_escalation() -> None:
    ew = engine_world()
    weak = {
        "factual_qa": 0.15,
        "chitchat": 0.12,
        "coding": 0.12,
        "billing": 0.11,
        "shipping": 0.1,
        "math_reasoning": 0.1,
        "document_qa": 0.1,
        "image_understanding": 0.1,
        "technical_support": 0.05,
        "other": 0.05,
    }
    low = {"true": 0.05, "false": 0.95}
    ew.use(
        [rule(m, "primary intent", weak, 900) for m in (DEEPSEEK, LUNA, HAIKU)]
        + [{"match": {}, "answer": {"dist": {**low, "trivial": 0.1, "moderate": 0.8, "hard": 0.1}}, "latency_ms": 900}]
    )
    router = ew.snapshot.decision_profile("router-default")
    qs = ew.compiler.compile(router, [], origin="turn", output_format="native", platform_from=router)
    req = DecisionRequest(
        ProfileRef("router-default", 1), qs, "Hi", 1000, 2, "profile_default", frozenset({"members"}), "turn", "native"
    )
    ctx = ew.context(turn_id="turn_1", escalate_until_s=1.05)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ctx, req)
    result: Any = snap.result
    assert result["provenance"]["stages"] == [] and snap.answers["intent"].final
    assert result["answers"][0]["confidence"]["value"] < 0.25
    assert tel(ew).counter("aghoo_escalations_total", outcome="skipped_budget") == 1


async def test_escalate_slack_ms_counts_the_budget_from_the_engine_t0() -> None:
    """Orchestrator request 10 (spec-04 §3, IMPL-023): a turn passes a conservative `escalate_until` (W measured
    before the entry) plus `escalate_slack_ms`; the engine then uses `t0 + deadline_ms + slack_ms` instead."""
    stages: dict[int | None, int] = {}
    for slack_ms in (None, 60_000):
        ew = engine_world()
        refusal = {"match": {"model": LUNA, "nth": 1}, "fault": {"kind": "refusal"}, "latency_ms": 100}
        ew.use([refusal, *ensemble_department()])
        req = ew.request([DEPARTMENT], profile="lightweight-model-decide", escalation="profile_default")
        ctx = replace(ew.context(escalate_until_s=0.001), escalate_slack_ms=slack_ms)
        async with ew.clock.autojump():
            snap = await ew.engine.decide(ctx, req)
        result: Any = snap.result
        stages[slack_ms] = len(result["provenance"]["stages"])
    assert stages == {None: 0, 60_000: 1}  # the 1 ms limit skips the stage; the derived limit fits it


async def test_t_cls_1_a_single_refusal_fires_any_refused() -> None:
    ew = engine_world()
    ew.use(
        [{"match": {"model": LUNA, "nth": 1}, "fault": {"kind": "refusal"}, "latency_ms": 100}, *ensemble_department()]
    )
    req = ew.request([DEPARTMENT], profile="lightweight-model-decide", escalation="profile_default")
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: Any = snap.result
    assert result["provenance"]["stages"][0]["triggered_by"] == ["refused"]
    assert result["answers"][0]["type"] == "choice"  # the final answer is the last stage's (IMPL-061)
    assert_valid(result, wire_request(req))


async def test_cancel_during_an_escalation_stage() -> None:
    ew = engine_world()
    hang = {"kind": "hang_before_first_byte"}
    ew.use(
        [
            rule(LUNA, "Which team", {"billing": 0.48, "technical": 0.02, "shipping": 0.48, "other": 0.02}, 300, nth=1),
            {"match": {}, "fault": hang, "latency_ms": 0},
        ]
    )
    req = ew.request([DEPARTMENT], profile="lightweight-model-decide", escalation="profile_default")
    async with ew.engine.run(ew.context(), req) as run:
        await ew.clock.advance(0.5)  # stage 0 closed at 300 ms; router-default members hang
        await run.cancel()
        final = await run.finalize()
    result: Any = final.result
    (stage,) = result["provenance"]["stages"]
    assert stage["profile_id"] == "router-default"
    answer = result["answers"][0]
    assert (answer["type"], answer["reason"]) == ("unavailable", "no_quorum")
    assert {m["status"] for m in answer["members"]} == {"late"}
    assert all(s.ended for s in tel(ew).spans)
    assert_valid(result, wire_request(req))
