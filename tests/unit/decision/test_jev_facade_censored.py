"""spec-10 §8 / spec-06 T-JEV-2: the K = 3 censored tail in Jev (0.40) and the facade (0.4); both validators pass
(IMPL-049)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.decision.checks import assert_jev
from tests.unit.decision.helpers import actx, question, slots, stage

import noema.contracts
from noema.decision.facade import map_questions, oa_response
from noema.decision.flat import flat_scored
from noema.decision.jev import jev_answer, jev_response

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"


ABC = {"type": "choice", "name": "abc", "instructions": "x", "choices": [{"value": v} for v in "ABC"]}


def censored_answer() -> Any:
    q = question(ABC)
    st_ = stage()
    ss = slots(st_, [{"A": 0.6, "B": 0.3999}, "error", "error"])
    ss[0].unresolved, ss[0].censored = 0.0001, ("C",)
    return q, flat_scored(q, st_, ss, 1, actx(), 0.0)


def test_t_jev_2_censored_tail_in_jev_and_facade() -> None:
    q, a = censored_answer()
    assert [c["probability"] for c in a["candidates"]] == [0.6, 0.3999, 0.0] and a["unresolved_mass"] == 0.0001
    assert a["confidence"]["value"] == 0.4667
    jev = jev_answer(q, a)
    assert jev == {"type": "choice", "choice": "A", "probabilities": {"A": 0.6, "B": 0.4, "C": 0.0}, "confidence": 0.4}
    resp = jev_response(
        {"id": "dec_x", "answers": [a], "usage": {"input_tokens": 1, "output_tokens": 1}},
        [q],
        model="aghoo/router-default@1",
    )
    assert_jev(resp, {"questions": [ABC]})
    (fq,) = map_questions([{"type": "choice", "instructions": "x", "choices": [{"value": v} for v in "ABC"]}])
    oa = oa_response(
        {"id": "dec_x", "answers": [{**a, "name": "q0"}], "usage": {}},
        [fq],
        model="aghoo/router-default@1",
        ensemble=False,
    )
    assert oa["answers"] == [
        {
            "type": "choice",
            "name": None,
            "choice": "A",
            "confidence": 0.4,
            "probabilities": [
                {"value": "A", "probability": 0.6},
                {"value": "B", "probability": 0.3999},
                {"value": "C", "probability": 0.0},
            ],
        }
    ]
