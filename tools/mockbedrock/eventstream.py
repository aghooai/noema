"""AWS eventstream binary framing (`application/vnd.amazon.eventstream`, spec-10 §2.1a).

One message, every integer big-endian:

    [total_length u32][headers_length u32][prelude_crc u32 = CRC32(first 8 bytes)]
    [headers: { name_len u8, name, type u8 = 7 (string), value_len u16, value }*]
    [payload][message_crc u32 = CRC32(every preceding byte)]

The header block of each event type is encoded once and cached, so a per-delta frame costs one `orjson.dumps`,
two `struct.pack` calls and two running CRC32 updates.
"""

from __future__ import annotations

import struct
import zlib
from functools import cache
from typing import Any, Final, Literal

import orjson

PRELUDE_LEN: Final = 12
CRC_LEN: Final = 4
STRING_HEADER: Final = 7
CONTENT_TYPE: Final = "application/vnd.amazon.eventstream"
# The exception types ConverseStream declares (botocore's ConverseStreamOutput members).
EXCEPTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "internalServerException",
        "modelStreamErrorException",
        "validationException",
        "throttlingException",
        "serviceUnavailableException",
    }
)
Corruption = Literal["prelude_crc", "message_crc"]


def encode_headers(headers: tuple[tuple[str, str], ...]) -> bytes:
    """The header block for string-valued headers (type 7), in order."""
    out = bytearray()
    for name, value in headers:
        n, v = name.encode(), value.encode()
        if len(n) > 255 or len(v) > 65535:
            raise ValueError(f"eventstream header {name!r} is too long")
        out += struct.pack(">B", len(n)) + n + struct.pack(">BH", STRING_HEADER, len(v)) + v
    return bytes(out)


def encode_message(header_block: bytes, payload: bytes, *, corrupt: Corruption | None = None) -> bytes:
    """One framed message. `corrupt` flips one bit of the prelude or the message CRC (malformed-frame faults)."""
    total = PRELUDE_LEN + len(header_block) + len(payload) + CRC_LEN
    prelude = struct.pack(">II", total, len(header_block))
    prelude_crc = zlib.crc32(prelude)
    head = prelude + struct.pack(">I", prelude_crc ^ (1 if corrupt == "prelude_crc" else 0))
    crc = zlib.crc32(payload, zlib.crc32(header_block, zlib.crc32(head)))
    return head + header_block + payload + struct.pack(">I", crc ^ (1 if corrupt == "message_crc" else 0))


@cache
def event_headers(event_type: str) -> bytes:
    """The cached header block of an `event` message."""
    return encode_headers(
        ((":event-type", event_type), (":content-type", "application/json"), (":message-type", "event"))
    )


@cache
def exception_headers(exception_type: str) -> bytes:
    """The cached header block of an `exception` message."""
    return encode_headers(
        (
            (":exception-type", exception_type),
            (":content-type", "application/json"),
            (":message-type", "exception"),
        )
    )


def event_frame(event_type: str, payload: Any, *, corrupt: Corruption | None = None) -> bytes:
    """An event message with a JSON payload."""
    return encode_message(event_headers(event_type), orjson.dumps(payload), corrupt=corrupt)


def raw_event_frame(event_type: str, payload: bytes) -> bytes:
    """An event message with verbatim payload bytes (valid CRCs; used for the truncated-JSON fault)."""
    return encode_message(event_headers(event_type), payload)


def exception_frame(exception_type: str, payload: Any) -> bytes:
    """An exception message (`:message-type: exception`); the stream ends after it. A type outside
    `EXCEPTION_TYPES` is sent as given (botocore then reports an empty error code)."""
    return encode_message(exception_headers(exception_type), orjson.dumps(payload))


def exception_type_of(name: str) -> str:
    """Normalize a scenario exception name (`ModelStreamErrorException` or `modelStreamErrorException`)."""
    return name[:1].lower() + name[1:]
