"""Shared by the Wave E3 decoder tests: run botocore's `EventStreamBuffer` and noema's `EventStreamDecoder` over the
same chunks and record what each yields (headers, payload) and the exception that ends it (type and args, then
whether the next call raises it again), so a test compares the two outcomes for equality."""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

from botocore.eventstream import EventStreamBuffer

from noema.adapters._eventstream import EventStreamDecoder

Outcome = tuple[list[tuple[dict[str, Any], bytes]], tuple[str, tuple[Any, ...], bool] | None]


def _error(exc: BaseException, again: bool) -> tuple[str, tuple[Any, ...], bool]:
    return type(exc).__name__, tuple(str(a) for a in exc.args), again


def botocore_outcome(chunks: Sequence[bytes]) -> Outcome:
    buf = EventStreamBuffer()
    out: list[tuple[dict[str, Any], bytes]] = []
    for chunk in chunks:
        buf.add_data(chunk)
        try:
            out.extend((dict(m.headers), m.payload) for m in buf)  # type: ignore[call-overload]  # stubs: an int
        except Exception as exc:
            try:
                next(buf)
                again = False
            except StopIteration:
                again = False
            except Exception as repeat:  # botocore raises a few types (struct.error, KeyError, ...)
                again = type(repeat) is type(exc)
            return out, _error(exc, again)
    return out, None


def noema_outcome(chunks: Sequence[bytes]) -> Outcome:
    decoder = EventStreamDecoder()
    out: list[tuple[dict[str, Any], bytes]] = []
    for chunk in chunks:
        decoder.add_data(chunk)
        try:
            while (message := decoder.next()) is not None:
                out.append((dict(message[0]), message[1]))
        except Exception as exc:
            try:
                decoder.next()
                again = False
            except Exception as repeat:
                again = type(repeat) is type(exc)
            return out, _error(exc, again)
    return out, None


def chunkings(data: bytes, rng: random.Random | None = None) -> list[list[bytes]]:
    """The body whole, in 1, 7 and 64 byte chunks, and (with `rng`) split at random points."""
    out = [[data] if data else [], *([data[i : i + n] for i in range(0, len(data), n)] for n in (1, 7, 64))]
    if rng is not None and len(data) > 1:
        cuts = sorted(rng.sample(range(1, len(data)), min(len(data) - 1, rng.randrange(1, 12))))
        out.append([data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)], strict=True)])
    return out


def assert_same(data: bytes, rng: random.Random | None = None) -> Outcome:
    """Both decoders agree on every chunking of `data`; returns botocore's outcome of the whole body."""
    for chunks in chunkings(data, rng):
        expected = botocore_outcome(chunks)
        assert noema_outcome(chunks) == expected, (data[:200], [len(c) for c in chunks][:20])
    return botocore_outcome([data])
