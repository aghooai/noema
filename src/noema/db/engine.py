"""Postgres connections: asyncpg pools per database role, JSON codecs and role checks (spec-03 §1, spec-02 §7).

Every process connects as exactly one login role: the API and workers as `noema_app` (NOBYPASSRLS), the
`noema-admin` CLI as `noema_cli`, migrations as `noema_owner` (`noema.db.alembic.runner`). A connection whose
`current_user` is not the expected role, or is a superuser or has BYPASSRLS, is refused (`WrongDatabaseRole`), so a
mis-set DSN can never run the app with RLS switched off. `jsonb`/`json` values are Python JSON values both ways
(orjson). Postgres being unreachable surfaces as `NoemaError(internal_error)` (retryable), within the 2 s pool
timeout. The app pool (`app_pool_settings`, Wave E) also bounds every statement: the driver gives up after
`STATEMENT_TIMEOUT_S` (a connection that stopped answering: a failover or partition without RST), the server cancels
a statement after `statement_timeout` and a lock wait after `lock_timeout`; each is the same retryable
`internal_error` (a caller's own timeouts inside a block still propagate unchanged).

Release without the reset round trip (the app pool, Wave E3; `PoolSettings.reset_on_release`): asyncpg resets every
released connection with one more query (`SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL`), one
of the ~10 Postgres round trips of a turn (E sprint A §8.2). On the `noema_app` pool it has nothing to undo: every
unit of work is one transaction whose RLS scope is `set_config(..., true)` (transaction-local), the system calls are
single SECURITY DEFINER calls (their own transactions; no session setting), advisory locks are transaction-level
(`pg_advisory_xact_lock`), LISTEN runs on dedicated unpooled connections and nothing declares a holdable cursor. So
that pool skips the query; asyncpg still rolls back a transaction left open (a cancelled block) and drops listener
callbacks before the connection is reused (tests/integration/db/test_pool_reset.py proves a released connection
carries no `app.tenant_id` / `app.project_id` after a commit, a rollback, a cancelled block and a system call). Every
other pool (the CLI's, the owner's) keeps asyncpg's full reset.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import asyncpg
import orjson
from asyncpg.pool import PoolConnectionProxy

from noema.core.errors import ErrorCode, NoemaError
from noema.core.secret import SecretStr

ROLE_OWNER: Final = "noema_owner"
ROLE_APP: Final = "noema_app"
ROLE_CLI: Final = "noema_cli"
ROLE_DEFINER: Final = "noema_definer"
LOGIN_ROLES: Final = (ROLE_OWNER, ROLE_APP, ROLE_CLI)
POOL_TIMEOUT_S: Final = 2.0  # spec-02 §7: acquiring a connection gives up after 2 s
STATEMENT_TIMEOUT_S: Final = 10.0  # the app pool: one statement (the driver's command timeout)
APP_SERVER_SETTINGS: Final[Mapping[str, str]] = {"statement_timeout": "10s", "lock_timeout": "5s"}

type PooledConnection = PoolConnectionProxy[asyncpg.Record]
type DbConnection = asyncpg.Connection[asyncpg.Record] | PooledConnection

# Errors of an established connection that mean "Postgres went away" (as opposed to a failed statement).
LOST_CONNECTION_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
)
# Errors of a statement that Postgres (statement_timeout, lock_timeout) or the driver cancelled.
CANCELLED_STATEMENT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncpg.QueryCanceledError,
    asyncpg.LockNotAvailableError,
)
# Errors of connecting or acquiring that mean "Postgres is unreachable" (refused, timed out, full, restarting).
CONNECTION_ERRORS: Final[tuple[type[BaseException], ...]] = (
    OSError,
    TimeoutError,
    asyncpg.TooManyConnectionsError,
    *LOST_CONNECTION_ERRORS,
)


class WrongDatabaseRole(RuntimeError):
    """The DSN logs in as another role than the process expects, or as a superuser/BYPASSRLS role."""


def db_unavailable() -> NoemaError:
    """The error every Database entry point raises when Postgres cannot be reached (500, retryable)."""
    return NoemaError(ErrorCode.INTERNAL_ERROR, "The database is temporarily unavailable.")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _dumps(value: object) -> str:
    return orjson.dumps(value, default=_json_default).decode()


async def _init_connection(conn: asyncpg.Connection[asyncpg.Record], expected_role: str | None) -> None:
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(typename, schema="pg_catalog", encoder=_dumps, decoder=orjson.loads, format="text")
    if expected_role is None:
        return
    row = await conn.fetchrow(
        "SELECT current_user::text AS who, r.rolsuper, r.rolbypassrls FROM pg_catalog.pg_roles r"
        " WHERE r.rolname = current_user"
    )
    if row is None or row["who"] != expected_role or row["rolsuper"] or row["rolbypassrls"]:
        raise WrongDatabaseRole(f"this process must connect as {expected_role} (NOSUPERUSER, NOBYPASSRLS)")


async def _transaction_scoped_reset(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    """The pool's `reset` for connections that only ever carry transaction-scoped state (module docstring): nothing
    to send. asyncpg has already rolled back an open transaction and dropped listener callbacks before calling it."""


def _raised_by_driver(exc: BaseException) -> bool:
    """True when asyncpg itself raised `exc` (its command timeout), not the caller's code inside the block."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return tb is not None and str(tb.tb_frame.f_globals.get("__name__", "")).startswith("asyncpg")


def _is_gone(conn: PooledConnection) -> bool:
    try:
        return conn.is_closed()
    except asyncpg.InterfaceError:  # the pool already detached the proxy of a terminated connection
        return True


@dataclass(frozen=True, slots=True)
class PoolSettings:
    """One role's pool. `expected_role` None skips the role check (test administration only)."""

    dsn: SecretStr
    expected_role: str | None = ROLE_APP
    min_size: int = 0
    max_size: int = 20
    acquire_timeout_s: float = POOL_TIMEOUT_S
    connect_timeout_s: float = POOL_TIMEOUT_S
    command_timeout_s: float | None = None
    application_name: str = "noema"
    server_settings: Mapping[str, str] = field(default_factory=dict)
    reset_on_release: bool = True  # False: skip asyncpg's reset query on release (module docstring; the app pool)


def app_pool_settings(dsn: SecretStr, *, max_size: int) -> PoolSettings:
    """The `noema_app` pool of the processes: every statement bounded (see the module docstring)."""
    return PoolSettings(
        dsn=dsn,
        expected_role=ROLE_APP,
        max_size=max_size,
        command_timeout_s=STATEMENT_TIMEOUT_S,
        server_settings=APP_SERVER_SETTINGS,
        reset_on_release=False,
    )


class Engine:
    """An asyncpg pool for one role. Create with `await Engine.create(settings)`; `close()` at shutdown."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], settings: PoolSettings) -> None:
        self._pool = pool
        self._settings = settings

    @classmethod
    async def create(cls, settings: PoolSettings) -> Engine:
        """Builds the pool (connections open lazily when `min_size` is 0). Raises `NoemaError(internal_error)` if
        `min_size` > 0 and Postgres is unreachable."""
        expected = settings.expected_role

        async def init(conn: asyncpg.Connection[asyncpg.Record]) -> None:
            await _init_connection(conn, expected)

        try:
            pool = await asyncpg.create_pool(
                settings.dsn.get_secret_value(),
                min_size=settings.min_size,
                max_size=settings.max_size,
                timeout=settings.connect_timeout_s,
                command_timeout=settings.command_timeout_s,
                server_settings={"application_name": settings.application_name, **settings.server_settings},
                init=init,
                reset=None if settings.reset_on_release else _transaction_scoped_reset,
            )
        except CONNECTION_ERRORS as exc:
            raise db_unavailable() from exc
        return cls(pool, settings)

    @property
    def settings(self) -> PoolSettings:
        return self._settings

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PooledConnection]:
        """A pooled connection for the block. Failing to connect or acquire (refused, 2 s timeout, server full),
        losing the connection inside the block, and a statement the driver timed out or the server cancelled
        (statement or lock timeout) raise `NoemaError(internal_error)`; every other exception of the block
        (statement errors, the caller's own timeouts) propagates unchanged."""
        try:
            conn = await self._pool.acquire(timeout=self._settings.acquire_timeout_s)
        except CONNECTION_ERRORS as exc:
            raise db_unavailable() from exc
        try:
            yield conn
        except (*LOST_CONNECTION_ERRORS, *CANCELLED_STATEMENT_ERRORS) as exc:
            raise db_unavailable() from exc
        except TimeoutError as exc:
            if _raised_by_driver(exc):
                raise db_unavailable() from exc
            raise
        except asyncpg.InterfaceError as exc:  # e.g. a transaction exit on a connection the server closed
            if _is_gone(conn):
                raise db_unavailable() from exc
            raise
        finally:
            await self._pool.release(conn)

    async def close(self) -> None:
        """Closes every connection (graceful shutdown, spec-11 §7)."""
        await self._pool.close()


async def connect(
    dsn: SecretStr, *, expected_role: str | None, timeout_s: float = POOL_TIMEOUT_S
) -> asyncpg.Connection[asyncpg.Record]:
    """One unpooled connection with the JSON codecs (the `noema-admin` CLI as `noema_cli`; test administration).
    Raises `NoemaError(internal_error)` when unreachable and `WrongDatabaseRole` on a role mismatch."""
    try:
        conn = await asyncpg.connect(dsn.get_secret_value(), timeout=timeout_s)
    except CONNECTION_ERRORS as exc:
        raise db_unavailable() from exc
    try:
        await _init_connection(conn, expected_role)
    except BaseException:
        await conn.close()
        raise
    return conn


# --- provisioning (a superuser or equivalent administrator; never the app) ------------------------------------------
_ROLE_ATTRIBUTES: Final[Mapping[str, str]] = {
    ROLE_DEFINER: "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS",
    ROLE_OWNER: "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
    ROLE_APP: "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
    ROLE_CLI: "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
}
_PROVISION_LOCK: Final = 0x6E6F656D61  # pg_advisory_xact_lock key shared by every provisioning step ("noema")
_DATABASE_NAME_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def check_database_name(name: str) -> str:
    """`name` if it is a safe database identifier (`[a-z0-9_]`, 1-63 characters), else ValueError."""
    if not 0 < len(name) <= 63 or not set(name) <= _DATABASE_NAME_CHARS:
        raise ValueError("database names are 1-63 characters of [a-z0-9_]")
    return name


async def ensure_roles(admin: DbConnection, *, passwords: Mapping[str, SecretStr] | None = None) -> None:
    """Creates or corrects the four cluster-wide roles of spec-03 §1 (noema_definer NOLOGIN BYPASSRLS; the three
    login roles NOSUPERUSER NOBYPASSRLS; noema_owner a member of noema_definer WITH INHERIT FALSE, SET TRUE).
    Idempotent and serialized by an advisory lock. `passwords` (role -> password) sets login passwords; roles
    without an entry keep theirs. Needs a superuser connection (only superusers grant BYPASSRLS)."""
    async with admin.transaction():
        await admin.execute("SELECT pg_catalog.pg_advisory_xact_lock($1)", _PROVISION_LOCK)
        for role, attributes in _ROLE_ATTRIBUTES.items():
            exists = await admin.fetchval("SELECT true FROM pg_catalog.pg_roles WHERE rolname = $1", role)
            await admin.execute(f"{'ALTER' if exists else 'CREATE'} ROLE {role} {attributes}")
            secret = (passwords or {}).get(role)
            if secret is not None:
                statement = await admin.fetchval(
                    "SELECT pg_catalog.format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)",
                    role,
                    secret.get_secret_value(),
                )
                await admin.execute(str(statement))
        await admin.execute(f"GRANT {ROLE_DEFINER} TO {ROLE_OWNER} WITH INHERIT FALSE, SET TRUE")


async def secure_database(admin: DbConnection, name: str) -> None:
    """The database ACL of spec-03 §1: no PUBLIC privileges (CONNECT, TEMP), CONNECT for the three login roles. A
    database cloned from a template starts with the default ACL again, so every clone needs this."""
    db = check_database_name(name)
    await admin.execute(f'REVOKE ALL ON DATABASE "{db}" FROM PUBLIC')
    await admin.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {", ".join(LOGIN_ROLES)}')


async def create_database(admin: DbConnection, name: str, *, template: str | None = None) -> None:
    """`CREATE DATABASE name OWNER noema_owner [TEMPLATE template]`, then `secure_database`. Needs CREATEDB (a
    superuser in dev/test)."""
    db = check_database_name(name)
    clause = f' TEMPLATE "{check_database_name(template)}"' if template is not None else ""
    await admin.execute(f'CREATE DATABASE "{db}" OWNER {ROLE_OWNER}{clause}')
    await secure_database(admin, db)


async def drop_database(admin: DbConnection, name: str) -> None:
    """`DROP DATABASE IF EXISTS name WITH (FORCE)` (terminates its sessions)."""
    await admin.execute(f'DROP DATABASE IF EXISTS "{check_database_name(name)}" WITH (FORCE)')
