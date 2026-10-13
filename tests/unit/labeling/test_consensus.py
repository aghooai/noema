"""Consensus statistics and the family-overlap lift (06 §2.2; ADR-003 rules A5, A6), plus the lifecycle edges."""

from __future__ import annotations

import pytest

from noema.labeling.consensus import (
    LabelerVote,
    SampledItem,
    consensus_stats,
    family_overlap_lift,
    retained_share,
)
from noema.labeling.lifecycle import InvalidTransition, check_activation, check_transition, gate_status

pytestmark = pytest.mark.unit
SOL, SONNET = "us.openai.gpt-6.1-sol", "us.anthropic.claude-sonnet-5-5"


def item(
    sol: str | None, sonnet: str | None, member: str, *, reason: str = "uniform", status: str = "ok"
) -> SampledItem:
    votes = (
        LabelerVote(SOL, "ok" if sol is not None else status, sol),
        LabelerVote(SONNET, "ok" if sonnet is not None else status, sonnet),
    )
    kept = sol is not None and sol == sonnet
    return SampledItem(kept, sol if kept else None, reason, votes, {"m1": member})


def test_consensus_rates_by_class() -> None:
    items = [
        item("a", "a", "a"),
        item("a", "b", "a"),  # dropped: involves a and b
        item("b", "b", "b"),
        item(None, "b", "b", status="refused"),  # involves b only
        item(None, None, "a", status="error"),  # involves no class
    ]
    stats = consensus_stats(items, ("a", "b", "c"))
    assert stats.rate == pytest.approx(2 / 5)
    assert stats.by_class == {"a": pytest.approx(1 / 2), "b": pytest.approx(1 / 3), "c": 0.0}
    assert stats.kept_by_class == {"a": 1, "b": 1, "c": 0}
    assert consensus_stats([], ("a", "b")).rate is None


def test_retained_share_uses_the_uniform_slice_only() -> None:
    items = [item("a", "a", "a"), item("a", "b", "a"), item("b", "a", "a", reason="uncertainty")]
    assert retained_share(items) == pytest.approx(0.5)
    assert retained_share([item("a", "b", "a", reason="uncertainty")]) == 0.0


def test_family_overlap_lift_against_the_random_label_baseline() -> None:
    # The member always agrees with Sol; Sonnet agrees on half the items.
    items = [item("a", "a", "a"), item("b", "a", "b"), item("a", "b", "a"), item("b", "b", "b")]
    # lift(sol) = 1 - (0.5*0.5 + 0.5*0.5) = 0.5; lift(sonnet) = 0.5 - 0.5 = 0.0
    assert family_overlap_lift(items, ["m1"], [SOL, SONNET]) == pytest.approx(0.5)


def test_independent_member_has_no_lift_spread() -> None:
    items = [item("a", "a", "a"), item("b", "b", "a"), item("a", "a", "b"), item("b", "b", "b")]
    assert family_overlap_lift(items, ["m1"], [SOL, SONNET]) == pytest.approx(0.0)


def test_lift_without_evidence_is_one() -> None:
    assert family_overlap_lift([], ["m1"], [SOL, SONNET]) == 1.0
    assert family_overlap_lift([item("a", "a", "a")], ["m9"], [SOL, SONNET]) == 1.0


def test_lifecycle_edges() -> None:
    for current, target in [
        ("fitted", "accepted"),
        ("fitted", "rejected"),
        ("accepted", "active"),
        ("active", "downgraded"),
        ("active", "retired"),
    ]:
        check_transition(current, target)
    for current, target in [
        ("rejected", "active"),
        ("downgraded", "active"),
        ("fitted", "active"),
        ("retired", "active"),
        ("x", "y"),
    ]:
        with pytest.raises(InvalidTransition):
            check_transition(current, target)
    assert gate_status(True) == "accepted" and gate_status(False) == "rejected"


def test_two_person_activation() -> None:
    check_activation("op_a", ["op_a", "op_b"])
    with pytest.raises(InvalidTransition):
        check_activation("op_a", ["op_a", "op_a"])
    with pytest.raises(InvalidTransition):
        check_activation(None, ["op_a", "op_b"])
    with pytest.raises(InvalidTransition):
        check_activation("op_a", ["op_b", "op_c"])
