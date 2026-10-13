"""Shared pieces of the outbox writer and drainer (spec-11 §3, IMPL-022): the Redis stream codec, the Postgres
apply with poison isolation, and the `session_pending` compare-and-delete that follows a `turn_finalize`.

Stream entries (`RedisKeys.outbox(crc32(tenant_id) % 8)`, consumer group `drain`) carry one field `r`: the orjson
object `{kind, id, tenant_id, payload, session_id}`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Final, cast, get_args

import orjson

from noema.core.clock import Clock, deadline_scope
from noema.core.errors import NoemaError
from noema.core.protocols.storage import Database, OutboxKind, OutboxRecord
from noema.core.redis_keys import RedisKeys
from noema.core.values import JsonValue
from noema.ops.jobs.timing import Wakeup

if TYPE_CHECKING:
    from redis.asyncio import Redis

log: Final = logging.getLogger("noema.ops.outbox")
FIELD: Final = b"r"
GROUP: Final = "drain"
KINDS: Final[frozenset[str]] = frozenset(get_args(OutboxKind))


def encode(record: OutboxRecord) -> bytes:
    """The stream entry value of `record` (TypeError for a payload that is not JSON)."""
    return orjson.dumps(
        {
            "kind": record.kind,
            "id": record.id,
            "tenant_id": record.tenant_id,
            "payload": record.payload,
            "session_id": record.session_id,
        }
    )


def decode(raw: bytes) -> OutboxRecord:
    """The record of a stream entry value; ValueError for anything malformed."""
    doc = orjson.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("outbox entry is not an object")
    kind, rid, tenant, payload, session = (doc.get(k) for k in ("kind", "id", "tenant_id", "payload", "session_id"))
    if kind not in KINDS or not isinstance(rid, str) or not isinstance(tenant, str) or not isinstance(payload, dict):
        raise ValueError("outbox entry lacks kind, id, tenant_id or payload")
    if session is not None and not isinstance(session, str):
        raise ValueError("outbox entry session_id is not a string")
    return OutboxRecord(cast(OutboxKind, kind), rid, tenant, cast(dict[str, JsonValue], payload), session)


async def apply_records(db: Database, records: Sequence[OutboxRecord]) -> list[str | None]:
    """`SystemCalls.outbox_apply` per record (None = applied or an idempotent replay, else the error code).
    Raises `NoemaError(internal_error)` when Postgres is unavailable. Any other failure of a batch (a payload the
    driver can't encode, a malformed result) is isolated by applying the records one by one, so a poison record
    never holds back the rest; a record that fails alone gets `apply_failed:<ExceptionType>`."""
    if not records:
        return []
    try:
        async with db.system_uow() as calls:
            return list(await calls.outbox_apply(records))
    except NoemaError:
        raise
    except Exception as exc:
        if len(records) == 1:
            return [f"apply_failed:{type(exc).__name__}"]
    out: list[str | None] = []
    for record in records:
        out.extend(await apply_records(db, (record,)))
    return out


RELEASE_TIMEOUT_S: Final = 2.0  # one release attempt (a slow or blackholed Redis never holds a caller longer)
RELEASE_BACKOFF_S: Final = 1.0  # first wait after a failed attempt, doubling up to RELEASE_MAX_BACKOFF_S
RELEASE_MAX_BACKOFF_S: Final = 30.0
RELEASE_CHUNK: Final = 500  # keys per script call (bounds the time Redis spends in one script)
IDLE_S: Final = 60.0
# Compare-and-delete: KEYS[i] = session_pending(sid), ARGV[i] = the turn id the key must still hold.
RELEASE_LUA: Final = """
local n = 0
for i, key in ipairs(KEYS) do
  if redis.call('GET', key) == ARGV[i] then
    n = n + redis.call('DEL', key)
  end
end
return n
"""


class PendingReleases:
    """Releases `RedisKeys.session_pending(sid)` after a `turn_finalize` was applied or dead-lettered (spec-11 §3).

    The key holds the turn id (`SETEX ... <turn_id>`), so a release is a compare-and-delete: a delayed or duplicate
    release of turn T1 never deletes the key of a later turn T2. Each attempt is bounded by 2 s; a failed attempt
    is retried after 1, 2, 4 ... 30 s (a stale key would keep the session busy for its 24 h TTL). `retry()` never
    raises."""

    __slots__ = ("_backoff", "_clock", "_keys", "_redis", "_retry_at", "_todo", "_wake")

    def __init__(self, redis: Redis, keys: RedisKeys, clock: Clock) -> None:
        self._redis = redis
        self._keys = keys
        self._clock = clock
        self._todo: set[tuple[str, str]] = set()
        self._retry_at = 0.0
        self._backoff = RELEASE_BACKOFF_S
        self._wake = Wakeup()

    @property
    def outstanding(self) -> frozenset[tuple[str, str]]:
        """(session id, turn id) pairs whose release still has to succeed."""
        return frozenset(self._todo)

    def add(self, records: Iterable[OutboxRecord]) -> None:
        """Queue the releases of the `turn_finalize` records among `records` (sync; wakes `run`)."""
        before = len(self._todo)
        self._todo.update((r.session_id, r.id) for r in records if r.kind == "turn_finalize" and r.session_id)
        if len(self._todo) > before:
            self._wake.set()

    async def retry(self, *, force: bool = False) -> bool:
        """One attempt at the outstanding releases (skipped while backing off, unless `force`). True iff none is
        left."""
        if not self._todo:
            return True
        if not force and self._clock.monotonic() < self._retry_at:
            return False
        batch = sorted(self._todo)
        try:
            async with deadline_scope(self._clock, self._clock.monotonic() + RELEASE_TIMEOUT_S) as scope:
                for start in range(0, len(batch), RELEASE_CHUNK):
                    chunk = batch[start : start + RELEASE_CHUNK]
                    keys = [self._keys.session_pending(sid) for sid, _ in chunk]
                    await self._redis.eval(RELEASE_LUA, len(chunk), *keys, *(tid for _, tid in chunk))
                    self._todo.difference_update(chunk)
            if scope.cancelled_caught:
                raise TimeoutError("session_pending release timed out")
        except Exception as exc:
            self._retry_at = self._clock.monotonic() + self._backoff
            log.warning(
                "outbox: session_pending release failed (%s); %d to retry in %.0f s",
                type(exc).__name__,
                len(self._todo),
                self._backoff,
            )
            self._backoff = min(self._backoff * 2, RELEASE_MAX_BACKOFF_S)
            return False
        self._retry_at, self._backoff = 0.0, RELEASE_BACKOFF_S
        return not self._todo

    async def run(self) -> None:
        """Release in the background until cancelled: as soon as releases are added, then at each retry time."""
        while True:
            if self._todo:
                await self.retry()
            wait = max(self._retry_at - self._clock.monotonic(), 0.0) if self._todo else IDLE_S
            await self._wake.wait(self._clock, wait)
