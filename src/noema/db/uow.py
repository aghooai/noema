"""Units of work: one Postgres transaction under one RLS scope (spec-02 §7, spec-03 §3; IMPL-009).

`PgDatabase` implements the `Database` Protocol. Each `uow`/`tenant_uow` checks out one pooled `noema_app`
connection, opens a transaction and, as its first statement, sets `app.tenant_id` and `app.project_id` with
`set_config(name, $value, true)` (transaction-local, bind parameters, never a string-built `SET LOCAL`). The RLS
policies then confine every statement to that tenant (and project, unless tenant-wide). Clean exit commits, an
exception rolls back. After the transaction both settings revert to '', which matches no row.

The repositories are not built here: `PgDatabase` takes a `unit_of_work` factory that turns the open `Tx` into the
`UnitOfWork` (the db repos package provides it; tests may pass any factory).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from noema.core.principal import Principal, Role
from noema.db.engine import Engine, PooledConnection
from noema.db.system import PgSystemCalls

if TYPE_CHECKING:
    from noema.core.protocols.storage import Database, SystemCalls, UnitOfWork

# Principals with one of these roles see the whole tenant: app.project_id = '' (spec-03 §3, spec-09 §3).
TENANT_WIDE_ROLES: Final[frozenset[Role]] = frozenset({"tenant_admin", "auditor"})

_SET_SCOPE: Final = (
    "SELECT pg_catalog.set_config('app.tenant_id', $1, true), pg_catalog.set_config('app.project_id', $2, true)"
)


@dataclass(frozen=True, slots=True)
class Scope:
    """The RLS scope of one transaction. `project_id` '' = tenant-wide."""

    tenant_id: str
    project_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("a unit of work needs a non-empty tenant_id")


def scope_for(principal: Principal) -> Scope:
    """`app.project_id = principal.project_id`, or '' iff the principal has tenant_admin or auditor. A
    project-scoped principal without a project is a bug (ValueError), never a silent tenant-wide scope."""
    if principal.roles & TENANT_WIDE_ROLES:
        return Scope(principal.tenant_id, "")
    if not principal.project_id:
        raise ValueError("a principal without tenant_admin/auditor needs a project_id")
    return Scope(principal.tenant_id, principal.project_id)


@dataclass(frozen=True, slots=True)
class Tx:
    """The open transaction of a unit of work: run every statement on `conn`; fill tenant_id/project_id of new
    rows from `scope` (RLS rejects anything else)."""

    conn: PooledConnection
    scope: Scope

    @property
    def tenant_id(self) -> str:
        return self.scope.tenant_id

    @property
    def project_id(self) -> str:
        """'' when the unit of work is tenant-wide."""
        return self.scope.project_id


class PgDatabase[U]:
    """The `Database` implementation over one `noema_app` Engine. `unit_of_work(tx)` builds the object a
    `uow`/`tenant_uow` block receives (the repositories; `PgDatabase[UnitOfWork]` is the production type).
    All three entry points raise `NoemaError(internal_error)` (retryable) when Postgres is unreachable."""

    def __init__(self, engine: Engine, unit_of_work: Callable[[Tx], U]) -> None:
        self._engine = engine
        self._unit_of_work = unit_of_work

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[U]:
        """One transaction scoped to the principal's tenant and project (tenant-wide for tenant_admin/auditor)."""
        return self._transaction(scope_for(principal))

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AbstractAsyncContextManager[U]:
        """One transaction for jobs, sweepers, drainer follow-ups and labeling; project_id None = tenant-wide."""
        return self._transaction(Scope(tenant_id, project_id or ""))

    @asynccontextmanager
    async def system_uow(self) -> AsyncIterator[PgSystemCalls]:
        """A connection with no tenant scope (RLS shows it nothing): only the SECURITY DEFINER functions granted to
        noema_app and the two platform reads; each call is its own transaction."""
        async with self._engine.acquire() as conn:
            yield PgSystemCalls(conn)

    @asynccontextmanager
    async def _transaction(self, scope: Scope) -> AsyncIterator[U]:
        async with self._engine.acquire() as conn, conn.transaction():
            await conn.execute(_SET_SCOPE, scope.tenant_id, scope.project_id)
            yield self._unit_of_work(Tx(conn, scope))


if TYPE_CHECKING:  # static conformance with the frozen Protocols (spec-02 §7)

    def _conforms(db: PgDatabase[UnitOfWork], calls: PgSystemCalls) -> tuple[Database, SystemCalls]:
        return db, calls
