"""The `labeler@1` prompt (02 §5.5, IMPL-043, IMPL-086, spec-09 §5): blind, permuted, spotlighted; the request
parameters; the strict one-letter parse; the unanimous rule with its drop precedence."""

from __future__ import annotations

import pytest
from tests.fakes.config import example_snapshot

from noema.core.values import TextBlock, UsageRecord
from noema.labeling.labelers import (
    SYSTEM_V1,
    TAIL_V1,
    app_question,
    build_request,
    letter_order,
    parse_answer,
    question_from_wire,
    question_signature,
    wire_value,
)
from noema.labeling.records import LabelerResult, decide

pytestmark = pytest.mark.unit

CHOICE = {
    "type": "choice",
    "name": "department",
    "instructions": "Which team should handle this message?",
    "choices": [
        {"value": "billing", "description": "Payments, refunds, invoices"},
        {"value": "technical", "description": "Bugs and errors"},
        {"value": "shipping", "description": "Delivery problems"},
        {"value": "other", "description": None},
    ],
}
PREDICATE = {
    "type": "predicate",
    "name": "needs_human",
    "instructions": "The customer asks for a <human>.",
    "criteria": {"true": "Asks for a person", "false": "No such request"},
}
SCORE = {
    "type": "score",
    "name": "complexity",
    "instructions": "How hard?",
    "levels": [{"label": "low"}, {"label": "high"}],
}


def test_question_signature_matches_spec06_vector() -> None:
    assert question_signature(CHOICE) == "sha256:734dae2685c4f2a89e350ac3cb8a74f58f9c67d32264a5cee3cd87eebce9455a"


def test_parse_wire_questions() -> None:
    q = question_from_wire(CHOICE)
    assert q.kind == "choice" and q.keys == ("billing", "technical", "shipping", "other")
    p = question_from_wire(PREDICATE)
    assert (
        p.kind == "predicate" and p.keys == ("true", "false") and p.criteria == ("Asks for a person", "No such request")
    )
    assert question_from_wire({**PREDICATE, "type": "noul"}).kind == "predicate"
    assert question_from_wire(SCORE).keys == ("low", "high")
    for bad in (
        {"type": "choice", "name": "best", "instructions": "x", "dynamic_choices": "candidates"},
        {"type": "choice", "name": "x", "instructions": "x", "choices": [{"value": "a"}, {"value": "a"}]},
        {"type": "mystery", "name": "x", "instructions": "x"},
        {"type": "score", "instructions": "x", "levels": []},
    ):
        with pytest.raises((ValueError, KeyError)):
            question_from_wire(bad)


def test_only_choice_options_are_permuted_per_labeler() -> None:
    q = question_from_wire(CHOICE)
    sol = letter_order(q, "dec_01J9ZQ6M3K8V", "us.openai.gpt-6.1-sol")
    sonnet = letter_order(q, "dec_01J9ZQ6M3K8V", "us.anthropic.claude-sonnet-5-5")
    assert sorted(sol) == [0, 1, 2, 3] and sol == letter_order(q, "dec_01J9ZQ6M3K8V", "us.openai.gpt-6.1-sol")
    assert sol != sonnet or sol != letter_order(q, "dec_other", "us.openai.gpt-6.1-sol")
    assert letter_order(question_from_wire(PREDICATE), "dec_1", "m") == [0, 1]  # A = true, B = false
    assert letter_order(question_from_wire(SCORE), "dec_1", "m") == [0, 1]  # ordinal order kept


def test_app_question_is_json_escaped() -> None:
    aq = app_question(question_from_wire(PREDICATE), [0, 1])
    assert "<" not in aq and ">" not in aq and "\\u003chuman\\u003e" in aq
    assert '"criteria": {"A": "Asks for a person", "B": "No such request"}' in aq
    assert aq.startswith('{"instructions": "The customer asks for a \\u003chuman\\u003e."')


def test_request_is_blind_static_and_never_retried() -> None:
    snapshot = example_snapshot()
    q = question_from_wire(CHOICE)
    blocks = (TextBlock("hello\n"),)
    sol = build_request(q, [2, 0, 1, 3], blocks, alias="sol-6.1", model=snapshot.model("sol-6.1"), snapshot=snapshot)
    assert sol.system == (SYSTEM_V1,) and sol.call_kind == "labeling" and sol.retry == "none"
    assert sol.temperature is None and sol.effort == "medium" and sol.max_tokens == 16 + 1024
    content = sol.messages[0].content
    assert isinstance(content[0], TextBlock) and content[0].text.startswith('<app_question>{"instructions"')
    assert content[1] == TextBlock("<untrusted_input>\n") and content[2] == blocks[0]
    assert content[-1] == TextBlock("\n</untrusted_input>\n" + TAIL_V1)
    assert '"letter": "A", "value": "shipping"' in content[0].text
    sonnet = build_request(
        q, [0, 1, 2, 3], blocks, alias="sonnet-5.5", model=snapshot.model("sonnet-5.5"), snapshot=snapshot
    )
    assert sonnet.effort is None and sonnet.max_tokens == 16  # no effort list, no reasoning (IMPL-043)
    prompt = "".join(b.text for b in sol.messages[0].content if isinstance(b, TextBlock))
    assert "probability" not in prompt and "candidates" not in prompt  # blind: nothing of the ensemble's answer


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B", "billing"),
        (" b ", None),
        ("(B).", "billing"),
        ('"C"', "technical"),
        ("**A**", "shipping"),
        ("<|channel|>final<|message|>D<|end|>", "other"),
        ("E", None),
        ("B or C", None),
        ("The answer is B", None),
        ("", None),
    ],
)
def test_parse_one_letter(text: str, expected: str | None) -> None:
    q = question_from_wire(CHOICE)
    assert parse_answer(text, q, [2, 0, 1, 3]) == expected


def test_wire_values() -> None:
    p, c = question_from_wire(PREDICATE), question_from_wire(CHOICE)
    assert wire_value(p, "true") is True and wire_value(p, "false") is False
    assert wire_value(c, "billing") == "billing" and wire_value(c, None) is None


def result(status: str, key: str | None = None) -> LabelerResult:
    return LabelerResult("a", "m", status, key, "native", usage=UsageRecord(1, 1))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([("ok", "x"), ("ok", "x")], ("x", None)),
        ([("ok", "x"), ("ok", "y")], (None, "disagreed")),
        ([("ok", "x"), ("refused", None)], (None, "refused")),
        ([("parse_error", None), ("ok", "x")], (None, "parse_error")),
        ([("error", None), ("refused", None)], (None, "labeler_error")),
        ([("refused", None), ("parse_error", None)], (None, "refused")),
        ([("ok", "x")], (None, "disagreed")),  # unanimous needs at least two labelers
    ],
)
def test_unanimous_rule(statuses: list[tuple[str, str | None]], expected: tuple[str | None, str | None]) -> None:
    assert decide([result(s, k) for s, k in statuses]) == expected
