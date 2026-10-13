"""Mock-fidelity diff (spec-10 §2.3): the live cassettes against the wire-mock rehearsal of the same tests.

`python -m tests.live.fidelity [--live GLOB] [--mock GLOB]` (defaults: every `tests/cassettes/live-*` run, the
newest `tests/cassettes/mock-*` run) pairs the calls of both sides by (test, model alias, operation, call kind),
skips cancelled calls (their shape is timing), and compares per pair, for the mock's author:
- the adapter-normalized event types in order and the stop reason;
- the wire: ConverseStream / Mantle SSE event signatures in order (repeat counts dropped), the JSON response key
  paths (usage field names included), the raw error (code, HTTP status, message);
- the classified error (kind, provider code), logprobs presence, and which usage counters were non-zero.
Writes `reports/live/fidelity.json`: per pair the differing fields, and the distinct differences over all pairs.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import orjson

ROOT: Final = Path(__file__).resolve().parents[2]
CASSETTES: Final = ROOT / "tests" / "cassettes"
Key = tuple[str, str, str, str]


def load(directories: Sequence[Path]) -> dict[Key, list[dict[str, Any]]]:
    out: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            c = orjson.loads(path.read_bytes())
            if c.get("cancelled"):
                continue
            out[(c["test"], c["alias"], c["op"], c["call_kind"])].append(c)
    return out


def _ordered(signatures: Sequence[str]) -> list[str]:
    """Event signatures in order with repeat counts dropped and consecutive duplicates merged."""
    out: list[str] = []
    for sig in signatures:
        base = re.sub(r"\*\d+$", "", sig)
        if not out or out[-1] != base:
            out.append(base)
    return out


def _chunk_keys(signatures: Sequence[str]) -> list[str]:
    """Mantle chunk signatures as their union of key paths (chunk order differs by design: text vs usage)."""
    keys: set[str] = set()
    for sig in signatures:
        if sig.startswith("chunk{"):
            keys.update(sig[6:-1].split(","))
    return sorted(keys)


def shape(c: Mapping[str, Any]) -> dict[str, Any]:
    wire = c.get("wire") or {}
    error, raw_error = c.get("error") or {}, wire.get("error") or {}
    usage = c.get("usage") or {}
    events = list(wire.get("events") or [])
    return {
        "events": list(dict.fromkeys(c.get("events") or [])),
        "stop": c.get("stop"),
        "wire_events": _ordered(events) if not any(e.startswith("chunk{") for e in events) else _chunk_keys(events),
        "wire_response": wire.get("response") or [],
        "error": [error.get("kind"), error.get("provider_code")] if error else None,
        "raw_error": [raw_error.get("code"), raw_error.get("http_status"), raw_error.get("message")]
        if raw_error
        else None,
        "logprobs": c.get("logprob_tokens") is not None and c.get("logprob_tokens", 0) > 0,
        "usage_nonzero": sorted(k for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool) and v),
    }


def diff(live: Mapping[str, Any], mock: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, value in live.items():
        other = mock[field]
        if value == other:
            continue
        if isinstance(value, list) and isinstance(other, list) and field.startswith("wire_"):
            out[field] = {
                "live_only": [v for v in value if v not in other],
                "mock_only": [v for v in other if v not in value],
            }
        else:
            out[field] = {"live": value, "mock": other}
    return out


def compare(live_dirs: Sequence[Path], mock_dirs: Sequence[Path]) -> dict[str, Any]:
    live, mock = load(live_dirs), load(mock_dirs)
    pairs: dict[str, Any] = {}
    distinct: dict[str, set[str]] = defaultdict(set)
    for key in sorted(set(live) & set(mock)):
        s_live, s_mock = shape(live[key][-1]), shape(mock[key][-1])  # the latest live run of a re-run test
        d = diff(s_live, s_mock)
        pairs[" | ".join(key)] = d or "identical"
        for field, value in d.items():
            distinct[f"{key[1]} {key[2]} {field}"].add(orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode())
    return {
        "live_runs": [p.name for p in live_dirs],
        "mock_runs": [p.name for p in mock_dirs],
        "pairs_compared": len(pairs),
        "identical": sum(1 for v in pairs.values() if v == "identical"),
        "live_only_calls": sorted(" | ".join(k) for k in set(live) - set(mock)),
        "mock_only_calls": sorted(" | ".join(k) for k in set(mock) - set(live)),
        "distinct_differences": {k: sorted(v) for k, v in sorted(distinct.items())},
        "pairs": pairs,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", default="live-*")
    parser.add_argument("--mock", default=None, help="default: the newest mock-* run")
    parser.add_argument("--out", default=str(ROOT / "reports" / "live" / "fidelity.json"))
    args = parser.parse_args(argv)
    live_dirs = sorted(CASSETTES.glob(args.live))
    mock_dirs = sorted(CASSETTES.glob(args.mock)) if args.mock else sorted(CASSETTES.glob("mock-*"))[-1:]
    if not live_dirs or not mock_dirs:
        raise SystemExit("no live or no mock cassettes")
    report = compare(live_dirs, mock_dirs)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    print(f"{report['pairs_compared']} pairs, {report['identical']} identical -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
