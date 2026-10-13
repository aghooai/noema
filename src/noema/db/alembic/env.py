"""Alembic environment (spec-03 §7): online migrations as noema_owner over asyncpg (SQLAlchemy's async engine).

The DSN comes from `Config.attributes["noema_dsn"]` (`noema.db.alembic.runner`) or `NOEMA_DB_OWNER_DSN` (the
alembic CLI, `make db-migrate`). Offline (`--sql`) mode is not supported: the revisions run SQL scripts on a live
connection.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from noema.core.secret import SecretStr
from noema.db.alembic.runner import DSN_ATTRIBUTE


def _dsn() -> str:
    configured = context.config.attributes.get(DSN_ATTRIBUTE)
    if isinstance(configured, SecretStr):
        return configured.get_secret_value()
    from_env = os.environ.get("NOEMA_DB_OWNER_DSN")
    if not from_env:
        raise RuntimeError("set NOEMA_DB_OWNER_DSN to the noema_owner DSN (spec-03 §7)")
    return from_env


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    url = make_url(_dsn()).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported; run them online as noema_owner")
asyncio.run(_main())
