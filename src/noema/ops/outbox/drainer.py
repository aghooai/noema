"""`OutboxDrainer`: applies the Redis outbox streams to Postgres (spec-11 §3, IMPL-022).

One consumer per process (name = the pod id) in group `drain` on all 8 shards. Every second: `XAUTOCLAIM` entries
idle for more than 30 s (a dead pod's, or this consumer's own failures), then `XREADGROUP` new entries, and
`ops.outbox_apply` them. Applied -> `XACK` + `XDEL`; failed -> left pending (claimed again after 30 s, which
counts another delivery); a failure at delivery count >= 10 -> `ops.outbox_dead` + `XACK`. While Postgres is
unreachable nothing is acknowledged or dead-lettered, and nothing is claimed or read either: once an apply found
Postgres down, each cycle first probes it with an empty unit of work and skips the streams until it answers. So an
outage adds at most one delivery to an entry (the cycle that found it down), however long it lasts, and the
first real failure after recovery is retried like any other. A `turn_finalize` that was applied or dead-lettered
releases its `session_pending` key (compare-and-delete, bounded by 2 s). `aghoo_outbox_backlog{sink="redis"}` is
the summed stream length.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from redis.exceptions import ResponseError

from noema.core.clock import Clock
from noema.core.errors import NoemaError
from noema.core.protocols.storage import Database, OutboxRecord
from noema.core.protocols.telemetry import Instruments
from noema.core.redis_keys import OUTBOX_SHARDS, RedisKeys
from noema.ops.outbox.sink import FIELD, GROUP, PendingReleases, apply_records, decode

if TYPE_CHECKING:
    from redis.asyncio import Redis

log: Final = logging.getLogger("noema.ops.outbox")
INTERVAL_S: Final = 1.0
MIN_IDLE_MS: Final = 30_000
MAX_DELIVERIES: Final = 10
BATCH_SIZE: Final = 500
PROBE_TENANT: Final = "__platform__"  # the Postgres probe opens (and commits) an empty unit of work in this scope


@dataclass(frozen=True, slots=True)
class _Entry:
    stream: bytes
    entry_id: bytes
    raw: bytes | None
    deliveries: int


class OutboxDrainer:
    """The Redis outbox consumer of one process (see the module docstring)."""

    def __init__(
        self,
        db: Database,
        redis: Redis,
        keys: RedisKeys,
        clock: Clock,
        instruments: Instruments,
        *,
        consumer: str,
        min_idle_ms: int = MIN_IDLE_MS,
        max_deliveries: int = MAX_DELIVERIES,
        batch_size: int = BATCH_SIZE,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self._db = db
        self._redis = redis
        self._clock = clock
        self._instruments = instruments
        self._consumer = consumer
        self._min_idle_ms = min_idle_ms
        self._max_deliveries = max_deliveries
        self._batch = batch_size
        self._interval = interval_s
        self._streams = [keys.outbox(shard) for shard in range(OUTBOX_SHARDS)]
        self._groups_ready = False
        self._pg_down = False
        self._releases = PendingReleases(redis, keys, clock)

    async def run(self) -> None:
        """Drain every second until cancelled (errors are logged; Redis or Postgres outages just wait)."""
        while True:
            try:
                await self.drain_once()
            except Exception as exc:
                self._groups_ready = self._groups_ready and not _is_nogroup(exc)
                log.warning("outbox drainer: cycle failed (%s)", type(exc).__name__)
            await self._clock.sleep(self._interval)

    async def drain_once(self) -> int:
        """One cycle; returns how many entries were acknowledged (applied, dead-lettered or undecodable)."""
        await self._ensure_groups()
        await self._releases.retry()
        done = 0
        if await self._postgres_up():
            entries = await self._claim_idle() + await self._read_new()  # claimed first: no double delivery
            done = await self._process(entries) if entries else 0
        await self._report_backlog()
        return done

    async def _postgres_up(self) -> bool:
        """True unless the last apply found Postgres down and it still does not answer (claiming or reading
        entries then would only raise their delivery counts)."""
        if not self._pg_down:
            return True
        try:
            async with self._db.tenant_uow(PROBE_TENANT):
                pass
        except Exception as exc:
            log.debug("outbox drainer: Postgres still unavailable (%s)", type(exc).__name__)
            return False
        log.info("outbox drainer: Postgres reachable again")
        self._pg_down = False
        return True

    async def _ensure_groups(self) -> None:
        if self._groups_ready:
            return
        for stream in self._streams:
            try:
                await self._redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        self._groups_ready = True

    async def _read_new(self) -> list[_Entry]:
        found: Any = await self._redis.xreadgroup(
            GROUP, self._consumer, dict.fromkeys(self._streams, ">"), count=self._batch
        )
        return [
            _Entry(bytes(stream), entry_id, raw, 1)
            for stream, messages in found or []
            for entry_id, raw in _parse(messages)
        ]

    async def _claim_idle(self) -> list[_Entry]:
        out: list[_Entry] = []
        for stream in self._streams:
            claimed: Any = await self._redis.xautoclaim(
                stream, GROUP, self._consumer, self._min_idle_ms, "0-0", count=self._batch
            )
            messages = _parse(claimed[1] if len(claimed) > 1 else [])
            if not messages:
                continue
            out += [
                _Entry(stream.encode(), eid, raw, n)
                for (eid, raw), n in zip(messages, await self._counts(stream, messages), strict=True)
            ]
        return out

    async def _counts(self, stream: str, messages: Sequence[tuple[bytes, bytes | None]]) -> list[int]:
        """The delivery count of each claimed entry: one exact `XPENDING stream drain id id 1` per entry (a range
        query could be filled by other pending entries of this consumer between the claimed ids)."""
        pipe = self._redis.pipeline(transaction=False)
        for entry_id, _ in messages:
            pipe.xpending_range(stream, GROUP, entry_id, entry_id, 1, consumername=self._consumer)
        found: list[Any] = await pipe.execute()
        return [int(rows[0]["times_delivered"]) if rows else 1 for rows in found]

    async def _process(self, entries: Sequence[_Entry]) -> int:
        ack: list[_Entry] = []
        good: list[tuple[_Entry, OutboxRecord]] = []
        for entry in entries:
            try:
                good.append((entry, decode(entry.raw or b"")))
            except ValueError:
                self._instruments.add("aghoo_outbox_dropped_total")
                log.error("outbox drainer: undecodable entry %r dropped", entry.entry_id)
                ack.append(entry)
        try:
            results = await apply_records(self._db, [record for _, record in good])
        except NoemaError:
            log.info("outbox drainer: Postgres unavailable; %d entries stay pending", len(good))
            self._pg_down = True
            await self._ack(ack)
            return len(ack)
        released: list[OutboxRecord] = []
        for (entry, record), error in zip(good, results, strict=True):
            last = error is not None and entry.deliveries >= self._max_deliveries
            if error is None or (last and await self._dead(record, error, entry.deliveries)):
                ack.append(entry)
                released.append(record)
        await self._ack(ack)
        self._releases.add(released)
        await self._releases.retry()
        return len(ack)

    async def _dead(self, record: OutboxRecord, error: str, deliveries: int) -> bool:
        try:
            async with self._db.system_uow() as calls:
                await calls.outbox_dead(record, error, attempts=deliveries)
        except Exception as exc:
            self._pg_down = self._pg_down or isinstance(exc, NoemaError)
            log.warning("outbox drainer: dead-lettering %s %s failed (%s)", record.kind, record.id, type(exc).__name__)
            return False
        alert = " (the session stays busy until released; alert)" if record.kind == "turn_finalize" else ""
        log.error(
            "outbox record dead-lettered after %d deliveries: kind=%s id=%s error=%s%s",
            deliveries,
            record.kind,
            record.id,
            error,
            alert,
        )
        return True

    async def _ack(self, entries: Sequence[_Entry]) -> None:
        if not entries:
            return
        by_stream: dict[bytes, list[bytes]] = {}
        for entry in entries:
            by_stream.setdefault(entry.stream, []).append(entry.entry_id)
        pipe = self._redis.pipeline(transaction=False)
        for stream, ids in by_stream.items():
            pipe.xack(stream, GROUP, *ids)
            pipe.xdel(stream, *ids)
        await pipe.execute()

    async def _report_backlog(self) -> None:
        pipe = self._redis.pipeline(transaction=False)
        for stream in self._streams:
            pipe.xlen(stream)
        lengths = await pipe.execute()
        self._instruments.set("aghoo_outbox_backlog", float(sum(int(n) for n in lengths)), sink="redis")


def _parse(messages: Any) -> list[tuple[bytes, bytes | None]]:
    """[(entry id, the `r` field)] of an XREADGROUP/XAUTOCLAIM message list (deleted entries have no fields)."""
    return [(bytes(entry_id), (fields or {}).get(FIELD)) for entry_id, fields in messages]


def _is_nogroup(exc: BaseException) -> bool:
    return isinstance(exc, ResponseError) and "NOGROUP" in str(exc)
