"""spec-10 §8 / spec-06 T-FAC-2: an explicit non-pattern name is stored as q0 and echoed with `x_aghoo` keyed by it;
facade request-shape errors (IMPL-047, IMPL-056)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.unit.decision.checks import assert_oa

import noema.contracts
from noema.core.errors import ErrorCode, NoemaError
from noema.decision.facade import map_questions, oa_response

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def test_t_fac_2_non_pattern_names_and_request_errors() -> None:
    oa_req = example("openai-request.valid-non-pattern-name.json")
    fqs = map_questions(oa_req["questions"])
    assert [(f.internal, f.effective) for f in fqs] == [("q0", "Severity Level"), ("regression", "regression")]
    native: Any = {
        "answers": [
            {
                "type": "score",
                "name": "q0",
                "score": 1.1,
                "candidates": [
                    {"value": "Workaround available", "probability": 0.7, "index": 1},
                    {"value": "Fully blocked", "probability": 0.2, "index": 2},
                    {"value": "Cosmetic", "probability": 0.1, "index": 0},
                ],
                "calibration": {"status": "uncalibrated"},
            },
            {
                "type": "choice",
                "name": "regression",
                "choice": "true",
                "candidates": [{"value": "true", "probability": 0.72}, {"value": "false", "probability": 0.28}],
                "calibration": {"status": "uncalibrated"},
            },
        ],
        "id": "dec_01J9ZV5D8Q",
        "usage": {},
    }
    resp = oa_response(native, fqs, model="aghoo/router-default@1", ensemble=True)
    expected = example("openai-response.valid-non-pattern-name.json")
    assert resp["answers"] == expected["answers"] and resp["x_aghoo"] == expected["x_aghoo"]
    assert_oa(resp, oa_req)
    with pytest.raises(NoemaError) as err:
        map_questions([{"type": "predicate", "name": "q3", "instructions": "x"}])
    assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, "questions[0].name")
    collision = example("openai-request.invalid-bool-string-collision.json")["questions"]
    with pytest.raises(NoemaError) as err:
        map_questions(collision)
    assert err.value.param == "questions[1].choices"
