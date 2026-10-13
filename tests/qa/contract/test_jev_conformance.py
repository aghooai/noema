"""Gate Q3 on the stack (06 §2.2, 03 §4.3, IMPL-049): every `output_format: "jev"` response validates against
`JevDecisionResponse` and `jev_invariants` (the CheckedClient's ContractValidator, which also parses it with the
tolerant client models), and its values equal the IMPL-049 map of the native result of the same `decision_id`
(GET /v1/decisions/{id}): probabilities = round(native p, 2) per declared option, choice = argmax of that map (ties
in declared order), score = round(sum i * p'_i, 2) and confidence = round(f(p'), 2) over the 2-dp map (f =
normalized_max with n = K for choices, ordinal_dispersion for scores), noul = round(P(true), 2).

K in {2, 3, 5, 16} for choice and score questions, `noul` and `predicate` (synonyms), on lightweight-model-decide
and router-default (escalation off). The mock's distributions are uneven so that rounding matters (the IMPL-049
K = 5 vector among them), and one choice ties after rounding. Pass criterion: 100% of the answers.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import tag

from noema.contracts import vendor

pytestmark = [pytest.mark.e2e]
F = vendor.load("check_examples")
VECTOR_5 = [0.1234, 0.2345, 0.3456, 0.1965, 0.1000]  # IMPL-049: Jev score 1.93, not round(1.9152, 2)
TIE = {"a": 0.4440, "b": 0.4449, "c": 0.1111}  # 0.44 / 0.44 / 0.11: the Jev choice is "a" (declared first)


def dist(k: int) -> list[float]:
    if k == 5:
        return VECTOR_5
    raw = [((i * 7) % k + 1) ** 1.3 for i in range(k)]
    return [round(x / sum(raw), 4) for x in raw[:-1]] + [round(1 - sum(round(x / sum(raw), 4) for x in raw[:-1]), 4)]


def questions(k: int, mark: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "choice",
            "name": "pick",
            "instructions": f"Pick one {mark}c{k}.",
            "choices": [{"value": f"o{i}"} for i in range(k)],
        },
        {
            "type": "score",
            "name": "level",
            "instructions": f"Rate it {mark}s{k}.",
            "levels": [{"label": f"l{i}"} for i in range(k)],
        },
        {"type": "noul" if k % 2 else "predicate", "name": "flag", "instructions": f"Is it so {mark}p{k}?"},
    ]


def rules(k: int, mark: str) -> list[dict[str, Any]]:
    p = dist(k)
    return [
        sc.answer({"prompt_contains": f"{mark}c{k}"}, {f"o{i}": p[i] for i in range(k)}, 30),
        sc.answer({"prompt_contains": f"{mark}s{k}"}, {f"l{i}": p[::-1][i] for i in range(k)}, 30),
        sc.answer({"prompt_contains": f"{mark}p{k}"}, {"true": 0.6173, "false": 0.3827}, 30),
        sc.answer({"prompt_contains": f"{mark}tie"}, TIE, 30),
    ]


def expected(native: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    """The IMPL-049 Jev answer recomputed from one native answer."""
    if question["type"] in ("predicate", "noul"):
        return {"type": "noul", "noul": round(native["probability"], 2)}
    by_value = {str(c["value"]): c["probability"] for c in native["candidates"]}
    if question["type"] == "choice":
        values = [c["value"] for c in question["choices"]]
        probs = {v: round(by_value.get(v, 0.0), 2) for v in values}
        top = max(values, key=lambda v: (probs[v], -values.index(v)))
        conf = round(F.normalized_max([probs[v] for v in values]), 2)
        return {"type": "choice", "choice": top, "probabilities": probs, "confidence": conf}
    labels = [lv["label"] for lv in question["levels"]]
    pv = [round(by_value.get(lb, 0.0), 2) for lb in labels]
    return {
        "type": "score",
        "score": round(sum(i * x for i, x in enumerate(pv)), 2),
        "probabilities": {str(i): x for i, x in enumerate(pv)},
        "confidence": round(F.ordinal_dispersion(pv), 2),
    }


@pytest.mark.parametrize("profile", ["lightweight-model-decide", "router-default"])
@pytest.mark.parametrize("k", [2, 3, 5, 16])
async def test_jev_values_are_the_impl049_map_of_the_native_result(
    stack: Stack, default_scenario: None, mock: Mock, k: int, profile: str
) -> None:
    mark = tag("jev")
    await mock.load(sc.document("qa-jev", rules(k, mark), [sc.answer({}, sc.LOW, 30)]))
    qs = questions(k, mark)
    if k == 3:
        qs.append(
            {
                "type": "choice",
                "name": "tie",
                "instructions": f"Which {mark}tie?",
                "choices": [{"value": v} for v in TIE],
            }
        )
    body: dict[str, Any] = {
        "profile": profile,
        "escalation": "off",
        "output_format": "jev",
        "input": f"Text {mark}",
        "questions": qs,
    }
    client = stack.client(0)
    jev = (await client.request("POST", "/v1/decisions", json=body, expect=200)).json()
    assert jev["model"] == f"aghoo/{profile}@1"
    native = (await stack.client(1).request("GET", f"/v1/decisions/{jev['decision_id']}", expect=200)).json()
    by_name = {a["name"]: a for a in native["answers"]}
    for q in qs:
        got = jev["answers"][q["name"]]
        assert got["type"] not in ("refusal", "unavailable"), (q["name"], got, by_name[q["name"]])
        want = expected(by_name[q["name"]], q)
        if q["type"] == "score":
            want["legend"] = {str(i): lv["label"] for i, lv in enumerate(q["levels"])}
        assert got == want, (q["name"], got, want)
    if k == 3:
        assert jev["answers"]["tie"]["choice"] == "a" and jev["answers"]["tie"]["probabilities"]["b"] == 0.44
