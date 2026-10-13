"""`WriteBehindOutbox`: the `Outbox` of spec-02 §7 (spec-11 §3, IMPL-022).

`put(record)` (sync, O(1), never raises) appends to a bounded in-process deque (100k). At the bound a record spills
straight to the Redis stream while Redis is up; with Redis also down the oldest record is dropped
(`aghoo_outbox_dropped_total` + an ERROR log, which pages).

`run()` is the batch writer (api and jobs processes): every 200 ms, or as soon as 500 records wait, it applies one
batch with `SystemCalls.outbox_apply`. Records the database rejects, or the whole batch when Postgres is down, are
XADDed to `RedisKeys.outbox(crc32(tenant_id) % 8)` for the drainer; when Redis is down as well they stay in memory
(`aghoo_outbox_backlog{sink="memory"}`). After a `turn_finalize` is applied, its `session_pending` key is released
(compare-and-delete) by a separate task of `run()`, outside the writer's lock and bounded by 2 s per attempt, so a
slow or blackholed Redis never throttles the Postgres writes.

`flush(timeout_s)` (graceful shutdown, after every turn finalized) writes until nothing is left or the timeout
passes; what is left is logged as lost.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Final

import anyio

from noema.core.clock import Clock, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Database, OutboxRecord
from noema.core.protocols.telemetry import Instruments
from noema.core.redis_keys import RedisKeys
from noema.ops.jobs.timing import Wakeup
from noema.ops.outbox.sink import FIELD, PendingReleases, apply_records, encode

if TYPE_CHECKING:
    from redis.asyncio import Redis

log: Final = logging.getLogger("noema.ops.outbox")
MAX_MEMORY: Final = 100_000
BATCH_SIZE: Final = 500
INTERVAL_S: Final = 0.2
REDIS_TIMEOUT_S: Final = 2.0
PG_RETRY_S: Final = 1.0  # after Postgres was unreachable, batches go straight to Redis for this long


class WriteBehindOutbox:
    """Implements `Outbox` (see the module docstring)."""

    def __init__(
        self,
        db: Database,
        redis: Redis,
        keys: RedisKeys,
        clock: Clock,
        instruments: Instruments,
        *,
        max_memory: int = MAX_MEMORY,
        batch_size: int = BATCH_SIZE,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self._db = db
        self._redis = redis
        self._keys = keys
        self._clock = clock
        self._instruments = instruments
        self._max = max_memory
        self._batch = batch_size
        self._interval = interval_s
        self._memory: deque[OutboxRecord] = deque()
        self._spill: deque[OutboxRecord] = deque()  # over the memory bound: straight to Redis
        self._redis_up = True
        self._pg_retry_at = 0.0
        self._pg_up = True
        self._dropped = 0
        self._wake = Wakeup()
        self._lock = anyio.Lock()
        self._releases = PendingReleases(redis, keys, clock)

    @property
    def pending_releases(self) -> PendingReleases:
        """The `session_pending` releases waiting for Redis (run by `run()`)."""
        return self._releases

    @property
    def backlog(self) -> int:
        """Records waiting in memory (both deques)."""
        return len(self._memory) + len(self._spill)

    def put(self, record: OutboxRecord) -> None:
        """Queue `record` for the batch writer (sync; never raises)."""
        if len(self._memory) < self._max:
            self._memory.append(record)
            if len(self._memory) >= self._batch:
                self._wake.set()
            return
        if self._redis_up and len(self._spill) < self._max:
            self._spill.append(record)
        else:
            lost = self._memory.popleft()
            self._memory.append(record)
            self._drop(lost, "both sinks down and the in-process outbox is full")
        self._wake.set()

    def _drop(self, record: OutboxRecord, why: str) -> None:
        self._dropped += 1
        self._instruments.add("aghoo_outbox_dropped_total")
        if self._dropped == 1 or self._dropped % 1000 == 0:
            log.error(
                "outbox record dropped (%s): kind=%s id=%s; %d dropped", why, record.kind, record.id, self._dropped
            )

    async def run(self) -> None:
        """The batch writer loop plus the `session_pending` releases (cancel to stop; `flush` drains what is left)."""
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._releases.run)
            while True:
                await self._wake.wait(self._clock, self._interval)
                try:
                    while await self.write_once() >= self._batch:
                        pass  # a full batch left: more may be waiting
                except Exception:
                    log.exception("outbox: batch writer cycle failed")

    async def write_once(self) -> int:
        """Write one batch from memory (spilled records to Redis, the rest to Postgres, rejects to Redis). Returns
        how many records left memory."""
        async with self._lock:
            moved = await self._write_spill() + await self._write_batch()
            self._instruments.set("aghoo_outbox_backlog", float(self.backlog), sink="memory")
            return moved

    async def _write_spill(self) -> int:
        batch = [self._spill.popleft() for _ in range(min(self._batch, len(self._spill)))]
        sent = False
        try:
            sent = not batch or await self._to_redis(batch)
        finally:
            if not sent:  # Redis down, or cancelled mid-write (flush timeout): keep the records
                self._spill.extendleft(reversed(batch))
        return len(batch) if sent else 0

    async def _write_batch(self) -> int:
        batch = [self._memory.popleft() for _ in range(min(self._batch, len(self._memory)))]
        if not batch:
            return 0
        kept: list[OutboxRecord] = batch  # what goes back to memory unless the write completes
        try:
            try:
                if self._clock.monotonic() < self._pg_retry_at:
                    raise NoemaError(ErrorCode.INTERNAL_ERROR, "Postgres was unavailable moments ago.")
                results = await apply_records(self._db, batch)
            except NoemaError:
                if self._clock.monotonic() >= self._pg_retry_at:  # a real attempt failed (not the skip above)
                    if self._pg_up:
                        log.warning("outbox: Postgres unavailable; batches go to the Redis outbox")
                    self._pg_up = False
                    self._pg_retry_at = self._clock.monotonic() + PG_RETRY_S
                rejected = batch
            else:
                if not self._pg_up:
                    log.info("outbox: Postgres reachable again")
                self._pg_up = True
                rejected = [r for r, error in zip(batch, results, strict=True) if error is not None]
                for record, error in zip(batch, results, strict=True):
                    if error is not None:
                        log.warning("outbox: %s %s rejected (%s); retried from Redis", record.kind, record.id, error)
                kept = rejected
                self._releases.add(r for r, error in zip(batch, results, strict=True) if error is None)
            if rejected and not await self._to_redis(rejected):
                return len(batch) - len(rejected)  # Redis down too: the rejected records stay in memory
            kept = []
            return len(batch)
        finally:
            self._memory.extendleft(reversed(kept))

    async def _to_redis(self, records: list[OutboxRecord]) -> bool:
        pipe = self._redis.pipeline(transaction=False)
        for record in records:
            try:
                value = encode(record)
            except TypeError:
                self._drop(record, "payload is not JSON")
                continue
            pipe.xadd(self._keys.outbox(RedisKeys.outbox_shard(record.tenant_id)), {FIELD: value})
        try:
            async with deadline_scope(self._clock, self._clock.monotonic() + REDIS_TIMEOUT_S) as scope:
                await pipe.execute()
            if scope.cancelled_caught:
                raise TimeoutError("redis XADD timed out")
        except Exception as exc:
            if self._redis_up:
                log.warning("outbox: Redis unavailable (%s); records stay in memory", type(exc).__name__)
            self._redis_up = False
            return False
        self._redis_up = True
        return True

    async def flush(self, timeout_s: float) -> None:
        """Write everything left, retrying every 200 ms until `timeout_s`; then log what is left as lost."""
        with anyio.CancelScope(shield=True):
            async with deadline_scope(self._clock, self._clock.monotonic() + timeout_s):
                while self.backlog:
                    if await self.write_once() == 0:
                        await self._clock.sleep(self._interval)
                await self._releases.retry(force=True)  # the release task stops with the runtime
            lost = [*self._memory, *self._spill]
            self._memory.clear()
            self._spill.clear()
            if lost:
                self._instruments.add("aghoo_outbox_dropped_total", float(len(lost)))
                log.error("outbox: %d records lost at shutdown (first: %s %s)", len(lost), lost[0].kind, lost[0].id)
            self._instruments.set("aghoo_outbox_backlog", 0.0, sink="memory")
