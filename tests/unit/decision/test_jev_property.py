"""spec-10 §3 Q3 / spec-06 §18: Jev conformance over random native results. Choice, score (K in {2, 3, 5, 16}) and
predicate answers come from `flat_scored` with random members, member errors and censored logprob tails. Jev
probabilities = round(native, 2) (censored -> 0.0), and the Jev choice, score and confidence are the IMPL-049
formulas over that 2-dp map, recomputed here independently. `jev_invariants` passes on every response."""

from __future__ import annotations

import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.decision.checks import assert_jev
from tests.unit.decision.helpers import actx, question, stage

from noema.decision.flat import flat_scored
from noema.decision.jev import jev_answer
from noema.decision.state import Slot

STAGE = stage("router-default")  # m1 deepseek (logprob: may censor), m2 luna, m3 haiku (verbalized)


def _wire(kind: str, k: int, rng: random.Random) -> dict[str, Any]:
    if kind == "predicate":
        return {"type": "predicate", "name": "q", "instructions": "x"}
    if kind == "choice":
        return {"type": "choice", "name": "q", "instructions": "x", "choices": [{"value": f"v{i}"} for i in range(k)]}
    levels = [{"label": f"l{i}", "description": f"level {i}" if rng.random() < 0.5 else None} for i in range(k)]
    return {"type": "score", "name": "q", "instructions": "x", "levels": levels}


def _slots(keys: list[str], rng: random.Random, censor: bool) -> list[Slot]:
    """Random member reads: m2 always ok; m1/m3 ok or error; m1 may censor its lowest options (a logprob top list
    that misses them), leaving their mass unresolved."""
    out = []
    for i, member in enumerate(STAGE.members):
        if i != 1 and rng.random() < 0.25:
            out.append(Slot(member, status="error", error=("upstream_error", "x")))
            continue
        raw = [rng.gammavariate(0.5, 1.0) + 1e-12 for _ in keys]
        dist = {key: x / sum(raw) for key, x in zip(keys, raw, strict=True)}
        slot = Slot(member, status="ok", dist=dist)
        if i == 0 and censor and len(keys) > 2:
            cut = sorted(keys, key=lambda key: dist[key])[: rng.randint(1, len(keys) - 1)]
            slot.censored = tuple(key for key in keys if key in cut)
            slot.unresolved = sum(dist[key] for key in cut)
            slot.dist = {key: p for key, p in dist.items() if key not in cut}
        out.append(slot)
    return out


def _expected(kind: str, wire: dict[str, Any], keys: list[str], native: dict[str, Any]) -> dict[str, Any]:
    """The Jev answer by spec-06 §12.2 / IMPL-049, from the native emitted values only."""
    if kind == "predicate":
        return {"type": "noul", "noul": round(native["probability"], 2)}
    k = len(keys)
    emitted = {str(c["value"]): c["probability"] for c in native["candidates"]}
    m = [round(emitted.get(key, 0.0), 2) for key in keys]  # censored or missing -> 0.0
    if abs(sum(m) - 1) > max(0.011, 0.005 * k):
        return {"type": "unavailable", "reason": "no_capable_member"}
    top = max(range(k), key=lambda i: (m[i], -i))  # ties -> declared order
    if kind == "choice":
        conf = max(0.0, (m[top] - 1 / k) / (1 - 1 / k))  # normalized_max, n = K
        return {
            "type": "choice",
            "choice": keys[top],
            "probabilities": dict(zip(keys, m, strict=True)),
            "confidence": round(conf, 2),
        }
    mad_u = sum(abs(i - (k - 1) / 2) for i in range(k)) / k
    dispersion = max(0.0, 1 - sum(p * abs(i - top) for i, p in enumerate(m)) / mad_u)  # ordinal, mode = top
    return {
        "type": "score",
        "score": round(sum(i * p for i, p in enumerate(m)), 2),
        "legend": {str(i): lv["description"] or lv["label"] for i, lv in enumerate(wire["levels"])},
        "probabilities": {str(i): p for i, p in enumerate(m)},
        "confidence": round(dispersion, 2),
    }


@settings(max_examples=300, deadline=None)
@given(
    kind=st.sampled_from(["choice", "score", "predicate"]),
    k=st.sampled_from([2, 3, 5, 16]),
    censor=st.booleans(),
    seed=st.integers(0, 2**32 - 1),
)
def test_q3_jev_property(kind: str, k: int, censor: bool, seed: int) -> None:
    rng = random.Random(seed)
    wire = _wire(kind, k, rng)
    q = question(wire)
    keys = [o.key for o in q.options]
    native: Any = flat_scored(q, STAGE, _slots(keys, rng, censor), 1, actx(members=False), 0.0)
    assert native["type"] == kind
    jev = jev_answer(q, native)
    assert jev == _expected(kind, wire, keys, native)
    response = {
        "model": "aghoo/router-default@1",
        "answers": {"q": jev},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    assert_jev({**response, "decision_id": "dec_x"}, {"questions": [wire]})


def test_q3_a_large_censored_tail_is_unavailable() -> None:
    """A single censoring member whose 2-dp map misses more than max(0.011, 0.005 K) of the mass."""
    q = question(_wire("choice", 3, random.Random(0)))
    m1 = Slot(STAGE.members[0], status="ok", dist={"v0": 0.6}, unresolved=0.4, censored=("v1", "v2"))
    errs = [Slot(m, status="error", error=("upstream_error", "x")) for m in STAGE.members[1:]]
    native: Any = flat_scored(q, STAGE, [m1, *errs], 1, actx(members=False), 0.0)
    assert jev_answer(q, native) == {"type": "unavailable", "reason": "no_capable_member"}
