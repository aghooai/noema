"""noema.db (spec-01 §1, spec-03): Postgres engine and roles, units of work under RLS, the SECURITY DEFINER calls.

- `engine`: `Engine`/`PoolSettings` (asyncpg pool for one role, role-checked), `connect`, provisioning
  (`ensure_roles`, `create_database`, `secure_database`, `drop_database`).
- `uow`: `PgDatabase[U]` (the `Database` Protocol), `Tx`, `Scope`, `scope_for`.
- `system`: `PgSystemCalls` (`SystemCalls`), `PgAdminCalls` and `AdminError` (the noema-admin functions).
- `alembic.runner`: migrations (`upgrade`, `downgrade`, `migration_hash`); imported on demand (pulls in alembic).
- `repos`: the repositories (spec-02 §7.1), the only other SQL of the app.
"""

from noema.db.engine import (
    CONNECTION_ERRORS,
    LOGIN_ROLES,
    LOST_CONNECTION_ERRORS,
    ROLE_APP,
    ROLE_CLI,
    ROLE_DEFINER,
    ROLE_OWNER,
    DbConnection,
    Engine,
    PooledConnection,
    PoolSettings,
    WrongDatabaseRole,
    connect,
    db_unavailable,
)
from noema.db.system import AdminError, AdminReason, PgAdminCalls, PgSystemCalls
from noema.db.uow import TENANT_WIDE_ROLES, PgDatabase, Scope, Tx, scope_for

__all__ = [
    "CONNECTION_ERRORS",
    "LOGIN_ROLES",
    "LOST_CONNECTION_ERRORS",
    "ROLE_APP",
    "ROLE_CLI",
    "ROLE_DEFINER",
    "ROLE_OWNER",
    "TENANT_WIDE_ROLES",
    "AdminError",
    "AdminReason",
    "DbConnection",
    "Engine",
    "PgAdminCalls",
    "PgDatabase",
    "PgSystemCalls",
    "PoolSettings",
    "PooledConnection",
    "Scope",
    "Tx",
    "WrongDatabaseRole",
    "connect",
    "db_unavailable",
    "scope_for",
]
