"""Jev score and the facade mapping (spec-06 §12.2-§12.3, T-JEV-3, T-FAC-1; IMPL-049): the facade reproduces the
vendored response; unavailable and large unresolved masses map to refusal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.unit.decision.checks import assert_oa
from tests.unit.decision.helpers import actx, question, slots, stage
from tests.unit.decision.world import answer, engine_world

import noema.contracts
from noema.core.protocols.decision import DecisionRequest, ProfileRef
from noema.decision.facade import map_input, map_questions, oa_response
from noema.decision.flat import flat_scored
from noema.decision.jev import jev_answer

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def test_t_jev_3_jev_score_comes_from_the_map() -> None:
    levels = [{"label": f"l{i}"} for i in range(5)]
    q = question({"type": "score", "name": "s", "instructions": "x", "levels": levels})
    st_ = stage("lightweight-model-decide")
    dist = dict(zip([f"l{i}" for i in range(5)], (0.1234, 0.2345, 0.3456, 0.1965, 0.1), strict=True))
    a: Any = flat_scored(q, st_, slots(st_, [dist]), 1, actx(), 0.0)
    assert (a["score"], a["confidence"]["value"]) == (1.9152, 0.2685)
    jev = jev_answer(q, a)
    assert jev["probabilities"] == {"0": 0.12, "1": 0.23, "2": 0.35, "3": 0.2, "4": 0.1}
    assert (jev["score"], jev["confidence"]) == (1.93, 0.28)
    assert jev["legend"] == {str(i): f"l{i}" for i in range(5)}


async def test_t_fac_1_facade_reproduces_the_example_response() -> None:
    oa_req = example("openai-request.valid.json")
    fqs = map_questions(oa_req["questions"])
    assert [(f.internal, f.effective, f.given) for f in fqs] == [
        ("q0", "q0", None),
        ("regression", "regression", "regression"),
    ]
    assert fqs[1].wire["choices"] == [{"value": "true", "description": None}, {"value": "false"}]
    ew = engine_world()
    sev = {"Cosmetic": 0.1, "Workaround available": 0.7, "Fully blocked": 0.2}
    ew.use([answer(None, "How severe", sev, 300), answer(None, "regression", {"true": 0.72, "false": 0.28}, 300)])
    p = ew.snapshot.decision_profile("router-default")
    qs = ew.compiler.compile(p, [f.wire for f in fqs], origin="facade", output_format="openai")

    value = map_input(oa_req["input"])
    assert value == [
        {"role": "user", "content": [{"type": "input_text", "text": "Export fails in Safari but works in Chrome."}]}
    ]
    req = DecisionRequest(
        ProfileRef("router-default", 1),
        qs,
        "Export fails in Safari but works in Chrome.",
        1000,
        2,
        "profile_default",
        frozenset(),
        "facade",
        "openai",
    )
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    resp = oa_response(snap.result, fqs, model="aghoo/router-default@1", ensemble=False)
    expected = example("openai-response.valid.json")
    assert resp["answers"] == expected["answers"] and resp["model"] == expected["model"]
    assert_oa(resp, oa_req)
    (row,) = ew.world.db.store.rows("decision")
    assert [a["name"] for a in row["result"]["answers"]] == ["q0", "regression"]


def test_facade_maps_unavailable_and_large_unresolved_to_refusal() -> None:
    (fq,) = map_questions(
        [{"type": "choice", "name": "x", "instructions": "x", "choices": [{"value": "a"}, {"value": "b"}]}]
    )
    unavailable: Any = {
        "answers": [{"type": "unavailable", "name": "x", "reason": "deadline_exceeded"}],
        "id": "dec_1",
        "usage": {},
    }
    resp = oa_response(unavailable, [fq], model="aghoo/router-default@1", ensemble=True)
    assert resp["answers"] == [{"type": "refusal", "name": "x"}]
    assert resp["x_aghoo"] == {
        "decision_id": "dec_1",
        "calibration": {},
        "mapped_unavailable": {"x": "deadline_exceeded"},
    }
    wide: Any = {
        "answers": [
            {
                "type": "choice",
                "name": "x",
                "choice": "a",
                "unresolved_mass": 0.002,
                "candidates": [{"value": "a", "probability": 0.6}, {"value": "b", "probability": 0.398}],
            }
        ],
        "id": "dec_2",
        "usage": {},
    }
    resp2: Any = oa_response(wide, [fq], model="aghoo/router-default@1", ensemble=True)
    assert resp2["x_aghoo"]["mapped_unavailable"] == {"x": "no_capable_member"}
