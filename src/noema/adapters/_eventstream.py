"""A ConverseStream eventstream decoder (Wave E3): the messages botocore's `EventStreamBuffer` yields for the same
bytes, however they are chunked, at about a third of its CPU (E sprint A: the buffer was ~15 us per event under
the profiler; tests/unit/adapters/test_eventstream.py compares both on every stream body of the equivalence suite,
the wire mock's streams and fuzzed, corrupt and split frames).

A well-formed message (prelude CRC, lengths within botocore's limits, `total_length >= headers_length + 16`,
message CRC, string headers) is decoded here: `struct.unpack_from` and `zlib.crc32` over a `memoryview` on one
bytes buffer with a read position, instead of botocore's slice-and-copy per field, its `MessagePrelude` and
`EventStreamMessage` objects; a header block seen before in the stream is not parsed again. Any other header block
(another header type, a truncated or repeated header, an undecodable name) is parsed by botocore's own
`EventStreamHeaderParser` (its result or its exception). A corrupt CRC or an over-long or inconsistent prelude
hands the unread bytes to a botocore `EventStreamBuffer` for the rest of the stream. So every stream yields what
botocore yields and raises what botocore raises (`ChecksumMismatch`, `InvalidHeadersLength`, `InvalidPayloadLength`,
`DuplicateHeader`, ...) at the same point; a partial message at the end is never yielded.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping
from typing import Any, Final, cast

from botocore.eventstream import EventStreamBuffer, EventStreamHeaderParser

PRELUDE_LENGTH: Final = 12
MAX_HEADERS_LENGTH: Final = 128 * 1024  # botocore.eventstream._MAX_HEADERS_LENGTH
MAX_PAYLOAD_LENGTH: Final = 24 * 1024 * 1024  # botocore.eventstream._MAX_PAYLOAD_LENGTH
STRING: Final = 7
CACHED_BLOCKS: Final = 16  # distinct string header blocks remembered per stream (an event type each, typically)
CACHED_BLOCK_BYTES: Final = 256
_PRELUDE: Final = struct.Struct("!III")
_U32: Final = struct.Struct("!I")
Message = tuple[Mapping[str, Any], bytes]  # (headers, payload), as EventStreamMessage.headers / .payload


def _string_headers(data: bytes) -> dict[str, Any] | None:
    """The header block `data` when every header is a string (type 7) that fits in it and no name repeats; None
    otherwise (botocore's parser then decides)."""
    headers: dict[str, Any] = {}
    pos, end = 0, len(data)
    while pos < end:
        name_end = pos + 1 + data[pos]
        if name_end + 3 > end or data[name_end] != STRING:
            return None
        value_start = name_end + 3
        value_end = value_start + (data[name_end + 1] << 8 | data[name_end + 2])
        if value_end > end:
            return None
        name = data[pos + 1 : name_end].decode("utf-8")
        if name in headers:
            return None
        headers[name] = data[value_start:value_end].decode("utf-8")
        pos = value_end
    return headers


class EventStreamDecoder:
    """`add_data(bytes)` as the body arrives, then `next()` until None: each complete message as (headers,
    payload); botocore's exceptions for a malformed stream (module docstring)."""

    __slots__ = ("_blocks", "_data", "_fallback", "_pos", "_prelude")

    def __init__(self) -> None:
        self._data = b""
        self._pos = 0  # where the next message starts in `_data`
        self._prelude: tuple[int, int, int] | None = None  # the validated prelude of the message at `_pos`
        self._fallback: EventStreamBuffer | None = None
        self._blocks: dict[bytes, dict[str, Any]] = {}  # header block -> its headers (a stream repeats a few blocks)

    def add_data(self, data: bytes) -> None:
        if self._fallback is not None:
            self._fallback.add_data(data)
        elif self._pos >= len(self._data):
            self._data, self._pos = bytes(data), 0
        else:
            self._data, self._pos = self._data[self._pos :] + data, 0

    def next(self) -> Message | None:
        """The next complete message, None until more data arrives."""
        if self._fallback is not None:
            return self._next_fallback()
        data, pos = self._data, self._pos
        available = len(data) - pos
        if available < PRELUDE_LENGTH:
            return None
        prelude = self._prelude
        if prelude is None:
            total, headers_length, prelude_crc = prelude = _PRELUDE.unpack_from(data, pos)
            if (
                zlib.crc32(memoryview(data)[pos : pos + 8]) != prelude_crc
                or headers_length > MAX_HEADERS_LENGTH
                or not 0 <= total - headers_length - 16 <= MAX_PAYLOAD_LENGTH
            ):
                return self._delegate()
            self._prelude = prelude
        else:
            total, headers_length, prelude_crc = prelude
        if available < total:
            return None
        end = pos + total
        if zlib.crc32(memoryview(data)[pos + 8 : end - 4], prelude_crc) != _U32.unpack_from(data, end - 4)[0]:
            return self._delegate()
        headers_end = pos + PRELUDE_LENGTH + headers_length
        block = data[pos + PRELUDE_LENGTH : headers_end]
        cached = self._blocks.get(block)
        if cached is not None:
            headers: dict[str, Any] | None = dict(cached)
        else:
            try:
                headers = _string_headers(block)
            except UnicodeDecodeError:
                headers = None
            if headers is not None and len(block) <= CACHED_BLOCK_BYTES and len(self._blocks) < CACHED_BLOCKS:
                self._blocks[block] = dict(headers)
        if headers is None:  # another header type, or a malformed block: botocore's parser (its result or exception)
            headers = EventStreamHeaderParser().parse(block)
        self._pos, self._prelude = end, None
        return headers, data[headers_end : end - 4]

    def _delegate(self) -> Message | None:
        """Hand the unread bytes to botocore's buffer for the rest of the stream."""
        self._fallback = EventStreamBuffer()
        self._fallback.add_data(self._data[self._pos :])
        self._data, self._pos, self._prelude = b"", 0, None
        return self._next_fallback()

    def _next_fallback(self) -> Message | None:
        assert self._fallback is not None
        try:
            message = self._fallback.next()
        except StopIteration:
            return None
        return cast("Mapping[str, Any]", message.headers), message.payload
