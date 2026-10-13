"""Identifier generation (spec-02 §1).

Ids are `f"{prefix}_{ULID}"`: a 26-character Crockford base32 ULID, monotonic within one process. API key ids are
bare ULIDs (IMPL-008), usage-line ids are deterministic hashes (spec-11 §2) and tenant ids are config keys, so
none of those come from `IdGen`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Literal, Protocol

from noema.core.clock import Clock

IdPrefix = Literal[
    "turn",
    "dec",
    "att",
    "ses",
    "file",
    "der",
    "prj",
    "prn",
    "op",
    "cal",
    "lrun",
    "lab",
    "ldrop",
    "job",
    "req",
    "sbx",
    "aud",
    "idem",
]

CROCKFORD32: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1
_TIME_MAX = (1 << 48) - 1


class IdGen(Protocol):
    """Generates prefixed ULIDs."""

    def new(self, prefix: IdPrefix) -> str:
        """Return `f"{prefix}_{ULID}"`; sync, never raises."""
        ...


def encode_ulid(value: int) -> str:
    """Encode a 128-bit integer as 26 Crockford base32 characters (most significant first)."""
    if not 0 <= value < (1 << 128):
        raise ValueError("a ULID is a 128-bit unsigned integer")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD32[value & 31])
        value >>= 5
    return "".join(reversed(chars))


class UlidIdGen:
    """The process `IdGen`: 48-bit millisecond time from `clock.now()` plus 80 random bits.

    Monotonic per instance: an id generated in the same (or an earlier) millisecond as the previous one reuses the
    previous time and increments the previous random part, so ids sort in generation order. Thread-safe.
    """

    def __init__(self, clock: Clock, *, randbytes: Callable[[int], bytes] = os.urandom) -> None:
        self._clock = clock
        self._randbytes = randbytes
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_rand = 0

    def new(self, prefix: IdPrefix) -> str:
        return f"{prefix}_{self.ulid()}"

    def ulid(self) -> str:
        """Return a bare ULID (API key ids use it without a prefix, IMPL-008)."""
        now_ms = int(self._clock.now().timestamp() * 1000)
        with self._lock:
            if now_ms <= self._last_ms:
                now_ms = self._last_ms
                rand = self._last_rand + 1
                if rand > _RANDOM_MAX:  # 2^80 ids in one millisecond: move to the next millisecond
                    now_ms += 1
                    rand = int.from_bytes(self._randbytes(10), "big")
            else:
                rand = int.from_bytes(self._randbytes(10), "big")
            if now_ms > _TIME_MAX:
                raise ValueError("timestamp beyond the ULID range")
            self._last_ms, self._last_rand = now_ms, rand
        return encode_ulid((now_ms << _RANDOM_BITS) | rand)
