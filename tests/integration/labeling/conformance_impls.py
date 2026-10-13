"""The real labeling implementations in the conformance suites (spec-02 §14): `labeling_sampler` (`Sampler`) and
`calibrator_registry` (`DbCalibratorRegistry`) over the fake database and over Postgres (noema_app, the definer
functions `ops.calibrator_epochs` / `ops.active_calibrators`). The Postgres kit activates artifacts the way the
CLI's two-person activation leaves them (status active, `activated_by` = two operators) and bumps the scope's epoch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tests.contract_internal.kits import CalibratorKit, SamplerKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.db import FakeDatabase
from tests.fakes.outbox import FakeOutbox
from tests.fixtures.db import seed_tenant
from tests.integration.labeling._pg import dedicated

from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.labeling import Calibrator
from noema.core.protocols.storage import OutboxRecord
from noema.db.engine import Engine, PoolSettings, connect
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.labeling.registry import DbCalibratorRegistry
from noema.labeling.sampler import Sampler


@register("labeling_sampler", "labeling")
@asynccontextmanager
async def sampler(h: Harness) -> AsyncIterator[SamplerKit]:
    def make(config: ConfigRegistry) -> tuple[Sampler, Any]:
        outbox = FakeOutbox()
        return Sampler(h.clock, config, outbox), lambda: list[OutboxRecord](outbox.records)

    yield SamplerKit(make)


def calibrator_row(cal: Calibrator, tenant_id: str | None) -> dict[str, Any]:
    """The `noema.calibrator` row of an active artifact (temperature or Platt pooled part)."""
    pooled = {"temperature": float(getattr(cal.pooled, "t", 1.0))}
    if cal.method == "platt":
        pooled = {"a": float(getattr(cal.pooled, "a", 1.0)), "b": float(getattr(cal.pooled, "b", 0.0))}
    return {
        "id": cal.id,
        "scope": cal.scope,
        "tenant_id": tenant_id if cal.scope == "tenant" else None,
        "profile_id": cal.key.profile_id,
        "profile_version": cal.key.profile_version,
        "question_name": "intent",
        "question_signature": cal.key.question_signature,
        "member_subset": list(cal.key.member_subset),
        "input_view_signature": cal.key.input_view_signature,
        "choice_method": cal.key.choice_method,
        "method": cal.method,
        # a stored row needs a T_m for every member of the subset (ADR-003 §1); the suite's artifacts carry them all
        "member_t": {m: float(cal.member_T[m]) for m in cal.key.member_subset},
        "pooled": pooled,
        "epsilon": cal.epsilon,
        "holdout": dict(cal.holdout),
        "acceptance": {"passed": True, "failed_rules": []},
        "reliability": {"bins": [], "ci": {}},
        "status": "active",
        "approved_by": "op_a",
        "activated_by": ["op_a", "op_b"],
    }


@register("calibrator_registry", "labeling-fakedb")
@asynccontextmanager
async def fakedb_registry(h: Harness) -> AsyncIterator[CalibratorKit]:
    db = FakeDatabase(h.clock)
    for scope_key in ("platform", "t_acme"):
        db.store.seed("calibrator_epoch", {"scope_key": scope_key, "n": 0})
    registry = DbCalibratorRegistry(db)

    async def add(cal: Calibrator, tenant_id: str | None) -> None:
        db.store.seed("calibrator", calibrator_row(cal, tenant_id))
        scope_key = tenant_id if cal.scope == "tenant" and tenant_id else "platform"
        current = db.store.table("calibrator_epoch").get((scope_key,), {"n": 0})
        db.store.seed("calibrator_epoch", {"scope_key": scope_key, "n": int(current["n"]) + 1})
        await registry.refresh()

    await registry.refresh()
    yield CalibratorKit(registry, add, "t_acme")


_INSERT = (
    "INSERT INTO noema.calibrator (id, scope, tenant_id, profile_id, profile_version, question_name,"
    " question_signature, member_subset, input_view_signature, choice_method, method, member_t, pooled, epsilon,"
    " holdout, acceptance,"
    " reliability, status, fitted_at, accepted_at, approved_by, activated_by, activated_at)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, now(), now(), $19, $20,"
    " now())"
)
_BUMP = (
    "INSERT INTO noema.calibrator_epoch (scope_key, n) VALUES ($1, 1)"
    " ON CONFLICT (scope_key) DO UPDATE SET n = noema.calibrator_epoch.n + 1"
)
_RETIRE_PLATFORM = (
    "UPDATE noema.calibrator SET status = 'retired', retired_at = now(), retired_reason = 'manual'"
    " WHERE scope = 'platform' AND status = 'active'"
)


@register("calibrator_registry", "labeling-postgres", marks=(pytest.mark.integration,))
@asynccontextmanager
async def postgres_registry(h: Harness) -> AsyncIterator[CalibratorKit]:
    database = await dedicated(h.request)
    admin = await connect(database.admin_dsn, expected_role=None)
    engine = await Engine.create(PoolSettings(dsn=database.app_dsn, max_size=2))
    try:
        tenant = (await seed_tenant(admin)).tenant_id
        await admin.execute(_RETIRE_PLATFORM)  # the suites reuse one binding key; start without a platform artifact
        await admin.execute(_BUMP, "platform")
        registry = DbCalibratorRegistry(PgDatabase(engine, make_unit_of_work))

        async def add(cal: Calibrator, tenant_id: str | None) -> None:
            r = calibrator_row(cal, tenant_id)
            if cal.scope == "platform":
                await admin.execute(_RETIRE_PLATFORM)
            await admin.execute("DELETE FROM noema.calibrator WHERE id = $1", cal.id)  # the suites reuse fixed ids
            await admin.execute(
                _INSERT,
                *(r[k] for k in ("id", "scope", "tenant_id", "profile_id", "profile_version", "question_name")),
                *(r[k] for k in ("question_signature", "member_subset", "input_view_signature", "choice_method")),
                *(r[k] for k in ("method", "member_t", "pooled", "epsilon", "holdout", "acceptance", "reliability")),
                r["status"],
                r["approved_by"],
                r["activated_by"],
            )
            await admin.execute(_BUMP, tenant_id if cal.scope == "tenant" and tenant_id else "platform")
            await registry.refresh()

        await registry.refresh()
        yield CalibratorKit(registry, add, tenant)
    finally:
        await engine.close()
        await admin.close()
