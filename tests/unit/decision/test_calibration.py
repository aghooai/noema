"""Calibrator application (spec-06 §8.3-§8.4, §17 T-CAL-1…4; ADR-003 §1-§2; IMPL-055) and the reproduction of
`decision-result.valid.json` from `decision-request.valid.json`."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.fakes.labeling import FakeCalibratorRegistry, IdentityTransform, PlattTransform, TemperatureTransform
from tests.unit.decision.checks import assert_valid
from tests.unit.decision.helpers import NEEDS_HUMAN, URGENCY, FixedCost, actx, question, slots, stage
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, engine_world

import noema.contracts
from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey, Transform
from noema.decision.flat import flat_scored
from noema.decision.signatures import input_view_signature

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
NATIVE3 = input_view_signature([("m1", "native"), ("m2", "native"), ("m3", "native")])


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def artifact(
    key: CalibratorBindingKey,
    *,
    pooled: Transform | None = None,
    members: Mapping[str, Transform] | None = None,
    holdout: Mapping[str, Any] | None = None,
    cal_id: str = "cal_test",
    method: Any = "temperature",
) -> Calibrator:
    per = dict(members or {})
    return Calibrator(
        cal_id,
        "platform",
        method,
        1e-4,
        dict.fromkeys(key.member_subset, 1.0),
        pooled or IdentityTransform(),
        lambda m: per.get(m, IdentityTransform()),
        dict(holdout or {}),
        key,
    )


def key_for(
    q: Any, subset: tuple[str, ...] = ("m1", "m2", "m3"), *, profile: str = "router-default", view: str = NATIVE3
) -> CalibratorBindingKey:
    return CalibratorBindingKey(profile, "1", q.signature, subset, view, "flat")


URG = [
    {"low": 0.15, "medium": 0.65, "high": 0.2},
    {"low": 0.05, "medium": 0.78, "high": 0.17},
    {"low": 0.1, "medium": 0.658, "high": 0.242},
]
HOLDOUT = example("decision-result.valid.json")["answers"][2]["calibration"]["holdout"]


def test_t_cal_1_pooled_temperature_on_a_score() -> None:
    q = question(URGENCY)
    reg = FakeCalibratorRegistry()
    reg.add(artifact(key_for(q), pooled=TemperatureTransform(1.4), holdout=HOLDOUT, cal_id="cal_urgency_v3"))
    st = stage()
    a: Any = flat_scored(q, st, slots(st, URG), 2, actx(reg), 0.0)
    assert [(c["value"], c["probability"], c["raw_score"]) for c in a["candidates"]] == [
        ("medium", 0.6001, 0.696),
        ("high", 0.2498, 0.204),
        ("low", 0.1501, 0.1),
    ]
    conf = a["confidence"]
    assert (a["score"], conf["value"], conf["band"], conf["margin"]) == (1.0997, 0.4002, "medium", 0.3504)
    assert (conf["normalized_entropy"], conf["member_divergence"]) == (0.8534, 0.0049)
    expected = example("decision-result.valid.json")["answers"][2]["calibration"]
    assert a["calibration"] == expected
    assert all(m["calibrator_id"] == "cal_urgency_v3" and "calibrated_distribution" in m for m in a["members"])


def test_t_cal_2_per_member_temperature_with_a_stated_zero() -> None:
    q = question({**URGENCY, "name": "u"})
    st = stage("lightweight-model-decide")
    k = CalibratorBindingKey(
        "lightweight-model-decide", "1", q.signature, ("m1",), input_view_signature([("m1", "native")])
    )
    reg = FakeCalibratorRegistry()
    reg.add(artifact(k, members={"m1": TemperatureTransform(2.0, 1e-4)}))
    a: Any = flat_scored(q, st, slots(st, [{"low": 0.7, "medium": 0.3, "high": 0.0}]), 1, actx(reg), 0.0)
    m1 = a["members"][0]
    assert m1["calibrated_distribution"] == {"low": 0.6, "medium": 0.3928, "high": 0.0072}
    assert m1["distribution"] == {"low": 0.7, "medium": 0.3, "high": 0.0}
    assert [c["raw_score"] for c in sorted(a["candidates"], key=lambda c: c["index"])] == [0.6, 0.3928, 0.0072]


def test_t_cal_3_platt_on_a_predicate() -> None:
    q = question(NEEDS_HUMAN)
    reg = FakeCalibratorRegistry()
    reg.add(artifact(key_for(q), pooled=PlattTransform(0.8, -0.2), method="platt"))
    st = stage()
    dists = [{"true": p, "false": 1 - p} for p in (0.03, 0.05, 0.085)]
    a: Any = flat_scored(q, st, slots(st, dists), 2, actx(reg), 0.0)
    assert a["probability"] == 0.0776 and a["calibration"]["method"] == "platt"
    assert {c["value"]: c["raw_score"] for c in a["candidates"]} == {True: 0.055, False: 0.945}


def test_t_cal_4_temperature_preserves_the_unresolved_mass() -> None:
    t = TemperatureTransform(1.5)
    out = t.apply({"A": 0.632, "B": 0.2141, "C": 0.0917, "D": 0.0408, "E": 0.0204}, 0.001)
    assert [round(out[k], 6) for k in "ABCDE"] == [0.493483, 0.239813, 0.136262, 0.079415, 0.050028]
    assert round(sum(out.values()), 9) == 0.999


def test_any_binding_mismatch_serves_uncalibrated() -> None:
    q = question(URGENCY)
    st = stage()
    for wrong in (
        key_for(q, ("m1", "m2")),  # another member subset
        key_for(q, profile="router-plus"),  # another stage
        key_for(q, view=input_view_signature([("m1", "ocr_text"), ("m2", "native"), ("m3", "native")])),
        CalibratorBindingKey("router-default", "2", q.signature, ("m1", "m2", "m3"), NATIVE3),  # another version
    ):
        reg = FakeCalibratorRegistry()
        reg.add(artifact(wrong, pooled=TemperatureTransform(1.4)))
        a: Any = flat_scored(q, st, slots(st, URG), 2, actx(reg), 0.0)
        assert a["calibration"]["status"] == "uncalibrated" and "calibrator_id" not in a["members"][0]


async def test_decision_result_valid_is_reproduced_from_its_request() -> None:
    ew = engine_world(cost=FixedCost())
    req_json = example("decision-request.valid.json")
    q_urgency = ew.compiler.compile(
        ew.snapshot.decision_profile("router-default"), req_json["questions"], origin="native", output_format="native"
    ).questions[2]
    ew.world.calibrators.add(
        artifact(key_for(q_urgency), pooled=TemperatureTransform(1.4), holdout=HOLDOUT, cal_id="cal_urgency_v3")
    )
    dists = {
        "Which team": [
            dict(zip(["billing", "technical", "shipping", "other"], d, strict=True))
            for d in ((0.2181, 0.3597, 0.3597, 0.0625), (0.44, 0.01, 0.53, 0.02), (0.45, 0.03, 0.45, 0.07))
        ],
        "speak with a human": [{"true": p, "false": 1 - p} for p in (0.03, 0.05, 0.085)],
        "How urgent": URG,
    }
    timing = {DEEPSEEK: (488, 60, 2), LUNA: (883, 120, 29), HAIKU: (938, 130, 35)}
    rules: list[dict[str, Any]] = []
    for text, per_member in dists.items():
        for (model, (ms, i, o)), dist in zip(timing.items(), per_member, strict=True):
            usage = {"stream": {"usage": {"input_tokens": i, "output_tokens": o}}}
            match = {"model": model, "prompt_contains": text}
            if model == DEEPSEEK:  # the time-triggered backup (400 ms) fails fast; the primary wins at 488 ms
                rules.append({"match": {**match, "nth": 1}, "answer": {"dist": dist}, "latency_ms": ms, **usage})
                rules.append({"match": match, "fault": {"kind": "throttling"}, "latency_ms": 5})
            else:
                rules.append({"match": match, "answer": {"dist": dist}, "latency_ms": ms, **usage})
    ew.use(rules)
    req = ew.request(req_json["questions"], value=req_json["input"], deadline_ms=1000, min_quorum=2)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: Any = json.loads(json.dumps(snap.result))
    expected = example("decision-result.valid.json")
    for m in result["answers"][2]["members"]:  # IMPL-055: an applied artifact adds these to every ok member
        assert m.pop("calibrator_id") == "cal_urgency_v3" and m.pop("calibrated_distribution") == m["distribution"]
    result["id"], result["created_at"] = expected["id"], expected["created_at"]
    for k in ("trace_id", "option_order_seed", "started_at", "completed_at", "input_digest"):
        result["provenance"][k] = expected["provenance"][k]
    assert result == expected
    assert_valid(snap.result, req_json)
