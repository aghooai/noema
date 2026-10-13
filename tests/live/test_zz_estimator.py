"""K1 (spec-10 §3, §6; 06 §8): the `TokenEstimator` against Bedrock's reported usage (n small, reported; the 06 §8
thresholds are tokens <= 2 % and cost <= 5 %). Runs last (file name) over the cassettes of every live run so far
(`tests/cassettes/live-*/`).

- input, Wave E3 onward (the calls whose cassette has `input_chars`; out of sample for `TOKEN_ESTIMATOR_V1`, which
  was fitted on the 48 Wave E calls): the session's `HeuristicTokenEstimator` (the per-model table, what admission
  reserves) and the chars/4 rule (`estimated_input_tokens_chars4`, the before) vs `usage.input_tokens`;
- input, every live call: `estimator_fit.report` with the shipped table, and a refit on every call (in-sample and
  leave-one-out errors: the Phase 0 refit candidate);
- output (streams): `ceil(streamed answer chars / cpt)` with cpt 4 (the waste line of a cancelled stream) and the
  table's cpt vs `usage.output_tokens` (which includes reasoning; Converse has no reasoning breakdown);
- cost: the dev catalog's FAKE uniform prices (input 1.5, output 6 USD per million tokens, tools.bootstrap_dev) on
  the estimated input + reported output vs the reported usage. Real prices and billing data are not available
  (06 §8 reconciliation: P0), so the cost error only weights the token errors.
Pass criterion of the test: a report row for every model that answered with usage.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Final

import orjson
import pytest
from tests.live.budget import Ledger
from tools.bootstrap_dev import DEV_PRICES

from noema.config.models import TOKEN_ESTIMATOR_V1
from noema.ops.cost.estimator import default_table
from noema.ops.cost.estimator_fit import fit_table, report

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]
TOKENS_GATE: Final = 0.02
COST_GATE: Final = 0.05


def _rel(estimate: float, actual: float) -> float:
    """Signed relative error (negative: an underestimate)."""
    return (estimate - actual) / actual


def _cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * float(DEV_PRICES["input"]) + output_tokens * float(DEV_PRICES["output"])) / 1e6


def _summary(errors: list[float], gate: float) -> dict[str, Any]:
    if not errors:
        return {"n": 0}
    absolute = [abs(e) for e in errors]
    return {"n": len(errors), "mean_abs_rel_error": round(sum(absolute) / len(errors), 4),
            "max_abs_rel_error": round(max(absolute), 4), "mean_signed_rel_error": round(sum(errors) / len(errors), 4),
            "within_gate": sum(e <= gate for e in absolute)}  # fmt: skip


def cassettes(ledger: Ledger) -> list[dict[str, Any]]:
    """Every cassette of every run of this mode (`tests/cassettes/<mode>-*/`): K1 covers all live calls so far."""
    root = ledger.root / "tests" / "cassettes"
    return [orjson.loads(p.read_bytes()) for p in sorted(root.glob(f"{ledger.mode}-*/*.json"))]


def _answered(call: dict[str, Any]) -> bool:
    usage = call.get("usage") or {}
    actual_in = usage.get("input_tokens")
    return isinstance(actual_in, int) and actual_in > 0 and isinstance(usage.get("output_tokens"), int)


def _row(call: dict[str, Any], out: dict[str, int] | None) -> dict[str, Any]:
    usage = call["usage"]
    keys = ("test", "alias", "model_id", "op", "call_kind", "effort", "blocks", "input_chars")
    return {k: call.get(k) for k in keys} | {
        "estimated_input": call["estimated_input_tokens"],
        "estimated_input_chars4": call["estimated_input_tokens_chars4"],
        "input": usage["input_tokens"],
        "output": usage["output_tokens"],
        "estimated_output": out,
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "cached": usage.get("cached_input_tokens"),
    }


def test_k1_estimator_accuracy(ledger: Ledger) -> None:
    calls = [c for c in cassettes(ledger) if _answered(c)]
    recent = [c for c in calls if "input_chars" in c]  # Wave E3 onward: out of sample for the shipped table
    if not recent:
        pytest.skip("no call with input_chars reported usage")
    errors: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    rows: list[dict[str, Any]] = []
    for call in recent:
        alias, usage = call["alias"], call["usage"]
        actual_in, actual_out = usage["input_tokens"], usage["output_tokens"]
        estimate, chars4 = call["estimated_input_tokens"], call["estimated_input_tokens_chars4"]
        assert isinstance(estimate, int) and isinstance(chars4, int), call  # every answered call carries both
        mine = errors[alias]
        mine["input_table"].append(_rel(estimate, actual_in))
        mine["input_chars4"].append(_rel(chars4, actual_in))
        mine["cost_fake_prices"].append(_rel(_cost(estimate, actual_out), _cost(actual_in, actual_out)))
        out = None
        if call["op"] == "stream" and actual_out > 0:
            chars, cpt = call.get("text_chars", 0), TOKEN_ESTIMATOR_V1.get(alias, (4.0, 0))[0]
            out = {"chars4": math.ceil(chars / 4), "table_cpt": math.ceil(chars / cpt)}
            mine["output_chars4"].append(_rel(out["chars4"], actual_out))
            mine["output_table_cpt"].append(_rel(out["table_cpt"], actual_out))
        rows.append(_row(call, out))
    models = {
        alias: {kind: _summary(errors[alias][kind], COST_GATE if kind.startswith("cost") else TOKENS_GATE)
                for kind in ("input_table", "input_chars4", "output_chars4", "output_table_cpt", "cost_fake_prices")}
        for alias in sorted(errors)
    }  # fmt: skip
    every_in = [abs(e) for m in errors.values() for e in m["input_table"]]
    every_cost = [abs(e) for m in errors.values() for e in m["cost_fake_prices"]]
    refit = fit_table(calls)
    verdict = {
        "calls_e3": len(recent),
        "tokens_input_le_2pct": all(e <= TOKENS_GATE for e in every_in),
        "cost_le_5pct_fake_prices": all(e <= COST_GATE for e in every_cost),
        "cost_vs_billing": "not run (P0): no real prices, no billing data",
    }
    body = {
        "gate": "K1 (06 §8)",
        "table": {alias: list(fit) for alias, fit in TOKEN_ESTIMATOR_V1.items()},
        "verdict": verdict,
        "models_e3": models,
        "all_live_shipped_table": report(calls, default_table()),
        "refit_all_live": {
            "table": {a: [f.chars_per_token, f.request_overhead_tokens] for a, f in sorted(refit.items())},
            **report(calls, refit),
        },
        "calls": rows,
    }
    ledger.write_report("estimator.json", body)
    assert set(models) == {c["alias"] for c in recent}
