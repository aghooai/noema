"""TokenEstimator vectors (spec-11 §2, IMPL-029c; Wave E2 defect 32): per-item ceil(chars / chars_per_token) plus a
per-request overhead from the alias's fit, images min(1600, ceil(w*h/750)), documents 1500 per page; exact for these
vectors. The fitted table (`TOKEN_ESTIMATOR_V1`) against the Wave E live cassettes: K1 input errors (spec-10 K1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import orjson
import pytest

from noema.config.models import TOKEN_ESTIMATOR_V1
from noema.core.values import ContentBlock, DocumentBlock, ImageBlock, TextBlock
from noema.ops.cost import HeuristicTokenEstimator, image_tokens, text_tokens
from noema.ops.cost.estimator import TokenFit, default_table, load_table
from noema.ops.cost.estimator_fit import main, points

pytestmark = pytest.mark.unit

EST = HeuristicTokenEstimator()
V1 = HeuristicTokenEstimator(table={})  # chars/4 and no overhead for every alias: the IMPL-029c arithmetic
CASSETTES: Final = Path(__file__).resolve().parents[2] / "cassettes"
V1_RUNS: Final = ("live-20261011T073404", "live-20261011T073549", "live-20261011T073610")  # the 48 answered calls


@pytest.mark.parametrize(("text", "tokens"), [("", 0), ("a", 1), ("abcd", 1), ("abcde", 2), ("é" * 8, 2)])
def test_text_is_ceil_of_code_points_over_four(text: str, tokens: int) -> None:
    assert text_tokens(text) == tokens


@pytest.mark.parametrize(
    ("w", "h", "tokens"), [(1, 1, 1), (100, 75, 10), (100, 76, 11), (1000, 1000, 1334), (1200, 1000, 1600)]
)
def test_image_formula_and_cap(w: int, h: int, tokens: int) -> None:
    assert image_tokens(w, h) == tokens


def test_items_round_separately_and_add_up() -> None:
    blocks: list[ContentBlock] = [
        TextBlock("abcde"),  # 2
        TextBlock("x"),  # 1
        ImageBlock("png", b"", 100, 76),  # 11
        ImageBlock("webp", b"", 8000, 8000),  # 1600
        DocumentBlock("pdf", "doc-1", b"", pages=2),  # 3000
        DocumentBlock("txt", "doc-2", b""),  # 1500 (one page)
    ]
    assert V1.estimate("opus-5.5", ["system prompt.", "x" * 9], blocks) == 4 + 3 + 2 + 1 + 11 + 1600 + 3000 + 1500
    fitted = 10 + 5 + 3 + 2 + 1 + 11 + 1600 + 3000 + 1500  # opus: 3.06 chars/token, 10 tokens per request
    assert EST.estimate("opus-5.5", ["system prompt.", "x" * 9], blocks) == fitted


def test_each_alias_counts_with_its_fit_and_unknown_ones_with_chars_over_four() -> None:
    text = [TextBlock("x" * 306)]
    expected = {"haiku-5.5": 10 + 100, "sol-6.1": 11 + 67, "deepseek-3.2": 7 + 75, "glm-5.3": 25 + 77, "x": 77}
    assert {alias: EST.estimate(alias, [], text) for alias in expected} == expected
    assert EST.estimate("unknown", [], []) == 0 and EST.estimate("sol-6.1", [], []) == 11  # the overhead once
    custom = HeuristicTokenEstimator(table={"sol-6.1": TokenFit(2.0, 1)})
    assert custom.estimate("sol-6.1", ["abc"], text) == 1 + 2 + 153 and custom.estimate("glm-5.3", [], text) == 77


def v1_calls() -> list[dict[str, Any]]:
    return [orjson.loads(p.read_bytes()) for run in V1_RUNS for p in sorted((CASSETTES / run).glob("*.json"))]


def test_k1_input_errors_on_the_wave_e_live_calls() -> None:
    """Defect 32 (live-smoke §4): chars/4 missed Bedrock's input count by 26% for Haiku, 16% for Sol, 61% for GLM.
    Each text-only live call is replayed as its chars (4 x the recorded chars/4 estimate) through the estimator."""
    pts = points(v1_calls())
    assert len(pts) == 46  # 48 answered calls, 2 with media (Sonnet image / PDF)
    for alias in {a for a, _, _ in pts}:
        errors = [(EST.estimate(alias, [], [TextBlock("x" * c)]) - y) / y for a, c, y in pts if a == alias]
        mean_abs = sum(abs(e) for e in errors) / len(errors)
        assert mean_abs <= 0.025 and max(abs(e) for e in errors) <= 0.10, (alias, errors)
        assert sum(errors) / len(errors) >= -0.02, (alias, errors)  # no model under-reserved by more than 2%


def test_the_shipped_table_is_the_fit_of_the_wave_e_live_calls(tmp_path: Path, capsys: Any) -> None:
    assert len(TOKEN_ESTIMATOR_V1) == 7
    paths = [str(p) for run in V1_RUNS for p in sorted((CASSETTES / run).glob("*.json"))]
    assert main(paths) == 0  # the fit of the 48 calls
    out = tmp_path / "fit.json"
    out.write_text(capsys.readouterr().out)
    assert load_table(out) == default_table()  # what the CLI writes is what a Phase 0 override loads
    bad = tmp_path / "bad.json"
    bad.write_bytes(orjson.dumps({"models": {"sol-6.1": {"chars_per_token": 0, "request_overhead_tokens": 1}}}))
    with pytest.raises(ValueError, match="invalid token fit"):
        load_table(bad)
    bad.write_bytes(orjson.dumps({"models": {"sol-6.1": {"chars_per_token": 4.0, "request_overhead_tokens": 1.5}}}))
    with pytest.raises(ValueError, match="invalid fit"):
        load_table(bad)
