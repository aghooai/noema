"""`HeuristicTokenEstimator`: the Phase 1 `TokenEstimator` (spec-11 §2, IMPL-029c, IMPL-078; Wave E2 defect 32).

Sync and pure. Per request, with the alias's `TokenFit` (chars per token, per-request overhead tokens):
- the overhead once (the tokens Bedrock adds around the prompt: roles, template);
- every system string and `TextBlock`: `ceil(chars / chars_per_token)`, rounded up per item; chars are Unicode
  code points;
- `ImageBlock`: `min(1600, ceil(width * height / 750))`;
- `DocumentBlock`: `1500 * pages`.

The table defaults to `TOKEN_ESTIMATOR_V1` (noema.config.models: fitted on the 48 live calls of Wave E,
`estimator_fit`); an alias not in it counts `CHARS_PER_TOKEN_V1` (4.0) and no overhead. A Phase 0 refit replaces
it without a code change: `HeuristicTokenEstimator(table=load_table(path))` with the JSON `estimator_fit` writes.
Admission reserves with it (input incl. history) and the waste lines of cancelled calls use it; its accuracy
against Bedrock usage is measured live (spec-10 K1), not gated here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import orjson

from noema.config.models import CHARS_PER_TOKEN_V1, TOKEN_ESTIMATOR_V1
from noema.core.values import ContentBlock, DocumentBlock, ImageBlock, TextBlock

IMAGE_PIXELS_PER_TOKEN: Final = 750
IMAGE_MAX_TOKENS: Final = 1600
DOCUMENT_TOKENS_PER_PAGE: Final = 1500


def text_tokens(text: str, chars_per_token: float = CHARS_PER_TOKEN_V1) -> int:
    """`ceil(len(text) / chars_per_token)`; 0 for an empty string."""
    return math.ceil(len(text) / chars_per_token)


def image_tokens(width: int, height: int) -> int:
    """`min(1600, ceil(width * height / 750))`."""
    return min(IMAGE_MAX_TOKENS, -(-(width * height) // IMAGE_PIXELS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class TokenFit:
    """One model's text counting: `chars_per_token` (> 0) and the tokens added once per request (>= 0)."""

    chars_per_token: float
    request_overhead_tokens: int

    def __post_init__(self) -> None:
        if not (self.chars_per_token > 0 and self.request_overhead_tokens >= 0):
            raise ValueError(f"invalid token fit {self.chars_per_token!r} / {self.request_overhead_tokens!r}")


V1_FIT: Final = TokenFit(CHARS_PER_TOKEN_V1, 0)  # an alias without a fit


def default_table() -> dict[str, TokenFit]:
    """`TOKEN_ESTIMATOR_V1` as fits."""
    return {alias: TokenFit(cpt, overhead) for alias, (cpt, overhead) in TOKEN_ESTIMATOR_V1.items()}


def load_table(path: Path) -> dict[str, TokenFit]:
    """A refit table: the `models` object of `estimator_fit`'s JSON, `{alias: {"chars_per_token": float,
    "request_overhead_tokens": int}}` (ValueError for anything else)."""
    raw: Any = orjson.loads(path.read_bytes())
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        raise ValueError(f"{path}: expected {{'models': {{alias: fit}}}}")
    out: dict[str, TokenFit] = {}
    for alias, fit in models.items():
        cpt, overhead = (fit.get(k) for k in ("chars_per_token", "request_overhead_tokens"))
        if not isinstance(cpt, int | float) or isinstance(cpt, bool) or type(overhead) is not int:
            raise ValueError(f"{path}: invalid fit for {alias!r}")
        out[alias] = TokenFit(float(cpt), overhead)
    return out


class HeuristicTokenEstimator:
    """Implements `TokenEstimator` (see the module docstring). `table`: alias -> fit (default `default_table()`;
    `{}` = chars/4 and no overhead for every alias, the IMPL-029c V1 rule)."""

    __slots__ = ("_table",)

    def __init__(self, table: Mapping[str, TokenFit] | None = None) -> None:
        self._table: Mapping[str, TokenFit] = default_table() if table is None else dict(table)

    def estimate(self, model_alias: str, system: Sequence[str], blocks: Sequence[ContentBlock]) -> int:
        """Estimated input tokens of `system` + `blocks` for `model_alias`."""
        fit = self._table.get(model_alias, V1_FIT)
        cpt = fit.chars_per_token
        total = fit.request_overhead_tokens
        for text in system:
            total += text_tokens(text, cpt)
        for block in blocks:
            if isinstance(block, TextBlock):
                total += text_tokens(block.text, cpt)
            elif isinstance(block, ImageBlock):
                total += image_tokens(block.width, block.height)
            elif isinstance(block, DocumentBlock):
                total += DOCUMENT_TOKENS_PER_PAGE * block.pages
        return total
