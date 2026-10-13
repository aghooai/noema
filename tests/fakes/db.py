"""`FakeDatabase` (spec-02 §7, §7.1): `uow`/`tenant_uow`/`system_uow` over the in-memory `FakeStore`.

`uow(principal)` scopes to the principal's project, or tenant-wide for tenant_admin/auditor principals (spec-03 §3);
`tenant_uow(t, project_id=None)` is tenant-wide; `system_uow()` exposes only the SECURITY DEFINER calls. A uow
commits on clean exit and rolls back (undo log) on any exception, cancellation included. `store.available = False`
makes all three raise `NoemaError(internal_error)` (Postgres down). Store semantics: `db_store.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Final

import anyio.lowlevel

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal

from .db_repos import (
    FakeAttemptRepo,
    FakeAuditRepo,
    FakeCalibratorRepo,
    FakeDecisionRepo,
    FakeDerivativeRepo,
    FakeFileRepo,
    FakeIdempotencyRepo,
    FakeJobRepo,
    FakeLabelRepo,
    FakeProjectRepo,
    FakeSessionRepo,
    FakeTurnRepo,
    FakeUsageRepo,
)
from .db_store import FakeStore, Scope, Tx
from .db_system import FakeSystemCalls

TENANT_WIDE_ROLES: Final = frozenset({"tenant_admin", "auditor"})


class FakeUnitOfWork:
    """Implements `UnitOfWork`: every repo of spec-02 §7.1 bound to one transaction and scope."""

    def __init__(self, tx: Tx, scope: Scope) -> None:
        self.scope = scope
        self._sessions = FakeSessionRepo(tx, scope)
        self._turns = FakeTurnRepo(tx, scope)
        self._attempts = FakeAttemptRepo(tx, scope)
        self._decisions = FakeDecisionRepo(tx, scope)
        self._files = FakeFileRepo(tx, scope)
        self._derivatives = FakeDerivativeRepo(tx, scope)
        self._usage = FakeUsageRepo(tx, scope)
        self._labels = FakeLabelRepo(tx, scope)
        self._calibrators = FakeCalibratorRepo(tx, scope)
        self._idempotency = FakeIdempotencyRepo(tx, scope)
        self._audit = FakeAuditRepo(tx, scope)
        self._projects = FakeProjectRepo(tx, scope)
        self._jobs = FakeJobRepo(tx, scope)

    @property
    def sessions(self) -> FakeSessionRepo:
        return self._sessions

    @property
    def turns(self) -> FakeTurnRepo:
        return self._turns

    @property
    def attempts(self) -> FakeAttemptRepo:
        return self._attempts

    @property
    def decisions(self) -> FakeDecisionRepo:
        return self._decisions

    @property
    def files(self) -> FakeFileRepo:
        return self._files

    @property
    def derivatives(self) -> FakeDerivativeRepo:
        return self._derivatives

    @property
    def usage(self) -> FakeUsageRepo:
        return self._usage

    @property
    def labels(self) -> FakeLabelRepo:
        return self._labels

    @property
    def calibrators(self) -> FakeCalibratorRepo:
        return self._calibrators

    @property
    def idempotency(self) -> FakeIdempotencyRepo:
        return self._idempotency

    @property
    def audit(self) -> FakeAuditRepo:
        return self._audit

    @property
    def projects(self) -> FakeProjectRepo:
        return self._projects

    @property
    def jobs(self) -> FakeJobRepo:
        return self._jobs


class FakeDatabase:
    """Implements `Database` over one `FakeStore` (shared by every fake that persists, e.g. the job queue)."""

    def __init__(self, clock: Clock, store: FakeStore | None = None) -> None:
        self._clock = clock
        self.store = store if store is not None else FakeStore(clock.now)

    def _check(self) -> None:
        if not self.store.available:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The database is unavailable.")

    @asynccontextmanager
    async def _uow(self, scope: Scope) -> AsyncIterator[FakeUnitOfWork]:
        await anyio.lowlevel.checkpoint()
        self._check()
        tx = Tx(self.store)
        try:
            yield FakeUnitOfWork(tx, scope)
        except BaseException:
            tx.rollback()
            raise
        tx.commit()

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[FakeUnitOfWork]:
        wide = bool(principal.roles & TENANT_WIDE_ROLES)
        return self._uow(Scope(principal.tenant_id, "" if wide else principal.project_id))

    def tenant_uow(
        self, tenant_id: str, *, project_id: str | None = None
    ) -> AbstractAsyncContextManager[FakeUnitOfWork]:
        return self._uow(Scope(tenant_id, project_id or ""))

    @asynccontextmanager
    async def _system(self) -> AsyncIterator[FakeSystemCalls]:
        await anyio.lowlevel.checkpoint()
        self._check()
        yield FakeSystemCalls(self.store)

    def system_uow(self) -> AbstractAsyncContextManager[FakeSystemCalls]:
        return self._system()

    def now(self) -> datetime:
        return self._clock.now()
