"""Wave E2 (finding 17, ruling R7): Postgres text cannot hold U+0000, so an id or cursor holding it cannot exist.
The lookups by id answer None without a statement (every caller then answers its own 404, never the 500 of the
driver's encoding error) and a cursor holding it is malformed (400 `schema_violation`, `param` `after`)."""

from __future__ import annotations

import base64

import orjson
import pytest
from tests.fixtures.db import SeededTenant
from tests.integration.db_repos._rows import principal

from noema.core.errors import ErrorCode, NoemaError
from noema.db.repos import PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration


async def test_an_id_holding_nul_is_not_found(repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant) -> None:
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        assert await u.files.get("file_\x00") is None
        assert await u.sessions.get("ses_\x00") is None
        assert await u.turns.get("turn_\x00") is None
        assert await u.decisions.get("dec_\x00") is None
        assert await u.sessions.get("ses_unknown") is None  # the transaction is still usable


async def test_a_cursor_holding_nul_is_malformed(repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant) -> None:
    cursor = base64.urlsafe_b64encode(orjson.dumps(["ses_\x00"])).decode().rstrip("=")
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        with pytest.raises(NoemaError) as err:
            await u.sessions.list(owner_principal_id=None, after=cursor, limit=10)
    assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, "after")
