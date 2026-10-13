"""A per-process scratch Postgres database for identity kits that run inside an event loop (the session `pg_database`
fixture is sync and cannot be requested from there). Cloned from the migrated template, dropped at session end."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from tests.fixtures.db import DEV_PASSWORDS, create_scratch_database, ensure_template

from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, ROLE_CLI, ROLE_OWNER, connect, drop_database


def dsn(base: str, *, dbname: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(base)
    user = user if user is not None else (parts.username or "")
    password = password if password is not None else (parts.password or "")
    host = parts.hostname or "127.0.0.1"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, ""))


@dataclass(frozen=True)
class ScratchDb:
    admin_dsn: SecretStr
    app_dsn: SecretStr
    cli_dsn: SecretStr
    owner_dsn: SecretStr


_DBS: dict[int, ScratchDb] = {}


def _drop(base: str, name: str) -> None:
    async def run() -> None:
        admin = await connect(SecretStr(base), expected_role=None)
        try:
            await drop_database(admin, name)
        finally:
            await admin.close()

    asyncio.run(run())


async def scratch_db(config: pytest.Config, admin_dsn: str) -> ScratchDb:
    """This process's scratch database (created on first use)."""
    found = _DBS.get(os.getpid())
    if found is not None:
        return found
    base = dsn(admin_dsn, dbname="postgres")
    template = await ensure_template(base)
    admin = await connect(SecretStr(base), expected_role=None)
    try:
        name = await create_scratch_database(admin, template=template)
    finally:
        await admin.close()
    config.add_cleanup(partial(_drop, base, name))

    def role(r: str) -> SecretStr:
        return SecretStr(dsn(base, dbname=name, user=r, password=DEV_PASSWORDS[r].get_secret_value()))

    found = ScratchDb(SecretStr(dsn(base, dbname=name)), role(ROLE_APP), role(ROLE_CLI), role(ROLE_OWNER))
    _DBS[os.getpid()] = found
    return found
