"""`FakeSecretsProvider` (spec-02 §3): versioned in-memory secrets with the 300 s cache of the real providers.

`reads` counts backend reads, so tests can observe the cache (a cached `get` does not read). `fail(ref)` makes the
backend unavailable for one ref (a plain NoemaError internal_error); a missing secret or version raises
`SecretMissing`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import anyio.lowlevel

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError, SecretMissing
from noema.core.secret import SecretStr

CACHE_TTL_S: Final = 300.0


class FakeSecretsProvider:
    """Implements `SecretsProvider`. `put(ref, value, version=)` adds a version (the last one put is current)."""

    def __init__(self, clock: Clock, secrets: Mapping[str, bytes | str] | None = None) -> None:
        self._clock = clock
        self._versions: dict[str, dict[str, bytes]] = {}
        self._current: dict[str, str] = {}
        self._cache: dict[tuple[str, str | None], tuple[float, bytes]] = {}
        self._failing: set[str] = set()
        self._held: list[SecretStr] = []  # registered values stay redacted while the provider holds them
        self.reads = 0
        for ref, value in (secrets or {}).items():
            self.put(ref, value)

    def put(self, ref: str, value: bytes | str, *, version: str | None = None) -> str:
        """Store a new version (default name `v<n>`) and make it current; returns the version name. The value is
        registered with the redactor (SecretStr registry)."""
        raw = value.encode() if isinstance(value, str) else value
        self._held.append(SecretStr(raw.decode("utf-8", "replace"), register=True))  # redacted (spec-09 §4)
        versions = self._versions.setdefault(ref, {})
        name = version or f"v{len(versions) + 1}"
        versions[name] = raw
        self._current[ref] = name
        return name

    def fail(self, ref: str, failing: bool = True) -> None:
        """Make reads of `ref` fail (backend outage) until called with failing=False."""
        (self._failing.add if failing else self._failing.discard)(ref)

    async def get(self, ref: str, *, version: str | None = None) -> bytes:
        await anyio.lowlevel.checkpoint()
        key = (ref, version)
        now = self._clock.monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        self.reads += 1
        if ref in self._failing:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "A platform secret is unavailable.")
        versions = self._versions.get(ref)
        name = version if version is not None else self._current.get(ref)
        if versions is None or name is None or name not in versions:
            raise SecretMissing()
        value = versions[name]
        self._cache[key] = (now, value)
        return value
