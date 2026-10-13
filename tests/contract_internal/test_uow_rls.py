"""Database / UnitOfWork conformance (spec-02 §7, spec-03 §3): tenant and project scoping, tenant-wide roles,
commit/rollback, and the Postgres-down error."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError

from .kits import DatabaseKit, principal
from .registry import kit_fixture
from .rows import seed_tenancy, session

kit = kit_fixture("database")


async def test_scopes(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    await seed_tenancy(kit, "t_other", ("p1",))
    dev_p1, dev_p2 = principal(), principal(project_id="p2", principal_id="prn_dev2")
    admin = principal(roles=("tenant_admin",), principal_id="prn_admin")
    async with kit.db.uow(dev_p1) as uow:
        await uow.sessions.create(session("ses_a"))
    async with kit.db.uow(dev_p2) as uow:
        await uow.sessions.create(session("ses_b", project_id="p2", owner="prn_dev2"))
        assert await uow.sessions.get("ses_a") is None  # other project
    async with kit.db.uow(admin) as uow:
        assert await uow.sessions.get("ses_a") is not None and await uow.sessions.get("ses_b") is not None
    async with kit.db.uow(principal(tenant_id="t_other")) as uow:
        assert await uow.sessions.get("ses_a") is None  # other tenant
    async with kit.db.tenant_uow("t_acme") as uow:
        assert await uow.sessions.get("ses_b") is not None
    async with kit.db.tenant_uow("t_acme", project_id="p1") as uow:
        assert await uow.sessions.get("ses_b") is None


async def test_rollback_on_exception(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    with pytest.raises(RuntimeError, match="abort"):  # noqa: PT012 (the uow must see the exception)
        async with kit.db.uow(principal()) as uow:
            await uow.sessions.create(session("ses_rb"))
            raise RuntimeError("abort")
    async with kit.db.uow(principal()) as uow:
        assert await uow.sessions.get("ses_rb") is None


async def test_write_outside_scope_fails(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    await seed_tenancy(kit, "t_other", ("p1",))
    with pytest.raises(Exception):  # noqa: B017, PT011 (an RLS violation: the driver's exception type)
        async with kit.db.uow(principal()) as uow:
            await uow.sessions.create(session("ses_x", tenant_id="t_other"))


async def test_database_down(kit: DatabaseKit) -> None:
    if kit.available is None:
        pytest.skip("no outage hook")
    kit.available(False)
    try:
        for factory in (lambda: kit.db.uow(principal()), lambda: kit.db.tenant_uow("t_acme"), kit.db.system_uow):
            with pytest.raises(NoemaError) as err:
                async with factory():
                    pass
            assert err.value.code is ErrorCode.INTERNAL_ERROR and err.value.spec.retryable
    finally:
        kit.available(True)
