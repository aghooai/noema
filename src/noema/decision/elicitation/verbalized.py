"""`verbalized-dist@1` / `verify-dist@1` parsing: strict validation, never repaired (spec-06 §5.7; IMPL-054)."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

from noema.core.protocols.adapters import StopReason

from .harmony import strip_harmony
from .parsed import Parsed, parse_error


class _Invalid(ValueError):
    pass


def _no_dup(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _Invalid("duplicate key")
        out[key] = value
    return out


def _reject(constant: str) -> object:
    raise _Invalid(f"non-finite constant {constant}")


def parse_verbalized(text: str, letters: Sequence[str], stop_reason: StopReason = "end_turn") -> Parsed:
    """The JSON object after the harmony final marker, mapping every letter to a probability in [0, 1] summing to
    0.5..1.5, normalized. A stated 0 stays 0. Empty text: `reasoning_only` at `max_tokens`, else `empty_output`."""
    s = strip_harmony(text)
    if not s:
        return parse_error("reasoning_only" if stop_reason == "max_tokens" else "empty_output")
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < a:
        return parse_error("parse_error")
    try:
        obj = json.loads(s[a : b + 1], object_pairs_hook=_no_dup, parse_constant=_reject)
    except (ValueError, RecursionError):  # nested past the recursion limit (e.g. injected output): not an answer
        return parse_error("parse_error")
    if not isinstance(obj, dict):
        return parse_error("parse_error")
    wanted = set(letters)
    if wanted - obj.keys():
        return parse_error("missing_options")
    if obj.keys() - wanted:
        return parse_error("unknown_options")
    vals: dict[str, float] = {}
    for k in letters:
        v = obj[k]
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or not 0 <= v <= 1:
            return parse_error("value_out_of_range")
        vals[k] = float(v)
    total = sum(vals.values())
    if not 0.5 <= total <= 1.5:
        return parse_error("sum_out_of_range")
    return Parsed({k: v / total for k, v in vals.items()})
