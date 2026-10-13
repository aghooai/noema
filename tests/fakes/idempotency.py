"""`FakeIdempotencyStore` (spec-02 §7, spec-11 §5 state machine, IMPL-077), in memory on a `Clock`.

`scope_hash = sha256(f"{project_id}\\n{method}\\n{route}")`. `begin` classifies exactly as spec-11 §5 (absent or
expired -> New; other fingerprint -> Conflict; completed -> Replay; not_stored -> New; in flight with a live
30 s lease -> InFlight; lapsed lease -> New). A take-over issues a new record id (the real store updates the row
in place), so tokens of the old and new owner stay distinct. `await_result` wakes on `complete` and polls every
250 ms on the clock. Bodies over 1 MiB are kept inline and listed in `large_bodies` (the real store moves them to
S3, IMPL-085).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Literal

import anyio
import anyio.lowlevel

from noema.core.clock import Clock, Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.principal import Principal
from noema.core.protocols.storage import Conflict, IdemToken, InFlight, New, Replay, StoredResponse

LEASE_S: Final = 30
POLL_S: Final = 0.25
INLINE_MAX_BYTES: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class _Record:
    record_id: str
    tenant_id: str
    project_id: str
    scope_hash: str
    key: str
    fingerprint: str
    state: Literal["in_flight", "completed", "not_stored"]
    owner: str
    lease_expires_at: datetime
    expires_at: datetime
    resource_id: str | None
    response: StoredResponse | None = None


def scope_hash(project_id: str, method: str, route: str) -> str:
    """spec-11 §5 scope hash."""
    return hashlib.sha256(f"{project_id}\n{method}\n{route}".encode()).hexdigest()


class FakeIdempotencyStore:
    """Implements `IdempotencyStore` (see the module docstring)."""

    def __init__(self, clock: Clock, ids: IdGen, *, ttl_s: int = 86_400) -> None:
        self._clock = clock
        self._ids = ids
        self._ttl_s = ttl_s
        self._records: dict[tuple[str, str, str, str], _Record] = {}
        self._by_id: dict[str, tuple[str, str, str, str]] = {}
        self._done = anyio.Event()
        self.large_bodies: set[str] = set()

    def _new(
        self, slot: tuple[str, str, str, str], principal: Principal, fingerprint: str, resource_id: str | None
    ) -> New:
        now = self._clock.now()
        rid = self._ids.new("idem")
        old = self._records.get(slot)
        if old is not None:
            self._by_id.pop(old.record_id, None)
        self._records[slot] = _Record(
            rid,
            principal.tenant_id,
            principal.project_id,
            slot[2],
            slot[3],
            fingerprint,
            "in_flight",
            f"fake:{principal.request_id}",
            now + timedelta(seconds=LEASE_S),
            now + timedelta(seconds=self._ttl_s),
            resource_id,
        )
        self._by_id[rid] = slot
        return New(IdemToken(scope_hash=slot[2], key=slot[3], record_id=rid))

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
        await anyio.lowlevel.checkpoint()
        slot = (principal.tenant_id, principal.project_id, scope_hash(principal.project_id, method, route), key)
        rec = self._records.get(slot)
        now = self._clock.now()
        if rec is None or rec.expires_at < now:
            return self._new(slot, principal, fingerprint, resource_id)
        if rec.fingerprint != fingerprint:
            return Conflict()
        if rec.state == "completed" and rec.response is not None:
            return Replay(rec.response)
        if rec.state == "not_stored" or rec.lease_expires_at <= now:
            return self._new(slot, principal, fingerprint, resource_id)
        return InFlight(IdemToken(rec.scope_hash, rec.key, rec.record_id), rec.resource_id)

    def _get(self, token: IdemToken) -> _Record | None:
        slot = self._by_id.get(token.record_id)
        return None if slot is None else self._records.get(slot)

    async def await_result(self, token: IdemToken, *, deadline: Deadline) -> StoredResponse:
        while True:
            rec = self._get(token)
            now = self._clock.now()
            if rec is None or rec.state == "not_stored" or (rec.state == "in_flight" and rec.lease_expires_at <= now):
                raise NoemaError(ErrorCode.INTERNAL_ERROR, "The original request did not complete.")
            if rec.state == "completed" and rec.response is not None:
                return rec.response
            if deadline.expired():
                raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The original request is still running.")
            done = self._done
            async with anyio.create_task_group() as tg:

                async def poll() -> None:
                    await self._clock.sleep(min(POLL_S, max(0.0, deadline.at - self._clock.monotonic())))
                    tg.cancel_scope.cancel()

                tg.start_soon(poll)
                await done.wait()
                tg.cancel_scope.cancel()

    async def heartbeat(self, token: IdemToken) -> bool:
        await anyio.lowlevel.checkpoint()
        rec = self._get(token)
        if rec is None or rec.state != "in_flight":
            return False
        slot = self._by_id[token.record_id]
        self._records[slot] = replace(rec, lease_expires_at=self._clock.now() + timedelta(seconds=LEASE_S))
        return True

    async def complete(self, token: IdemToken, resp: StoredResponse | None) -> None:
        with anyio.CancelScope(shield=True):
            await anyio.lowlevel.checkpoint()
        rec = self._get(token)
        if rec is None or rec.state != "in_flight":
            return  # idempotent; a record taken over by another owner is not ours to complete
        slot = self._by_id[token.record_id]
        if resp is None:
            self._records[slot] = replace(rec, state="not_stored")
        else:
            if resp.body is not None and len(resp.body) > INLINE_MAX_BYTES:
                self.large_bodies.add(rec.record_id)
            self._records[slot] = replace(rec, state="completed", response=resp)
        self._done.set()
        self._done = anyio.Event()
