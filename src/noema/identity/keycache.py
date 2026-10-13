"""The API-key row cache of the authenticator (spec-09 §2 step 1, IMPL-008).

An in-process LRU keyed by `key_id`: positive entries live 60 s, negative entries (unknown key ids) 10 s. It holds
the lookup row (pre-parsed for the hot path), never the presented secret or a verification result. `evict` serves
the `keys_revoked` listener; the TTL bounds revocation at 60 s when Redis is down.

Wave E: an expired positive entry is kept for `STALE_S` more as the last good row (`stale`), which the
authenticator uses only while Postgres is unavailable (06 §7: `store: false` turns continue). Revocations still
evict it; with Postgres and Redis down at once, a key revoked just before stays usable for at most `STALE_S`.
"""

from __future__ import annotations

import ipaddress
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, get_args

from noema.core.principal import Role, Scope

DEFAULT_CAPACITY: Final = 10_000
DEFAULT_TTL_S: Final = 60.0
DEFAULT_NEGATIVE_TTL_S: Final = 10.0
STALE_S: Final = 600.0  # an expired positive entry serves as the last good row this long (Postgres down only)
_ROLES: Final[frozenset[str]] = frozenset(get_args(Role))
_SCOPES: Final[frozenset[str]] = frozenset(get_args(Scope))

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(slots=True)
class KeyRow:
    """One `auth.lookup_api_key` row, parsed once."""

    tenant_id: str
    project_id: str
    principal_id: str
    key_hash: str
    pepper_version: int
    env: str
    scopes: frozenset[Scope]
    roles: frozenset[Role]
    expires_at: datetime | None
    revoked_at: datetime | None
    ip_allowlist: tuple[Network, ...]

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> KeyRow:
        """Parse a lookup row; unknown roles and scopes are dropped (the DB CHECKs make them impossible)."""
        networks: list[Network] = []
        for item in _as_list(row.get("ip_allowlist")):
            if isinstance(item, ipaddress.IPv4Network | ipaddress.IPv6Network):
                networks.append(item)
            else:
                networks.append(ipaddress.ip_network(str(item), strict=False))
        scopes: frozenset[Scope] = frozenset(s for s in _as_list(row.get("scopes")) if s in _SCOPES)  # type: ignore[misc]
        roles: frozenset[Role] = frozenset(r for r in _as_list(row.get("roles")) if r in _ROLES)  # type: ignore[misc]
        pepper_version = row.get("pepper_version")
        return cls(
            tenant_id=str(row["tenant_id"]),
            project_id=str(row["project_id"]),
            principal_id=str(row["principal_id"]),
            key_hash=str(row["key_hash"]),
            pepper_version=pepper_version if isinstance(pepper_version, int) else -1,
            env=str(row["env"]),
            scopes=scopes,
            roles=roles,
            expires_at=_as_dt(row.get("expires_at")),
            revoked_at=_as_dt(row.get("revoked_at")),
            ip_allowlist=tuple(networks),
        )


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


def _as_dt(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


@dataclass(slots=True)
class CacheEntry:
    """A cached lookup (`row` None = unknown key id) with its expiry and the per-key housekeeping state."""

    row: KeyRow | None
    expires_mono: float
    last_touch_mono: float | None = None
    rehash_started: bool = False


class KeyCache:
    """LRU of `CacheEntry` by key id. Not thread-safe (one event loop)."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        ttl_s: float = DEFAULT_TTL_S,
        negative_ttl_s: float = DEFAULT_NEGATIVE_TTL_S,
    ) -> None:
        if capacity < 1 or ttl_s <= 0 or negative_ttl_s <= 0:
            raise ValueError("capacity >= 1 and positive TTLs")
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._capacity = capacity
        self._ttl_s = ttl_s
        self._negative_ttl_s = negative_ttl_s

    def get(self, key_id: str, now_mono: float) -> CacheEntry | None:
        """The live entry (refreshed in the LRU order), or None when absent or expired (an expired positive entry
        stays for `stale` until `STALE_S` after its expiry)."""
        entry = self._entries.get(key_id)
        if entry is None:
            return None
        if now_mono >= entry.expires_mono:
            if entry.row is None or now_mono >= entry.expires_mono + STALE_S:
                del self._entries[key_id]
            return None
        self._entries.move_to_end(key_id)
        return entry

    def stale(self, key_id: str, now_mono: float) -> CacheEntry | None:
        """The last good row of an expired positive entry, within `STALE_S` of its expiry (else None)."""
        entry = self._entries.get(key_id)
        if entry is None or entry.row is None or now_mono >= entry.expires_mono + STALE_S:
            return None
        return entry

    def put(self, key_id: str, row: KeyRow | None, now_mono: float) -> CacheEntry:
        """Store a lookup result (None = unknown key, negative TTL); evicts the least recently used entry."""
        ttl = self._ttl_s if row is not None else self._negative_ttl_s
        entry = CacheEntry(row=row, expires_mono=now_mono + ttl)
        self._entries[key_id] = entry
        self._entries.move_to_end(key_id)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return entry

    def peek(self, key_id: str) -> CacheEntry | None:
        """The entry without touching the LRU order or its expiry (the revocation listener)."""
        return self._entries.get(key_id)

    def evict(self, key_id: str) -> bool:
        """Drop one entry; True if it was cached."""
        return self._entries.pop(key_id, None) is not None

    def __len__(self) -> int:
        return len(self._entries)
