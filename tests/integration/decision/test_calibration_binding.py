"""Calibrator binding enforcement through the engine (spec-06 §8.3-§8.4, spec-10 §3 Q6; ADR-003 §1-§2): an
artifact applies only to the answering stage's profile, version, question, member subset, views and method."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes.labeling import IdentityTransform, TemperatureTransform
from tests.unit.decision.checks import assert_valid, wire_request
from tests.unit.decision.helpers import DEPARTMENT
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, engine_world

from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey
from noema.decision.signatures import input_view_signature

pytestmark = pytest.mark.integration
CLASSES = ["billing", "technical", "shipping", "other"]
HOLDOUT: dict[str, Any] = {
    "dataset_id": "ds_test",
    "label_source": "llm_consensus",
    "labelers": ["us.openai.gpt-6.1-sol", "us.anthropic.claude-sonnet-5-5"],
    "consensus_rate": 0.8,
    "consensus_rate_by_class": dict.fromkeys(CLASSES, 0.8),
    "n": 1200,
    "n_by_class": dict.fromkeys(CLASSES, 300),
    "ece": 0.03,
    "brier": 0.2,
    "nll": 0.7,
    "nll_uncalibrated": 0.8,
    "flagged_classes": [],
    "scope": {
        "target": "llm_consensus_agreement",
        "retained_share": 0.8,
        "slices": [{"name": "language:en", "n": 1200}],
    },
    "family_overlap": {"max_member_lift": 0.04, "threshold": 0.1},
    "evaluated_at": "2026-10-01T12:00:00Z",
}
NATIVE3 = input_view_signature([("m1", "native"), ("m2", "native"), ("m3", "native")])
WEAK = {"billing": 0.3, "technical": 0.25, "shipping": 0.25, "other": 0.2}
STRONG = {"billing": 0.8, "technical": 0.05, "shipping": 0.1, "other": 0.05}


def calibrator(profile: str, signature: str, cal_id: str) -> Calibrator:
    key = CalibratorBindingKey(profile, "1", signature, ("m1", "m2", "m3"), NATIVE3)
    return Calibrator(
        cal_id,
        "tenant",
        "temperature",
        1e-4,
        dict.fromkeys(key.member_subset, 1.0),
        TemperatureTransform(1.2),
        lambda _m: IdentityTransform(),
        HOLDOUT,
        key,
    )


def rules(*, escalate: bool) -> list[dict[str, Any]]:
    first = WEAK if escalate else STRONG
    out = [
        {"match": {"model": m, "nth": 1}, "answer": {"dist": first}, "latency_ms": 100} for m in (DEEPSEEK, LUNA, HAIKU)
    ]
    return [*out, {"match": {}, "answer": {"dist": STRONG}, "latency_ms": 100}]


@pytest.mark.parametrize(
    ("escalate", "artifact_for", "status"),
    [
        (False, "router-default", "calibrated"),
        (True, "router-default", "uncalibrated"),  # the answer came from router-plus: a stage-0 artifact never applies
        (True, "router-plus", "calibrated"),
        (False, "router-plus", "uncalibrated"),
    ],
)
async def test_stage_bound_binding(escalate: bool, artifact_for: str, status: str) -> None:
    ew = engine_world()
    ew.use(rules(escalate=escalate))
    req = ew.request([DEPARTMENT], escalation="profile_default")
    ew.world.calibrators.add(
        calibrator(artifact_for, req.questions.questions[0].signature, "cal_x"), tenant_id="t_acme"
    )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result: Any = snap.result
    answer = result["answers"][0]
    assert bool(result["provenance"]["stages"]) is escalate
    assert answer["calibration"]["status"] == status
    if status == "calibrated":
        assert answer["calibration"]["binding"]["profile_id"] == artifact_for
        assert all(m["calibrator_id"] == "cal_x" for m in answer["members"])
    assert_valid(result, wire_request(req))
