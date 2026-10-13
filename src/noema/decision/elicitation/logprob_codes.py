"""`logprob-codes@2` parsing: the option-letter token and its top log-probabilities (spec-06 §5.6)."""

from __future__ import annotations

import math
from collections.abc import Sequence

from noema.core.protocols.adapters import TokenLogprob

from .harmony import final_start
from .parsed import Parsed, parse_error


def parse_logprob(tokens: Sequence[TokenLogprob] | None, letters: Sequence[str]) -> Parsed:
    """The distribution read from the first code token after the harmony final marker. Whitespace variants of a
    letter merge (case-sensitive); letters absent from the top list are censored and their share stays unresolved:
    `p_c = mass_c / (S + T)`, `unresolved = T / (S + T)` with `T = max(0, 1 - S - other)`."""
    toks = tokens or ()
    allowed = set(letters)
    start = final_start(toks)
    pos = next((i for i in range(start, len(toks)) if toks[i].token.strip() in allowed), None)
    if pos is None:
        return parse_error("no_code_token")
    chosen = toks[pos]
    entries = list(chosen.top)
    if all(t != chosen.token for t, _ in entries):
        entries.append((chosen.token, chosen.logprob))
    mass = dict.fromkeys(letters, 0.0)
    other = 0.0
    for tok, logprob in entries:
        code = tok.strip()
        if code in mass:
            mass[code] += math.exp(logprob)
        else:
            other += math.exp(logprob)
    s = sum(mass.values())
    if s <= 0:
        return parse_error("no_code_token")
    tail = max(0.0, 1.0 - s - other)
    censored = tuple(c for c in letters if mass[c] == 0.0)
    if not censored:
        return Parsed({c: mass[c] / s for c in letters})
    total = s + tail
    return Parsed({c: mass[c] / total for c in letters if mass[c] > 0}, censored, tail / total)
