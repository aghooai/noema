"""A minimal in-memory stand-in for the redis.asyncio subset the outbox writer and the notify bridge use in unit
tests (XADD through a non-transactional pipeline, DELETE, PUBLISH, SET/EXISTS, and EVAL of the outbox's
`session_pending` compare-and-delete script). `down = True` makes every command raise ConnectionError, as an
unreachable Redis does; `hang` (an Event) makes EVAL wait for it, as a blackholed Redis does. Stream behavior
(groups, claims) and the real Lua script are tested on Valkey in tests/integration/ops_ledger."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
from redis.exceptions import ConnectionError as RedisConnectionError


class _Pipeline:
    def __init__(self, owner: MemoryRedis) -> None:
        self._owner = owner
        self._ops: list[Callable[[], Any]] = []

    def xadd(self, name: str, fields: dict[bytes, bytes]) -> _Pipeline:
        self._ops.append(lambda: self._owner._xadd(name, fields))
        return self

    async def execute(self) -> list[Any]:
        self._owner._check()
        return [op() for op in self._ops]


class MemoryRedis:
    """See the module docstring."""

    def __init__(self) -> None:
        self.streams: dict[str, list[dict[bytes, bytes]]] = {}
        self.values: dict[str, bytes] = {}
        self.published: list[tuple[str, bytes]] = []
        self.down = False
        self.hang: anyio.Event | None = None
        self.deleted: list[str] = []
        self.evals = 0

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("redis is down")

    def _xadd(self, name: str, fields: dict[bytes, bytes]) -> bytes:
        entries = self.streams.setdefault(name, [])
        entries.append(dict(fields))
        return f"{len(entries)}-0".encode()

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self)

    async def delete(self, *names: str) -> int:
        self._check()
        gone = [n for n in names if self.values.pop(n, None) is not None]
        self.deleted.extend(names)
        return len(gone)

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        """The release script's semantics: DEL KEYS[i] iff its value equals ARGV[i]."""
        self.evals += 1
        if self.hang is not None:
            await self.hang.wait()
        self._check()
        keys, args = keys_and_args[:numkeys], keys_and_args[numkeys:]
        gone = 0
        for key, expected in zip(keys, args, strict=True):
            if self.values.get(key) == expected.encode():
                del self.values[key]
                self.deleted.append(key)
                gone += 1
        return gone

    async def set(self, name: str, value: bytes, ex: int | None = None) -> bool:
        self._check()
        self.values[name] = value
        return True

    async def exists(self, *names: str) -> int:
        self._check()
        return sum(1 for n in names if n in self.values)

    async def publish(self, channel: str, message: bytes) -> int:
        self._check()
        self.published.append((channel, message))
        return 1

    def entries(self) -> list[bytes]:
        """Every XADDed `r` value, across streams, in stream-name then insertion order."""
        return [e[b"r"] for name in sorted(self.streams) for e in self.streams[name]]
