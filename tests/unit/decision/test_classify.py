"""Quorum, classification and confidence (spec-06 §7.3, §11; T-CLS-1, T-OD-1; IMPL-028, IMPL-058, IMPL-079)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from tests.unit.decision.checks import assert_valid
from tests.unit.decision.helpers import DEPARTMENT, actx, question, slots, stage

from noema.core.values import Modality
from noema.decision.answer import classify, qcount
from noema.decision.confidence import normalized_entropy, normalized_max, ordinal_dispersion
from noema.decision.flat import flat_scored, flat_votes
from noema.decision.pool import lr_units, round_units

D = {"billing": 0.4, "technical": 0.3, "shipping": 0.2, "other": 0.1}
IMAGE: frozenset[Modality] = frozenset({"image"})

ROWS: list[tuple[list[Any], list[str], frozenset[Modality], tuple[str, str | None]]] = [
    (["ok", "ok", "timeout"], ["native"] * 3, frozenset(), ("scored", None)),
    (["ok", "refused", "refused"], ["native"] * 3, frozenset(), ("refusal", None)),
    (["error", "timeout", "ok"], ["native"] * 3, frozenset(), ("unavailable", "deadline_exceeded")),
    (["error", "error", "error"], ["native"] * 3, frozenset(), ("unavailable", "all_members_failed")),
    (["skipped", "skipped", "ok"], ["skipped", "skipped", "native"], frozenset(), ("unavailable", "no_capable_member")),
    (["ok", "ok", "error"], ["ocr_text", "ocr_text", "native"], IMAGE, ("unavailable", "no_capable_member")),
    (["skipped", "ok", "ok"], ["metadata_only", "native", "native"], IMAGE, ("scored", None)),
]


@pytest.mark.parametrize(("statuses", "views", "modalities", "want"), ROWS)
def test_t_cls_1_classification(
    statuses: list[str], views: list[str], modalities: frozenset[Modality], want: tuple[str, str | None]
) -> None:
    st = stage(views=views)
    ss = slots(st, [D if s == "ok" else cast(Any, s) for s in statuses])
    assert classify(ss, 2, modalities) == want


def test_t_cls_1_metadata_only_member_counts_in_total_only() -> None:
    q = question(DEPARTMENT)
    st = stage(views=["metadata_only", "native", "native"])
    ss = slots(st, ["skipped", D, D])
    a: Any = flat_scored(q, st, ss, 2, actx(), 0.0)
    assert a["quorum"] == {"required": 2, "responded": 2, "refused": 0, "total": 3}
    assert a["members"][0]["input_view"] == "metadata_only" and a["members"][0]["skip_reason"] == "input_view"
    assert_valid(
        {
            "id": "dec_x",
            "object": "decision",
            "created_at": 0,
            "status": "completed",
            "answers": [a],
            "provenance": _provenance(),
            "usage": _usage(),
        },
        {"questions": [DEPARTMENT], "input": "x"},
    )


def test_unscored_answers_keep_ok_members_with_their_distribution() -> None:
    st = stage()
    ss = slots(st, ["error", "timeout", D])
    votes: Any = flat_votes(ss, list(D), actx(), 0.0)
    assert votes[2]["status"] == "ok" and votes[2]["distribution"] == D and votes[2]["weight"] == 1.0


def test_qcount_counts_distinct_models() -> None:
    st = stage()
    ss = slots(st, [D, D, D])
    assert qcount(ss) == 3 and qcount(ss[:1] * 3) == 1


def test_t_od_1_confidence_self_tests() -> None:
    assert round(normalized_max(0.95, 4), 2) == 0.93
    assert round(ordinal_dispersion([0.1, 0.7, 0.2]), 2) == 0.55
    assert round(ordinal_dispersion([0, 0.3, 0.3, 0.4, 0]), 3) == 0.25
    assert round(ordinal_dispersion([0.3, 0.7]), 9) == round(normalized_max(0.7, 2), 9) == 0.4
    assert normalized_entropy([0.5, 0.5]) == 1.0 and normalized_max(0.2, 4) == 0.0


def test_largest_remainder_rounding() -> None:
    assert round_units({"a": 0.44485, "b": 0.32905, "c": 0.18485, "d": 0.04125}, ["b", "c", "a", "d"], 0.0) == {
        "b": 0.3291,
        "c": 0.1849,
        "a": 0.4448,
        "d": 0.0412,
    }
    assert sum(lr_units({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, ["a", "b", "c"], 10_000).values()) == 10_000
    assert round_units({"a": 0.6, "b": 0.3999}, ["a", "b", "c"], 0.0001) == {"a": 0.6, "b": 0.3999, "c": 0.0}


def _provenance() -> dict[str, Any]:
    return {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "profile_id": "router-default",
        "profile_version": "1",
        "question_signature": "sha256:" + "0" * 64,
        "input_digest": "hmac-sha256:" + "0" * 64,
        "input_view_signature": "sha256:" + "0" * 64,
        "option_order_seed": 1,
        "prompt_templates": {"m1": "logprob-codes@2", "m2": "verbalized-dist@1", "m3": "verbalized-dist@1"},
        "aggregation": {"method": "linear_pool"},
        "deadline_ms": 1000,
        "quorum": {"required": 2, "responded": 2, "refused": 0, "total": 3},
        "cache": {"hit": False},
        "stages": [],
        "started_at": "2026-10-10T12:00:00.000Z",
        "completed_at": "2026-10-10T12:00:01.000Z",
    }


def _usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "cost_estimated": False,
        "tokens_estimated": False,
    }
