"""spec-10 §8: Jev from the 2-dp map passes `jev_invariants` for 10,000 Dirichlet(0.5) draws at K = 16; the vendored K
= 16 example answers."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from tests.unit.decision.helpers import question

import noema.contracts
from noema.contracts.vendor import function
from noema.decision.jev import jev_answer
from noema.decision.pool import round_units

EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def _native_choice(k: int, probs: list[float], u: float = 0.0) -> tuple[Any, dict[str, Any]]:
    values = [f"v{i}" for i in range(k)]
    q = question({"type": "choice", "name": "c", "instructions": "x", "choices": [{"value": v} for v in values]})
    emitted = round_units(dict(zip(values, probs, strict=True)), values, u)
    order = sorted(values, key=lambda v: (-emitted[v], values.index(v)))
    a = {
        "type": "choice",
        "name": "c",
        "candidates": [{"value": v, "probability": emitted[v]} for v in order],
        "choice": order[0],
    }
    return q, a


def test_jev_k16_dirichlet_and_the_k16_example() -> None:
    jev_invariants = function("check_examples", "jev_invariants")
    rng = random.Random(16)
    for _ in range(10_000):
        raw = [rng.gammavariate(0.5, 1.0) + 1e-12 for _ in range(16)]
        q, a = _native_choice(16, [x / sum(raw) for x in raw])
        jev = jev_answer(q, a)
        assert jev_invariants({"answers": {"c": jev}}, {"questions": [dict(q.wire)]}) == []
    req = example("decision-request.valid-jev-k16.json")
    expected = example("decision-jev-response.valid-k16.json")
    pc, sc = req["questions"]
    k16 = {
        "accounts": 0.0154,
        "billing": 0.0143,
        "checkout": 0.0118,
        "catalog": 0.0132,
        "search": 0.0349,
        "recommendations": 0.0069,
        "notifications": 0.0255,
        "mobile_app": 0.0196,
        "web_app": 0.0644,
        "api": 0.1187,
        "integrations": 0.0835,
        "reporting": 0.3046,
        "exports": 0.2213,
        "permissions": 0.0451,
        "security": 0.0105,
        "other": 0.0091,
    }
    q1 = question(pc)
    a1: Any = {
        "type": "choice",
        "candidates": [{"value": v, "probability": p} for v, p in k16.items()],
        "unresolved_mass": 0.0012,
    }
    score = [
        0.0143,
        0.0141,
        0.0146,
        0.0139,
        0.0148,
        0.0242,
        0.0238,
        0.0341,
        0.0447,
        0.0656,
        0.0951,
        0.1548,
        0.2146,
        0.1257,
        0.0753,
        0.0704,
    ]
    q2 = question(sc)
    a2: Any = {
        "type": "score",
        "candidates": [
            {"value": lv["label"], "probability": p, "index": i}
            for i, (lv, p) in enumerate(zip(sc["levels"], score, strict=True))
        ],
    }
    assert jev_answer(q1, a1) == expected["answers"]["product_area"]
    assert jev_answer(q2, a2) == expected["answers"]["checklist_items"]
