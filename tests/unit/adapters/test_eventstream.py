"""Wave E3: `noema.adapters._eventstream.EventStreamDecoder` yields the messages (headers, payload) botocore's
`EventStreamBuffer` yields and raises what it raises, at the same point, for every stream body of the lean
ConverseStream equivalence suite (test_lean_converse_stream.CASES) and fuzzed streams: frames with every header type
(and unknown ones), duplicate and undecodable names, corrupt CRCs, truncations, garbage, and preludes whose lengths
are inconsistent or over botocore's limits but carry a valid CRC; each body whole, in 1, 7 and 64 byte chunks and
split at random points. The wire mock's streams: test_eventstream_mock.py."""

from __future__ import annotations

import random
import struct
import zlib

import pytest
from tests.unit.adapters.eventstream_compare import assert_same, noema_outcome
from tests.unit.adapters.test_lean_converse_stream import CASES

from noema.adapters._eventstream import MAX_HEADERS_LENGTH, MAX_PAYLOAD_LENGTH


@pytest.mark.parametrize("name", list(CASES))
def test_every_equivalence_suite_body_decodes_as_botocore_decodes_it(name: str) -> None:
    assert_same(b"".join(CASES[name]), random.Random(name))


def _value(rng: random.Random, kind: int) -> bytes:
    sizes = {2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
    if kind in (0, 1):
        return b""
    if kind in sizes:
        return rng.randbytes(sizes[kind])
    if kind in (6, 7):  # byte array / string (rarely undecodable)
        raw = rng.choice([b"", b"application/json", b"contentBlockDelta", "é漢\U0001f600".encode()])
        if kind == 6 or rng.random() < 0.03:
            raw = rng.randbytes(rng.randrange(0, 40))
        return struct.pack("!H", len(raw)) + raw
    return rng.randbytes(rng.randrange(0, 6))  # an unknown type: botocore raises KeyError


def _headers(rng: random.Random) -> bytes:
    names = [b":event-type", b":content-type", b":message-type", b":exception-type", b"x", "名".encode()]
    out = b""
    chosen = rng.sample(names, rng.randrange(0, 5))
    if chosen and rng.random() < 0.05:
        chosen.append(chosen[0])  # a repeated name
    for name in chosen:
        if rng.random() < 0.03:
            name = rng.randbytes(rng.randrange(0, 6))  # may not be utf-8
        kind = 7 if rng.random() < 0.7 else rng.choice([*range(10), 10, 200]) if rng.random() < 0.9 else 7
        out += bytes([len(name)]) + name + bytes([kind]) + _value(rng, kind)
    return out


def frame(headers: bytes, payload: bytes, *, total: int | None = None, headers_length: int | None = None) -> bytes:
    """One message; `total` / `headers_length` override the prelude's fields (both CRCs stay valid)."""
    total = 12 + len(headers) + len(payload) + 4 if total is None else total
    prelude = struct.pack("!II", total, len(headers) if headers_length is None else headers_length)
    head = prelude + struct.pack("!I", zlib.crc32(prelude))
    body = head + headers + payload
    return body + struct.pack("!I", zlib.crc32(body))


def _mutated(rng: random.Random, data: bytes) -> bytes:
    roll = rng.randrange(7)
    if roll == 0 and data:  # flip a bit (a CRC mismatch, or a length the next prelude reads)
        i = rng.randrange(len(data))
        return data[:i] + bytes([data[i] ^ (1 << rng.randrange(8))]) + data[i + 1 :]
    if roll == 1:  # truncated
        return data[: rng.randrange(len(data) + 1)]
    if roll == 2:  # garbage
        return rng.randbytes(rng.randrange(1, 30))
    headers, payload = _headers(rng), rng.randbytes(rng.randrange(0, 40))
    natural = 16 + len(headers) + len(payload)
    if roll == 3:  # a total that disagrees with the content (valid CRCs)
        return frame(headers, payload, total=rng.choice([0, 3, 12, 15, 16, natural - 1, natural + 1, natural + 9]))
    if roll == 4:  # a headers length that disagrees with the block (valid CRCs)
        return frame(headers, payload, headers_length=rng.choice([0, 1, max(0, len(headers) - 1), len(headers) + 2]))
    if roll == 5:  # over botocore's limits (valid prelude CRC; the rest never arrives)
        return frame(b"", b"", total=MAX_PAYLOAD_LENGTH + 17 + rng.randrange(2), headers_length=0)[:12]
    return frame(b"", b"", total=2 * MAX_HEADERS_LENGTH, headers_length=MAX_HEADERS_LENGTH + 1)[:12]


def fuzzed_stream(rng: random.Random) -> bytes:
    parts = []
    for _ in range(rng.randrange(1, 9)):
        valid = frame(_headers(rng), rng.randbytes(rng.randrange(0, 80)))
        parts.append(valid if rng.random() < 0.85 else _mutated(rng, valid))
    return b"".join(parts)


@pytest.mark.parametrize("seed", range(6))
def test_fuzzed_streams_decode_and_fail_as_botocore_does(seed: int) -> None:
    rng = random.Random(seed)
    outcomes = [assert_same(fuzzed_stream(rng), rng)[1] for _ in range(300)]
    failures = {o[0] for o in outcomes if o is not None}
    assert {"ChecksumMismatch", "KeyError"} <= failures  # the fuzz reaches botocore's failure paths


@pytest.mark.parametrize(
    "data",
    [
        frame(b"", b"", total=MAX_PAYLOAD_LENGTH + 17, headers_length=0)[:12],  # InvalidPayloadLength
        frame(b"", b"", total=MAX_HEADERS_LENGTH * 2, headers_length=MAX_HEADERS_LENGTH + 1)[:12],  # headers
        frame(b"\x01a\x07\x00\x01b\x01a\x07\x00\x01c", b"{}"),  # DuplicateHeader
        frame(b"\x02\xff\xfe\x07\x00\x00", b"{}"),  # an undecodable header name
        frame(b"\x01a\x0c", b""),  # an unknown header type
        frame(b"\x01a\x07\x00\x09ab", b""),  # a string value longer than the block (botocore keeps the rest)
        frame(b"\x01a\x00\x01b\x05" + bytes(8) + b"\x01c\x09" + bytes(16), b"payload"),  # bool, long, uuid
        frame(b"", b"x", total=5),  # total below the prelude
    ],
    ids=["payload_limit", "headers_limit", "duplicate", "bad_name", "unknown_type", "long_value", "types", "total5"],
)
def test_edge_frames_decode_and_fail_as_botocore_does(data: bytes) -> None:
    assert_same(data + frame(b"\x01a\x07\x00\x01b", b"{}"), random.Random(len(data)))


def test_a_partial_message_is_never_yielded_and_valid_ones_follow_each_other() -> None:
    one, two = frame(b"\x01a\x07\x00\x01b", b"{1}"), frame(b"\x01a\x07\x00\x01c", b"{2}")
    assert noema_outcome([one + two[:-1]]) == ([({"a": "b"}, b"{1}")], None)
    assert noema_outcome([one + two[:-1], two[-1:]]) == ([({"a": "b"}, b"{1}"), ({"a": "c"}, b"{2}")], None)
