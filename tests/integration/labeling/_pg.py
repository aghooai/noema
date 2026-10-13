"""A dedicated Postgres database per test process for the labeling conformance kit (cloned from the migrated
template inside the running event loop, dropped at session end), plus the DSN helper."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from tests.fixtures.db import DEV_PASSWORDS, create_scratch_database, ensure_template

from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, connect, drop_database


def dsn(base: str, *, dbname: str, user: str | None = None, password: str | None = None) -> str:
    """`base` with another database (and optionally another login)."""
    parts = urlsplit(base)
    user = user if user is not None else (parts.username or "")
    password = password if password is not None else (parts.password or "")
    host = parts.hostname or "127.0.0.1"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, ""))


@dataclass(frozen=True)
class Dedicated:
    admin_dsn: SecretStr
    app_dsn: SecretStr


_BY_PID: dict[int, Dedicated] = {}


def _drop(base: str, name: str) -> None:
    async def run() -> None:
        admin = await connect(SecretStr(base), expected_role=None)
        try:
            await drop_database(admin, name)
        finally:
            await admin.close()

    asyncio.run(run())


async def dedicated(request: pytest.FixtureRequest) -> Dedicated:
    """This process's labeling database (created on first use)."""
    found = _BY_PID.get(os.getpid())
    if found is not None:
        return found
    base = dsn(str(request.getfixturevalue("noema_services").pg_admin_dsn), dbname="postgres")
    template = await ensure_template(base)
    admin = await connect(SecretStr(base), expected_role=None)
    try:
        name = await create_scratch_database(admin, template=template)
    finally:
        await admin.close()
    request.config.add_cleanup(partial(_drop, base, name))
    password = DEV_PASSWORDS[ROLE_APP].get_secret_value()
    found = Dedicated(
        SecretStr(dsn(base, dbname=name)), SecretStr(dsn(base, dbname=name, user=ROLE_APP, password=password))
    )
    _BY_PID[os.getpid()] = found
    return found
