"""Fit the `HeuristicTokenEstimator` table from recorded live calls (Wave E2 defect 32; spec-10 K1; Phase 0 refits).

Input: the cassettes of `tests/live` (`tests/cassettes/live-*/*.json`); per answered call the alias, the content
block kinds, the prompt length and Bedrock's `usage.input_tokens`. The length is `input_chars` when the cassette has
it; the Wave E cassettes do not keep the request text, so there it is 4x `estimated_input_tokens`, which they
recorded with the chars/4 rule (every system string and text block rounded up: exact up to 3 chars per text item).
Only text-only calls are fitted: an image or document term would be confounded with the text.

Model per fit group (the Claude models share a tokenizer: one group, `GROUPS`): `input = overhead + ceil(chars /
cpt)`, cpt on a 0.01 grid in [2, 8], an integer overhead in [0, 100], minimizing the mean absolute relative error
(ties: the max). A group whose calls all have one prompt length keeps cpt = CHARS_PER_TOKEN_V1 and fits only the
overhead. `report` gives per alias n, the mean / max absolute relative error, the calls within 2% (06 §8) and the
signed mean error, for the chars/4 estimate and for a table, plus each group's leave-one-out error.

    python -m noema.ops.cost.estimator_fit tests/cassettes/live-*/*.json > fit.json
    # then HeuristicTokenEstimator(table=load_table(Path("fit.json")))
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import orjson

from noema.config.models import CHARS_PER_TOKEN_V1
from noema.ops.cost.estimator import TokenFit

GROUPS: Final[Mapping[str, str]] = {"haiku-5.5": "claude", "sonnet-5.5": "claude", "opus-5.5": "claude"}
CPT_GRID: Final = tuple(round(2.0 + 0.01 * i, 2) for i in range(601))
MAX_OVERHEAD: Final = 100
K1_TOKENS: Final = 0.02  # 06 §8

Point = tuple[str, int, int]  # (alias, chars, Bedrock input tokens)


def points(calls: Iterable[Mapping[str, Any]]) -> list[Point]:
    """The fit points: answered, text-only calls (see the module docstring)."""
    out: list[Point] = []
    for call in calls:
        usage = call.get("usage") or {}
        actual, estimate = usage.get("input_tokens"), call.get("estimated_input_tokens")
        if not isinstance(actual, int) or actual <= 0 or not isinstance(usage.get("output_tokens"), int):
            continue
        chars = call.get("input_chars", 4 * estimate if isinstance(estimate, int) else None)
        if isinstance(chars, int) and set(call.get("blocks") or {}) <= {"TextBlock"}:
            out.append((str(call["alias"]), chars, actual))
    return out


def predict(fit: TokenFit, chars: int) -> int:
    return fit.request_overhead_tokens + math.ceil(chars / fit.chars_per_token)


def _errors(fit: TokenFit, pts: Sequence[Point]) -> list[float]:
    return [(predict(fit, chars) - actual) / actual for _, chars, actual in pts]


def fit_group(pts: Sequence[Point]) -> TokenFit:
    """The (cpt, overhead) of the least mean absolute relative error over `pts` (ties: the least max)."""
    grid = CPT_GRID if len({chars for _, chars, _ in pts}) > 1 else (CHARS_PER_TOKEN_V1,)
    best: tuple[tuple[float, float], TokenFit] | None = None
    for cpt in grid:
        residuals = [actual - math.ceil(chars / cpt) for _, chars, actual in pts]
        low = min(MAX_OVERHEAD, max(0, min(residuals)))
        high = max(low, min(MAX_OVERHEAD, max(residuals)))
        for overhead in range(low, high + 1):  # outside [min, max] residual every error only grows
            errors = [abs(overhead - r) / actual for r, (_, _, actual) in zip(residuals, pts, strict=True)]
            key = (round(sum(errors) / len(errors), 12), round(max(errors), 12))
            if best is None or key < best[0]:
                best = (key, TokenFit(cpt, overhead))
    assert best is not None
    return best[1]


def fit_table(calls: Iterable[Mapping[str, Any]], groups: Mapping[str, str] = GROUPS) -> dict[str, TokenFit]:
    """Alias -> fit, one fit per group (an alias outside `groups` is its own group)."""
    pts = points(calls)
    by_group: dict[str, list[Point]] = {}
    for p in pts:
        by_group.setdefault(groups.get(p[0], p[0]), []).append(p)
    fits = {g: fit_group(group) for g, group in by_group.items()}
    return {alias: fits[groups.get(alias, alias)] for alias in sorted({p[0] for p in pts})}


def _summary(errors: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(errors),
        "mean_abs_rel_error": round(sum(abs(e) for e in errors) / len(errors), 4),
        "max_abs_rel_error": round(max(abs(e) for e in errors), 4),
        "within_2pct": sum(abs(e) <= K1_TOKENS for e in errors),
        "mean_rel_error": round(sum(errors) / len(errors), 4),
    }


def report(
    calls: Iterable[Mapping[str, Any]], table: Mapping[str, TokenFit], *, groups: Mapping[str, str] = GROUPS
) -> dict[str, Any]:
    """Per alias: the chars/4 estimate's and `table`'s input errors; per group: the leave-one-out error."""
    pts = points(calls)
    v1 = TokenFit(CHARS_PER_TOKEN_V1, 0)
    models: dict[str, Any] = {}
    for alias in sorted({p[0] for p in pts}):
        mine = [p for p in pts if p[0] == alias]
        fit = table.get(alias, v1)
        models[alias] = {
            "chars_per_token": fit.chars_per_token,
            "request_overhead_tokens": fit.request_overhead_tokens,
            "chars_div_4": _summary(_errors(v1, mine)),
            "fitted": _summary(_errors(fit, mine)),
        }
    loo: dict[str, Any] = {}
    for group in sorted({groups.get(p[0], p[0]) for p in pts}):
        mine = [p for p in pts if groups.get(p[0], p[0]) == group]
        if len(mine) > 2:
            held = [_errors(fit_group(mine[:i] + mine[i + 1 :]), [p])[0] for i, p in enumerate(mine)]
            loo[group] = _summary(held)
    return {"models": models, "leave_one_out": loo}


def main(argv: Sequence[str]) -> int:
    calls = [orjson.loads(Path(p).read_bytes()) for p in argv]
    table = fit_table(calls)
    body = {
        "models": {
            a: {"chars_per_token": f.chars_per_token, "request_overhead_tokens": f.request_overhead_tokens}
            for a, f in table.items()
        },
        "report": report(calls, table),
    }
    sys.stdout.write(orjson.dumps(body, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
