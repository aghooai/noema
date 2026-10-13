"""Engine-level pool vectors (spec-06 §17 T-POOL-1, T-POOL-2, T-PRED-1) through real member calls on the fake
adapters: logprob reads for DeepSeek, verbalized JSON for Luna and Haiku."""

from __future__ import annotations

from typing import Any

from tests.unit.decision.checks import assert_valid, wire_request
from tests.unit.decision.helpers import DEPARTMENT, NEEDS_HUMAN
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, answer, engine_world, res

OPTS = ["billing", "technical", "shipping", "other"]
M1 = dict(zip(OPTS, (0.2181, 0.3597, 0.3597, 0.0625), strict=True))
M2 = dict(zip(OPTS, (0.44, 0.01, 0.53, 0.02), strict=True))
M3 = dict(zip(OPTS, (0.45, 0.03, 0.45, 0.07), strict=True))


def department_rules(*, m3_ms: float = 938) -> list[dict[str, Any]]:
    return [
        answer(DEEPSEEK, "Which team", M1, 300),
        answer(LUNA, "Which team", M2, 883),
        answer(HAIKU, "Which team", M3, m3_ms),
    ]


async def test_t_pool_1_three_members_uncalibrated() -> None:
    ew = engine_world()
    ew.use(department_rules())
    req = ew.request([DEPARTMENT])
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert [(c["value"], c["probability"], c["votes"]) for c in a["candidates"]] == [
        ("shipping", 0.4466, 1),
        ("billing", 0.3694, 1),
        ("technical", 0.1332, 1),
        ("other", 0.0508, 0),
    ]
    assert a["confidence"] == {
        "kind": "heuristic",
        "value": 0.2621,
        "method": "normalized_max",
        "band": "low",
        "margin": 0.0772,
        "normalized_entropy": 0.828,
        "agreement": 0.3333,
        "member_divergence": 0.0472,
    }
    assert a["quorum"] == {"required": 2, "responded": 3, "refused": 0, "total": 3}
    assert a["calibration"] == {"status": "uncalibrated", "source": "mixed", "member_subset": ["m1", "m2", "m3"]}
    assert snap.status == "completed" and a["choice"] == "shipping"
    assert [m["weight"] for m in a["members"]] == [0.3333, 0.3333, 0.3333]
    assert_valid(snap.result, wire_request(req))


async def test_t_pool_2_deadline_with_quorum_met_is_completed() -> None:
    """IMPL-001: m3 misses the 1,000 ms deadline; {m1, m2} meet min_quorum 2: scored, `completed`."""
    ew = engine_world()
    ew.use(department_rules(m3_ms=1400))
    req = ew.request([DEPARTMENT])
    t0 = ew.clock.monotonic()
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert [(c["value"], c["probability"], c["raw_score"]) for c in a["candidates"]] == [
        ("shipping", 0.4448, 0.4449),
        ("billing", 0.3291, 0.3291),
        ("technical", 0.1849, 0.1849),
        ("other", 0.0412, 0.0413),
    ]
    conf = a["confidence"]
    assert (conf["value"], conf["margin"], conf["agreement"]) == (0.2598, 0.1158, 0.5)
    assert (conf["normalized_entropy"], conf["member_divergence"]) == (0.8437, 0.0569)
    assert a["quorum"] == {"required": 2, "responded": 2, "refused": 0, "total": 3}
    assert a["calibration"]["status"] == "uncalibrated" and a["calibration"]["member_subset"] == ["m1", "m2"]
    m3 = a["members"][2]
    assert (m3["status"], m3["weight"], m3["latency_ms"]) == ("timeout", 0, 1000)
    assert snap.status == "completed"
    assert round((ew.clock.monotonic() - t0) * 1000) == 1000
    assert_valid(snap.result, wire_request(req))


async def test_t_pred_1_predicate_pool() -> None:
    ew = engine_world()
    ew.use(
        [
            answer(DEEPSEEK, "speak with a human", {"true": 0.03, "false": 0.97}, 300),
            answer(LUNA, "speak with a human", {"true": 0.05, "false": 0.95}, 500),
            answer(HAIKU, "speak with a human", {"true": 0.085, "false": 0.915}, 600),
        ]
    )
    req = ew.request([NEEDS_HUMAN])
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    a: Any = res(snap)["answers"][0]
    assert [(c["value"], c["probability"]) for c in a["candidates"]] == [(False, 0.945), (True, 0.055)]
    assert a["probability"] == 0.055
    conf = a["confidence"]
    assert (conf["value"], conf["band"], conf["margin"], conf["normalized_entropy"]) == (0.89, "high", 0.89, 0.3073)
    assert (conf["agreement"], conf["member_divergence"]) == (1.0, 0.0018)
    assert snap.answers["needs_human"].value == 0.055 and snap.answers["needs_human"].band == "high"
    assert_valid(snap.result, wire_request(req))
