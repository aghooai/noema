"""Postgres test fixtures (spec-10 §1.1, spec-03 §6): a migrated template per migration hash, one clone per session.

- `pg_template` (session): under `pg_advisory_lock`, ensures the four roles (dev passwords) and the template database
  `noema_tpl_<migration hash[:16]>`, migrated once as noema_owner and then closed to connections; stale test
  databases (older than 6 h, or of a dead process on this host) are reaped on the way.
- `pg_database` (session): the clone `noema_t_<agent>_<worker>_<pid>_<token>` (owner noema_owner, the spec-03 §1
  database ACL), dropped at session end. Tests connect as `noema_app` (no BYPASSRLS, FORCE RLS) through
  `pg_app_engine` / `pg_db`; `pg_admin` is a superuser connection for seeding past RLS and catalog checks.
- Rows persist for the whole session: create your own tenant ids (`new_tenant_id()`, `seed_tenant()`).

The root conftest registers this module as a pytest plugin (`pytest_plugins`), so every test package gets these
fixtures without importing them.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

from noema.core.secret import SecretStr
from noema.db.alembic.runner import migration_hash, upgrade
from noema.db.engine import (
    ROLE_APP,
    ROLE_CLI,
    ROLE_OWNER,
    Engine,
    PoolSettings,
    check_database_name,
    connect,
    create_database,
    drop_database,
    ensure_roles,
)
from noema.db.uow import PgDatabase, Tx

__all__ = [
    "DEV_PASSWORDS",
    "PgTestDatabase",
    "SeededTenant",
    "clone_name",
    "create_scratch_database",
    "ensure_template",
    "new_tenant_id",
    "pg_admin",
    "pg_app_engine",
    "pg_cli",
    "pg_database",
    "pg_db",
    "pg_owner",
    "pg_template",
    "reap",
    "reap_stale_databases",
    "seed_tenant",
    "stale_clone",
    "template_name",
]

# Dev/test-only passwords of the cluster-wide login roles (the compose Postgres is disposable, .env.example).
DEV_PASSWORDS: Final[Mapping[str, SecretStr]] = {
    ROLE_OWNER: SecretStr(os.environ.get("NOEMA_DB_OWNER_PASSWORD", "noema-owner-dev-only"), register=True),
    ROLE_APP: SecretStr(os.environ.get("NOEMA_DB_APP_PASSWORD", "noema-app-dev-only"), register=True),
    ROLE_CLI: SecretStr(os.environ.get("NOEMA_DB_CLI_PASSWORD", "noema-cli-dev-only"), register=True),
}
TEMPLATE_PREFIX: Final = "noema_tpl_"
CLONE_PREFIX: Final = "noema_t_"
STALE_AFTER: Final = timedelta(hours=6)
_LOCK_KEY: Final = 0x6E6F656D6174  # pg_advisory_lock key of template builds and clones ("noemat")


def _with(dsn: str, *, user: str | None = None, password: str | None = None, dbname: str | None = None) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    netloc_host = f"[{host}]" if ":" in host else host
    port = f":{parts.port}" if parts.port else ""
    user = user if user is not None else (parts.username or "")
    password = password if password is not None else (parts.password or "")
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if password else f"{quote(user, safe='')}@"
    path = f"/{dbname}" if dbname is not None else parts.path
    return urlunsplit((parts.scheme, f"{auth}{netloc_host}{port}", path, parts.query, ""))


def template_name() -> str:
    """`noema_tpl_<first 16 hex of the migration hash>`."""
    return f"{TEMPLATE_PREFIX}{migration_hash()[:16]}"


def clone_name(*, agent: str, worker: str, pid: int, token: str) -> str:
    """`noema_t_<agent>_<worker>_<pid>_<token>` in `[a-z0-9_]`, at most 63 characters (the agent part is shortened).
    `token` keeps two fixture instances of one process apart (packages that each import these fixtures)."""

    def safe(text: str) -> str:
        return "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in text.lower())

    tail = f"_{safe(worker)}_{pid}_{safe(token)}"
    return check_database_name(f"{CLONE_PREFIX}{safe(agent)[: 63 - len(CLONE_PREFIX) - len(tail)]}{tail}")


@dataclass(frozen=True)
class PgTestDatabase:
    """The session's database and a DSN per role."""

    name: str
    admin_dsn: SecretStr  # superuser on this database: seeding past RLS, catalog checks
    owner_dsn: SecretStr
    app_dsn: SecretStr
    cli_dsn: SecretStr

    def dsn_for(self, role: str) -> SecretStr:
        return {ROLE_OWNER: self.owner_dsn, ROLE_APP: self.app_dsn, ROLE_CLI: self.cli_dsn}[role]


def _role_dsn(admin_dsn: str, role: str, dbname: str) -> SecretStr:
    return SecretStr(_with(admin_dsn, user=role, password=DEV_PASSWORDS[role].get_secret_value(), dbname=dbname))


async def _comment(admin: asyncpg.Connection[asyncpg.Record], db: str, meta: Mapping[str, object]) -> None:
    text = await admin.fetchval("SELECT pg_catalog.format('%L', $1::text)", json.dumps(meta, sort_keys=True))
    await admin.execute(f'COMMENT ON DATABASE "{check_database_name(db)}" IS {text}')


async def _databases(admin: asyncpg.Connection[asyncpg.Record], prefix: str) -> dict[str, dict[str, object]]:
    records = await admin.fetch(
        "SELECT d.datname, pg_catalog.shobj_description(d.oid, 'pg_database') AS meta FROM pg_catalog.pg_database d"
        " WHERE starts_with(d.datname, $1)",
        prefix,
    )
    found: dict[str, dict[str, object]] = {}
    for r in records:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except ValueError:
            meta = {}
        found[r["datname"]] = meta if isinstance(meta, dict) else {}
    return found


def _created_at(meta: Mapping[str, object]) -> datetime | None:
    raw = meta.get("created_at")
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _pid_namespace() -> str | None:
    """This process's PID namespace (`pid:[<inode>]`), None without /proc (then the host check alone applies)."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _provenance() -> dict[str, object]:
    """The comment of a clone or scratch database: created_at and the owner's pid, host and PID namespace."""
    now = datetime.now(UTC).isoformat()
    return {"created_at": now, "pid": os.getpid(), "host": socket.gethostname(), "pidns": _pid_namespace()}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale_clone(meta: Mapping[str, object], *, now: datetime, host: str, pidns: str | None) -> bool:
    """Whether the reaper drops a clone with this provenance comment: none, older than 6 h, or made on `host` in
    the PID namespace `pidns` by a process that is gone."""
    created = _created_at(meta)
    pid = meta.get("pid")
    here = meta.get("host") == host and meta.get("pidns") == pidns
    return created is None or now - created > STALE_AFTER or (here and isinstance(pid, int) and not _pid_alive(pid))


async def _drop_any(admin: asyncpg.Connection[asyncpg.Record], name: str) -> None:
    exists = await admin.fetchval("SELECT true FROM pg_catalog.pg_database WHERE datname = $1", name)
    if exists:
        await admin.execute(
            f'ALTER DATABASE "{check_database_name(name)}" WITH IS_TEMPLATE false ALLOW_CONNECTIONS true'
        )
        await drop_database(admin, name)


async def reap_stale_databases(admin: asyncpg.Connection[asyncpg.Record], *, now: datetime, keep: str) -> list[str]:
    """Drops clones older than 6 h or of a dead process on this host, and templates other than `keep` older than
    6 h (spec-10 §1.1). A pid counts as dead only when its clone was made in this PID namespace: a sandboxed agent
    with the same hostname has its own pids (ops-ledger request 7). Call under the fixture lock. Returns the
    dropped names."""
    dropped: list[str] = []
    host, pidns = socket.gethostname(), _pid_namespace()
    for name, meta in (await _databases(admin, CLONE_PREFIX)).items():
        if stale_clone(meta, now=now, host=host, pidns=pidns):
            await _drop_any(admin, name)
            dropped.append(name)
    for name, meta in (await _databases(admin, TEMPLATE_PREFIX)).items():
        created = _created_at(meta)
        if name != keep and (created is None or now - created > STALE_AFTER):
            await _drop_any(admin, name)
            dropped.append(name)
    return dropped


async def _locked(admin_dsn: str) -> asyncpg.Connection[asyncpg.Record]:
    admin = await connect(SecretStr(admin_dsn), expected_role=None)
    await admin.execute("SELECT pg_catalog.pg_advisory_lock($1)", _LOCK_KEY)
    return admin


async def reap(admin_dsn: str) -> list[str]:
    """`reap_stale_databases` under the fixture lock, keeping the current template (tools/reap_test_dbs.py)."""
    admin = await _locked(admin_dsn)
    try:
        return await reap_stale_databases(admin, now=datetime.now(UTC), keep=template_name())
    finally:
        await admin.close()


async def ensure_template(admin_dsn: str) -> str:
    """The migrated template database of this migration hash (created under the advisory lock if missing or
    half-built; stale test databases are reaped on the way); returns its name. Async counterpart of the sync
    `pg_template` fixture, for code that already runs in an event loop (conformance kits). `admin_dsn`: a
    superuser DSN of the cluster."""
    tpl = template_name()
    admin = await _locked(admin_dsn)
    try:
        await ensure_roles(admin, passwords=DEV_PASSWORDS)
        now = datetime.now(UTC)
        await reap_stale_databases(admin, now=now, keep=tpl)
        if not (await _databases(admin, tpl)).get(tpl, {}).get("ready"):
            await _drop_any(admin, tpl)  # a half-built template of a crashed session
            await create_database(admin, tpl)
            await asyncio.to_thread(upgrade, _role_dsn(admin_dsn, ROLE_OWNER, tpl))
            await _comment(admin, tpl, {"ready": True, "created_at": now.isoformat(), "hash": migration_hash()})
            await admin.execute(f'ALTER DATABASE "{tpl}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false')
        return tpl
    finally:
        await admin.close()  # also releases the session-level advisory lock


async def _create_clone(admin_dsn: str, template: str, name: str) -> None:
    admin = await _locked(admin_dsn)
    try:
        await _drop_any(admin, name)
        await create_database(admin, name, template=template)
        await _comment(admin, name, _provenance())
    finally:
        await admin.close()


async def create_scratch_database(admin: asyncpg.Connection[asyncpg.Record], *, template: str | None = None) -> str:
    """An extra empty (or cloned) test database with the provenance comment the reaper reads, so a concurrent
    session never reaps it while this process lives. Created and commented under the fixture lock (taken on
    `admin`; re-entrant if `admin` holds it already), so a reaper never sees it without its comment. The caller
    drops it (`drop_database`)."""
    name = check_database_name(f"{CLONE_PREFIX}scratch_{os.getpid()}_{uuid.uuid4().hex[:12]}")
    await admin.execute("SELECT pg_catalog.pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await create_database(admin, name, template=template)
        await _comment(admin, name, _provenance())
    finally:
        await admin.execute("SELECT pg_catalog.pg_advisory_unlock($1)", _LOCK_KEY)
    return name


async def _drop_clone(admin_dsn: str, name: str) -> None:
    admin = await connect(SecretStr(admin_dsn), expected_role=None)
    try:
        await drop_database(admin, name)
    finally:
        await admin.close()


def _admin_base(noema_services: object) -> str:
    return _with(str(getattr(noema_services, "pg_admin_dsn")), dbname="postgres")  # noqa: B009


@pytest.fixture(scope="session")
def pg_template(noema_services: object) -> str:
    """The migrated template database of the current migration hash (built once across all sessions)."""
    return asyncio.run(ensure_template(_admin_base(noema_services)))


@pytest.fixture(scope="session")
def pg_database(pg_template: str, noema_services: object, worker_id: str) -> Iterator[PgTestDatabase]:
    """This session's clone of the template; dropped at session end."""
    admin_base = _admin_base(noema_services)
    agent = os.environ.get("NOEMA_AGENT_ID") or "local"
    worker = worker_id if worker_id != "master" else "w0"
    name = clone_name(agent=agent, worker=worker, pid=os.getpid(), token=uuid.uuid4().hex[:6])
    asyncio.run(_create_clone(admin_base, pg_template, name))
    yield PgTestDatabase(
        name=name,
        admin_dsn=SecretStr(_with(admin_base, dbname=name)),
        owner_dsn=_role_dsn(admin_base, ROLE_OWNER, name),
        app_dsn=_role_dsn(admin_base, ROLE_APP, name),
        cli_dsn=_role_dsn(admin_base, ROLE_CLI, name),
    )
    asyncio.run(_drop_clone(admin_base, name))


@pytest.fixture
async def pg_app_engine(pg_database: PgTestDatabase) -> AsyncIterator[Engine]:
    """A small noema_app pool on the session database (role-checked: NOSUPERUSER, NOBYPASSRLS)."""
    engine = await Engine.create(PoolSettings(dsn=pg_database.app_dsn, max_size=4))
    try:
        yield engine
    finally:
        await engine.close()


@pytest.fixture
async def pg_db(pg_app_engine: Engine) -> PgDatabase[Tx]:
    """`PgDatabase` whose units of work are the bare `Tx` (connection + scope); repos build their own factory."""
    return PgDatabase(pg_app_engine, lambda tx: tx)


@pytest.fixture
async def pg_admin(pg_database: PgTestDatabase) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
    """A superuser connection to the session database (bypasses RLS: seeding and inspection only)."""
    conn = await connect(pg_database.admin_dsn, expected_role=None)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def pg_cli(pg_database: PgTestDatabase) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
    """A noema_cli connection (EXECUTE on admin.* and auth.lookup_operator only)."""
    conn = await connect(pg_database.cli_dsn, expected_role=ROLE_CLI)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def pg_owner(pg_database: PgTestDatabase) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
    """A noema_owner connection (FORCE RLS binds it too)."""
    conn = await connect(pg_database.owner_dsn, expected_role=ROLE_OWNER)
    try:
        yield conn
    finally:
        await conn.close()


# --- seeding --------------------------------------------------------------------------------------------------------
def new_tenant_id() -> str:
    """A tenant id unique within the session database."""
    return f"t_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class SeededTenant:
    tenant_id: str
    project_ids: tuple[str, ...]
    principal_ids: Mapping[str, str] = field(default_factory=dict)  # project id -> its principal id


async def seed_tenant(
    admin: asyncpg.Connection[asyncpg.Record],
    tenant_id: str | None = None,
    *,
    projects: tuple[str, ...] = ("prj_a",),
    roles: tuple[str, ...] = ("developer",),
) -> SeededTenant:
    """Inserts a live tenant, its projects and one principal per project (as superuser, past RLS)."""
    tid = tenant_id or new_tenant_id()
    await admin.execute("INSERT INTO noema.tenant (id, aws_account_id) VALUES ($1, '111122223333')", tid)
    principals: dict[str, str] = {}
    for project in projects:
        await admin.execute("INSERT INTO noema.project (tenant_id, id, name) VALUES ($1, $2, $2)", tid, project)
        principal = f"prn_{uuid.uuid4().hex[:12]}"
        await admin.execute(
            "INSERT INTO noema.principal (tenant_id, project_id, id, subject, roles) VALUES ($1, $2, $3, $3, $4)",
            tid,
            project,
            principal,
            list(roles),
        )
        principals[project] = principal
    return SeededTenant(tenant_id=tid, project_ids=projects, principal_ids=principals)
