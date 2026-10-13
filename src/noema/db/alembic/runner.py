"""Alembic entry points, the migration hash and the SQL-script runner of the revisions (spec-03 §6-§7).

Migrations run as `noema_owner` (`make db-migrate`), never by the app. Each revision executes its `sql/<revision>/`
scripts in file-name order on the migration connection, inside alembic's single transaction. Production is
forward-only: `downgrade` exists for CI and tests and refuses outside `NOEMA_ENV` dev/test.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Final

from alembic import command, op
from alembic.config import Config
from sqlalchemy.util import await_

from noema.core.errors import ForbiddenInEnvironment
from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, ROLE_CLI, ROLE_OWNER, connect, create_database, ensure_roles, secure_database

ALEMBIC_DIR: Final = Path(__file__).resolve().parent
SQL_DIR: Final = ALEMBIC_DIR / "sql"
INI_PATH: Final = ALEMBIC_DIR.parent / "alembic.ini"
DSN_ATTRIBUTE: Final = "noema_dsn"  # Config.attributes key read by env.py
_HASHED_SUFFIXES: Final = frozenset({".py", ".sql", ".mako"})
_NOEMA_SCHEMAS: Final = ("noema", "auth", "jobs", "ops", "admin", "platform")


def migration_hash() -> str:
    """sha256 (hex) over every revision, SQL script and environment file under `noema/db/alembic` (relative path and
    bytes). It names the migrated test template database `noema_tpl_<hash[:16]>` (spec-03 §7)."""
    digest = hashlib.sha256()
    files = sorted(
        p
        for p in ALEMBIC_DIR.rglob("*")
        if p.is_file() and p.suffix in _HASHED_SUFFIXES and "__pycache__" not in p.parts
    )
    for path in files:
        digest.update(path.relative_to(ALEMBIC_DIR).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def alembic_config(owner_dsn: SecretStr) -> Config:
    """An alembic Config for this package that connects with `owner_dsn` (the noema_owner DSN)."""
    config = Config(str(INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.attributes[DSN_ATTRIBUTE] = owner_dsn
    return config


def upgrade(owner_dsn: SecretStr, revision: str = "head") -> None:
    """`alembic upgrade <revision>` as noema_owner. Sync: it runs its own event loop, so call it from sync code (a
    CLI, a session fixture) or a worker thread."""
    command.upgrade(alembic_config(owner_dsn), revision)


def downgrade(owner_dsn: SecretStr, revision: str = "base") -> None:
    """`alembic downgrade <revision>`; CI/test only (raises ForbiddenInEnvironment outside NOEMA_ENV dev/test)."""
    _require_disposable_env()
    command.downgrade(alembic_config(owner_dsn), revision)


def run_sql_scripts(revision_dir: str) -> None:
    """Inside a revision's `upgrade()`: executes `sql/<revision_dir>/*.sql` in name order on the migration
    connection. Scripts may hold several statements and dollar-quoted bodies (asyncpg simple-query protocol)."""
    scripts = sorted((SQL_DIR / revision_dir).glob("*.sql"))
    if not scripts:
        raise FileNotFoundError(f"no SQL scripts under {SQL_DIR / revision_dir}")
    op.execute("SELECT 1")  # starts the transaction on this connection before the driver-level scripts run
    driver = op.get_bind().connection.driver_connection
    if driver is None:
        raise RuntimeError("the migration connection has no driver connection")
    for script in scripts:
        await_(driver.execute(script.read_text(encoding="utf-8")))


def run_downgrade_scripts(revision_dir: str) -> None:
    """Inside a revision's `downgrade()` (CI/test only, refused outside NOEMA_ENV dev/test): executes
    `sql/<revision_dir>/down/*.sql` in name order, the reverse of that revision's scripts."""
    _require_disposable_env()
    run_sql_scripts(f"{revision_dir}/down")


def drop_noema_schemas() -> None:
    """Inside a revision's `downgrade()`: drops the six noema schemas with everything in them (CI/test only)."""
    _require_disposable_env()
    op.execute(f"DROP SCHEMA IF EXISTS {', '.join(_NOEMA_SCHEMAS)} CASCADE")


def _require_disposable_env() -> None:
    if os.environ.get("NOEMA_ENV") not in ("dev", "test"):
        raise ForbiddenInEnvironment("schema downgrades are CI/test-only; production migrations are forward-only")


# --- command line (make db-bootstrap / db-migrate / db-hash) ---------------------------------------------------------
def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or default
    if not value:
        raise SystemExit(f"{name} is required")
    return value


async def _bootstrap(admin_dsn: SecretStr, database: str, passwords: dict[str, SecretStr]) -> None:
    admin = await connect(admin_dsn, expected_role=None)
    try:
        await ensure_roles(admin, passwords=passwords)
        if await admin.fetchval("SELECT true FROM pg_catalog.pg_database WHERE datname = $1", database):
            await secure_database(admin, database)
        else:
            await create_database(admin, database)
    finally:
        await admin.close()


def main(argv: list[str] | None = None) -> int:
    """`python -m noema.db.alembic.runner bootstrap|migrate|hash`.

    bootstrap: roles and database via NOEMA_PG_ADMIN_DSN (a superuser), NOEMA_DB_NAME and
    NOEMA_DB_{OWNER,APP,CLI}_PASSWORD (dev defaults only when NOEMA_ENV is dev/test). migrate: `upgrade head` with
    NOEMA_DB_OWNER_DSN. hash: prints the migration hash.
    """
    command_name = (argv if argv is not None else sys.argv[1:] or [""])[0]
    if command_name == "hash":
        sys.stdout.write(migration_hash() + "\n")
    elif command_name == "migrate":
        upgrade(SecretStr(_env("NOEMA_DB_OWNER_DSN"), register=True))
    elif command_name == "bootstrap":
        dev = os.environ.get("NOEMA_ENV") in ("dev", "test")
        passwords = {
            role: SecretStr(_env(f"NOEMA_DB_{role.removeprefix('noema_').upper()}_PASSWORD",
                                 f"{role.replace('_', '-')}-dev-only" if dev else None), register=True)
            for role in (ROLE_OWNER, ROLE_APP, ROLE_CLI)
        }  # fmt: skip
        admin_dsn = SecretStr(_env("NOEMA_PG_ADMIN_DSN"), register=True)
        asyncio.run(_bootstrap(admin_dsn, _env("NOEMA_DB_NAME", "noema"), passwords))
    else:
        sys.stderr.write("usage: python -m noema.db.alembic.runner bootstrap|migrate|hash\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
