"""Decision spans and metrics (spec-06 §16; IMPL-006): names and attribute keys from the closed registry (the
strict FakeTelemetry raises on anything else), no prompt text or distributions in attributes."""

from __future__ import annotations

from typing import Any, cast

from tests.fakes.telemetry import FakeTelemetry
from tests.unit.decision.helpers import DEPARTMENT
from tests.unit.decision.world import HAIKU, LUNA, answer, engine_world, fault

WEAK = {"billing": 0.48, "technical": 0.02, "shipping": 0.48, "other": 0.02}
STRONG = {"billing": 0.9, "technical": 0.02, "shipping": 0.06, "other": 0.02}


async def test_spans_and_metrics() -> None:
    ew = engine_world()
    tel = cast(FakeTelemetry, ew.world.telemetry)
    ew.use(
        [
            {"match": {"model": LUNA, "nth": 1}, "answer": {"dist": WEAK}, "latency_ms": 300},
            {**fault(HAIKU, "invalid_verbalized", 50, variant="not_json"), "answer": {"dist": STRONG}},
            answer(None, None, STRONG, 300),
        ]
    )
    req = ew.request([DEPARTMENT], profile="lightweight-model-decide", escalation="profile_default")
    async with ew.clock.autojump():
        await ew.engine.decide(ew.context(), req)
    names = [s.name for s in tel.spans]
    assert names.count("decision") == 1 and names.count("decision.escalation") == 1
    decision = next(s for s in tel.spans if s.name == "decision")
    assert decision.attributes["aghoo.profile"] == "lightweight-model-decide"
    members = [s for s in tel.spans if s.name == "decision.member"]
    assert len(members) == 4  # stage 0 (Luna), then the three router-default members (DeepSeek answers before 400 ms)
    for span in members:
        attrs: dict[str, Any] = dict(span.attributes)
        assert {"aghoo.member.id", "aghoo.member.question", "gen_ai.request.model", "aghoo.member.backup"} <= set(attrs)
        assert all("billing" not in str(v) for v in attrs.values())
    escalation = next(s for s in tel.spans if s.name == "decision.escalation")
    assert (escalation.attributes["aghoo.escalation.from"], escalation.attributes["aghoo.escalation.to"]) == (
        "lightweight-model-decide",
        "router-default",
    )
    assert tel.counter("aghoo_decision_parse_errors_total", code="parse_error") == 1
    assert tel.counter("aghoo_decision_member_status_total", status="ok") >= 3
    assert tel.counter("aghoo_decision_member_status_total", status="error") == 1
    assert tel.counter("aghoo_escalations_total", outcome="ran") == 1
    observed = {m.name for m in tel.metrics}
    assert {"aghoo_decision_seconds", "aghoo_decision_quorum_reached_seconds"} <= observed
