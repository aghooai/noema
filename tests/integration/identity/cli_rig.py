"""A `noema-admin` rig: a fresh migrated database per test (bootstrap is one-time per database), moto Secrets
Manager holding the API-key pepper, and an in-process runner returning (exit code, stdout JSON, stderr JSON)."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import pytest
from tests.fixtures.db import DEV_PASSWORDS, create_scratch_database, ensure_template
from tests.integration.identity.moto_support import moto_url, put_secret
from tests.integration.identity.pg_support import dsn
from tests.unit.identity.helpers import PEPPER_V1

from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, ROLE_CLI, ROLE_OWNER, connect, drop_database
from noema.identity.apikeys import PEPPER_REF, pepper_value
from noema.identity.cli.noema_admin import amain


@dataclass
class CliRig:
    environ: dict[str, str]
    admin: asyncpg.Connection[asyncpg.Record]  # superuser connection to the test database (inspection, seeding)
    app_dsn: SecretStr
    moto: str
    keys: dict[str, str] = field(default_factory=dict)  # operator name -> operator key

    async def run(self, *argv: str, operator: str | None = None) -> tuple[int, Any, Any]:
        out, err = io.StringIO(), io.StringIO()
        environ = dict(self.environ)
        if operator is not None:
            environ["NOEMA_OPERATOR_KEY"] = self.keys[operator]
        code = await amain(list(argv), environ=environ, stdout=out, stderr=err)
        return code, _json(out.getvalue()), _json(err.getvalue())

    async def ok(self, *argv: str, operator: str | None = "alice") -> Any:
        code, out, err = await self.run(*argv, operator=operator)
        assert code == 0, err
        return out

    async def operators(self) -> None:
        """Bootstrap alice, then alice creates bob (two people for the two-person rules)."""
        boot = await self.ok("bootstrap", "--operator", "alice", operator=None)
        self.keys["alice"] = boot["key"]
        bob = await self.ok("operators", "create", "--name", "bob")
        self.keys["bob"] = bob["key"]


def _json(text: str) -> Any:
    return json.loads(text) if text.strip() else None


@asynccontextmanager
async def open_cli_rig(
    noema_services: Any, config: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CliRig]:
    """The rig of one test (a fresh database, dropped afterwards)."""
    base = dsn(str(noema_services.pg_admin_dsn), dbname="postgres")
    template = await ensure_template(base)
    admin = await connect(SecretStr(base), expected_role=None)
    try:
        name = await create_scratch_database(admin, template=template)
    finally:
        await admin.close()
    url = moto_url(config)
    await put_secret(url, PEPPER_REF, pepper_value(1, PEPPER_V1).decode())
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")  # moto accepts any credentials (the CLI uses the default chain)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    def role(r: str) -> str:
        return dsn(base, dbname=name, user=r, password=DEV_PASSWORDS[r].get_secret_value())

    environ: Mapping[str, str] = {
        "NOEMA_ENV": "test",
        "NOEMA_CLI_DSN": role(ROLE_CLI),
        "NOEMA_DB_OWNER_DSN": role(ROLE_OWNER),
        "NOEMA_AWS_ENDPOINT_OVERRIDES": json.dumps({"secretsmanager": url}),
    }
    conn = await connect(SecretStr(dsn(base, dbname=name)), expected_role=None)
    try:
        yield CliRig(dict(environ), conn, SecretStr(role(ROLE_APP)), url)
    finally:
        await conn.close()
        admin = await connect(SecretStr(base), expected_role=None)
        try:
            await drop_database(admin, name)
        finally:
            await admin.close()
