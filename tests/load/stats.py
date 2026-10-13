"""Load metrics (spec-10 §5): join the driver's turn records with the mock's request records by nonce, compute the
per-turn overhead, percentiles with bootstrap 95% CIs, the validity checks and the lag statistics.

Per turn (all CLOCK_MONOTONIC ns, one host):
- `ttft_overhead = (t_first_delta - t_send) - (t_first_text_sent - t_req_recv)` (the gated quantity, 06 §4);
- `e2e_overhead = (t_done - t_send) - (t_mock_done - t_req_recv)` (turn.completed vs the end of the mock stream);
- decomposition: `ingress = t_req_recv - t_send`, `egress_first = t_first_delta - stamp0`;
- `*_nominal`: the same against the scripted latencies (first text 300 ms; stream end 300 + 99 x 66.67 ms), which
  also charges the mock's own lateness to Noema (an upper bound).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import orjson
from tests.load.lag import read_lag

FIRST_TEXT_MS: Final = 300.0
STREAM_END_MS: Final = 300.0 + 99 * 4 / 60 * 1000  # 100 chunks of 4 tokens at 60 tokens/s
DECISION_LATENCY_MS: Final = 50.0
QUANTILES: Final = (50.0, 95.0, 99.0)
BOOTSTRAP: Final = 1000
VALID_SHARE: Final = 0.999  # spec-10 §5: >= 99.9% of turns with holdback_ms = 0 and reason spec_agrees
VALID_LAG_P99_MS: Final = 2.0  # spec-10 §5: every driver and mock process's loop lag p99


def read_jsonl(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_bytes().splitlines():
            if line.strip():
                row = orjson.loads(line)
                if "summary" not in row:
                    rows.append(row)
    return rows


def percentile_ci(values: Sequence[float], q: float, *, seed: int = 7) -> tuple[float, float, float]:
    """(estimate, ci_low, ci_high): the q-th percentile and its bootstrap 95% CI (1,000 resamples)."""
    data = np.asarray(values, dtype=np.float64)
    if data.size == 0:
        return (math.nan, math.nan, math.nan)
    estimate = float(np.percentile(data, q))
    rng = np.random.default_rng(seed)
    boot = np.empty(BOOTSTRAP)
    for i in range(BOOTSTRAP):
        boot[i] = np.percentile(data[rng.integers(0, data.size, data.size)], q)
    low, high = np.percentile(boot, [2.5, 97.5])
    return (estimate, float(low), float(high))


def summarize(values: Sequence[float], *, ci: bool = True) -> dict[str, Any]:
    """n, mean, max and p50/p95/p99 (with CIs when `ci`) of `values` (ms)."""
    out: dict[str, Any] = {"n": len(values)}
    if not values:
        return out
    data = np.asarray(values, dtype=np.float64)
    out["mean"] = round(float(data.mean()), 3)
    out["max"] = round(float(data.max()), 3)
    for q in QUANTILES:
        if ci:
            est, low, high = percentile_ci(values, q)
            out[f"p{q:g}"] = [round(est, 3), round(low, 3), round(high, 3)]
        else:
            out[f"p{q:g}"] = round(float(np.percentile(data, q)), 3)
    return out


@dataclass(frozen=True)
class Joined:
    """One timed turn joined with its generation request at the mock."""

    turn: Mapping[str, Any]
    stream: Mapping[str, Any]
    decisions: int

    @property
    def ttft_ms(self) -> float:
        return float(self.turn["t_first_delta"] - self.turn["t_send"]) / 1e6

    @property
    def upstream_ttft_ms(self) -> float:
        return float(self.stream["t_first_text_sent_ns"] - self.stream["t_req_recv_ns"]) / 1e6

    @property
    def ttft_overhead_ms(self) -> float:
        return self.ttft_ms - self.upstream_ttft_ms

    @property
    def e2e_ms(self) -> float:
        return float(self.turn["t_done"] - self.turn["t_send"]) / 1e6

    @property
    def e2e_overhead_ms(self) -> float:
        return self.e2e_ms - float(self.stream["t_done_ns"] - self.stream["t_req_recv_ns"]) / 1e6

    @property
    def ingress_ms(self) -> float:
        return float(self.stream["t_req_recv_ns"] - self.turn["t_send"]) / 1e6

    @property
    def egress_first_ms(self) -> float | None:
        stamp = self.turn.get("stamp0")
        return None if stamp is None else float(self.turn["t_first_delta"] - stamp) / 1e6


def join(turns: Sequence[Mapping[str, Any]], mock: Sequence[Mapping[str, Any]]) -> tuple[list[Joined], Counter[str]]:
    """Timed turns joined to their first converse-stream record (by receive time: several mock processes number
    their records independently); the counter tallies why others were left out."""
    streams: dict[str, Mapping[str, Any]] = {}
    decisions: Counter[str] = Counter()
    for rec in mock:
        nonce = rec.get("nonce")
        if nonce is None:
            continue
        if rec.get("op") in ("converse-stream", "chat-stream"):
            if nonce not in streams or rec["t_req_recv_ns"] < streams[nonce]["t_req_recv_ns"]:
                streams[nonce] = rec
        else:
            decisions[nonce] += 1
    out: list[Joined] = []
    skipped: Counter[str] = Counter()
    for turn in turns:
        if turn.get("status") != 200 or turn.get("error") is not None:
            skipped[f"error:{turn.get('status')}:{turn.get('error')}"] += 1
            continue
        if turn.get("t_first_delta") is None or turn.get("t_done") is None:
            skipped["no_delta"] += 1
            continue
        stream = streams.get(turn["nonce"])
        if stream is None or stream.get("t_first_text_sent_ns") is None or stream.get("t_done_ns") is None:
            skipped["no_mock_stream"] += 1
            continue
        out.append(Joined(turn, stream, decisions[turn["nonce"]]))
    return out, skipped


def window(rows: Sequence[Mapping[str, Any]], t_from: int, t_to: int) -> list[Mapping[str, Any]]:
    """Turns sent in [t_from, t_to)."""
    return [r for r in rows if t_from <= r["t_send"] < t_to]


def lag_summary(paths: Iterable[Path], t_from: int, t_to: int) -> dict[str, Any]:
    """Lag samples (ms) of one or more processes inside the window: n, p50, p99, p99.9, max."""
    lags = [lag / 1e6 for path in paths for t, lag in read_lag(path) if t_from <= t < t_to]
    if not lags:
        return {"n": 0}
    data = np.asarray(lags)
    return {
        "n": len(lags),
        "p50": round(float(np.percentile(data, 50)), 3),
        "p99": round(float(np.percentile(data, 99)), 3),
        "p99.9": round(float(np.percentile(data, 99.9)), 3),
        "max": round(float(data.max()), 3),
    }


def worst_process_lag_p99(paths: Iterable[Path], t_from: int, t_to: int) -> float | None:
    """The highest per-process lag p99 (ms) inside the window (spec-10 §5 judges each process on its own)."""
    p99s = [lag_summary([path], t_from, t_to).get("p99") for path in paths]
    known = [v for v in p99s if v is not None]
    return max(known) if known else None


def validity(
    joined: Sequence[Joined], lag_files: Mapping[str, Sequence[Path]], t_from: int, t_to: int
) -> dict[str, Any]:
    """spec-10 §5 validity: >= 99.9% of turns with holdback_ms = 0 and reason spec_agrees, every driver and mock
    process with lag p99 <= 2 ms; plus the committed reasons, the fan-out per turn and the non-zero holdbacks."""
    n = len(joined)
    zero = sum(1 for j in joined if j.turn.get("holdback_ms") == 0)
    agree_zero = sum(1 for j in joined if j.turn.get("holdback_ms") == 0 and j.turn.get("reason") == "spec_agrees")
    nonzero = Counter(j.turn.get("holdback_ms") for j in joined if j.turn.get("holdback_ms") != 0)
    lag = {name: worst_process_lag_p99(lag_files.get(name, ()), t_from, t_to) for name in ("drivers", "mock")}
    share = round(agree_zero / n, 5) if n else None
    checks = {
        "holdback_zero_and_spec_agrees": share is not None and share >= VALID_SHARE,
        "driver_lag_p99": lag["drivers"] is not None and lag["drivers"] <= VALID_LAG_P99_MS,
        "mock_lag_p99": lag["mock"] is not None and lag["mock"] <= VALID_LAG_P99_MS,
    }
    return {
        "valid": all(checks.values()),
        "checks": checks,
        "holdback_zero_share": round(zero / n, 5) if n else None,
        "holdback_zero_and_spec_agrees_share": share,
        "holdback_nonzero_ms": {str(k): v for k, v in sorted(nonzero.items(), key=lambda kv: (kv[0] is None, kv[0]))},
        "worst_process_lag_p99_ms": lag,
        "committed_reasons": dict(Counter(str(j.turn.get("reason")) for j in joined)),
        "decision_calls_per_turn": {str(k): v for k, v in sorted(Counter(j.decisions for j in joined).items())},
    }


def mock_lateness(mock: Sequence[Mapping[str, Any]], t_from: int, t_to: int) -> dict[str, Any]:
    """How late the mock answered vs its script (ms): first text of streams, the answer of decision calls."""
    first_text: list[float] = []
    answer: list[float] = []
    for rec in mock:
        if not t_from <= rec["t_req_recv_ns"] < t_to or rec.get("t_done_ns") is None:
            continue
        if rec.get("op") in ("converse-stream", "chat-stream"):
            if rec.get("t_first_text_sent_ns") is not None:
                first_text.append((rec["t_first_text_sent_ns"] - rec["t_req_recv_ns"]) / 1e6 - FIRST_TEXT_MS)
        elif rec.get("t_first_byte_ns") is not None:
            answer.append((rec["t_first_byte_ns"] - rec["t_req_recv_ns"]) / 1e6 - DECISION_LATENCY_MS)
    return {"stream_first_text": summarize(first_text, ci=False), "decision_answer": summarize(answer, ci=False)}


def analyze(
    turns: Sequence[Mapping[str, Any]],
    mock: Sequence[Mapping[str, Any]],
    t_from: int,
    t_to: int,
    *,
    lag_files: Mapping[str, Sequence[Path]],
) -> dict[str, Any]:
    """Everything reported for one measurement window."""
    in_window = window(turns, t_from, t_to)
    joined, skipped = join(in_window, mock)
    seconds = (t_to - t_from) / 1e9
    egress = [e for e in (j.egress_first_ms for j in joined) if e is not None]
    result: dict[str, Any] = {
        "window_s": round(seconds, 1),
        "turns_sent": len(in_window),
        "turns_timed": len(joined),
        "turns_per_s": round(len(joined) / seconds, 2) if seconds > 0 else None,
        "skipped": dict(skipped),
        "ttft_overhead_ms": summarize([j.ttft_overhead_ms for j in joined]),
        "e2e_overhead_ms": summarize([j.e2e_overhead_ms for j in joined]),
        "ttft_overhead_nominal_ms": summarize([j.ttft_ms - FIRST_TEXT_MS for j in joined], ci=False),
        "e2e_overhead_nominal_ms": summarize([j.e2e_ms - STREAM_END_MS for j in joined], ci=False),
        "ingress_ms": summarize([j.ingress_ms for j in joined], ci=False),
        "egress_first_ms": summarize(egress, ci=False),
        "egress_max_other_ms": summarize(
            [j.turn["egress_max_us"] / 1000 for j in joined if j.turn.get("egress_max_us") is not None], ci=False
        ),
        "client_ttft_ms": summarize([j.ttft_ms for j in joined], ci=False),
        "client_e2e_ms": summarize([j.e2e_ms for j in joined], ci=False),
        "decision_ms": summarize(
            [float(j.turn["decision_ms"]) for j in joined if j.turn.get("decision_ms") is not None]
        ),
        "decision_lead_ms": summarize(
            [float(j.turn["decision_lead_ms"]) for j in joined if j.turn.get("decision_lead_ms") is not None], ci=False
        ),
        "validity": validity(joined, lag_files, t_from, t_to),
        "mock_lateness_ms": mock_lateness(mock, t_from, t_to),
        "lag_ms": {name: lag_summary(paths, t_from, t_to) for name, paths in lag_files.items()},
    }
    return result


def errors_by_kind(turns: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: defaultdict[str, int] = defaultdict(int)
    for t in turns:
        if t.get("error") is not None or t.get("status") != 200:
            out[f"{t.get('status')}:{t.get('error')}"] += 1
    return dict(out)
