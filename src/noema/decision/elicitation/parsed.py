"""The result of parsing one member call: a distribution over option letters, or a parse error code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

ParseCode = Literal[
    "no_code_token",
    "parse_error",
    "missing_options",
    "unknown_options",
    "value_out_of_range",
    "sum_out_of_range",
    "reasoning_only",
    "empty_output",
]
PARSE_MESSAGES: Final[Mapping[str, str]] = {
    "no_code_token": "No option letter in the top log-probabilities.",
    "parse_error": "The output is not a JSON object of option probabilities.",
    "missing_options": "The output misses option letters.",
    "unknown_options": "The output has unknown option letters.",
    "value_out_of_range": "A probability is not a number in [0, 1].",
    "sum_out_of_range": "The probabilities do not sum to about 1.",
    "reasoning_only": "The model spent its output budget on reasoning.",
    "empty_output": "The model returned no text.",
}


@dataclass(frozen=True, slots=True)
class Parsed:
    """`dist`: letter -> probability over the measured letters (None on error); `censored`: letters absent from a
    logprob top list (no probability, no floor); `unresolved`: the unlisted tail mass, never spread."""

    dist: Mapping[str, float] | None
    censored: tuple[str, ...] = ()
    unresolved: float = 0.0
    error: ParseCode | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_error(code: ParseCode) -> Parsed:
    return Parsed(dist=None, error=code)
