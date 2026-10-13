"""Pure per-attempt rules: IMPL-015 effort, IMPL-084 max_tokens, IMPL-076 walk codes, IMPL-020 estimates and the
wire `Usage` object."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from tests.fakes.config import example_snapshot

from noema.config.models import GenMemberCfg
from noema.core.errors import ErrorCode, ErrorKind
from noema.core.values import Cost, UsageRecord
from noema.generation.policy import attempt_max_tokens, effective_effort, estimate_usage, usage_wire, walk_error


def test_effective_effort() -> None:
    sol = example_snapshot().model("sol-6.1")  # low..max
    member = GenMemberCfg(model="sol-6.1", effort="low")
    assert effective_effort(None, member, sol) == ("low", None)
    assert effective_effort("max", member, sol) == ("max", None)
    effort, deg = effective_effort("none", member, sol)
    assert effort == "low" and deg is not None
    assert (deg.from_, deg.to, deg.reason) == ("effort:none", "effort:low", "param_unsupported")
    effort, deg = effective_effort("xhigh", GenMemberCfg(model="sol-6.1"), sol)
    assert effort is None and deg is not None and deg.to == "effort:default"
    assert effective_effort(None, GenMemberCfg(model="sol-6.1"), sol) == (None, None)


def test_attempt_max_tokens() -> None:
    snap = example_snapshot()
    opus = replace(snap.model("opus-5.5"), reasoning_reserve_tokens=1000)
    capped = replace(snap.model("glm-5.3"), reasoning_reserve_tokens=1000)  # cap 4096
    bare = GenMemberCfg(model="x")
    assert attempt_max_tokens(None, bare, opus) == 4096 + 1000
    assert attempt_max_tokens(None, GenMemberCfg(model="x", max_output_tokens=2000), opus) == 3000
    assert attempt_max_tokens(1500, GenMemberCfg(model="x", max_output_tokens=2000), opus) == 2500
    assert attempt_max_tokens(None, bare, capped) == 4096  # min(cap, cap + reserve)
    assert attempt_max_tokens(2000, bare, capped) == 3000


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (ErrorKind.RETRYABLE, ErrorCode.ALL_ROUTES_UNAVAILABLE),
        (ErrorKind.THROTTLED, ErrorCode.ALL_ROUTES_UNAVAILABLE),
        (ErrorKind.FATAL, ErrorCode.INTERNAL_ERROR),
        (ErrorKind.INVALID_REQUEST, ErrorCode.INTERNAL_ERROR),
        (ErrorKind.TENANT_ACCESS, ErrorCode.TENANT_ACCESS_MISCONFIGURED),
        (None, ErrorCode.ALL_ROUTES_UNAVAILABLE),
    ],
)
def test_walk_error(kind: ErrorKind | None, code: ErrorCode) -> None:
    assert walk_error(kind) is code


def test_estimate_usage_counts_text_reasoning_and_the_overrun() -> None:
    usage = estimate_usage(input_tokens=48, text_chars=9, reasoning_chars=401, chars_per_token=4.0, overrun_tokens=64)
    assert usage == UsageRecord(
        input_tokens=48, output_tokens=3 + 101 + 64, reasoning_tokens=101, tokens_estimated=True
    )
    assert estimate_usage(input_tokens=0, text_chars=0, reasoning_chars=0, chars_per_token=4.0, overrun_tokens=0) == (
        UsageRecord(0, 0, tokens_estimated=True)
    )


def test_usage_wire() -> None:
    usage = UsageRecord(10, 5, cached_input_tokens=2, cache_write_input_tokens=1, reasoning_tokens=3)
    assert usage_wire(usage, Cost(Decimal("0.0000125"), estimated=False)) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_input_tokens": 2,
        "cache_write_input_tokens": 1,
        "reasoning_tokens": 3,
        "cost_usd": 0.000012,
        "cost_estimated": False,
        "tokens_estimated": False,
    }
