"""Small rendering helpers shared by the Converse and Mantle wires. Every random value comes from the request's
seeded draw (spec-10 §2.2 determinism rule), so ids, padding and signatures repeat with the scenario seed."""

from __future__ import annotations

import base64
import random
import string
import uuid
from typing import Any, Final

from .clock import MockClock

REASONING_FILLER: Final = "Reasoning step. "  # 16 characters = one planned reasoning delta (REASONING_CHARS)
STOP_TO_FINISH: Final = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}


def request_id(seq: int, draw: random.Random | None) -> str:
    """A request id (`x-amzn-RequestId`, `chatcmpl-...`): from the draw when resolved, else from the log seq."""
    if draw is None:
        return f"00000000-0000-4000-8000-{seq:012x}"
    return str(uuid.UUID(int=draw.getrandbits(128), version=4))


def reasoning_text(chars: int) -> str:
    """Deterministic reasoning text of `chars` characters."""
    reps = chars // len(REASONING_FILLER) + 1
    return (REASONING_FILLER * reps)[:chars]


def signature(draw: random.Random | None) -> str:
    """A Claude reasoning signature (opaque base64)."""
    raw = bytes(draw.getrandbits(8) for _ in range(48)) if draw is not None else b"\x00" * 48
    return base64.b64encode(raw).decode()


def pad_member(payload: dict[str, Any], draw: random.Random | None, n: int) -> dict[str, Any]:
    """The `pad` quirk: an unknown `"p"` member of n random a-z letters (parsers must ignore it)."""
    if n > 0:
        letters = string.ascii_lowercase
        payload["p"] = "".join(letters[draw.randrange(26)] if draw is not None else "a" for _ in range(n))
    return payload


def stamp(clock: MockClock, text: str) -> str:
    """The load-mode stamp (spec-10 §5): `«ts:<monotonic_ns>»` + the chunk."""
    return f"«ts:{clock.now_ns()}»{text}"
