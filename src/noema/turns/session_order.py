"""Session ordering (spec-04 §2; IMPL-022): the session lease with a FIFO wait.

`acquire()` runs `try_once` (the `session_pending` check, then `SessionRepo.acquire`); when busy it joins the
Redis FIFO queue and retries when it is the head, woken by `session_released` or a 50 ms poll, until
`platform.sessions.queue_timeout_ms` -> 409 `session_busy`. 404 `session_not_found` and 409 `session_conflict`
come from the repository and never wait. Redis down: polling only (no fairness, no pending check).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Final

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.storage import Busy, Database, SessionLease, UnitOfWork
from noema.turns.coordination import Coordinator

# a read in the acquiring unit of work, at the locked last_seq (E sprint A: the lease and the history in one
# transaction); it raising rolls the lease back
Then = Callable[[UnitOfWork, SessionLease], Awaitable[object]]

log = logging.getLogger("noema.turns")

LEASE_S: Final = 30
RENEW_EVERY_S: Final = 10.0
POLL_S: Final = 0.05


async def try_once(
    db: Database,
    coordinator: Coordinator,
    principal: Principal,
    *,
    session_id: str,
    turn_id: str,
    expected_seq: int | None,
) -> SessionLease | None:
    """One attempt: busy (None) while a finalize is pending in the outbox, else the repository's UPDATE.
    Raises 404 `session_not_found` / 409 `session_conflict`."""
    lease, _ = await _try(db, coordinator, principal, session_id, turn_id, expected_seq, None)
    return lease


async def _try(
    db: Database,
    coordinator: Coordinator,
    principal: Principal,
    session_id: str,
    turn_id: str,
    expected_seq: int | None,
    then: Then | None,
) -> tuple[SessionLease | None, object]:
    """`try_once`, then `then(uow, lease)` in the unit of work that took the lease (its result second)."""
    if await coordinator.session_pending(session_id):
        async with db.uow(principal) as uow:  # a missing session must still be 404, not busy
            if await uow.sessions.get(session_id) is None:
                raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.", param="session_id")
        return None, None
    async with db.uow(principal) as uow:
        result = await uow.sessions.acquire(session_id, turn_id, expected_seq=expected_seq, lease_s=LEASE_S)
        if isinstance(result, Busy):
            return None, None
        return result, (await then(uow, result) if then is not None else None)


async def acquire(
    db: Database,
    coordinator: Coordinator,
    clock: Clock,
    principal: Principal,
    *,
    session_id: str,
    turn_id: str,
    expected_seq: int | None,
    queue_timeout_ms: int,
) -> SessionLease:
    """The spec-04 §2 algorithm (see the module docstring)."""
    lease, _ = await acquire_then(
        db,
        coordinator,
        clock,
        principal,
        session_id=session_id,
        turn_id=turn_id,
        expected_seq=expected_seq,
        queue_timeout_ms=queue_timeout_ms,
        then=None,
    )
    return lease


async def acquire_then(
    db: Database,
    coordinator: Coordinator,
    clock: Clock,
    principal: Principal,
    *,
    session_id: str,
    turn_id: str,
    expected_seq: int | None,
    queue_timeout_ms: int,
    then: Then | None,
) -> tuple[SessionLease, object]:
    """`acquire`, running `then(uow, lease)` in the unit of work that takes the lease (one transaction: the
    caller's read sees the session at the locked `last_seq`); returns the lease and `then`'s result."""
    t_end = clock.monotonic() + queue_timeout_ms / 1000
    lease, held = await _try(db, coordinator, principal, session_id, turn_id, expected_seq, then)
    if lease is not None:
        return lease, held
    joined = await coordinator.queue_join(session_id, turn_id, queue_timeout_ms)
    try:
        while (remaining := t_end - clock.monotonic()) > 0:
            head = await coordinator.queue_head(session_id, turn_id, queue_timeout_ms) if joined else None
            if head is None or head == turn_id:
                lease, held = await _try(db, coordinator, principal, session_id, turn_id, expected_seq, then)
                if lease is not None:
                    return lease, held
            await coordinator.wait_released(session_id, min(POLL_S, remaining))
    finally:
        if joined:
            await coordinator.queue_leave(session_id, turn_id)
    raise NoemaError(
        ErrorCode.SESSION_BUSY,
        "Another turn holds this session; retry shortly.",
        param="session_id",
        retry_after_s=1,
    )


async def release(db: Database, coordinator: Coordinator, principal: Principal, lease: SessionLease) -> None:
    """`SessionRepo.release` (a 429 at admission, a failed start) and wake the next waiter. Never raises."""
    try:
        async with db.uow(principal) as uow:
            await uow.sessions.release(lease)
    except Exception:  # the lease then expires by itself (30 s)
        return
    await coordinator.publish_released(principal.tenant_id, lease.session_id, None)


async def renew(db: Database, principal: Principal, lease: SessionLease) -> bool:
    """`SessionRepo.renew(lease, 30)`; a DB error (connection loss, statement timeout, serialization failure, ...)
    counts as still held: logged, and the next renewal retries."""
    try:
        async with db.uow(principal) as uow:
            return await uow.sessions.renew(lease, LEASE_S)
    except Exception:
        log.warning("session lease renewal of turn %s failed; retrying", lease.turn_id, exc_info=True)
        return True
