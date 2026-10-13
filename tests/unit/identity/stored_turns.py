"""Turn rows in the orchestrator's stored format (requests/orchestrator.md item 7, `noema.turns.context.history`).

identity may not import `noema.turns`, so these are literal copies of what `noema.turns.persist.stored_content`
returns (checked against it when this fixture was written): `input_parts` is a flat list of `{"type": "text",
"text"}`, `{"type": "json", "json"}`, `{"type": "image" | "file", "file_id"}`; the redacted policy replaces each PII
match with `[REDACTED:<kind>]` and stores code-point spans over the user text = the text and JSON parts (compact
orjson) joined by "\\n", and over the output.
"""

from __future__ import annotations

from typing import Any, Final

# stored_content([text "Hello, mail me at bob@example.com", json {"order": 42, "mail": "eve@example.org"}],
#                "Sure, bob@example.com", "full", redact)
FULL: Final[dict[str, Any]] = {
    "content_policy": "full",
    "input_parts": [
        {"type": "text", "text": "Hello, mail me at bob@example.com"},
        {"type": "json", "json": {"order": 42, "mail": "eve@example.org"}},
    ],
    "output_text": "Sure, bob@example.com",
    "redactions": {"user": [], "assistant": []},
}

# The same input under the redacted policy.
REDACTED: Final[dict[str, Any]] = {
    "content_policy": "redacted",
    "input_parts": [
        {"type": "text", "text": "Hello, mail me at [REDACTED:pii_email]"},
        {"type": "json", "json": {"order": 42, "mail": "[REDACTED:pii_email]"}},
    ],
    "output_text": "Sure, [REDACTED:pii_email]",
    "redactions": {
        "user": [{"start": 18, "end": 38, "reason": "pii_email"}, {"start": 59, "end": 79, "reason": "pii_email"}],
        "assistant": [{"start": 6, "end": 26, "reason": "pii_email"}],
    },
}

METADATA_ONLY: Final[dict[str, Any]] = {
    "content_policy": "metadata_only",
    "input_parts": None,
    "output_text": None,
    "redactions": {"user": [], "assistant": []},
}


def text_turn(text: str, output: str) -> dict[str, Any]:
    """A full-policy turn with one text part."""
    return {
        "content_policy": "full",
        "input_parts": [{"type": "text", "text": text}],
        "output_text": output,
        "redactions": {"user": [], "assistant": []},
    }


# REDACTED as Postgres returns it: jsonb reorders object keys (shorter first), so the stored user offsets of the JSON
# part no longer point at its marker.
REDACTED_FROM_JSONB: Final[dict[str, Any]] = {
    **REDACTED,
    "input_parts": [
        {"type": "text", "text": "Hello, mail me at [REDACTED:pii_email]"},
        {"type": "json", "json": {"mail": "[REDACTED:pii_email]", "order": 42}},
    ],
}
