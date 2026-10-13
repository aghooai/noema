"""`noema.identity.history` over turn rows in the orchestrator's stored format (listSessionTurns, IMPL-040)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from tests.unit.identity.stored_turns import FULL, METADATA_ONLY, REDACTED, REDACTED_FROM_JSONB, text_turn

from noema.identity.history import redact_spans, turn_messages, user_text

AT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def messages(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", turn_messages(turn))


def row(content: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "turn_1", "seq": 1, "created_at": AT, "completed_at": AT, **content, **extra}


def test_full_turn_reads_text_and_json_parts_in_order() -> None:
    user, assistant = messages(row(FULL))
    assert user["text"] == 'Hello, mail me at bob@example.com\n{"order":42,"mail":"eve@example.org"}'
    assert user["redactions"] == [] and user["role"] == "user" and user["seq"] == 1
    assert assistant["text"] == "Sure, bob@example.com" and assistant["role"] == "assistant"


def test_media_parts_have_no_text_and_order_is_kept() -> None:
    parts = [
        {"type": "image", "file_id": "file_1"},
        {"type": "text", "text": "first"},
        {"type": "file", "file_id": "file_2"},
        {"type": "json", "json": [1, "two", None]},
        {"type": "text", "text": "last"},
    ]
    assert user_text(parts) == 'first\n[1,"two",null]\nlast'
    assert user_text([{"type": "image", "file_id": "file_1"}]) == ""
    assert user_text(None) == "" and user_text("text") == "" and user_text([1, None]) == ""


def test_redacted_turn_reads_redacted_markers_with_recomputed_spans() -> None:
    """The stored `[REDACTED:pii_email]` markers read "[redacted]" (contract) and the spans point at them."""
    user, assistant = messages(row(REDACTED))
    assert user["text"] == 'Hello, mail me at [redacted]\n{"order":42,"mail":"[redacted]"}'
    assert user["redactions"] == [
        {"start": 18, "end": 28, "reason": "pii_email"},
        {"start": 49, "end": 59, "reason": "pii_email"},
    ]
    assert assistant["text"] == "Sure, [redacted]"
    assert assistant["redactions"] == [{"start": 6, "end": 16, "reason": "pii_email"}]
    for message in (user, assistant):
        for span in message["redactions"]:
            assert message["text"][span["start"] : span["end"]] == "[redacted]"


def test_redacted_spans_survive_the_jsonb_key_reordering() -> None:
    """jsonb reorders object keys, so the stored offsets of a JSON part are stale: the spans come from the markers."""
    user, _ = messages(row(REDACTED_FROM_JSONB))
    assert user["text"] == 'Hello, mail me at [redacted]\n{"mail":"[redacted]","order":42}'
    assert user["redactions"] == [
        {"start": 18, "end": 28, "reason": "pii_email"},
        {"start": 38, "end": 48, "reason": "pii_email"},
    ]


def test_a_marker_typed_into_a_full_turn_is_text() -> None:
    user, assistant = messages(row(text_turn("I typed [REDACTED:pii_email] myself", "ok")))
    assert user["text"] == "I typed [REDACTED:pii_email] myself" and user["redactions"] == []
    assert assistant["text"] == "ok"


def test_spans_are_code_points() -> None:
    text = "\U0001f600 hi [REDACTED:pii_phone] ok"
    out, spans = redact_spans(text, [{"start": 5, "end": 25, "reason": "pii_phone"}])
    assert out == "\U0001f600 hi [redacted] ok" and spans == [{"start": 5, "end": 15, "reason": "pii_phone"}]


def test_malformed_out_of_range_and_overlapping_spans_are_dropped() -> None:
    text = "abcdefghij"
    stored: list[object] = [
        {"start": 6, "end": 8, "reason": "b"},
        {"start": 1, "end": 3, "reason": "a"},
        {"start": 2, "end": 5, "reason": "overlap"},
        {"start": 8, "end": 11, "reason": "out_of_range"},
        {"start": True, "end": 2, "reason": "bool"},
        {"start": 0, "end": 1},
        "junk",
    ]
    out, spans = redact_spans(text, stored)
    assert out == "a[redacted]def[redacted]ij"
    assert spans == [{"start": 1, "end": 11, "reason": "a"}, {"start": 14, "end": 24, "reason": "b"}]
    assert redact_spans(text, None) == (text, []) and redact_spans(text, "x") == (text, [])


def test_metadata_only_reads_redacted() -> None:
    user, assistant = messages(row(METADATA_ONLY, completed_at=None))
    for message in (user, assistant):
        assert message["text"] == "[redacted]"
        assert message["redactions"] == [{"start": 0, "end": 10, "reason": "metadata_only"}]
    assert assistant["created_at"] == 0 and user["created_at"] == int(AT.timestamp())
