"""History selection and the decision context (IMPL-045), the budget formulas (IMPL-084), and the stored content
under the content policies (IMPL-018, IMPL-029e)."""

from __future__ import annotations

import pytest
from tests.fakes.config import example_snapshot

from noema.core.protocols.media import ContentPart
from noema.telemetry.redact import redact
from noema.turns.context.budget import (
    attempt_max_tokens,
    candidate_profiles,
    context_budget,
    profile_usable,
)
from noema.turns.context.history import committed_turns, decision_context, select_history
from noema.turns.persist import line_id, stored_content, usage_sum, usage_wire
from noema.turns.request import HistoryOptions

pytestmark = pytest.mark.unit


def _row(seq: int, user: str, out: str | None, *, parts: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "id": f"turn_{seq}",
        "seq": seq,
        "input_parts": parts or [{"type": "text", "text": user}],
        "output_text": out,
    }


def test_committed_turns_skip_metadata_only_and_empty_answers() -> None:
    rows = [
        _row(5, "u5", "a5"),
        {"id": "turn_4", "seq": 4, "input_parts": None, "output_text": None},
        _row(3, "u3", ""),
        _row(2, "u2", "a2"),
    ]
    assert [t.seq for t in committed_turns(rows)] == [5, 2]


def test_select_history_newest_first_within_budget_chronological_out() -> None:
    turns = committed_turns([_row(i, "u" * 40, "a" * 40) for i in (9, 8, 7, 6)])  # 20 tokens each
    assert [t.seq for t in select_history(turns, HistoryOptions(), 60)] == [7, 8, 9]
    assert [t.seq for t in select_history(turns, HistoryOptions(max_tokens=40), 1000)] == [8, 9]
    assert [t.seq for t in select_history(turns, HistoryOptions(mode="last_n", last_n=1), 1000)] == [9]
    assert select_history(turns, HistoryOptions(mode="none"), 1000) == ()
    assert select_history(turns, HistoryOptions(), 0) == ()


def test_decision_context_last_two_turns_cut_to_2000_tokens() -> None:
    turns = committed_turns([_row(3, "q3", "a3"), _row(2, "q2", "a2"), _row(1, "q1", "a1")])
    assert decision_context(turns) == (("q2", "a2"), ("q3", "a3"))
    big = committed_turns([_row(2, "x" * 6000, "y" * 4000), _row(1, "old", "old")])
    ((user, assistant),) = decision_context(big)  # the oldest dropped first, then the newest cut from its start
    assert len(user) + len(assistant) == 8000 and assistant == "y" * 4000 and user == "x" * 4000


def test_budget_formulas_over_candidate_members() -> None:
    snap = example_snapshot()
    profiles = candidate_profiles(snap, spec0_profile="sol-fast", pinned=None)
    assert profiles == ("glm-reason", "opus-deep", "sol-fast")  # race / best_of_n are unreachable without a pin
    assert candidate_profiles(snap, spec0_profile="sol-fast", pinned="race-sol-glm") == ("race-sol-glm", "sol-fast")
    budget = context_budget(snap, profiles, None)
    assert budget.context_window == 128_000 and budget.reserve == 6400
    assert budget.max_output == 4096  # glm cap 4096; sol/opus 4096 default vs binding 4000/600
    assert budget.available(1000) == 128_000 - 4096 - 6400 - 1000
    glm = snap.model("glm-5.3")
    assert attempt_max_tokens(glm, None, 4096) == 4096 and attempt_max_tokens(snap.model("sol-6.1"), 600, None) == 600
    assert profile_usable(snap, "sol-fast", allowed_models=None, est_input=1000, spec_max=None)
    assert not profile_usable(snap, "sol-fast", allowed_models=("opus-5.5",), est_input=1000, spec_max=None)
    assert not profile_usable(snap, "sol-fast", allowed_models=None, est_input=125_000, spec_max=None)
    assert not profile_usable(snap, "best-of-sol-opus", allowed_models=None, est_input=1, spec_max=None)
    assert not profile_usable(snap, "missing", allowed_models=None, est_input=1, spec_max=None)


def test_stored_content_per_policy_with_redaction_spans() -> None:
    parts = (
        ContentPart(0, "text", None, text="mail me at a@b.example"),
        ContentPart(1, "json", None, json={"card": "4111 1111 1111 1111"}),
    )
    full, out, red = stored_content(parts, "ok a@b.example", "full", redact)
    assert full == [
        {"type": "text", "text": "mail me at a@b.example"},
        {"type": "json", "json": {"card": "4111 1111 1111 1111"}},
    ]
    assert out == "ok a@b.example" and red == {"user": [], "assistant": []}
    stored, out2, spans = stored_content(parts, "ok a@b.example", "redacted", redact)
    assert stored is not None and "a@b.example" not in str(stored) and "4111" not in str(stored)
    assert out2 == "ok [REDACTED:pii_email]"
    assert spans["assistant"] == [{"start": 3, "end": 23, "reason": "pii_email"}]
    user_spans = spans["user"]
    assert isinstance(user_spans, list)
    assert [s["reason"] for s in user_spans if isinstance(s, dict)] == ["pii_email", "pii_card"]
    assert stored_content(parts, "x", "metadata_only", redact) == (None, None, {"user": [], "assistant": []})


def test_usage_wire_sum_and_line_ids() -> None:
    from decimal import Decimal

    from noema.core.values import Cost, UsageRecord

    a = usage_wire(UsageRecord(10, 5), Cost(Decimal("0.000123"), False))
    b = usage_wire(UsageRecord(1, 2, reasoning_tokens=1, tokens_estimated=True), Cost(Decimal("0.000001"), True))
    total = usage_sum([a, b])
    assert total["input_tokens"] == 11 and total["output_tokens"] == 7 and total["reasoning_tokens"] == 1
    assert total["cost_usd"] == 0.000124 and total["cost_estimated"] is True and total["tokens_estimated"] is True
    assert line_id("att_1", "generation") == line_id("att_1", "generation") != line_id("att_1", "waste")
    assert len(line_id("x")) == 26
