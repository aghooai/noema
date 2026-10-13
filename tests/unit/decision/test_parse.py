"""Member output parsing (spec-06 §5.6-§5.7; T-LP-1, T-VB-1; IMPL-053, IMPL-054)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from tests.unit.decision.checks import assert_valid
from tests.unit.decision.helpers import FixedCost
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, engine_world

import noema.contracts
from noema.core.protocols.adapters import TokenLogprob
from noema.decision.elicitation.harmony import strip_harmony
from noema.decision.elicitation.logprob_codes import parse_logprob
from noema.decision.elicitation.verbalized import parse_verbalized
from noema.decision.pool import round_units, unresolved_units

T_LP_1 = [
    ("A", 0.62),
    ("B", 0.21),
    (" C", 0.06),
    ("C", 0.03),
    ("D", 0.04),
    ("E", 0.02),
    ("<|end|>", 0.015),
    (" ", 0.004),
]


def token(top: list[tuple[str, float]], chosen: str = "A") -> TokenLogprob:
    entries = tuple((t, math.log(p)) for t, p in top)
    return TokenLogprob(chosen, dict(entries)[chosen], entries)


def test_t_lp_1_censored_logprob_tail() -> None:
    letters = list("ABCDEF")
    parsed = parse_logprob((token(T_LP_1),), letters)
    assert parsed.dist is not None and parsed.censored == ("F",) and "F" not in parsed.dist
    expected = {"A": 0.632008, "B": 0.214067, "C": 0.091743, "D": 0.040775, "E": 0.020387}
    assert {k: round(v, 6) for k, v in parsed.dist.items()} == expected
    assert round(parsed.unresolved, 6) == 0.001019
    emitted = round_units(parsed.dist, letters[:5], parsed.unresolved)
    assert emitted == {"A": 0.632, "B": 0.2141, "C": 0.0917, "D": 0.0408, "E": 0.0204}
    assert unresolved_units(parsed.unresolved) == 10
    assert round((parsed.dist["A"] - 1 / 7) / (6 / 7), 6) == 0.570676


def test_logprob_harmony_marker_and_misses() -> None:
    marker = (TokenLogprob("final", -0.1, ()), TokenLogprob("<|message|>", -0.1, ()))
    pre = TokenLogprob("B", math.log(0.9), (("B", math.log(0.9)),))
    after = token([("A", 0.7), ("B", 0.3)])
    parsed = parse_logprob((pre, *marker, after), ["A", "B"])
    assert parsed.dist is not None and round(parsed.dist["A"], 6) == 0.7
    assert parse_logprob(None, ["A", "B"]).error == "no_code_token"
    assert parse_logprob((TokenLogprob("x", 0.0, ()),), ["A", "B"]).error == "no_code_token"
    lower = token([("a", 0.9), ("B", 0.1)], chosen="B")
    assert parse_logprob((lower,), ["A", "B"]).censored == ("A",)  # case-sensitive: "a" is other mass


VB_ROWS: list[tuple[str, int, str | None]] = [
    ('{"A": 0.44, "B": 0.01, "C": 0.53, "D": 0.02}', 4, None),
    ('```json\n{"A": 0.5, "B": 0.6}\n```', 2, None),
    ('{"A": 0.2, "B": 0.2}', 2, "sum_out_of_range"),
    ('{"A": 0.7}', 2, "missing_options"),
    ('{"A": 0.5, "B": 0.5, "C": 0}', 2, "unknown_options"),
    ('{"A": 1.2, "B": 0.1}', 2, "value_out_of_range"),
    ('{"A": true, "B": 0}', 2, "value_out_of_range"),
    ('{"A": 0.5, "A": 0.5, "B": 0.0}', 2, "parse_error"),
    ("I can't help with that.", 4, "parse_error"),
    ('<|channel|>analysis<|message|>x<|end|><|channel|>final<|message|>{"A":0.9,"B":0.1}<|return|>', 2, None),
    ('{"A": NaN, "B": 0.5}', 2, "parse_error"),
    ("[0.5, 0.5]", 2, "parse_error"),
]


@pytest.mark.parametrize(("text", "k", "error"), VB_ROWS)
def test_t_vb_1_verbalized_parse_table(text: str, k: int, error: str | None) -> None:
    letters = [chr(65 + i) for i in range(k)]
    parsed = parse_verbalized(text, letters)
    assert parsed.error == error
    if error is None:
        assert parsed.dist is not None and abs(sum(parsed.dist.values()) - 1) < 1e-12


def test_verbalized_normalizes_and_keeps_stated_zeros() -> None:
    parsed = parse_verbalized('```json\n{"A": 0.5, "B": 0.6}\n```', ["A", "B"])
    assert parsed.dist is not None and (round(parsed.dist["A"], 6), round(parsed.dist["B"], 6)) == (0.454545, 0.545455)
    zero = parse_verbalized('{"A": 1, "B": 0}', ["A", "B"])
    assert zero.dist == {"A": 1.0, "B": 0.0}


def test_empty_outputs() -> None:
    assert parse_verbalized("", ["A", "B"], "max_tokens").error == "reasoning_only"
    assert parse_verbalized("  ", ["A", "B"], "end_turn").error == "empty_output"
    assert strip_harmony("<|channel|>final<|message|> {} <|end|>") == "{}"


async def test_decision_result_valid_censored_is_reproduced_from_its_request() -> None:

    examples = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
    req_json = json.loads((examples / "decision-request.valid-censored.json").read_text())
    expected = json.loads((examples / "decision-result.valid-censored.json").read_text())
    ew = engine_world(cost=FixedCost())
    top = {
        "answer": {
            "dist": {"A": 0.62, "B": 0.21, "C": 0.03, "D": 0.04, "E": 0.02},
            "censor": ["F"],
            "whitespace_variants": {"C": 0.06},
            "eos_mass": 0.015,
            "other_tokens": {" ": 0.004},
        }
    }
    ew.use(
        [
            {
                "match": {"model": DEEPSEEK, "nth": 1},
                **top,
                "latency_ms": 488,
                "stream": {"usage": {"input_tokens": 60, "output_tokens": 2}},
            },
            {"match": {"model": DEEPSEEK}, "fault": {"kind": "throttling"}, "latency_ms": 5},  # the 400 ms backup
            {"match": {"model": LUNA}, "fault": {"kind": "throttling"}, "latency_ms": 140},
            {"match": {"model": HAIKU}, "fault": {"kind": "throttling"}, "latency_ms": 95},
        ]
    )
    req = ew.request(req_json["questions"], value=req_json["input"], min_quorum=1)
    async with ew.clock.autojump():
        snap = await ew.engine.decide(ew.context(), req)
    result = json.loads(json.dumps(snap.result))
    result["id"], result["created_at"] = expected["id"], expected["created_at"]
    for k in ("trace_id", "option_order_seed", "started_at", "completed_at", "input_digest"):
        result["provenance"][k] = expected["provenance"][k]
    assert result == expected
    assert_valid(snap.result, req_json)


def test_deeply_nested_or_huge_output_is_a_parse_error_not_an_exception() -> None:
    """F8: member output nested past the interpreter's recursion limit (e.g. under prompt injection) is the
    member's `parse_error`, never a RecursionError out of the parser (Wave E fix)."""
    deep = '{"A": ' + "[" * 10_000 + "]" * 10_000 + ', "B": 0.5}'
    assert parse_verbalized(deep, ["A", "B"]).error == "parse_error"
