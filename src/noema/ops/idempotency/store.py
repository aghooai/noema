"""`PgIdempotencyStore`: the `IdempotencyStore` of spec-02 §7 (spec-11 §5, IMPL-077, IMPL-085).

Records live in `noema.idempotency_record` (through `IdempotencyRepo`, under `tenant_uow(tenant, project)`, so every
lookup uses the unique (tenant, project, scope_hash, key) index). `scope_hash = sha256(project \\n method \\n
route template)`; `owner = "<pod>:<request_id>"`; leases are 30 s (renewed by `heartbeat` every 10 s); records
expire after `platform.idempotency.ttl_s`.

`begin` inserts an in_flight record (`New`) or classifies the existing one: expired -> taken over as `New`; other
fingerprint -> `Conflict`; completed -> `Replay` (a large body is read from S3; a read failure is 500
`internal_error`, retryable, and the record stays completed); not_stored -> taken over; in_flight with a live
lease -> `InFlight` (the original's turn/decision id for SSE attach); lapsed lease -> taken over. A take-over is a
conditional UPDATE: of racing takers exactly one wins, the others re-read and re-classify. Postgres down raises
500 `internal_error` (retryable) from the unit of work, so a keyed request never risks a double execution.

`await_result` waits on the `idem_done` pub/sub channel plus a 250 ms poll: 504 `deadline_exceeded` at the
duplicate's deadline, 500 `internal_error` when the original turns not_stored or its lease lapses. Every waiter of
the store (one per process) shares one pub/sub connection (`DoneSubscriber`, Wave E2).
`complete` stores the response (None -> not_stored) iff this owner still holds the record; bodies over 1 MiB go to
S3 first (one retry within 2 s; failing that the record becomes not_stored and
`aghoo_idempotency_store_errors_total` counts it), then the row; then `idem_done` is published (bounded by 0.5 s;
a lost message only costs the waiters one poll).

Every Redis and S3 call on these paths has a time limit, so a slow or blackholed dependency gives a prompt answer:
SUBSCRIBE min(0.5 s, the deadline), PUBLISH 0.5 s, GetObject of a replay 2 s (then 500, retryable), and the
DeleteObject of a taken-over record's stale body 1 s (then left for the new owner's overwrite or the sweeper; it
is awaited before `begin` returns, never deferred, because the new owner writes the same key).

`aghoo_idempotency_attach_total{result}`: `in_flight` per duplicate that `begin` attaches to a running original,
then per `await_result` its outcome: `completed`, `deadline` or `lost` (not_stored, lapsed or taken over).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import anyio
import orjson

from noema.core.clock import Clock, Deadline, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.storage import (
    Conflict,
    Database,
    IdemToken,
    InFlight,
    New,
    Replay,
    Row,
    StoredResponse,
    UnitOfWork,
)
from noema.core.protocols.telemetry import Instruments
from noema.core.redis_keys import RedisKeys
from noema.db.repos import IDEM_INLINE_MAX_BYTES
from noema.ops.idempotency.bodies import BodyStore
from noema.ops.idempotency.waiting import DoneSubscriber

if TYPE_CHECKING:
    from redis.asyncio import Redis

log: Final = logging.getLogger("noema.ops.idempotency")
LEASE_S: Final = 30
POLL_S: Final = 0.25
PUT_TIMEOUT_S: Final = 2.0
GET_TIMEOUT_S: Final = 2.0
STALE_DELETE_TIMEOUT_S: Final = 1.0
PUBLISH_TIMEOUT_S: Final = 0.5
ATTACH: Final = "aghoo_idempotency_attach_total"
BEGIN_TRIES: Final = 5


def scope_hash(project_id: str, method: str, route: str) -> str:
    """sha256 hex of `project_id \\n method \\n route template` (spec-11 §5)."""
    return hashlib.sha256(f"{project_id}\n{method}\n{route}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OwnedToken(IdemToken):
    """An `IdemToken` that also carries the record's scope and the owner it was issued to (the original's owner
    for `InFlight`), so tokens of an old owner and of the one that took the record over never compare equal."""

    tenant_id: str
    project_id: str
    owner: str


@dataclass(frozen=True, slots=True)
class _Completed:
    row: Row


class PgIdempotencyStore:
    """Implements `IdempotencyStore` (see the module docstring)."""

    def __init__(
        self,
        db: Database,
        redis: Redis,
        keys: RedisKeys,
        bodies: BodyStore,
        clock: Clock,
        ids: IdGen,
        config: ConfigRegistry,
        instruments: Instruments,
        *,
        pod_id: str,
        poll_s: float = POLL_S,
    ) -> None:
        self._db = db
        self._redis = redis
        self._keys = keys
        self._bodies = bodies
        self._clock = clock
        self._ids = ids
        self._config = config
        self._instruments = instruments
        self._pod = pod_id
        self._poll_s = poll_s
        self._done = DoneSubscriber(redis, clock)  # one idem_done connection for every waiter (Wave E2)

    async def begin(
        self,
        principal: Principal,
        method: str,
        route: str,
        key: str,
        fingerprint: str,
        *,
        resource_id: str | None = None,
    ) -> New | InFlight | Replay | Conflict:
        """Claim `key` for this request (see the module docstring). ValueError for a principal without a project
        (every API key belongs to one)."""
        if not principal.project_id:
            raise ValueError("idempotency needs a project-scoped principal")
        token = OwnedToken(
            scope_hash(principal.project_id, method, route),
            key,
            "",
            principal.tenant_id,
            principal.project_id,
            f"{self._pod}:{principal.request_id}",
        )
        ttl_s = self._config.current().platform.idempotency.ttl_s
        for _ in range(BEGIN_TRIES):
            async with self._db.tenant_uow(principal.tenant_id, project_id=principal.project_id) as uow:
                outcome = await self._classify(uow, token, fingerprint, ttl_s, resource_id)
            if isinstance(outcome, _Completed):
                return Replay(await self._stored(outcome.row))
            if isinstance(outcome, tuple):
                new, stale_key = outcome
                if stale_key is not None:
                    await self._delete_stale(token.tenant_id, new.token.record_id)
                return new
            if isinstance(outcome, InFlight):
                self._instruments.add(ATTACH, result="in_flight")
            if outcome is not None:
                return outcome
        raise NoemaError(ErrorCode.INTERNAL_ERROR, "The request is being processed concurrently; retry.")

    async def _classify(
        self, uow: UnitOfWork, token: OwnedToken, fingerprint: str, ttl_s: int, resource_id: str | None
    ) -> InFlight | Conflict | _Completed | tuple[New, str | None] | None:
        repo, now = uow.idempotency, self._clock.now()
        record_id = self._ids.new("idem")
        row = {
            "id": record_id,
            "scope_hash": token.scope_hash,
            "key": token.key,
            "fingerprint": fingerprint,
            "owner": token.owner,
            "lease_expires_at": now + timedelta(seconds=LEASE_S),
            "expires_at": now + timedelta(seconds=ttl_s),
            "resource_id": resource_id,
        }
        if await repo.insert_in_flight(row):
            return New(_with_id(token, record_id)), None
        rec = await repo.get_by_key(token.scope_hash, token.key)
        if rec is None:
            return None  # swept in between: insert again
        state, existing = rec["state"], str(rec["id"])
        if not _before(rec["expires_at"], now):
            if rec["fingerprint"] != fingerprint:
                return Conflict()
            if state == "completed":
                return _Completed(rec)
            if state == "in_flight" and not _before(rec["lease_expires_at"], now):
                return InFlight(_with_id(token, existing, owner=str(rec["owner"])), _opt(rec["resource_id"]))
        taken = await repo.take_over(
            existing, owner=token.owner, fingerprint=fingerprint, lease_s=LEASE_S, ttl_s=ttl_s, resource_id=resource_id
        )
        return (New(_with_id(token, existing)), _opt(rec["response_body_key"])) if taken else None

    async def _delete_stale(self, tenant_id: str, record_id: str) -> None:
        try:
            async with deadline_scope(self._clock, self._clock.monotonic() + STALE_DELETE_TIMEOUT_S) as scope:
                await self._bodies.delete(tenant_id, record_id)
            if scope.cancelled_caught:
                raise TimeoutError("DeleteObject timed out")
        except Exception as exc:  # overwritten by the new owner's large body, else deleted by the sweeper
            log.warning("idempotency: stale body of %s not deleted (%s)", record_id, type(exc).__name__)

    async def _stored(self, rec: Row) -> StoredResponse:
        raw = rec["response_body"]
        body = bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else None
        body_key = _opt(rec["response_body_key"])
        if body_key is not None:
            try:
                async with deadline_scope(self._clock, self._clock.monotonic() + GET_TIMEOUT_S) as scope:
                    body = await self._bodies.get(body_key)
                if scope.cancelled_caught:
                    raise TimeoutError("GetObject timed out")
            except Exception as exc:
                log.error("idempotency: stored body of %s unreadable (%s)", rec["id"], type(exc).__name__)
                raise NoemaError(ErrorCode.INTERNAL_ERROR, "The stored response is temporarily unavailable.") from None
        raw_headers = rec["response_headers"] or []
        assert isinstance(raw_headers, list)
        headers = tuple((str(h[0]), str(h[1])) for h in raw_headers)
        status = rec["response_status"]
        assert isinstance(status, int)
        return StoredResponse(status, headers, body, _opt(rec["sse_turn_id"]))

    async def heartbeat(self, token: IdemToken) -> bool:
        """Renew the 30 s lease; False = the lease is lost (taken over, completed or gone)."""
        t = _owned(token)
        async with self._db.tenant_uow(t.tenant_id, project_id=t.project_id) as uow:
            return await uow.idempotency.renew(t.record_id, owner=t.owner, lease_s=LEASE_S)

    async def complete(self, token: IdemToken, resp: StoredResponse | None) -> None:
        """Store `resp` (None = not stored); shielded, idempotent, a no-op once this owner lost the record."""
        t = _owned(token)
        with anyio.CancelScope(shield=True):
            stored = resp
            if resp is not None and resp.body is not None and len(resp.body) > IDEM_INLINE_MAX_BYTES:
                if not await self.heartbeat(t):
                    return  # taken over (or already completed): never overwrite another owner's object
                stored = await self._put_large(t, resp.body, resp)
            async with self._db.tenant_uow(t.tenant_id, project_id=t.project_id) as uow:
                await uow.idempotency.finish(t.record_id, owner=t.owner, response=stored)
            await self._publish_done(t)

    async def _put_large(self, t: OwnedToken, body: bytes, resp: StoredResponse) -> StoredResponse | None:
        stored_ok = False
        async with deadline_scope(self._clock, self._clock.monotonic() + PUT_TIMEOUT_S):
            stored_ok = await self._bodies.put(t.tenant_id, t.record_id, body)
        if stored_ok:
            return resp
        self._instruments.add("aghoo_idempotency_store_errors_total")
        log.error("idempotency: a %d-byte body could not be stored; record %s is not_stored", len(body), t.record_id)
        return None

    async def _publish_done(self, t: OwnedToken) -> None:
        message = orjson.dumps({"tenant_id": t.tenant_id, "record_id": t.record_id})
        try:
            async with deadline_scope(self._clock, self._clock.monotonic() + PUBLISH_TIMEOUT_S) as scope:
                await self._redis.publish(self._keys.idem_done(t.scope_hash, t.key), message)
            if scope.cancelled_caught:
                raise TimeoutError("PUBLISH timed out")
        except Exception as exc:  # waiters fall back to the 250 ms poll
            log.debug("idempotency: idem_done not published (%s)", type(exc).__name__)

    async def await_result(self, token: IdemToken, *, deadline: Deadline) -> StoredResponse:
        """The original's stored response (see the module docstring for the errors)."""
        t = _owned(token)
        channel = self._keys.idem_done(t.scope_hash, t.key)
        async with self._done.signal(channel, t.tenant_id, subscribe_until=deadline.at) as done:
            while True:
                async with self._db.tenant_uow(t.tenant_id, project_id=t.project_id) as uow:
                    rec = await uow.idempotency.get_by_key(t.scope_hash, t.key)
                if rec is not None and rec["id"] == t.record_id and rec["state"] == "completed":
                    self._instruments.add(ATTACH, result="completed")
                    return await self._stored(rec)
                if (
                    rec is None
                    or rec["id"] != t.record_id
                    or rec["state"] != "in_flight"
                    or rec["owner"] != t.owner
                    or _before(rec["lease_expires_at"], self._clock.now())
                ):
                    self._instruments.add(ATTACH, result="lost")
                    raise NoemaError(ErrorCode.INTERNAL_ERROR, "The original request did not complete; retry.")
                if deadline.expired():
                    self._instruments.add(ATTACH, result="deadline")
                    raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The original request is still running.")
                await done.wait(self._clock, min(self._poll_s, max(0.0, deadline.at - self._clock.monotonic())))


def _with_id(token: OwnedToken, record_id: str, *, owner: str | None = None) -> OwnedToken:
    return OwnedToken(token.scope_hash, token.key, record_id, token.tenant_id, token.project_id, owner or token.owner)


def _owned(token: IdemToken) -> OwnedToken:
    if not isinstance(token, OwnedToken):
        raise TypeError("the token was not issued by PgIdempotencyStore")
    return token


def _opt(value: object) -> str | None:
    return None if value is None else str(value)


def _before(at: object, now: object) -> bool:
    """`at <= now` for two aware datetimes."""
    return bool(at <= now)  # type: ignore[operator]
