"""A minimal bytes Redis double for `DecisionCacheImpl` tests: GET, SET EX (TTL on the injected clock), SCAN MATCH,
UNLINK; `down` raises, `delay_s` hangs every call on the clock."""

from __future__ import annotations

import fnmatch
from typing import Any

from noema.core.clock import Clock


class FakeRedis:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.data: dict[str, tuple[bytes, float]] = {}
        self.down = False
        self.delay_s = 0.0

    async def _latency(self) -> None:
        if self.delay_s:
            await self._clock.sleep(self.delay_s)
        if self.down:
            raise ConnectionError("redis down")

    async def get(self, key: str) -> bytes | None:
        await self._latency()
        hit = self.data.get(key)
        if hit is None or hit[1] <= self._clock.monotonic():
            self.data.pop(key, None)
            return None
        return hit[0]

    async def set(self, key: str, value: bytes, ex: int) -> bool:
        await self._latency()
        self.data[key] = (value, self._clock.monotonic() + ex)
        return True

    async def scan(self, cursor: int = 0, match: str = "*", count: int = 10) -> tuple[int, list[bytes]]:
        await self._latency()
        keys = sorted(k for k in self.data if fnmatch.fnmatchcase(k, match.replace("\\", "")))
        page = keys[cursor : cursor + count]
        nxt = cursor + count if cursor + count < len(keys) else 0
        return nxt, [k.encode() for k in page]

    async def unlink(self, *keys: Any) -> int:
        await self._latency()
        n = 0
        for k in keys:
            n += self.data.pop(k.decode() if isinstance(k, bytes) else k, None) is not None
        return n
