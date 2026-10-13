"""The hierarchical choice method (spec-06 §19.11 T-HIER-1…10; IMPL-027): group formation, expansion, the two
rounds on the engine, member figures, deadlines, skips, surfaces, censored reads, binding and cache key."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.labeling import IdentityTransform
from tests.unit.decision.checks import assert_valid
from tests.unit.decision.helpers import FixedCost, actx, question, stage
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, answer, engine_world, res, tel

import noema.contracts
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import DecisionRequest, OptionSpec, ProfileRef
from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey
from noema.decision.cache import profile_part
from noema.decision.compiler import compile_question
from noema.decision.hier_answer import hier_scored
from noema.decision.hierarchical import expand, form_groups
from noema.decision.signatures import input_view_signature
from noema.decision.state import Slot

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
H_STAGE2_BILLING = [0.55, 0.08, 0.04, 0.15, 0.12, 0.03, 0.02, 0.01]
H_STAGE2_SHIPPING = [0.20, 0.60, 0.03, 0.02, 0.05, 0.01, 0.08, 0.01]


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def opts(*keys: str) -> tuple[OptionSpec, ...]:
    return tuple(OptionSpec(k, k, None, i) for i, k in enumerate(keys))


def hier_rules(round1_ms: float = 700, round2_ms: float = 730) -> list[dict[str, Any]]:
    req = example("decision-request.valid-hierarchical.json")
    values = [c["value"] for c in req["questions"][0]["choices"]]
    billing = dict(zip(values[0:8], H_STAGE2_BILLING, strict=True))
    shipping = dict(zip(values[16:24], H_STAGE2_SHIPPING, strict=True))
    groups = {"billing": 0.4799, "technical": 0.0201, "shipping": 0.4799, "other": 0.0201}
    usage1 = {"stream": {"usage": {"input_tokens": 130, "output_tokens": 30}}}
    usage2 = {"stream": {"usage": {"input_tokens": 150, "output_tokens": 30}}}
    return [
        answer(LUNA, "billing_double_charge, billing_refund_status", groups, round1_ms, **usage1),
        answer(LUNA, None, {**billing, **shipping}, round2_ms, **usage2),
    ]


async def test_t_hier_1_reproduces_the_example_from_its_request() -> None:
    cost = FixedCost()
    ew = engine_world(cost=cost)
    ew.use(hier_rules())
    req_json = example("decision-request.valid-hierarchical.json")
    req = ew.request(
        req_json["questions"],
        profile="lightweight-model-decide",
        value=req_json["input"],
        deadline_ms=3000,
        escalation="profile_default",
    )
    assert req.questions.questions[0].method == "hierarchical"
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: dict[str, Any] = json.loads(json.dumps(snap.result))
    expected = example("decision-result.valid-hierarchical.json")
    for key in ("id", "created_at"):
        result[key] = expected[key]
    injected = ("trace_id", "option_order_seed", "started_at", "completed_at", "input_digest")
    for key in injected:  # the digest depends on the tenant key (vector: test_signatures)
        result["provenance"][key] = expected["provenance"][key]
    assert result == expected
    assert_valid(snap.result, {**req_json, "include": ["members"]})
    assert tel(ew).counter("aghoo_escalations_total", outcome="skipped_too_many_choices") == 1
    assert tel(ew).counter("aghoo_decision_hierarchical_total", outcome="scored") == 1
    assert sorted((ln.line, ln.usage.input_tokens, ln.usage.output_tokens) for ln in cost.lines) == [
        ("decision", 430, 90)
    ]


def test_t_hier_2_server_split_sizes_and_labels() -> None:
    cases = {
        (17, 16): [(1, 9), (10, 17)],
        (40, 16): [(1, 14), (15, 27), (28, 40)],
        (27, 26): [(1, 14), (15, 27)],
    }
    for (k, limit), ranges in cases.items():
        groups = form_groups(opts(*(f"v{i}" for i in range(k))), [None] * k, limit)
        assert [g.label for g in groups] == [f"options {a}-{b}" for a, b in ranges]
        assert [g.id for g in groups] == [f"g{i + 1}" for i in range(len(ranges))]
    g255 = form_groups(opts(*(f"v{i}" for i in range(255))), [None] * 255, 16)
    assert [len(g.options) for g in g255] == [16] * 15 + [15] and g255[-1].label == "options 241-255"
    g255b = form_groups(opts(*(f"v{i}" for i in range(255))), [None] * 255, 26)
    assert [len(g.options) for g in g255b] == [26] * 5 + [25] * 5 and g255b[-1].label == "options 231-255"
    supplied = form_groups(opts("x1", "y1", "x2"), ["a", "b", "a"], 16)
    assert [(g.id, g.label, [o.key for o in g.options], g.description) for g in supplied] == [
        ("g1", "a", ["x1", "x2"], "x1, x2"),
        ("g2", "b", ["y1"], "y1"),
    ]


async def test_t_hier_2_round_one_shows_groups_with_their_option_lists() -> None:
    ew = engine_world()
    ew.use(hier_rules())
    req_json = example("decision-request.valid-hierarchical.json")
    req = ew.request(
        req_json["questions"], profile="lightweight-model-decide", value=req_json["input"], deadline_ms=3000
    )
    async with ew.clock.autojump():
        await ew.engine.decide(ew.context(), req)
    first = ew.world.adapters.calls()[0].request.messages[0].content[0]
    text = getattr(first, "text", "")
    assert (
        '"value": "billing", "description": "billing_double_charge, billing_refund_status, billing_invoice_copy' in text
    )


def test_t_hier_3_expand() -> None:
    """The spec-06 §19.11 table (group sizes 2 unless given), plus R1: `E` must hold >= 2 options in total."""
    rows: list[tuple[tuple[float, ...], tuple[int, ...] | None, list[int], list[str], int]] = [
        ((0.6, 0.25, 0.1, 0.05), None, [6000, 2500, 1000, 500], ["g1", "g2", "g3"], 500),
        ((0.3, 0.3, 0.2, 0.2), None, [3000, 3000, 2000, 2000], ["g1", "g2", "g3"], 2000),
        ((0.9, 0.1), None, [9000, 1000], ["g1"], 1000),
        ((0.45, 0.44996, 0.10004), None, [4500, 4500, 1000], ["g1", "g2"], 1000),
        ((0.5, 0.39994, 0.10006), None, [5000, 3999, 1001], ["g1", "g2", "g3"], 0),
        ((0.95, 0.05), (1, 2), [9500, 500], ["g1", "g2"], 0),  # g1 = {a} alone holds 1 option (R1)
        ((0.03, 0.95, 0.02), (5, 1, 1), [300, 9500, 200], ["g2", "g1"], 200),  # the next group in mass order
        ((0.95, 0.03, 0.02), (1, 1, 5), [9500, 300, 200], ["g1", "g2"], 200),  # two one-option groups suffice
        ((0.9, 0.1), (1, 2), [9000, 1000], ["g1", "g2"], 0),  # exactly 9,000 units, but 1 option
    ]
    for probs, sizes, units, expanded, unresolved in rows:
        gids = [f"g{i + 1}" for i in range(len(probs))]
        size = dict(zip(gids, sizes or (2,) * len(gids), strict=True))
        got, got_units = expand(dict(zip(gids, probs, strict=True)), gids, size)
        assert got == expanded and [got_units[g] for g in gids] == units
        assert sum(got_units[g] for g in gids if g not in got) == unresolved


async def test_t_hier_3_one_option_group_needs_no_round_two_call() -> None:
    """g1 = {a}, g2 = {b1..b26} (K = 27 > 26): both expanded; one round-2 call (for g2); p(a | g1) = 1."""
    ew = engine_world()
    choices = [{"value": "a", "group": "ga"}] + [{"value": f"b{i}", "group": "gb"} for i in range(1, 27)]
    question = {"type": "choice", "name": "pick", "instructions": "Pick one.", "choices": choices}
    rest = {f"b{i}": 0.0 for i in range(3, 27)}
    ew.use(
        [
            answer(LUNA, '"value": "ga"', {"ga": 0.6, "gb": 0.4}, 300),
            answer(LUNA, None, {"b1": 0.75, "b2": 0.25, **rest}, 300),
        ]
    )
    req = ew.request([question], profile="lightweight-model-decide", deadline_ms=3000)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert len(ew.world.adapters.calls()) == 2
    assert [g["expanded"] for g in a["groups"]] == [True, True] and "unresolved_mass" not in a
    by_value = {c["value"]: c["probability"] for c in a["candidates"]}
    assert (by_value["a"], by_value["b1"], by_value["b2"]) == (0.6, 0.3, 0.1)
    assert_valid(snap.result, {"questions": [question], "input": "x"})


async def test_t_hier_3_lone_dominant_one_option_group_expands_the_next_group() -> None:
    """R1 (replaces the C1 `unavailable / no_quorum` path): P(ga) >= 0.9 with ga = {a} expands gb too, so the
    answer is scored with every option of both groups as candidates; one round-2 call (for gb)."""
    ew = engine_world()
    choices = [{"value": "a", "group": "ga"}] + [{"value": f"b{i}", "group": "gb"} for i in range(1, 27)]
    question = {"type": "choice", "name": "pick", "instructions": "Pick one.", "choices": choices}
    rest = {f"b{i}": 0.0 for i in range(3, 27)}
    ew.use(
        [
            answer(LUNA, '"value": "ga"', {"ga": 0.95, "gb": 0.05}, 300),
            answer(LUNA, None, {"b1": 0.6, "b2": 0.4, **rest}, 300),
        ]
    )
    req = ew.request([question], profile="lightweight-model-decide", deadline_ms=3000)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert (snap.status, a["type"], a["choice"]) == ("completed", "choice", "a")
    assert [(g["probability"], g["expanded"]) for g in a["groups"]] == [(0.95, True), (0.05, True)]
    assert "unresolved_mass" not in a and len(a["candidates"]) == 27
    assert [(c["value"], c["probability"]) for c in a["candidates"][:3]] == [("a", 0.95), ("b1", 0.03), ("b2", 0.02)]
    assert len(ew.world.adapters.calls()) == 2
    member = a["members"][0]
    assert member["status"] == "ok" and (member["distribution"]["a"], member["distribution"]["b1"]) == (0.95, 0.03)
    assert tel(ew).counter("aghoo_decision_hierarchical_total", outcome="scored") == 1
    assert_valid(snap.result, {"questions": [question], "input": "x"})


async def test_t_hier_3_reproduces_the_lone_group_example_from_its_request() -> None:
    """`decision-result.valid-hierarchical-lone-group.json` (R1): fraud = {fraud_report} holds 0.93, so billing is
    expanded too (round 2 for billing only); technical, shipping and other stay unexpanded (0.03 unresolved)."""
    cost = FixedCost(per_call="0.00003")  # two calls; the example keeps the T-HIER-1 cost 6e-05
    ew = engine_world(cost=cost)
    req_json = example("decision-request.valid-hierarchical-lone-group.json")
    values = [c["value"] for c in req_json["questions"][0]["choices"]]
    groups = {"fraud": 0.93, "billing": 0.04, "technical": 0.01, "shipping": 0.015, "other": 0.005}
    ew.use(
        [
            answer(LUNA, "billing_double_charge, billing_refund_status", groups, 700, stream=_usage(130, 30)),
            answer(LUNA, None, dict(zip(values[1:9], H_STAGE2_BILLING, strict=True)), 730, stream=_usage(300, 60)),
        ]
    )
    req = ew.request(
        req_json["questions"], profile="lightweight-model-decide", value=req_json["input"], deadline_ms=3000
    )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: dict[str, Any] = json.loads(json.dumps(snap.result))
    expected = example("decision-result.valid-hierarchical-lone-group.json")
    result["id"], result["created_at"] = expected["id"], expected["created_at"]
    for key in ("trace_id", "option_order_seed", "started_at", "completed_at", "input_digest"):
        result["provenance"][key] = expected["provenance"][key]
    assert result == expected
    assert_valid(snap.result, req_json)
    assert len(ew.world.adapters.calls()) == 2 and [ln.line for ln in cost.lines] == ["decision"]


def _usage(tokens_in: int, tokens_out: int) -> dict[str, Any]:
    return {"usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}


def _slot(member_index: int, r1: dict[str, float], r2: dict[str, dict[str, float]] | None, status: str = "ok") -> Any:

    st = stage("router-default")
    slot = Slot(st.members[member_index], status=status)  # type: ignore[arg-type]
    if status == "ok":
        slot.round1, slot.round2 = r1, r2 or {}
    return slot


def _abc_question() -> Any:
    choices = [{"value": v, "group": v[0]} for v in ("a1", "a2", "b1", "b2", "c1", "c2")]
    return question({"type": "choice", "name": "abc", "instructions": "x", "choices": choices}, limit=4)


def test_t_hier_4_three_members_one_round_two_timeout() -> None:

    q = _abc_question()
    assert q.method == "hierarchical" and [g.label for g in q.groups] == ["a", "b", "c"]
    st = stage("router-default")
    slots = [
        _slot(0, {"g1": 0.6, "g2": 0.3, "g3": 0.1}, {"g1": {"a1": 0.9, "a2": 0.1}, "g2": {"b1": 0.5, "b2": 0.5}}),
        _slot(1, {"g1": 0.5, "g2": 0.4, "g3": 0.1}, {"g1": {"a1": 0.8, "a2": 0.2}, "g2": {"b1": 0.4, "b2": 0.6}}),
        _slot(2, {}, None, status="timeout"),
    ]
    a: Any = hier_scored(q, st, slots, 2, actx(), 0.0, ["g2", "g1"])
    assert [(c["value"], c["probability"]) for c in a["candidates"]] == [
        ("a1", 0.47),
        ("b2", 0.195),
        ("b1", 0.155),
        ("a2", 0.08),
    ]
    assert a["unresolved_mass"] == 0.1
    assert [(g["label"], g["probability"], g["expanded"]) for g in a["groups"]] == [
        ("a", 0.55, True),
        ("b", 0.35, True),
        ("c", 0.1, False),
    ]
    conf = a["confidence"]
    assert (conf["value"], conf["band"], conf["margin"]) == (0.3375, "low", 0.275)
    assert (conf["normalized_entropy"], conf["agreement"], conf["member_divergence"]) == (0.8667, 1.0, 0.0047)
    assert a["quorum"] == {"required": 2, "responded": 2, "refused": 0, "total": 3}
    m1, m2, m3 = a["members"]
    assert m1["distribution"] == {"a1": 0.54, "a2": 0.06, "b1": 0.15, "b2": 0.15} and m1["unresolved_mass"] == 0.1
    assert m2["distribution"] == {"a1": 0.4, "a2": 0.1, "b1": 0.16, "b2": 0.24} and m2["unresolved_mass"] == 0.1
    assert (m3["status"], m3["weight"]) == ("timeout", 0)


def test_t_hier_5_the_answering_members_change_the_expansion() -> None:

    q = _abc_question()
    st = stage("router-default")
    a_dist = {"a1": 0.5, "a2": 0.5}
    p1_hi, p1_m3 = {"g1": 0.98, "g2": 0.01, "g3": 0.01}, {"g1": 0.8, "g2": 0.15, "g3": 0.05}
    # (a) round 1 expands only g1; m1 times out in round 2; {m2, m3} re-pool to (0.89, 0.08, 0.03): g2 needed
    a: Any = hier_scored(
        q,
        st,
        [_slot(0, {}, None, "timeout"), _slot(1, p1_hi, {"g1": a_dist}), _slot(2, p1_m3, {"g1": a_dist})],
        2,
        actx(),
        0.0,
        ["g1"],
    )
    assert (a["type"], a["reason"]) == ("unavailable", "no_quorum")
    # (b) m3 times out instead: {m1, m2} keep E = {g1}; scored with unresolved 0.02
    b: Any = hier_scored(
        q,
        st,
        [_slot(0, p1_hi, {"g1": a_dist}), _slot(1, p1_hi, {"g1": a_dist}), _slot(2, {}, None, "timeout")],
        2,
        actx(),
        0.0,
        ["g1"],
    )
    assert b["type"] == "choice" and b["unresolved_mass"] == 0.02
    # (c) round 1 expands all three; m3 times out; {m1, m2} need {g1, g2}: g3 reported unexpanded, 0.08 unresolved
    p1 = {"g1": 0.6, "g2": 0.32, "g3": 0.08}
    r2 = {"g1": a_dist, "g2": {"b1": 0.5, "b2": 0.5}, "g3": {"c1": 0.5, "c2": 0.5}}
    c: Any = hier_scored(
        q, st, [_slot(0, p1, r2), _slot(1, p1, r2), _slot(2, {}, None, "timeout")], 2, actx(), 0.0, ["g1", "g2", "g3"]
    )
    assert [g["expanded"] for g in c["groups"]] == [True, True, False] and c["unresolved_mass"] == 0.08
    assert {cand["value"] for cand in c["candidates"]} == {"a1", "a2", "b1", "b2"}


def _req_hier(ew: Any, *, deadline_ms: int) -> Any:
    req_json = example("decision-request.valid-hierarchical.json")
    return ew.request(
        req_json["questions"], profile="lightweight-model-decide", value=req_json["input"], deadline_ms=deadline_ms
    )


async def test_t_hier_6_profile_default_deadline_times_out_round_one() -> None:
    cost = FixedCost()
    ew = engine_world(cost=cost)
    ew.use(hier_rules(round1_ms=600))
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), _req_hier(ew, deadline_ms=1000))
    a: Any = res(snap)["answers"][0]
    assert (a["type"], a["reason"], snap.status) == ("unavailable", "deadline_exceeded", "partial")
    assert "candidates" not in a and "groups" not in a
    assert a["members"][0]["status"] == "timeout" and a["members"][0]["latency_ms"] == 500
    assert [(ln.line, ln.usage.tokens_estimated) for ln in cost.lines] == [("waste", True)]
    assert_valid(snap.result, {**example("decision-request.valid-hierarchical.json"), "include": ["members"]})


async def test_t_hier_6_round_two_misses_the_stage_deadline() -> None:
    cost = FixedCost()
    ew = engine_world(cost=cost)
    ew.use(hier_rules(round1_ms=550, round2_ms=700))
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), _req_hier(ew, deadline_ms=1200))
    a: Any = res(snap)["answers"][0]
    assert (a["type"], a["reason"]) == ("unavailable", "deadline_exceeded")
    assert a["members"][0]["status"] == "timeout" and a["members"][0]["latency_ms"] == 1200
    assert sorted(ln.line for ln in cost.lines) == ["decision", "waste"]  # round 1 billed, round 2 cancelled


async def test_t_hier_6_round_two_dispatch_check() -> None:
    """A 90 ms budget (capped by ctx.deadline): round 1 closes at 44 ms (D1 = 45); 46 ms < 50 ms: no round 2."""
    ew = engine_world()
    ew.use(hier_rules(round1_ms=44))
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(budget_s=0.09), _req_hier(ew, deadline_ms=3000))
    a: Any = res(snap)["answers"][0]
    assert (a["type"], a["reason"]) == ("unavailable", "deadline_exceeded")
    assert a["members"][0]["status"] == "timeout"
    assert len(ew.world.adapters.calls()) == 1


def _grouped(name: str, sizes: dict[str, int]) -> dict[str, Any]:
    choices = [{"value": f"{g}{i}", "group": g} for g, n in sizes.items() for i in range(n)]
    return {"type": "choice", "name": name, "instructions": "Pick the item.", "choices": choices}


async def test_t_hier_7_too_many_choices_skips_members() -> None:
    ew = engine_world()
    q = _grouped("item", {"big": 20, "small": 10})
    big = {f"big{i}": (0.5 if i == 0 else 0.5 / 19) for i in range(20)}
    ew.use(
        [
            answer(None, '"value": "big"', {"big": 0.95, "small": 0.05}, 200),
            answer(None, None, big, 200),
        ]
    )
    req = ew.request([q], deadline_ms=3000)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    m1 = a["members"][0]
    assert (m1["status"], m1["skip_reason"], m1["latency_ms"]) == ("skipped", "too_many_choices", 0)
    assert a["quorum"] == {"required": 2, "responded": 2, "refused": 0, "total": 3}
    assert DEEPSEEK not in {c.model_id for c in ew.world.adapters.calls()}
    assert {c.model_id for c in ew.world.adapters.calls()} == {LUNA, HAIKU}
    assert_valid(snap.result, {"questions": [q], "input": "x"})
    singles = {
        "type": "choice",
        "name": "single",
        "instructions": "x",
        "choices": [{"value": f"v{i}", "group": f"g{i}"} for i in range(30)],
    }
    ew2 = engine_world()
    ew2.use([])
    req2 = ew2.request([singles], deadline_ms=3000)
    async with ew2.clock.autojump():
        snap2 = await ew2.engine.decide(ew2.context(), req2)
    a2: Any = res(snap2)["answers"][0]
    assert (a2["type"], a2["reason"]) == ("unavailable", "no_capable_member")
    assert {m["skip_reason"] for m in a2["members"]} == {"too_many_choices"} and ew2.world.adapters.calls() == []
    assert_valid(snap2.result, {"questions": [singles], "input": "x"})


def test_t_hier_8_jev_and_facade_reject_the_hierarchical_method() -> None:

    ew = engine_world()
    q = example("decision-request.valid-hierarchical.json")["questions"]
    profile = ew.snapshot.decision_profile("lightweight-model-decide")
    for fmt in ("jev", "openai"):
        with pytest.raises(NoemaError) as err:
            ew.compiler.compile(profile, q, origin="native", output_format=fmt)
        assert err.value.code is ErrorCode.UNSUPPORTED_PARAMETER and err.value.param == "questions[0].choices"
        assert err.value.details == {"max_choices": 26, "method": "hierarchical"}


def test_t_hier_8_platform_questions_stay_flat() -> None:

    intent = {
        "type": "choice",
        "name": "intent",
        "instructions": "x",
        "choices": [{"value": f"v{i}"} for i in range(10)],
    }
    with pytest.raises(NoemaError) as err:
        compile_question(intent, source="platform", limit=4, output_format="native", critical=True, param="p")
    assert err.value.code is ErrorCode.UNSUPPORTED_PARAMETER and err.value.param == "decisions.profile"
    assert err.value.details == {"max_choices": 4, "question": "intent"}


def _turn_rules(*, hier_r1_ms: float, hier_r2_ms: float) -> list[dict[str, Any]]:
    low = {"true": 0.05, "false": 0.95}
    item = {f"v{i}": (0.9 if i == 0 else 0.1 / 19) for i in range(20)}
    return [
        answer(None, '"value": "options 1-10"', {"options 1-10": 0.95, "options 11-20": 0.05}, hier_r1_ms),
        answer(None, "Pick the item", item, hier_r2_ms),
        answer(None, "primary intent", {"factual_qa": 0.9, "chitchat": 0.05, "coding": 0.05}, 100),
        answer(None, "How hard", {"trivial": 0.1, "moderate": 0.8, "hard": 0.1}, 100),
        answer(None, None, low, 100),
    ]


async def test_t_hier_8_turn_app_question_is_provisional_in_s1() -> None:
    ew = engine_world()
    ew.use(_turn_rules(hier_r1_ms=300, hier_r2_ms=300))
    router = ew.snapshot.decision_profile("router-default")
    app: Any = [
        {
            "type": "choice",
            "name": "item",
            "instructions": "Pick the item.",
            "choices": [{"value": f"v{i}"} for i in range(20)],
        }
    ]
    for blocking in ((), ("item",)):
        qs = ew.compiler.compile(
            router, app, origin="turn", output_format="native", blocking=blocking, platform_from=router
        )
        assert qs.questions[-1].method == "hierarchical"
        req = DecisionRequest(
            ProfileRef("router-default", 1),
            qs,
            "Find it.",
            1000,
            2,
            "profile_default",
            frozenset({"members"}),
            "turn",
            "native",
        )
        async with ew.clock.autojump(), ew.engine.run(ew.context(turn_id="turn_1", escalate=False), req) as run:
            s1 = await run.commit_critical()
            updates = [u async for u in run.updates()]
        item = s1.answers["item"]
        if blocking:
            assert s1.final and item.final and item.type == "choice" and updates == []
        else:
            assert s1.status == "quorum" and not item.final and item.type == "unavailable"
            assert len(updates) == 1 and updates[0].answers["item"].type == "choice"
            provisional: Any = next(a for a in res(s1)["answers"] if a["name"] == "item")
            assert "candidates" not in provisional and "groups" not in provisional
            assert_valid(s1.result, {"questions": [dict(q.wire) for q in qs.questions], "input": "x"})


async def test_t_hier_9_censored_round_one_read_is_an_error() -> None:
    ew = engine_world()
    q = _grouped("item", {"ga": 6, "gb": 6, "gc": 6})
    inner = {f"ga{i}": (0.5 if i == 0 else 0.1) for i in range(6)}
    ew.use(
        [
            answer(DEEPSEEK, '"value": "ga"', {"ga": 0.6, "gb": 0.4, "gc": 0.0}, 100, answer={"censor": ["gc"]}),
            answer(None, '"value": "ga"', {"ga": 0.95, "gb": 0.04, "gc": 0.01}, 100),
            answer(None, None, inner, 100),
        ]
    )
    req = ew.request([q], deadline_ms=3000)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    m1 = a["members"][0]
    assert (m1["status"], m1["error"]["code"]) == ("error", "censored_tail")
    assert a["type"] == "choice" and a["calibration"]["member_subset"] == ["m2", "m3"]
    assert all("censored_options" not in m for m in a["members"])
    assert_valid(snap.result, {"questions": [q], "input": "x"})


async def test_t_hier_10_flat_artifacts_never_apply_to_hierarchical_answers() -> None:

    ew = engine_world()
    ew.use(hier_rules())
    req = _req_hier(ew, deadline_ms=3000)
    q = req.questions.questions[0]
    for method in ("flat", "hierarchical"):
        key = CalibratorBindingKey(
            "lightweight-model-decide", "1", q.signature, ("m1",), input_view_signature([("m1", "native")]), method
        )
        ew.world.calibrators.add(
            Calibrator(
                f"cal_{method}",
                "platform",
                "temperature",
                1e-4,
                {"m1": 1.0},
                IdentityTransform(),
                lambda _m: IdentityTransform(),
                {},
                key,
            )
        )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert a["calibration"]["status"] == "uncalibrated" and "calibrated_distribution" not in a["members"][0]


def test_t_hier_10_cache_key_records_the_method() -> None:
    ew = engine_world()
    hier = _req_hier(ew, deadline_ms=3000)
    assert profile_part(ew.context(), hier).endswith(";m=h")
    twenty = [
        {"type": "choice", "name": "item", "instructions": "x", "choices": [{"value": f"v{i}"} for i in range(20)]}
    ]
    flat = ew.request(twenty, profile="lightweight-model-decide")
    assert profile_part(ew.context(), flat).endswith(";m=f")
    narrow = engine_world(patch={"templates": {"verbalized-dist@1": {"max_choices": 16}}})
    hier20 = narrow.request(twenty, profile="lightweight-model-decide")
    assert hier20.questions.questions[0].method == "hierarchical"
    assert profile_part(narrow.context(), hier20).endswith(";m=h")


async def test_t_hier_7_skips_that_make_quorum_impossible_make_no_call() -> None:
    """Review finding 4 (§19.3): m1 is skipped (too_many_choices) and min_quorum 3 cannot be met by the other two:
    `unavailable / no_capable_member` with no call (no estimated waste, no usage line)."""
    ew = engine_world()
    q = _grouped("item", {"big": 20, "small": 10})
    ew.use([answer(None, None, {"big": 0.6, "small": 0.4}, 300)])
    req = ew.request([q], deadline_ms=3000, min_quorum=3)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert (a["type"], a["reason"]) == ("unavailable", "no_capable_member")
    assert [m["status"] for m in a["members"]] == ["skipped", "late", "late"]
    assert all(m["latency_ms"] == 0 and "usage" not in m for m in a["members"])
    assert ew.world.adapters.calls() == [] and ew.world.cost.lines == []
    assert_valid(snap.result, {"questions": [q], "input": "x", "include": ["members"]})
