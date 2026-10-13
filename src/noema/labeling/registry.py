"""`DbCalibratorRegistry`: the per-pod snapshot of active calibrators (spec-02 §9, ADR-003 §2, IMPL-072).

`lookup` and `epoch` are sync dictionary reads of one immutable snapshot. `refresh()` (called by the composition
root's 5 s poll and by `listen()` on every `calibrators_changed` message) reads `SystemCalls.calibrator_epochs()`
(every live scope key, 0 before its first bump), then `active_calibrators(changed scope keys)` for the scopes whose
counter moved, and swaps the snapshot atomically. Any error keeps the last snapshot (logged); a malformed row is
skipped, so that binding stays uncalibrated. Before the first successful refresh `lookup` returns None and `epoch`
returns "" (L2 bypass); an unknown tenant's epoch is "" as well.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio

from noema.core.clock import Clock
from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey
from noema.core.protocols.storage import Database
from noema.core.pubsub import heartbeat_messages
from noema.labeling.artifact import calibrator_from_row

if TYPE_CHECKING:
    from redis.asyncio import Redis

PLATFORM: Final = "platform"
LISTEN_BACKOFF_MAX_S: Final = 30.0
_log: Final = logging.getLogger("noema.labeling.registry")


@dataclass(frozen=True, slots=True)
class _Snapshot:
    epochs: Mapping[str, int]
    tenant: Mapping[tuple[str, CalibratorBindingKey], Calibrator] = field(default_factory=dict)
    platform: Mapping[CalibratorBindingKey, Calibrator] = field(default_factory=dict)


class DbCalibratorRegistry:
    """Implements `CalibratorRegistry` over `Database.system_uow()` (see the module docstring)."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._snap: _Snapshot | None = None
        self._lock = anyio.Lock()
        self.refreshes = 0
        self.refresh_errors = 0

    def lookup(self, tenant_id: str, key: CalibratorBindingKey) -> Calibrator | None:
        """The active tenant artifact of exactly this binding, else the active platform one, else None."""
        snap = self._snap
        if snap is None:
            return None
        return snap.tenant.get((tenant_id, key)) or snap.platform.get(key)

    def epoch(self, tenant_id: str) -> str:
        """`f"{platform_n}.{tenant_n}"`, or "" before the first refresh and for an unknown tenant."""
        snap = self._snap
        if snap is None or tenant_id == PLATFORM:
            return ""
        tenant_n = snap.epochs.get(tenant_id)
        if tenant_n is None:
            return ""
        return f"{snap.epochs.get(PLATFORM, 0)}.{tenant_n}"

    async def refresh(self) -> None:
        """Reload the scopes whose epoch changed; never raises (cancellation propagates)."""
        async with self._lock:
            try:
                await self._refresh()
            except Exception as exc:  # keep the last snapshot
                self.refresh_errors += 1
                _log.warning(
                    "calibrator refresh failed; keeping the last snapshot", extra={"error": type(exc).__name__}
                )
            else:
                self.refreshes += 1

    async def _refresh(self) -> None:
        old = self._snap
        async with self._db.system_uow() as calls:
            epochs = {str(k): int(n) for k, n in (await calls.calibrator_epochs()).items()}
            epochs.setdefault(PLATFORM, 0)
            before = {} if old is None else dict(old.epochs)
            changed = sorted(k for k, n in epochs.items() if before.get(k) != n)
            gone = set(before) - set(epochs)
            if old is not None and not changed and not gone:
                return
            rows = await calls.active_calibrators(changed) if changed else ()
        stale = set(changed) | gone
        tenant = {} if old is None else {k: c for k, c in old.tenant.items() if k[0] not in stale}
        platform = {} if old is None or PLATFORM in stale else dict(old.platform)
        for row in rows:
            try:
                cal = calibrator_from_row(row)
            except (KeyError, TypeError, ValueError) as exc:
                _log.warning(
                    "skipping a malformed calibrator row", extra={"calibrator_id": row.get("id"), "error": str(exc)}
                )
                continue
            if cal.scope == "platform":
                platform[cal.key] = cal
            else:
                tenant[(str(row["tenant_id"]), cal.key)] = cal
        self._snap = _Snapshot(MappingProxyType(epochs), MappingProxyType(tenant), MappingProxyType(platform))

    async def listen(self, redis: Redis, channel: str, *, clock: Clock) -> None:
        """Until cancelled: subscribe to `channel` (`RedisKeys.calibrators_changed()`) and refresh on every message;
        a lost connection, or one silent for 15 s despite PINGs (`noema.core.pubsub.heartbeat_messages`), is
        retried with exponential backoff (1 s .. 30 s on `clock`). The 5 s poll covers the gap."""
        delay = 1.0
        while True:
            try:
                pubsub = redis.pubsub()
                try:
                    await pubsub.subscribe(channel)
                    delay = 1.0
                    await self.refresh()  # changes published while unsubscribed
                    async for _ in heartbeat_messages(pubsub, clock):
                        await self.refresh()
                finally:
                    with anyio.CancelScope(shield=True):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]
            except Exception as exc:
                _log.warning("calibrators_changed subscription lost", extra={"error": type(exc).__name__})
                await clock.sleep(delay)
                delay = min(delay * 2.0, LISTEN_BACKOFF_MAX_S)
